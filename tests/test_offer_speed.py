"""Offer snapshots and optional notifications never weaken checkout guards."""
import asyncio

from patchright.async_api import async_playwright

from retail.amazon import Amazon, AuthenticationRequired
from retail.interactions import InteractionError


def test_offer_snapshot_preserves_seller_controls_restrictions_and_freshness():
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            page=await browser.new_page()
            await page.route('**/*',lambda r:r.fulfill(body='<body></body>',content_type='text/html'))
            await page.goto('https://www.amazon.com/dp/B012345678')
            adapter=Amazon(None)
            recoveries=[]
            async def recovery(*args):
                recoveries.append(True)
                raise InteractionError('Fixture missing control')
            adapter.resolve_action=recovery
            base='''<span id="productTitle">Fixture</span><div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$20.00</span></span></div>'''
            button='<button id="add-to-cart-button">Add to cart</button>'
            cases=[
                (button, True, False),
                (button+'<button hidden>Add to cart</button>',True,False),
                (button+'<button disabled>Add to cart</button>',True,False),
                (button+'<button>Add to cart</button>',False,False),
                ('<fieldset disabled>'+button+'</fieldset>',False,True),
                ('<div aria-disabled="true">'+button+'</div>',False,True),
                ('<div id="availability">Currently unavailable</div>'+button,False,False),
                ('<div id="availability">Currently unavailable</div>',False,False),
                ('<div id="rightCol">This item cannot be shipped to your selected delivery location.</div>',False,False),
                ('<div id="rightCol">Sign in to get started</div>',False,False),
                ('',False,True),
            ]
            for markup,available,recovered in cases:
                recoveries.clear()
                await page.set_content(base+'<div id="merchant-info">Ships from and sold by Amazon.com.</div>'+markup)
                product=await adapter.read_product(page,{'asin':'B012345678'})
                assert product['available']==available
                assert product['price']==20 and product['seller']=='Amazon.com'
                assert bool(recoveries)==recovered
            for seller,expected in [
                ('<div id="merchant-info">Ships from Amazon.com</div>','Unknown'),
                ('<div id="merchantInfoFeature_feature_div"><span class="offer-display-feature-text-message">Other seller</span></div>','Other seller'),
                ('<span class="seller-name">Amazon.com</span><span class="seller-name">Other seller</span>','Unknown'),
                ('<span class="seller-name" hidden>Other seller</span><span class="seller-name">Amazon.com</span>','Amazon.com'),
            ]:
                await page.set_content(base+button+seller)
                product=await adapter.read_product(page,{'asin':'B012345678'})
                assert product['seller']==expected
                assert product['amazon_seller']==(expected=='Amazon.com')
            await page.locator('.a-offscreen').evaluate("e=>e.textContent='$99.00'")
            assert (await adapter.read_product(page,{'asin':'B012345678'}))['price']==99
            await page.locator('body').evaluate('''e=>e.insertAdjacentHTML('beforeend','<div id="nav-link-accountList"><span class="nav-line-1">Hello, sign in</span></div>')''')
            try:
                await adapter.read_product(page,{'asin':'B012345678'})
            except AuthenticationRequired:
                pass
            else:
                raise AssertionError('Signed-out offer must not authorize carting')
            async def repaired(*args):
                return page.locator('.repaired')
            adapter.resolve_action=repaired
            await page.set_content(base+'<button class="repaired">Custom action</button>'*2)
            assert not (await adapter.read_product(page,{'asin':'B012345678'}))['available']
            await page.locator('.repaired').last.evaluate('e=>e.remove()')
            assert (await adapter.read_product(page,{'asin':'B012345678'}))['available']
            await browser.close()
    asyncio.run(scenario())


def test_slow_offer_webhook_does_not_block_cart_or_shutdown(tmp_path,monkeypatch):
    from test_monitors import fixture, task_record, until
    async def scenario():
        store,group,account,engine=fixture(tmp_path)
        started,cancelled=asyncio.Event(),asyncio.Event()
        class Client:
            def __init__(self,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def post(self,*args,**kwargs):
                started.set()
                try:await asyncio.Event().wait()
                finally:cancelled.set()
        monkeypatch.setattr('retail.engine.httpx.AsyncClient',Client)
        store.put('settings',{'notifications':True,'webhook':'https://fixture.invalid/hook'},'settings')
        group=store.put('groups',{**group,'notify_offer':True,'offer_id':'fixture-offer'})
        engine.amazon.stock.add('B012345678')
        task=task_record(store,group,account,use_account_proxy=True)
        try:
            await engine.start(task['id'])
            await until(lambda:task['id'] not in engine.jobs)
            assert started.is_set() and not cancelled.is_set()
            assert engine.amazon.carts==[(account['id'],'B012345678')]
            assert store.get('tasks',task['id'])['status']=='completed'
        finally:
            await asyncio.wait_for(engine.close(),1)
            store.db.close()
        assert cancelled.is_set()
    asyncio.run(scenario())


def test_notification_queue_is_bounded_and_delivery_failures_are_isolated(tmp_path,monkeypatch):
    from test_monitors import fixture
    async def scenario():
        store,_,_,engine=fixture(tmp_path)
        calls=[]
        class Client:
            def __init__(self,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def post(self,*args,**kwargs):
                calls.append(True)
                raise RuntimeError('Fixture connection failure')
        monkeypatch.setattr('retail.engine.httpx.AsyncClient',Client)
        store.put('settings',{'notifications':True,'webhook':'https://fixture.invalid/hook'},'settings')
        task={'id':'fixture','simulation':False}
        try:
            for _ in range(40):await engine.notify(task,'Fixture')
            assert engine.notification_queue.qsize()==32
            await asyncio.wait_for(engine.notification_queue.join(),2)
            assert len(calls)==32
            assert all(e['status']=='notification_error' for e in store.all('events'))
            assert len(store.all('events'))==40
        finally:
            await engine.close()
            store.db.close()
    asyncio.run(scenario())
