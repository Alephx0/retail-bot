"""Isolated checkout-recovery drill; all retailer requests stay in the fixture."""
from patchright.async_api import async_playwright

from .amazon import Amazon
from .browser_agent import BrowserAgent
from .browser_mcp import AMAZON_ACTIONS


ASIN = 'B012345678'
HOST = 'https://www.amazon.com'


class CheckStore:
    def __init__(self, connection):
        self.connection = connection
        self.records = {}

    def get(self, table, key):
        if table == 'settings':
            return {'ai_connection_id': self.connection['id'], 'agent_mode': 'recovery',
                    'agent_max_steps': 4, 'agent_timeout_seconds': 60}
        if table == 'ai_connections' and key == self.connection['id']:
            return self.connection
        return None

    def all(self, table):
        return self.records.get(table, [])

    def put(self, table, record, id=None):
        self.records.setdefault(table, []).append(record)
        return record

    def put_bounded(self, table, record, id=None, **kwargs):
        return self.put(table, record, id)


def fixture(path):
    if path.startswith('/dp/'):
        return '<h1>Fixture item</h1><button data-new-control="cart" onclick="location.href=\'/gp/cart/view.html\'">Add to bag</button>'
    if path == '/gp/cart/view.html':
        return (f'<main><section><div data-asin="{ASIN}">Fixture item · Quantity: 1</div></section>'
                '<aside><div><button data-new-control="checkout" onclick="location.href=\'/checkout/byg\'">Proceed to checkout</button></div></aside></main>')
    if path == '/checkout/byg':
        return ('<dialog open style="position:fixed;inset:0;background:white"><button onclick="this.closest(\'dialog\').remove()">No, thanks</button><button>Add</button></dialog>'
                '<a href="/checkout/p/example/spc">Continue to checkout</a>'
                '<a href="/checkout/p/example/spc">Continue to checkout</a>')
    if path == '/checkout/p/example/spc':
        return (f'<div id="spc-orders"><div data-asin="{ASIN}" data-quantity="1" data-seller="Amazon.com" data-condition="new"><span class="a-price"><span class="a-offscreen">$20.00</span></span>Fixture item</div></div>'
                '<ul><li>Items: $20.00</li><li>Shipping &amp; handling: $1.20</li><li>Grand total: $21.20</li></ul>'
                '<input type="submit" name="placeYourOrder1" value="Place your order" onclick="location.href=\'/gp/buy/thankyou/handlers/display.html\'">')
    if path == '/gp/buy/thankyou/handlers/display.html':
        return '<h1>Order placed, thanks!</h1><p>123-4567890-1234567</p>'
    raise ValueError('Checkout fixture reached an unexpected path: ' + path)


async def check_recovery(connection, provider_factory=None):
    """Exercise changed controls, popup and total through actual MCP/CDP tools.

    The purchase control only reaches a local simulated confirmation page.
    """
    scenarios = []
    stage = 'launch'
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True)
        try:
            context = await browser.new_context(service_workers='block')

            async def route(request):
                from urllib.parse import urlsplit
                url = urlsplit(request.request.url)
                if url.hostname != 'www.amazon.com':
                    await request.abort()
                    return
                await request.fulfill(content_type='text/html', body=fixture(url.path))

            await context.route('**/*', route)
            page = await context.new_page()
            await page.goto(HOST + '/dp/' + ASIN)
            session = await context.new_cdp_session(page)
            try:
                if not (await session.send('Accessibility.getFullAXTree')).get('nodes'):
                    raise ValueError('CDP accessibility is unavailable')
            finally:
                await session.detach()
            scenarios.append({'name': 'CDP browser inspection', 'ok': True})
            store = CheckStore(connection)
            kwargs = {'provider_factory': provider_factory} if provider_factory else {}
            agent = BrowserAgent(store, **kwargs)
            for stage, action, next_path in [
                ('changed add-to-cart button', 'ADD_TO_CART', '/gp/cart/view.html'),
                ('rearranged cart layout and changed checkout button', 'BEGIN_CHECKOUT', '/checkout/byg'),
            ]:
                control = await agent.resolve(page, action, {'www.amazon.com'}, AMAZON_ACTIONS)
                await control.click()
                await page.wait_for_url('**' + next_path)
                scenarios.append({'name': stage, 'ok': True})
            stage = 'unexpected checkout popup'
            control = await agent.resolve(page, 'DISMISS_CHECKOUT_OFFER', {'www.amazon.com'}, AMAZON_ACTIONS)
            await control.click()
            if await page.locator('dialog[open]').count():
                raise ValueError('Popup remained open')
            scenarios.append({'name': stage, 'ok': True})
            stage = 'duplicate checkout links'
            control = await Amazon(store).resolve_action(page, 'CONTINUE_CHECKOUT')
            await control.click()
            await page.wait_for_url('**/checkout/p/example/spc')
            scenarios.append({'name': stage, 'ok': True})
            stage = 'renamed final order total'
            adapter = Amazon(store)
            adapter.agent = agent
            result = await adapter.checkout_snapshot(page, ASIN, 1, 25)
            if result['total'] != 21.20 or not page.url.endswith('/spc'):
                raise ValueError('Final total was incorrect or review was left prematurely')
            scenarios.append({'name': stage, 'ok': True})
            stage = 'simulated order confirmation'
            await adapter.submit_order(page)
            if await adapter.confirmation(page) != '123-4567890-1234567':
                raise ValueError('Simulated checkout confirmation was not detected')
            scenarios.append({'name': stage, 'ok': True})
            return {'ok': True, 'message': 'Bounded AI proposals and deterministic CDP controls recovered through the local checkout fixture, verified the $21.20 total and detected simulated order confirmation. No Amazon account or real purchase was used.', 'scenarios': scenarios}
        except Exception as exc:
            scenarios.append({'name': stage, 'ok': False})
            return {'ok': False, 'message': f'Browser recovery failed at {stage}: {exc}', 'scenarios': scenarios}
        finally:
            await browser.close()
