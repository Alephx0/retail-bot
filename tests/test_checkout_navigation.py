import asyncio
import json
import re

import pytest
from patchright.async_api import async_playwright

from retail.amazon import Amazon, CartRejected
from retail.interactions import InteractionError
from retail.browser_agent import BrowserAgent
from retail.browser_mcp import AMAZON_ACTIONS, BrowserTools
from retail.models import AIConnection
from retail.store import Store


class NavigationProvider:
    def __init__(self, connection):
        self.observed = None

    async def turn(self, instructions, history, tools):
        if any(t['name'] == 'observe_price_rows' for t in tools):
            if self.observed is None:
                return [{'id': 'observe', 'name': 'observe_price_rows', 'arguments': '{}'}]
            chosen = next(c for c in self.observed['price_rows'] if c['label'] == 'Grand total')
            return [{'id': 'choose', 'name': 'validate_total', 'arguments': json.dumps({'ref': chosen['ref']})}]
        if self.observed is None:
            return [{'id': 'observe', 'name': 'observe_controls', 'arguments': '{}'}]
        pattern = AMAZON_ACTIONS[self.observed['expected_action']]
        chosen = next(c for c in self.observed['controls'] if re.fullmatch(pattern, c['label'], re.I))
        return [{'id': 'choose', 'name': 'validate_control', 'arguments': json.dumps({'ref': chosen['ref']})}]

    def tool_result(self, history, call, result):
        if call['name'] in ('observe_controls', 'observe_price_rows'):
            self.observed = json.loads(result[0])


def test_user_browser_recovery_check():
    from retail.recovery_check import check_recovery
    result = asyncio.run(check_recovery({'id': 'fixture', 'model': 'fixture'}, NavigationProvider))
    assert result['ok']
    assert len(result['scenarios']) == 7
    assert all(s['ok'] for s in result['scenarios'])


@pytest.mark.parametrize('modal', [False, True])
def test_byg_link_and_dialog_recover_without_model(tmp_path, modal):
    async def scenario():
        store = Store(tmp_path)
        connection = store.put('ai_connections', AIConnection(name='fixture').model_dump())
        store.put('settings', {'agent_mode': 'recovery', 'ai_connection_id': connection['id']}, 'settings')
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            requests = []

            async def route(r):
                requests.append(r.request.url)
                if '/checkout/byg' in r.request.url:
                    body = '<a href="/checkout/review">Continue to checkout</a>' + '<input type="submit" name="submit.addToCart" value="Add" onclick="window.unwanted=true">' * 30
                    if modal:
                        body += '<dialog open style="position:fixed;inset:0"><a href="#" onclick="event.preventDefault();this.parentElement.remove()">No, thanks</a><button onclick="window.unwanted=true">Add</button></dialog>'
                else:
                    body = '<div id="spc-orders"><div data-asin="B012345678">Review</div></div><button onclick="window.unwanted=true">Place your order</button>'
                await r.fulfill(body=body, content_type='text/html')

            await page.route('**/*', route)
            await page.goto('https://www.amazon.com/checkout/byg')
            adapter = Amazon(store)
            adapter.agent = BrowserAgent(store, provider_factory=NavigationProvider)
            await adapter.advance_checkout(page)
            assert page.url.endswith('/checkout/review')
            assert not await page.evaluate('Boolean(window.unwanted)', isolated_context=False)
            runs = store.all('agent_runs')
            assert not runs, 'A safe semantic dismissal should not require a model'
            assert all(r['status'] == 'validated' for r in runs)
            assert len(requests) == 2
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_continuation_rejects_external_and_purchase_controls():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(body='<body></body>', content_type='text/html'))
            await page.goto('https://www.amazon.com/checkout/byg')
            tools = BrowserTools(page, 'CONTINUE_CHECKOUT', {'www.amazon.com'}, AMAZON_ACTIONS)
            for markup in ['<a href="https://evil.test/checkout">Continue to checkout</a>', '<button>Add</button>', '<button>Place your order</button>', '<a href="javascript:void(0)">Continue to checkout</a>']:
                await page.set_content(markup)
                await tools.observe_controls()
                assert not (await tools.validate_control('1'))['validated']
            await browser.close()
    asyncio.run(scenario())


