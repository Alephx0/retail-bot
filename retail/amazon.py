import asyncio
import json
import profile
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from .fingerprint import build_scripts
from .native_fingerprint import launch_options as native_launch_options, needs_profile_browser
from .worker_profiles import WorkerProfiles, debugging_port
from .fingerprint_suite import build_profile as build_suite_profile
from . import us_fingerprint, proxy_location, behavior
from urllib.parse import urlparse

from patchright.async_api import async_playwright

from .models import DOMAINS, proxy_config, Settings, account_fingerprint_settings
from .identity import IdentityService
from .store import now
from .interactions import resolve, InteractionError
from .browser_bridge import validate_endpoint
from .browser_agent import BrowserAgent
from .browser_mcp import AMAZON_ACTIONS
from .browser_visibility import set_visible
from .account_consistency import AccountBrowserProfiles


def money(text: str) -> float | None:
    match = re.search(r"(?:US\$|CA\$|CDN\$|[$£])\s*([\d,]+(?:\.\d{2})?)", text)
    return float(match[1].replace(",", "")) if match else None


class Attention(Exception):
    pass


class AuthenticationRequired(Attention):
    """Login or security verification interrupted a retriable workflow step."""


class ChallengeDetected(AuthenticationRequired):
    """A retailer verification/challenge page that must be handled manually."""

    def __init__(self, message: str, *, kind: str = "verification"):
        super().__init__(message)
        self.kind = kind


