"""Endpoint requests run only against intercepted retailer fixtures."""
import asyncio
from urllib.parse import parse_qs
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

from retail.amazon import Amazon, Attention, BackoffRequired, CartRejected
from test_offer_shortcut import DirectFixture, TARGET, TOKEN


ITEM = {'asin': TARGET, 'offer_id_shortcut': True, 'offer_id_endpoint': True}


class EndpointFixture(DirectFixture):
    def __init__(self, outcome='success', delay=0):
        super().__init__(outcome, delay)
        self.requests = []

    async def route(self, route):
        request = route.request
        self.requests.append({'url': request.url, 'method': request.method,
            'navigation': request.is_navigation_request(), 'kind': request.resource_type,
            'cookie': (await request.all_headers()).get('cookie', '')})
        if request.method == 'POST' and self.outcome in ('redirect', 'redirect_wrong', 'redirect_extra', 'scripts', 'signin', 'challenge', 'large', 'json', 'denied'):
            self.posts.append(parse_qs(request.post_data, keep_blank_values=True))
            if self.outcome.startswith('redirect'):
                await route.fulfill(status=307, headers={'Location': 'https://untrusted.invalid/never-follow'}, body='')
            elif self.outcome == 'denied':
                await route.fulfill(status=403, body='Access Denied')
            else:
                contents = {
                    'scripts': '<div id=huc-v2-order-row-confirm-text>Added to Cart</div><script>sessionStorage.responseScript="executed"</script><img src="/never-load.png"><iframe src="/never-load-frame"></iframe>',
                    'signin': '<input id=ap_email><div id=huc-v2-order-row-confirm-text>Added to Cart</div>',
                    'challenge': '<input id=captchacharacters>Robot check',
                    'large': '<div id=huc-v2-order-row-confirm-text>Added to Cart</div>' + (' ' * (2 * 1024 * 1024)),
                    'json': '{"success":true}',
                }
                await route.fulfill(content_type='application/json' if self.outcome == 'json' else 'text/html', body=contents[self.outcome])
        elif '/gp/cart/' in request.url and self.outcome in ('redirect_wrong', 'redirect_extra'):
            self.cart_visits += 1
            extra = '<div data-asin=B099999999 data-quantity=1>Other</div>' if self.outcome == 'redirect_extra' else ''
            qty = 2 if self.outcome == 'redirect_wrong' else 1
            await route.fulfill(content_type='text/html', body=f'<div id=sc-active-cart><div data-asin={TARGET} data-quantity={qty} data-offer-id="{TOKEN}">Target</div>{extra}</div>')
        else:
            await super().route(route)


@pytest.mark.parametrize('quantity', [1, 2])
@pytest.mark.parametrize('delay', [0, .2])
def test_endpoint_cart_sends_one_fetch_with_browser_session_and_exact_fields(quantity, delay):
    async def scenario():
        fixture = EndpointFixture(delay=delay)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            context = await browser.new_context()
            await context.add_cookies([{'name':'fixture-account', 'value':'account-one', 'domain':'www.amazon.com', 'path':'/', 'httpOnly':True, 'secure':True}])
            page = await context.new_page()
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, ITEM)
            adapter.click_bound_offer = AsyncMock(side_effect=AssertionError('No click in endpoint path'))
            assert await adapter.cart(page, quantity, TARGET) == quantity
            assert len(fixture.posts) == fixture.cart_visits == 1
            post = next(r for r in fixture.requests if r['method'] == 'POST')
            assert post['kind'] == 'fetch' and not post['navigation']
            assert 'fixture-account=account-one' in post['cookie']
            fields = fixture.posts[0]
            assert fields['offerListingID'] == fields['items[0.base][offerListingId]'] == [TOKEN]
            assert fields['ASIN'] == [TARGET] and fields['quantity'] == [str(quantity)]
            assert fields['anti-csrftoken-a2z'] == ['fixture-csrf']
            assert fields['duplicate'] == ['a','b'] and fields['checked'] == ['keep']
            assert not any(k in fields for k in ('isBuyNow','pipelineType','unchecked','disabled','submit.buy-now'))
            assert await page.evaluate('Number(sessionStorage.quantityChanges||0)+Number(sessionStorage.clicks||0)+Number(sessionStorage.submits||0)') == 0
            assert page._retail_offer_usage['route'] == 'direct_endpoint'
            assert page._retail_offer_usage['form'] == 'endpoint_acknowledged'
            assert page._retail_offer_usage['cart'] == 'matched'
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('use_offer,shortcut,endpoint,expected', [
    (False, True, True, 'document'), (True, False, True, 'document'),
    (True, True, False, 'document'), (True, True, True, 'fetch')])
def test_endpoint_requires_all_three_switches(use_offer, shortcut, endpoint, expected):
    async def scenario():
        fixture = EndpointFixture()
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, {**ITEM, 'use_offer_id':use_offer,
                'offer_id_shortcut':shortcut, 'offer_id_endpoint':endpoint})
            await adapter.cart(page, 1, TARGET)
            posts = [r for r in fixture.requests if r['method'] == 'POST']
            assert len(posts) == 1 and posts[0]['kind'] == expected
            await browser.close()
    asyncio.run(scenario())


