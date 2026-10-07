import asyncio

from fastapi.testclient import TestClient
from patchright.async_api import async_playwright, expect

from retail.amazon import Amazon
from retail.app import create_app
from retail.models import Account, account_fingerprint_settings
from retail.store import Store

HEADERS = {'X-Retail-Client': 'dashboard'}


def test_override_inheritance_validation_and_persistence(tmp_path):
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        client.post('/api/settings', json={'fingerprint_canvas': True})
        response = client.post('/api/accounts', json={'name': 'Test', 'fingerprint_overrides': {
            'fingerprint_canvas': False, 'fingerprint_screen': True}})
        assert response.status_code == 200, response.text
        account = response.json()
        assert account['fingerprint_overrides'] == {'fingerprint_canvas': False, 'fingerprint_screen': True}
        settings = client.app.state.store.get('settings', 'settings')
        effective = account_fingerprint_settings(settings, account)
        assert effective['fingerprint_canvas'] is False
        assert effective['fingerprint_screen'] is True
        assert settings['fingerprint_canvas'] is True
        client.post('/api/settings', json={'fingerprint_audio': True})
        effective = account_fingerprint_settings(client.app.state.store.get('settings', 'settings'), account)
        assert effective['fingerprint_audio'] is True
        assert effective['fingerprint_canvas'] is False
        for override in [{'fingerprint_backend': 'invalid'}, {'show_browser_window': False}]:
            assert client.put('/api/accounts/'+account['id'], json={'fingerprint_overrides': override}).status_code == 422
        assert client.post('/api/settings', json={'fingerprint_canvas': False, 'cdp_attach': True}).status_code == 422
        cleared = client.put('/api/accounts/'+account['id'], json={'fingerprint_overrides': {}}).json()
        assert cleared['fingerprint_overrides'] == {}
        assert account_fingerprint_settings(settings, cleared)['fingerprint_canvas'] is True
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        assert client.get('/api/state').json()['accounts'][0]['fingerprint_overrides'] == {}


