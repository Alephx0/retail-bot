import asyncio

import pytest
from fastapi.testclient import TestClient

from retail.app import create_app
from retail.engine import Engine
from retail.models import Group, Task, account_fingerprint_settings
from retail.monitors import monitor_items
from retail.store import Store


class Page:
    def __init__(self, context):
        self.context = context

    async def close(self):
        pass


class Context:
    def __init__(self, identity):
        self.identity = identity
        self.closed = False

    async def new_page(self):
        return Page(self)

    async def close(self):
        self.closed = True


class Adapter:
    def __init__(self):
        self.contexts = []
        self.routes = []
        self.stock = set()
        self.carts = []
        self.account_gate = None
        self.monitor_error = None

    async def context(self, identity, proxy='', solver=''):
        context = Context(identity)
        self.contexts.append(context)
        self.routes.append(proxy)
        return context

    async def ensure_session(self, *args):
        if self.account_gate:
            await self.account_gate.wait()

    async def inspect(self, page, item, region):
        if self.monitor_error and page.context.identity['id'].startswith('monitor-'):
            raise self.monitor_error
        return {**item, 'title': 'Fixture', 'price': 10, 'available': item['asin'] in self.stock,
                'amazon_seller': True, 'seller': 'Amazon.com', 'condition': 'new'}

    async def cart(self, page, quantity, asin):
        self.carts.append((page.context.identity['id'], asin))
        return quantity

    async def prepare_checkout(self, *args):
        pass

    async def checkout_snapshot(self, page, asin, quantity, *args, **kwargs):
        return {'asin': asin, 'quantity': quantity, 'total': 10, 'currency': 'USD'}

    async def close(self):
        pass


async def until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(.01)


def fixture(tmp_path):
    store = Store(tmp_path)
    group = store.put('groups', Group(name='Watch', products='B012345678\nB087654321').model_dump())
    account = store.put('accounts', {'name': 'Buyer', 'region': 'US', 'password': 'private', 'proxy': 'account.example:80'})
    engine = Engine(store)
    engine.amazon = Adapter()
    return store, group, account, engine


def task_record(store, group, account, **changes):
    return store.put('tasks', Task(group_id=group['id'], account_id=account['id'], simulation=False,
                                  checkout_mode='quote', **changes).model_dump(mode='json'))


def test_shared_watchers_leave_account_free_and_close_on_last_stop(tmp_path):
    async def scenario():
        store, group, account, engine = fixture(tmp_path)
        first = task_record(store, group, account)
        second = task_record(store, group, account)
        try:
            await engine.start(first['id'])
            await engine.start(second['id'])
            await until(lambda: len(engine.monitors.active)==2 and all(m.sequence for m in engine.monitors.active.values()))
            assert len(engine.amazon.contexts)==2
            assert not engine.account_locks[account['id']].locked()
            assert engine.browser_slots._value==10
            assert engine.amazon.routes==['', '']
            identities = [context.identity for context in engine.amazon.contexts]
            assert len({x['fingerprint_seed'] for x in identities})==2
            assert all('password' not in x and 'session' not in x for x in identities)
            assert all(account_fingerprint_settings({}, x)['fingerprint_backend']=='fingerprint-suite' for x in identities)
            assert all(len(m.subscribers)==2 for m in engine.monitors.active.values())
            await engine.stop(first['id'])
            assert len(engine.monitors.active)==2
            assert not any(c.closed for c in engine.amazon.contexts)
            await engine.stop(second['id'])
            assert not engine.monitors.active
            assert all(c.closed for c in engine.amazon.contexts)
            assert all(m['status']=='stopped' for m in engine.monitors.snapshot())
        finally:
            await engine.close()
            store.db.close()
    asyncio.run(scenario())


def test_task_assignment_ignores_stock_on_other_monitor(tmp_path):
    async def scenario():
        store, group, account, engine = fixture(tmp_path)
        engine.amazon.stock.add('B012345678')
        ready = task_record(store, group, account, monitor_asin='B012345678')
        waiting = task_record(store, group, account, monitor_asin='B087654321')
        try:
            await engine.start(waiting['id'])
            await engine.start(ready['id'])
            await until(lambda: ready['id'] not in engine.jobs)
            assert store.get('tasks', ready['id'])['status']=='completed'
            assert engine.amazon.carts==[(account['id'], 'B012345678')]
            assert waiting['id'] in engine.jobs
            assert not engine.account_locks[account['id']].locked()
        finally:
            await engine.close()
            store.db.close()
    asyncio.run(scenario())


