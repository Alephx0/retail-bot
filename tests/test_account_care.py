import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from retail.account_care import AccountCare, BrowsingRoutine
from retail.amazon import BackoffRequired
from retail.app import create_app
from retail.engine import Engine
from retail.store import Store


class Page:
    url = 'about:blank'
    viewport_size = {'width': 1000, 'height': 800}
    def __init__(self, context):
        self.context = context
        self.mouse = SimpleNamespace(move=AsyncMock(), wheel=AsyncMock())
    def set_default_timeout(self, value):
        self.timeout = value


    async def evaluate(self, script, *args):
        return {'body': 'Fixture product', 'blocked': False}


class Context:
    def __init__(self, account):
        self.account = account
        self.closed = False
        self.fail_close = False

    async def new_page(self):
        return Page(self)

    async def storage_state(self, **kwargs):
        return {'cookies': [{'name': self.account['id']}], 'origins': []}

    async def close(self):
        if self.fail_close:
            raise RuntimeError('Fixture close failure')
        self.closed = True


class Adapter:
    def __init__(self):
        self.logins, self.fingerprint_tests = {}, {}
        self.recovery = SimpleNamespace(snapshot=lambda: [])
        self.contexts, self.visits = [], []
        self.label = 'Hello, Fixture'
        self.error = None

    async def context(self, account, **kwargs):
        assert kwargs['interactive'] and kwargs['headless'] is False
        kwargs['progress']('Launching fixture browser')
        context = Context(account)
        self.contexts.append(context)
        return context

    async def expose(self, page):
        pass

    async def navigate(self, page, url, **kwargs):
        self.visits.append(url)
        if self.error:
            raise self.error
        page.url = url

    async def wait_session_header(self, page):
        return self.label

    async def capture_session_storage(self, page):
        return {}

    async def close(self):
        pass


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.005)


def fixture(tmp_path):
    store = Store(tmp_path)
    store.put('settings', {'max_running_tasks': 1}, 'settings')
    accounts = [store.put('accounts', {'name': name, 'retailer': 'amazon', 'region': 'US',
                'email': name+'@example.test', 'password': 'secret', 'session': {'cookies': [], 'origins': []},
                'notes': 'Keep notes', 'proxy': name+'.example:80'}) for name in ('Alpha', 'Beta')]
    engine = Engine(store)
    engine.amazon = Adapter()
    return store, engine, accounts


def test_routine_validation_and_independent_encrypted_records(tmp_path):
    store, engine, accounts = fixture(tmp_path)
    a, b = accounts
    routine = BrowsingRoutine(products=['b012345678', 'B012345678'], visit_limit=2)
    engine.account_care.save(a['id'], routine)
    assert engine.account_care.routine(a['id'])['products'] == ['B012345678']
    assert engine.account_care.routine(b['id'])['products'] == []
    assert store.get('accounts', a['id']) == a and store.get('accounts', b['id']) == b
    for products in [['https://evil.test/cart'], ['B123'], ['A'*11]]:
        with pytest.raises(ValueError):
            BrowsingRoutine(products=products)
    with pytest.raises(ValueError):
        BrowsingRoutine(seconds_per_page=0)
    store.db.close()


def test_reading_range_legacy_records_and_validation(tmp_path):
    store, engine, (account, _) = fixture(tmp_path)
    for old_seconds, expected in [(15, (15, 19)), (30, (22, 38)), (120, (90, 120))]:
        original = store.put('care_routines', {'account_id': account['id'], 'seconds_per_page': old_seconds}, 'care-routine-'+account['id'])
        routine = engine.account_care.routine(account['id'])
        assert (routine['min_seconds_per_page'], routine['max_seconds_per_page']) == expected
        assert store.get('care_routines', original['id']) == original
        assert BrowsingRoutine.model_validate(routine).model_dump() == routine
    for bounds in [(30, 30), (40, 20), (14, 40), (20, 121), (20, None)]:
        with pytest.raises(ValueError):
            BrowsingRoutine(min_seconds_per_page=bounds[0], max_seconds_per_page=bounds[1])
    store.db.close()