def test_starting_websites_validation_crud_and_test_dispatch(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        defaults = client.get('/api/state').json()['settings'][0]['fingerprint_test_sites']
        assert [site['name'] for site in defaults] == ['CreepJS', 'Google']
        sites = [{'name': 'Local fixture', 'url': 'http://localhost:8766/'}]
        assert client.post('/api/settings', json={'fingerprint_test_sites': sites}).status_code == 200
        sites[0]['name'] = 'Edited'
        assert client.post('/api/settings', json={'fingerprint_test_sites': sites}).json()['fingerprint_test_sites'] == sites
        for url in ['javascript:alert(1)', 'file:///C:/secret', 'https://user:pass@example.com', 'https://', 'https://example.com:bad']:
            assert client.post('/api/settings', json={'fingerprint_test_sites': [{'name': 'Bad', 'url': url}]}).status_code == 422
        assert client.post('/api/settings', json={'fingerprint_test_sites': []}).json()['fingerprint_test_sites'] == []
        account = client.post('/api/accounts', json={'name': 'Test', 'retailer': 'walmart'}).json()
        calls = []

        async def launch(value):
            calls.append(value['id'])
            return {'ok': True, 'failed_sites': []}

        monkeypatch.setattr(client.app.state.engine.amazon, 'test_fingerprint', launch)
        assert client.post(f"/api/accounts/{account['id']}/test-fingerprint").status_code == 200
        assert calls == [account['id']]
        assert client.post(f"/api/accounts/{account['id']}/close-fingerprint-test").status_code == 200


def test_visible_test_browser_isolated_repeatable_and_cleaned_up(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'show_browser_window': False, 'fingerprint_canvas': True,
                              'fingerprint_test_sites': [{'name': 'One', 'url': 'https://one.test/'},
                                                         {'name': 'Two', 'url': 'https://two.test/'}]}, 'settings')
        account = store.put('accounts', {**Account(name='Test', fingerprint_overrides={
            'fingerprint_canvas': False, 'fingerprint_audio': True}).model_dump(),
            'session': {'cookies': [{'name': 'secret', 'value': 'saved-cookie', 'domain': 'one.test',
                                     'path': '/', 'expires': -1, 'httpOnly': False, 'secure': True, 'sameSite': 'Lax'}],
                        'origins': []}})
        adapter = Amazon(store)
        driver = await async_playwright().start()
        adapter.driver = driver
        actual_launch = driver.chromium.launch
        launches = []

        async def launch(**options):
            launches.append(options)
            browser = await actual_launch(**{**options, 'headless': True})
            actual_context = browser.new_context

            async def new_context(**kwargs):
                context = await actual_context(**kwargs)
                await context.route('**/*', lambda route: route.abort() if 'bad.test' in route.request.url
                                    else route.fulfill(body='<h1>Fixture</h1>', content_type='text/html'))
                return context

            monkeypatch.setattr(browser, 'new_context', new_context)
            return browser

        async def visible(*args):
            pass

        monkeypatch.setattr(driver.chromium, 'launch', launch)
        monkeypatch.setattr('retail.amazon.set_visible', visible)
        try:
            task = await adapter.context(account)
            assert (await task.cookies())[0]['value'] == 'saved-cookie'
            seed = adapter.profiles.get(account)['seed']
            result = await adapter.test_fingerprint(account)
            assert result == {'ok': True, 'failed_sites': []}
            context = adapter.fingerprint_tests[account['id']]
            assert [page.url for page in context.pages] == ['https://one.test/', 'https://two.test/']
            assert await context.cookies() == []
            assert launches[-1]['headless'] is False
            assert 'args' not in launches[-1]  # Explicit Off overrides global graphics/worker launch.
            assert context.browser is not task.browser
            assert adapter.profiles.get(account)['seed'] == seed
            assert store.get('settings', 'settings')['show_browser_window'] is False
            assert store.get('accounts', account['id'])['session'] == account['session']
            first_browser = context.browser
            await adapter.test_fingerprint(account)
            if adapter.profile_close_tasks:
                await asyncio.gather(*adapter.profile_close_tasks)
            assert not first_browser.is_connected()
            await adapter.close_fingerprint_test(account['id'])
            assert account['id'] not in adapter.fingerprint_tests
            assert task.browser.is_connected()
            store.put('settings', {'show_browser_window': False, 'fingerprint_test_sites': []}, 'settings')
            await adapter.test_fingerprint(account)
            blank = adapter.fingerprint_tests[account['id']]
            assert len(blank.pages) == 1 and blank.pages[0].url == 'about:blank'
            await blank.close()
            assert account['id'] not in adapter.fingerprint_tests
            store.put('settings', {'show_browser_window': False, 'fingerprint_test_sites': [
                {'name': 'Unavailable', 'url': 'https://bad.test/'},
                {'name': 'Available', 'url': 'https://one.test/'}]}, 'settings')
            assert (await adapter.test_fingerprint(account))['failed_sites'] == ['Unavailable']
            assert adapter.fingerprint_tests[account['id']].pages[1].url == 'https://one.test/'
        finally:
            await adapter.close()
            store.db.close()

    asyncio.run(scenario())


def test_mixed_implementations_use_the_correct_process(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'fingerprint_backend': 'native', 'show_browser_window': False}, 'settings')
        adapter = Amazon(store)
        native_launches = []

        def native_options(settings, seed=None):
            native_launches.append((settings, seed))
            return {'headless': True}

        monkeypatch.setattr('retail.amazon.native_launch_options', native_options)
        try:
            js_account = store.put('accounts', Account(name='JS', fingerprint_overrides={'fingerprint_backend': 'javascript'}).model_dump())
            native_account = store.put('accounts', Account(name='Native').model_dump())
            js = await adapter.context(js_account)
            assert not native_launches  # Does not require the globally selected native binary.
            native = await adapter.context(native_account)
            assert len(native_launches) == 1
            assert native.browser is not js.browser
            assert (await adapter.context(js_account)).browser is js.browser
        finally:
            await adapter.close()
            store.db.close()

    asyncio.run(scenario())


