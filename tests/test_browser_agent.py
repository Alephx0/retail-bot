import asyncio
import json
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient
from patchright.async_api import async_playwright

from retail.ai_provider import AIProvider, ProviderError
from retail.amazon import Amazon, Attention
from retail.app import create_app
from retail.browser_agent import BrowserAgent
from retail.browser_mcp import AMAZON_ACTIONS, BrowserTools, PriceTools
from retail.interactions import InteractionError
from retail.models import AIConnection
from retail.store import Store


def test_browser_window_setting_controls_launch(tmp_path, monkeypatch):
    class FakeBrowser:
        def is_connected(self):
            return True

    class FakeChromium:
        def __init__(self):
            self.options = []

        async def launch(self, **options):
            self.options.append(options)
            return FakeBrowser()

    class FakeDriver:
        def __init__(self):
            self.chromium = FakeChromium()

    class FakePlaywright:
        def __init__(self, driver):
            self.driver = driver

        async def start(self):
            return self.driver

    async def scenario():
        store = Store(tmp_path)
        driver = FakeDriver()
        monkeypatch.setattr('retail.amazon.async_playwright', lambda: FakePlaywright(driver))
        adapter = Amazon(store)
        await adapter.ready()
        assert len(driver.chromium.options) == 1
        assert driver.chromium.options[0]['headless'] is False
        assert driver.chromium.options[0]['channel'] == 'chromium'
        assert adapter.browser_visible
        store.put('settings', {'show_browser_window': False}, 'settings')
        adapter.browser = None
        await adapter.ready()
        assert driver.chromium.options[-1]['headless'] is True
        assert not adapter.browser_visible
        store.db.close()

    asyncio.run(scenario())


def test_live_view_requires_local_header_and_open_task_page(tmp_path):
    class FakePage:
        def is_closed(self):
            return False

        async def screenshot(self, **options):
            assert options['type'] == 'jpeg'
            return b'\xff\xd8fixture\xff\xd9'

    with TestClient(create_app(tmp_path)) as client:
        engine = client.app.state.engine
        client.app.state.store.put('tasks', {'id': 'fixture', 'group_id': 'fixture', 'status': 'running'}, 'fixture')
        path = '/api/tasks/fixture/live-frame'
        assert client.get(path).status_code == 403
        assert client.get(path, headers={'X-Retail-Client': 'dashboard'}).status_code == 409
        engine.pages['fixture'] = [FakePage()]
        response = client.get(path, headers={'X-Retail-Client': 'dashboard'})
        assert response.status_code == 200
        assert response.headers['content-type'] == 'image/jpeg'
        assert response.headers['cache-control'] == 'no-store'
        assert response.content == b'\xff\xd8fixture\xff\xd9'
        assert client.get('/api/browser/tasks/fixture/frame').status_code == 403
        assert client.get('/api/browser/tasks/fixture/frame', headers={'X-Retail-Client': 'dashboard'}).content == response.content
        assert client.post('/api/browser/tasks/fixture/input', json={'kind': 'text', 'text': 'secret'}).status_code == 403
        assert client.post('/api/browser/tasks/fixture/input', headers={'X-Retail-Client': 'dashboard'},
                           json={'kind': 'text', 'text': 'secret'}).status_code == 409
        client.app.state.store.put('accounts', {'id': 'account', 'name': 'Fixture',
                                                'session_storage': {'www.amazon.com': {'fixture': 'PRIVATE'}}}, 'account')
        assert 'PRIVATE' not in client.get('/api/state').text


