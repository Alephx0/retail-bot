import asyncio

from patchright.async_api import async_playwright

from retail.amazon import Amazon
from retail.models import Account
from retail.store import Store


def test_automatic_login_and_encrypted_session_capture(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        account = store.put('accounts',Account(name='Fixture',email='fixture@example.com',password='fixture-password').model_dump())
        adapter = Amazon(store)
        requests = []
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            async def route_handler(route):
                request = route.request
                requests.append((request.url,request.post_data))
                if '/password' in request.url:
                    html='<body><form action="/verified" method="post"><input id="ap_password" name="password" type="password"><button id="signInSubmit">Sign in</button></form></body>'
                elif '/verified' in request.url:
                    html='<body><div id="nav-link-accountList"><span class="nav-line-1">Hello, Fixture</span></div>Your Orders</body>'
                else:
                    html='<body><form action="/password" method="post"><input id="ap_email" name="email"><button id="continue">Continue</button></form></body>'
                await route.fulfill(body=html,content_type='text/html')
            await page.route('https://www.amazon.com/**',route_handler)
            adapter.context_accounts[context] = account
            await adapter.ensure_session(context,account,page)
            saved = store.get('accounts',account['id'])
            assert saved['logged_in']
            assert 'session_saved_at' in saved
            assert 'cookies' in saved['session']
            assert any(body and 'fixture-password' in body for _,body in requests)
            assert store.all('events') == []
            await page.set_content('<body>Order placed 123-1234567-1234567. Payment verification required.</body>')
            assert await adapter.payment_verification(page)
            assert await adapter.confirmation(page) == '123-1234567-1234567'
            await page.goto('https://www.amazon.com/gp/buy/thankyou/handlers/display.html')
            await page.set_content('<h4>Order placed, thanks!</h4><a href="/dp/B012345678">Fixture product 1</a>')
            assert await adapter.confirmation(page) is None
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_manual_login_is_saved_automatically_after_verification(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        account = store.put('accounts', Account(name='Fixture', email='fixture@example.com').model_dump())
        adapter = Amazon(store)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            context = await browser.new_context()
            await context.add_cookies([{'name': 'fixture_login', 'value': 'signed-in', 'url': 'https://www.amazon.com/'}])
            page = await context.new_page()
            authenticated = {'value': False}
            async def route_handler(route):
                label = 'Hello, Fixture' if authenticated['value'] else 'Sign in'
                await route.fulfill(body=f'<div id="nav-link-accountList"><span class="nav-line-1">{label}</span></div>',
                                    content_type='text/html')
            await page.route('https://www.amazon.com/**', route_handler)
            await page.goto('https://www.amazon.com/gp/your-account/order-history')
            adapter.logins[account['id']] = context
            watcher = asyncio.create_task(adapter.watch_login(account['id'], context, page))
            adapter.login_watchers[account['id']] = watcher
            authenticated['value'] = True
            await page.set_content('<div id="nav-link-accountList"><span class="nav-line-1">Hello, Fixture</span></div>')
            for _ in range(30):
                if store.get('accounts', account['id']).get('logged_in'):
                    break
                await asyncio.sleep(.1)
            saved = store.get('accounts', account['id'])
            assert saved['logged_in']
            assert any(c['name'] == 'fixture_login' for c in saved['session']['cookies'])
            # Saving login state precedes asynchronous context cleanup. Await
            # the lifecycle operation instead of racing its close() call.
            await asyncio.wait_for(watcher, 5)
            assert account['id'] not in adapter.logins
            await browser.close()
        store.db.close()

    asyncio.run(scenario())


def test_product_start_continues_shopping_then_signs_in_and_saves_session(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        account = store.put('accounts', Account(name='Fixture', email='fixture@example.com', password='fixture-password').model_dump())
        adapter = Amazon(store)
        state = {'continued': False, 'signed_in': False}
        paths = []
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            async def route_handler(route):
                from urllib.parse import urlparse
                path = urlparse(route.request.url).path
                paths.append(path)
                if path == '/continue-shopping':
                    state['continued'] = True
                if path == '/verified':
                    state['signed_in'] = True
                if not state['continued']:
                    html = '<body>Click the button below to continue shopping<form action="/continue-shopping" method="post"><button>Continue shopping</button></form></body>'
                elif path == '/ap/signin':
                    html = '<form action="/password" method="post"><input id="ap_email" name="email"><button id="continue">Continue</button></form>'
                elif path == '/password':
                    html = '<form action="/verified" method="post"><input id="ap_password" name="password" type="password"><button id="signInSubmit">Sign in</button></form>'
                else:
                    label = 'Hello, Fixture' if state['signed_in'] else 'Hello, sign in'
                    html = f'<a href="/ap/signin" id="nav-link-accountList"><span class="nav-line-1">{label}</span></a><div id="availability">In Stock</div>'
                await route.fulfill(body=html, content_type='text/html')
            await page.route('**/*', route_handler)
            page._retail_start_url = 'https://www.amazon.com/dp/B012345678'
            adapter.context_accounts[context] = account
            await adapter.ensure_session(context, account, page)
            assert state == {'continued': True, 'signed_in': True}
            assert paths.count('/continue-shopping') == 1
            assert '/ap/signin' in paths and '/password' in paths and '/verified' in paths
            assert page.url == page._retail_start_url
            saved = store.get('accounts', account['id'])
            assert saved['logged_in'] and saved['session_saved_at']
            assert 'cookies' in saved['session']
            await browser.close()
        store.db.close()
    asyncio.run(scenario())