def test_reading_varies_per_visit_and_uses_frozen_range(tmp_path, monkeypatch):
    async def scenario():
        store, engine, (a, b) = fixture(tmp_path)
        care = engine.account_care
        care.save(a['id'], BrowsingRoutine(products=['B012345678', 'B087654321'], min_seconds_per_page=20, max_seconds_per_page=40))
        draws = iter([21.25, 37.5])
        def sample(low, high):
            assert (low, high) == (20, 40)
            return next(draws)
        monkeypatch.setattr('retail.account_care.random.uniform', sample)
        times = []
        async def dwell(page, seconds, domain):
            times.append(seconds)
            store.put('care_routines', {**BrowsingRoutine(min_seconds_per_page=70, max_seconds_per_page=90).model_dump(), 'account_id': a['id']}, 'care-routine-'+a['id'])
        care.dwell = dwell
        await care.start(a['id'])
        await until(lambda: not care.runs)
        record = store.get('care_runs', 'care-run-'+a['id'])
        assert record['status'] == 'completed'
        assert times == [21.25, 37.5]
        assert [h['reading_seconds'] for h in record['history']] == times
        assert record['routine']['min_seconds_per_page'] == 20
        assert care.routine(b['id'])['min_seconds_per_page'] == 22
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_bounded_run_freezes_plan_saves_only_own_session(tmp_path):
    async def scenario():
        store, engine, (a, b) = fixture(tmp_path)
        care = engine.account_care
        care.save(a['id'], BrowsingRoutine(products=['B012345678', 'B087654321'], visit_limit=1))
        gate = asyncio.Event()
        async def dwell(*args):
            await gate.wait()
        care.dwell = dwell
        await care.start(a['id'])
        await until(lambda: engine.amazon.visits)
        with pytest.raises(ValueError, match='Stop browsing'):
            care.save(a['id'], BrowsingRoutine())
        with pytest.raises(ValueError, match='already active'):
            await care.start(a['id'])
        # Even an external config write cannot change this run's captured plan.
        store.put('care_routines', {**BrowsingRoutine(products=['B000000000']).model_dump(), 'account_id': a['id']}, 'care-routine-'+a['id'])
        gate.set()
        await until(lambda: not care.runs)
        assert engine.amazon.visits == ['https://www.amazon.com/dp/B012345678']
        assert next(c for c in care.snapshot() if c['account_id'] == a['id'])['run']['status'] == 'completed'
        assert store.get('accounts', a['id'])['notes'] == 'Keep notes'
        assert store.get('accounts', b['id']) == b
        assert engine.amazon.contexts[0].account['proxy'] == a['proxy']
        assert engine.amazon.contexts[0].closed
        assert not engine.account_locks[a['id']].locked()
        assert engine.browser_slots._value == 1
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('immediate', [True, False])
def test_stop_races_and_shared_capacity(tmp_path, immediate):
    async def scenario():
        store, engine, (a, b) = fixture(tmp_path)
        care = engine.account_care
        gate = asyncio.Event()
        async def dwell(*args):
            await gate.wait()
        care.dwell = dwell
        await care.start(a['id'])
        if not immediate:
            await until(lambda: engine.amazon.visits)
        await care.start(b['id'])
        await asyncio.gather(care.stop(a['id']), care.stop(a['id']))
        await until(lambda: any(c.account['id'] == b['id'] for c in engine.amazon.contexts))
        assert len([c for c in engine.amazon.contexts if not c.closed]) == 1
        await engine.stop_all()
        assert not care.runs
        assert all(not lock.locked() for lock in engine.account_locks.values())
        assert engine.browser_slots._value == 1
        await care.start(a['id'])
        await care.stop(a['id'])
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_cleanup_failure_retains_ownership_until_retry(tmp_path):
    async def scenario():
        store, engine, (a, _) = fixture(tmp_path)
        care = engine.account_care
        async def dwell(*args):
            engine.amazon.contexts[0].fail_close = True
        care.dwell = dwell
        await care.start(a['id'])
        await until(lambda: care.runs[a['id']]['job'].done())
        assert engine.account_locks[a['id']].locked()
        assert engine.browser_slots._value == 0
        assert next(c for c in care.snapshot() if c['account_id'] == a['id'])['run']['status'] == 'cleanup_required'
        engine.amazon.contexts[0].fail_close = False
        await care.stop(a['id'])
        assert not care.runs and engine.browser_slots._value == 1
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('interruption', ['signed_out', 'rate_limit', 'connection'])
def test_interruptions_do_not_retry_or_mutate_session(tmp_path, interruption):
    async def scenario():
        store, engine, (a, _) = fixture(tmp_path)
        care = engine.account_care
        if interruption == 'signed_out':
            engine.amazon.label = 'Hello, sign in'
        else:
            engine.amazon.error = BackoffRequired('Fixture rate limit', status=429, retry_after_seconds=3600) if interruption == 'rate_limit' else RuntimeError('secret proxy error')
        await care.start(a['id'])
        await until(lambda: not care.runs)
        run = next(c for c in care.snapshot() if c['account_id'] == a['id'])['run']
        assert run['status'] == ('cooldown' if interruption == 'rate_limit' else 'attention')
        assert len(engine.amazon.visits) == 1
        assert store.get('accounts', a['id']) == a
        assert 'secret' not in run['message']
        if interruption == 'rate_limit':
            assert (datetime.fromisoformat(run['retry_at']) - datetime.now(timezone.utc)).total_seconds() > 3590
            with pytest.raises(ValueError, match='cooldown'):
                await care.start(a['id'])
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_restart_marks_runs_interrupted_without_restarting(tmp_path):
    store, engine, (a, _) = fixture(tmp_path)
    engine.account_care.update(a['id'], status='browsing', routine=BrowsingRoutine().model_dump())
    care = AccountCare(engine)
    assert next(c for c in care.snapshot() if c['account_id'] == a['id'])['run']['status'] == 'interrupted'
    assert not care.runs
    assert store.get('accounts', a['id']) == a
    store.db.close()