def test_keys_preserved_redacted_and_connections_validated(tmp_path):
    with TestClient(create_app(tmp_path), headers={'X-Retail-Client': 'dashboard'}) as client:
        data = {'name': 'OpenAI', 'api_key': 'SECRET-API-KEY'}
        saved = client.post('/api/ai_connections', json=data).json()
        assert saved['has_api_key'] and 'api_key' not in saved
        updated = client.put('/api/ai_connections/' + saved['id'], json={'name': 'Renamed', 'api_key': ''})
        assert updated.json()['has_api_key']
        assert 'SECRET-API-KEY' not in client.get('/api/state').text
        assert b'SECRET-API-KEY' not in (tmp_path / 'retail.sqlite3').read_bytes()
        assert client.post('/api/settings', json={'agent_mode': 'agent'}).status_code == 422
        assert client.post('/api/settings', json={'agent_mode': 'recovery', 'ai_connection_id': saved['id']}).status_code == 200
        assert client.delete('/api/ai_connections/' + saved['id']).status_code == 409
        assert not client.put('/api/ai_connections/' + saved['id'], json={'clear_api_key': True}).json()['has_api_key']
        assert client.post('/api/ai_connections', json={**data, 'base_url': 'https://other.example/v1'}).status_code == 422
        assert client.post('/api/ai_connections', json={**data, 'provider': 'compatible', 'base_url': 'http://other.example/v1'}).status_code == 422
        assert client.post('/api/ai_connections', json={**data, 'provider': 'compatible', 'protocol': 'chat', 'base_url': 'https://other.example/v1'}).status_code == 200


def test_provider_protocol_and_errors():
    async def scenario():
        for protocol in ('responses', 'chat'):
            def handler(request):
                data = json.loads(request.content)
                assert request.headers['Authorization'] == 'Bearer TEST-KEY'
                if protocol == 'responses':
                    assert request.url.path == '/v1/responses' and data['store'] is False
                    return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'function_call', 'call_id': 'x', 'name': 'connection_ok', 'arguments': '{}'}]})
                assert request.url.path == '/v1/chat/completions'
                return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'tool_calls': [{'id': 'x', 'type': 'function', 'function': {'name': 'connection_ok', 'arguments': '{}'}}]}}]})
            provider = AIProvider({'protocol': protocol, 'base_url': 'https://example.test/v1', 'model': 'fixture', 'api_key': 'TEST-KEY'}, transport=httpx.MockTransport(handler))
            assert (await provider.test())['ok']
            provider.transport = httpx.MockTransport(lambda request: httpx.Response(401, text='echo TEST-KEY'))
            with pytest.raises(ProviderError, match='401') as error:
                await provider.test()
            assert 'TEST-KEY' not in str(error.value)
    asyncio.run(scenario())