def test_amazon_bypass_uses_checkout_link_without_model(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(body='<body></body>', content_type='text/html'))
            await page.goto('https://www.amazon.com/checkout/byg')
            await page.set_content('<a href="/checkout/review">Continue to checkout</a><input name="submit.addToCart" value="Add"><a href="/checkout/review">Continue to checkout</a>')
            adapter = Amazon(store)
            control = await adapter.resolve_action(page, 'CONTINUE_CHECKOUT')
            assert await control.get_attribute('href') == '/checkout/review'
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_different_checkout_links_require_review(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        connection = store.put('ai_connections', AIConnection(name='fixture').model_dump())
        store.put('settings', {'agent_mode': 'recovery', 'ai_connection_id': connection['id']}, 'settings')
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(body='<body></body>', content_type='text/html'))
            await page.goto('https://www.amazon.com/checkout/byg')
            await page.set_content('<a href="/checkout/entry">Continue to checkout</a><a href="/checkout/review">Continue to checkout</a><button>Add</button>')
            adapter = Amazon(store)
            adapter.agent = BrowserAgent(store, provider_factory=NavigationProvider)
            with pytest.raises(InteractionError, match='multiple'):
                await adapter.resolve_action(page, 'CONTINUE_CHECKOUT')
            assert not store.all('agent_runs'), 'A model may not choose between conflicting destinations'
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('signed_out', [False, True])
def test_product_session_reuses_first_navigation(tmp_path, signed_out):
    async def scenario():
        store = Store(tmp_path)
        account = store.put('accounts', {'region': 'US', 'name': 'Fixture'})
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            context = await browser.new_context()
            page = await context.new_page()
            requests = []
            authenticated = not signed_out

            async def route(r):
                requests.append(r.request.url)
                label = 'Hello Fixture' if authenticated else 'Hello, sign in'
                await r.fulfill(body=f'<a href="/ap/signin" id="nav-link-accountList"><span class="nav-line-1">{label}</span></a><span id="productTitle">Fixture</span><button>Add to cart</button>', content_type='text/html')

            await page.route('**/*', route)
            page._retail_start_url = 'https://www.amazon.com/dp/B012345678'
            adapter = Amazon(store)
            if signed_out:
                async def authenticate(page, account):
                    nonlocal authenticated
                    assert '/ap/signin' in page.url
                    authenticated = True
                    await page.goto(page._retail_start_url)
                adapter.authenticate = authenticate
            await adapter.ensure_session(context, account, page)
            await adapter.inspect(page, {'asin': 'B012345678', 'offer_id': ''}, 'US')
            assert requests == ([page._retail_start_url, 'https://www.amazon.com/ap/signin', page._retail_start_url] if signed_out else [page._retail_start_url])
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('cart_html,expected', [
    ('<div data-asin="B012345678" data-quantity="1">Target</div>', 'existing'),
    ('<div data-asin="B012345678" data-quantity="1">Target</div><div data-asin="B000000001" data-quantity="1">Other</div>', 'other'),
    ('<div data-asin="B012345678" data-quantity="2">Target</div>', 'quantity'),
])
def test_cart_preflight_preserves_existing_items(tmp_path, cart_html, expected):
    async def scenario():
        store = Store(tmp_path)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            async def route(r):
                if '/gp/cart/' in r.request.url:
                    await r.fulfill(body='<div id="sc-active-cart">' + cart_html + '</div>')
                else:
                    await r.fulfill(body='<button onclick="window.added=true">Add to cart</button>')
            await page.route('**/*', route)
            await page.goto('https://www.amazon.com/dp/B012345678')
            adapter = Amazon(store)
            if expected == 'existing':
                assert await adapter.cart(page, 1, 'B012345678') == 1
            else:
                with pytest.raises(CartRejected, match='Save for Later|different quantity'):
                    await adapter.cart(page, 1, 'B012345678')
            assert page.url.endswith('/gp/cart/view.html')
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_modern_final_review_is_not_treated_as_checkout_continuation(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body='<input id="placeOrder" name="placeYourOrder1" type="submit" value="Place your order"><a href="/checkout/address">Continue to checkout</a>'))
            await page.goto('https://www.amazon.com/checkout/p/example/spc')
            assert await page.locator('#placeOrder').count() == 1, (page.url, (await page.locator('body').inner_text())[:200])
            assert await page.locator('#placeOrder:visible').count() == 1
            adapter = Amazon(store)
            await adapter.advance_checkout(page)
            assert page.url.endswith('/spc')
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_existing_target_quantity_is_normalized_without_adding(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            async def route(r):
                if '/gp/cart/' in r.request.url:
                    body = '<div id="sc-active-cart"><div data-asin="B012345678" data-quantity="2"><button onclick="this.parentElement.dataset.quantity=1">Decrease item quantity</button></div></div>'
                else:
                    body = '<button onclick="window.added=true">Add to cart</button>'
                await r.fulfill(body=body, content_type='text/html')
            await page.route('**/*', route)
            await page.goto('https://www.amazon.com/dp/B012345678')
            adapter = Amazon(store)
            assert await adapter.cart(page, 1, 'B012345678') == 1
            assert (await adapter.get_cart(page)) == [{'asin': 'B012345678', 'quantity': 1}]
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_unrelated_cart_item_is_saved_and_target_is_not_added_twice(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            async def route(r):
                if '/gp/cart/' in r.request.url:
                    body = '''<div id="sc-active-cart">
                    <div data-asin="B012345678" data-quantity="1">Target</div>
                    <div data-asin="B000000001" data-quantity="1">Other
                      <input type="button" name="submit.save-for-later.abc" value="Save for later"
                        onclick="document.querySelector('#sc-saved-cart').append(this.parentElement)">
                    </div></div><div id="sc-saved-cart"></div>'''
                else:
                    body = '<button onclick="window.added=true">Add to cart</button>'
                await r.fulfill(body=body, content_type='text/html')
            await page.route('**/*', route)
            await page.goto('https://www.amazon.com/dp/B012345678')
            adapter = Amazon(store)
            assert await adapter.cart(page, 1, 'B012345678') == 1
            assert await adapter.get_cart(page) == [{'asin': 'B012345678', 'quantity': 1}]
            assert await page.locator('#sc-saved-cart [data-asin="B000000001"]').count() == 1
            assert not await page.evaluate('window.added || false', isolated_context=False)
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_saved_items_are_not_mistaken_for_active_cart_items(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body='''
                <div id="sc-active-cart"></div>
                <div id="sc-saved-cart"><div data-asin="B000000001" data-quantity="1">Saved item</div></div>'''))
            await page.goto('https://www.amazon.com/gp/cart/view.html')
            assert await Amazon(store).get_cart(page) == []
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_save_for_later_verifies_server_side_change_after_reload(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        saved = {'value': False}
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            async def route(r):
                if r.request.url.endswith('/save'):
                    saved['value'] = True
                    await r.fulfill(body='ok')
                else:
                    row = '<div data-asin="B000000001" data-quantity="1">Other<input type="button" name="submit.save-for-later.abc" value="Save for later" onclick="fetch(\'/save\')"></div>'
                    body = ('<div id="sc-active-cart"></div><div id="sc-saved-cart">' + row + '</div>') if saved['value'] else ('<div id="sc-active-cart">' + row + '</div><div id="sc-saved-cart"></div>')
                    await r.fulfill(body=body, content_type='text/html')
            await page.route('**/*', route)
            await page.goto('https://www.amazon.com/gp/cart/view.html')
            adapter = Amazon(store)
            await adapter.save_unrelated_cart_items(page, 'B012345678')
            assert saved['value']
            assert await adapter.get_cart(page) == []
            assert await page.locator('#sc-saved-cart [data-asin="B000000001"]').count() == 1
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_duplicate_identical_order_controls_are_one_semantic_action():
    async def scenario():
        from retail.interactions import resolve
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.set_content('<input name="placeYourOrder1" type="submit" value="Place your order"><input name="placeYourOrder1" type="submit" value="Place your order">')
            control = await resolve(page, 'SUBMIT_ORDER')
            assert await control.count() == 1
            await browser.close()
    asyncio.run(scenario())


def test_modern_review_accepts_only_complete_item_and_total_evidence():
    async def scenario():
        from retail.amazon import Attention
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body='<div data-asin="B012345678" data-quantity="1" data-seller="Amazon.com" data-condition="new"><span class="a-price"><span class="a-offscreen">$20.00</span></span></div><ul><li>Order total: $21.20</li></ul><input name="placeYourOrder1" type="submit" value="Place your order"><input name="placeYourOrder1" type="submit" value="Place your order">'))
            await page.goto('https://www.amazon.com/checkout/p/example/spc')
            adapter = Amazon(None)
            snapshot = await adapter.checkout_snapshot(page, 'B012345678', 1, 25, max_unit_price=20)
            assert snapshot['total'] == 21.2
            with pytest.raises(Attention, match='quantity'):
                await adapter.checkout_snapshot(page, 'B012345678', 2, 25, max_unit_price=20)
            with pytest.raises(Attention, match='budget'):
                await adapter.checkout_snapshot(page, 'B012345678', 1, 20, max_unit_price=20)
            await browser.close()
    asyncio.run(scenario())