def test_stop_during_natural_cleanup_waits_for_browser_owner(tmp_path):
    async def scenario():
        store, engine, (a, _) = fixture(tmp_path)
        care = engine.account_care
        closing, release = asyncio.Event(), asyncio.Event()
        async def dwell(*args):
            context = engine.amazon.contexts[0]
            async def close():
                closing.set()
                await release.wait()
                context.closed = True
            context.close = close
        care.dwell = dwell
        await care.start(a['id'])
        await closing.wait()
        stop = asyncio.create_task(care.stop(a['id']))
        await asyncio.sleep(.02)
        assert not stop.done() and engine.account_locks[a['id']].locked()
        release.set()
        await stop
        assert not care.runs and engine.amazon.contexts[0].closed
        assert engine.browser_slots._value == 1
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_read_only_browsing_in_real_browser_fixture(tmp_path):
    """Actual navigation, header readiness, smooth scroll and cookie capture; no network."""
    import json
    import time
    from patchright.async_api import async_playwright
    from retail.amazon import Amazon, Attention

    async def scenario():
        store, engine, (a, _) = fixture(tmp_path)
        engine.amazon = Amazon(store)
        care = engine.account_care
        care.save(a['id'], BrowsingRoutine(products=['B012345678'], visit_limit=1, seconds_per_page=15))
        requests, observations = [], []
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            async def create_context(account, **kwargs):
                assert kwargs['interactive'] and kwargs['headless'] is False
                context = await browser.new_context()
                async def respond(route):
                    requests.append((route.request.method, route.request.url))
                    await route.fulfill(content_type='text/html', body='''<html><body style="height:4000px" data-clicks="0" data-scrolls="0">
                        <a id="nav-link-accountList"><span class="nav-line-1">Hello, Fixture</span></a>
                        <h1>Fixture product</h1><button onclick="document.body.dataset.clicks=1">Add to Cart</button>
                        <script>addEventListener('scroll',()=>document.body.dataset.scrolls=Number(document.body.dataset.scrolls)+1);</script>
                        </body></html>''')
                await context.route('**/*', respond)
                original_close = context.close
                async def close():
                    observations.append(await context.pages[0].evaluate('({scrolls:Number(document.body.dataset.scrolls),clicks:Number(document.body.dataset.clicks),y:scrollY})'))
                    await original_close()
                context.close = close
                return context
            engine.amazon.context = create_context
            engine.amazon.expose = AsyncMock()
            start = time.perf_counter()
            await care.start(a['id'])
            async with asyncio.timeout(30):
                await care.runs[a['id']]['job']
            await until(lambda: not care.runs)
            elapsed = time.perf_counter() - start
            assert store.get('care_runs', 'care-run-'+a['id'])['status'] == 'completed'
            assert requests == [('GET', 'https://www.amazon.com/dp/B012345678')]
            assert observations[0]['scrolls'] > 0 and observations[0]['clicks'] == 0
            assert elapsed >= 15
            # Challenge, signed-out and off-site documents are stopped read-only.
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(body='<body>Fixture</body>', content_type='text/html'))
            for document in ['<input id="captchacharacters">', '<a id="nav-link-accountList"><span class="nav-line-1">Hello, sign in</span></a>']:
                await page.goto('https://www.amazon.com/dp/B012345678')
                await page.set_content(document)
                with pytest.raises(Attention):
                    await care.inspect(page, 'www.amazon.com')
            await page.goto('https://example.test/')
            with pytest.raises(Attention):
                await care.inspect(page, 'www.amazon.com')
            output = Path('artifacts/account-care-review')
            output.mkdir(parents=True, exist_ok=True)
            (output / 'fixture-measurement.json').write_text(json.dumps({'reading_seconds': store.get('care_runs', 'care-run-'+a['id'])['history'][0]['reading_seconds'], 'elapsed_seconds': round(elapsed, 3), 'requests': len(requests), 'purchase_clicks': observations[0]['clicks'], 'scroll_events': observations[0]['scrolls'], 'limitations': 'One local routed Chromium fixture. Reading delay is intentional; excludes real retailer latency and browser identity startup.'}, indent=2))
            await browser.close()
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_api_guards_account_network_and_browser_configuration(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, headers={'X-Retail-Client': 'dashboard'}) as client:
        engine = app.state.engine
        engine.amazon = Adapter()
        a = client.post('/api/accounts', json={'name': 'Alpha', 'email': 'alpha@example.test', 'password': 'secret'}).json()
        proxy = client.post('/api/proxies', json={'name': 'Fixture', 'entries': 'localhost:1234'}).json()
        app.state.store.put('accounts', {**app.state.store.get('accounts', a['id']), 'proxy_list_id': proxy['id'], 'session': {'cookies': [], 'origins': []}})
        assert client.put(f"/api/account-care/{a['id']}/routine", json={'seconds_per_page': 15}).status_code == 200
        assert client.post(f"/api/account-care/{a['id']}/start").status_code == 200
        assert client.put(f"/api/accounts/{a['id']}", json={'name': 'Blocked'}).status_code == 409
        assert client.delete(f"/api/accounts/{a['id']}").status_code == 409
        assert client.post(f"/api/accounts/{a['id']}/login").status_code == 409
        assert client.put(f"/api/proxies/{proxy['id']}", json={'entries': 'localhost:1235'}).status_code == 409
        assert client.post('/api/settings', json={'max_running_tasks': 2}).status_code == 409
        assert client.put(f"/api/account-care/{a['id']}/routine", json={'seconds_per_page': 30}).status_code == 409
        assert client.post(f"/api/account-care/{a['id']}/stop").json()['ok']
        assert client.get('/api/state').json()['accounts'][0]['name'] == 'Alpha'