def test_restock_wakes_task_and_revalidates_stock_in_account(tmp_path):
    import time

    async def scenario():
        store, group, account, engine = fixture(tmp_path)
        task = task_record(store, group, account, monitor_asin='B012345678')
        try:
            await engine.start(task['id'])
            await until(lambda: engine.monitors.active and all(m.sequence for m in engine.monitors.active.values()))
            assert not engine.amazon.carts
            assert not engine.account_locks[account['id']].locked()
            monitor = next(iter(engine.monitors.active.values()))
            # A monitor signal wakes a subscriber; it is not enough to cart if
            # the purchasing account's fresh inspection still says unavailable.
            monitor.product = {**monitor.product, 'available': True}
            monitor.sequence += 1
            monitor.observed_at = time.monotonic()
            monitor.update('in_stock', 'Fixture stock observation')
            await until(lambda: len(engine.amazon.contexts) > 1 and
                        not engine.account_locks[account['id']].locked())
            assert not engine.amazon.carts
            engine.amazon.stock.add('B012345678')
            monitor.sequence += 1
            monitor.observed_at = time.monotonic()
            monitor.update('in_stock', 'Fixture restock observation')
            await until(lambda: task['id'] not in engine.jobs)
            assert store.get('tasks', task['id'])['status']=='completed'
            assert engine.amazon.carts==[(account['id'], 'B012345678')]
        finally:
            await engine.close()
            store.db.close()
    asyncio.run(scenario())


def test_cancel_queued_checkout_preserves_owner_then_releases(tmp_path):
    async def scenario():
        store, group, account, engine = fixture(tmp_path)
        engine.amazon.stock.add('B012345678')
        engine.amazon.account_gate = asyncio.Event()
        first = task_record(store, group, account, monitor_asin='B012345678')
        second = task_record(store, group, account, monitor_asin='B012345678')
        try:
            await engine.start(first['id'])
            await until(lambda: store.get('tasks', first['id']).get('status')=='authenticating')
            await engine.start(second['id'])
            await until(lambda: store.get('tasks', second['id']).get('status')=='in_queue')
            await engine.stop(second['id'])
            assert engine.account_locks[account['id']].locked()
            await engine.stop(first['id'])
            assert not engine.account_locks[account['id']].locked()
            engine.amazon.account_gate.set()
            await engine.start(second['id'])
            await until(lambda: second['id'] not in engine.jobs)
            assert store.get('tasks', second['id'])['status']=='completed'
        finally:
            await engine.close()
            store.db.close()
    asyncio.run(scenario())


def test_monitor_challenge_stops_without_carting_or_recreating_identity(tmp_path):
    from retail.amazon import ChallengeDetected
    async def scenario():
        store, group, account, engine = fixture(tmp_path)
        engine.amazon.monitor_error = ChallengeDetected('Verification required')
        task = task_record(store, group, account, monitor_asin='B012345678')
        try:
            await engine.start(task['id'])
            await until(lambda: task['id'] not in engine.jobs)
            assert store.get('tasks', task['id'])['status']=='error'
            assert not engine.amazon.carts
            assert len(engine.amazon.contexts)==1
            assert engine.monitors.snapshot()[0]['status']=='attention'
            assert not engine.account_locks[account['id']].locked()
        finally:
            await engine.close()
            store.db.close()
    asyncio.run(scenario())


def test_api_normalizes_links_and_validates_assignment(tmp_path):
    with TestClient(create_app(tmp_path), headers={'X-Retail-Client': 'dashboard'}) as client:
        response = client.post('/api/groups', json={'name': 'Products', 'products':
            'https://www.amazon.com/name/dp/b012345678?ref=test\nhttp://amazon.ca/gp/aw/d/B087654321'})
        assert response.status_code==200, response.text
        group = response.json()
        assert group['products']=='B012345678\nB087654321'
        assert client.post('/api/tasks', json={'group_id': group['id'], 'monitor_asin': 'B000000000'}).status_code==422
        task = client.post('/api/tasks', json={'group_id': group['id'], 'monitor_asin': 'B087654321', 'checkout_mode': 'monitor'}).json()
        assert task['monitor_asin']=='B087654321'
        assert client.post('/api/tasks/'+task['id']+'/start').status_code==200
        state = client.get('/api/state').json()
        assert {m['asin'] for m in state['monitors']}=={'B087654321'}
        assert client.put('/api/tasks/'+task['id'], json={'monitor_asin': 'B012345678'}).status_code==409
        assert client.post('/api/tasks/'+task['id']+'/stop').status_code==200
        assert not client.get('/api/state').json()['active']


def test_removed_assignment_rejected_at_start(tmp_path):
    async def scenario():
        store, group, account, engine = fixture(tmp_path)
        task = task_record(store, group, account, monitor_asin='B087654321')
        store.put('groups', {**group, 'products': 'B012345678'})
        try:
            with pytest.raises(ValueError, match='assigned monitor'):
                await engine.start(task['id'])
            assert not engine.jobs and not engine.monitors.active
        finally:
            await engine.close()
            store.db.close()
    asyncio.run(scenario())


