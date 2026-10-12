"""Offer discovery and one native add on intercepted Amazon fixtures only."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

from retail.amazon import Amazon, CartRejected
from retail.interactions import resolve
from scripts.cart_fixture import CartFixture, TARGET, OTHER


BASE = '''<h1 id="productTitle">Offer discovery fixture</h1>
<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$20.00</span></span></div>
<div id="merchant-info">Ships from and sold by Amazon.com.</div><div id="availability">In Stock</div>'''


def form(token='token%2B+/=', asin=TARGET, name='offeringID.1', extra='', action=''):
    return f'''<form action="{action}" onsubmit="event.preventDefault();window.adds=(window.adds||0)+1">
        <input type="hidden" name="ASIN" value="{asin}">
        <input type="hidden" name="{name}" value="{token}">{extra}
        <button id="add-to-cart-button">Add to cart</button></form>'''


@pytest.mark.parametrize('name', ['offerListingID', 'offeringID.1', 'OfferListingId.1'])
def test_offer_discovery_uses_active_form_and_preserves_exact_token(name):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r:r.fulfill(body=BASE+form(name=name),content_type='text/html'))
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            await page.locator('body').evaluate("e=>e.insertAdjacentHTML('afterbegin', '<input name=offerListingID value=wrong><form hidden><input name=offeringID.1 value=stale></form>')")
            adapter = Amazon(None)
            product = await adapter.read_product(page, {'asin':TARGET})
            assert product['offer_id'] == 'token%2B+/='
            assert product['offer_id_status'] == 'discovered'
            adapter.resolve_action = AsyncMock(side_effect=AssertionError('Discovered control should avoid repeated resolution'))
            await adapter.click_product_offer(page, TARGET)
            assert await page.evaluate('window.adds', isolated_context=False) == 1
            assert page._retail_discovered_offer is None
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('markup,status', [
    (form(token=''), 'missing'),
    (form(extra='<input type=hidden name=offerListingID value=conflict>'), 'ambiguous'),
    (form(asin=OTHER), 'product_mismatch'),
    (form(action='https://other.example/cart'), 'unsupported'),
    ('<input name=offerListingID value=unrelated><button>Add to cart</button>', 'unsupported'),
    (form()+form(), 'unavailable'),
])
def test_unsupported_ambiguous_or_other_product_never_supplies_offer(markup, status):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r:r.fulfill(body=BASE+markup,content_type='text/html'))
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            product = await Amazon(None).read_product(page, {'asin':TARGET}, inventory_only=True)
            assert product['offer_id'] == '' and product['offer_id_status'] == status
            if status == 'product_mismatch':
                assert not product['available']
            assert page._retail_discovered_offer is None
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['token', 'asin', 'expired', 'navigation', 'detached', 'during_movement'])
def test_changed_discovered_offer_never_clicks(change):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r:r.fulfill(body=BASE+form(),content_type='text/html'))
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, {'asin':TARGET})
            if change == 'token':
                await page.locator('[name="offeringID.1"]').evaluate("e=>e.value='changed'")
            elif change == 'asin':
                await page.locator('[name=ASIN]').evaluate(f"e=>e.value='{OTHER}'")
            elif change == 'expired':
                page._retail_discovered_offer['at'] -= 16
            elif change == 'navigation':
                await page.goto(f'https://www.amazon.com/dp/{OTHER}')
            elif change == 'detached':
                await page.locator('form').evaluate('e=>e.remove()')
            else:
                page.context._retail_paced_input = True
                await page.evaluate("document.addEventListener('mousemove',()=>document.querySelector('[name=\"offeringID.1\"]').value='changed',{once:true})")
            with pytest.raises(CartRejected):
                await adapter.click_product_offer(page, TARGET)
            assert await page.evaluate('window.adds||0', isolated_context=False) == 0
            await browser.close()
    asyncio.run(scenario())


def test_account_isolation_inventory_signal_and_native_fallback():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            pages = [await browser.new_page() for _ in range(2)]
            adapter = Amazon(None)
            for page, token in zip(pages, ['account-one','account-two']):
                await page.route('**/*', lambda r:r.fulfill(body=BASE+form(token),content_type='text/html'))
                await page.goto(f'https://www.amazon.com/dp/{TARGET}')
                assert (await adapter.read_product(page, {'asin':TARGET},inventory_only=True))['offer_id'] == token
                assert page._retail_discovered_offer is None
                await adapter.read_product(page, {'asin':TARGET})
                assert page._retail_discovered_offer['id'] == token
            await pages[0].set_content(BASE+form(token=''))
            await adapter.read_product(pages[0], {'asin':TARGET})
            async def fallback(page, action):
                return await resolve(page, action)
            adapter.resolve_action = AsyncMock(side_effect=fallback)
            await adapter.click_product_offer(pages[0], TARGET)
            assert adapter.resolve_action.await_count == 1
            assert pages[1]._retail_discovered_offer['id'] == 'account-two'
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('width', [1000,390])
def test_discovered_offer_native_cart_once_and_quantity_verified(width):
    async def scenario():
        fixture = CartFixture()
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page(viewport={'width':width,'height':700})
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            await page.evaluate('''base=>{
                document.body.insertAdjacentHTML('afterbegin', base);
                const button=document.querySelector('#add-to-cart-button');
                const form=document.createElement('form');button.before(form);
                form.innerHTML='<input type="hidden" name="ASIN" value="B012345678"><input type="hidden" name="offeringID.1" value="native-token">';
                button.type='button';form.append(button);
            }''', BASE)
            adapter = Amazon(None)
            await adapter.read_product(page, {'asin':TARGET})
            adapter.resolve_action = AsyncMock(side_effect=AssertionError('No control rediscovery'))
            artifacts = Path('artifacts/offer-discovery');artifacts.mkdir(parents=True,exist_ok=True)
            await page.screenshot(path=str(artifacts/f'product-{width}.png'))
            assert await adapter.cart(page, 2, TARGET) == 2
            assert fixture.events == ['add']
            assert await adapter.get_cart(page) == [{'asin':TARGET,'quantity':2}]
            assert fixture.navigations == 2
            await page.screenshot(path=str(artifacts/f'cart-{width}.png'))
            await browser.close()
    asyncio.run(scenario())