def test_stop_api_acknowledges_before_slow_close_and_holds_lease(tmp_path):
    import time
    import json
    app = create_app(tmp_path)
    with TestClient(app, headers={'X-Retail-Client': 'dashboard'}) as client:
        engine = app.state.engine
        engine.amazon = Adapter()
        account = app.state.store.put('accounts', {'name': 'Fixture', 'region': 'US', 'retailer': 'amazon', 'session': {'cookies': []}})
        care = engine.account_care
        original = engine.amazon.context
        async def context(*args, **kwargs):
            result = await original(*args, **kwargs)
            async def close():
                await asyncio.sleep(1)
                result.closed = True
            result.close = close
            return result
        engine.amazon.context = context
        start = time.perf_counter()
        assert client.post(f"/api/account-care/{account['id']}/start").status_code == 200
        start_ms = (time.perf_counter()-start)*1000
        for _ in range(100):
            if engine.amazon.contexts:
                break
            time.sleep(.01)
        start = time.perf_counter()
        response = client.post(f"/api/account-care/{account['id']}/stop")
        stop_ms = (time.perf_counter()-start)*1000
        assert response.status_code == 202 and response.json()['stopping']
        assert stop_ms < 500 and start_ms < 500
        assert engine.account_locks[account['id']].locked()
        assert client.post(f"/api/account-care/{account['id']}/start").status_code == 409
        assert client.get('/api/account-care').json()['account_care'][0]['run']['status'] == 'stopping'
        for _ in range(200):
            if account['id'] not in care.runs:
                break
            time.sleep(.01)
        assert account['id'] not in care.runs and engine.amazon.contexts[0].closed
        assert client.post(f"/api/account-care/{account['id']}/start").status_code == 200
        client.post(f"/api/account-care/{account['id']}/stop")
        output = Path('artifacts/account-care-review')
        output.mkdir(parents=True, exist_ok=True)
        (output/'control-measurement.json').write_text(json.dumps({'start_ack_ms': round(start_ms, 2), 'stop_ack_ms': round(stop_ms, 2), 'fixture_close_seconds': 1, 'limitations': 'Local TestClient, fake browser with one-second teardown. Measures acknowledgement, not complete browser startup or closure.'}, indent=2))


