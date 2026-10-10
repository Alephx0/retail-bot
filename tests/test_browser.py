import asyncio
import pytest

from patchright.async_api import async_playwright

from retail.amazon import Amazon, Attention, ChallengeDetected, BackoffRequired, AccessDenied


def test_amazon_browser_adapter_with_fixtures():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            page = await browser.new_page()
            html = '''<body><span id="productTitle">Fixture product</span>
              <div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$24.99</span></span></div>
              <div id="merchant-info">Ships from Amazon. Sold by Example Store.</div>
              <input name="offerListingID" value="fixture-offer">
              <select id="quantity"><option value="1">1</option><option value="2">2</option></select>
              <button id="add-to-cart-button">Add to cart</button></body>'''
            async def route_handler(route):
                if '/gp/cart/' in route.request.url:
                    await route.fulfill(body='<body><div data-asin="B012345678" data-quantity="2">Fixture product</div></body>', content_type='text/html')
                else:
                    await route.fulfill(body=html, content_type='text/html')
            await page.route('https://www.amazon.com/**', route_handler)
            adapter = Amazon(None)
            product = await adapter.inspect(page, {'asin':'B012345678'}, 'US')
            assert product['price'] == 24.99
            assert product['offer_id'] == 'fixture-offer'
            assert not product['amazon_seller'], 'Fulfilled by Amazon is not sold by Amazon'
            assert product['available']
            with pytest.raises(Attention,match='different quantity'):
                await adapter.cart(page,5,'B012345678')
            assert await adapter.cart(page,2,'B012345678') == 2
            await page.set_content('<body><input id="captchacharacters"></body>')
            with pytest.raises(ChallengeDetected, match='No automated solve was attempted'):
                await adapter.check(page)
            await page.set_content('<body>Click the button below to continue shopping<button>Continue shopping</button></body>')
            with pytest.raises(ChallengeDetected,match='Continue shopping confirmation'):
                await adapter.check(page)
            await browser.close()
    asyncio.run(scenario())


def test_amazon_response_guard_distinguishes_rate_limit_and_access_denied():
    class Response:
        def __init__(self, status, headers=None):
            self.status = status
            self.headers = headers or {}

    with pytest.raises(BackoffRequired) as limited:
        Amazon._raise_for_response(Response(429, {'retry-after': '120'}))
    assert limited.value.status == 429
    assert limited.value.retry_after_seconds == 120

    with pytest.raises(BackoffRequired) as unavailable:
        Amazon._raise_for_response(Response(503))
    assert unavailable.value.status == 503

    with pytest.raises(AccessDenied):
        Amazon._raise_for_response(Response(403))

    Amazon._raise_for_response(Response(200))


def test_stock_is_distinct_from_purchase_controls_and_delivery_restrictions():
    from retail.models import Group, eligible, rejection_reasons
    from retail.task_groups.domain import qualifying

    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            page = await browser.new_page()
            adapter = Amazon(None)
            item = {'asin': 'B07ZLF9WQ5', 'max_price': None, 'offer_id': ''}
            group = Group(name='Grocery').model_dump()
            base = '''<span id="productTitle">Banana Bunch (4-5 Count)</span>
                <div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$0.99</span></span></div>
                <div id="merchant-info">Sold by Amazon.com</div>'''
            stock = '<div id="availability">In Stock</div>'
            location = '<div>This item cannot be shipped to your selected delivery location. Please choose a different delivery location.</div>'
            signin = '<div>Sign in to get started<button>Sign In</button></div>'
            cart = '<button id="add-to-cart-button">Add to cart</button>'
            cases = [
                (stock + location + signin, 'available', False, ['delivery location', 'Sign in required']),
                (stock + signin, 'available', False, ['Sign in required']),
                (stock, 'available', False, ['Add-to-cart control']),
                (stock + '<button disabled id="add-to-cart-button">Add to cart</button>', 'available', False, ['Add-to-cart control']),
                ('<div id="outOfStock">Currently unavailable. We don\'t know when this will be back in stock.</div>', 'unavailable', False, ['Out of stock']),
                ('', 'unknown', False, ['Stock could not be verified']),
                (stock + location + cart, 'available', False, ['delivery location']),
                (stock + cart, 'available', True, ['In stock']),
                ('<div hidden id="outOfStock">Out of stock</div>' + stock + cart, 'available', True, ['In stock']),
            ]
            for fragment, status, can_buy, messages in cases:
                html = base + '<nav>Sign in to get started</nav><div id="rightCol">' + fragment + '</div>'
                # Locally fulfilled product page; no retailer calls or purchases.
                await page.route('https://www.amazon.com/**', lambda route: route.fulfill(body=html, content_type='text/html'))
                product = await adapter.inspect(page, item, 'US')
                assert product['availability_status'] == status
                assert product['available'] is can_buy
                assert all(message in product['availability_message'] for message in messages)
                assert eligible(product, item, group) is can_buy
                if not can_buy:
                    assert product['availability_message'] in rejection_reasons(product, item, group)
                    result = qualifying(product, {}, {})
                    assert result[0] is False
                    if status != 'unavailable':
                        assert result[1] != 'out_of_stock'
                await page.unroute_all()
            await browser.close()

    asyncio.run(scenario())
