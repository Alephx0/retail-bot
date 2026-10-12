"""Direct ATC posts only to intercepted fixtures; never to a real retailer."""
import asyncio
from urllib.parse import parse_qs
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

from retail.amazon import Amazon, Attention, BackoffRequired, CartRejected
from retail.amazon_offers import DIRECT_OFFER_FORM
from test_offer_discovery import BASE, TARGET


TOKEN = 'offer%2B+/=fixture'


def product_html():
    return BASE + f'''<form method=post action="/gp/product/handle-buy-box/ref=fixture"
        onsubmit="sessionStorage.submits=Number(sessionStorage.submits||0)+1">
      <input name=ASIN type=hidden value="{TARGET}">
      <input name=offerListingID type=hidden value="{TOKEN}">
      <input name="items[0.base][offerListingId]" type=hidden value="{TOKEN}">
      <input name="items[0.base][asin]" type=hidden value="{TARGET}">
      <input name=anti-csrftoken-a2z type=hidden value="fixture-csrf">
      <input name=session-id type=hidden value="fixture-session">
      <select id=quantity name=quantity onchange="sessionStorage.quantityChanges=Number(sessionStorage.quantityChanges||0)+1"><option>1</option><option>2</option></select>
      <input name=unchecked type=checkbox value=omit><input name=checked type=checkbox checked value=keep>
      <input name=disabled disabled value=omit><input name=duplicate type=hidden value=a><input name=duplicate type=hidden value=b>
      <button id=add-to-cart-button name=submit.add-to-cart value="Add to Cart"
        onclick="sessionStorage.clicks=Number(sessionStorage.clicks||0)+1">Add to Cart</button>
      <button name=submit.buy-now value=omit>Buy Now</button>
      <div id=buyNow_feature_div><input name=isBuyNow type=hidden value=1><input name=pipelineType type=hidden value=buy-now></div>
    </form>'''


class DirectFixture:
    def __init__(self, outcome='success', delay=0):
        self.outcome, self.delay = outcome, delay
        self.posts, self.cart_visits = [], 0

    async def route(self, route):
        request = route.request
        if request.method == 'POST':
            self.posts.append(parse_qs(request.post_data, keep_blank_values=True))
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.outcome == 'abort':
                await route.abort()
                return
            if self.outcome == 'limit':
                await route.fulfill(status=429, headers={'Retry-After':'1800'}, body='Wait')
                return
            if self.outcome == 'no_ack':
                await route.fulfill(content_type='text/html', body='<p>Unrecognized response</p>')
                return
            # Keep every navigation intercepted: route.fulfill redirects may
            # be followed internally without invoking the route handler again.
            await route.fulfill(content_type='text/html', body='<div id=huc-v2-order-row-confirm-text>Added to Cart</div>')
        elif '/gp/cart/' in request.url:
            self.cart_visits += 1
            quantity = self.posts[-1]['quantity'][0]
            token = 'changed-token' if self.outcome == 'mismatch' else TOKEN
            await route.fulfill(content_type='text/html', body=f'''<div id=sc-active-cart><div data-asin="{TARGET}"
                data-quantity="{quantity}" data-offer-id="{token}">Target</div></div>''')
        else:
            await route.fulfill(content_type='text/html', body=product_html())