def test_buy_now_uses_product_control_and_reaches_review(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        connection = store.put('ai_connections', AIConnection(name='fixture').model_dump())
        store.put('settings', {'agent_mode': 'recovery', 'ai_connection_id': connection['id']}, 'settings')
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            async def route(r):
                if '/dp/' in r.request.url:
                    body = '<span id="productTitle">Fixture product</span><select id="quantity"><option value="1">1</option></select><button onclick="location.href=\'/checkout\'">Buy Now</button>'
                else:
                    body = '<div id="spc-orders"><div data-asin="B012345678" data-quantity="1" data-seller="Amazon.com" data-condition="new"><span class="a-price"><span class="a-offscreen">$20.00</span></span></div></div><table id="subtotals-marketplace-table"><tr><td>Order total:</td><td>$20.00</td></tr></table><button>Place your order</button>'
                await r.fulfill(body=body, content_type='text/html')
            await page.route('**/*', route)
            await page.goto('https://www.amazon.com/dp/B012345678')
            adapter = Amazon(store)
            assert await adapter.buy_now(page, 1, 'B012345678') is True
            assert '/checkout' in page.url
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_buy_now_unavailable_can_fall_back_to_cart(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body='<span id="productTitle">Fixture product</span><button>Add to cart</button>'))
            await page.goto('https://www.amazon.com/dp/B012345678')
            assert await Amazon(store).buy_now(page, 1, 'B012345678') is False
            assert page.url.endswith('/dp/B012345678')
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_amazon_spc_review_without_asin_uses_verified_cart_identity():
    async def scenario():
        from retail.amazon import Attention
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            title = 'Banana Bunch (4-5 Count)'
            body = '<ul><li>Items (1): $0.99</li><li>Order total: $3.98</li></ul>'
            body += f'<div>{title} 100K+ bought in past month $0.99<br>Ships from and sold by<br>Amazon.com</div>'
            body += f'<div role="group" aria-label="Change quantity of {title}">1 1</div>'
            body += '<input name="placeYourOrder1" type="submit" value="Place your order"><input name="placeYourOrder1" type="submit" value="Place your order">'
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body=body))
            await page.goto('https://www.amazon.com/checkout/p/example/spc')
            page._retail_cart_asin = 'B012345678'
            page._retail_cart_title = title
            page._retail_product_condition = 'new'
            adapter = Amazon(None)
            snapshot = await adapter.checkout_snapshot(page, 'B012345678', 1, 5, max_unit_price=1)
            assert snapshot['total'] == 3.98
            with pytest.raises(Attention, match='quantity|item count'):
                await adapter.checkout_snapshot(page, 'B012345678', 2, 5, max_unit_price=1)
            with pytest.raises(Attention, match='budget'):
                await adapter.checkout_snapshot(page, 'B012345678', 1, 3, max_unit_price=1)
            page._retail_cart_title = 'Different product'
            with pytest.raises(Attention, match='product contents'):
                await adapter.checkout_snapshot(page, 'B012345678', 1, 5, max_unit_price=1)
            await browser.close()
    asyncio.run(scenario())


