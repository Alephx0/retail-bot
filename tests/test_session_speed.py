"""Session/product handoff on canonical Amazon URLs, entirely local fixtures."""
import asyncio
import pytest
from patchright.async_api import async_playwright
from retail.amazon import Amazon, AuthenticationRequired, ChallengeDetected, same_product_page
from retail.store import Store

TARGET = 'https://www.amazon.com/dp/B012345678'
CANONICAL = 'https://www.amazon.com/Fixture-Product/dp/B012345678?th=1&psc=1'


@pytest.mark.parametrize('url,expected', [
    (CANONICAL, True), (TARGET + '/ref=example', True),
    ('https://www.amazon.com/gp/product/B012345678?ref=x', True),
    ('https://www.amazon.ca/dp/B012345678', False),
    ('https://www.amazon.com/dp/B012345679', False),
    ('https://www.amazon.com/ap/signin?return_to=' + TARGET, False),
    ('https://www.amazon.com.evil.invalid/dp/B012345678', False),
    ('https://user@www.amazon.com/dp/B012345678', False),
])
def test_product_identity(url, expected):
    assert same_product_page(url, TARGET) == expected


@pytest.mark.parametrize('delay', [0, 300])
def test_canonical_handoff_reads_live_offer_once_without_reloading(tmp_path, delay):
    async def scenario():
        store = Store(tmp_path)
        account = store.put('accounts', {'name': 'Fixture', 'region': 'US'})
        adapter = Amazon(store)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            requests = []
            async def route(r):
                requests.append(r.request.url)
                await r.fulfill(content_type='text/html', body=f'''<a id="nav-link-accountList"><span class="nav-line-1"></span></a>
                    <script>history.replaceState(null,'','{CANONICAL}');setTimeout(()=>document.querySelector('.nav-line-1').textContent='Hello Fixture',{delay})</script>
                    <span id="productTitle">Fixture product</span><button>Add to cart</button>
                    <div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$20.00</span></span></div>''')
            await page.route('**/*', route)
            page._retail_start_url = TARGET
            await adapter.ensure_session(page.context, account, page)
            # Fresh DOM data, not an offer cached during session verification.
            await page.locator('.a-offscreen').evaluate("e => e.textContent='$19.00'")
            offer = await adapter.inspect(page, {'asin':'B012345678'}, 'US')
            assert offer['price'] == 19
            assert requests == [TARGET]
            # Handoff is single-use; later monitoring does a fresh navigation.
            await adapter.inspect(page, {'asin':'B012345678'}, 'US')
            assert requests == [TARGET, TARGET]
            assert store.get('accounts', account['id'])['logged_in']
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_resumed_auth_returning_to_product_avoids_second_navigation(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        account = store.put('accounts', {'name':'Fixture', 'region':'US'})
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            requests = []
            async def route(r):
                requests.append(r.request.url)
                await r.fulfill(content_type='text/html', body='<div id="nav-link-accountList"><span class="nav-line-1">Hello Fixture</span></div><span id="productTitle">Fixture product</span><button>Add to cart</button>')
            await page.route('**/*', route)
            await page.goto('https://www.amazon.com/ap/signin')
            page._retail_start_url = TARGET
            adapter = Amazon(store)
            async def authenticate(page, account):
                await page.goto(CANONICAL)
            adapter.authenticate = authenticate
            await adapter.ensure_session(page.context, account, page)
            await adapter.inspect(page, {'asin':'B012345678'}, 'US')
            assert requests == ['https://www.amazon.com/ap/signin', CANONICAL]
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('markup,error', [
    ('<div id="nav-link-accountList"><span class="nav-line-1" hidden>Hello Fixture</span></div>', AuthenticationRequired),
    ('<input id="captchacharacters">', ChallengeDetected),
    ('<div id="nav-link-accountList"><span class="nav-line-1">Hello Fixture</span></div><p>Verify your identity</p>', ChallengeDetected),
])
def test_missing_or_blocked_session_is_never_saved(tmp_path, markup, error):
    async def scenario():
        store = Store(tmp_path)
        account = store.put('accounts', {'name':'Fixture', 'region':'US'})
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            page.set_default_timeout(300)
            await page.route('**/*', lambda r: r.fulfill(content_type='text/html', body=markup))
            page._retail_start_url = TARGET
            with pytest.raises(error):
                await Amazon(store).ensure_session(page.context, account, page)
            assert not store.get('accounts', account['id']).get('logged_in')
            await browser.close()
        store.db.close()
    asyncio.run(scenario())
