"""Bounded, explicitly started browsing routines; no purchase actions or scheduler."""
import asyncio
import copy
import random
import re
import time
from typing import Literal
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from .amazon import Attention, BackoffRequired
from .models import DOMAINS
from .store import now
from . import behavior
from .performance import stage
from .care_agent import CarePlanningError
from .care_reading import read_with_pointer


class BrowsingRoutine(BaseModel):
    mode: Literal['products', 'ai'] = 'products'
    interests: str = Field(default='', max_length=300)
    ai_connection_id: str = Field(default='', max_length=100)
    products: list[str] = Field(default_factory=list, max_length=20)
    visit_limit: int = Field(default=3, ge=1, le=10)
    seconds_per_page: int = Field(default=30, ge=15, le=120)
    min_seconds_per_page: int | None = Field(default=None, ge=15, le=119)
    max_seconds_per_page: int | None = Field(default=None, ge=16, le=120)

    @model_validator(mode='after')
    def reading_range(self):
        # Existing encrypted records remain intact; old durations become a range.
        if self.min_seconds_per_page is None and self.max_seconds_per_page is None:
            self.min_seconds_per_page = max(15, round(self.seconds_per_page * .75))
            self.max_seconds_per_page = min(120, round(self.seconds_per_page * 1.25))
        if self.min_seconds_per_page is None or self.max_seconds_per_page is None:
            raise ValueError('Set both minimum and maximum reading times')
        if self.min_seconds_per_page >= self.max_seconds_per_page:
            raise ValueError('Maximum reading time must be greater than minimum reading time')
        return self

    @field_validator('products')
    @classmethod
    def product_ids(cls, values):
        result = []
        for value in values:
            value = value.strip().upper()
            if not re.fullmatch(r'[A-Z0-9]{10}', value):
                raise ValueError('Use one 10-character Amazon ASIN per line')
            if value not in result:
                result.append(value)
        return result


ACTIVE = {'queued', 'opening', 'loading', 'verifying', 'thinking', 'acting', 'saving', 'browsing', 'stopping'}


def reading_scroll_times(seconds, rng):
    """Reading offsets, not readiness waits. Leave time before/after each burst."""
    count = min(12, max(2, round(seconds/rng.uniform(4.5, 8.5))))
    weights = [rng.uniform(.5, 1.7) for _ in range(count+1)]
    total, elapsed = sum(weights), 0
    for weight in weights[:-1]:
        elapsed += weight
        yield seconds * elapsed / total


