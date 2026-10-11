import asyncio
import json
import math
import profile
import re
import time
import uuid
import tempfile
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from .fingerprint import build_scripts
from .native_fingerprint import launch_options as native_launch_options, needs_profile_browser
from .worker_profiles import WorkerProfiles, debugging_port
from .fingerprint_suite import build_profile as build_suite_profile
from . import us_fingerprint, proxy_location, behavior
from urllib.parse import urlparse, urljoin

from patchright.async_api import async_playwright, TimeoutError as BrowserTimeoutError

from .models import DOMAINS, proxy_config, Settings, account_fingerprint_settings
from .identity import IdentityService
from .store import now
from .interactions import resolve, InteractionError
from .browser_bridge import validate_endpoint
from .browser_agent import BrowserAgent
from .browser_recovery import RecoveryController
from .browser_mcp import AMAZON_ACTIONS
from .browser_visibility import set_visible
from .browser_runtime import browser_options, extension_paths
from .fingerprint_profiles import generated, inspect_hardware
from .account_consistency import AccountBrowserProfiles
from .performance import stage, timed
from .amazon_states import PAGE_STATE


def money(text: str) -> float | None:
    match = re.search(r"(?:US\$|CA\$|CDN\$|[$£])\s*([\d,]+(?:\.\d{2})?)", text)
    return float(match[1].replace(",", "")) if match else None