def test_account_fingerprint_and_website_editor_in_browser(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        account = client.post('/api/accounts', json={'name': 'Fixture', 'email': 'fixture@example.com'}).json()
        opened = []

        async def launch(value):
            opened.append(value)
            return {'ok': True, 'failed_sites': []}

        monkeypatch.setattr(client.app.state.engine.amazon, 'test_fingerprint', launch)

        async def scenario():
            async with async_playwright() as driver:
                browser = await driver.chromium.launch(headless=True)
                page = await browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))

                async def local_route(route):
                    from urllib.parse import urlsplit
                    request = route.request
                    url = urlsplit(request.url)
                    response = client.request(request.method, url.path + ('?' + url.query if url.query else ''),
                                              content=request.post_data, headers=HEADERS)
                    await route.fulfill(status=response.status_code, body=response.content,
                                        headers={'content-type': response.headers.get('content-type', 'text/plain')})

                await page.route('**/*', local_route)
                await page.goto('http://127.0.0.1/')
                await page.locator('[data-view=accounts]').click()
                await page.locator('[data-fingerprint-edit]').click()
                from pathlib import Path
                screenshots = Path('artifacts/account-fingerprints')
                screenshots.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshots / 'account-profile.png'))
                await page.locator('[name=account_fingerprint_backend]').select_option('native')
                await page.locator('[name=account_fingerprint_canvas]').select_option('true')
                await page.get_by_role('button', name='Save & test', exact=True).click()
                await expect(page.locator('dialog[open]')).to_have_count(0)
                assert opened[-1]['fingerprint_overrides']['fingerprint_backend'] == 'native'
                assert opened[-1]['fingerprint_overrides']['fingerprint_canvas'] is True
                await page.locator('[data-fingerprint-edit]').click()
                await expect(page.locator('[name=account_fingerprint_canvas]')).to_have_value('true')
                await page.locator('[data-reset-fingerprint]').click()
                await page.get_by_role('button', name='Save', exact=True).click()
                await expect(page.locator('dialog[open]')).to_have_count(0)
                await page.locator('[data-fingerprint-sites]').click()
                await expect(page.locator('.fingerprint-site-row')).to_have_count(2)
                await page.locator('[data-site-name]').first.fill('Edited CreepJS')
                await page.locator('.fingerprint-site-row').last.get_by_role('button', name='Delete website').click()
                await page.locator('[data-add-site]').click()
                await page.locator('[data-site-name]').last.fill('Example')
                await page.locator('[data-site-url]').last.fill('https://example.com/')
                await page.get_by_role('button', name='Save websites').click()
                await expect(page.locator('dialog[open]')).to_have_count(0)
                await page.locator('[data-fingerprint-sites]').click()
                await expect(page.locator('[data-site-name]').first).to_have_value('Edited CreepJS')
                await expect(page.locator('[data-site-url]').last).to_have_value('https://example.com/')
                await page.set_viewport_size({'width': 390, 'height': 844})
                await page.screenshot(path=str(screenshots / 'test-websites-mobile.png'))
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await page.get_by_role('button', name='Cancel', exact=True).click()
                await page.set_viewport_size({'width': 1280, 'height': 720})
                await page.locator('[data-action=edit][data-kind=accounts]').click()
                await page.locator('.account-fingerprint-fields summary').click()
                await page.locator('[name=account_fingerprint_audio]').select_option('false')
                await page.get_by_role('button', name='Save account', exact=True).click()
                await expect(page.locator('#modal')).not_to_be_visible()
                saved = client.get('/api/state').json()['accounts'][0]
                assert saved['fingerprint_overrides'] == {'fingerprint_audio': False}
                assert not errors, errors
                await browser.close()

        asyncio.run(scenario())
