import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import quote, urlsplit

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


class SolverError(ValueError):
    """Categorical provider failure; raw response prose is never exported."""
    def __init__(self, code):
        self.code = code
        super().__init__('Solver rejected the request: ' + code)


class SolverService:
    @staticmethod
    def token_task(solver, kind, url, params, proxy=None):
        provider = solver['provider']
        types = {
            '2captcha': {'arkose':'FunCaptchaTask','recaptcha':'RecaptchaV2Task','turnstile':'TurnstileTask'},
            'capmonster': {'arkose':'FunCaptchaTask','recaptcha':'RecaptchaV2Task','turnstile':'TurnstileTask'},
        }
        if kind not in types.get(provider,{}):
            raise ValueError('The assigned provider has no supported adapter for this CAPTCHA type.')
        if provider=='capmonster' and kind=='arkose' and not proxy:
            raise ValueError('CapMonster FunCaptcha requires the same proxy as the account browser.')
        task_type = types[provider][kind]
        if provider=='2captcha' and not proxy:
            task_type += 'Proxyless'
        task = {'type':task_type,'websiteURL':url,
                'websitePublicKey' if kind=='arkose' else 'websiteKey':params['key']}
        if proxy:
            endpoint=urlsplit(proxy['server'])
            if endpoint.scheme not in ('http','https','socks4','socks5') or not endpoint.hostname or not endpoint.port:
                raise ValueError('The browser proxy cannot be used by this provider.')
            task.update(proxyType=endpoint.scheme,proxyAddress=endpoint.hostname,proxyPort=endpoint.port)
            if proxy.get('username'):task['proxyLogin']=proxy['username']
            if proxy.get('password'):task['proxyPassword']=proxy['password']
        if kind=='arkose':
            if params.get('blob'):task['data']=json.dumps({'blob':params['blob']})
            if params.get('subdomain'):
                subdomain=urlsplit(params['subdomain'] if '://' in params['subdomain'] else 'https://'+params['subdomain']).hostname
                if not subdomain or not (subdomain=='arkoselabs.com' or subdomain.endswith('.arkoselabs.com')):
                    raise ValueError('Unrecognized Arkose API domain.')
                task['funcaptchaApiJSSubdomain']=subdomain
            task['userAgent']=params['userAgent']
        elif kind=='recaptcha':
            task['isInvisible']=params.get('invisible',False)
            if params.get('dataS'):task['recaptchaDataSValue']=params['dataS']
        elif kind=='turnstile':
            if params.get('action'):task['action']=params['action']
            if params.get('cdata'):task['data']=params['cdata']
        return task

    async def solve_token(self, solver, task, *, user_agent=None, progress=None):
        # One createTask call, even after a provider error or timeout.
        async with asyncio.timeout(min(300,solver.get('timeout_seconds',120))):
            result=await self.call(solver,'createTask',{'task':task})
            task_id=result.get('taskId')
            if progress:progress('provider_processing',task_id=task_id)
            while result.get('status')!='ready':
                if not task_id:
                    raise ValueError('Solver did not return a task ID')
                await asyncio.sleep(3)
                result=await self.call(solver,'getTaskResult',{'taskId':task_id})
            solution=result.get('solution') or {}
            if user_agent and solution.get('userAgent') and solution['userAgent']!=user_agent:
                raise SolverError('BROWSER_IDENTITY_MISMATCH')
            token=solution.get('token') or solution.get('gRecaptchaResponse')
            if not isinstance(token,str) or not token or len(token)>65536:
                raise ValueError('Solver returned no valid response token')
            if progress:progress('solution_received')
            return token

    async def call(self, solver, method, payload=None):
        base = PROVIDERS[solver["provider"]]
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(f"{base}/{method}", json={"clientKey": solver["api_key"], **(payload or {})})
            response.raise_for_status()
            data = response.json()
        if data.get("errorId"):
            # Do not echo provider text that may contain credentials or request content.
            # Accept known provider codes only, not arbitrary uppercase payloads.
            code=data.get('errorCode')
            known={'ERROR_CAPTCHA_UNSOLVABLE','ERROR_KEY_DOES_NOT_EXIST','ERROR_ZERO_BALANCE',
                   'ERROR_NO_SLOT_AVAILABLE','ERROR_PROXY_CONNECT_REFUSED','ERROR_PROXY_CONNECT_TIMEOUT',
                   'ERROR_PROXY_READ_TIMEOUT','ERROR_PROXY_BANNED','ERROR_PROXY_TRANSPARENT',
                   'ERROR_TASK_ABSENT','ERROR_TASK_NOT_SUPPORTED','ERROR_INCORRECT_SESSION_DATA',
                   'ERROR_INCORRECT_TASK_DATA','ERROR_IP_NOT_ALLOWED','ERROR_TOKEN_EXPIRED'}
            raise SolverError(code if code in known else 'PROVIDER_ERROR')
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
    from .proxy_protocol import parse_proxy
    return parse_proxy(line).url if line.strip() else None


def proxy_fingerprint(line):
    from .proxy_protocol import parse_proxy
    return parse_proxy(line).fingerprint


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


from .proxy_testing import ProxyTesting
from .proxy_protocol import parse_proxy


class ProxyHealth(ProxyTesting):
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
                            "host": parse_proxy(line).host,
                            "transport": "chromium",
                        }
                        context = None
                        try:
                            context = await browser.new_context(proxy=parse_proxy(line).config(browser=True))
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