class BackoffRequired(Attention):
    """The retailer asked the client to slow down or is temporarily unavailable."""

    def __init__(self, message: str, *, status: int, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after_seconds = retry_after_seconds


class CartRejected(Attention):
    """A verified pre-cart failure; no cart mutation was attempted."""
    pass


class Amazon:
    def __init__(self, store):
        self.store = store
        self.driver = None
        self.browser = None
        self.logins = {}
        self.fingerprint_tests = {}
        self.fingerprint_test_lock = asyncio.Lock()
        self.login_watchers = {}
        self.launch_lock = asyncio.Lock()
        self.identities = IdentityService(store)
        self.context_accounts = {}
        self.profile_browsers = set()
        self.profile_close_tasks = set()
        self.cdp_attached = False
        self.browser_visible = False
        self.browser_initially_visible = False
        self.agent = BrowserAgent(store) if store else None
        self.profiles = AccountBrowserProfiles(store) if store else None

    @staticmethod
    def _retry_after_seconds(response) -> int | None:
        if not response:
            return None
        try:
            raw = (response.headers or {}).get("retry-after")
        except Exception:
            raw = None
        if not raw:
            return None
        raw = str(raw).strip()
        if raw.isdigit():
            return max(0, min(3600, int(raw)))
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = int((when - datetime.now(timezone.utc)).total_seconds())
            return max(0, min(3600, seconds))
        except Exception:
            return None

    @classmethod
    def _raise_for_response(cls, response):
        if not response:
            return
        status = int(response.status)
        if status == 403:
            raise AccessDenied("Amazon refused the browser request (HTTP 403); automatic retry is disabled.")
        if status in (429, 503):
            retry_after = cls._retry_after_seconds(response)
            detail = f" Retry after {retry_after}s." if retry_after is not None else ""
            raise BackoffRequired(
                f"Amazon requested a slower retry (HTTP {status}).{detail}",
                status=status,
                retry_after_seconds=retry_after,
            )

    async def navigate(self, page, url: str, **kwargs):
        response = await page.goto(url, **kwargs)
        self._raise_for_response(response)
        bridge = getattr(page.context, '_retail_worker_profiles', None)
        if bridge and bridge.errors:
            raise Attention('Worker fingerprint initialization failed: ' + bridge.errors[0])
        return response

    async def resolve_action(self, page, action):
        settings = (self.store.get('settings', 'settings') or {}) if self.store else {}
        mode = settings.get('agent_mode', 'off')
        page._retail_expected_action = action
        if mode != 'agent':
            try:
                return await resolve(page, action)
            except InteractionError as exc:
                if mode == 'off' or ('multiple' in str(exc) and action != 'CONTINUE_CHECKOUT'):
                    raise
                if (page.url, action) in getattr(page, '_retail_agent_attempts', set()):
                    raise InteractionError('Agent already attempted this page action; review the task browser') from exc
                if self.agent:
                    repaired = await self.agent.reuse(page, action, set(DOMAINS.values()), AMAZON_ACTIONS)
                    if repaired is not None:
                        attempts = getattr(page, '_retail_agent_attempts', set())
                        attempts.add((page.url, action))
                        page._retail_agent_attempts = attempts
                        return repaired
                from .diagnostics import Diagnostics
                await Diagnostics(self.store).capture({'id': getattr(page.context, '_retail_task_id', '')}, page, action, exc)
        # One bounded attempt per action/URL per page lifetime prevents an API
        # request on every monitor poll. A new task gets a fresh budget.
        attempts = getattr(page, '_retail_agent_attempts', set())
        key = (page.url, action)
        if key in attempts:
            raise InteractionError('Agent already attempted this page action; review the task browser')
        attempts.add(key)
        page._retail_agent_attempts = attempts
        return await self.agent.resolve(page, action, set(DOMAINS.values()), AMAZON_ACTIONS)

    async def ready(self, settings=None):
        async with self.launch_lock:
            if not self.driver:
                self.driver = await async_playwright().start()
            if not self.browser or not self.browser.is_connected():
                settings = settings if settings is not None else (self.store.get("settings", "settings") or {})
                self.browser_initially_visible = bool(settings.get('show_browser_window', True))
                # Window mode is explicit. The dashboard controls the same page
                # during intervention; a headless process cannot become a GUI
                # in place.
                options = {"headless": not self.browser_initially_visible}
                # Keep the shared browser on the configured standard channel.
                # Native profiles always own their process, so accounts can mix
                # implementations without borrowing another backend's identity.
                if settings.get("browser_channel", "chromium") != "chromium":
                    options["channel"] = settings["browser_channel"]
                if settings.get("cdp_attach"):
                    endpoint = settings.get("cdp_endpoint", "http://127.0.0.1:9222")
                    validate_endpoint(endpoint)
                    self.browser = await self.driver.chromium.connect_over_cdp(endpoint, timeout=10000)
                    self.cdp_attached = True
                    self.browser_visible = True
                else:
                    self.browser = await self.driver.chromium.launch(**options)
                    self.cdp_attached = False
                    self.browser_visible = self.browser_initially_visible

    async def expose(self, page):
        if self.cdp_attached:
            await page.bring_to_front()
        elif getattr(page.context, '_retail_interactive_window', False):
            await set_visible(page, True)
            return
        elif not self.browser_initially_visible:
            return  # The interactive dashboard shares this headless page.
        else:
            await set_visible(page, True)
        self.browser_visible = True

    async def hide(self, page):
        if self.cdp_attached or not self.browser_initially_visible:
            return  # External windows are user-owned; headless needs no hiding.
        await set_visible(page, False)
        self.browser_visible = False

    def account_proxy(self, account):
        if account.get("proxy"):
            return account["proxy"]
        from .proxy_pool import ProxyPool
        return ProxyPool(self.store).choose(account.get("proxy_list_id", ""), account["id"])

    async def context(self, account, proxy=None, solver_id="", *, interactive=False):
        settings = account_fingerprint_settings(self.store.get("settings", "settings") or {}, account)
        await self.ready(settings)
        if interactive:
            # Account sign-in is a desktop interaction, independent of task
            # window mode. Do not mutate saved settings or the task browser.
            settings = {**settings, 'show_browser_window': True}
        options = self.profiles.options(account)
        us_options = us_fingerprint.context_options(self.profiles.get(account), settings, account['region'])
        options.update(us_options)
        browser_identity = (await us_fingerprint.browser_identity_user_agent(self.browser)
                            if us_options and settings.get('fingerprint_navigator') else None)
        if account.get("session"):
            options["storage_state"] = account["session"]
        if proxy is None:
            proxy = self.account_proxy(account)
        if proxy:
            options["proxy"] = proxy_config(proxy)
        location = None
        if us_options and settings.get('fingerprint_proxy_location', True):
            if not proxy and (account.get('proxy') or account.get('proxy_list_id')):
                raise proxy_location.ProxyLocationError('This account requires a proxy, but no proxy route was selected.')
            if proxy:
                location = await proxy_location.bootstrap_location(self.browser, options['proxy'])
                options.update(proxy_location.context_options(location))
        native = settings.get('fingerprint_backend') == 'native'
        suite = settings.get('fingerprint_backend') == 'fingerprint-suite'
        suite_profile = None
        if suite:
            if self.cdp_attached:
                raise ValueError('Fingerprint-suite requires an app-managed browser')
            profile = self.profiles.get(account)
            suite_profile = await build_suite_profile(int(profile['seed'], 16), profile['locale'], self.browser.version)
            options.update(suite_profile['options'])
        managed_workers = not native and not suite and (needs_profile_browser(settings) or bool(us_options))
        if managed_workers and self.cdp_attached:
            raise ValueError('Complete JavaScript graphics profiles require an app-managed browser; disable external CDP attachment.')
        owned_browser = None
        worker_profiles = None
        worker_port = None
        if native:
            # Launch switches apply process-wide: never share a seeded browser
            # between accounts or retrofit it onto an already-running context.
            seed = int(self.profiles.get(account)['seed'], 16) & 0xffffffff
            owned_browser = await self.driver.chromium.launch(**native_launch_options(settings, seed))
            self.profile_browsers.add(owned_browser)
        elif managed_workers:
            # The DevTools subscription belongs only to this account's process.
            # It cannot initialize workers from another account or browser.
            worker_port = debugging_port()
            launch = {'headless': not settings.get('show_browser_window', True),
                      'args': [f'--remote-debugging-port={worker_port}',
                               '--remote-debugging-address=127.0.0.1']}
            if us_options:
                # WorkerNavigator uses process languages, while context locale
                # affects documents. Set both natively for US profiles.
                launch['args'] += ['--lang=en-US', '--accept-lang=en-US']
            if browser_identity:
                # A process switch covers service workers as well as page and
                # dedicated-worker headers, without context UA emulation.
                launch['args'].append('--user-agent=' + browser_identity)
            if settings.get('browser_channel', 'chromium') != 'chromium':
                launch['channel'] = settings['browser_channel']
            owned_browser = await self.driver.chromium.launch(**launch)
            self.profile_browsers.add(owned_browser)
        elif interactive and not self.cdp_attached:
            launch = {'headless': False}
            if settings.get('browser_channel', 'chromium') != 'chromium':
                launch['channel'] = settings['browser_channel']
            owned_browser = await self.driver.chromium.launch(**launch)
            self.profile_browsers.add(owned_browser)
        try:
            context = await (owned_browser or self.browser).new_context(**options)
        except BaseException:
            if owned_browser:
                await owned_browser.close()
                self.profile_browsers.discard(owned_browser)
            raise

        def release_context(_):
            self.context_accounts.pop(context, None)
            if owned_browser:
                async def close_owned():
                    try:
                        if worker_profiles:
                            await worker_profiles.close()
                    finally:
                        try:
                            await owned_browser.close()
                        finally:
                            self.profile_browsers.discard(owned_browser)
                task = asyncio.create_task(close_owned())
                self.profile_close_tasks.add(task)
                def finished(done):
                    self.profile_close_tasks.discard(done)
                    if not done.cancelled():
                        done.exception()  # Observe teardown errors; close() also drains owners.
                task.add_done_callback(finished)
        context.on('close', release_context)
        context._retail_interactive_window = interactive and not self.cdp_attached
        context._retail_paced_input = settings.get('interaction_pacing') == 'paced'
        if us_options:
            context._retail_us_profile_options = {key: options[key] for key in ('locale', 'viewport', 'screen', 'device_scale_factor', 'timezone_id')}
            context._retail_us_profile_options['surfaces'] = [key for key in us_fingerprint.SURFACES if settings.get('fingerprint_' + key)]
        try:
            # Fingerprint transformations are explicit Settings opt-ins, not
            # derived from the headed/headless launch mode. GPU identity spans
            # both APIs; JS graphics contexts initialize workers automatically.
            # Native graphics are configured at process launch; only audio
            # fallbacks need an init script in that backend.
            if suite_profile:
                for script in suite_profile['scripts']:
                    await context.add_init_script(script)
                context._retail_suite_profile = suite_profile
            elif self.profiles:
                profile = self.profiles.get(account)
                seed_int = int(profile['seed'], 16) & 0xFFFFFFFF
                scripts = build_scripts(
                    seed_int,
                    perturb_canvas=not native and settings.get('fingerprint_canvas', False),
                    spoof_webgl=not native and settings.get('fingerprint_webgl', False),
                    spoof_webgpu=not native and settings.get('fingerprint_webgpu', False),
                    spoof_audio=settings.get('fingerprint_audio', False),
                    spoof_navigator=not native and settings.get('fingerprint_navigator', False),
                    restrict_fonts=not native and settings.get('fingerprint_fonts', False),
                    intercept_workers=not native and not managed_workers and settings.get('fingerprint_workers', False),
                )
                for script in scripts:
                    await context.add_init_script(script)
                if managed_workers:
                    worker_profiles = await WorkerProfiles.start(owned_browser, worker_port, '\n'.join(scripts))
                    context._retail_worker_profiles = worker_profiles
            saved_storage = account.get('session_storage', {})
            domain = DOMAINS[account['region']]
            values = saved_storage.get(domain, {}) if isinstance(saved_storage, dict) else {}
            if isinstance(values, dict) and values:
                safe = {key: value for key, value in values.items() if isinstance(key, str) and isinstance(value, str)}
                if len(json.dumps(safe)) <= 65536:
                    await context.add_init_script('''(() => {
                        if (location.hostname !== %s || sessionStorage.getItem('__retail_restored_v1')) return;
                        for (const [key, value] of Object.entries(%s)) sessionStorage.setItem(key, value);
                        sessionStorage.setItem('__retail_restored_v1', '1');
                    })()''' % (json.dumps(domain), json.dumps(safe)))
            if location:
                confirmed = await proxy_location.lookup_location(context)
                proxy_location.ensure_same_route(location, confirmed)
                context._retail_proxy_location = proxy_location.summary(confirmed)
            context.set_default_timeout(settings.get("browser_timeout_ms", 30000))
            if settings.get("trace_enabled"):
                await context.tracing.start(screenshots=True,snapshots=True,sources=False)
                context._retail_tracing=True
            self.context_accounts[context] = {**account, "solver_id": solver_id or account.get("solver_id", ""), "otp_since": time.time() - 10}
            return context
        except BaseException:
            await context.close()
            raise

    async def test_fingerprint(self, account):
        # Tests share the account seed and proxy, but never load or save login cookies.
        async with self.fingerprint_test_lock:
            settings = Settings.model_validate(self.store.get('settings', 'settings') or {})
            if settings.cdp_attach:
                raise ValueError('Disable external CDP attachment in Settings to open a separate fingerprint test browser.')
            if account['id'] not in self.fingerprint_tests and len(self.fingerprint_tests) >= 5:
                raise ValueError('Close a fingerprint test browser before opening another (maximum 5).')
            await self.close_fingerprint_test(account['id'])
            clean_account = {key: value for key, value in account.items() if key not in ('session', 'session_storage')}
            context = await self.context(clean_account, interactive=True)
            self.fingerprint_tests[account['id']] = context

            def forget(_):
                if self.fingerprint_tests.get(account['id']) is context:
                    self.fingerprint_tests.pop(account['id'], None)

            context.on('close', forget)
            try:
                sites = settings.fingerprint_test_sites
                pages = [await context.new_page() for _ in (sites or [None])]
                # A failed website should not prevent the other tabs from opening.
                results = await asyncio.gather(*[
                    page.goto(site.url, wait_until='domcontentloaded')
                    for page, site in zip(pages, sites)
                ], return_exceptions=True)
                await self.expose(pages[0])
                return {'ok': True, 'failed_sites': [site.name for site, result in zip(sites, results)
                                                    if isinstance(result, BaseException)]}
            except BaseException:
                await context.close()
                raise

    async def close_fingerprint_test(self, account_id):
        context = self.fingerprint_tests.pop(account_id, None)
        if context:
            await context.close()

    async def login(self, account):
        if account["id"] in self.logins:
            await self.expose(self.logins[account["id"]].pages[0])
            return
        limit = min(5, (self.store.get('settings', 'settings') or {}).get('max_running_tasks', 10))
        if len(self.logins) >= limit:
            raise ValueError('Too many account sign-ins are open. Finish or close another account session first.')
        context = await self.context(account, interactive=True)
        try:
            page = await context.new_page()
            await self.expose(page)
            await self.navigate(page, f"https://{DOMAINS[account['region']]}/gp/your-account/order-history", wait_until="domcontentloaded")
            self.logins[account["id"]] = context
            await self.authenticate(page, account)
            self.login_watchers[account['id']] = asyncio.create_task(self.watch_login(account['id'], context, page))
        except BaseException:
            await context.close()
            self.logins.pop(account["id"], None)
            raise

    async def watch_login(self, account_id, context, page):
        """Save a manually completed login without asking the user to click Save."""
        deadline = time.monotonic() + 1800
        try:
            while self.logins.get(account_id) is context and not page.is_closed() and time.monotonic() < deadline:
                if urlparse(page.url).hostname in DOMAINS.values() and '/ap/' not in urlparse(page.url).path:
                    label = await self.text(page, '#nav-link-accountList .nav-line-1')
                    if label and 'sign in' not in label.lower():
                        account = self.store.get('accounts', account_id)
                        if account:
                            try:
                                # A stale rendered header is not proof of a
                                # live login. Revisit the account page before
                                # persisting the authenticated storage state.
                                await self.ensure_session(context, account, page)
                            except Attention:
                                await asyncio.sleep(1)
                                continue
                        await context.close()
                        self.logins.pop(account_id, None)
                        return
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The explicit Save session action remains available if the page
            # closes or Amazon uses a layout the watcher cannot verify.
            return
        finally:
            if self.logins.get(account_id) is context and (page.is_closed() or time.monotonic() >= deadline):
                try:
                    await context.close()
                except Exception:
                    pass
                self.logins.pop(account_id, None)
            if self.login_watchers.get(account_id) is asyncio.current_task():
                self.login_watchers.pop(account_id, None)

    async def authenticate(self, page, account):
        """Fill only the current account's Amazon sign-in fields, with bounded steps."""
        for _ in range(6):
            if urlparse(page.url).hostname not in DOMAINS.values():
                return
            if await page.locator("#ap_email:visible").count() and account.get("email"):
                await behavior.fill(page, page.locator("#ap_email"), account["email"])
                await behavior.click(page, page.locator("#continue"))
            elif await page.locator("#ap_password:visible").count() and account.get("password"):
                await behavior.fill(page, page.locator("#ap_password"), account["password"])
                await behavior.click(page, page.locator("#signInSubmit"))
            elif await page.locator("#auth-mfa-otpcode:visible, #cvf-input-code:visible").count() and account.get("auto_otp"):
                try:
                    await self.fill_otp(page, account)
                except ValueError:
                    return
            else:
                return
            await page.wait_for_timeout(800)

    async def fill_otp(self, page, account):
        if urlparse(page.url).hostname not in DOMAINS.values():
            raise ValueError("Verification is only available on this account's retailer")
        field = page.locator("#auth-mfa-otpcode:visible, #cvf-input-code:visible").first
        if not await field.count():
            raise ValueError("No supported verification field is visible")
        result = await self.identities.code(account, since=self.context_accounts.get(page.context, {}).get("otp_since"))
        await behavior.fill(page, field, result["code"])
        submit = page.locator("#auth-signin-button:visible, input[aria-labelledby='cvf-submit-otp-button-announce']:visible, #cvf-submit-otp-button input:visible").first
        if await submit.count():
            await behavior.click(page, submit)
            await page.wait_for_timeout(800)

    async def register(self, account):
        if not account.get("email") or not account.get("password"):
            raise ValueError("Enter this account's email and password before opening registration")
        if account["id"] in self.logins:
            raise ValueError("Close or save the existing account browser first")
        limit = min(5, (self.store.get('settings', 'settings') or {}).get('max_running_tasks', 10))
        if len(self.logins) >= limit:
            raise ValueError('Too many account sign-ins are open. Finish or close another account session first.')
        context = await self.context({**account, "session": None}, interactive=True)
        self.logins[account["id"]] = context
        page = await context.new_page()
        await self.expose(page)
        await self.navigate(page, f"https://{DOMAINS[account['region']]}/ap/register", wait_until="domcontentloaded")
        for selector, value in [("#ap_customer_name", account["name"]), ("#ap_email", account["email"]), ("#ap_password", account["password"]), ("#ap_password_check", account["password"])]:
            if await page.locator(selector).count():
                await behavior.fill(page, page.locator(selector), value)
        # Registration terms and any phone verification remain visible to the user.
        await page.bring_to_front()
        self.login_watchers[account['id']] = asyncio.create_task(self.watch_login(account['id'], context, page))

    async def save_login(self, account):
        context = self.logins.get(account["id"])
        if not context or not context.pages:
            raise ValueError("Open the login browser first")
        watcher = self.login_watchers.pop(account['id'], None)
        if watcher:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        page = context.pages[0]
        await self.navigate(page, f"https://{DOMAINS[account['region']]}/gp/your-account/order-history", wait_until="domcontentloaded")
        await self.check(page)
        if await page.locator("#ap_email, #ap_password").count() or "/ap/" in page.url:
            raise ValueError("Finish signing in to Amazon before saving the session")
        if not await page.locator("#nav-item-signout, a[href*='sign-out'], #nav-link-accountList .nav-line-1").count():
            raise ValueError("Could not verify login; open Your Orders and try again")
        label = await self.text(page, "#nav-link-accountList .nav-line-1")
        if "sign in" in label.lower():
            raise ValueError("Amazon still shows you as signed out")
        account["session"] = await context.storage_state(indexed_db=True)
        await self.check_browser_health(page, account)
        account['session_storage'] = await self.capture_session_storage(page)
        account["logged_in"] = True
        self.store.put("accounts", account)
        await context.close()
        del self.logins[account["id"]]

    async def ensure_session(self, context, account, page):
        """Prepare and verify a login in the same context the checkout will use."""
        # On Resume, inspect the existing challenge first. Navigating away from
        # an MFA/passkey page can invalidate the user's in-progress verification.
        if urlparse(page.url).hostname in DOMAINS.values() and ("/ap/" in urlparse(page.url).path or await page.locator("#ap_email, #ap_password, #auth-mfa-otpcode, #captchacharacters").count()):
            await self.authenticate(page, account)
            await self.check(page)
        await self.navigate(page, getattr(page, '_retail_start_url', None) or f"https://{DOMAINS[account['region']]}/gp/your-account/order-history", wait_until="domcontentloaded")
        start_url = getattr(page, '_retail_start_url', None)
        # Product pages do not redirect signed-out shoppers automatically, unlike
        # Your Orders. Follow only Amazon's own sign-in link when needed.
        label = await self.text(page, "#nav-link-accountList .nav-line-1")
        if start_url and 'sign in' in label.lower():
            sign_in = page.locator('#nav-link-accountList')
            href = await sign_in.evaluate("e => e.href || ''") if await sign_in.count() == 1 else ''
            parsed = urlparse(href)
            if parsed.scheme == 'https' and parsed.hostname == DOMAINS[account['region']] and parsed.path.startswith(('/ap/signin', '/gp/sign-in')):
                await self.navigate(page, href, wait_until='domcontentloaded')
            else:
                raise AuthenticationRequired('Sign in in the task browser, then Resume. The product page shows a signed-out session.')
        await self.authenticate(page, account)
        await self.check(page)
        if start_url and page.url != start_url:
            await self.navigate(page, start_url, wait_until='domcontentloaded')
            await self.check(page)
        label = await self.text(page, "#nav-link-accountList .nav-line-1")
        if not label or "sign in" in label.lower() or "/ap/" in page.url:
            raise AuthenticationRequired("Account sign-in is not verified. Complete login in this browser, then Resume.")
        page._retail_initial_product = page.url if getattr(page, '_retail_start_url', None) == page.url else None
        current = self.store.get("accounts", account["id"])
        if current:
            await self.check_browser_health(page, account)
            current.update(session=await context.storage_state(indexed_db=True), session_storage=await self.capture_session_storage(page),
                           logged_in=True, session_saved_at=now(), last_login=now())
            self.store.put("accounts", current)
            self.store.put("sessions", {"account_id":account['id'],"retailer":account.get('retailer','amazon'),"status":"ready","last_login":now(),"network":account.get('proxy_list_id','')}, 'session-'+account['id'])

    async def check_browser_health(self, page, account):
        try:
            await self.profiles.check(page, account)
        except Exception:
            # Diagnostic failure must not invalidate a successfully verified login.
            self.store.put('browser_health', {'account_id': account['id'], 'at': now(),
                           'status': 'unavailable', 'drift': []}, 'browser-health-' + account['id'])

    async def capture_session_storage(self, page):
        domain = urlparse(page.url).hostname
        if domain not in DOMAINS.values():
            return {}
        # Read the website's storage, not Patchright's isolated evaluation world.
        values = await page.evaluate("() => Object.fromEntries(Object.entries(sessionStorage).filter(([key]) => key !== '__retail_restored_v1'))", isolated_context=False)
        if not isinstance(values, dict) or len(json.dumps(values)) > 65536:
            return {}
        return {domain: values}

    async def text(self, page, selector):
        loc = page.locator(selector).first
        return (await loc.inner_text()).strip() if await loc.count() else ""

    async def seller_text(self, root, *, product_page=False):
        """Read seller evidence, never infer the seller from 'Ships from'."""
        direct = root.locator("#sellerProfileTriggerId, [tabular-attribute-name='Sold by'] .tabular-buybox-text, .tabular-buybox-text[tabular-attribute-name='Sold by'], .seller-name")
        values = []
        for node in await direct.all():
            if await node.is_visible():
                value = (await node.inner_text()).strip()
                if value: values.append(value)
        if values:
            return values[0] if len(set(values)) == 1 else ''
        if product_page:
            # Amazon's newer offer-display layout gives the seller a dedicated
            # merchant feature, separate from the shipping provider feature.
            merchant = root.locator('#merchantInfoFeature_feature_div .offer-display-feature-text-message')
            if await merchant.count() == 1 and await merchant.is_visible():
                value = (await merchant.inner_text()).strip()
                if value and '\n' not in value:
                    return value
            blocks = root.locator('#merchantInfoFeature_feature_div, #merchant-info, #tabular-buybox')
            text = '\n'.join(await blocks.all_inner_texts())
        else:
            text = await root.inner_text()
        match = re.search(r'(?:ships from and sold by|shipper\s*/\s*seller|sold by)\s*:?\s*([^\n]+)', text, re.I)
        if match:
            return re.split(r'\s+and (?:fulfilled|ships|shipped)|\s+Returns\b', match[1], flags=re.I)[0].strip().rstrip('.')
        return ''

    async def check(self, page):
        account = self.context_accounts.get(page.context, {})
        if urlparse(page.url).hostname in DOMAINS.values():
            # Account-owned OTP may still be filled automatically. Retailer
            # anti-bot/image challenges are never solved here; they are
            # detected and escalated for manual review.
            if await page.locator("#auth-mfa-otpcode:visible, #cvf-input-code:visible").count() and account.get("auto_otp"):
                try:
                    await self.fill_otp(page, account)
                except ValueError:
                    pass

        body = (await page.locator("body").inner_text())[:30000].lower()
        path = urlparse(page.url).path.lower()

        if "access denied" in body:
            raise AccessDenied("Access Denied; account or connection was refused")

        if (
            await page.locator("#captchacharacters:visible").count()
            or "validatecaptcha" in path
            or any(x in body for x in (
                "enter the characters you see",
                "type the characters you see",
                "robot check",
                "make sure you're not a robot",
                "make sure you are not a robot",
            ))
        ):
            raise ChallengeDetected(
                "Amazon presented a robot/CAPTCHA challenge. No automated solve was attempted. "
                "Complete verification in the task browser, then Resume.",
                kind="captcha",
            )

        if "click the button below to continue shopping" in body:
            raise ChallengeDetected(
                "Amazon requires a Continue shopping confirmation. No automatic confirmation was attempted. "
                "Complete it in the task browser, then Resume.",
                kind="continue-shopping",
            )

        if any(x in body for x in ("verify it's you", "verify your identity", "additional verification required")):
            raise ChallengeDetected(
                "Amazon requires additional verification. Complete it in the task browser, then Resume.",
                kind="identity-verification",
            )

        if "no default payment method" in body:
            raise Attention("No Default Payment Method: check the default card in your Amazon account")
        if "no default address" in body:
            raise Attention("No Default Address: select a default shipping address in your Amazon account")

        if await page.locator("#auth-mfa-otpcode, #cvf-input-code").count():
            raise AuthenticationRequired("Amazon needs account verification. Complete it in the task browser, then Resume.")
        if "/ap/signin" in page.url or await page.locator("#ap_password").count():
            raise AuthenticationRequired("Session expired. Sign in in the task browser, then Resume.")

    async def inspect(self, page, item, region):
        target = f"https://{DOMAINS[region]}/dp/{item['asin']}"
        reuse_initial = getattr(page, '_retail_initial_product', None) == target and page.url == target
        page._retail_initial_product = None
        response = None if reuse_initial else await self.navigate(page, target, wait_until="domcontentloaded", timeout=45000)
        await self.check(page)
        title = await self.text(page, "#productTitle")
        if not title:
            heading=page.get_by_role("heading",level=1)
            if await heading.count()==1: title=(await heading.inner_text()).strip()
        if not title:
            raise ValueError("Product page is unavailable or its layout is unsupported")
        price = money(await self.text(page, "#corePrice_feature_div .a-price .a-offscreen, #corePriceDisplay_desktop_feature_div .a-price .a-offscreen, #priceblock_ourprice"))
        if price is None:
            # Structured product metadata is a bounded fallback for layout
            # changes. Final checkout still verifies its own price and total.
            metadata = page.locator("meta[property='product:price:amount'], meta[itemprop='price']")
            if await metadata.count() == 1:
                raw = await metadata.get_attribute('content') or ''
                if re.fullmatch(r'\d{1,7}(?:\.\d{1,2})?', raw):
                    price = float(raw)
        original = money(await self.text(page, "#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen, #corePrice_feature_div .a-text-price .a-offscreen"))
        seller = await self.seller_text(page, product_page=True)
        offer_loc = page.locator("input[name='offerListingID'], input[name='offeringID.1']").first
        offer = await offer_loc.get_attribute("value") if await offer_loc.count() else ""
        condition = (await self.text(page, "#condition, #condition-value")).lower()
        used = bool(await page.locator("#usedBuySection").count()) and not bool(await page.locator("#newBuyBoxPrice, #newBuyBox").count())
        agent_error = ''
        try:
            cart = await resolve(page,"ADD_TO_CART")
        except InteractionError:
            # Stock checks do not spend an AI request each polling cycle.
            cart = page.get_by_role('button', name=re.compile(AMAZON_ACTIONS['ADD_TO_CART'], re.I))
            if self.agent and (self.store.get('settings', 'settings') or {}).get('agent_mode') in ('recovery', 'agent'):
                buttons = page.locator('button,input[type=submit],input[type=button],[role=button]')
                labels = await buttons.evaluate_all("els => els.slice(0,150).map(e => e.getAttribute('aria-label') || (e.matches('input') ? e.value : e.innerText) || '')")
                observed = getattr(page, '_retail_monitor_attempts', set())
                if page.url not in observed and any(re.search(r'cart|basket|bag', label, re.I) for label in labels[:150]):
                    observed.add(page.url)
                    page._retail_monitor_attempts = observed
                    try:
                        cart = await self.agent.resolve(page, 'ADD_TO_CART', set(DOMAINS.values()), AMAZON_ACTIONS)
                    except InteractionError as exc:
                        agent_error = str(exc)
        unique = await cart.count() == 1 if hasattr(cart, 'count') else True
        available = unique and await cart.is_visible() and await cart.is_enabled()
        stock_text = (await self.text(page, '#availability')).lower()
        stock_status = 'available' if available else 'unavailable' if any(x in stock_text for x in ('currently unavailable', 'out of stock')) else 'unknown'
        return {"asin": item["asin"], "title": title, "price": price,
                "original_price": original, "offer_id": offer or "",
                "image": await page.locator("#landingImage").get_attribute("src") if await page.locator("#landingImage").count()==1 else "",
                "amazon_seller": bool(re.fullmatch(r"Amazon(?:\.com|\.co\.uk|\.ca)?(?: Services(?:,? Inc\.?)?|\.com Services LLC)?", seller, re.I)),
                "seller": seller or "Unknown", "condition": "used" if used or "used" in condition else "new",
                "available": available, 'availability_status': stock_status, 'agent_error': agent_error}

    async def cart(self, page, quantity, asin):
        domain = urlparse(page.url).hostname
        if domain not in DOMAINS.values():
            raise Attention("Unexpected page. Review the browser before continuing.")
        product_url = page.url
        # Check the active cart before any mutation. Amazon increments quantity
        # when the same ASIN is added again; retries and pre-existing items must
        # not silently turn a one-item request into a two-item order.
        await self.navigate(page, f"https://{domain}/gp/cart/view.html", wait_until="domcontentloaded")
        await self.check(page)
        existing = await self.get_cart(page)
        if any(line['asin'] != asin for line in existing):
            await self.save_unrelated_cart_items(page, asin)
            existing = await self.get_cart(page)
        if not existing:
            cart_count = await self.text(page, '#nav-cart-count')
            if cart_count.isdigit() and int(cart_count) > 0:
                raise CartRejected('Amazon reports items in the cart, but their product identities could not be verified')
        matches = [line for line in existing if line['asin'] == asin]
        if len(matches) > 1:
            raise CartRejected("The target appears more than once in the cart; inspect Amazon before checkout")
        if existing and len(existing) != 1:
            raise CartRejected("Other items are already in the active cart. Clear or save those items in Amazon before automatic checkout")
        if matches:
            if matches[0]['quantity'] == quantity:
                return quantity
            # A supported native cart quantity picker can safely normalize a
            # pre-existing target. Verify the resulting DOM state after the
            # change; never add the target again to adjust quantity.
            row = page.locator(f"#sc-active-cart [data-asin='{asin}']")
            picker = row.locator("select[name='quantity']")
            if await row.count() == 1 and await picker.count() == 1:
                choices = await picker.locator('option').evaluate_all("els => els.map(e => e.value)")
                if str(quantity) in choices:
                    await picker.select_option(str(quantity))
                    await page.wait_for_timeout(500)
                    updated = await self.get_cart(page)
                    if len(updated) == 1 and updated[0]['asin'] == asin and updated[0]['quantity'] == quantity:
                        return quantity
            if (await row.count() == 1 and matches[0]['quantity'] is not None
                    and 1 <= quantity <= 30 and 1 <= matches[0]['quantity'] <= 30):
                direction = 'Increase' if quantity > matches[0]['quantity'] else 'Decrease'
                step = 1 if direction == 'Increase' else -1
                for expected in range(matches[0]['quantity'] + step, quantity + step, step):
                    control = row.get_by_role('button', name=re.compile(rf'^{direction} (?:item quantity$|quantity by one, Quantity is \d+)', re.I))
                    if await control.count() != 1 or not await control.is_visible() or not await control.is_enabled():
                        break
                    await behavior.click(page, control)
                    try:
                        await page.wait_for_function("([asin, qty]) => {const rows=[...document.querySelectorAll('#sc-active-cart [data-asin]')].filter(e=>e.getAttribute('data-asin')===asin); return rows.length===1 && rows[0].getAttribute('data-quantity')===String(qty)}", arg=[asin, expected], timeout=3000)
                    except Exception:
                        break
                updated = await self.get_cart(page)
                if len(updated) == 1 and updated[0]['asin'] == asin and updated[0]['quantity'] == quantity:
                    return quantity
            raise CartRejected("The target is already in the cart with a different quantity; adjust its quantity in Amazon before starting this task")
        if existing:
            raise CartRejected("Other items are already in the active cart. Clear or save those items in Amazon before automatic checkout")
        await self.navigate(page, product_url, wait_until="domcontentloaded")
        await self.check(page)
        selector = page.locator("select#quantity")
        actual = 1
        if await selector.count():
            values = await selector.locator("option").evaluate_all("els => els.map(e => Number(e.value)).filter(x => Number.isInteger(x) && x > 0)")
            if quantity not in values:
                raise CartRejected("Requested item quantity is unavailable; choose a supported quantity")
            actual=quantity
            await selector.select_option(str(actual))
        elif quantity!=1:
            raise CartRejected("Requested item quantity could not be selected")
        try:
            await behavior.click(page, await self.resolve_action(page,"ADD_TO_CART"))
        except InteractionError as exc:
            raise Attention(str(exc)) from exc
        await page.wait_for_timeout(1500)
        await self.check(page)
        # Cart is a handoff, never evidence that an order was placed.
        await self.navigate(page, f"https://{domain}/gp/cart/view.html", wait_until="domcontentloaded")
        await self.check(page)
        lines=await self.get_cart(page)
        matching=[line for line in lines if line['asin']==asin]
        if len(matching)!=1 or matching[0]['quantity']!=actual:
            raise Attention("Cart product or quantity could not be verified; inspect the cart before restarting")
        return actual

    async def buy_now(self, page, quantity, asin):
        """Use Buy Now when offered; return False before mutation if unavailable."""
        if urlparse(page.url).hostname not in DOMAINS.values():
            raise Attention('Unexpected page. Review the browser before continuing.')
        offered = page.locator("#buy-now-button:visible, input[name='submit.buy-now']:visible").or_(
            page.get_by_role('button', name=re.compile(AMAZON_ACTIONS['BUY_NOW'], re.I)))
        if not await offered.count():
            return False
        selector = page.locator('select#quantity')
        if await selector.count():
            values = await selector.locator("option").evaluate_all("els => els.map(e => Number(e.value)).filter(x => Number.isInteger(x) && x > 0)")
            if quantity not in values:
                raise CartRejected('Requested item quantity is unavailable; choose a supported quantity')
            await selector.select_option(str(quantity))
        elif quantity != 1:
            raise CartRejected('Requested item quantity could not be selected')
        title = await self.text(page, '#productTitle')
        if not title:
            raise Attention('Buy Now product title could not be verified')
        page._retail_cart_title = title.splitlines()[0].strip()
        page._retail_cart_asin = asin
        try:
            await behavior.click(page, await self.resolve_action(page, 'BUY_NOW'))
        except InteractionError as exc:
            raise Attention(str(exc)) from exc
        await self.advance_checkout(page)
        return True

    async def get_cart(self,page):
        if urlparse(page.url).hostname not in DOMAINS.values(): raise Attention("Unexpected cart domain")
        active=page.locator("#sc-active-cart [data-asin]")
        if not await page.locator('#sc-active-cart').count():
            active=page.locator("[data-asin][data-quantity]:not(#sc-saved-cart *):not(#sc-saved-cart-items *)")
        lines=[]
        for row in await active.all():
            if not await row.is_visible(): continue
            asin=await row.get_attribute('data-asin')
            quantity=await row.get_attribute('data-quantity')
            select=row.locator("select[name='quantity']")
            if quantity is None and await select.count()==1:quantity=await select.input_value()
            lines.append({'asin':asin,'quantity':int(quantity) if quantity and quantity.isdigit() else None})
        return lines

    async def save_unrelated_cart_items(self, page, target_asin):
        """Move only identified, unrelated active-cart rows to Saved for Later."""
        lines = await self.get_cart(page)
        unrelated = [line['asin'] for line in lines if line['asin'] != target_asin]
        if any(not asin for asin in unrelated) or len(unrelated) != len(set(unrelated)):
            raise CartRejected('Cart item identities are ambiguous; no items were removed')
        for asin in unrelated:
            row = page.locator(f"#sc-active-cart [data-asin='{asin}']")
            if await row.count() != 1:
                raise CartRejected('An unrelated cart item could not be uniquely identified')
            action = row.locator("input[name^='submit.save-for-later'], button[name^='submit.save-for-later'], input[aria-label^='Save for later'], button[aria-label^='Save for later']").or_(
                row.get_by_role('button', name=re.compile(r'^save for later$', re.I))).or_(
                row.get_by_role('link', name=re.compile(r'^save for later$', re.I)))
            if await action.count() != 1 or not await action.is_visible() or not await action.is_enabled():
                raise CartRejected('Save for Later is unavailable for an unrelated cart item; cart was not cleared')
            await behavior.click(page, action)
            try:
                await page.wait_for_function("asin => ![...document.querySelectorAll('#sc-active-cart [data-asin]')].some(e => e.getAttribute('data-asin') === asin)", arg=asin, timeout=3000)
            except Exception:
                # Amazon may persist Save for Later on the server while leaving
                # this tab's cart markup stale. Reload and verify both sides.
                await page.reload(wait_until='domcontentloaded')
                try:
                    await page.wait_for_function("asin => ![...document.querySelectorAll('#sc-active-cart [data-asin]')].some(e => e.getAttribute('data-asin') === asin)", arg=asin, timeout=5000)
                except Exception as exc:
                    raise CartRejected('Amazon did not confirm that an item left the active cart') from exc
            saved = page.locator(f"#sc-saved-cart [data-asin='{asin}'], #sc-saved-cart-items [data-asin='{asin}']")
            try:
                await saved.wait_for(state='visible', timeout=5000)
            except Exception:
                await page.reload(wait_until='domcontentloaded')
                try:
                    await saved.wait_for(state='visible', timeout=5000)
                except Exception as exc:
                    raise CartRejected('Amazon did not verify the item in Saved for Later; review the cart') from exc
            if await saved.count() != 1:
                raise CartRejected('Amazon saved-item identity is ambiguous; review the cart')
        remaining = await self.get_cart(page)
        if any(line['asin'] != target_asin for line in remaining):
            raise CartRejected('Unrelated items remain in the active cart')

    async def prepare_checkout(self, page, asin, quantity):
        """Refuse automatic checkout of a mixed, unrecognized, or mismatched cart."""
        lines = await self.get_cart(page)
        if any(line['asin'] != asin for line in lines):
            await self.save_unrelated_cart_items(page, asin)
            lines = await self.get_cart(page)
        if len(lines) != 1 or lines[0]['asin'] != asin:
            raise Attention("Automatic checkout requires exactly the target product in the active cart")
        if lines[0]['quantity'] != quantity:
            raise Attention("Cart quantity cannot be verified or differs from the requested quantity")
        row = page.locator(f"#sc-active-cart [data-asin='{asin}']")
        titles = [value.splitlines()[0].strip() for value in await row.locator('.sc-product-title').all_inner_texts() if value.strip()]
        page._retail_cart_title = titles[0] if titles and len(set(titles)) == 1 else ''
        page._retail_cart_asin = asin
        try:
            button = await self.resolve_action(page,"BEGIN_CHECKOUT")
        except InteractionError as exc:
            raise Attention(str(exc)) from exc
        await behavior.click(page, button)
        await self.advance_checkout(page)

    async def advance_checkout(self, page):
        """Recover bounded, non-purchasing steps before verifying the final review.

        Recommendations are not cart contents. Only explicit checkout continuation
        or refusal of a modal offer can be clicked here; never Add or Place order.
        """
        for _ in range(5):
            # Wait for hydrated content instead of assuming checkout is ready
            # after one fixed sleep. This also handles same-URL transitions.
            for poll in range(40):
                await self.check(page)
                dialogs = page.locator('[role=dialog]:visible,dialog[open]:visible,[aria-modal=true]:visible')
                review = page.locator('#spc-orders [data-asin]:visible, #checkout-item-block [data-asin]:visible')
                final_review = page.locator("input[name='placeYourOrder1']:visible, #placeOrder:visible")
                continuation = page.get_by_role('link', name=re.compile(AMAZON_ACTIONS['CONTINUE_CHECKOUT'], re.I)).or_(page.get_by_role('button', name=re.compile(AMAZON_ACTIONS['CONTINUE_CHECKOUT'], re.I)))
                if await dialogs.count() or await review.count() or await final_review.count() or await continuation.count():
                    break
                await page.wait_for_timeout(250)
            if urlparse(page.url).hostname not in set(DOMAINS.values()):
                raise Attention('Checkout left the permitted retailer; review the browser')
            if await dialogs.count():
                action = 'DISMISS_CHECKOUT_OFFER'
            elif await review.count() or await final_review.count():
                return
            else:
                action = 'CONTINUE_CHECKOUT'
            try:
                control = await self.resolve_action(page, action)
                href = await control.evaluate("e => e.closest('a[href]')?.href || ''")
                if href and (urlparse(href).scheme != 'https' or urlparse(href).hostname not in set(DOMAINS.values())):
                    raise InteractionError('Checkout continuation leaves the permitted retailer')
                await behavior.click(page, control, timeout=5000)
                await page.wait_for_timeout(250)
            except InteractionError as exc:
                # AI is a bounded fallback after deterministic semantics. Keep
                # this message specific so the user knows whether the issue is
                # a missing continuation or a failed model/tool call.
                raise Attention(f'Checkout navigation needs review: {exc}. AI cannot bypass missing item or price evidence') from exc
        raise Attention('Checkout navigation did not reach a verifiable order review after five safe steps')

    async def checkout_snapshot(self, page, asin, quantity, max_total, *, max_unit_price=None, allow_third_party=False, allow_used=False):
        """Fail closed: only the known US checkout review structure may submit."""
        if urlparse(page.url).hostname != "www.amazon.com":
            raise Attention("Automatic order submission currently requires Amazon US")
        lines = page.locator("#spc-orders [data-asin], #checkout-item-block [data-asin]")
        if await lines.count() == 0 and '/checkout/' in urlparse(page.url).path:
            # Current Amazon review variants may move the item row outside
            # legacy order containers. The ASIN still has to be present in a
            # unique DOM-backed item row before this fallback can be used.
            lines = page.locator(f"[data-asin='{asin}']")
        modern = await lines.count() == 0 and urlparse(page.url).path.endswith('/spc')
        if modern:
            # The current SPC review omits the ASIN entirely. Bind its exact
            # product title to the single ASIN/quantity verified in the cart,
            # then cross-check the review's own item count, quantity, seller
            # and unit price. An unrelated cart item or missing fact fails.
            title = getattr(page, '_retail_cart_title', '')
            if getattr(page, '_retail_cart_asin', '') != asin or not title or len(title) < 8:
                raise Attention('Checkout product contents could not be verified')
            group = page.get_by_role('group', name=f'Change quantity of {title}', exact=True)
            if await group.count() != 1:
                raise Attention('Checkout product contents could not be verified')
            group_text = (await group.inner_text()).replace(title, '', 1)
            group_quantities = [int(value) for value in re.findall(r'\b\d+\b', group_text)]
            if not group_quantities or any(value != quantity for value in group_quantities):
                raise Attention('Checkout quantity could not be verified')
            body_text = await page.locator('body').inner_text()
            start = body_text.find(title)
            if start < 0 or title in body_text[:start]:
                raise Attention('Checkout product contents could not be verified')
            item_context = body_text[start + len(title):start + len(title) + 180]
            unit_price = money(item_context)
            seller_match = re.search(r'Ships from and sold by\s+([^\n]+)', item_context, re.I)
            seller = seller_match[1].strip() if seller_match else ''
            if re.search(r'\b(?:Condition\s*:\s*Used|Used\s*[-:]\s*(?:Like New|Very Good|Good|Acceptable))\b', item_context, re.I):
                raise Attention('Checkout item condition changed to used')
            if not allow_used and getattr(page, '_retail_product_condition', '') != 'new':
                raise Attention('Checkout item condition could not be verified as new')
            counts = [int(value) for value in re.findall(r'\bItems?\s*\((\d+)\)\s*:', body_text, re.I)]
            # The live SPC summary now says "Items: $..." without a count.
            # The product's named quantity group is the quantity evidence;
            # cross-check a summary count only when Amazon includes one.
            if counts and any(value != quantity for value in counts):
                raise Attention('Checkout item count differs from the requested quantity')
            item_rows = page.locator('li').filter(has_text=re.compile(r'^\s*Items?(?:\s*\(\d+\))?\s*:', re.I))
            item_values = [money(value) for value in await item_rows.all_inner_texts()]
            if not item_values or any(value is None or value != item_values[0] for value in item_values):
                raise Attention('Checkout item subtotal is missing or ambiguous')
        else:
            if await lines.count() != 1 or await lines.first.get_attribute("data-asin") != asin:
                raise Attention("Checkout product contents could not be verified")
            actual = await lines.first.get_attribute("data-quantity")
            if actual != str(quantity):
                raise Attention("Checkout quantity could not be verified")
            unit_price = money(await self.text(lines.first, ".a-price .a-offscreen, [data-unit-price]"))
            seller = await lines.first.get_attribute("data-seller") or await self.seller_text(lines.first)
        if unit_price is None or (max_unit_price is not None and unit_price > max_unit_price):
            raise Attention("Checkout unit price is unknown or exceeds the item's price limit")
        if not allow_third_party and not re.fullmatch(r"Amazon(?:\.com)?(?: Services LLC)?", seller or "", re.I):
            raise Attention("Checkout seller could not be verified as Amazon")
        if not modern:
            condition = await lines.first.get_attribute("data-condition")
            if condition is None:
                condition_match = re.search(r'\bCondition\s*:\s*(New|Used)\b', await lines.first.inner_text(), re.I)
                condition = condition_match[1].lower() if condition_match else None
            if not allow_used and condition != "new":
                raise Attention("Checkout item condition could not be verified as new")
        try:
            await page.wait_for_function("() => [...document.querySelectorAll('#subtotals-marketplace-table tr,li')].some(e => /^\\s*(?:(?:order|grand)\\s+total|total\\s+due|amount\\s+due|amount\\s+payable)\\s*:/i.test(e.textContent) && /[$£]\\s*[\\d,.]+/.test(e.textContent))", timeout=10000)
        except Exception as exc:
            if not self.agent or not self.store or (self.store.get('settings', 'settings') or {}).get('agent_mode') not in ('recovery', 'agent'):
                raise Attention('Final checkout order total did not appear within 10 seconds') from exc
        totals = page.locator("#subtotals-marketplace-table tr")
        if await totals.count() == 0:
            totals = page.locator('li').filter(has_text=re.compile(r'^\s*Order total\s*:', re.I))
        values = []
        for row in await totals.all():
            text = await row.inner_text()
            if re.search(r"^\s*Order total\s*:", text, re.I):
                values.append(money(text))
        if not values and self.agent and self.store and (self.store.get('settings', 'settings') or {}).get('agent_mode') in ('recovery', 'agent'):
            try:
                values = [await self.agent.resolve_total(page, set(DOMAINS.values()))]
            except InteractionError as exc:
                raise Attention('Final order total changed and AI could not verify it: ' + str(exc)) from exc
        if not values or any(value is None or value != values[0] for value in values) or values[0] > max_total:
            raise Attention("Order total is missing, ambiguous, or above the configured budget")
        try:
            button = getattr(page, '_retail_submit_control', None)
            if button is None:
                button = await self.resolve_action(page,"SUBMIT_ORDER")
            await button.click(trial=True, timeout=3000)
        except InteractionError as exc:
            raise Attention(str(exc)) from exc
        page._retail_submit_control = button
        page._retail_review = {'url': page.url, 'at': time.monotonic(), 'args': (asin, quantity, max_total),
                              'limits': dict(max_unit_price=max_unit_price, allow_third_party=allow_third_party, allow_used=allow_used)}
        components = []
        rows = page.locator('#subtotals-marketplace-table tr') if await page.locator('#subtotals-marketplace-table tr').count() else page.locator('li')
        for value in await rows.all_inner_texts():
            compact = ' '.join(value.split())
            match = re.match(r'^([^:]{2,55}):\s*(.*)$', compact)
            if not match or match[1].strip().lower() == 'order total':
                continue
            amount = money(match[2])
            if amount is None:
                continue
            if re.search(r'[-−]\s*(?:US\$|CA\$|CDN\$|[$£])', match[2]) or re.search(r'\(\s*[$£]', match[2]):
                amount = -amount
            entry = {'label': match[1].strip(), 'amount': amount}
            if entry not in components:
                components.append(entry)
        return {"total": values[0], "quantity": quantity, "asin": asin, "currency": "USD",
                "unit_price": unit_price, "price_components": components}

    async def submit_order(self, page):
        # Caller must persist the submission intent BEFORE invoking this method.
        review = getattr(page, '_retail_review', None)
        if not review or review['url'] != page.url or time.monotonic() - review['at'] > 60:
            raise Attention('Checkout review expired or changed; submission stopped')
        control = getattr(page, '_retail_submit_control', None)
        if control is not None:
            await behavior.prepare(page, control)
        if review['url'] != page.url or time.monotonic() - review['at'] > 60:
            raise Attention('Checkout review expired or changed during input preparation; submission stopped')
        # Check financial facts again after any agent round trips, using the
        # already validated control. There is no model call after journaling.
        await self.checkout_snapshot(page, *review['args'], **review['limits'])
        button = page._retail_submit_control
        page._retail_review = None
        page._retail_submit_control = None
        await button.click(no_wait_after=True)
        await page.wait_for_timeout(1800)

    async def confirmation(self, page):
        if urlparse(page.url).hostname not in DOMAINS.values():
            return None
        for _ in range(24):
            body = await page.locator('body').inner_text()
            if re.search(r'\b\d{3}-\d{7}-\d{7}\b', body) or 'order placed' in body.lower():
                break
            await page.wait_for_timeout(250)
        order = re.search(r"\b\d{3}-\d{7}-\d{7}\b", body)
        lower = body.lower()
        heading = page.get_by_role('heading', name=re.compile(r'order placed|order confirmed|thank you', re.I))
        confirmed = (any(text in lower for text in ("order placed", "order has been placed", "order confirmed", "thank you, your order"))
                     or await heading.count() > 0) and ('thank' in lower or 'placed' in lower or 'confirmed' in lower)
        if not confirmed:
            return None
        if order:
            return order[0]
        path = urlparse(page.url).path.lower()
        if path != '/gp/buy/thankyou/handlers/display.html':
            return None
        exact_heading = page.get_by_role('heading', name=re.compile(r'^order placed,? thanks!?$', re.I))
        if await exact_heading.count() != 1:
            return None
        task_id = getattr(page.context, '_retail_task_id', None)
        journal = self.store.get('submissions', 'submission-' + task_id) if self.store and task_id else None
        if journal:
            asin = journal.get('asin')
            quantity = journal.get('quantity')
            links = page.locator(f"a[href*='/dp/{asin}']") if asin else page.locator('a[href*="/dp/"]')
            if await links.count() != 1 or not quantity or not re.search(rf'\b{quantity}\s*$', await links.first.inner_text()):
                return None
        return 'amazon-confirmed-' + uuid.uuid4().hex

    async def free_shipping(self, page):
        options = page.locator("label").filter(has_text=re.compile(r"FREE.*(?:delivery|shipping)|(?:delivery|shipping).*FREE", re.I))
        for index in range(await options.count()):
            option = options.nth(index)
            radio = option.locator("input[type=radio]")
            if await radio.count() == 1 and await radio.is_visible():
                await radio.check()
                await page.wait_for_timeout(700)
                break
        rows = page.locator("#subtotals-marketplace-table tr").filter(has_text=re.compile(r"shipping|delivery", re.I))
        text = " ".join(await rows.all_inner_texts())
        charges = re.findall(r"\$\s*([\d,.]+)", text)
        if not text or not ("free" in text.lower() or charges and all(float(x.replace(",", "")) == 0 for x in charges)):
            raise Attention("Free shipping could not be verified; select a free option in the browser")

    async def verify_cvv(self, page, cvv):
        # Only a dedicated Amazon card-verification form; never a bank challenge or order button.
        if urlparse(page.url).hostname not in ("www.amazon.com", "amazon.com"):
            return False
        field = page.locator("input[name='cvv']:visible, input[name='CVV']:visible, input[name='addCreditCardVerificationNumber']:visible")
        if await field.count() != 1:
            return False
        form = field.locator("xpath=ancestor::form[1]")
        if await form.count() != 1:
            return False
        action = await form.get_attribute("action") or ""
        from urllib.parse import urljoin
        if urlparse(urljoin(page.url, action)).hostname not in ("www.amazon.com", "amazon.com"):
            return False
        button = form.get_by_role("button", name=re.compile(r"^(?:verify(?: card| payment)?|confirm card)$", re.I))
        if await button.count() != 1:
            return False
        await behavior.fill(page, field, cvv)
        await behavior.click(page, button)
        await page.wait_for_timeout(800)
        return True

    async def payment_verification(self, page):
        body = (await page.locator("body").inner_text()).lower()
        return bool(await page.locator("input[name*='cvv']:visible, input[name*='CVV']:visible, iframe[src*='3ds']:visible").count()) or any(phrase in body for phrase in ("payment verification required", "verify your payment", "verify your card", "approve this payment", "payment revision needed"))

    async def close(self):
        try:
            for watcher in list(self.login_watchers.values()):
                watcher.cancel()
            if self.login_watchers:
                await asyncio.gather(*self.login_watchers.values(), return_exceptions=True)
            self.login_watchers.clear()
            for context in list(self.context_accounts):
                await context.close()
            if self.profile_close_tasks:
                await asyncio.gather(*self.profile_close_tasks, return_exceptions=True)
            for browser in list(self.profile_browsers):
                await browser.close()
            self.profile_browsers.clear()
            if self.browser and not self.cdp_attached:
                await self.browser.close()
        finally:
            # Driver teardown disconnects CDP without closing external Chrome.
            if self.driver:
                await self.driver.stop()
            self.driver = self.browser = None
            self.logins.clear()
            self.fingerprint_tests.clear()
            self.context_accounts.clear()


class AccessDenied(Attention):
    pass
