import asyncio
import pytest

from patchright.async_api import async_playwright

from retail.amazon import Amazon, Attention


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
            try:
                await adapter.check(page)
                raise AssertionError('Challenge must pause the task')
            except Attention:
                pass
            await page.set_content('<body>Click the button below to continue shopping<button>Continue shopping</button></body>')
            with pytest.raises(Attention,match='Continue shopping confirmation'):
                await adapter.check(page)
            await browser.close()
    asyncio.run(scenario())
