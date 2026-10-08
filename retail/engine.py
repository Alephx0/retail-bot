import asyncio
from datetime import datetime, timezone

import httpx

from .amazon import Amazon, Attention, AccessDenied
from .models import Group, Task, eligible, inputs
from .retailers import RETAILERS
from .store import now
from .scheduling import occurrences
from .task_state import transition
from .diagnostics import Diagnostics
from .proxy_pool import ProxyPool


class Engine:
    def __init__(self, store):
        self.store = store
        self.amazon = Amazon(store)
        self.jobs = {}
        self.account_locks = {}
        self.browser_slots = asyncio.Semaphore((store.get('settings', 'settings') or {}).get('max_running_tasks', 10))
        self.wakes = {}
        self.scheduler = None
        self.pages = {}
        self.live_view_locks = {}
        self.stopping_all = False
        self.diagnostics = Diagnostics(store)

    def status(self, id, status, message, **metadata):
        task = self.store.get("tasks", id)
        if task:
            previous=task.get('state','')
            state,event=transition(previous,status)
            payment_pending=status=='attention' and 'payment verification is required' in message.lower()
            if payment_pending:
                state,event='PAYMENT_CONFIRMATION','PAYMENT_REQUIRED'
            task.update(status=status, state=state, message=message, updated_at=now(), **metadata)
            event_data={"task_id":id,"group_id":task['group_id'],"account_id":task.get('account_id',''),"simulation":task.get('simulation',True),"state":state,"previous_state":previous,"event":event,"message":message,"at":now(), **metadata}
            if not (state in ('MONITORING','OUT_OF_STOCK') and previous in ('MONITORING','OUT_OF_STOCK')):
                self.store.put('task_events',event_data)
            if status=='error' and previous in ('CARTING','CARTED','CHECKOUT','PAYMENT_CONFIRMATION'):
                self.store.put('task_events',{**event_data,'event':'CHECKOUT_FAILED'})
            if task.get('account_id'):
                account=self.store.get('accounts',task['account_id'])
                if account: self.store.put('accounts',{**account,'last_used':now()})
            self.store.put("tasks", task)
            self.store.event(id, status, message)

    async def boot(self):
        for task in self.store.all("tasks"):
            if task.get("status") not in ("idle", "stopped", "scheduled", "completed", "error"):
                self.status(task["id"], "stopped", "Stopped after application restart; review Amazon cart before restarting")
        for group in self.store.all("groups"):
            if group.get("schedule", {}).get("auto_start"):
                await self.start_group(group["id"])
        self.scheduler = asyncio.create_task(self.schedule())

    async def start_group(self, group_id):
        for task in self.store.all("tasks"):
            if task["group_id"] == group_id and task["id"] not in self.jobs:
                try:
                    await self.start(task["id"])
                except ValueError as exc:
                    self.status(task["id"], "error", str(exc))

    async def schedule(self):
        while True:
            local = datetime.now().astimezone()
            for group in self.store.all("groups"):
                due = occurrences(group.get("schedule", {}), local)
                record_id = "schedule-" + group["id"]
                ledger = self.store.get("schedule_runs", record_id) or {"seen": [], "active": False}
                keys = [key for key, _ in due]
                if ledger["active"] and not keys:
                    for task in self.store.all("tasks"):
                        if task["group_id"] == group["id"] and task["id"] in self.jobs:
                            await self.stop(task["id"])
                    ledger["active"] = False
                fresh = [key for key in keys if key not in ledger["seen"]]
                if fresh:
                    ledger["seen"] = (ledger["seen"] + fresh)[-100:]
                    ledger["active"] = True
                    self.store.put("schedule_runs", ledger, record_id)
                    await self.start_group(group["id"])
                if ledger.get("id") or fresh:
                    self.store.put("schedule_runs", ledger, record_id)
            for task in self.store.all("tasks"):
                if task.get("status") == "scheduled" and task.get("scheduled_at") and datetime.fromisoformat(task["scheduled_at"]) <= datetime.now(timezone.utc):
                    try:
                        await self.start(task["id"])
                    except ValueError as exc:
                        self.status(task["id"], "error", str(exc))
            await asyncio.sleep(0.5)

    async def notify(self, task, message):
        settings = self.store.get("settings", "settings") or {}
        category = "webhook_checkouts" if task.get("status") == "completed" else "webhook_attention"
        if not settings.get(category, True):
            return
        if settings.get("notifications") and settings.get("webhook") and not task["simulation"]:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(settings["webhook"], json={"content": f"Retail Desk · {message}"}, follow_redirects=False)
                    response.raise_for_status()
            except Exception:
                self.store.event(task["id"], "notification_error", "Discord notification could not be delivered")

    async def start(self, id):
        if self.stopping_all:
            raise ValueError("All tasks are stopping; try again after the stop completes")
        if id in self.jobs:
            return
        task = self.store.get("tasks", id)
        if not task:
            raise ValueError("Task not found")
        group = self.store.get("groups", task["group_id"])
        if group and (group.get('task_group_id') or (getattr(self,'group_coordinator',None) and any(g.get('legacy_id')==group['id'] for g in self.group_coordinator.repo.all('group')))):
            raise ValueError('This group has migrated. Start it in the new Task Groups workspace.')
        coordinator = getattr(self, 'group_coordinator', None)
        if coordinator and task.get('account_id') and coordinator.busy_account(task['account_id']):
            raise ValueError('This account is reserved or busy in a task group')
        if not group or not (group.get("products", "").strip() or group.get("input_list_id")):
            raise ValueError("Configure the group monitor input before starting tasks")
        settings = self.store.get("settings", "settings") or {}
        if len(self.jobs) >= 1000:
            raise ValueError('Task queue is full; stop or finish queued work before adding more')
        submission = self.store.get("submissions", "submission-" + id)
        if submission and not task["simulation"]:
            raise ValueError("This task already has an order submission record. Review order history; create a new task only for an intentional new purchase.")
        if not task["simulation"]:
            if not RETAILERS[group.get("retailer", "amazon")].get("automation"):
                raise ValueError("This retailer's live adapter is planned; Amazon automation is being implemented first")
            account = self.store.get("accounts", task["account_id"])
            if not account or not (account.get("session") or account.get("password")):
                raise ValueError("Save an account session or provide credentials for automatic login first")
            if task["account_id"] in getattr(self.amazon, "logins", {}):
                raise ValueError("Close or save this account's login browser before starting tasks")
            if account.get("retailer", "amazon") != group.get("retailer", "amazon"):
                raise ValueError("Account and task group must use the same retailer")
            if task.get("checkout_mode") == "automatic" and account["region"] != "US":
                raise ValueError("Automatic order submission currently supports Amazon US")
        self.wakes[id] = asyncio.Event()
        self.status(id, "starting", "Starting simulation" if task["simulation"] else "Opening Amazon browser")
        self.jobs[id] = asyncio.create_task(self.run(id))

    async def stop(self, id):
        job = self.jobs.get(id)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.jobs.pop(id, None)
        self.wakes.pop(id, None)
        self.status(id, "stopped", "Stopped")

    async def stop_all(self):
        self.stopping_all = True
        try:
            coordinator = getattr(self, 'group_coordinator', None)
            group_count = await coordinator.stop_all() if coordinator else 0
            ids = {task["id"] for task in self.store.all("tasks") if task.get("status") == "scheduled" or task["id"] in self.jobs}
            await asyncio.gather(*(self.stop(id) for id in ids))
            return len(ids) + group_count
        finally:
            self.stopping_all = False

    def resume(self, id):
        if id not in self.wakes:
            raise ValueError("Task is not running")
        self.wakes[id].set()

    async def pause(self, id, status, message):
        self.wakes[id].clear()
        self.status(id, status, message)
        if status in ('attention', 'review'):
            page = next((p for p in reversed(self.pages.get(id, [])) if not getattr(p, 'is_closed', lambda: False)()), None)
            if page:
                try:
                    await self.amazon.expose(page)
                except Exception:
                    self.store.event(id, 'browser_attention', 'Could not expose the browser window; use View live or enable a visible browser in Settings')
        if status == "attention":
            self.store.put("harvesters", {"task_id": id, "status": "waiting", "message": message, "at": now()}, "harvester-" + id)
        task=self.store.get("tasks",id)
        if self.pages.get(id):
            await self.diagnostics.capture(task,self.pages[id][-1],task.get('state',''),message)
        await self.notify(task, message)
        await self.wakes[id].wait()
        self.store.delete("harvesters", "harvester-" + id)

    async def focus(self, id):
        pages = self.pages.get(id, [])
        if not pages:
            raise ValueError("No task browser is open")
        page = next((page for page in reversed(pages) if not page.is_closed()), None)
        if page is None:
            raise ValueError("No task browser is open")
        await self.amazon.expose(page)

    async def hide(self, id):
        pages = self.pages.get(id, [])
        page = next((page for page in reversed(pages) if not page.is_closed()), None)
        if page is None:
            raise ValueError('No task browser is open')
        await self.amazon.hide(page)

    async def live_frame(self, id):
        return await self.browser_frame('tasks', id)

    def browser_page(self, scope, id):
        if scope == 'tasks':
            pages = self.pages.get(id, [])
        elif scope == 'accounts':
            context = self.amazon.logins.get(id)
            pages = context.pages if context else []
        elif scope == 'group_attempts' and getattr(self, 'group_coordinator', None):
            page = self.group_coordinator.pages.get(id)
            pages = [page] if page else []
        else:
            raise ValueError('Unknown browser scope')
        return next((page for page in reversed(pages) if not page.is_closed()), None)

    async def browser_frame(self, scope, id):
        page = self.browser_page(scope, id)
        if page is None:
            raise ValueError('No browser page is available for this account or task')
        lock = self.live_view_locks.setdefault((scope, id), asyncio.Lock())
        async with lock:
            try:
                return await asyncio.wait_for(page.screenshot(type='jpeg', quality=70, animations='disabled', timeout=2500), timeout=3)
            except Exception as exc:
                raise ValueError('Browser frame is temporarily unavailable during navigation') from exc

    async def browser_input(self, scope, id, action):
        page = self.browser_page(scope, id)
        if page is None:
            raise ValueError('No browser page is available')
        if scope == 'tasks':
            task = self.store.get('tasks', id)
            if not task or task.get('status') not in ('attention', 'review') or id not in self.wakes or self.wakes[id].is_set():
                raise ValueError('Take Control is available only while this task is paused')
        if scope == 'group_attempts':
            attempt = self.group_coordinator.repo.require('attempt', id)
            run = self.group_coordinator.repo.require('run', attempt['run_id'])
            if attempt['state'] not in ('waiting_user', 'reconciliation_required') or (attempt['state']=='waiting_user' and run['state']!='watching'):
                raise ValueError('Browser input is available only for manual review or reconciliation')
        kind = action.get('kind')
        if kind == 'click':
            size = page.viewport_size or await page.evaluate('({width: innerWidth, height: innerHeight})')
            x, y = action.get('x'), action.get('y')
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or not 0 <= x < size['width'] or not 0 <= y < size['height']:
                raise ValueError('Click is outside the current browser view')
            await page.mouse.click(x, y)
        elif kind == 'text':
            value = action.get('text')
            if not isinstance(value, str) or not 1 <= len(value) <= 512:
                raise ValueError('Type 1 to 512 characters at a time')
            await page.keyboard.insert_text(value)
        elif kind == 'key':
            key = action.get('key')
            if key not in ('Enter', 'Tab', 'Backspace', 'Delete', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Space'):
                raise ValueError('Unsupported browser key')
            await page.keyboard.press(key)
        elif kind == 'scroll':
            delta = action.get('delta')
            if not isinstance(delta, (int, float)) or not -1000 <= delta <= 1000:
                raise ValueError('Invalid scroll amount')
            await page.mouse.wheel(0, delta)
        else:
            raise ValueError('Unknown browser input')
        return {'ok': True}

    def adapter_for(self, retailer):
        if retailer=="amazon": return self.amazon
        raise ValueError("This retailer adapter is not implemented")

    def proxy(self, proxy_id, id, offset=0):
        return ProxyPool(self.store).choose(proxy_id,id)

    async def run(self, id):
        from .runner import TaskRunner
        await TaskRunner(self).run(id)

    async def close(self):
        if self.scheduler:
            self.scheduler.cancel()
            await asyncio.gather(self.scheduler, return_exceptions=True)
        for id in list(self.jobs):
            await self.stop(id)
        await self.amazon.close()
