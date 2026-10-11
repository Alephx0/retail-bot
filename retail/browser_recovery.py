"""Exception-only recovery. No scheduler, browser creation or purchase retries."""
import asyncio
from collections import OrderedDict, deque
from contextvars import ContextVar
from dataclasses import dataclass
import re
import time
from urllib.parse import urlsplit

from .browser_mcp import BrowserTools, AMAZON_ACTIONS
from .interactions import InteractionError
from .models import DOMAINS
from .performance import stage
from .recovery_evidence import VERSION, PROTECTED_DIALOG, incident_key, page_key
from .store import now

# A parent execution deadline is inherited by all child coroutines.
execution_deadline = ContextVar('recovery_execution_deadline', default=None)
DOMAINS_ALLOWED = set(DOMAINS.values())
OPTIONAL_TITLE = re.compile(r'^(?:sign up (?:for|to) (?:our |the )?newsletter|newsletter|special offer|promotional offer|what.s new|new features|stay in the loop)$', re.I)
CLOSE_LABEL = re.compile(r'^(?:close|dismiss|no thanks|no, thanks|not now|maybe later)$', re.I)



@dataclass(frozen=True)
class Budgets:
    total: float = 15.0
    structural: float = 3.0
    queue: float = .15
    minimum_ai: float = 3.0
    workers: int = 2
    circuit_failures: int = 3
    circuit_seconds: float = 30.0