@pytest.mark.parametrize('shortcut', [False, True])
@pytest.mark.parametrize('quantity', [1, 2])
@pytest.mark.parametrize('delay', [0, .2])
def test_direct_and_native_forms_send_exact_fields_once(shortcut, quantity, delay, monkeypatch):
    async def scenario():
        fixture = DirectFixture(delay=delay)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, {'asin':TARGET,'offer_id_shortcut':shortcut})
            if shortcut:
                monkeypatch.setattr('retail.amazon.behavior.prepare', AsyncMock(side_effect=AssertionError('No mouse preparation in shortcut')))
            assert await adapter.cart(page, quantity, TARGET) == quantity
            assert len(fixture.posts) == fixture.cart_visits == 1
            fields = fixture.posts[0]
            assert fields['ASIN'] == [TARGET]
            assert fields['offerListingID'] == [TOKEN]
            assert fields['items[0.base][offerListingId]'] == [TOKEN]
            assert fields['anti-csrftoken-a2z'] == ['fixture-csrf']
            assert fields['session-id'] == ['fixture-session']
            assert fields['submit.add-to-cart'] == ['Add to Cart']
            assert fields['quantity'] == [str(quantity)]
            assert fields['duplicate'] == ['a','b']
            assert fields['checked'] == ['keep']
            assert not any(k in fields for k in ('unchecked','disabled','submit.buy-now'))
            if shortcut:
                assert 'isBuyNow' not in fields and 'pipelineType' not in fields
            assert await page.evaluate('Number(sessionStorage.clicks||0)') == (0 if shortcut else 1)
            assert await page.evaluate('Number(sessionStorage.submits||0)') == (0 if shortcut else 1)
            assert await page.evaluate('Number(sessionStorage.quantityChanges||0)') == (1 if not shortcut and quantity == 2 else 0)
            assert page._retail_offer_usage['route'] == ('direct_form' if shortcut else 'native_click')
            assert page._retail_offer_usage['cart'] == 'matched'
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['abort','limit','no_ack','mismatch'])
def test_post_failures_never_fall_back_or_repeat_add(outcome):
    async def scenario():
        fixture = DirectFixture(outcome)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            page.set_default_timeout(500)
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, {'asin':TARGET,'offer_id_shortcut':True})
            adapter.click_bound_offer = AsyncMock(side_effect=AssertionError('Never click after POST'))
            with pytest.raises(Exception) as caught:
                await adapter.cart(page, 1, TARGET)
            assert not isinstance(caught.value, (CartRejected, AssertionError))
            assert len(fixture.posts) == 1
            assert adapter.click_bound_offer.await_count == 0
            if outcome == 'limit':
                assert isinstance(caught.value, BackoffRequired)
                assert caught.value.retry_after_seconds == 1800
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ["form.method='get'", "form.action='/unrecognized'", "form.target='_blank'",
    "form.querySelector('[name=anti-csrftoken-a2z]').remove()", "form.insertAdjacentHTML('beforeend','<input type=file name=upload>')",
    "form.insertAdjacentHTML('beforeend','<input name=isBuyNow value=1>')"])
def test_unsupported_forms_return_before_any_dispatch(change):
    async def scenario():
        fixture = DirectFixture()
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, {'asin':TARGET,'offer_id_shortcut':True})
            await page.evaluate('()=>{const form=document.querySelector("form");'+change+'}')
            assert not await adapter.direct_offer_cart(page, page.locator('#add-to-cart-button'), page._retail_discovered_offer, 1)
            assert fixture.posts == []
            await browser.close()
    asyncio.run(scenario())


def test_form_change_before_atomic_dispatch_never_posts(monkeypatch):
    async def scenario():
        fixture = DirectFixture()
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, {'asin':TARGET,'offer_id_shortcut':True})
            control = page.locator('#add-to-cart-button')
            evaluate = control.evaluate
            async def changed(script, args):
                if args['submit']:
                    await page.locator('[name=offerListingID]').evaluate("e=>e.value='changed'")
                return await evaluate(script, args)
            monkeypatch.setattr(control,'evaluate',changed)
            with pytest.raises(CartRejected):
                await adapter.direct_offer_cart(page, control, page._retail_discovered_offer, 1)
            assert fixture.posts == []
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', [
    "document.querySelector('[name=offerListingID]').value='changed'",
    "document.querySelector('#quantity option:last-child').disabled=true",
    "document.querySelector('form').append(document.querySelector('#add-to-cart-button').cloneNode(true))",
    "document.body.insertAdjacentHTML('beforeend','<input id=ap_password>')",
    "const fields=document.createElement('fieldset'); fields.disabled=true; document.querySelector('form').append(fields); document.querySelectorAll('[name=offerListingID],[name=\"items[0.base][offerListingId]\"]').forEach(e=>fields.append(e))",
])
@pytest.mark.parametrize('endpoint', [False, True])
def test_atomic_cart_rejects_changed_or_interrupted_product_before_post(change, endpoint):
    async def scenario():
        fixture = DirectFixture()
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, {'asin':TARGET, 'offer_id_shortcut':True, 'offer_id_endpoint':endpoint})
            await page.evaluate('()=>{'+change+'}')
            adapter.click_bound_offer = AsyncMock(side_effect=AssertionError('No fallback for changed evidence'))
            with pytest.raises(Attention):
                await adapter.cart(page, 2, TARGET)
            assert not fixture.posts
            assert adapter.click_bound_offer.await_count == 0
            await browser.close()
    asyncio.run(scenario())


