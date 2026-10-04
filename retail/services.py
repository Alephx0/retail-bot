import asyncio
import base64
import hashlib
import time
from urllib.parse import quote

import httpx

from .models import proxy_config
from .retailers import RETAILERS
from .store import now

PROVIDERS = {
    "capmonster": "https://api.capmonster.cloud",
    "2captcha": "https://api.2captcha.com",
    "anticaptcha": "https://api.anti-captcha.com",
    "capsolver": "https://api.capsolver.com",
}


class SolverService:
    async def call(self, solver, method, payload=None):
        base = PROVIDERS[solver["provider"]]
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(f"{base}/{method}", json={"clientKey": solver["api_key"], **(payload or {})})
            response.raise_for_status()
            data = response.json()
        if data.get("errorId"):
            # Do not echo provider text that may contain credentials or request content.
            raise ValueError("Solver rejected the request: " + str(data.get("errorCode", "provider_error"))[:80])
        return data

    async def health(self, solver):
        if solver["provider"] == "manual":
            return {"ok": True, "message": "Manual browser harvester ready"}
        if solver["provider"] == "flaresolverr":
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                response = await client.post(solver["endpoint"] + "/v1", json={"cmd": "sessions.list"})
                response.raise_for_status()
                result = response.json()
                if result.get("status") != "ok":
                    raise ValueError("FlareSolverr did not report ready")
            return {"ok": True, "message": "FlareSolverr connected", "sessions": len(result.get("sessions", []))}
        data = await self.call(solver, "getBalance")
        return {"ok": True, "balance": data.get("balance"), "message": "Provider credentials accepted"}

    async def solve_image(self, solver, image_bytes):
        if solver["provider"] not in PROVIDERS:
            raise ValueError("Choose an image CAPTCHA provider; manual and FlareSolverr do not solve image text")
        result = await self.call(solver, "createTask", {"task": {"type": "ImageToTextTask", "body": base64.b64encode(image_bytes).decode()}})
        deadline = time.monotonic() + solver["timeout_seconds"]
        while result.get("status") != "ready" and not result.get("solution"):
            if time.monotonic() >= deadline:
                raise ValueError("Solver timed out")
            task_id = result.get("taskId")
            if not task_id:
                raise ValueError("Solver did not return a task ID")
            await asyncio.sleep(3)
            result = {**await self.call(solver, "getTaskResult", {"taskId": task_id}), "taskId": task_id}
        answer = result.get("solution", {}).get("text")
        if not answer:
            raise ValueError("Solver returned no image text")
        return answer

    async def flare_fetch(self, solver, retailer):
        """Explicit diagnostic; never copies anonymous challenge cookies into an account session."""
        async with httpx.AsyncClient(timeout=solver["timeout_seconds"] + 5, trust_env=False) as client:
            response = await client.post(solver["endpoint"] + "/v1", json={"cmd": "request.get", "url": "https://" + RETAILERS[retailer]["domain"] + "/", "maxTimeout": solver["timeout_seconds"] * 1000})
            response.raise_for_status()
            result = response.json()
            if result.get("status") != "ok":
                raise ValueError("FlareSolverr could not complete the diagnostic")
            return {"ok": True, "http_status": result.get("solution", {}).get("status"), "message": "Retailer diagnostic completed"}


def proxy_url(line):
    config = proxy_config(line)
    if not config:
        return None
    auth = (quote(config["username"], safe="") + ":" + quote(config["password"], safe="") + "@") if "username" in config else ""
    return "http://" + auth + config["server"].removeprefix("http://")


def proxy_fingerprint(line):
    return hashlib.sha256(line.strip().encode()).hexdigest()[:16]


BROWSER_CHALLENGE_MARKERS = (
    "enter the characters you see",
    "type the characters you see",
    "robot check",
    "make sure you're not a robot",
    "make sure you are not a robot",
    "click the button below to continue shopping",
    "verify it's you",
)


def classify_browser_probe(http_status, body_text):
    """Classify a read-only browser navigation without attempting recovery."""
    body = (body_text or "").lower()
    if http_status == 429:
        return "rate_limited"
    if http_status in (403, 503) or "access denied" in body:
        return "access_denied"
    if any(marker in body for marker in BROWSER_CHALLENGE_MARKERS):
        return "challenge"
    if http_status is None:
        return "navigation_error"
    if 200 <= http_status < 400:
        return "ok"
    return "http_error"


