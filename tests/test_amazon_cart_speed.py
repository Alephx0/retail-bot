"""Add-first cart flow: real browser, controlled server, no retailer traffic."""
import asyncio
import pytest
from patchright.async_api import async_playwright

from retail.amazon import Amazon, Attention, CartRejected
from scripts.cart_fixture import CartFixture, TARGET, OTHER


@pytest.mark.parametrize('existing,other,requested,direct,delay', [
    (0, 0, 1, False, 0), (0, 1, 1, False, 0),
    (1, 1, 1, False, 150), (2, 0, 2, False, 0),
    (0, 0, 1, True, 0), (1, 0, 1, True, 150),
])
def test_add_once_then_reconcile(existing, other, requested, direct, delay):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            fixture = CartFixture(quantity=existing, unrelated=other, direct=direct, delay=delay)
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            fixture.navigations = 0
            adapter = Amazon(None)
            assert await adapter.cart(page, requested, TARGET) == requested
            assert fixture.items == {TARGET: requested}
            assert fixture.events[0] == 'add'
            assert fixture.events.count('add') == 1
            assert fixture.events.count('delete') == int(bool(other))
            assert fixture.navigations == 1
            assert await adapter.get_cart(page) == [{'asin': TARGET, 'quantity': requested}]
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['add', 'product_recommendation', 'delete', 'duplicates', 'ambiguous_delete', 'quantity'])
def test_uncertain_mutations_stop_without_repeating(failure):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            page.set_default_timeout(450)
            fixture = CartFixture(quantity=1 if failure == 'quantity' else 0,
                                  unrelated=1 if failure in ('delete', 'ambiguous_delete') else 0,
                                  stall='add' if failure == 'product_recommendation' else failure, duplicates=failure == 'duplicates')
            async def route(r):
                await fixture.route(r)
            await page.route('**/*', route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            if failure == 'product_recommendation':
                await page.locator('body').evaluate('(e, asin) => e.insertAdjacentHTML("beforeend", `<div data-asin="${asin}" data-quantity="1">Recommended product</div>`)', TARGET)
            adapter = Amazon(None)
            navigate = adapter.navigate
            async def inspect_cart(page, url, **kwargs):
                result = await navigate(page, url, **kwargs)
                if failure == 'ambiguous_delete':
                    await page.locator("button[name^='submit.delete']").evaluate('e => e.after(e.cloneNode(true))')
                if failure == 'quantity':
                    await page.locator("select[name='quantity']").evaluate('e => e.remove()')
                return result
            adapter.navigate = inspect_cart
            with pytest.raises(Attention) as caught:
                await adapter.cart(page, 1, TARGET)
            assert not isinstance(caught.value, CartRejected), 'Post-add failures must retain the mutation flag'
            assert fixture.events.count('add') == 1
            assert fixture.events.count('delete') == int(failure == 'delete')
            if failure in ('add', 'product_recommendation'):
                assert fixture.navigations == 1, 'Do not navigate away before Amazon acknowledges the add'
            await browser.close()
    asyncio.run(scenario())


def test_cancel_after_add_preserves_uncertainty_and_other_account_progress():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            stalled, healthy = CartFixture(stall='add'), CartFixture()
            pages = [await browser.new_page(), await browser.new_page()]
            added = asyncio.Event()
            async def route(r):
                await stalled.route(r)
                if stalled.first_add:
                    added.set()
            await pages[0].route('**/*', route)
            await pages[1].route('**/*', healthy.route)
            for page in pages:
                await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            job = asyncio.create_task(Amazon(None).cart(pages[0], 1, TARGET))
            await asyncio.wait_for(added.wait(), 3)
            assert await Amazon(None).cart(pages[1], 1, TARGET) == 1
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await job
            assert stalled.events == ['add'] and healthy.events == ['add']
            assert stalled.items == healthy.items == {TARGET: 1}
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('markup', [
    '<select id="quantity"><option value="1">1</option></select>',
    '<select id="quantity"><option value="2" disabled>2</option></select>',
    '<select id="quantity"><option value="2">2</option></select>' * 2,
])
def test_unsupported_or_ambiguous_quantity_never_adds(markup):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(body=markup + '<button id="add-to-cart-button" onclick="window.added=true">Add to cart</button>', content_type='text/html'))
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            with pytest.raises(CartRejected):
                await Amazon(None).cart(page, 2, TARGET)
            assert not await page.evaluate('window.added || false', isolated_context=False)
            await browser.close()
    asyncio.run(scenario())


def test_target_removed_before_checkout_does_not_clear_other_items():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            fixture = CartFixture(unrelated=1)
            await page.route('**/*', fixture.route)
            await page.goto('https://www.amazon.com/gp/cart/view.html')
            with pytest.raises(Attention, match='no items were removed'):
                await Amazon(None).prepare_checkout(page, TARGET, 1)
            assert fixture.items == {OTHER: 1} and not fixture.events
            await browser.close()
    asyncio.run(scenario())