class RecoveryController:
    def __init__(self, store, budgets=None):
        self.store = store
        self.budgets = budgets or Budgets()
        self.slots = asyncio.Semaphore(self.budgets.workers)
        self.active = {}
        self.recent = deque(maxlen=100)
        self.circuits = OrderedDict()
        self.counts = {'recovered': 0, 'failed': 0, 'cancelled': 0, 'busy': 0, 'ai': 0}

    def snapshot(self):
        return {'active': list(self.active.values()), 'recent': list(self.recent),
                'counts': dict(self.counts), 'workers': self.budgets.workers}

    def deadline(self, page):
        settings = (self.store.get('settings', 'settings') or {}) if self.store else {}
        duration = min(self.budgets.total, settings.get('browser_timeout_ms', 30000) / 1000,
                       settings.get('agent_timeout_seconds', 60))
        return min(time.monotonic() + duration, execution_deadline.get() or float('inf'),
                   getattr(page, '_retail_recovery_deadline', float('inf')))

    def _record(self, page, action, category, outcome, started, tier):
        host, route = page_key(page.url)
        row = {'task_id': getattr(page.context, '_retail_task_id', ''), 'action': action,
               'category': category, 'status': outcome, 'tier': tier,
               'duration_ms': round((time.monotonic()-started)*1000, 2), 'at': now()}
        self.recent.append(row)
        self.counts[outcome] = self.counts.get(outcome, 0) + 1
        if self.store and host in DOMAINS_ALLOWED:
            key = incident_key(host, route, action, category)
            old = self.store.get('recovery_incidents', key) or {}
            self.store.put_bounded('recovery_incidents', {
                'schema_version': VERSION, 'host': host, 'route': route, 'action': action,
                'category': category, 'count': old.get('count', 0)+1,
                'recovered': old.get('recovered', 0)+(outcome == 'recovered'),
                'status': 'observed', 'last_outcome': outcome, 'at': row['at'],
                'handler_version': VERSION, 'tier': tier}, key)

    def _scope(self, page, context, owner, url):
        if (page.is_closed() or page.context is not context or page.url != url
                or getattr(context, '_retail_task_id', '') != owner
                or urlsplit(url).hostname not in DOMAINS_ALLOWED):
            raise InteractionError('Browser ownership or location changed; review the task browser')

    async def install(self, page):
        """One browser-side trigger. Python runs only when an optional dialog appears."""
        owner = getattr(page.context, '_retail_task_id', '')
        installed = getattr(page, '_retail_overlay_handler', None)
        if installed and installed[0] == owner:
            return
        if installed:
            await page.remove_locator_handler(installed[1])
        self.watch_navigation(page)
        trigger = page.locator('[role=dialog],dialog[open],[aria-modal=true]').filter(
            has=page.get_by_role('heading', name=OPTIONAL_TITLE)).first
        page._retail_overlay_handler = (owner, trigger)

        async def handle():
            # A handler exception must not accidentally release the pending action.
            # Leave unsafe dialogs visible; the action's own timeout stops it.
            try:
                if getattr(page.context, '_retail_task_id', '') == owner:
                    await self.dismiss(page, from_handler=True)
            except (InteractionError, TimeoutError):
                pass
        await page.add_locator_handler(trigger, handle, times=3)

    @staticmethod
    def watch_navigation(page):
        if getattr(page, '_retail_recovery_navigation', False):
            return
        page._retail_recovery_navigation = True

        def navigated(frame):
            if frame is page.main_frame:
                # Cart preflight legitimately navigates away and returns to the
                # same product URL. Element handles belong to a document, not URL.
                page._retail_recovered_controls = {}
                page._retail_agent_attempts = set()
        page.on('framenavigated', navigated)

    async def dismiss(self, page, *, from_handler=False):
        started = time.monotonic()
        context, owner, url = page.context, getattr(page.context, '_retail_task_id', ''), page.url
        self._scope(page, context, owner, url)
        task = asyncio.current_task()
        previous = self.active.get(id(task))
        self.active[id(task)] = {'task_id': owner, 'status': 'Recovering', 'action': 'DISMISS_OPTIONAL'}
        outcome = 'failed'
        matched = False
        removed_handler = False
        deadline = min(self.deadline(page), started+self.budgets.structural)
        try:
            async with asyncio.timeout_at(deadline):
                frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame
                          and urlsplit(f.url).hostname == urlsplit(url).hostname][:4]
                for frame in frames:
                    dialogs = frame.locator('[role=dialog],dialog[open],[aria-modal=true]')
                    for dialog in (await dialogs.all())[:8]:
                        if not await dialog.is_visible():
                            continue
                        titles = await dialog.get_by_role('heading').all_text_contents()
                        if not any(OPTIONAL_TITLE.fullmatch(t.strip()) for t in titles):
                            continue
                        matched = True
                        # Local classification only: prose never leaves the browser process.
                        if PROTECTED_DIALOG.search(await dialog.inner_text()) or await dialog.locator('input[type=password],input[autocomplete*=cc-]').count():
                            raise InteractionError('This dialog needs a user decision')
                        buttons = dialog.get_by_role('button', name=CLOSE_LABEL)
                        available = [b for b in await buttons.all() if await b.is_visible() and await b.is_enabled()]
                        if len(available) != 1:
                            raise InteractionError('Dialog dismissal is ambiguous')
                        self._scope(page, context, owner, url)
                        installed = getattr(page, '_retail_overlay_handler', None)
                        if installed and not from_handler:
                            # A direct dismissal must not trigger the same native
                            # handler recursively before its own close click.
                            await page.remove_locator_handler(installed[1])
                            page._retail_overlay_handler = None
                            removed_handler = True
                        await available[0].click(timeout=max(1, (self.deadline(page)-time.monotonic())*1000))
                        await dialog.wait_for(state='hidden', timeout=self.budgets.structural*1000)
                        self._scope(page, context, owner, url)
                        outcome = 'recovered'
                        return True
                return False
        except asyncio.CancelledError:
            outcome = 'cancelled'
            raise
        finally:
            self.active.pop(id(task), None)
            if previous:
                self.active[id(task)] = previous
            if matched:
                self._record(page, 'DISMISS_OPTIONAL', 'optional_overlay', outcome, started, 'handler')
            if removed_handler and not page.is_closed():
                async with asyncio.timeout_at(deadline):
                    await self.install(page)

    async def resolve(self, page, action, agent, *, allow_ai=False):
        started = time.monotonic()
        context, owner, url = page.context, getattr(page.context, '_retail_task_id', ''), page.url
        self._scope(page, context, owner, url)
        if hasattr(page, 'on'):
            self.watch_navigation(page)
        deadline = self.deadline(page)
        key = (url, action)
        cache = getattr(page, '_retail_recovered_controls', {})
        if key in cache:
            browser, ref = cache[key]
            async with asyncio.timeout_at(deadline):
                if (await browser.validate_control(ref)).get('validated'):
                    return browser.chosen
            del cache[key]
        attempted = getattr(page, '_retail_agent_attempts', set())
        if key in attempted or len(attempted) >= 32:
            raise InteractionError('Recovery already attempted this page action; review the task browser')
        attempted.add(key)
        page._retail_agent_attempts = attempted
        circuit_key = (*page_key(url), action)
        failures, opened = self.circuits.get(circuit_key, (0, 0))
        task = asyncio.current_task()
        acquired = False
        outcome, tier = 'failed', 'structural'
        self.active[id(task)] = {'task_id': owner, 'status': 'Recovering', 'action': action}
        try:
            async with asyncio.timeout_at(deadline):
                try:
                    async with asyncio.timeout(self.budgets.queue):
                        await self.slots.acquire()
                        acquired = True
                except TimeoutError:
                    outcome = 'busy'
                    raise InteractionError('Recovery is busy; review or resume this task') from None
                with stage('Structural recovery'):
                    async with asyncio.timeout_at(min(deadline, started+self.budgets.structural)):
                        blocked = await page.evaluate("""() => !!document.querySelector('#captchacharacters,#ap_password,#auth-mfa-otpcode,iframe[src*=captcha]') || /robot check|access denied|verify your identity/i.test(document.body?.innerText || '')""")
                        if blocked:
                            raise InteractionError('Account or access verification requires attention')
                        await self.dismiss(page)
                        if agent is not None and hasattr(agent, 'reuse'):
                            repaired = await agent.reuse(page, action, DOMAINS_ALLOWED, AMAZON_ACTIONS)
                            if repaired is not None:
                                outcome, tier = 'recovered', 'reviewed_handler'
                                return repaired
                        loading = await page.locator('[aria-busy=true]:visible').count()
                        browser = BrowserTools(page, action, DOMAINS_ALLOWED, AMAZON_ACTIONS)
                        observed = await browser.observe_controls()
                        if not observed['controls'] and loading:
                            # A declared loading state justifies waiting for the
                            # expected control, not an immediate provider call.
                            pattern = re.compile(AMAZON_ACTIONS[action], re.I)
                            pending = page.get_by_role('button', name=pattern).or_(page.get_by_role('link', name=pattern))
                            await pending.first.wait_for(state='visible', timeout=max(1, (min(deadline, started+self.budgets.structural)-time.monotonic())*1000))
                        if not observed['controls']:
                            # The component can finish between the loading check
                            # and observation. Re-read once before escalation.
                            observed = await browser.observe_controls()
                        if len(observed['controls']) == 1:
                            ref = observed['controls'][0]['ref']
                            if (await browser.validate_control(ref)).get('validated'):
                                cache[key] = (browser, ref)
                                page._retail_recovered_controls = cache
                                outcome = 'recovered'
                                return browser.chosen
                        elif len(observed['controls']) > 1:
                            raise InteractionError('Multiple eligible controls; review the task browser')
                failures, opened = self.circuits.get(circuit_key, (0, 0))
                if failures >= self.budgets.circuit_failures and started-opened < self.budgets.circuit_seconds:
                    raise InteractionError('Repeated recovery failures on this workflow; automatic AI paused')
                if not allow_ai or agent is None or deadline-time.monotonic() < self.budgets.minimum_ai:
                    raise InteractionError('Expected control could not be verified within the recovery budget')
                tier = 'ai'
                self.counts['ai'] += 1
                with stage('AI recovery'):
                    result = await agent.resolve(page, action, DOMAINS_ALLOWED, AMAZON_ACTIONS)
                self._scope(page, context, owner, url)
                outcome = 'recovered'
                return result
        except asyncio.CancelledError:
            outcome = 'cancelled'
            raise
        except TimeoutError:
            raise InteractionError('Recovery timed out; no purchase action was repeated') from None
        except InteractionError:
            raise
        except Exception:
            raise InteractionError('Browser recovery stopped; review the task browser') from None
        finally:
            if acquired:
                self.slots.release()
            self.active.pop(id(task), None)
            if outcome == 'recovered':
                self.circuits.pop(circuit_key, None)
            elif outcome == 'failed':
                failures, opened = self.circuits.get(circuit_key, (0, 0))
                self.circuits[circuit_key] = (failures+1 if started-opened < self.budgets.circuit_seconds else 1, started)
                if len(self.circuits) > 128:
                    self.circuits.popitem(last=False)
            self._record(page, action, 'changed_control', outcome, started, tier)

    async def read_total(self, page, agent, *, allow_model):
        """Price inspection shares the recovery deadline and worker bound."""
        async with asyncio.timeout_at(self.deadline(page)):
            acquired = False
            try:
                async with asyncio.timeout(self.budgets.queue):
                    await self.slots.acquire()
                    acquired = True
                # Known total labels are deterministic. Unknown labels require
                # source review rather than an AI price assertion at submission.
                return await agent.resolve_total(page, DOMAINS_ALLOWED, allow_model=False)
            except TimeoutError:
                raise InteractionError('Price inspection exceeded its recovery budget') from None
            finally:
                if acquired:
                    self.slots.release()
