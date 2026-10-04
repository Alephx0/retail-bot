import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from retail.app import create_app
from retail.resources import Resources
from retail.store import Store, now
from retail.analytics import report

HEADERS={'X-Retail-Client':'dashboard'}


def test_memberships_reference_canonical_items_and_assignment_preview(tmp_path):
    with TestClient(create_app(tmp_path),headers=HEADERS) as client:
        account=client.post('/api/accounts',json={'name':'Account','email':'fixture@example.com'}).json()
        profile=client.post('/api/profiles',json={'name':'Profile'}).json()
        folder=client.post('/api/folders',json={'name':'Family','resource_kind':'profiles'}).json()
        assert client.post('/api/organization/members',json={'folder_id':folder['id'],'ids':[profile['id']]}).status_code==200
        assert client.post('/api/organization/members',json={'folder_id':folder['id'],'ids':[account['id']]}).status_code==422
        client.post('/api/organization/members',json={'folder_id':folder['id'],'ids':[profile['id']]})
        state=client.get('/api/state').json()
        assert len(state['profiles'])==1 and len(state['memberships'])==1
        group=client.post('/api/groups',json={'name':'Group','products':'B012345678;25'}).json()
        config={'group_id':group['id'],'profile_group_id':folder['id'],'match_profiles':True,'count':2,'simulation':True}
        assert len(client.post('/api/assignments/preview',json=config).json()['errors'])==2
        client.post('/api/organization/relationship',json={'account_id':account['id'],'profile_id':profile['id']})
        preview=client.post('/api/assignments/preview',json=config).json()
        assert not preview['errors']
        assert all(row['account_id']==account['id'] and row['profile_id']==profile['id'] for row in preview['rows'])
        assert client.post('/api/assignments/create',json={**config,'preview':[]}).status_code==422
        created=client.post('/api/assignments/create',json={**config,'preview':preview['rows']})
        assert len(created.json()['created'])==2
        client.delete('/api/folders/'+folder['id'])
        assert len(client.get('/api/state').json()['profiles'])==1


def test_analytics_separate_simulation_currency_and_unverified_payment(tmp_path):
    store=Store(tmp_path);start=datetime.now(timezone.utc)-timedelta(days=1);end=datetime.now(timezone.utc)+timedelta(days=1)
    order={'at':now(),'quantity':2,'total':50,'unit_price':20,'reference_price':30,'currency':'USD','simulation':False,'status':'confirmation_detected'}
    store.put('checkouts',order)
    store.put('checkouts',{**order,'simulation':True})
    store.put('checkouts',{**order,'status':'payment_verification'})
    store.put('checkouts',{**order,'currency':'GBP'})
    store.put('task_events',{'at':now(),'event':'CHECKOUT_FAILED','simulation':False})
    value=report(store,start,end)
    assert (value['spent'],value['saved'],value['checkouts'],value['failures'])==(50,20,1,1)
    assert len(value['orders'])==2
    store.db.close()


def test_monitor_partial_failure_and_semantic_control_fallback():
    from retail.adapters import MonitorService
    from retail.interactions import resolve, InteractionError
    from patchright.async_api import async_playwright
    import pytest
    class Adapter:
        async def inspect(self,page,item,region):
            if item['asin']=='bad': raise TimeoutError()
            return {'price':12,'available':True,'seller':'Amazon','offer_id':'offer'}
    async def scenario():
        values=await MonitorService(Adapter()).scan([None,None],[{'asin':'bad'},{'asin':'good'}],'US')
        assert isinstance(values[0],TimeoutError) and values[1].availability=='available'
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            page=await browser.new_page()
            await page.set_content('<button aria-label="Add to Cart">New markup</button>')
            assert await (await resolve(page,'ADD_TO_CART')).inner_text()=='New markup'
            await page.set_content('<button>Add to Cart</button><button>Add to Cart</button>')
            with pytest.raises(InteractionError):await resolve(page,'ADD_TO_CART')
            await browser.close()
    asyncio.run(scenario())


def test_folder_migration_idempotent(tmp_path):
    store=Store(tmp_path);record=store.put('accounts',{'name':'Account','group':'Family'})
    resources=Resources(store);resources.migrate();resources.migrate()
    folder=store.all('folders')[0]
    assert resources.members(folder['id'])==[record['id']]
    assert len(store.all('accounts'))==1
    store.db.close()


def test_live_loop_only_continues_after_confirmed_order_and_is_bounded(tmp_path):
    from retail.engine import Engine
    from retail.models import Group, Task
    class Page:
        async def close(self): pass
        async def bring_to_front(self): pass
    class Context:
        async def new_page(self): return Page()
        async def close(self): pass
    class Adapter:
        submissions=0
        async def context(self,*args): return Context()
        async def ensure_session(self,*args): pass
        async def inspect(self,*args): return {'asin':'B012345678','price':20,'available':True,'amazon_seller':True,'condition':'new','offer_id':'','title':'Fixture','original_price':25}
        async def cart(self,*args): return 1
        async def prepare_checkout(self,*args): pass
        async def checkout_snapshot(self,*args,**kwargs): return {'total':21,'quantity':1,'asin':'B012345678','currency':'USD'}
        async def submit_order(self,*args): self.submissions+=1
        async def confirmation(self,*args): return '000-0000000-'+str(self.submissions).zfill(7)
        async def payment_verification(self,*args): return False
        async def close(self): pass
    async def scenario():
        store=Store(tmp_path);engine=Engine(store);engine.amazon=Adapter()
        account=store.put('accounts',{'name':'Fixture','region':'US','session':{'cookies':[]}})
        group=store.put('groups',Group(name='Loop',products='B012345678;25',loop=True,max_checkouts=2,delay_ms=3500).model_dump(mode='json'))
        task=store.put('tasks',{**Task(group_id=group['id'],account_id=account['id'],simulation=False,checkout_mode='automatic').model_dump(mode='json'),'status':'idle'})
        await engine.start(task['id'])
        await asyncio.wait_for(asyncio.gather(*engine.jobs.values()),10)
        assert engine.amazon.submissions==2
        assert len(store.all('checkouts'))==2
        assert store.get('tasks',task['id'])['state']=='SUCCESS'
        import pytest
        with pytest.raises(ValueError,match='submission record'):await engine.start(task['id'])
        await engine.close();store.db.close()
    asyncio.run(scenario())