def test_real_mcp_round_trip_recovery_and_rejections(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        connection = store.put('ai_connections', AIConnection(name='fixture', api_key='unused').model_dump())
        store.put('settings', {'agent_mode': 'recovery', 'ai_connection_id': connection['id'], 'agent_max_steps': 3}, 'settings')

        class FakeProvider:
            def __init__(self, connection): self.calls = 0
            async def turn(self, instructions, history, tools):
                assert {t['name'] for t in tools} == {'observe_controls', 'inspect_accessibility', 'validate_control'}
                self.calls += 1
                return [{'id': str(self.calls), 'name': 'observe_controls' if self.calls == 1 else 'validate_control',
                         'arguments': '{}' if self.calls == 1 else '{"ref":"1"}'}]
            def tool_result(self, history, call, result): history.append(result)

        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('https://www.amazon.com/**', lambda r: r.fulfill(body='<body></body>', content_type='text/html'))
            await page.goto('https://www.amazon.com/dp/B012345678?secret=not-for-ai')
            await page.set_content('<button onclick="window.clicked=true">Add item to cart</button><input type=password value="PRIVATE">')
            adapter = Amazon(store)
            adapter.agent = BrowserAgent(store, provider_factory=FakeProvider)
            node = await adapter.resolve_action(page, 'ADD_TO_CART')
            assert not await page.evaluate('Boolean(window.clicked)', isolated_context=False), 'Replay and trial must never click the real cart'
            await node.click()
            assert await page.evaluate('window.clicked', isolated_context=False)
            assert store.all('agent_runs')[-1]['status'] == 'validated'
            assert len(store.all('repair_recipes')) == 1
            with pytest.raises(InteractionError, match='already attempted'):
                await adapter.resolve_action(page, 'ADD_TO_CART')
            next_page = await browser.new_page()
            await next_page.route('https://www.amazon.com/**', lambda r: r.fulfill(body='<body></body>', content_type='text/html'))
            await next_page.goto('https://www.amazon.com/dp/B012345678')
            await next_page.set_content('<button>Add item to cart</button>')
            assert await adapter.resolve_action(next_page, 'ADD_TO_CART')
            assert len(store.all('agent_runs')) == 1, 'Saved repair should be replayed without another API call'
            await next_page.close()

            tools = BrowserTools(page, 'ADD_TO_CART', {'www.amazon.com'}, AMAZON_ACTIONS)
            observed = await tools.observe_controls()
            assert 'PRIVATE' not in json.dumps(observed) and 'secret' not in observed['url']
            assert (await tools.inspect_accessibility())['button_names'] == ['Add item to cart']
            # A malicious model cannot choose an order control while carting.
            await page.set_content('<button>Place your order</button>')
            await tools.observe_controls()
            assert not (await tools.validate_control('1'))['validated']
            await page.set_content('<button>Add item to cart</button><button>Add item to cart</button>')
            await tools.observe_controls()
            assert not (await tools.validate_control('1'))['validated']
            await page.set_content('<button>Add item to cart</button>')
            await tools.observe_controls()
            await page.set_content('<button>Place order</button>')
            assert not (await tools.validate_control('1'))['validated']  # stale handles are never rebound
            await page.goto('https://www.amazon.com/different')
            with pytest.raises(ValueError, match='Page changed'):
                await tools.observe_controls()
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_mcp_recovers_renamed_final_total_without_clicking(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        connection = store.put('ai_connections', AIConnection(name='fixture', api_key='unused').model_dump())
        store.put('settings', {'agent_mode': 'recovery', 'ai_connection_id': connection['id'], 'agent_max_steps': 3}, 'settings')
        class FakeProvider:
            def __init__(self, connection): self.calls = 0
            async def turn(self, instructions, history, tools):
                self.calls += 1
                return [{'id': str(self.calls), 'name': 'observe_price_rows' if self.calls == 1 else 'validate_total',
                         'arguments': '{}' if self.calls == 1 else '{"ref":"1"}'}]
            def tool_result(self, history, call, result): history.append(result)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body='<body></body>'))
            await page.goto('https://www.amazon.com/checkout/p/example/spc')
            await page.set_content('<div id="spc-orders"><div data-asin="B012345678" data-quantity="1" data-seller="Amazon.com" data-condition="new"><span class="a-price"><span class="a-offscreen">$20.00</span></span></div></div><ul><li>Grand total: $23.50</li></ul><input name="placeYourOrder1" type="submit" value="Place your order" onclick="window.ordered=true">')
            adapter = Amazon(store)
            adapter.agent = BrowserAgent(store, provider_factory=FakeProvider)
            snapshot = await adapter.checkout_snapshot(page, 'B012345678', 1, 30, max_unit_price=20)
            assert snapshot['total'] == 23.50
            assert not await page.evaluate('Boolean(window.ordered)', isolated_context=False)
            assert store.all('agent_runs')[-1]['status'] == 'validated'
            assert len(store.all('price_recipes')) == 1
            tools = PriceTools(page, {'www.amazon.com'})
            await page.set_content('<ul><li>Grand total: $23.50</li><li>Amount due: $24.50</li></ul><input name="placeYourOrder1" type="submit" value="Place your order">')
            await tools.observe_price_rows()
            assert not (await tools.validate_total('1'))['validated']
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_unknown_model_tools_and_step_limit_fail_closed(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        c = store.put('ai_connections', AIConnection(name='fixture').model_dump())
        store.put('settings', {'ai_connection_id': c['id'], 'agent_max_steps': 1}, 'settings')
        class BadProvider:
            tool = 'click_arbitrary'
            def __init__(self, connection): pass
            async def turn(self, *args): return [{'id': 'x', 'name': self.tool, 'arguments': '{}'}]
            def tool_result(self, *args): pass
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('https://www.amazon.com/**', lambda r: r.fulfill(body='<button>Add to cart</button>', content_type='text/html'))
            await page.goto('https://www.amazon.com/dp/B012345678')
            agent = BrowserAgent(store, provider_factory=BadProvider)
            with pytest.raises(InteractionError): await agent.resolve(page, 'ADD_TO_CART', {'www.amazon.com'}, AMAZON_ACTIONS)
            BadProvider.tool = 'observe_controls'
            with pytest.raises(InteractionError): await agent.resolve(page, 'ADD_TO_CART', {'www.amazon.com'}, AMAZON_ACTIONS)
            assert all(r['status'] == 'needs_review' for r in store.all('agent_runs'))
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_checkout_rechecks_price_before_single_submission():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('https://www.amazon.com/**', lambda r: r.fulfill(body='<body></body>', content_type='text/html'))
            await page.goto('https://www.amazon.com/checkout')
            fixture = '''<div id="spc-orders"><div data-asin="B012345678" data-quantity="1" data-seller="Amazon.com" data-condition="new"><span class="a-price"><span class="a-offscreen">$20.00</span></span></div></div><table id="subtotals-marketplace-table"><tr><td>Order total:</td><td id="total">$21.20</td></tr></table><button onclick="window.orders=(window.orders||0)+1;this.insertAdjacentHTML('afterend','<h1>Order placed 123-1234567-1234567</h1>')">Place your order</button>'''
            await page.set_content(fixture)
            adapter = Amazon(None)
            with pytest.raises(Attention): await adapter.submit_order(page)
            await adapter.checkout_snapshot(page, 'B012345678', 1, 25)
            await page.locator('#total').evaluate("e=>e.textContent='$24.00'")
            with pytest.raises(Attention): await adapter.submit_order(page)
            assert not await page.evaluate('Boolean(window.orders)', isolated_context=False)
            await page.locator('#total').evaluate("e=>e.textContent='$21.20'")
            await adapter.checkout_snapshot(page, 'B012345678', 1, 25)
            await adapter.submit_order(page)
            assert await page.evaluate('window.orders', isolated_context=False) == 1
            with pytest.raises(Attention): await adapter.submit_order(page)
            await browser.close()
    asyncio.run(scenario())


def test_monitor_uses_structured_price_and_mcp_control_recovery(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        connection = store.put('ai_connections', AIConnection(name='fixture').model_dump())
        store.put('settings', {'ai_connection_id': connection['id'], 'agent_mode': 'recovery'}, 'settings')
        class FakeProvider:
            def __init__(self, connection): self.step = 0
            async def turn(self, *args):
                self.step += 1
                return [{'id': str(self.step), 'name': 'observe_controls' if self.step == 1 else 'validate_control',
                         'arguments': '{}' if self.step == 1 else '{"ref":"1"}'}]
            def tool_result(self, *args): pass
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            html = '<h1>Changed product</h1><meta property="product:price:amount" content="19.75"><span id="sellerProfileTriggerId">Amazon.com</span><button>Add to bag</button>'
            await page.route('https://www.amazon.com/**', lambda r: r.fulfill(body=html, content_type='text/html'))
            adapter = Amazon(store)
            adapter.agent = BrowserAgent(store, provider_factory=FakeProvider)
            item = await adapter.inspect(page, {'asin': 'B012345678'}, 'US')
            assert item['title'] == 'Changed product' and item['price'] == 19.75 and item['available'], repr(item)
            assert store.all('agent_runs')[0]['status'] == 'validated'
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_cdp_disconnect_keeps_external_chromium_alive(tmp_path):
    async def scenario():
        async with async_playwright() as driver:
            profile = tmp_path / 'chrome-profile'
            process = subprocess.Popen([driver.chromium.executable_path, '--headless', '--remote-debugging-port=0', '--no-first-run', '--no-sandbox', '--user-data-dir=' + str(profile)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            store = Store(tmp_path / 'store')
            adapter = Amazon(store)
            try:
                port_file = profile / 'DevToolsActivePort'
                for _ in range(100):
                    if port_file.exists(): break
                    await asyncio.sleep(.1)
                port = port_file.read_text().splitlines()[0]
                store.put('settings', {'cdp_attach': True, 'cdp_endpoint': 'http://127.0.0.1:' + port}, 'settings')
                context = await adapter.context({'id': 'fixture', 'region': 'US'})
                await context.new_page()
                await adapter.close()
                assert process.poll() is None
                async with httpx.AsyncClient(trust_env=False) as client:
                    assert (await client.get('http://127.0.0.1:' + port + '/json/version')).status_code == 200
            finally:
                await adapter.close()
                process.terminate()
                process.wait(timeout=10)
                store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('seller_missing', [False, True])
def test_amazon_agent_end_to_end_checkout_fixture(tmp_path, seller_missing):
    from retail.engine import Engine
    from retail.models import Group, Task

    async def scenario():
        store = Store(tmp_path)
        connection = store.put('ai_connections', AIConnection(name='fixture').model_dump())
        store.put('settings', {'ai_connection_id': connection['id'], 'agent_mode': 'agent'}, 'settings')
        group = store.put('groups', Group(name='Fixture', products='B012345678;25', max_total=25).model_dump())
        account = store.put('accounts', {'name': 'Fixture', 'region': 'US', 'session': {'cookies': [], 'origins': []}})
        task = store.put('tasks', {**Task(group_id=group['id'], account_id=account['id'], simulation=False, checkout_mode='automatic').model_dump(mode='json'), 'status': 'idle'})
        orders = []
        cart_items = 0

        class FakeProvider:
            def __init__(self, connection): self.step = 0
            async def turn(self, *args):
                self.step += 1
                return [{'id': str(self.step), 'name': 'observe_controls' if self.step == 1 else 'validate_control',
                         'arguments': '{}' if self.step == 1 else '{"ref":"1"}'}]
            def tool_result(self, *args): pass

        async def route(r):
            nonlocal cart_items
            path = r.request.url
            if '/add-item' in path:
                cart_items += 1
                await r.fulfill(body='ok')
                return
            if '/order-history' in path:
                html = '<div id="nav-link-accountList"><span class="nav-line-1">Hello, Fixture</span></div>'
            elif '/dp/' in path:
                html = '<span id="productTitle">Fixture</span><div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$20.00</span></span></div><span id="sellerProfileTriggerId">Amazon.com</span><span id="nav-cart-count">0</span><button onclick="fetch(\'/add-item\').then(()=>document.querySelector(\'#nav-cart-count\').textContent=\'1\')">Add item to cart</button>'
                html += '<div id="nav-link-accountList"><span class="nav-line-1">Hello, Fixture</span></div>'
            elif '/gp/cart/' in path:
                html = '<div id="sc-active-cart">' + ('<div data-asin="B012345678" data-quantity="1">Fixture</div>' if cart_items else '') + '</div><button onclick="location.href=\'/checkout\'">Continue to checkout</button>'
            elif '/checkout' in path:
                html = '<div id="spc-orders"><div data-asin="B012345678" data-quantity="1" data-seller="Amazon.com" data-condition="new"><span class="a-price"><span class="a-offscreen">$20.00</span></span></div></div><table id="subtotals-marketplace-table"><tr><td>Order total:</td><td>$21.20</td></tr></table><button onclick="location.href=\'/confirmation\'">Confirm and place order</button>'
            elif '/confirmation' in path:
                assert store.get('submissions', 'submission-' + task['id'])['status'] == 'submitting'
                orders.append(path)
                html = '<body>Thank you, your order has been placed. 123-1234567-1234567</body>'
            else:
                raise AssertionError('Unexpected request: ' + path)
            if seller_missing:
                html = html.replace('<span id="sellerProfileTriggerId">Amazon.com</span>', '')
                if '/checkout' in path:
                    html = html.replace('data-seller="Amazon.com"', '').replace('data-condition="new"', '')
                    html = html.replace('<span class="a-price">', '<p>Sold by: Amazon.com</p><p>Condition: New</p><span class="a-price">')
            await r.fulfill(body=html, content_type='text/html')

        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            engine = Engine(store)
            adapter = engine.amazon
            adapter.browser, adapter.driver = browser, driver
            adapter.agent = BrowserAgent(store, provider_factory=FakeProvider)
            original_context = adapter.context
            async def context(*args):
                result = await original_context(*args)
                await result.route('**/*', route)
                return result
            adapter.context = context
            try:
                await engine.start(task['id'])
                for _ in range(300):
                    current = store.get('tasks', task['id'])
                    if current['status'] in ('completed', 'error', 'review', 'attention'): break
                    await asyncio.sleep(.05)
                assert current['status'] == 'completed', current
                assert len(orders) == 1
                assert len(store.all('checkouts')) == 1
                # Inventory monitoring never invokes AI action recovery. The
                # account offer and cart still resolve their purchase controls.
                assert [r['action'] for r in store.all('agent_runs')] == ['ADD_TO_CART', 'ADD_TO_CART', 'BEGIN_CHECKOUT', 'SUBMIT_ORDER']
                assert cart_items == 1
                assert store.get('submissions', 'submission-' + task['id'])['status'] == 'confirmed'
            finally:
                await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_agent_settings_browser_flow(tmp_path, monkeypatch):
    import socket
    import threading
    import uvicorn

    async def fake_test(self): return {'ok': True, 'message': 'Fixture connection verified'}
    monkeypatch.setattr(AIProvider, 'test', fake_test)
    from retail import recovery_check
    async def fake_recovery(connection):
        return {'ok': True, 'message': 'Fixture browser recovery verified'}
    monkeypatch.setattr(recovery_check, 'check_recovery', fake_recovery)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(tmp_path), log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()

    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            for _ in range(100):
                if server.started: break
                await asyncio.sleep(.05)
            await page.goto(f'http://127.0.0.1:{port}')
            await page.locator('[data-view=settings]').click()
            await page.locator('[data-settings-tab=integrations]').click()
            await page.get_by_role('button', name='Add API connection', exact=True).click()
            await page.get_by_label('Connection name', exact=True).fill('Test OpenAI')
            await page.get_by_label('API key', exact=True).fill('UI-SECRET-KEY')
            await page.get_by_role('button', name='Save connection', exact=True).click()
            await page.locator('[data-ai-test]').click()
            await page.get_by_text('Fixture connection verified', exact=True).first.wait_for()
            await page.locator('[data-context-view=troubleshooting]').click()
            await page.locator('[data-ai-recovery]').click()
            await page.get_by_text('Fixture browser recovery verified', exact=True).first.wait_for()
            await page.locator('[data-view=settings]').last.click()
            await page.locator('[data-settings-tab=integrations]').click()
            await page.get_by_label('Connection', exact=True).select_option(label='Test OpenAI · gpt-6-sol')
            await page.get_by_label('When AI may assist', exact=True).select_option('agent')
            await page.get_by_role('button', name='Save changes', exact=True).click()
            from patchright.async_api import expect
            await expect(page.get_by_label('When AI may assist', exact=True)).to_have_value('agent')
            await page.locator('[data-settings-tab=browser]').click()
            await page.get_by_label('Browser connection', exact=True).select_option('true')
            await page.get_by_role('button', name='Save changes', exact=True).click()
            await expect(page.get_by_label('Browser connection', exact=True)).to_have_value('true')
            await page.reload()
            await page.locator('[data-view=settings]').click()
            await page.locator('[data-settings-tab=integrations]').click()
            await page.locator('[data-ai-edit]').click()
            assert await page.get_by_label('API key', exact=True).input_value() == ''
            assert not errors, errors
            await browser.close()
    try:
        asyncio.run(scenario())
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
