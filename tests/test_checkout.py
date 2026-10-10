import asyncio

import pytest
from patchright.async_api import async_playwright

from retail.amazon import Amazon, Attention, AuthenticationRequired
from retail.engine import Engine
from retail.models import Group, Task
from retail.store import Store


def test_checkout_validation_browser_fixtures():
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            page=await browser.new_page()
            await page.route('https://www.amazon.com/**',lambda route:route.fulfill(body='<body>Checkout</body>',content_type='text/html'))
            await page.goto('https://www.amazon.com/gp/buy/spc/handlers/display.html')
            adapter=Amazon(None)
            html='''<body><div id="spc-orders"><div data-asin="B012345678" data-quantity="1" data-seller="Amazon.com" data-condition="new"><span class="a-price"><span class="a-offscreen">$20.00</span></span></div></div><table id="subtotals-marketplace-table"><tr><td>Order total:</td><td>$21.20</td></tr></table><input name="placeYourOrder1" type="button" value="Place order"></body>'''
            await page.set_content(html)
            snapshot=await adapter.checkout_snapshot(page,'B012345678',1,25,max_unit_price=20)
            assert snapshot['total'] == 21.20
            for asin,quantity,budget in [('B000000000',1,25),('B012345678',2,25),('B012345678',1,21)]:
                with pytest.raises(Attention):
                    await adapter.checkout_snapshot(page,asin,quantity,budget)
            for modified in [html.replace('Amazon.com','Untrusted seller'),html.replace('data-condition="new"','data-condition="used"'),html.replace('Order total:','Subtotal:'),html.replace('data-quantity="1"','')]:
                await page.set_content(modified)
                with pytest.raises(Attention):
                    await adapter.checkout_snapshot(page,'B012345678',1,25,max_unit_price=20)
            await page.set_content(html)
            with pytest.raises(Attention):
                await adapter.checkout_snapshot(page,'B012345678',1,25,max_unit_price=19)
            await browser.close()
    asyncio.run(scenario())


def test_ambiguous_order_submission_never_retries(tmp_path):
    class Page:
        async def bring_to_front(self): pass
        async def close(self): pass
    class Context:
        async def new_page(self): return Page()
        async def close(self): pass
    class Adapter:
        submitted=0
        async def context(self,*args):return Context()
        async def ensure_session(self,*args):pass
        async def inspect(self,*args):return {'asin':'B012345678','title':'fixture','price':20,'available':True,'amazon_seller':True,'condition':'new','offer_id':''}
        async def cart(self,*args):return 1
        async def prepare_checkout(self,*args):pass
        async def checkout_snapshot(self,*args,**kwargs):return {'asin':'B012345678','quantity':1,'total':21,'currency':'USD'}
        async def submit_order(self,*args):
            self.submitted+=1
            raise TimeoutError('Response was lost after sending the order')
        async def close(self):pass
    async def scenario():
        store=Store(tmp_path)
        group=store.put('groups',Group(name='test',products='B012345678;25').model_dump())
        account=store.put('accounts',{'name':'fixture','region':'US','session':{'cookies':[]}})
        task=store.put('tasks',{**Task(group_id=group['id'],account_id=account['id'],simulation=False,checkout_mode='automatic').model_dump(mode='json'),'status':'idle'})
        engine=Engine(store)
        adapter=Adapter()
        engine.amazon=adapter
        await engine.start(task['id'])
        for _ in range(100):
            if store.get('tasks',task['id'])['status']=='attention':break
            await asyncio.sleep(.01)
        assert adapter.submitted == 1
        assert store.get('submissions','submission-'+task['id'])['status'] == 'submitting'
        assert len(store.all('harvesters')) == 1
        engine.resume(task['id'])
        await asyncio.gather(*list(engine.jobs.values()))
        assert store.all('checkouts') == []
        assert adapter.submitted == 1
        with pytest.raises(ValueError,match='submission record'):
            await engine.start(task['id'])
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('interrupted_stage', ['cart', 'review'])
def test_resume_authentication_rechecks_interrupted_checkout(tmp_path, interrupted_stage):
    class Page:
        def __init__(self, context):
            self.context = context

        def is_closed(self):
            return False

        async def close(self):
            pass

    class Context:
        async def new_page(self):
            return Page(self)

        async def close(self):
            pass

    class Adapter:
        def __init__(self):
            self.login_checks = 0
            self.carts = 0
            self.reviews = 0
            self.submissions = 0

        async def context(self, *args):
            return Context()

        async def ensure_session(self, *args):
            self.login_checks += 1
            if self.login_checks == 2:
                raise AuthenticationRequired('Session expired')

        async def inspect(self, *args):
            return {'asin': 'B012345678', 'title': 'Fixture item', 'price': 20,
                    'original_price': 25, 'available': True, 'amazon_seller': True,
                    'condition': 'new', 'offer_id': ''}

        async def cart(self, *args):
            self.carts += 1
            if interrupted_stage == 'cart' and self.carts == 1:
                raise AuthenticationRequired('Session expired')
            return 1

        async def prepare_checkout(self, *args):
            self.reviews += 1
            if interrupted_stage == 'review' and self.reviews == 1:
                raise AuthenticationRequired('Session expired')

        async def checkout_snapshot(self, *args, **kwargs):
            return {'total': 21, 'quantity': 1, 'asin': 'B012345678', 'currency': 'USD'}

        async def submit_order(self, *args):
            self.submissions += 1

        async def close(self):
            pass

    async def scenario():
        store = Store(tmp_path)
        group = store.put('groups', Group(name='Recovery', products='B012345678;25').model_dump())
        account = store.put('accounts', {'name': 'Fixture', 'region': 'US', 'session': {'cookies': []}})
        task = store.put('tasks', Task(group_id=group['id'], account_id=account['id'], simulation=False,
                                       checkout_mode='quote').model_dump(mode='json'))
        engine = Engine(store)
        adapter = Adapter()
        engine.amazon = adapter
        await engine.start(task['id'])
        for _ in range(200):
            if store.get('tasks', task['id'])['status'] == 'attention':
                break
            await asyncio.sleep(.01)
        assert store.get('tasks', task['id'])['status'] == 'attention', store.get('tasks', task['id'])['message']
        assert adapter.login_checks == 2
        engine.resume(task['id'])
        await asyncio.wait_for(asyncio.gather(*list(engine.jobs.values())), 10)
        assert store.get('tasks', task['id'])['status'] == 'completed'
        assert adapter.login_checks == 3
        assert adapter.carts == 2
        assert adapter.submissions == 0
        assert store.all('quotes')[0]['total'] == 21
        await engine.close()
        store.db.close()

    asyncio.run(scenario())
