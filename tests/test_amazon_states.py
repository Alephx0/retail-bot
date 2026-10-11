"""Local retailer fixtures only: no live orders or account input."""
import asyncio
import pytest
from patchright.async_api import async_playwright

from retail.amazon import Amazon, Attention, ChallengeDetected


async def no_fixed_wait(*args, **kwargs):
    raise AssertionError('Amazon action readiness must not use a fixed sleep')


@pytest.mark.parametrize('delay', [0, 1700])
def test_cart_waits_for_acknowledgement_and_hydrated_quantity(delay):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            page.wait_for_timeout = no_fixed_wait
            page.set_default_timeout(5000)
            visits = 0
            async def local(route):
                nonlocal visits
                if '/gp/cart/' in route.request.url:
                    visits += 1
                    # The single cart visit initially has no rows while hydrating.
                    body = '''<div id="sc-active-cart"></div><script>
                    setTimeout(() => document.querySelector('#sc-active-cart').innerHTML =
                        '<div data-asin="B012345678" data-quantity="1">Fixture</div>', 75);
                    </script>'''
                else:
                    body = f'''<span id="nav-cart-count">0</span><button id="add-to-cart-button"
                        onclick="window.clicks=(window.clicks||0)+1;setTimeout(()=>{{document.querySelector('#nav-cart-count').textContent='1';window.ack=true;}}, {delay})">Add to cart</button>'''
                await route.fulfill(body=body, content_type='text/html')
            await page.route('**/*', local)
            await page.goto('https://www.amazon.com/dp/B012345678')
            adapter = Amazon(None)
            navigate = adapter.navigate
            async def checked_navigation(page, url, **kwargs):
                if '/gp/cart/' in url:
                    assert await page.evaluate('window.ack', isolated_context=False)
                    assert await page.evaluate('window.clicks', isolated_context=False) == 1
                return await navigate(page, url, **kwargs)
            adapter.navigate = checked_navigation
            assert await adapter.cart(page, 1, 'B012345678') == 1
            assert visits == 1
            await browser.close()
    asyncio.run(scenario())


def test_cart_quantity_waits_for_committed_row_not_optimistic_select():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            page.wait_for_timeout = no_fixed_wait
            async def local(route):
                await route.fulfill(content_type='text/html', body='''<div id="sc-active-cart">
                <div data-asin="B012345678" data-quantity="2"><select name="quantity"
                onchange="setTimeout(()=>this.parentElement.dataset.quantity=this.value,650)">
                <option value="1">1</option><option selected value="2">2</option></select></div></div>''')
            await page.route('**/*', local)
            await page.goto('https://www.amazon.com/gp/cart/view.html')
            assert await Amazon(None).cart(page, 1, 'B012345678') == 1
            assert await page.locator('[data-asin]').get_attribute('data-quantity') == '1'
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('stall', [False, True])
def test_same_url_checkout_does_not_reclick_while_transition_pending(stall):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            page.wait_for_timeout = no_fixed_wait
            page.set_default_timeout(300 if stall else 4000)
            action = '' if stall else "setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<input id=placeOrder name=placeYourOrder1 value=Submit>'),650)"
            html = f'<button onclick="window.clicks=(window.clicks||0)+1;{action}">Continue to checkout</button>'
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body=html))
            await page.goto('https://www.amazon.com/checkout/byg')
            adapter = Amazon(None)
            if stall:
                with pytest.raises(Attention, match='no action was repeated'):
                    await adapter.advance_checkout(page)
            else:
                await adapter.advance_checkout(page)
                assert await page.locator('#placeOrder').count() == 1
            assert await page.evaluate('window.clicks', isolated_context=False) == 1
            await browser.close()
    asyncio.run(scenario())


REVIEW = '''<div id="spc-orders"><div data-asin="B012345678" data-quantity="1"
    data-seller="Amazon.com" data-condition="new"><span class="a-price"><span class="a-offscreen">$20.00</span></span>
    </div></div><table id="subtotals-marketplace-table"><tr><td>Order total:</td><td>$20.00</td></tr></table>'''