def test_amazon_spc_review_allows_items_summary_without_count():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            title = 'Banana Bunch (4-5 Count)'
            body = '<ul><li>Items: $0.99</li><li>Shipping & handling: $2.99</li><li>Estimated tax to be collected: $0.00</li><li>Order total: $3.98</li></ul>'
            body += f'<div>{title} $0.99<br>Ships from and sold by<br>Amazon.com</div>'
            body += f'<div role="group" aria-label="Change quantity of {title}">1 1</div>'
            body += '<input name="placeYourOrder1" type="submit" value="Place your order">'
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body=body))
            await page.goto('https://www.amazon.com/checkout/p/example/spc')
            page._retail_cart_asin = 'B012345678'
            page._retail_cart_title = title
            page._retail_product_condition = 'new'
            snapshot = await Amazon(None).checkout_snapshot(page, 'B012345678', 1, 5, max_unit_price=1)
            assert snapshot['total'] == 3.98
            assert snapshot['unit_price'] == 0.99
            assert snapshot['price_components'] == [
                {'label': 'Items', 'amount': 0.99},
                {'label': 'Shipping & handling', 'amount': 2.99},
                {'label': 'Estimated tax to be collected', 'amount': 0.0},
            ]
            await page.set_content(body.replace('<li>Order total: $3.98</li>', '<li>Promotion applied: -$0.50</li><li>Order total: $3.48</li>'))
            discounted = await Amazon(None).checkout_snapshot(page, 'B012345678', 1, 5, max_unit_price=1)
            assert discounted['total'] == 3.48
            assert {'label': 'Promotion applied', 'amount': -0.50} in discounted['price_components']
            await browser.close()
    asyncio.run(scenario())
