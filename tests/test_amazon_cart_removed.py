"""Removal notices remain in Amazon's DOM; they must not block reconciliation."""
import asyncio
from pathlib import Path

import pytest
from patchright.async_api import async_playwright

from retail.amazon import Amazon, Attention
from retail.amazon_states import PAGE_STATE
from scripts.cart_fixture import TARGET, OTHER


@pytest.mark.parametrize('mode,delay', [
    ('notice', 0), ('notice', 150), ('changed_quantity', 0),
    ('stalled', 0), ('interrupted', 0), ('busy', 0),
])
def test_removed_notice_then_quantity_then_checkout(mode, delay):
    async def scenario():
        markup=(Path(__file__).parent/'fixtures/amazon_cart_removed_notice.html').read_text(encoding='utf-8')
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            page=await browser.new_page()
            page.set_default_timeout(1000)
            await page.route('**/*',lambda r:r.fulfill(body=markup,content_type='text/html'))
            await page.goto('https://www.amazon.com/gp/cart/view.html')
            await page.evaluate('(v)=>Object.assign(fixture,v)',{'mode':mode,'delay':delay},isolated_context=False)
            adapter=Amazon(None)
            if mode in ('stalled','interrupted','busy'):
                with pytest.raises(Attention):
                    await adapter.cart(page,1,TARGET)
                assert await page.evaluate('cartEvents',isolated_context=False)==[{'action':'delete','asin':OTHER}]
            else:
                assert await adapter.cart(page,1,TARGET)==1
                assert await adapter.get_cart(page)==[{'asin':TARGET,'quantity':1}]
                assert await page.locator('#sc-active-cart [data-removed="true"]:visible').count()==2
                await adapter.prepare_checkout(page,TARGET,1)
                quantities=[1] if mode=='changed_quantity' else [2,1]
                assert await page.evaluate('cartEvents',isolated_context=False)==[
                    {'action':'delete','asin':OTHER}, {'action':'delete','asin':'B000000002'},
                    *[{'action':'quantity','quantity':q} for q in quantities], {'action':'checkout'}]
                assert await page.locator('#spc-orders [data-asin]').count()==1
                assert await page.locator('#sc-saved-cart [data-asin]').count()==1
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fallback',[False,True])
def test_removed_target_notice_is_not_inventory_or_quantity_proof(fallback):
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            page=await browser.new_page()
            root='legacy-cart' if fallback else 'sc-active-cart'
            body=f'''<div id="{root}"><div data-asin="{TARGET}" data-quantity="1" data-removed="true">
                <div data-asin="{TARGET}" data-quantity="1">Item removed</div></div></div>'''
            await page.route('**/*',lambda r:r.fulfill(body=body,content_type='text/html'))
            await page.goto('https://www.amazon.com/gp/cart/view.html')
            adapter=Amazon(None)
            assert await adapter.get_cart(page)==[]
            for state in ('quantity','cart_items'):
                assert not await page.evaluate(PAGE_STATE,{'state':state,'asin':TARGET,'quantity':1})
            # Readding the same ASIN must ignore its old removal notice in
            # both the read model and the quantity-control locator.
            if not fallback:
                await page.locator('#sc-active-cart').evaluate('''(e,asin)=>e.insertAdjacentHTML('beforeend',
                    `<div data-asin="${asin}" data-quantity="2" data-removed="false">
                    <select name="quantity" onchange="this.parentElement.dataset.quantity=this.value">
                    <option value="1">1</option><option value="2" selected>2</option></select></div>`)''',TARGET)
                assert await adapter.cart(page,1,TARGET)==1
                assert await adapter.get_cart(page)==[{'asin':TARGET,'quantity':1}]
            await browser.close()
    asyncio.run(scenario())
