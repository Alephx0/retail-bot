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


class ProxyHealth:
    def __init__(self, store):
        self.store = store
        self.jobs = {}

    async def start(self, record, retailer):
        if record["id"] in self.jobs:
            raise ValueError("This list is already being checked")
        self.jobs[record["id"]] = asyncio.create_task(self.run(record, retailer))

    async def run(self, record, retailer):
        settings = self.store.get("settings", "settings") or {}
        semaphore = asyncio.Semaphore(settings.get("proxy_concurrency", 5))
        result_id = "health-" + record["id"]
        results = []
        def persist(status):
            self.store.put("proxy_health", {"list_id": record["id"], "retailer": retailer, "status": status, "results": results, "at": now()}, result_id)
        persist("testing")

        async def test(index, line):
            async with semaphore:
                started = time.monotonic()
                result = {"index": index + 1, "fingerprint": proxy_fingerprint(line), "host": line.split(":", 1)[0]}
                try:
                    async with httpx.AsyncClient(proxy=proxy_url(line), timeout=settings.get("proxy_timeout_seconds", 15), trust_env=False) as client:
                        async with client.stream("GET", "https://" + RETAILERS[retailer]["domain"] + "/", follow_redirects=False) as response:
                            result.update(http_status=response.status_code, status="healthy" if 200 <= response.status_code < 400 else "blocked" if response.status_code in (403, 429, 503) else "http_error")
                except Exception as exc:
                    result.update(status="failed", error=type(exc).__name__)
                result["latency_ms"] = round((time.monotonic() - started) * 1000)
                results.append(result)
                persist("testing")
        try:
            await asyncio.gather(*(test(i, line) for i, line in enumerate(record["entries"].splitlines()) if line.strip()))
            persist("completed")
            from .proxy_pool import ProxyPool
            ProxyPool(self.store).sync()
        except asyncio.CancelledError:
            persist("cancelled")
            raise
        finally:
            self.jobs.pop(record["id"], None)

    async def close(self):
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self.jobs.clear()