def test_live_quote_reaches_final_review_without_submitting(tmp_path):
    from retail.engine import Engine
    from retail.models import Group, Task
    class Page:
        async def close(self): pass
        async def bring_to_front(self): pass
    class Context:
        async def new_page(self): return Page()
        async def close(self): pass
    class Adapter:
        submitted = False
        carts = 0
        prepared = 0
        async def context(self,*args): return Context()
        async def ensure_session(self,*args): pass
        async def inspect(self,*args): return {'asin':'B012345678','price':20,'available':True,'amazon_seller':True,'condition':'new','offer_id':'','title':'Fixture','original_price':25}
        async def buy_now(self,*args): return False
        async def cart(self,*args): self.carts+=1; return 1
        async def prepare_checkout(self,*args): self.prepared+=1
        async def checkout_snapshot(self,*args,**kwargs): return {'total':23.50,'quantity':1,'asin':'B012345678','currency':'USD','price_components':[{'label':'Shipping','amount':3.50}]}
        async def submit_order(self,*args): self.submitted=True
        async def close(self): pass
    async def scenario():
        store=Store(tmp_path);engine=Engine(store);engine.amazon=Adapter()
        account=store.put('accounts',{'name':'Fixture','region':'US','session':{'cookies':[]}})
        group=store.put('groups',Group(name='Quote',products='B012345678;25').model_dump(mode='json'))
        task=store.put('tasks',{**Task(group_id=group['id'],account_id=account['id'],simulation=False,checkout_mode='quote',use_buy_now=True).model_dump(mode='json'),'status':'idle'})
        await engine.start(task['id'])
        await asyncio.wait_for(asyncio.gather(*engine.jobs.values()),10)
        assert not engine.amazon.submitted
        assert engine.amazon.carts == 1 and engine.amazon.prepared == 1
        assert store.get('tasks',task['id'])['status']=='completed'
        assert store.all('checkouts') == []
        assert store.all('quotes')[0]['total'] == 23.50
        await engine.close();store.db.close()
    asyncio.run(scenario())


def test_sticky_proxy_routes_and_redaction(tmp_path):
    from retail.proxy_pool import ProxyPool
    from retail.services import proxy_fingerprint
    store=Store(tmp_path)
    pool=store.put('proxies',{'name':'Pool','entries':'first.example:8080:user:secret\nsecond.example:8080'})
    store.put('proxy_health',{'status':'completed','results':[{'fingerprint':proxy_fingerprint('second.example:8080'),'status':'reachable'}]},'health-'+pool['id'])
    selector=ProxyPool(store);assert selector.choose(pool['id'],'account')=='second.example:8080'
    store.put('proxy_health',{'status':'completed','results':[]},'health-'+pool['id'])
    assert selector.choose(pool['id'],'account')=='second.example:8080', 'An existing session keeps its explicit route'
    import pytest
    with pytest.raises(ValueError,match='No reachable proxies'):selector.choose(pool['id'],'new-account')
    selector.sync();assert len(store.all('proxy_endpoints'))==2
    store.db.close()


def test_cancelling_queued_task_does_not_release_another_accounts_lock(tmp_path):
    from retail.engine import Engine
    from retail.models import Group, Task
    class Page:
        async def close(self): pass
    class Context:
        async def new_page(self): return Page()
        async def close(self): pass
    class Adapter:
        async def context(self,*args):return Context()
        async def ensure_session(self,*args):await asyncio.Event().wait()
        async def close(self):pass
    async def scenario():
        store=Store(tmp_path);engine=Engine(store);engine.amazon=Adapter()
        group=store.put('groups',Group(name='Queue',products='B012345678').model_dump(mode='json'))
        account=store.put('accounts',{'name':'Fixture','region':'US','session':{'cookies':[]}})
        task=Task(group_id=group['id'],account_id=account['id'],simulation=False).model_dump(mode='json')
        first=store.put('tasks',task);second=store.put('tasks',task)
        await engine.start(first['id']);await asyncio.sleep(.03)
        await engine.start(second['id']);await asyncio.sleep(.03)
        assert store.get('tasks',second['id'])['state']=='IN_QUEUE'
        await engine.stop(second['id'])
        assert engine.account_locks[account['id']].locked()
        await engine.stop(first['id'])
        assert not engine.account_locks[account['id']].locked()
        await engine.close();store.db.close()
    asyncio.run(scenario())