def test_direct_cart_uses_acknowledgement_without_waiting_for_deferred_scripts():
    async def scenario():
        fixture = DirectFixture()
        script_requested, release_script = asyncio.Event(), asyncio.Event()
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            async def intercepted(route):
                if '/slow-fixture.js' in route.request.url:
                    script_requested.set()
                    await release_script.wait()
                    await route.fulfill(content_type='application/javascript', body='')
                elif route.request.method == 'POST':
                    fixture.posts.append(parse_qs(route.request.post_data, keep_blank_values=True))
                    await route.fulfill(content_type='text/html', body='''
                        <div id=huc-v2-order-row-confirm-text>Added to Cart</div>
                        <script>document.addEventListener('DOMContentLoaded',()=>sessionStorage.responseReady='yes')</script>
                        <script defer src="/slow-fixture.js"></script>''')
                else:
                    await fixture.route(route)
            await page.route('**/*', intercepted)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, {'asin':TARGET, 'offer_id_shortcut':True})
            operation = asyncio.create_task(adapter.cart(page, 1, TARGET))
            try:
                await asyncio.wait_for(script_requested.wait(), 3)
                assert await asyncio.wait_for(operation, 3) == 1
                assert not release_script.is_set()
                assert await page.evaluate('sessionStorage.responseReady') is None
                assert len(fixture.posts) == fixture.cart_visits == 1
                assert page._retail_offer_usage['cart'] == 'matched'
            finally:
                release_script.set()
                await browser.close()
    asyncio.run(scenario())


def test_unsupported_shortcut_uses_one_native_click_before_any_post():
    async def scenario():
        fixture = DirectFixture()
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            await page.locator('[name=anti-csrftoken-a2z]').evaluate('e=>e.remove()')
            adapter = Amazon(None)
            await adapter.read_product(page, {'asin':TARGET,'offer_id_shortcut':True})
            await adapter.cart(page, 1, TARGET)
            assert len(fixture.posts) == 1
            assert await page.evaluate('Number(sessionStorage.clicks||0)') == 1
            assert page._retail_offer_usage['shortcut'] == 'unsupported_form'
            assert page._retail_offer_usage['route'] == 'native_click'
            await browser.close()
    asyncio.run(scenario())


def test_cancel_after_post_preserves_uncertainty_and_never_clicks():
    async def scenario():
        fixture = DirectFixture()
        arrived, release = asyncio.Event(), asyncio.Event()
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            async def local(route):
                if route.request.method == 'POST':
                    arrived.set()
                    await release.wait()
                await fixture.route(route)
            await page.route('**/*', local)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, {'asin':TARGET,'offer_id_shortcut':True})
            adapter.click_bound_offer = AsyncMock(side_effect=AssertionError('No fallback after dispatch'))
            operation = asyncio.create_task(adapter.cart(page, 1, TARGET))
            try:
                await asyncio.wait_for(arrived.wait(), 3)
                operation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await operation
                assert page._retail_offer_usage['form'] == 'dispatch_uncertain'
                assert adapter.click_bound_offer.await_count == 0
            finally:
                release.set()
                await browser.close()
    asyncio.run(scenario())


def test_dispatch_marker_prevents_repeated_form_submission():
    async def scenario():
        fixture = DirectFixture()
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, {'asin':TARGET,'offer_id_shortcut':True})
            await page.locator('form').evaluate("e=>e.dataset.retailOfferDispatched='true'")
            with pytest.raises(Attention, match='already dispatched') as caught:
                await adapter.direct_offer_cart(page, page.locator('#add-to-cart-button'), page._retail_discovered_offer, 1)
            assert not isinstance(caught.value, CartRejected)
            assert not fixture.posts
            await browser.close()
    asyncio.run(scenario())
