import asyncio

from fastapi.testclient import TestClient
from patchright.async_api import async_playwright, expect

from retail.amazon import Amazon
from retail.app import create_app
from retail.models import Account
from retail.store import Store


def test_manual_login_requests_visible_owner_without_changing_task_mode(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'show_browser_window': False}, 'settings')
        account = store.put('accounts', Account(name='Fixture', email='fixture@example.com').model_dump())
        driver = await async_playwright().start()
        actual_launch = driver.chromium.launch
        launches, exposures = [], []

        async def launch(**options):
            launches.append(options)
            # Check the requested desktop mode without opening test windows.
            return await actual_launch(**{**options, 'headless': True})

        async def visible(page, value):
            exposures.append((page, value))

        async def navigate(page, *args, **kwargs):
            await page.set_content('<h1>Sign in</h1><input type="email">')

        async def authenticate(*args):
            pass

        monkeypatch.setattr(driver.chromium, 'launch', launch)
        monkeypatch.setattr('retail.amazon.set_visible', visible)
        adapter = Amazon(store)
        adapter.driver = driver
        monkeypatch.setattr(adapter, 'navigate', navigate)
        monkeypatch.setattr(adapter, 'authenticate', authenticate)
        try:
            task_context = await adapter.context(account)
            task_page = await task_context.new_page()
            await task_page.set_content('<h1>Existing task</h1>')
            await adapter.login(account)
            login_context = adapter.logins[account['id']]
            assert [options['headless'] for options in launches] == [True, False]
            assert login_context.browser is not task_context.browser
            assert exposures == [(login_context.pages[0], True)]
            assert store.get('settings', 'settings')['show_browser_window'] is False
            assert adapter.browser_initially_visible is False
            assert adapter.browser_visible is False
            await adapter.login(account)
            assert len(launches) == 2  # Reopening focuses the same sign-in window.
            owner = login_context.browser
            await login_context.close()
            if adapter.profile_close_tasks:
                await asyncio.gather(*adapter.profile_close_tasks)
            assert not owner.is_connected()
            assert await task_page.locator('h1').inner_text() == 'Existing task'
        finally:
            await adapter.close()
            store.db.close()

    asyncio.run(scenario())


def test_account_open_has_no_take_control_dialog(tmp_path, monkeypatch):
    headers = {'X-Retail-Client': 'dashboard'}
    with TestClient(create_app(tmp_path), headers=headers) as client:
        account = client.post('/api/accounts', json={'name': 'Fixture', 'email': 'fixture@example.com'}).json()
        opened = []

        async def fake_login(value):
            opened.append(value['id'])

        monkeypatch.setattr(client.app.state.engine.amazon, 'login', fake_login)

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
                                              content=request.post_data, headers=headers)
                    await route.fulfill(status=response.status_code, body=response.content,
                                        headers={'content-type': response.headers.get('content-type', 'text/plain')})

                await page.route('**/*', local_route)
                await page.goto('http://127.0.0.1/')
                await page.locator('[data-view=accounts]').click()
                await page.locator(f'[data-account="{account["id"]}"][data-op=login]').click()
                await expect(page.locator('#toast')).to_contain_text('Complete sign-in in the browser window')
                assert opened == [account['id']]
                await expect(page.locator('dialog[open]')).to_have_count(0)
                await expect(page.locator('[data-take-control]')).to_have_count(0)
                assert not errors, errors
                await browser.close()

        asyncio.run(scenario())