class AccountCare:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.runs = {}
        self.stops = {}
        for record in self.store.all('care_runs'):
            if record.get('status') in ACTIVE or record.get('status') == 'cleanup_required':
                self.update(record['account_id'], status='interrupted', message='Application restarted. Review the account before starting again.', ended_at=now())

    def routine(self, account_id):
        return BrowsingRoutine.model_validate(self.store.get('care_routines', 'care-routine-'+account_id) or {}).model_dump()

    def update(self, account_id, **values):
        old = self.store.get('care_runs', 'care-run-'+account_id) or {}
        return self.store.put('care_runs', {**old, **values, 'account_id': account_id, 'updated_at': now()}, 'care-run-'+account_id)

    def snapshot(self):
        records = []
        routines = {r['account_id']: r for r in self.store.all('care_routines')}
        runs = {r['account_id']: r for r in self.store.all('care_runs')}
        task_accounts = {t.get('account_id') for t in self.store.all('tasks') if t['id'] in self.engine.jobs}
        for account in self.store.all('accounts'):
            account_id = account['id']
            coordinator = getattr(self.engine, 'group_coordinator', None)
            busy = bool(coordinator and coordinator.busy_account(account_id)) or account_id in task_accounts
            run = runs.get(account_id)
            elapsed = max(0, int((datetime.now(timezone.utc)-datetime.fromisoformat(run.get('stage_started_at') or run['updated_at'])).total_seconds())) if run and account_id in self.runs else None
            records.append({'account_id': account_id, 'routine': BrowsingRoutine.model_validate(routines.get(account_id, {})).model_dump(),
                            'configured': account_id in routines,
                            'run': run, 'active': account_id in self.runs, 'stage_elapsed_seconds': elapsed,
                            'busy': busy, 'session_open': account_id in self.engine.amazon.logins})
        return records

    def save(self, account_id, routine):
        if account_id in self.runs:
            raise ValueError('Stop browsing before editing this routine')
        self.store.put('care_routines', {**routine.model_dump(), 'account_id': account_id}, 'care-routine-'+account_id)
        return self.routine(account_id)

    async def start(self, account_id):
        engine = self.engine
        if engine.closing or engine.stopping_all:
            raise ValueError('The engine is stopping; try again after it finishes')
        if account_id in self.runs:
            raise ValueError('Browsing is already active for this account')
        account = self.store.get('accounts', account_id)
        if not account or account.get('retailer', 'amazon') != 'amazon':
            raise ValueError('Browsing currently supports Amazon accounts only')
        if not account.get('session'):
            raise ValueError('Open and save a signed-in session first')
        if (self.store.get('settings', 'settings') or {}).get('cdp_attach'):
            raise ValueError('Account browsing requires an app-managed visible window. Disable external CDP attachment in Settings; AI browsing creates its own scoped CDP session.')
        engine.check_pending_order(account_id)
        record = self.store.get('care_runs', 'care-run-'+account_id) or {}
        if record.get('retry_at') and datetime.fromisoformat(record['retry_at']) > datetime.now(timezone.utc):
            raise ValueError('Retailer cooldown is active until ' + record['retry_at'])
        coordinator = getattr(engine, 'group_coordinator', None)
        if coordinator and coordinator.repo.claimed(account_id):
            raise ValueError('This account is reserved by a task group')
        if any(t.get('account_id') == account_id and t['id'] in engine.jobs for t in self.store.all('tasks')):
            raise ValueError("Stop this account's tasks before browsing")
        if account_id in engine.amazon.logins or account_id in engine.amazon.fingerprint_tests:
            raise ValueError('Finish or close the account browser first')
        routine = copy.deepcopy(self.routine(account_id))
        connection = None
        if routine['mode'] == 'ai':
            settings = self.store.get('settings', 'settings') or {}
            connection = self.store.get('ai_connections', routine['ai_connection_id'] or settings.get('ai_connection_id', ''))
            if not connection:
                raise ValueError('Choose an AI connection in the routine or in Settings > Integrations')
            if not routine['interests'].strip():
                raise ValueError('Add browsing interests so the AI knows what to explore')
        lock = engine.account_locks.setdefault(account_id, asyncio.Lock())
        if lock.locked():
            raise ValueError('This account is busy')
        # No yield between testing an unlocked asyncio.Lock and acquiring it.
        await lock.acquire()
        entry = {'lock': lock, 'context': None, 'slot': False, 'page': None, 'stop_requested': False, 'connection': copy.deepcopy(connection)}
        entry['input_calibration'] = behavior.calibration_for(self.store.get('settings', 'settings') or {}, browsing=True)
        self.runs[account_id] = entry
        total = routine['visit_limit'] if routine['mode'] == 'ai' else min(routine['visit_limit'], len(routine['products']) or 1)
        self.update(account_id, status='queued', message='Waiting for a shared browser slot',
                    started_at=now(), ended_at=None, completed=0, total=total, current='',
                    routine=routine, history=[], interaction_count=0, last_interaction=None, reading_seconds=None, retry_at=None, stage_started_at=now(), timings={})
        job = asyncio.create_task(self.scoped_execute(account_id, copy.deepcopy(account), routine, entry))
        entry['job'] = job
        job.add_done_callback(lambda task: self.finished(account_id, entry, task))
        return {'ok': True}

    def phase(self, account_id, status, message):
        entry = self.runs.get(account_id)
        if entry and entry['stop_requested']:
            raise asyncio.CancelledError()
        self.update(account_id, status=status, message=message, stage_started_at=now())

    async def scoped_execute(self, account_id, account, routine, entry):
        with self.engine.performance.scope('account-care', account_id):
            await self.execute(account_id, account, routine, entry)

    def finished(self, account_id, entry, task):
        # Also handles cancellation before the coroutine's first instruction.
        if entry['context'] is not None:
            return  # Keep ownership until cleanup can be retried explicitly.
        if entry['slot']:
            self.engine.browser_slots.release()
            entry['slot'] = False
        if entry['lock'].locked():
            entry['lock'].release()
        if self.runs.get(account_id) is entry:
            self.runs.pop(account_id)
        record = self.store.get('care_runs', 'care-run-'+account_id) or {}
        if record.get('status') in ACTIVE:
            self.update(account_id, status='stopped', message='Browsing stopped', ended_at=now())

    async def inspect(self, page, domain):
        parsed = urlparse(page.url)
        if parsed.scheme != 'https' or parsed.netloc != domain:
            raise Attention('Browsing left the expected Amazon site. Review the saved session.')
        observed = await page.evaluate('''() => {
            const visible = s => [...document.querySelectorAll(s)].some(e => e.getClientRects().length);
            return {body:(document.body?.innerText || '').slice(0,30000).toLowerCase(),
                blocked:visible('#captchacharacters,#auth-mfa-otpcode,#cvf-input-code,#ap_password,#ap_email,#ap_email_login')};
        }''')
        if (observed['blocked'] or '/ap/' in parsed.path or 'validatecaptcha' in parsed.path.lower()
                or any(word in observed['body'] for word in ('access denied', 'robot check', 'verify your identity',
                    "verify it's you", 'additional verification required', 'click the button below to continue shopping',
                    'enter the characters you see', "make sure you're not a robot"))):
            raise Attention('Sign-in or verification needs attention. Open the account session to continue.')
        label = await self.engine.amazon.wait_session_header(page)
        if not label or 'sign in' in label.lower():
            raise Attention('The saved session is signed out. Open the account session to sign in.')

    async def save_session(self, account_id, context, page):
        storage = await context.storage_state(indexed_db=True)
        session_storage = await self.engine.amazon.capture_session_storage(page)
        current = self.store.get('accounts', account_id)
        if current:
            self.store.put('accounts', {**current, 'session': storage, 'session_storage': session_storage,
                                       'logged_in': True, 'session_saved_at': now()})
            self.store.put('sessions', {'account_id': account_id, 'retailer': 'amazon', 'status': 'ready',
                                       'checked_at': now(), 'network': current.get('proxy_list_id', '')}, 'session-'+account_id)

    async def dwell(self, page, seconds, domain):
        # Intentional reading time; pacing never acts as a readiness condition.
        started = time.monotonic()
        deadline = started + seconds
        paced = behavior.controller(page)

        async def read_until(target):
            await read_with_pointer(page, max(0, target-time.monotonic()),
                                    lambda: self.inspect(page, domain))
            await self.inspect(page, domain)

        await self.inspect(page, domain)
        for offset in reading_scroll_times(seconds, paced.rng):
            await read_until(started+offset)
            if time.monotonic() >= deadline:
                break
            bounds = await page.evaluate('''() => ({top:scrollY,viewport:innerHeight,
                height:document.scrollingElement?.scrollHeight || innerHeight})''')
            remaining = max(0, bounds['height']-bounds['viewport']-bounds['top'])
            distance = max(100, min(650, bounds['viewport']*paced.rng.triangular(.18, .7, .38)))
            backwards = bounds['top']>150 and (remaining<40 or paced.rng.random()<.18)
            delta = -min(distance*.65, bounds['top']) if backwards else min(distance, remaining)
            if abs(delta)>=20:
                await paced.scroll(delta)
        await read_until(deadline)
        await self.inspect(page, domain)

    async def visit_complete(self, account_id, page, label, routine, domain):
        seconds = round(random.uniform(routine['min_seconds_per_page'], routine['max_seconds_per_page']), 2)
        self.phase(account_id, 'browsing', f'Reading {label} for about {seconds:.0f}s')
        self.update(account_id, current=label, reading_seconds=seconds)
        with stage('Reading and paced scrolling'):
            await self.dwell(page, seconds, domain)
        record = self.store.get('care_runs', 'care-run-'+account_id)
        self.update(account_id, completed=record['completed']+1,
                    history=[*record['history'], {'page': label, 'at': now(), 'reading_seconds': seconds}][-10:])

    async def execute(self, account_id, account, routine, entry):
        adapter = self.engine.amazon
        try:
            async with asyncio.timeout(1800):
                coordinator = getattr(self.engine, 'group_coordinator', None)
                if coordinator:
                    await coordinator.pool.discard(account_id)
                    if self.engine.browser_slots.locked():
                        await coordinator.pool.trim(all_idle=True)
                if self.engine.browser_slots.locked():
                    await self.engine.evict_ready()
                await self.engine.browser_slots.acquire()
                entry['slot'] = True
                self.phase(account_id, 'opening', 'Opening a visible account browser')
                launched = time.monotonic()
                async with asyncio.timeout(60):
                    entry['context'] = await adapter.context(account, interactive=True, headless=False,
                        progress=lambda message: self.phase(account_id, 'opening', message))
                    entry['context']._retail_paced_input = True
                    entry['context']._retail_browsing_input = True
                    entry['context']._retail_input_calibration = entry['input_calibration']
                    page = entry['page'] = await entry['context'].new_page()
                    page.set_default_timeout(10000)
                    await adapter.expose(page)
                self.update(account_id, timings={'browser_open_ms': round((time.monotonic()-launched)*1000)})
                domain = DOMAINS[account.get('region', 'US')]
                if routine['mode'] == 'ai':
                    from .care_agent import CareAgent
                    await CareAgent(self, account_id, page, domain, routine, entry['connection']).run()
                else:
                    products = routine['products'][:routine['visit_limit']] or ['']
                    for asin in products:
                        self.phase(account_id, 'loading', 'Loading '+(asin or 'Amazon home'))
                        async with asyncio.timeout(25):
                            await adapter.navigate(page, f'https://{domain}/dp/{asin}' if asin else f'https://{domain}/', wait_until='domcontentloaded', timeout=20000)
                            self.phase(account_id, 'verifying', 'Checking the live account session')
                            await self.inspect(page, domain)
                        await self.visit_complete(account_id, page, asin or 'Amazon home', routine, domain)
                self.phase(account_id, 'saving', 'Saving the browsing session')
                async with asyncio.timeout(10):
                    await self.inspect(page, domain)
                    await self.save_session(account_id, entry['context'], page)
                self.update(account_id, status='completed', message='Browsing finished; session saved', ended_at=now())
        except asyncio.CancelledError:
            self.update(account_id, status='stopping', message='Closing the browsing window', ended_at=None)
            raise
        except BackoffRequired as exc:
            retry_at = (datetime.now(timezone.utc) + timedelta(seconds=max(60, exc.retry_after_seconds or 60))).isoformat()
            self.update(account_id, status='cooldown', message='Retailer requested a break. No automatic retry.', retry_at=retry_at, ended_at=now())
        except CarePlanningError as exc:
            self.update(account_id, status='attention', message=str(exc), ended_at=now())
        except Attention as exc:
            self.update(account_id, status='attention', message=str(exc), ended_at=now())
            self.store.put('sessions', {'account_id': account_id, 'status': 'verification_required', 'checked_at': now()}, 'session-'+account_id)
        except TimeoutError:
            self.update(account_id, status='attention', message='This browsing step timed out. Check the browser or connection and try again.', ended_at=now())
        except Exception:
            self.update(account_id, status='attention', message='Browser or connection interrupted. Review the session before trying again.', ended_at=now())
        finally:
            cleanup = asyncio.create_task(self.cleanup(account_id, entry))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Stop arriving during natural completion must not interrupt close.
                await asyncio.shield(cleanup)
                raise

    async def cleanup(self, account_id, entry):
        if entry['context'] is not None:
            try:
                async with asyncio.timeout(20):
                    await entry['context'].close()
                    owner = getattr(entry['context'], '_retail_close_task', None)
                    if owner:
                        await asyncio.shield(owner)
                entry['context'] = None
            except Exception:
                self.update(account_id, status='cleanup_required', message='Browser could not close. Use Stop browsing to retry cleanup.', ended_at=now())

    def request_stop(self, account_id):
        entry = self.runs.get(account_id)
        if not entry:
            return None
        entry['stop_requested'] = True
        self.update(account_id, status='stopping', message='Stop requested; closing the browsing window', stage_started_at=now())
        if account_id not in self.stops:
            operation = self.stops[account_id] = asyncio.create_task(self._stop(account_id))
            def forget(task):
                if self.stops.get(account_id) is task:
                    self.stops.pop(account_id, None)
                if not task.cancelled():
                    task.exception()
            operation.add_done_callback(forget)
        return self.stops[account_id]

    async def stop(self, account_id):
        operation = self.request_stop(account_id)
        if operation is None:
            return
        try:
            await asyncio.shield(operation)
        finally:
            if operation.done() and self.stops.get(account_id) is operation:
                self.stops.pop(account_id)

    async def _stop(self, account_id):
        entry = self.runs.get(account_id)
        if not entry:
            return
        job = entry['job']
        if not job.done():
            self.update(account_id, status='stopping', message='Closing the browsing session')
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        elif entry['context'] is not None:
            await self.cleanup(account_id, entry)
            if entry['context'] is None:
                self.update(account_id, status='stopped', message='Browsing stopped', ended_at=now())
            self.finished(account_id, entry, job)

    async def stop_all(self):
        ids = list(self.runs)
        await asyncio.gather(*(self.stop(account_id) for account_id in ids))
        return len(ids)
