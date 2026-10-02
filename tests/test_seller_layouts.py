import asyncio

from patchright.async_api import async_playwright

from retail.amazon import Amazon


def test_seller_layouts_do_not_confuse_fulfillment_with_merchant():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            adapter = Amazon(None)
            fixtures = [
                ('<div id="merchantInfoFeature_feature_div">Shipper / Seller\nAmazon.com</div>', 'Amazon.com'),
                ('<div id="merchantInfoFeature_feature_div"><span>Sold by</span><span class="offer-display-feature-text-message">Amazon.com</span></div>', 'Amazon.com'),
                ('<div id="fulfillerInfoFeature_feature_div">Ships from Amazon.com</div><div id="merchantInfoFeature_feature_div"><span>Sold by</span><span class="offer-display-feature-text-message">Example Store</span></div>', 'Example Store'),
                ('<div id="tabular-buybox"><div tabular-attribute-name="Sold by"><span class="tabular-buybox-text">Amazon.com</span></div></div>', 'Amazon.com'),
                ('<div id="merchant-info">Ships from and sold by Amazon.com.</div>', 'Amazon.com'),
                ('<div id="merchant-info">Ships from Amazon.com</div>', ''),
            ]
            for html, expected in fixtures:
                await page.set_content(html)
                assert await adapter.seller_text(page, product_page=True) == expected
            await browser.close()
    asyncio.run(scenario())
