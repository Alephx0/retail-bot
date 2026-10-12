"""Offer evidence through native cart/review; all requests are intercepted."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

from retail.amazon import Amazon, Attention, CartRejected
from test_offer_discovery import BASE, form, TARGET, OTHER
from test_amazon_states import REVIEW


@pytest.mark.parametrize('token', ['current-token', '', 'different-token', 'ambiguous'])
def test_cart_offer_evidence_after_exactly_one_native_add(token):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            adds = []
            async def local(route):
                if '/gp/cart/' in route.request.url:
                    adds.append('add')
                    row = f'<div data-asin="{TARGET}" data-quantity="1" data-offer-id="{token}">Target'
                    if token == 'ambiguous':
                        row += '<input type=hidden name=offerListingID value=current-token>'
                    body = '<div id=sc-active-cart>' + row + '</div></div>'
                    # Unrelated/global tokens must not count as row evidence.
                    body += '<input name=offerListingID value=unrelated><div id=sc-saved-cart><div data-offer-id=unrelated></div></div>'
                else:
                    body = BASE + form('current-token').replace(
                        'event.preventDefault();window.adds=(window.adds||0)+1',
                        "event.preventDefault();location.href='/gp/cart/view.html'")
                await route.fulfill(body=body, content_type='text/html')
            await page.route('**/*', local)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            updates = []
            page._retail_report_offer_usage = updates.append
            await adapter.read_product(page, {'asin': TARGET})
            if token in ('different-token', 'ambiguous'):
                with pytest.raises(Attention, match='Cart Offer ID differs'):
                    await adapter.cart(page, 1, TARGET)
                assert page._retail_offer_usage['cart'] == 'mismatch'
            else:
                await adapter.cart(page, 1, TARGET)
                assert page._retail_offer_usage['cart'] == ('matched' if token else 'not_exposed')
            assert adds == ['add']
            assert page._retail_offer_usage['offer_id'] == 'current-token'
            assert page._retail_offer_usage['form'] == 'matched_clicked'
            assert [u['form'] for u in updates[:2]] == ['matched_dispatching', 'matched_clicked']
            assert page._retail_offer_usage['checkout'] == 'not_checked'
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('token', ['current-token', '', 'different-token'])
def test_checkout_validates_row_offer_and_records_honest_missing_evidence(token):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            body = REVIEW.replace('data-seller=', f'data-offer-id="{token}" data-seller=')
            body += '<input name=offerListingID value=unrelated><button onclick="window.submits=(window.submits||0)+1">Place your order</button>'
            await page.route('**/*', lambda r: r.fulfill(body=body, content_type='text/html'))
            await page.goto('https://www.amazon.com/checkout/spc')
            page._retail_offer_usage = {'offer_id': 'current-token', 'asin': TARGET, 'origin': 'www.amazon.com', 'form': 'matched_clicked', 'cart': 'matched', 'action': 'add_to_cart'}
            adapter = Amazon(None)
            if token == 'different-token':
                with pytest.raises(Attention, match='Checkout Offer ID differs'):
                    await adapter.checkout_snapshot(page, TARGET, 1, 25)
                assert page._retail_offer_usage['checkout'] == 'mismatch'
            else:
                snapshot = await adapter.checkout_snapshot(page, TARGET, 1, 25)
                assert snapshot['offer_usage']['checkout'] == ('matched' if token else 'not_exposed')
            assert await page.evaluate('window.submits||0', isolated_context=False) == 0
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['token', 'missing', 'asin', 'during_movement'])
def test_submit_rechecks_offer_and_never_submits_changed_evidence(change, monkeypatch):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            body = REVIEW.replace('data-seller=', 'data-offer-id="current-token" data-seller=')
            body += '<button onclick="window.submits=(window.submits||0)+1">Place your order</button>'
            await page.route('**/*', lambda r: r.fulfill(body=body, content_type='text/html'))
            await page.goto('https://www.amazon.com/checkout/spc')
            page._retail_offer_usage = {'offer_id': 'current-token', 'asin': TARGET, 'origin': 'www.amazon.com', 'form': 'matched_clicked', 'cart': 'matched'}
            adapter = Amazon(None)
            await adapter.checkout_snapshot(page, TARGET, 1, 25)
            row = page.locator('[data-asin]')
            if change == 'token':
                await row.evaluate("e=>e.setAttribute('data-offer-id','different')")
            elif change == 'missing':
                await row.evaluate("e=>e.removeAttribute('data-offer-id')")
            elif change == 'asin':
                await row.evaluate('(e,asin)=>e.dataset.asin=asin', OTHER)
            else:
                async def prepare(*args):
                    await row.evaluate("e=>e.setAttribute('data-offer-id','different')")
                monkeypatch.setattr('retail.amazon.behavior.prepare', prepare)
            with pytest.raises(Attention):
                await adapter.submit_order(page)
            assert await page.evaluate('window.submits||0', isolated_context=False) == 0
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('changed', [False, True])
def test_buy_now_uses_same_offer_binding(changed):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            body = BASE + form('current-token').replace('</form>', '<button id=buy-now-button>Buy Now</button></form>')
            await page.route('**/*', lambda r: r.fulfill(body=body, content_type='text/html'))
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            adapter.advance_checkout = AsyncMock()
            await adapter.read_product(page, {'asin': TARGET})
            if changed:
                await page.locator('[name="offeringID.1"]').evaluate("e=>e.value='different'")
                with pytest.raises(CartRejected):
                    await adapter.buy_now(page, 1, TARGET)
                assert await page.evaluate('window.adds||0', isolated_context=False) == 0
            else:
                assert await adapter.buy_now(page, 1, TARGET)
                assert await page.evaluate('window.adds', isolated_context=False) == 1
                assert page._retail_offer_usage['action'] == 'buy_now'
                assert page._retail_offer_usage['form'] == 'matched_clicked'
            await browser.close()
    asyncio.run(scenario())


def test_interrupted_native_click_does_not_report_completed_use(monkeypatch):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(body=BASE+form(), content_type='text/html'))
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, {'asin': TARGET})
            control = page.locator('#add-to-cart-button')
            monkeypatch.setattr(control, 'click', AsyncMock(side_effect=asyncio.CancelledError()))
            with pytest.raises(asyncio.CancelledError):
                await adapter.click_bound_offer(page, control, page._retail_discovered_offer, 'add_to_cart')
            assert page._retail_offer_usage['form'] == 'dispatch_uncertain'
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['cart', 'checkout'])
def test_row_evidence_ignores_other_products_and_stops_when_proof_disappears(phase):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            body = f'''<div data-asin="{TARGET}" id=target><input type=hidden name=offerListingID value=current-token>
              <div data-asin="{OTHER}"><input name=offerListingID value=other-token></div></div>'''
            await page.route('**/*', lambda r: r.fulfill(body=body, content_type='text/html'))
            await page.goto('https://www.amazon.com/gp/cart/view.html')
            adapter = Amazon(None)
            page._retail_offer_usage = {'offer_id': 'current-token', 'asin': TARGET, 'origin': 'www.amazon.com'}
            row = page.locator('#target')
            await adapter.verify_offer_row(page, TARGET, row, phase)
            assert page._retail_offer_usage[phase] == 'matched'
            await page.locator('#target > input').evaluate('e=>e.remove()')
            with pytest.raises(Attention, match='evidence disappeared'):
                await adapter.verify_offer_row(page, TARGET, row, phase)
            assert page._retail_offer_usage[phase] == 'evidence_lost'
            await browser.close()
    asyncio.run(scenario())


def test_usage_callback_persists_only_its_task(tmp_path):
    from test_monitors import fixture, task_record
    from retail.runner import TaskRunner
    store, group, account, engine = fixture(tmp_path)
    first = task_record(store, group, account)
    second = task_record(store, group, account)
    usage = {'offer_id': 'current-token', 'asin': TARGET, 'cart': 'matched'}
    TaskRunner(engine).record_offer_usage(first['id'], usage)
    assert store.get('tasks', first['id'])['offer_usage'] == usage
    assert store.get('tasks', second['id']) == second
    store.db.close()