def same_product_page(actual, expected):
    """Amazon may decorate/redirect a product URL without changing the ASIN."""
    a, b = urlparse(actual), urlparse(expected or '')
    if (a.scheme != 'https' or b.scheme != 'https' or a.netloc != b.netloc
            or a.hostname not in DOMAINS.values() or a.username or a.password):
        return False
    def product(path):
        match = re.search(r'/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:/|$)', path, re.I)
        return match[1].upper() if match else None
    target = product(b.path)
    return bool(target and product(a.path) == target)


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
        self.identity_hardware = {}
        self.hardware_lock = asyncio.Lock()
        self.browser_key = None
        self.logins = {}
        self.fingerprint_tests = {}
        self.fingerprint_test_lock = asyncio.Lock()
        self.login_watchers = {}
        self.launch_lock = asyncio.Lock()
        self.session_probe_slots = asyncio.Semaphore((store.get('settings', 'settings') or {}).get('max_running_tasks', 10) if store else 10)
        self.identities = IdentityService(store)
        self.context_accounts = {}
        self.profile_browsers = set()
        self.profile_close_tasks = set()
        self.cdp_attached = False
        self.browser_visible = False
        self.browser_initially_visible = False
        self.agent = BrowserAgent(store) if store else None
        self.recovery = RecoveryController(store)
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
            return max(0, int(raw))
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = math.ceil((when - datetime.now(timezone.utc)).total_seconds())
            return max(0, seconds)
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

    @timed('Page navigation')
    async def navigate(self, page, url: str, **kwargs):
        response = await page.goto(url, **kwargs)
        self._raise_for_response(response)
        bridge = getattr(page.context, '_retail_worker_profiles', None)
        if bridge and bridge.errors:
            raise Attention('Worker fingerprint initialization failed: ' + bridge.errors[0])
        return response

    async def resolve_action(self, page, action):
        page._retail_expected_action = action
        try:
            # All persisted modes, including legacy "agent", are deterministic first.
            return await resolve(page, action)
        except InteractionError as exc:
            if 'multiple' in str(exc):
                raise
            settings = (self.store.get('settings', 'settings') or {}) if self.store else {}
            return await self.recovery.resolve(page, action, self.agent,
                                               allow_ai=settings.get('agent_mode', 'off') != 'off')

    @timed('Browser engine readiness')
    async def ready(self, settings=None):
        async with self.launch_lock:
            if not self.driver:
                self.driver = await async_playwright().start()
            if not self.browser or not self.browser.is_connected():
                self.identity_hardware.clear()
                settings = settings if settings is not None else (self.store.get("settings", "settings") or {})
                self.browser_initially_visible = bool(settings.get('show_browser_window', True))
                # Window mode is explicit. The dashboard controls the same page
                # during intervention; a headless process cannot become a GUI
                # in place.
                options = {"headless": not self.browser_initially_visible, "args": ["--enable-unsafe-extension-debugging"], "ignore_default_args": ["--disable-extensions"]}
                # Keep the shared browser on the configured standard channel.
                # Native profiles always own their process, so accounts can mix
                # implementations without borrowing another backend's identity.
                options.update(browser_options({**settings, "fingerprint_backend": "javascript"}))
                self.browser_key = (settings.get("browser_identity", "default"), settings.get("browser_channel", "chromium"))
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
        elif getattr(page.context, '_retail_headless', not self.browser_initially_visible):
            return  # The interactive dashboard shares this headless page.
        else:
            await set_visible(page, True)
        self.browser_visible = True

    async def hide(self, page):
        if self.cdp_attached or getattr(page.context, '_retail_headless', not self.browser_initially_visible):
            return  # External windows are user-owned; headless needs no hiding.
        await set_visible(page, False)
        self.browser_visible = False

    def account_proxy(self, account):
        if account.get("proxy"):
            return account["proxy"]
        from .proxy_pool import ProxyPool
        return ProxyPool(self.store).choose(account.get("proxy_list_id", ""), account["id"])

    @timed('Browser hardware profile')
    async def hardware_profile(self, settings=None):
        settings = settings if settings is not None else (self.store.get('settings', 'settings') or {})
        # Measure an unmodified engine, including for native profiles. Never put
        # the local inspection page into the visible or user-owned CDP browser.
        options = browser_options({**settings, 'fingerprint_backend': 'javascript'})
        key = tuple(sorted(options.items()))
        async with self.hardware_lock:
            if key not in self.identity_hardware:
                probe = await self.driver.chromium.launch(**options, headless=True)
                try:
                    hardware = await asyncio.wait_for(inspect_hardware(probe), timeout=30)
                finally:
                    await probe.close()
                self.identity_hardware[key] = hardware
            return self.identity_hardware[key]

    async def prewarm(self):
        """Pay the local engine/probe cost before the first live task starts."""
        tasks = [task for task in self.store.all('tasks') if not task.get('simulation', True)]
        if not tasks:
            return
        account = self.store.get('accounts', tasks[0].get('account_id', ''))
        if not account:
            return
        try:
            settings = account_fingerprint_settings(self.store.get('settings', 'settings') or {}, account)
            if settings.get('cdp_attach'):
                return
            await self.ready(settings)
            await self.hardware_profile(settings)
        except Exception:
            self.store.event('', 'browser_warmup', 'Browser warmup unavailable; task startup will retry')

    @timed('Browser context setup')
    async def context(self, account, proxy=None, solver_id="", *, interactive=False, headless=None):
        settings = account_fingerprint_settings(self.store.get("settings", "settings") or {}, account)
        if headless is not None:
            if settings.get('cdp_attach'):
                raise ValueError('Session window overrides require an app-managed browser')
            settings = {**settings, 'show_browser_window': not headless}
        identity_launch = browser_options(settings)
        extensions = extension_paths(self.store, settings.get('browser_extension_ids', []))
        custom_extensions = bool(extensions)
        if settings.get('cdp_attach') and (extensions or not settings.get('browser_incognito', True)):
            raise ValueError('Normal profiles and managed extensions require an app-managed browser.')
        if not settings.get('cdp_attach'):
            extensions = [str(Path(__file__).resolve().parents[1] / 'browser-extensions' / 'runtime-bridge'), *extensions]
        await self.ready(settings)
        if interactive:
            # Account sign-in is a desktop interaction, independent of task
            # window mode. Do not mutate saved settings or the task browser.
            settings = {**settings, 'show_browser_window': True}
        options = self.profiles.options(account)
        us_options = us_fingerprint.context_options(self.profiles.get(account), settings, account['region'])
        options.update(us_options)
        profile = self.profiles.get(account)
        hardware = await self.hardware_profile(settings)
        values = (generated(profile, account.get('fingerprint_values', {}), hardware)
                  if settings.get('fingerprint_backend') != 'fingerprint-suite' else {})
        if settings.get('fingerprint_screen') and us_options:
            options.update({key: values[key] for key in ('screen', 'viewport', 'device_scale_factor')})
        # Headed browsers already supply their real identity. Even an identical
        # --user-agent override discards detailed native UA client hints.
        browser_identity = (hardware['user_agent'].replace('HeadlessChrome/', 'Chrome/')
                            if settings.get('fingerprint_backend') == 'javascript' and settings.get('fingerprint_navigator')
                            and not settings.get('show_browser_window', False) else None)
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
        managed_workers = not native and not suite and (needs_profile_browser(settings) or us_fingerprint.enabled(settings))
        if managed_workers and self.cdp_attached:
            raise ValueError('Complete JavaScript graphics profiles require an app-managed browser; disable external CDP attachment.')
        owned_browser = None
        worker_profiles = None
        worker_port = None
        profile_directory = None
        temporary_profile = None
        saved_state = None
        launch = None
        persistent = not settings.get('browser_incognito', True)
        different_browser = self.browser_key != (settings.get('browser_identity', 'default'), settings.get('browser_channel', 'chromium'))
        if native:
            seed = int(profile['seed'], 16) & 0xffffffff
            launch = native_launch_options({**settings, 'fingerprint_timezone': options.get('timezone_id', settings['fingerprint_timezone'])}, seed, values)
        elif (managed_workers or persistent or interactive or different_browser or custom_extensions
              or self.browser_initially_visible != settings.get('show_browser_window', True)):
            launch = {'headless': not settings.get('show_browser_window', True), **identity_launch}
        if launch is not None:
            if 'executable_path' not in launch:
                launch.setdefault('channel', 'chromium')
            args = launch.setdefault('args', [])
            if us_options:
                args += ['--lang=en-US', '--accept-lang=en-US']
            if browser_identity:
                args.append('--user-agent=' + browser_identity)
            if managed_workers:
                worker_port = debugging_port()
                args += [f'--remote-debugging-port={worker_port}', '--remote-debugging-address=127.0.0.1']
            if extensions:
                args += ['--enable-unsafe-extension-debugging']
                launch['ignore_default_args'] = ['--disable-extensions']
        try:
            if persistent:
                if account.get('_fingerprint_test'):
                    temporary_profile = tempfile.TemporaryDirectory(prefix='retail-fingerprint-')
                    profile_directory = temporary_profile.name
                else:
                    identity_key = [account['id'], settings['fingerprint_backend'], settings['browser_identity'],
                                    launch.get('channel'), launch.get('executable_path')]
                    key = hashlib.sha256(json.dumps(identity_key).encode()).hexdigest()
                    profile_directory = str(self.store.folder / 'browser-profiles' / key)
                initialized = Path(profile_directory) / '.retail-state-initialized'
                saved_state = options.pop('storage_state', None)
                context = await self.driver.chromium.launch_persistent_context(profile_directory, **launch, **options)
                owned_browser = context.browser
                # Import once, after extensions are ready. Never overwrite
                # newer storage from the persistent profile on later launches.
                if initialized.exists():
                    saved_state = None
            else:
                if launch is not None:
                    owned_browser = await self.driver.chromium.launch(**launch)
                saved_state = options.pop('storage_state', None)
                context = await (owned_browser or self.browser).new_context(**options)
            if owned_browser:
                self.profile_browsers.add(owned_browser)
            if extensions:
                extension_session = await (owned_browser or self.browser).new_browser_cdp_session()
                try:
                    if persistent:
                        installed = await extension_session.send('Extensions.getExtensions')
                        allowed = {str(Path(path).resolve()).casefold() for path in extensions}
                        for item in installed['extensions']:
                            if str(Path(item['path']).resolve()).casefold() not in allowed:
                                await extension_session.send('Extensions.uninstall', {'id': item['id']})
                    for path in extensions:
                        try:
                            with stage('Load browser extension'):
                                await asyncio.wait_for(extension_session.send('Extensions.loadUnpacked', {'path': path, 'enableInIncognito': settings.get('browser_incognito', True)}), 5)
                        except asyncio.TimeoutError as exc:
                            raise ValueError('A browser extension did not finish loading. Check the selected extensions before restarting the task.') from exc
                finally:
                    # Cleanup must not replace a useful startup error (or task
                    # cancellation) with TargetClosedError.
                    try:
                        await extension_session.detach()
                    except Exception:
                        pass
            # Loading extensions after session restoration can hang Chrome's
            # incognito extension initialization. This ordering is intentional.
            if saved_state:
                with stage('Restore account session'):
                    await context.set_storage_state(saved_state)
            if persistent:
                initialized.touch()
        except BaseException:
            if owned_browser:
                await owned_browser.close()
                self.profile_browsers.discard(owned_browser)
            if temporary_profile:
                temporary_profile.cleanup()
            raise

        def release_context(_):
            self.context_accounts.pop(context, None)
            if self.logins.get(account['id']) is context:
                self.logins.pop(account['id'], None)
                watcher = self.login_watchers.pop(account['id'], None)
                if watcher and watcher is not asyncio.current_task():
                    watcher.cancel()
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
                            if temporary_profile:
                                temporary_profile.cleanup()
                task = asyncio.create_task(close_owned())
                context._retail_close_task = task
                self.profile_close_tasks.add(task)
                def finished(done):
                    self.profile_close_tasks.discard(done)
                    if not done.cancelled():
                        done.exception()  # Observe teardown errors; close() also drains owners.
                task.add_done_callback(finished)
        context.on('close', release_context)
        context._retail_interactive_window = interactive and not self.cdp_attached
        context._retail_headless = not settings.get('show_browser_window', True) and not self.cdp_attached
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
                    profile_values=values,
                    restrict_fonts=settings.get('fingerprint_fonts', False),
                    intercept_workers=not native and not managed_workers and settings.get('fingerprint_workers', False),
                )
                for script in scripts:
                    await context.add_init_script(script)
                if managed_workers:
                    with stage('Initialize browser workers'):
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
            context = await self.context({**clean_account, '_fingerprint_test': True}, interactive=True)
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
            existing = self.logins[account['id']]
            pages = [page for page in existing.pages if not page.is_closed()]
            if pages and (not existing.browser or existing.browser.is_connected()):
                await self.expose(pages[0])
                return
            self.logins.pop(account['id'], None)
            await existing.close()
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
        previous = None
        try:
            while self.logins.get(account_id) is context and not page.is_closed() and time.monotonic() < deadline:
                # Wait inside the browser for a signed-in header. After a failed
                # verification require a changed page state before trying again.
                handle = await page.wait_for_function('''previous => {
                    if (!['www.amazon.com', 'www.amazon.co.uk', 'www.amazon.ca'].includes(location.hostname)) return false;
                    const label = document.querySelector('#nav-link-accountList .nav-line-1');
                    const value = label?.getClientRects().length ? label.innerText.trim() : '';
                    if (!value || /sign in/i.test(value) || location.pathname.includes('/ap/')) return false;
                    const blocked = [...document.querySelectorAll('#captchacharacters, #auth-error-message-box, #auth-warning-message-box')].some(e => e.getClientRects().length);
                    if (blocked || /access denied|robot check|verify your identity|verify it's you|additional verification required|no default payment method|no default address/i.test(document.body?.innerText || '')) return false;
                    const signature = performance.timeOrigin + '|' + location.href + '|' + value;
                    return signature !== previous ? signature : false;
                }''', arg=previous, timeout=max(1, (deadline - time.monotonic()) * 1000))
                try:
                    previous = await handle.json_value()
                finally:
                    await handle.dispose()
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
                                continue
                        await context.close()
                        self.logins.pop(account_id, None)
                        return
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

    async def wait_for_signin_step(self, page, selector):
        """Advance when the submitted form changes, without a fixed delay."""
        try:
            await page.wait_for_function('''selector => {
                const visible = e => e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
                const oldVisible = [...document.querySelectorAll(selector)].some(visible);
                const error = [...document.querySelectorAll('#auth-error-message-box, #auth-warning-message-box, #captchacharacters')].some(visible);
                const next = [...document.querySelectorAll('#ap_email, #ap_email_login, #ap_password, #auth-mfa-otpcode, #cvf-input-code, #nav-link-accountList .nav-line-1')].some(visible);
                const challenge = /verify your identity|verify it's you|additional verification required|robot check|click the button below to continue shopping/i.test(document.body?.innerText || '');
                const passwordStep = selector.includes('#ap_email') && visible(document.querySelector('#ap_password'));
                return error || challenge || passwordStep || (!oldVisible && next);
            }''', arg=selector, timeout=10000)
        except BrowserTimeoutError as exc:
            raise AuthenticationRequired('Sign-in did not advance. Review the task browser, then Resume.') from exc
        if await page.locator('#captchacharacters:visible').count():
            raise ChallengeDetected('Amazon requires CAPTCHA verification. Complete it in the task browser, then Resume.', kind='captcha')
        if await page.locator('#auth-error-message-box:visible, #auth-warning-message-box:visible').count():
            raise AuthenticationRequired('Amazon could not complete sign-in. Review the account details in the task browser, then Resume.')

    @timed('Account sign-in')
    async def authenticate(self, page, account):
        """Fill only the current account's Amazon sign-in fields, with bounded steps."""
        # Credential entry always uses the existing bounded mouse/keyboard
        # controller. This does not slow unrelated product/checkout actions.
        paced = getattr(page, '_retail_paced_controller', None)
        if paced is None:
            paced = page._retail_paced_controller = behavior.PacedInput(page)
        for _ in range(6):
            origin = urlparse(page.url)
            if origin.scheme != 'https' or origin.netloc != DOMAINS[account.get('region', 'US')]:
                raise AuthenticationRequired('Sign-in left this account’s Amazon region; credentials were not entered.')
            try:
                await page.wait_for_function('''() => {
                    const visible=e=>e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
                    return [...document.querySelectorAll('#ap_email,#ap_email_login,#ap_password,#auth-mfa-otpcode,#cvf-input-code,#captchacharacters,#auth-error-message-box,#auth-warning-message-box')].some(visible)
                        || [...document.querySelectorAll('#nav-link-accountList .nav-line-1')].some(e=>visible(e) && e.innerText.trim() && !/sign in/i.test(e.innerText))
                        || /verify your identity|verify it's you|additional verification|robot check|continue shopping/i.test(document.body?.innerText || '');
                }''', timeout=10000)
            except BrowserTimeoutError as exc:
                raise AuthenticationRequired('Amazon did not present a supported sign-in step; review View live.') from exc
            origin = urlparse(page.url)
            if origin.scheme != 'https' or origin.netloc != DOMAINS[account.get('region', 'US')]:
                raise AuthenticationRequired('Sign-in left this account’s Amazon region; credentials were not entered.')
            body = (await page.locator('body').inner_text()).lower()
            if re.search(r"verify your identity|verify it's you|additional verification|robot check|continue shopping", body):
                await self.check(page)
            if await page.locator('#captchacharacters:visible, #auth-error-message-box:visible, #auth-warning-message-box:visible').count():
                await self.check(page)
                raise AuthenticationRequired('Amazon rejected sign-in. Check the saved account credentials before resuming.')
            email = page.locator('#ap_email:visible, #ap_email_login:visible')
            password = page.locator('#ap_password:visible')
            if await password.count():
                if not account.get('password'):
                    raise AuthenticationRequired('Save a password for this account, or complete sign-in in View live.')
                # Combined email/password layouts must fill both fields before submit.
                if await email.count() and account.get('email'):
                    await self.validate_signin_destination(email, account)
                    if (await email.input_value()).strip().casefold() != account['email'].strip().casefold():
                        await paced.fill(email, account['email'])
                await self.validate_signin_destination(password, account)
                await paced.fill(password, account['password'])
                submit = page.locator('#signInSubmit:visible')
                await self.validate_signin_destination(submit, account)
                await paced.click(submit)
                await self.wait_for_signin_step(page, '#ap_password')
            elif await email.count():
                if not account.get('email'):
                    raise AuthenticationRequired('Save an email or username for this account, or complete sign-in in View live.')
                await self.validate_signin_destination(email, account)
                await paced.fill(email, account['email'])
                submit = page.locator('#continue:visible')
                await self.validate_signin_destination(submit, account)
                await paced.click(submit)
                await self.wait_for_signin_step(page, '#ap_email, #ap_email_login')
            elif await page.locator("#auth-mfa-otpcode:visible, #cvf-input-code:visible").count() and account.get("auto_otp"):
                try:
                    await self.fill_otp(page, account)
                except ValueError:
                    return
            else:
                return

    async def validate_signin_destination(self, control, account):
        if await control.count() != 1:
            raise AuthenticationRequired('The sign-in control is missing or ambiguous; review View live.')
        destination = await control.evaluate("e => e.hasAttribute('formaction') ? e.formAction : (e.form?.action || location.href)")
        target = urlparse(destination)
        if target.scheme != 'https' or target.netloc != DOMAINS[account.get('region', 'US')]:
            raise AuthenticationRequired('The sign-in form leaves this account’s Amazon region; credentials were not submitted.')

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
            await self.wait_for_signin_step(page, '#auth-mfa-otpcode, #cvf-input-code')

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

    @timed('Verify account session')
    async def ensure_session(self, context, account, page):
        """Prepare and verify a login in the same context the checkout will use."""
        page._retail_initial_product = None
        start_url = getattr(page, '_retail_start_url', None)
        # On Resume, inspect the existing challenge first. Navigating away from
        # an MFA/passkey page can invalidate the user's in-progress verification.
        resuming_auth = urlparse(page.url).hostname in DOMAINS.values() and ("/ap/" in urlparse(page.url).path or await page.locator("#ap_email, #ap_email_login, #ap_password, #auth-mfa-otpcode, #captchacharacters").count())
        if resuming_auth:
            await self.authenticate(page, account)
            await self.check(page)
        if not (resuming_auth and same_product_page(page.url, start_url)):
            await self.navigate(page, start_url or f"https://{DOMAINS[account['region']]}/gp/your-account/order-history", wait_until="domcontentloaded")
        if 'click the button below to continue shopping' in (await page.locator('body').inner_text()).lower():
            await self.check(page)
        # Product pages do not redirect signed-out shoppers automatically, unlike
        # Your Orders. Follow only Amazon's own sign-in link when needed.
        label = await self.wait_session_header(page)
        if start_url and 'sign in' in label.lower():
            sign_in = page.locator('#nav-link-accountList')
            href = await sign_in.evaluate("e => e.href || ''") if await sign_in.count() == 1 else ''
            parsed = urlparse(href)
            if parsed.scheme == 'https' and parsed.netloc == DOMAINS[account['region']] and parsed.path.startswith(('/ap/signin', '/gp/sign-in')):
                await self.navigate(page, href, wait_until='domcontentloaded')
            else:
                # Some Amazon headers use JavaScript or an account-home link.
                # The protected orders route supplies Amazon's own sign-in
                # redirect without guessing parameters or visiting a foreign URL.
                await self.navigate(page, f"https://{DOMAINS[account['region']]}/gp/your-account/order-history", wait_until='domcontentloaded')
        if not label or 'sign in' in label.lower():
            await self.authenticate(page, account)
        await self.check(page)
        if start_url and not same_product_page(page.url, start_url):
            await self.navigate(page, start_url, wait_until='domcontentloaded')
            await self.check(page)
        label = await self.wait_session_header(page)
        if not label or "sign in" in label.lower() or "/ap/" in page.url:
            raise AuthenticationRequired("Account sign-in is not verified. Complete login in this browser, then Resume.")
        # Single-use, account-context-local navigation handoff; offer and access
        # checks still read the live document immediately before carting.
        page._retail_initial_product = start_url if same_product_page(page.url, start_url) else None
        current = self.store.get("accounts", account["id"])
        if current:
            await self.check_browser_health(page, account)
            with stage('Save verified session'):
                current.update(session=await context.storage_state(indexed_db=True), session_storage=await self.capture_session_storage(page),
                               logged_in=True, session_saved_at=now(), last_login=now())
                self.store.put("accounts", current)
                self.store.put("sessions", {"account_id":account['id'],"retailer":account.get('retailer','amazon'),"status":"ready","last_login":now(),"network":account.get('proxy_list_id','')}, 'session-'+account['id'])

    async def probe_session(self, account, connection):
        from types import SimpleNamespace
        from .session_probe import probe_session
        def guard(response):
            # Preserve existing retailer cooldown/access-denied semantics.
            self._raise_for_response(SimpleNamespace(status=response.status_code, headers=response.headers))
        async with self.session_probe_slots:
            return await probe_session(account, connection, response_guard=guard)

    @timed('Wait for account header')
    async def wait_session_header(self, page):
        """Wait for header hydration or an authentication interruption, not a reload."""
        try:
            handle = await page.wait_for_function('''() => {
                const visible = e => e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
                const labels = [...document.querySelectorAll('#nav-link-accountList .nav-line-1')].filter(visible);
                const label = labels.length === 1 ? labels[0].innerText.trim() : '';
                const blocked = [...document.querySelectorAll('#ap_email,#ap_email_login,#ap_password,#auth-mfa-otpcode,#cvf-input-code,#captchacharacters')].some(visible)
                    || /access denied|robot check|verify your identity|verify it's you|additional verification required|click the button below to continue shopping/i.test(document.body?.innerText || '');
                return blocked || location.pathname.startsWith('/ap/') ? {label:''} : label ? {label} : false;
            }''')
            try:
                return (await handle.json_value())['label']
            finally:
                await handle.dispose()
        except BrowserTimeoutError as exc:
            raise AuthenticationRequired('Account header did not become ready. Review sign-in in this browser, then Resume.') from exc

    @timed('Verify browser health')
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

    @timed('Page access checks')
    async def check(self, page):
        account = self.context_accounts.get(page.context, {})
        async def snapshot():
            return await page.evaluate('''() => {
                const visible = s => [...document.querySelectorAll(s)].some(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
                return {body:(document.body?.innerText || '').slice(0,30000).toLowerCase(),
                    otpVisible:visible('#auth-mfa-otpcode,#cvf-input-code'),
                    captcha:visible('#captchacharacters'),
                    otp:!!document.querySelector('#auth-mfa-otpcode,#cvf-input-code'),
                    password:visible('#ap_password,#ap_email,#ap_email_login')};
            }''')
        observed = await snapshot()
        if urlparse(page.url).hostname in DOMAINS.values():
            # Account-owned OTP may still be filled automatically. Retailer
            # anti-bot/image challenges are never solved here; they are
            # detected and escalated for manual review.
            if observed['otpVisible'] and account.get("auto_otp"):
                try:
                    await self.fill_otp(page, account)
                except ValueError:
                    pass
                observed = await snapshot()

        body = observed['body']
        path = urlparse(page.url).path.lower()

        if "access denied" in body:
            raise AccessDenied("Access Denied; account or connection was refused")

        if (
            observed['captcha']
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
            # A plain navigation confirmation is not a CAPTCHA. Click only its
            # unique, exact control on Amazon, once per page lifetime. Never loop
            # on a recurring interstitial or submit a form to another origin.
            message = ('Amazon Continue shopping could not be completed automatically. '
                       'Review the task browser, then Resume.')
            if any(text in body for text in ("verify it's you", 'verify your identity', 'additional verification required')):
                raise ChallengeDetected('Amazon requires identity verification; complete it in the task browser, then Resume.', kind='identity-verification')
            url = urlparse(page.url)
            controls = page.get_by_role('button', name=re.compile(r'^continue shopping$', re.I))
            visible = [node for node in await controls.all() if await node.is_visible() and await node.is_enabled()]
            if (url.scheme != 'https' or url.hostname not in DOMAINS.values()
                    or getattr(page, '_retail_continue_attempted', False) or len(visible) != 1):
                raise ChallengeDetected(message, kind='continue-shopping')
            control = visible[0]
            destination = await control.evaluate("e => e.hasAttribute('formaction') ? e.formAction : (e.form?.action || e.closest('a')?.href || location.href)")
            target = urlparse(urljoin(page.url, destination))
            if target.scheme != 'https' or target.netloc != url.netloc:
                raise ChallengeDetected(message, kind='continue-shopping')
            page._retail_continue_attempted = True
            responses = []
            def record_navigation(response):
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    responses.append(response)
            page.on('response', record_navigation)
            try:
                await control.click(timeout=5000)
                await page.wait_for_function("!document.body?.innerText.toLowerCase().includes('click the button below to continue shopping')", timeout=10000)
            except Exception as exc:
                for response in responses:
                    self._raise_for_response(response)
                raise ChallengeDetected(message, kind='continue-shopping') from exc
            finally:
                page.remove_listener('response', record_navigation)
            for response in responses:
                self._raise_for_response(response)
            if urlparse(page.url).netloc != url.netloc:
                raise Attention('Unexpected destination after Continue shopping; review the browser')
            await self.check(page)
            return

        if any(x in body for x in ("verify it's you", "verify your identity", "additional verification required")):
            raise ChallengeDetected(
                "Amazon requires additional verification. Complete it in the task browser, then Resume.",
                kind="identity-verification",
            )

        if "no default payment method" in body:
            raise Attention("No Default Payment Method: check the default card in your Amazon account")
        if "no default address" in body:
            raise Attention("No Default Address: select a default shipping address in your Amazon account")

        if observed['otp']:
            raise AuthenticationRequired("Amazon needs account verification. Complete it in the task browser, then Resume.")
        if "/ap/signin" in page.url or observed['password']:
            raise AuthenticationRequired("Amazon requires sign-in; restoring this account's saved session.")

    async def inspect_stock(self, page, item, region):
        """Read inventory without AI recovery or checkout-control resolution."""
        return await self.inspect(page, item, region, inventory_only=True)

    @timed('Product inspection total')
    async def inspect(self, page, item, region, *, inventory_only=False):
        target = f"https://{DOMAINS[region]}/dp/{item['asin']}"
        reuse_initial = getattr(page, '_retail_initial_product', None) == target and same_product_page(page.url, target)
        page._retail_initial_product = None
        response = None if reuse_initial else await self.navigate(page, target, wait_until="domcontentloaded", timeout=45000)
        await self.check(page)
        return await self.read_product(page, item, inventory_only=inventory_only)

    @timed('Read product details')
    async def read_product(self, page, item, *, inventory_only=False):
        # One browser round trip for passive product fields. Keep action
        # resolution and seller verification separate; they have stricter rules.
        details = await page.evaluate('''() => {
            const first = selector => (document.querySelector(selector)?.innerText || '').trim();
            const visibleText = selector => [...document.querySelectorAll(selector)]
                .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
                .map(e => e.innerText).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
            const metadata = document.querySelectorAll("meta[property='product:price:amount'], meta[itemprop='price']");
            const images = document.querySelectorAll('#landingImage');
            return {
                title: first('#productTitle'),
                price: first('#corePrice_feature_div .a-price .a-offscreen, #corePriceDisplay_desktop_feature_div .a-price .a-offscreen, #priceblock_ourprice'),
                metadata: metadata.length === 1 ? metadata[0].getAttribute('content') || '' : '',
                original: first('#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen, #corePrice_feature_div .a-text-price .a-offscreen'),
                offer: document.querySelector("input[name='offerListingID'], input[name='offeringID.1']")?.getAttribute('value') || '',
                condition: first('#condition, #condition-value').toLowerCase(),
                used: !!document.querySelector('#usedBuySection') && !document.querySelector('#newBuyBoxPrice, #newBuyBox'),
                image: images.length === 1 ? images[0].getAttribute('src') || '' : '',
                stock: visibleText('#availability, #availabilityInsideBuyBox_feature_div, #outOfStock'),
                account: visibleText('#nav-link-accountList .nav-line-1'),
                purchase: visibleText('#buybox, #desktop_buybox, #buybox_feature_div, #rightCol, #deliveryBlockMessage')
            };
        }''')
        if not inventory_only and 'sign in' in details.get('account', ''):
            raise AuthenticationRequired('Account session expired; saved credentials will be used to sign in again.')
        title = details['title']
        if not title:
            heading=page.get_by_role("heading",level=1)
            if await heading.count()==1: title=(await heading.inner_text()).strip()
        if not title:
            raise ValueError("Product page is unavailable or its layout is unsupported")
        price = money(details['price'])
        if price is None:
            # Structured product metadata is a bounded fallback for layout
            # changes. Final checkout still verifies its own price and total.
            raw = details['metadata']
            if re.fullmatch(r'\d{1,7}(?:\.\d{1,2})?', raw):
                price = float(raw)
        original = money(details['original'])
        seller = await self.seller_text(page, product_page=True)
        agent_error = ''
        try:
            if inventory_only:
                # A read-only stock signal needs neither an action locator nor
                # a model request. A strict native control is a fallback when
                # the page has no explicit inventory label.
                cart = page.locator('#add-to-cart-button, input[name="submit.add-to-cart"]').or_(
                    page.get_by_role('button', name=re.compile(AMAZON_ACTIONS['ADD_TO_CART'], re.I)))
            else:
                with stage('Resolve purchase control'):
                    cart = await resolve(page,"ADD_TO_CART")
        except InteractionError:
            cart = page.get_by_role('button', name=re.compile(AMAZON_ACTIONS['ADD_TO_CART'], re.I))
            if not inventory_only:
                try:
                    with stage('Purchase-control recovery'):
                        cart = await self.resolve_action(page, 'ADD_TO_CART')
                except InteractionError as exc:
                    agent_error = str(exc)
        unique = await cart.count() == 1 if hasattr(cart, 'count') else True
        cart_available = unique and await cart.is_visible() and await cart.is_enabled()
        # Inventory and permission to buy are different. Grocery pages can say
        # "In Stock" while replacing the purchase controls with sign-in or a
        # delivery-location restriction. Ignore navigation and hidden templates.
        evidence = details
        stock_text = evidence['stock']
        out_of_stock = bool(re.search(r'currently unavailable|out of stock|not in stock', stock_text))
        in_stock = bool(re.search(r'\bin stock\b', stock_text)) and not out_of_stock
        restrictions = []
        if re.search(r'(?:cannot|can\'t|can not) be (?:shipped|delivered) to (?:your|the) (?:selected )?(?:delivery )?location', evidence['purchase']):
            restrictions.append('Unavailable for the selected delivery location')
        if 'sign in to get started' in evidence['purchase']:
            restrictions.append('Sign in required for this offer')
        available = cart_available and not out_of_stock and not restrictions
        stock_status = 'unavailable' if out_of_stock else 'available' if in_stock or available else 'unknown'
        stock_message = {'available': 'In stock', 'unavailable': 'Out of stock', 'unknown': 'Stock could not be verified'}[stock_status]
        if not available and not restrictions and stock_status != 'unavailable':
            restrictions.append('Add-to-cart control could not be verified')
        availability_message = '; '.join([stock_message, *restrictions])
        return {"asin": item["asin"], "title": title, "price": price,
                "original_price": original, "offer_id": details['offer'],
                "image": details['image'],
                "amazon_seller": bool(re.fullmatch(r"Amazon(?:\.com|\.co\.uk|\.ca)?(?: Services(?:,? Inc\.?)?|\.com Services LLC)?", seller, re.I)),
                "seller": seller or "Unknown", "condition": "used" if details['used'] or "used" in details['condition'] else "new",
                "available": available, 'availability_status': stock_status,
                'availability_message': availability_message, 'agent_error': agent_error}

    async def wait_state(self, page, state, **evidence):
        """Return on observable progress; default browser timeout only bounds stalls."""
        with stage('Wait for ' + state + ' state'):
            try:
                handle = await page.wait_for_function(PAGE_STATE, arg={'state': state, **evidence})
            except BrowserTimeoutError as exc:
                raise Attention(f'Amazon did not confirm the {state} state before the browser timeout. Review the browser; no action was repeated.') from exc
            try:
                result = await handle.json_value()
            finally:
                await handle.dispose()
            if isinstance(result, dict) and result.get('error'):
                await self.check(page)
                if result.get('continuation'):
                    # check() permits one exact, same-origin Continue shopping
                    # action. Re-observe its resulting page, never assume success.
                    return await self.wait_state(page, state, **evidence)
                raise Attention(result['error'])
            return result

    @timed('Add target and reconcile cart')
    async def cart(self, page, quantity, asin):
        domain = urlparse(page.url).hostname
        if domain not in DOMAINS.values():
            raise Attention("Unexpected page. Review the browser before continuing.")
        if not re.fullmatch(r'[A-Z0-9]{10}', asin) or not 1 <= quantity <= 30:
            raise CartRejected('Invalid product or quantity')
        cart_url = f"https://{domain}/gp/cart/view.html"
        if getattr(page, '_retail_reconcile_cart', False) and not self.is_cart_page(page):
            # Authentication may have interrupted an already-dispatched add.
            # Inspect its result, never replay it or infer failure from redirect.
            await self.navigate(page, cart_url, wait_until='domcontentloaded')
        # The account's product/offer was already verified by the caller. Add
        # once, then reconcile the resulting cart instead of leaving and
        # reloading the product for a preflight inspection.
        if not self.is_cart_page(page):
            await self.select_product_quantity(page, quantity)
            before = await page.evaluate(PAGE_STATE, {'state': 'cart', 'snapshot': True, 'asin': asin, 'quantity': quantity})
            if before.get('error'):
                await self.check(page)
                raise Attention(before['error'])
            with stage('Add target acknowledgement'):
                try:
                    await behavior.click(page, await self.resolve_action(page, "ADD_TO_CART"))
                except InteractionError as exc:
                    raise Attention(str(exc)) from exc
                await self.wait_state(page, 'cart', asin=asin, quantity=quantity, before=before)
            # Amazon sometimes sends Add directly to the cart. Reuse it.
            if not self.is_cart_page(page):
                with stage('Open cart after add'):
                    await self.navigate(page, cart_url, wait_until="domcontentloaded")
        with stage('Reconcile target cart'):
            # Do not wait for the requested quantity yet: an existing target
            # may have been incremented. Wait for committed identifiable rows.
            await self.wait_state(page, 'cart_items', asin=asin)
            await self.check(page)
            lines = await self.get_cart(page)
            matching = [line for line in lines if line['asin'] == asin]
            if len(matching) != 1:
                raise Attention('The target could not be uniquely identified in the cart; review before restarting')
            if any(line['asin'] != asin for line in lines):
                await self.remove_unrelated_cart_items(page, asin, lines)
            await self.normalize_cart_quantity(page, asin, quantity, matching[0])
            lines = await self.get_cart(page)
            if lines != [{'asin': asin, 'quantity': quantity}]:
                raise Attention('Cart product or quantity could not be verified; inspect the cart before restarting')
        return quantity

    @staticmethod
    def is_cart_page(page):
        return bool(re.fullmatch(r'/gp/cart/(?:view(?:\.html)?|desktop/go-to-cart\.html)/?', urlparse(page.url).path))

    async def select_product_quantity(self, page, quantity):
        choice = await page.evaluate('''() => {
            const pickers = document.querySelectorAll('select#quantity');
            return {count:pickers.length, value:pickers[0]?.value,
                values:[...(pickers[0]?.options || [])].filter(e => !e.disabled).map(e => e.value)};
        }''')
        if not choice['count'] and quantity == 1:
            return
        if choice['count'] != 1 or str(quantity) not in choice['values']:
            raise CartRejected('Requested item quantity is unavailable or ambiguous; choose a supported quantity')
        if choice['value'] != str(quantity):
            await page.locator('select#quantity').select_option(str(quantity))

    async def normalize_cart_quantity(self, page, asin, quantity, match):
        if match['quantity'] == quantity:
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
                await self.wait_state(page, 'quantity', asin=asin, quantity=quantity)
                updated = await self.get_cart(page)
                if len(updated) == 1 and updated[0]['asin'] == asin and updated[0]['quantity'] == quantity:
                    return quantity
        if (await row.count() == 1 and match['quantity'] is not None
                and 1 <= quantity <= 30 and 1 <= match['quantity'] <= 30):
            direction = 'Increase' if quantity > match['quantity'] else 'Decrease'
            step = 1 if direction == 'Increase' else -1
            for expected in range(match['quantity'] + step, quantity + step, step):
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
        raise Attention("The target is already in the cart with a different quantity; adjust its quantity in Amazon before starting this task")

    @timed('Buy Now and checkout navigation')
    async def buy_now(self, page, quantity, asin):
        """Use Buy Now when offered; return False before mutation if unavailable."""
        if urlparse(page.url).hostname not in DOMAINS.values():
            raise Attention('Unexpected page. Review the browser before continuing.')
        offered = page.locator("#buy-now-button:visible, input[name='submit.buy-now']:visible").or_(
            page.get_by_role('button', name=re.compile(AMAZON_ACTIONS['BUY_NOW'], re.I)))
        if not await offered.count():
            return False
        await self.select_product_quantity(page, quantity)
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
        # One snapshot instead of multiple controller round trips per cart row.
        return await page.evaluate('''() => {
            const selector = document.querySelector('#sc-active-cart') ? '#sc-active-cart [data-asin]' :
                '[data-asin][data-quantity]:not(#sc-saved-cart *):not(#sc-saved-cart-items *)';
            return [...document.querySelectorAll(selector)].filter(e => e.getClientRects().length &&
                getComputedStyle(e).visibility !== 'hidden').map(e => {
                const picker = e.querySelectorAll("select[name='quantity']");
                const qty = e.getAttribute('data-quantity') ?? (picker.length === 1 ? picker[0].value : null);
                return {asin:e.getAttribute('data-asin'), quantity:qty && /^\\d+$/.test(qty) ? Number(qty) : null};
            });
        }''')

    @timed('Remove unrelated cart items')
    async def remove_unrelated_cart_items(self, page, target_asin, lines=None):
        """Delete only uniquely identified unrelated active rows, once per row."""
        lines = await self.get_cart(page) if lines is None else lines
        unrelated = [line['asin'] for line in lines if line['asin'] != target_asin]
        if any(not re.fullmatch(r'[A-Z0-9]{10}', asin or '') for asin in unrelated) or len(unrelated) != len(set(unrelated)):
            raise Attention('Cart item identities are ambiguous; no items were removed')
        for asin in unrelated:
            row = page.locator(f"#sc-active-cart [data-asin='{asin}']:visible")
            if await row.count() != 1:
                raise Attention('An unrelated cart item could not be uniquely identified')
            # Amazon keeps hidden responsive copies and sometimes labels the
            # clickable input through aria-labelledby. Count usable controls,
            # not hidden markup or the wrapper surrounding the same button.
            try:
                handle = await page.wait_for_function('''asin => {
                    const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
                    const rows=[...document.querySelectorAll('#sc-active-cart [data-asin]')].filter(e=>e.dataset.asin===asin && visible(e));
                    if(rows.length!==1) return {error:'row'};
                    const candidates=[...rows[0].querySelectorAll('input[type="submit"],input[type="button"],button,a,[role="button"]')].filter(e=>{
                        if(!visible(e) || e.matches(':disabled') || e.getAttribute('aria-disabled')==='true') return false;
                        const labelled=(e.getAttribute('aria-labelledby') || '').split(/\\s+/).map(id=>document.getElementById(id)?.textContent || '').join(' ');
                        const label=(e.getAttribute('aria-label') || labelled.trim() || e.value || e.textContent || '').trim();
                        return /^submit\\.delete(?:-active)?(?:\\.|$)/.test(e.getAttribute('name') || '')
                            || e.getAttribute('data-action')==='delete-active' || /^delete(?:\\s.*)?$/i.test(label);
                    });
                    const actions=candidates.filter(e=>!candidates.some(child=>child!==e && e.contains(child)));
                    return actions.length>1 ? {error:'ambiguous'} : actions[0] || false;
                }''', arg=asin, timeout=5000)
            except BrowserTimeoutError as exc:
                raise Attention('Delete did not become available for an unrelated cart item; review the cart') from exc
            try:
                action = handle.as_element()
                if action is None:
                    raise Attention('Delete is ambiguous for an unrelated cart item; review the cart')
                await behavior.click(page, action)
            finally:
                await handle.dispose()
            # If acknowledgement is lost, pause; never issue the mutation again.
            await self.wait_state(page, 'cart_removed', asin=asin)
        remaining = await self.get_cart(page)
        if any(line['asin'] != target_asin for line in remaining):
            raise Attention('Unrelated items remain in the active cart')

    async def prepare_checkout(self, page, asin, quantity):
        """Refuse automatic checkout of a mixed, unrecognized, or mismatched cart."""
        lines = await self.get_cart(page)
        if len([line for line in lines if line['asin'] == asin]) != 1:
            raise Attention('The target could not be uniquely identified before checkout; no items were removed')
        if any(line['asin'] != asin for line in lines):
            await self.remove_unrelated_cart_items(page, asin, lines)
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
        before = None
        for _ in range(5):
            observed = await self.wait_state(page, 'checkout', before=before)
            await self.check(page)
            if observed['kind'] == 'dialog':
                action = 'DISMISS_CHECKOUT_OFFER'
            elif observed['kind'] == 'review':
                return
            else:
                action = 'CONTINUE_CHECKOUT'
            try:
                control = await self.resolve_action(page, action)
                href = await control.evaluate("e => e.closest('a[href]')?.href || ''")
                if href and (urlparse(href).scheme != 'https' or urlparse(href).hostname not in set(DOMAINS.values())):
                    raise InteractionError('Checkout continuation leaves the permitted retailer')
                await behavior.click(page, control, timeout=5000)
                before = observed
            except InteractionError as exc:
                # AI is a bounded fallback after deterministic semantics. Keep
                # this message specific so the user knows whether the issue is
                # a missing continuation or a failed model/tool call.
                raise Attention(f'Checkout navigation needs review: {exc}. AI cannot bypass missing item or price evidence') from exc
        raise Attention('Checkout navigation did not reach a verifiable order review after five safe steps')

    async def checkout_snapshot(self, page, asin, quantity, max_total, *, max_unit_price=None, allow_third_party=False, allow_used=False, allow_recovery=True):
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
        if not values and self.agent:
            try:
                values = [await self.recovery.read_total(page, self.agent, allow_model=allow_recovery and
                    (self.store.get('settings', 'settings') or {}).get('agent_mode', 'off') != 'off')]
            except InteractionError as exc:
                raise Attention('Final order total changed and AI could not verify it: ' + str(exc)) from exc
        if not values or any(value is None or value != values[0] for value in values) or values[0] > max_total:
            raise Attention("Order total is missing, ambiguous, or above the configured budget")
        try:
            button = getattr(page, '_retail_submit_control', None)
            if button is None:
                if not allow_recovery:
                    raise Attention('Validated submission control is missing')
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
        snapshot = {"total": values[0], "quantity": quantity, "asin": asin, "currency": "USD",
                    "unit_price": unit_price, "price_components": components}
        page._retail_review['snapshot'] = snapshot
        return snapshot

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
        fresh = await self.checkout_snapshot(page, *review['args'], **review['limits'], allow_recovery=False)
        if any(fresh.get(key) != review['snapshot'].get(key) for key in ('asin','quantity','currency','total','unit_price')):
            raise Attention('Checkout facts changed after submission intent; reconcile before retrying')
        gate = getattr(page, '_retail_submit_gate', None)
        if gate:
            gate()
        button = page._retail_submit_control
        page._retail_review = None
        page._retail_submit_control = None
        await button.click(no_wait_after=True)
        await self.wait_state(page, 'submission')

    async def confirmation(self, page):
        if urlparse(page.url).hostname not in DOMAINS.values():
            return None
        try:
            result = await self.wait_state(page, 'confirmation')
        except Attention:
            # No order ID plus success evidence is an uncertain outcome, not a
            # failed order or permission to submit again.
            return None
        return result.get('order')

    async def free_shipping(self, page):
        options = page.locator("label").filter(has_text=re.compile(r"FREE.*(?:delivery|shipping)|(?:delivery|shipping).*FREE", re.I))
        for index in range(await options.count()):
            option = options.nth(index)
            radio = option.locator("input[type=radio]")
            if await radio.count() == 1 and await radio.is_visible():
                await radio.check()
                break
        await self.wait_state(page, 'shipping')
        rows = page.locator("#subtotals-marketplace-table tr").filter(has_text=re.compile(r"shipping|delivery", re.I))
        text = " ".join(await rows.all_inner_texts())
        charges = re.findall(r"\$\s*([\d,.]+)", text)
        if not text or not (all(float(x.replace(",", "")) == 0 for x in charges) if charges else "free" in text.lower()):
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
        await self.wait_state(page, 'cvv')
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
            self.identity_hardware.clear()
            self.logins.clear()
            self.fingerprint_tests.clear()
            self.context_accounts.clear()


class AccessDenied(Attention):
    pass