class ProxyHealth:
    """Keep raw transport reachability separate from Chromium-session health."""

    def __init__(self, store):
        self.store = store
        self.jobs = {}

    def busy(self, record_id):
        return any(key.endswith(":" + record_id) for key in self.jobs)

    async def start(self, record, retailer, mode="connectivity"):
        if mode not in ("connectivity", "browser"):
            raise ValueError("Proxy check mode must be connectivity or browser")
        key = mode + ":" + record["id"]
        if key in self.jobs:
            raise ValueError(f"This list already has a {mode} check running")
        self.jobs[key] = asyncio.create_task(self.run(record, retailer, mode, key))

    def _persist(self, record, retailer, mode, results, status):
        prefix = "health-" if mode == "connectivity" else "browser-health-"
        self.store.put(
            "proxy_health",
            {
                "list_id": record["id"],
                "retailer": retailer,
                "kind": mode,
                "status": status,
                "results": results,
                "at": now(),
            },
            prefix + record["id"],
        )

    async def run(self, record, retailer, mode, key):
        try:
            if mode == "browser":
                await self._run_browser(record, retailer)
            else:
                await self._run_connectivity(record, retailer)
        finally:
            self.jobs.pop(key, None)

    async def _run_connectivity(self, record, retailer):
        """Fast HTTP-client check: tests route reachability, not browser acceptance."""
        settings = self.store.get("settings", "settings") or {}
        semaphore = asyncio.Semaphore(settings.get("proxy_concurrency", 5))
        results = []
        self._persist(record, retailer, "connectivity", results, "testing")

        async def test(index, line):
            async with semaphore:
                started = time.monotonic()
                result = {
                    "index": index + 1,
                    "fingerprint": proxy_fingerprint(line),
                    "host": line.split(":", 1)[0],
                    "transport": "httpx",
                }
                try:
                    async with httpx.AsyncClient(
                        proxy=proxy_url(line),
                        timeout=settings.get("proxy_timeout_seconds", 15),
                        trust_env=False,
                    ) as client:
                        async with client.stream(
                            "GET",
                            "https://" + RETAILERS[retailer]["domain"] + "/",
                            follow_redirects=False,
                        ) as response:
                            # Receiving any HTTP response proves the route is
                            # reachable. 403/429/503 are application responses,
                            # not transport failures.
                            result.update(
                                http_status=response.status_code,
                                status="reachable",
                            )
                except Exception as exc:
                    result.update(status="failed", error=type(exc).__name__)
                result["latency_ms"] = round((time.monotonic() - started) * 1000)
                results.append(result)
                self._persist(record, retailer, "connectivity", results, "testing")

        await asyncio.gather(*(
            test(i, line)
            for i, line in enumerate(record["entries"].splitlines())
            if line.strip()
        ))
        self._persist(record, retailer, "connectivity", results, "completed")

    async def _run_browser(self, record, retailer):
        """Read-only Chromium probe using the same browser transport as tasks."""
        from patchright.async_api import async_playwright

        settings = self.store.get("settings", "settings") or {}
        # Browser probes are intentionally lower-concurrency than raw health
        # checks because each one creates a real browser session.
        semaphore = asyncio.Semaphore(min(2, settings.get("proxy_concurrency", 5)))
        results = []
        self._persist(record, retailer, "browser", results, "testing")
        timeout_ms = settings.get("browser_timeout_ms", 30000)
        target = "https://" + RETAILERS[retailer]["domain"] + "/"

        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                async def test(index, line):
                    async with semaphore:
                        started = time.monotonic()
                        result = {
                            "index": index + 1,
                            "fingerprint": proxy_fingerprint(line),
                            "host": line.split(":", 1)[0],
                            "transport": "chromium",
                        }
                        context = None
                        try:
                            context = await browser.new_context(proxy=proxy_config(line))
                            page = await context.new_page()
                            response = await page.goto(
                                target,
                                wait_until="domcontentloaded",
                                timeout=timeout_ms,
                            )
                            body = ""
                            try:
                                body = (await page.locator("body").inner_text())[:15000]
                            except Exception:
                                pass
                            http_status = response.status if response else None
                            result.update(
                                http_status=http_status,
                                status=classify_browser_probe(http_status, body),
                            )
                        except Exception as exc:
                            result.update(status="failed", error=type(exc).__name__)
                        finally:
                            if context:
                                await context.close()
                        result["latency_ms"] = round((time.monotonic() - started) * 1000)
                        results.append(result)
                        self._persist(record, retailer, "browser", results, "testing")

                await asyncio.gather(*(
                    test(i, line)
                    for i, line in enumerate(record["entries"].splitlines())
                    if line.strip()
                ))
            finally:
                await browser.close()
        self._persist(record, retailer, "browser", results, "completed")

    async def close(self):
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self.jobs.clear()