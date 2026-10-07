import asyncio
import os
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .engine import Engine
from .models import Account, Group, ProxyList, Settings, Task, Profile, Mailbox, Solver, InputList
from .identity import test_mailbox
from .retailers import catalog, RETAILERS
from .services import ProxyHealth, SolverService
from .store import Store, now
from .resources import Resources
from .analytics import report
from .models import ResourceFolder
from .recovery import diagnose, validate_candidate
from .browser_bridge import inspect_session
from .proxy_pool import ProxyPool
from .models import AIConnection, account_fingerprint_settings
from .ai_provider import AIProvider, ProviderError

ROOT = Path(__file__).resolve().parent.parent
MODELS = {"accounts": Account, "groups": Group, "proxies": ProxyList, "tasks": Task, "settings": Settings,
          "folders": ResourceFolder, "profiles": Profile, "mailboxes": Mailbox, "solvers": Solver, "input_lists": InputList,
          "ai_connections": AIConnection}


def create_app(data_dir=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(Path(data_dir or os.environ.get("RETAIL_DATA", ROOT / "data")))
        app.state.resources = Resources(app.state.store)
        app.state.resources.migrate()
        app.state.proxy_pool = ProxyPool(app.state.store)
        app.state.proxy_pool.sync()
        app.state.engine = Engine(app.state.store)
        app.state.proxy_health = ProxyHealth(app.state.store)
        for record in app.state.store.all("harvesters"):
            app.state.store.delete("harvesters", record["id"])
        for record in app.state.store.all("proxy_health"):
            if record.get("status") == "testing":
                app.state.store.put("proxy_health", {**record, "status": "interrupted"})
        await app.state.engine.boot()
        yield
        await app.state.engine.close()
        await app.state.proxy_health.close()
        app.state.store.db.close()

    app = FastAPI(title="Retail Desk", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("x-retail-client") != "dashboard":
                return JSONResponse({"detail": "Local dashboard header required"}, 403)
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "Cross-origin request denied"}, 403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'"
        return response

    def store():
        return app.state.store

    def require(kind, id):
        result = store().get(kind, id)
        if result is None:
            raise HTTPException(404, "Record not found")
        return result

    def validate_task(valid, id=None):
        group = require("groups", valid["group_id"])
        account = require("accounts", valid["account_id"]) if valid["account_id"] else None
        if not valid["simulation"]:
            if not account or not (account.get("session") or account.get("password")):
                raise HTTPException(422, "Live tasks need a saved session or credentials for automatic login")
            if not RETAILERS[group.get("retailer", "amazon")].get("automation"):
                raise HTTPException(422, "This retailer adapter is planned; use simulation until it is implemented")
            if valid["checkout_mode"] in ("automatic", "quote") and account.get("region") != "US":
                raise HTTPException(422, "Automatic checkout and final-price quotes currently require Amazon US")
        if account and account.get("retailer", "amazon") != group.get("retailer", "amazon"):
            raise HTTPException(422, "Account and group retailer must match")
        for field, target in [("proxy_id", "proxies"), ("profile_id", "profiles"), ("solver_id", "solvers")]:
            if valid[field]:
                require(target, valid[field])
        if id in app.state.engine.jobs:
            raise HTTPException(409, "Stop the task before editing")
        valid.update(status="scheduled" if valid["scheduled_at"] else "idle", message="Scheduled" if valid["scheduled_at"] else "Ready to start", updated_at=now())
        return valid

    def public(kind, value):
        value = dict(value)
        if kind == "accounts":
            value.pop("session", None)
            value['fingerprint_overrides'] = {k: v for k, v in value.get('fingerprint_overrides', {}).items() if v is not None}
            value.pop("session_storage", None)
            value["has_proxy"] = bool(value.pop("proxy", ""))
            value["has_password"] = bool(value.pop("password", ""))
            value["has_totp"] = bool(value.pop("totp_secret", ""))
            value["has_cvv"] = bool(value.pop("cvv", ""))
        if kind == "profiles":
            number = value.pop("card_number", "")
            value["card_last4"] = number[-4:]
        if kind == "mailboxes":
            value["has_password"] = bool(value.pop("password", ""))
        if kind in ("solvers", "ai_connections"):
            value["has_api_key"] = bool(value.pop("api_key", ""))
        if kind == "proxies":
            value["count"] = len([x for x in value.pop("entries", "").splitlines() if x.strip()])
        if kind == "settings":
            value["has_webhook"] = bool(value.pop("webhook", ""))
        return value

    @app.get("/api/state")
    async def state():
        result = {kind: [public(kind, x) for x in store().all(kind)] for kind in [*MODELS, "feed", "checkouts", "quotes", "proxy_health", "harvesters", "submissions"]}
        result["events"] = store().all("events")[-150:][::-1]
        result["active"] = list(app.state.engine.jobs)
        result['fingerprint_tests'] = list(app.state.engine.amazon.fingerprint_tests)
        result['settings'] = [public('settings', {**Settings().model_dump(), **(store().get('settings', 'settings') or {})})]
        result["retailers"] = catalog()
        result["memberships"] = [{"folder_id":f["id"],"resource_id":i} for f in store().all("folders") for i in app.state.resources.members(f["id"])]
        result["account_profiles"] = app.state.resources.links()
        result["task_events"] = store().all("task_events")[-250:]
        result["sessions"] = store().all("sessions")
        result['browser_health'] = store().all('browser_health')
        result["repairs"] = store().all("repairs")
        result["agent_runs"] = store().all("agent_runs")[-100:]
        result["proxy_endpoints"] = [{k:v for k,v in p.items() if k!="connection"} for p in store().all("proxy_endpoints")]
        result["diagnostics"] = [{k:v for k,v in d.items() if k not in ("dom","accessibility","screenshot","trace")} for d in store().all("diagnostics")][-100:]
        return result

    @app.post("/api/{kind}")
    @app.put("/api/{kind}/{id}")
    async def save(kind: str, request: Request, id: str | None = None):
        if kind not in MODELS:
            raise HTTPException(404, "Unknown collection")
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(422, "Expected a JSON object")
        old = require(kind, id) if id else {}
        if kind == "settings":
            id = "settings"
            old = {**Settings().model_dump(), **(store().get(kind, id) or {})}
            expected = data.pop('_expected', {})
            if not isinstance(expected, dict):
                raise HTTPException(422, 'Invalid settings revision')
            if any(key in data and old.get(key) != value for key, value in expected.items()):
                raise HTTPException(409, 'These settings changed in another window. Discard your draft to reload the saved values, then try again.')
        # Redacted secrets are preserved when omitted from edits.
        merged = {**old, **data}
        if kind == 'ai_connections':
            if id and not data.get('api_key') and not data.get('clear_api_key'):
                merged['api_key'] = old.get('api_key', '')
            if data.get('clear_api_key'):
                merged['api_key'] = ''
        if kind == 'settings':
            if merged.get('ai_connection_id'):
                require('ai_connections', merged['ai_connection_id'])
            if merged.get('agent_mode', 'off') != 'off' and not merged.get('ai_connection_id'):
                raise HTTPException(422, 'Select an AI connection before enabling the browser agent')
            if app.state.engine.jobs and any(merged.get(k) != old.get(k) for k in ('cdp_attach','cdp_endpoint','browser_channel','show_browser_window','fingerprint_backend','native_browser_executable','agent_mode','ai_connection_id','max_running_tasks')):
                raise HTTPException(409, 'Stop running tasks before changing browser or AI connections')
        if kind == "groups" and not id and "delay_ms" not in data:
            merged["delay_ms"] = (store().get("settings", "settings") or {}).get("default_monitor_delay", 4500)
        if kind == "folders" and old and data.get("resource_kind",old["resource_kind"]) != old["resource_kind"]:
            raise HTTPException(422, "Folder resource type cannot change")
        if kind == "accounts" and id in app.state.engine.amazon.logins:
            raise HTTPException(409, "Close or save this account's open browser before editing")
        try:
            valid = MODELS[kind].model_validate(merged).model_dump(mode="json")
            if kind == 'accounts':
                account_fingerprint_settings(store().get('settings', 'settings') or {}, valid)
            elif kind == 'settings':
                for account in store().all('accounts'):
                    account_fingerprint_settings(valid, account)
        except ValidationError as exc:
            raise HTTPException(422, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in exc.errors()))
        if kind == "groups" and id:
            if any(t["group_id"] == id and t["id"] in app.state.engine.jobs for t in store().all("tasks")) and any(valid.get(k) != Group.model_validate(old).model_dump(mode="json").get(k) for k in data if k not in ("delay_ms", "retry_delay_ms", "highlight", "schedule")):
                raise HTTPException(409, "Stop this group's tasks before editing")
        if kind in ("accounts", "proxies") and id:
            field = "account_id" if kind == "accounts" else "proxy_id"
            if any(t.get(field) == id and t["id"] in app.state.engine.jobs for t in store().all("tasks")):
                raise HTTPException(409, "Stop tasks using this record before editing")
        if kind == "proxies" and app.state.proxy_health.busy(id):
            raise HTTPException(409, "Wait for the proxy check to finish before editing")
        if kind == "input_lists" and id and any(g.get("input_list_id") == id and any(t["group_id"] == g["id"] and t["id"] in app.state.engine.jobs for t in store().all("tasks")) for g in store().all("groups")):
            raise HTTPException(409, "Stop tasks using this input list before editing")
        if kind == "accounts":
            for field, target in [("mailbox_id", "mailboxes"), ("solver_id", "solvers"), ("proxy_list_id", "proxies")]:
                if valid[field]:
                    require(target, valid[field])
            if valid["retailer"] != old.get("retailer", valid["retailer"]):
                old.pop("session", None)
                old.pop('session_storage', None)
                old["logged_in"] = False
        if kind == "groups":
            if "schedule" in data:
                valid["schedule"]["configured_at"] = now()
            if id and valid["retailer"] != old.get("retailer") and any(t["group_id"] == id for t in store().all("tasks")):
                raise HTTPException(409, "Remove tasks before changing the group site")
            if valid["monitor_proxy_id"]:
                require("proxies", valid["monitor_proxy_id"])
            if valid["input_list_id"]:
                linked = require("input_lists", valid["input_list_id"])
                if linked["retailer"] != valid["retailer"]:
                    raise HTTPException(422, "Input list and group retailer must match")
        if kind == "accounts" and (valid["region"] != old.get("region", valid["region"]) or valid["email"] != old.get("email", valid["email"])):
            old.pop("session", None)
            old.pop('session_storage', None)
            old["logged_in"] = False
        if kind == "tasks":
            validate_task(valid, id)
        folder_id = data.get("folder_id")
        if folder_id:
            folder = require("folders", folder_id)
            if folder["resource_kind"] != kind:
                raise HTTPException(422, "Folder type does not match the item")
        if kind == 'ai_connections' and any(valid.get(key) != old.get(key) for key in ('api_key', 'model', 'provider', 'protocol', 'base_url')):
            old.pop('health', None)
            old.pop('browser_health', None)
        result = store().put(kind, {**old, **valid}, id)
        if kind == 'settings' and result.get('max_running_tasks') != old.get('max_running_tasks'):
            app.state.engine.browser_slots = asyncio.Semaphore(result['max_running_tasks'])
        if folder_id:
            app.state.resources.add(folder_id, [result["id"]])
        if kind=="proxies": app.state.proxy_pool.sync()
        if kind == 'settings' and any(result.get(k) != old.get(k) for k in ('cdp_attach', 'cdp_endpoint', 'browser_channel', 'show_browser_window', 'fingerprint_backend', 'native_browser_executable')):
            if not app.state.engine.jobs:
                await app.state.engine.amazon.close()
        return public(kind, result)

    @app.delete("/api/{kind}/{id}")
    async def delete(kind: str, id: str):
        if kind not in MODELS or kind == "settings":
            raise HTTPException(404, "Unknown collection")
        require(kind, id)
        if kind == 'ai_connections' and (store().get('settings', 'settings') or {}).get('ai_connection_id') == id:
            raise HTTPException(409, 'Deselect this AI connection in Settings before deleting it')
        if kind == "tasks":
            await app.state.engine.stop(id)
        if kind in ("groups", "accounts", "proxies"):
            field = {"groups": "group_id", "accounts": "account_id", "proxies": "proxy_id"}[kind]
            if any(t.get(field) == id for t in store().all("tasks")):
                raise HTTPException(409, "Delete associated tasks first")
        dependencies = {"profiles": [("tasks", "profile_id")], "mailboxes": [("accounts", "mailbox_id")],
                        "solvers": [("accounts", "solver_id"), ("tasks", "solver_id")],
                        "input_lists": [("groups", "input_list_id")], "proxies": [("groups", "monitor_proxy_id"), ("accounts", "proxy_list_id")]}
        for collection, field in dependencies.get(kind, []):
            if any(x.get(field) == id for x in store().all(collection)):
                raise HTTPException(409, "Unlink associated records before deleting")
        if kind == "proxies" and app.state.proxy_health.busy(id):
            raise HTTPException(409, "Wait for the proxy check to finish")
        if kind == "accounts" and id in app.state.engine.amazon.logins:
            await app.state.engine.amazon.logins.pop(id).close()
        if kind == 'accounts':
            await app.state.engine.amazon.close_fingerprint_test(id)
        app.state.resources.cleanup(kind, id)
        store().delete(kind, id)
        return {"ok": True}

    @app.post("/api/tasks/{id}/{action}")
    async def task_action(id: str, action: str):
        require("tasks", id)
        try:
            if action == "start":
                await app.state.engine.start(id)
            elif action == "stop":
                await app.state.engine.stop(id)
            elif action == "resume":
                app.state.engine.resume(id)
            elif action == "focus":
                await app.state.engine.focus(id)
            elif action == "hide":
                await app.state.engine.hide(id)
            else:
                raise HTTPException(404, "Unknown action")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True}

    @app.get('/api/tasks/{id}/live-frame')
    async def task_live_frame(id: str, request: Request):
        if request.headers.get('x-retail-client') != 'dashboard':
            raise HTTPException(403, 'Local dashboard header required')
        require('tasks', id)
        try:
            frame = await app.state.engine.live_frame(id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(content=frame, media_type='image/jpeg')

    @app.get('/api/browser/{scope}/{id}/frame')
    async def browser_frame(scope: str, id: str, request: Request):
        if request.headers.get('x-retail-client') != 'dashboard':
            raise HTTPException(403, 'Local dashboard header required')
        if scope not in ('tasks', 'accounts'):
            raise HTTPException(404, 'Unknown browser scope')
        require(scope, id)
        try:
            frame = await app.state.engine.browser_frame(scope, id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(content=frame, media_type='image/jpeg')

    @app.post('/api/browser/{scope}/{id}/input')
    async def browser_input(scope: str, id: str, request: Request):
        if scope not in ('tasks', 'accounts'):
            raise HTTPException(404, 'Unknown browser scope')
        require(scope, id)
        try:
            action = await request.json()
            if not isinstance(action, dict):
                raise ValueError('Invalid browser input')
            return await app.state.engine.browser_input(scope, id, action)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/accounts/{id}/{action}")
    async def account_action(id: str, action: str):
        account = require("accounts", id)
        if action in ("login", "register", "save-session") and any(t.get("account_id") == id and t["id"] in app.state.engine.jobs for t in store().all("tasks")):
            raise HTTPException(409, "Stop this account's running tasks before changing its session")
        if account.get("retailer", "amazon") != "amazon" and action not in ('test-fingerprint', 'close-fingerprint-test'):
            raise HTTPException(409, "Account browser automation for this retailer is planned")
        try:
            if action == 'test-fingerprint':
                return await app.state.engine.amazon.test_fingerprint(account)
            elif action == 'close-fingerprint-test':
                await app.state.engine.amazon.close_fingerprint_test(id)
            elif action == "login":
                await app.state.engine.amazon.login(account)
            elif action == "save-session":
                await app.state.engine.amazon.save_login(account)
            elif action == "register":
                await app.state.engine.amazon.register(account)
            elif action == "otp":
                return await app.state.engine.amazon.identities.code(account)
            elif action == "fill-otp":
                context = app.state.engine.amazon.logins.get(id)
                if not context or not context.pages:
                    raise ValueError("Open the account browser first")
                await app.state.engine.amazon.fill_otp(context.pages[0], account)
            elif action == "close-browser":
                context = app.state.engine.amazon.logins.pop(id, None)
                if context:
                    await context.close()
            else:
                raise HTTPException(404, "Unknown action")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(409, "Browser action failed. Run python -m patchright install chromium, then retry; complete any Amazon verification in the browser.")
        return {"ok": True}

    @app.post("/api/mailboxes/{id}/test")
    async def mailbox_test(id: str):
        mailbox = require("mailboxes", id)
        try:
            result = await asyncio.to_thread(test_mailbox, mailbox)
        except Exception as exc:
            raise HTTPException(409, f"IMAP connection failed ({type(exc).__name__}); check host, TLS port and app password")
        store().put("mailboxes", {**mailbox, "tested_at": now()})
        return result

    @app.post("/api/solvers/{id}/{action}")
    async def solver_action(id: str, action: str, request: Request):
        solver = require("solvers", id)
        try:
            if action == "test":
                result = await SolverService().health(solver)
            elif action == "diagnostic" and solver["provider"] == "flaresolverr":
                body = await request.json()
                retailer = body.get("retailer", "amazon")
                if retailer not in RETAILERS:
                    raise ValueError("Unknown retailer")
                result = await SolverService().flare_fetch(solver, retailer)
            else:
                raise HTTPException(404, "Unknown solver action")
            store().put("solvers", {**solver, "health": result, "tested_at": now()})
            return result
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(409, f"Solver request failed ({type(exc).__name__}); check service availability and credentials")

    @app.post("/api/proxies/{id}/test")
    async def proxy_test(id: str, request: Request):
        record = require("proxies", id)
        body = await request.json()
        retailer = body.get("retailer", "amazon")
        mode = body.get("mode", "connectivity")
        if retailer not in RETAILERS:
            raise HTTPException(422, "Unknown retailer")
        if mode not in ("connectivity", "browser"):
            raise HTTPException(422, "Proxy check mode must be connectivity or browser")
        try:
            await app.state.proxy_health.start(record, retailer, mode)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        label = "Raw connectivity" if mode == "connectivity" else "Browser-session"
        return {"ok": True, "message": f"{label} proxy checks started"}

    @app.post("/api/import/{kind}")
    async def import_records(kind: str, request: Request):
        if kind not in ("accounts", "profiles", "input_lists"):
            raise HTTPException(404, "Unsupported import collection")
        body = await request.json()
        if not isinstance(body, list) or not 1 <= len(body) <= 100:
            raise HTTPException(422, "Import a JSON array containing 1–100 records")
        try:
            records = [MODELS[kind].model_validate(value).model_dump(mode="json") for value in body]
        except ValidationError:
            raise HTTPException(422, "Import validation failed. No records were imported; check the required fields.")
        if kind == "accounts":
            for value in records:
                for field, collection in [("mailbox_id", "mailboxes"), ("solver_id", "solvers"), ("proxy_list_id", "proxies")]:
                    if value.get(field):
                        require(collection, value[field])
        return {"created": [public(kind, record) for record in store().put_many(kind, records)]}

    @app.post("/api/account-batches/create")
    async def account_batch(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise HTTPException(422, "Enter accounts, one per line")
        lines = [line.strip() for line in body["text"].splitlines() if line.strip()]
        if not 1 <= len(lines) <= 100:
            raise HTTPException(422, "Enter 1?100 accounts")
        records = []
        for index, line in enumerate(lines, 1):
            parts = line.split(";")
            login, separator, password = parts[0].partition(":")
            amazon = body.get("retailer", "amazon") == "amazon"
            if not login or not separator or not password or len(parts) > (4 if amazon else 3):
                raise HTTPException(422, f"Line {index}: use login:password;proxy;secret" + (";cvv" if amazon else ""))
            try:
                record = Account(name=login, email=login, password=password, retailer=body.get("retailer", "amazon"),
                                 group=body.get("group", "Personal"), proxy=parts[1] if len(parts)>1 else "",
                                 totp_secret=parts[2] if len(parts)>2 else "", cvv=parts[3] if len(parts)>3 else "").model_dump(mode="json")
            except ValidationError:
                raise HTTPException(422, f"Line {index}: invalid account, proxy, authenticator secret or CVV. Nothing imported.")
            records.append(record)
        folder_id=body.get("folder_id")
        if folder_id and require("folders",folder_id)["resource_kind"]!="accounts": raise HTTPException(422,"Choose an account folder")
        created=store().put_many("accounts",records)
        if folder_id:app.state.resources.add(folder_id,[r["id"] for r in created])
        return {"created": [public("accounts", r) for r in created]}

    @app.post("/api/task-batches/create")
    async def task_batch(request: Request):
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(422, "Expected a JSON object")
        group = require("groups", data.get("group_id", ""))
        scope = data.get("account_group_scope", "")
        if scope:
            account_ids = [a["id"] for a in store().all("accounts") if a.get("group") == scope and a.get("retailer", "amazon") == group.get("retailer", "amazon")]
        else:
            account_ids = data.get("account_ids", [])
        if not isinstance(account_ids, list) or not 1 <= len(account_ids) <= 100 or any(not isinstance(x, str) for x in account_ids):
            raise HTTPException(422, "Choose 1–100 matching accounts")
        quantity = data.get("task_count", 1)
        if type(quantity) is not int or not 1 <= quantity <= 100 or len(set(account_ids)) * quantity > 100:
            raise HTTPException(422, "Create between 1 and 100 tasks per batch")
        records = []
        try:
            for account_id in dict.fromkeys(account_ids):
                require("accounts", account_id)
                record = Task.model_validate({**data, "account_id": account_id}).model_dump(mode="json")
                records.extend([validate_task(dict(record)) for _ in range(quantity)])
        except ValidationError:
            raise HTTPException(422, "Task settings are invalid; no tasks were created")
        return {"created": store().put_many("tasks", records)}

    @app.post("/api/organization/members")
    async def memberships(request: Request):
        data = await request.json()
        try:
            if data.get("remove"):
                app.state.resources.remove(data["folder_id"], data["resource_id"])
            else:
                app.state.resources.add(data["folder_id"], data.get("ids", []))
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc))
        return {"ok": True}

    @app.post("/api/organization/relationship")
    async def relationship(request: Request):
        data = await request.json()
        try:
            app.state.resources.link(data["account_id"], data["profile_id"], data.get("enabled", True))
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc))
        return {"ok": True}

    @app.post("/api/assignments/preview")
    async def assignment_preview(request: Request):
        data = await request.json()
        try:
            return app.state.resources.assignments(data)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc))

    @app.post("/api/assignments/create")
    async def assignment_create(request: Request):
        data = await request.json()
        try:
            preview = app.state.resources.assignments(data)
            if preview["errors"]: raise ValueError("; ".join(preview["errors"]))
            if data.get("preview") != preview["rows"]: raise ValueError("Assignments changed; refresh the preview before creating tasks")
            records = [validate_task(Task.model_validate({**data, **row}).model_dump(mode="json")) for row in preview["rows"]]
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc))
        return {"created":store().put_many("tasks",records)}

    @app.get("/api/analytics/report")
    async def analytics(start: datetime, end: datetime, currency: str = "USD", simulation: bool = False):
        try:
            return report(store(),start,end,currency,simulation)
        except ValueError as exc:
            raise HTTPException(422,str(exc))

    @app.get("/api/diagnostics/{id}/detail")
    async def diagnostic_detail(id: str):
        return {k:v for k,v in require("diagnostics",id).items() if k!="trace"}

    @app.get("/api/diagnostics/{id}/trace")
    async def diagnostic_trace(id: str):
        import base64
        item=require("diagnostics",id)
        if not item.get("trace"):raise HTTPException(404,"No trace attached")
        return Response(base64.b64decode(item["trace"]),media_type="application/zip",headers={"Content-Disposition":"attachment; filename=retail-trace.zip"})

    @app.post("/api/account-manager/bulk")
    async def manage_accounts(request: Request):
        data=await request.json()
        ids=data.get("ids",[])
        if not isinstance(ids,list) or not 1<=len(ids)<=100:
            raise HTTPException(422,"Select 1?100 accounts")
        accounts=[require("accounts",i) for i in dict.fromkeys(ids)]
        action=data.get("action")
        if action not in ("open","verify","group","profile","network"):
            raise HTTPException(422,"Unknown account action")
        results=[]
        for account in accounts:
            try:
                if any(t.get("account_id")==account["id"] and t["id"] in app.state.engine.jobs for t in store().all("tasks")):
                    raise ValueError("Stop this account's tasks first")
                if action in ("open","verify"):
                    if account.get("retailer","amazon")!="amazon": raise ValueError("Retailer adapter is not implemented")
                    await app.state.engine.amazon.login(account)
                    if action=="verify":
                        context=app.state.engine.amazon.logins[account["id"]]
                        await app.state.engine.amazon.ensure_session(context,account,context.pages[0])
                elif action=="group": app.state.resources.add(data.get("target_id",""),[account["id"]])
                elif action=="profile": app.state.resources.link(account["id"],data.get("target_id",""))
                elif action=="network":
                    if account["id"] in app.state.engine.amazon.logins: raise ValueError("Close the account browser before changing network")
                    if data.get("target_id"): require("proxies",data["target_id"])
                    store().put("accounts",{**account,"proxy_list_id":data.get("target_id",""),"proxy":""})
                results.append({"id":account["id"],"ok":True})
            except Exception as exc:
                message=str(exc) if isinstance(exc,ValueError) else "Verification or browser action requires attention"
                if action in ("open","verify"):
                    store().put('sessions',{'account_id':account['id'],'retailer':account.get('retailer','amazon'),'status':'verification_required','checked_at':now(),'action_required':message},'session-'+account['id'])
                results.append({"id":account["id"],"ok":False,"message":message})
        return {"results":results}

    @app.post("/api/recovery/{id}/{action}")
    async def recovery(id: str, action: str):
        try:
            if action=="diagnose": return await diagnose(store(),require("diagnostics",id))
            if action=="validate": return await validate_candidate(store(),require("repairs",id))
            raise HTTPException(404,"Unknown recovery action")
        except HTTPException: raise
        except ValueError as exc: raise HTTPException(422,str(exc))
        except Exception: raise HTTPException(409,"Diagnosis or replay service failed; check the local endpoint")

    @app.post("/api/browser/inspect-cdp")
    async def cdp_inspect():
        try:
            return await inspect_session((store().get("settings","settings") or {}).get("cdp_endpoint","http://127.0.0.1:9222"))
        except Exception:
            raise HTTPException(409,"Could not attach to the local Chromium debugging endpoint")

    recovery_check_lock = asyncio.Lock()

    @app.post('/api/ai-connections/{id}/test-recovery')
    async def test_browser_recovery(id: str):
        from .recovery_check import check_recovery
        from .interactions import InteractionError
        connection = require('ai_connections', id)
        if recovery_check_lock.locked():
            raise HTTPException(409, 'A browser recovery test is already running')
        async with recovery_check_lock:
            try:
                result = await check_recovery(connection)
            except (ProviderError, InteractionError) as exc:
                result = {'ok': False, 'message': str(exc)}
            except Exception:
                result = {'ok': False, 'message': 'Browser recovery test failed. Check browser installation and API connection.'}
            current = require('ai_connections', id)
            if any(current.get(key) != connection.get(key) for key in ('api_key', 'model', 'provider', 'protocol', 'base_url')):
                raise HTTPException(409, 'Connection changed during the test. Test the saved connection again.')
            store().put('ai_connections', {**current, 'browser_health': {**result, 'at': now()}})
            return result

    @app.post('/api/ai-connections/{id}/test')
    async def test_ai_connection(id: str):
        connection = require('ai_connections', id)
        try:
            result = await AIProvider(connection).test()
        except ProviderError as exc:
            result = {'ok': False, 'message': str(exc)}
        except Exception:
            result = {'ok': False, 'message': 'Provider did not return a supported tool response'}
        current = require('ai_connections', id)
        if any(current.get(key) != connection.get(key) for key in ('api_key', 'model', 'provider', 'protocol', 'base_url')):
            raise HTTPException(409, 'Connection changed during the test. Test the saved connection again.')
        store().put('ai_connections', {**current, 'health': {**result, 'at': now()}})
        return result

    @app.get('/api/settings/browser-options')
    async def browser_options():
        from patchright.async_api import async_playwright
        import shutil
        async with async_playwright() as driver:
            bundled = Path(driver.chromium.executable_path).is_file()
        def installed(relative, commands):
            if os.name == 'nt':
                return any((Path(os.environ[root]) / relative).is_file()
                           for root in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA') if os.environ.get(root))
            return any(shutil.which(command) for command in commands)
        return [
            {'id': 'chromium', 'label': 'Bundled Chromium', 'available': bundled},
            {'id': 'chrome', 'label': 'Chrome', 'available': installed('Google/Chrome/Application/chrome.exe', ['google-chrome', 'google-chrome-stable'])},
            {'id': 'msedge', 'label': 'Edge', 'available': installed('Microsoft/Edge/Application/msedge.exe', ['microsoft-edge', 'microsoft-edge-stable'])},
        ]

    @app.post('/api/settings/test-discord')
    async def test_discord():
        import httpx
        saved = store().get('settings', 'settings') or {}
        webhook = saved.get('webhook', '')
        if not webhook:
            raise HTTPException(422, 'Save a Discord webhook before sending a test.')
        try:
            Settings.valid_webhook(webhook)
        except ValueError:
            raise HTTPException(422, 'Replace the saved webhook with a valid Discord webhook URL.')
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(webhook, json={'content': 'Retail Desk: notification connection test.'}, follow_redirects=False)
            if response.status_code not in (200, 204):
                raise HTTPException(502, f'Discord rejected the test (HTTP {response.status_code}). Check your saved webhook.')
        except httpx.HTTPError:
            raise HTTPException(502, 'Could not reach Discord. Check your connection and try again.')
        return {'message': 'Test message sent to the saved Discord webhook.'}

    @app.get("/api/data/backup")
    async def backup():
        import io, sqlite3, tempfile, zipfile
        with tempfile.TemporaryDirectory(prefix='retail-backup-') as directory:
            target=Path(directory)/'retail.sqlite3'
            db=sqlite3.connect(target)
            try:store().db.backup(db)
            finally:db.close()
            output=io.BytesIO()
            with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
                archive.write(target,'retail.sqlite3')
                archive.write(store().folder/'vault.key','vault.key')
        return Response(output.getvalue(),media_type="application/zip",headers={"Content-Disposition":"attachment; filename=retail-desk-encrypted-backup.zip"})

    @app.post("/api/control/stop-all")
    async def stop_all():
        return {"stopped": await app.state.engine.stop_all()}

    @app.post("/api/demo/load")
    async def demo():
        group = store().put("groups", Group(name="Amazon test run", products="B0DEMO0001;35\nB0DEMO0002;50", max_price=50).model_dump())
        for _ in range(3):
            store().put("tasks", {**Task(group_id=group["id"]).model_dump(mode="json"), "status": "idle", "message": "Ready to simulate", "updated_at": now()})
        return {"group_id": group["id"]}

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "static" / "index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app


app = create_app()
