import asyncio
from patchright.async_api import async_playwright
from retail.interactions import resolve


def test_add_this_product_to_cart_label():
    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def fulfill_fixture(route):
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="""<!doctype html><button type='button' onclick=\"document.querySelector('#outcome').textContent='cart updated'\">Add this product to cart</button><div id='outcome'></div>""",
                )

            await page.route("**/*", fulfill_fixture)
            await page.goto("https://fixture.invalid/")
            control = await resolve(page, "ADD_TO_CART")
            await control.click()
            assert await page.locator("#outcome").inner_text() == "cart updated"
            await browser.close()

    asyncio.run(run())