def test_browser_care_workspace(tmp_path):
    from patchright.async_api import async_playwright, expect
    app = create_app(tmp_path)
    headers = {'X-Retail-Client': 'dashboard'}
    with TestClient(app, headers=headers) as client:
        app.state.engine.amazon = Adapter()
        # This test drives the dashboard against a fake retailer page. Keep its
        # routine alive until Stop; actual reading DOM/input has browser fixtures.
        async def reading_until_stopped(*_):
            await asyncio.Event().wait()
        app.state.engine.account_care.dwell = reading_until_stopped
        accounts = [client.post('/api/accounts', json={'name': name, 'email': name.lower()+'@example.test'}).json() for name in ('Alpha', 'Beta')]
        for account in accounts:
            saved = app.state.store.get('accounts', account['id'])
            app.state.store.put('accounts', {**saved, 'logged_in': True, 'session': {'cookies': [], 'origins': []}})
        app.state.engine.account_care.update(accounts[0]['id'], status='completed', completed=1, total=1, interaction_count=3,
            started_at='2026-10-11T12:00:00+00:00', ended_at='2026-10-11T12:02:00+00:00',
            history=[{'page':'B012345678', 'at':'2026-10-11T12:01:00+00:00', 'reading_seconds':25,
                      'interactions':[{'label':label, 'reading_seconds':4} for label in ['Blue variant', 'Expand product details', 'Read customer reviews']]}])
        folder = client.post('/api/folders', json={'name': 'Everyday accounts', 'resource_kind': 'accounts'}).json()
        client.post('/api/organization/members', json={'folder_id': folder['id'], 'ids': [accounts[0]['id']], 'action': 'add'})

        async def scenario():
            async with async_playwright() as driver:
                browser = await driver.chromium.launch(headless=True)
                page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                async def local_route(route):
                    request = route.request
                    url = urlsplit(request.url)
                    response = client.request(request.method, url.path, content=request.post_data, headers=headers)
                    await route.fulfill(status=response.status_code, body=response.content, headers={'content-type': response.headers.get('content-type', 'text/plain')})
                await page.route('**/*', local_route)
                await page.goto('http://127.0.0.1/')
                await page.locator('[data-view=accounts]').click()
                await page.get_by_role('tab', name='Sessions & relationships').click()
                await expect(page.get_by_role('heading', name='Account care', exact=True)).to_be_visible()
                cdp = await page.context.new_cdp_session(page)
                initial = await cdp.send('Runtime.evaluate', {'expression': "(() => { const saved=state.folders; try { state.folders=undefined; return managerView().includes('Automated browsing'); } finally { state.folders=saved; } })()", 'returnByValue': True})
                assert initial['result']['value']
                await cdp.detach()
                await page.get_by_role('button', name='Set routine', exact=True).click()
                await page.get_by_label('Browsing mode', exact=True).select_option('ai')
                await expect(page.get_by_label('Products to browse')).not_to_be_visible()
                await expect(page.get_by_label('Browsing interests')).to_be_visible()
                await page.get_by_label('Browsing interests').fill('Coffee equipment')
                await page.get_by_label('Browsing mode', exact=True).select_option('products')
                await page.get_by_label('Products to browse').fill('B012345678\nB087654321')
                await page.get_by_label('Maximum pages per run').fill('2')
                await page.get_by_label('Minimum reading time (seconds)').fill('15')
                await page.get_by_label('Minimum reading time (seconds)').fill('20')
                await page.get_by_label('Maximum reading time (seconds)').fill('19')
                await page.get_by_role('button', name='Save routine', exact=True).click()
                await expect(page.locator('.form-error')).to_contain_text('greater than minimum')
                await expect(page.get_by_label('Maximum reading time (seconds)')).to_be_focused()
                await page.get_by_label('Minimum reading time (seconds)').fill('15')
                await page.get_by_role('button', name='Save routine', exact=True).click()
                await expect(page.locator('dialog[open]')).to_have_count(0)
                assert all(c['routine']['products'] == ['B012345678', 'B087654321'] for c in client.get('/api/state').json()['account_care'])
                assert all(c['routine']['min_seconds_per_page'] == 15 and c['routine']['max_seconds_per_page'] == 19 for c in client.get('/api/account-care').json()['account_care'])
                await page.get_by_label('Select Alpha', exact=True).check()
                await page.get_by_role('button', name='Configured', exact=True).click()
                await expect(page.get_by_label('Select Alpha', exact=True)).not_to_be_checked()
                await page.get_by_role('button', name='All', exact=True).click()
                await page.get_by_role('button', name='Session tools', exact=True).click()
                await expect(page.get_by_role('button', name='Verify sessions', exact=True)).to_be_visible()
                await page.get_by_role('button', name='Close', exact=True).click()
                await page.get_by_label('Select Alpha', exact=True).check()
                await page.get_by_label('Search accounts', exact=True).fill('Beta')
                await expect(page.get_by_role('button', name='Start visible', exact=True)).to_be_visible()
                await expect(page.get_by_label('Search accounts', exact=True)).to_be_focused()
                await page.get_by_role('button', name='Start visible', exact=True).click()
                await expect(page.locator('.care-table')).to_contain_text('Browsing')
                assert [c['account_id'] for c in client.get('/api/state').json()['account_care'] if c['active']] == [accounts[1]['id']]
                await page.get_by_label('Search accounts', exact=True).fill('')
                shots = Path('artifacts/account-care-review')
                shots.mkdir(parents=True, exist_ok=True)
                for width in (1440, 390):
                    await page.set_viewport_size({'width': width, 'height': 1000})
                    await page.screenshot(path=str(shots / f'workspace-{width}.png'), full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await page.get_by_role('button', name='Routine and session for Alpha').click()
                    await expect(page.get_by_label('Products to browse')).to_have_value('B012345678\nB087654321')
                    await expect(page.get_by_label('Minimum reading time (seconds)')).to_have_value('15')
                    await expect(page.get_by_label('Maximum reading time (seconds)')).to_have_value('19')
                    await page.get_by_label('Browsing mode', exact=True).select_option('ai')
                    await expect(page.get_by_label('Browsing interests')).to_have_value('Coffee equipment')
                    await expect(page.locator('[data-care-ai]')).to_contain_text('Up to four interactions per product')
                    await page.screenshot(path=str(shots / f'ai-routine-{width}.png'), full_page=False)
                    await page.get_by_label('Browsing mode', exact=True).select_option('products')
                    await page.screenshot(path=str(shots / f'routine-{width}.png'), full_page=False)
                    await page.get_by_text('Latest run history', exact=True).click()
                    await page.locator('.care-history small').last.scroll_into_view_if_needed()
                    await expect(page.locator('.care-history')).to_contain_text('Read customer reviews / 4s')
                    await expect(page.locator('.care-history')).to_contain_text('Blue variant / 4s')
                    await page.screenshot(path=str(shots / f'interaction-history-{width}.png'), full_page=False)
                    await page.get_by_role('button', name='Close', exact=True).click()
                await page.get_by_role('button', name='Stop visible', exact=True).click()
                await expect(page.locator('.care-table')).to_contain_text('Stopped')
                await page.get_by_role('button', name='Needs attention', exact=True).click()
                await expect(page.get_by_role('heading', name='No matching accounts')).to_be_visible()
                assert not errors, errors
                await browser.close()
        asyncio.run(scenario())