def test_browser_monitor_input_start_assignment_and_live_log(tmp_path):
    from patchright.async_api import async_playwright, expect
    from urllib.parse import urlsplit

    headers = {'X-Retail-Client': 'dashboard'}
    with TestClient(create_app(tmp_path), headers=headers) as client:
        group = client.post('/api/groups', json={'name': 'Monitor UI', 'products': 'B012345678'}).json()
        task = client.post('/api/tasks', json={'group_id': group['id'], 'checkout_mode': 'monitor'}).json()

        async def scenario():
            async with async_playwright() as driver:
                browser = await driver.chromium.launch(headless=True)
                page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))

                async def local_route(route):
                    request = route.request
                    url = urlsplit(request.url)
                    response = client.request(request.method, url.path + ('?' + url.query if url.query else ''),
                                              content=request.post_data, headers=headers)
                    await route.fulfill(status=response.status_code, body=response.content,
                                        headers={'content-type': response.headers.get('content-type', 'text/plain')})

                await page.route('**/*', local_route)
                await page.goto('http://127.0.0.1/')
                await page.locator('[data-view=task_groups]').click()
                await page.locator(f'[data-work-open="{group["id"]}"]').first.click()
                await page.locator('[data-work-tab=monitoring]').click()
                assert not errors, errors
                assert await page.locator('#monitor-products').count(), await page.locator('#screen').inner_text()
                field = page.locator('#monitor-products')
                await field.fill('https://www.amazon.com/title/dp/b012345678?ref=test\nB087654321')
                await page.locator('#f-delay_ms').focus()
                await expect(field).to_have_value('B012345678\nB087654321')
                await page.locator('[data-work-settings] [type=submit]').click()
                await expect(page.locator('#toast')).to_contain_text('Group settings saved')
                assert not client.get('/api/state').json()['monitors']
                await page.locator(f'[data-work-task="{task["id"]}"][data-work-command=start]').click()
                await expect(page.locator('[data-monitor-log]')).to_have_count(2)
                assert client.get('/api/state').json()['groups'][0]['products']=='B012345678\nB087654321'
                await page.locator('[data-monitor-log]').first.click()
                await expect(page.locator('dialog[open]')).to_contain_text('monitor activity')
                await expect(page.locator('[data-monitor-log-body]')).to_contain_text('device network')
                await expect(page.locator('[data-monitor-log-body]')).to_contain_text('Checking stock')
                await page.locator('[data-close-monitor-log]').click()
                await page.locator(f'[data-work-task="{task["id"]}"][data-work-command=stop]').click()
                selector = page.locator(f'[data-task-monitor="{task["id"]}"]')
                await expect(selector).to_be_enabled()
                await selector.select_option('B087654321')
                await expect(selector).to_have_value('B087654321')
                await page.locator(f'[data-work-task="{task["id"]}"][data-work-command=start]').click()
                await expect(page.locator(f'[data-work-task="{task["id"]}"][data-work-command=stop]')).to_be_visible()
                current = client.get('/api/state').json()
                assert {m['asin'] for m in current['monitors'] if m['task_ids']}=={'B087654321'}
                await expect(page.locator(f'[data-task-monitor="{task["id"]}"]')).to_be_disabled()
                await page.screenshot(path=str(tmp_path / 'monitor-workspace.png'), full_page=True)
                assert not errors, errors
                await browser.close()

        asyncio.run(scenario())


def test_real_monitor_contexts_have_distinct_fingerprints_and_no_account_session(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'show_browser_window': False}, 'settings')
        engine = Engine(store)
        adapter = engine.amazon
        group = store.put('groups', Group(name='Local browser fixture', products='B012345678\nB087654321').model_dump())
        contexts = []
        real_context = adapter.context

        async def local_context(*args):
            context = await real_context(*args)
            contexts.append(context)
            await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <body><span id="productTitle">Local fixture</span>
                <div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$10.00</span></span></div>
                <div id="merchant-info">Sold by Amazon.com</div><div id="availability">Currently unavailable</div></body>'''))
            return context

        adapter.context = local_context
        task = {'id': 'fixture-task', 'simulation': False}
        monitors = engine.monitors.subscribe(task, group, monitor_items(store, group), 'US')
        try:
            async with asyncio.timeout(45):
                while not all(m.sequence or m.error for m in monitors):
                    await asyncio.sleep(.05)
            assert not [m.error for m in monitors if m.error]
            assert len(contexts)==2
            assert len({c._retail_suite_profile['digest'] for c in contexts})==2
            for context in contexts:
                assert not (await context.storage_state())['cookies']
            assert all(m.status=='out_of_stock' for m in monitors)
        finally:
            await engine.monitors.unsubscribe(task['id'], monitors)
            await engine.close()
            store.db.close()
    asyncio.run(scenario())