def test_unsupported_endpoint_uses_native_click_only_before_dispatch():
    async def scenario():
        fixture = EndpointFixture()
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            await page.locator('[name=anti-csrftoken-a2z]').evaluate('e=>e.remove()')
            adapter = Amazon(None)
            await adapter.read_product(page, ITEM)
            await adapter.cart(page, 1, TARGET)
            posts = [r for r in fixture.requests if r['method'] == 'POST']
            assert len(posts) == 1 and posts[0]['navigation']
            assert page._retail_offer_usage['route'] == 'native_click'
            assert page._retail_offer_usage['shortcut'] == 'unsupported_form'
            await browser.close()
    asyncio.run(scenario())


def test_endpoint_sessions_stay_independent_with_concurrent_accounts():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            async def account(number):
                fixture = EndpointFixture()
                context = await browser.new_context()
                await context.add_cookies([{'name':'fixture-account', 'value':str(number),
                    'domain':'www.amazon.com', 'path':'/', 'httpOnly':True, 'secure':True}])
                page = await context.new_page()
                await page.route('**/*', fixture.route)
                await page.goto(f'https://www.amazon.com/dp/{TARGET}')
                adapter = Amazon(None)
                await adapter.read_product(page, ITEM)
                await adapter.cart(page, 1, TARGET)
                post = next(r for r in fixture.requests if r['method'] == 'POST')
                assert post['cookie'] == 'fixture-account='+str(number)
                assert len(fixture.posts) == 1
                await context.close()
            await asyncio.gather(account(1), account(2))
            await browser.close()
    asyncio.run(scenario())


def test_response_is_not_rendered_and_cannot_execute_scripts_or_load_resources():
    async def scenario():
        fixture = EndpointFixture('scripts')
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', fixture.route)
            url = f'https://www.amazon.com/dp/{TARGET}'
            await page.goto(url)
            adapter = Amazon(None)
            await adapter.read_product(page, ITEM)
            offer = page._retail_discovered_offer
            assert await adapter.endpoint_offer_cart(page, None, offer, 1)
            assert page.url == url
            assert await page.locator('#huc-v2-order-row-confirm-text').count() == 0
            assert await page.evaluate('sessionStorage.responseScript') is None
            assert len(fixture.requests) == 2
            with pytest.raises(Attention, match='already dispatched'):
                await adapter.endpoint_offer_cart(page, None, offer, 1)
            assert len(fixture.posts) == 1
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['abort','limit','no_ack','mismatch','signin','challenge','large','json','denied'])
def test_endpoint_errors_never_click_or_repeat(outcome):
    async def scenario():
        fixture = EndpointFixture(outcome)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            page.set_default_timeout(500)
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, ITEM)
            adapter.click_bound_offer = AsyncMock(side_effect=AssertionError('Never fall back after fetch'))
            with pytest.raises(Attention) as caught:
                await adapter.cart(page, 1, TARGET)
            assert not isinstance(caught.value, CartRejected)
            assert len(fixture.posts) == 1
            assert adapter.click_bound_offer.await_count == 0
            if outcome == 'limit':
                assert isinstance(caught.value, BackoffRequired)
                assert caught.value.retry_after_seconds == 1800
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['redirect','redirect_wrong','redirect_extra'])
def test_redirect_never_follows_or_replays_post_and_requires_exact_cart(outcome):
    async def scenario():
        fixture = EndpointFixture(outcome)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, ITEM)
            adapter.normalize_cart_quantity = AsyncMock(return_value=1)
            adapter.remove_unrelated_cart_items = AsyncMock(side_effect=AssertionError('Only inspect after opaque response'))
            if outcome == 'redirect':
                assert await adapter.cart(page, 1, TARGET) == 1
                assert page._retail_offer_usage['form'] == 'endpoint_redirected'
                assert page._retail_offer_usage['cart'] == 'matched'
            else:
                with pytest.raises(Attention, match='without readable acknowledgement'):
                    await adapter.cart(page, 1, TARGET)
                assert adapter.normalize_cart_quantity.await_count == 0
            assert len(fixture.posts) == fixture.cart_visits == 1
            assert not any('untrusted.invalid' in r['url'] for r in fixture.requests)
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', [False, True])
def test_endpoint_timeout_and_cancellation_leave_uncertain_result_without_retry(cancel, monkeypatch):
    async def scenario():
        fixture = EndpointFixture()
        arrived, release = asyncio.Event(), asyncio.Event()
        monkeypatch.setattr('retail.amazon.ENDPOINT_TIMEOUT_MS', 500 if not cancel else 15000)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            posts = []
            async def stalled(route):
                if route.request.method == 'POST':
                    posts.append(route.request.post_data)
                    arrived.set()
                    await release.wait()
                await fixture.route(route)
            await page.route('**/*', stalled)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            await adapter.read_product(page, ITEM)
            adapter.click_bound_offer = AsyncMock(side_effect=AssertionError('No fallback'))
            operation = asyncio.create_task(adapter.cart(page, 1, TARGET))
            try:
                await asyncio.wait_for(arrived.wait(), 3)
                if cancel:
                    operation.cancel()
                with pytest.raises(asyncio.CancelledError if cancel else Attention):
                    await asyncio.wait_for(operation, 3)
                assert len(posts) == 1 and not fixture.cart_visits
                assert page._retail_offer_usage['form'] == 'dispatch_uncertain'
                assert adapter.click_bound_offer.await_count == 0
                assert await page.evaluate('!!globalThis.__retailOfferRequest') is False
            finally:
                release.set()
                await browser.close()
    asyncio.run(scenario())