@pytest.mark.parametrize('outcome', ['delayed_id', 'payment', 'stalled'])
def test_order_submission_waits_for_result_and_never_resubmits(outcome):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            page.wait_for_timeout = no_fixed_wait
            page.set_default_timeout(300 if outcome == 'stalled' else 4000)
            action = {
                'delayed_id': "document.body.insertAdjacentHTML('beforeend','<h1>Order placed</h1>');setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<p>123-1234567-1234567</p>'),2000)",
                'payment': "document.body.insertAdjacentHTML('beforeend','<input name=cvv><p>Verify your card</p>')",
                'stalled': '',
            }[outcome]
            body = REVIEW + f'<button onclick="window.clicks=(window.clicks||0)+1;{action}">Place your order</button>'
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body=body))
            await page.goto('https://www.amazon.com/checkout/spc')
            adapter = Amazon(None)
            await adapter.checkout_snapshot(page, 'B012345678', 1, 25)
            if outcome == 'stalled':
                with pytest.raises(Attention, match='submission state'):
                    await adapter.submit_order(page)
            else:
                await adapter.submit_order(page)
            assert await adapter.confirmation(page) == ('123-1234567-1234567' if outcome == 'delayed_id' else None)
            with pytest.raises(Attention, match='expired'):
                await adapter.submit_order(page)
            assert await page.evaluate('window.clicks', isolated_context=False) == 1
            await browser.close()
    asyncio.run(scenario())


def test_shipping_and_cvv_wait_for_results_not_selected_inputs():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            page.wait_for_timeout = no_fixed_wait
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body='<body></body>'))
            await page.goto('https://www.amazon.com/checkout/spc')
            adapter = Amazon(None)
            await page.set_content('''<label>FREE shipping<input type="radio"
                onchange="setTimeout(()=>document.querySelector('#charge').textContent='$0.00',900)"></label>
                <table id="subtotals-marketplace-table"><tr><td>Shipping</td><td id="charge">$5.00</td></tr></table>''')
            await adapter.free_shipping(page)
            assert await page.locator('#charge').inner_text() == '$0.00'
            await page.set_content('''<h1>Order placed 123-1234567-1234567</h1>
                <form onsubmit="event.preventDefault();window.verifications=(window.verifications||0)+1;setTimeout(()=>this.remove(),1000)">
                <input name="cvv"><button>Verify card</button></form>''')
            assert await adapter.verify_cvv(page, '123')
            assert await page.locator('input[name=cvv]').count() == 0
            assert await page.evaluate('window.verifications', isolated_context=False) == 1
            await browser.close()
    asyncio.run(scenario())


def test_state_waits_cancel_and_stop_on_challenges_and_unknown_confirmation():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body='<body>Loading</body>'))
            await page.goto('https://www.amazon.com/checkout/spc')
            adapter = Amazon(None)
            task = asyncio.create_task(adapter.wait_state(page, 'checkout'))
            await asyncio.sleep(.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await page.set_content('<input id="captchacharacters">Robot check')
            with pytest.raises(ChallengeDetected):
                await adapter.wait_state(page, 'cart', asin='B012345678', quantity=1, before={'count': '0', 'added': ''})
            await page.set_content('''<p>Click the button below to continue shopping</p><button
                onclick="document.body.innerHTML='<input id=placeOrder name=placeYourOrder1 value=Submit>'">Continue shopping</button>''')
            assert (await adapter.wait_state(page, 'checkout'))['kind'] == 'review'
            page.set_default_timeout(120)
            for markup in ('<h1>Order placed</h1>', '<p>Previous order 123-1234567-1234567</p>', '<p>Loading</p>'):
                await page.set_content(markup)
                assert await adapter.confirmation(page) is None
            await browser.close()
    asyncio.run(scenario())


def test_login_watcher_waits_for_new_state_after_failed_verification(tmp_path):
    from retail.store import Store
    from retail.amazon import AuthenticationRequired

    async def scenario():
        store = Store(tmp_path)
        account = store.put('accounts', {'name': 'Fixture', 'region': 'US'})
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            context = await browser.new_context()
            page = await context.new_page()
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body='<div id="nav-link-accountList"><span class="nav-line-1">Hello, Fixture</span></div>'))
            await page.goto('https://www.amazon.com/account')
            adapter = Amazon(store)
            adapter.logins[account['id']] = context
            attempts = []
            async def verify(*args):
                attempts.append(page.url)
                if len(attempts) == 1:
                    raise AuthenticationRequired('Session needs attention')
            adapter.ensure_session = verify
            watcher = asyncio.create_task(adapter.watch_login(account['id'], context, page))
            await asyncio.sleep(.15)
            assert len(attempts) == 1
            await page.goto('https://www.amazon.com/account/verified')
            await asyncio.wait_for(watcher, 3)
            assert len(attempts) == 2
            assert account['id'] not in adapter.logins
            await browser.close()
        store.db.close()
    asyncio.run(scenario())
