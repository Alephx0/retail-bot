import asyncio
from datetime import datetime, timezone

import pytest

from retail.engine import Engine
from retail.store import Store
from retail.task_groups.coordinator import Coordinator
from retail.task_groups.domain import Plan


class Page:
    def __init__(self): self.closed=False; self.url='https://www.amazon.com/'
    def is_closed(self): return self.closed


class Context:
    def __init__(self): self.page=Page()
    async def new_page(self): return self.page
    async def close(self): self.page.closed=True


class Adapter:
    def __init__(self):
        self.logins={}; self.fingerprint_tests={}; self.inspections=0; self.submissions=0
        self.contexts=0; self.authentications=0; self.carts=0
        self.fail_cart=False; self.fail_submit=False; self.before_submit=lambda:None
    async def context(self,account): self.contexts+=1; return Context()
    async def ensure_session(self,*args): self.authentications+=1
    async def inspect(self,page,item,region):
        self.inspections+=1
        return {'asin':item['asin'],'available':True,'price':75,'amazon_seller':True,'seller':'Amazon','condition':'new','offer_id':item.get('offer_id',''),'title':'Fixture'}
    async def cart(self,page,quantity,asin):
        self.carts+=1
        if self.fail_cart: raise TimeoutError('Cart outcome unknown')
        return quantity
    async def prepare_checkout(self,*args): pass
    async def checkout_snapshot(self,page,asin,quantity,*args,**kwargs):
        return {'asin':asin,'quantity':quantity,'total':80,'currency':'USD'}
    async def submit_order(self,page):
        self.before_submit(); self.submissions+=1
        if self.fail_submit: raise TimeoutError('Retailer response lost')
    async def confirmation(self,page): return 'ORDER-123'
    async def payment_verification(self,page): return False
    async def expose(self,page): pass
    async def close(self): pass


def setup(tmp_path,action='automatic',clock=None,**overrides):
    store=Store(tmp_path); engine=Engine(store); adapter=Adapter(); engine.amazon=adapter
    c=Coordinator(engine,clock); engine.group_coordinator=c
    account=store.put('accounts',{'name':'Fixture','region':'US','retailer':'amazon','session':{'cookies':[]}},'account')
    plan=Plan(name='Fixture',simulation=False,action=action,account_ids=[account['id']],
              products=[{'product_id':'B012345678','max_unit_cents':8000}],**overrides).model_dump(mode='json')
    group=c.repo.save_plan(plan); run=c.repo.start(group['id'],'start',current=clock() if clock else None)
    return store,adapter,c,account,run


def test_live_fixture_submission_observes_durable_intent(tmp_path):
    async def scenario():
        store,adapter,c,account,run=setup(tmp_path)
        a=c.repo.reserve(run['id'],account['id'],'B012345678',0,{'price':75})
        def check_intent():
            saved=c.repo.require('attempt',a['id'])
            assert saved['state']=='submitting' and saved['intent']['snapshot']['total']==80
        adapter.before_submit=check_intent
        await c.executor.run(a)
        assert adapter.submissions==1 and adapter.carts==1
        assert c.repo.require('attempt',a['id'])['state']=='confirmed'
        assert c.repo.progress(run)['spent_cents']==8000
        assert store.all('checkouts')[0]['order_id']=='ORDER-123'
        await c.close();store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary',['cart','submit'])
def test_unknown_mutation_never_retries_or_releases_account(tmp_path,boundary):
    async def scenario():
        store,adapter,c,account,run=setup(tmp_path)
        adapter.fail_cart=boundary=='cart';adapter.fail_submit=boundary=='submit'
        a=c.repo.reserve(run['id'],account['id'],'B012345678',0,{'price':75})
        await c.executor.run(a)
        assert c.repo.require('attempt',a['id'])['state']=='reconciliation_required'
        assert c.repo.claimed(account['id'])==a['id']
        await c.inspect(run,account['id'],run['plan']['products'][0])
        assert adapter.carts==1 and adapter.submissions==(1 if boundary=='submit' else 0)
        assert c.repo.progress(run)['reserved_units']==1
        await c.close();store.db.close()
    asyncio.run(scenario())


def test_read_only_filters_do_not_open_checkout_or_pause_group(tmp_path):
    async def scenario():
        store,adapter,c,account,run=setup(tmp_path)
        original=adapter.inspect
        async def expensive(*args): return {**await original(*args),'price':100}
        adapter.inspect=expensive
        await c.scan(run)
        assert c.repo.require('run',run['id'])['state']=='watching'
        assert c.repo.all('observation',run['id'])[0]['reason']=='above_price_cap'
        assert adapter.carts==0 and not c.repo.attempts(run['id'])
        await c.close();store.db.close()
    asyncio.run(scenario())


def test_same_account_monitor_cache_is_shared_but_other_accounts_are_isolated(tmp_path):
    async def scenario():
        store,adapter,c,account,run=setup(tmp_path,action='notify')
        second=c.repo.save_plan({**run['plan'],'name':'Second'})
        other=c.repo.start(second['id'],'start')
        await c.scan(run);await c.scan(other)
        assert adapter.inspections==1 and adapter.contexts==1
        # Session bookkeeping timestamps do not recreate a browser profile.
        account['last_login']='changed';store.put('accounts',account,account['id'])
        c.cache.clear();await c.scan(run)
        assert adapter.contexts==1 and adapter.authentications==1
        second_account=store.put('accounts',{**account,'name':'Other'},'other-account')
        third=c.repo.save_plan({**run['plan'],'account_ids':[second_account['id']]})
        await c.scan(c.repo.start(third['id'],'start'))
        assert adapter.inspections==3 and adapter.contexts==2
        assert adapter.carts==0
        await c.close();store.db.close()
    asyncio.run(scenario())


def test_timed_preparation_does_not_inspect_or_cart_before_window(tmp_path):
    async def scenario():
        moment=[datetime(2026,10,9,13,54,tzinfo=timezone.utc)]
        store,adapter,c,account,run=setup(tmp_path,action='notify',clock=lambda:moment[0],
            schedule={'kind':'once','date':'2026-10-09','start':'10:00','end':'10:30','timezone':'America/New_York','prepare_minutes':5})
        await c.tick();assert c.repo.require('run',run['id'])['state']=='scheduled'
        moment[0]=datetime(2026,10,9,13,56,tzinfo=timezone.utc)
        await c.tick();await asyncio.gather(*c.scans.values())
        assert c.repo.require('run',run['id'])['state']=='preparing'
        assert adapter.authentications==1 and adapter.inspections==adapter.carts==0
        moment[0]=datetime(2026,10,9,14,0,tzinfo=timezone.utc);c.next_scan.clear()
        await c.tick();await asyncio.gather(*c.scans.values())
        assert c.repo.require('run',run['id'])['state']=='watching'
        assert adapter.inspections==1
        moment[0]=datetime(2026,10,9,14,31,tzinfo=timezone.utc)
        await c.tick();assert c.repo.require('run',run['id'])['state']=='stopped'
        await c.close();store.db.close()
    asyncio.run(scenario())


def test_review_waits_for_user_and_requires_manual_total_reconciliation(tmp_path):
    async def scenario():
        store,adapter,c,account,run=setup(tmp_path,action='review')
        a=c.repo.reserve(run['id'],account['id'],'B012345678',0,{'price':75})
        job=asyncio.create_task(c.executor.run(a))
        for _ in range(100):
            if a['id'] in c.wakes:break
            await asyncio.sleep(.001)
        assert c.repo.require('attempt',a['id'])['state']=='waiting_user'
        assert adapter.submissions==0
        c.wakes[a['id']].set();await job
        assert c.repo.require('attempt',a['id'])['state']=='reconciliation_required'
        assert adapter.submissions==0
        await c.close();store.db.close()
    asyncio.run(scenario())


def test_uncertain_account_browser_is_not_evicted_for_capacity(tmp_path):
    from retail.task_groups.repository import Conflict
    async def scenario():
        store,adapter,c,account,run=setup(tmp_path)
        c.engine.browser_slots=asyncio.Semaphore(1)
        adapter.fail_cart=True
        a=c.repo.reserve(run['id'],account['id'],'B012345678',0,{'price':75})
        await c.executor.run(a)
        page=c.pool.sessions[account['id']]['page']
        other=store.put('accounts',{**account,'name':'Other'},'other-account')
        with pytest.raises(Conflict,match='browser slot'):
            async with c.pool.lease(other): pass
        assert not page.is_closed()
        await c.pool.trim(all_idle=True)
        assert not page.is_closed()
        await c.close();store.db.close()
    asyncio.run(scenario())


def test_stop_all_disarms_recurring_schedule_between_completed_windows(tmp_path):
    async def scenario():
        moment=datetime(2026,10,9,14,0,tzinfo=timezone.utc)
        store,adapter,c,account,run=setup(tmp_path,clock=lambda:moment,
            schedule={'kind':'weekly','weekdays':[4],'start':'10:00','end':'10:30','quota_scope':'window'})
        c.repo.run_state(run['id'],'completed','Window goal fulfilled')
        assert c.repo.require('group',run['group_id'])['armed']
        assert not c.active()
        assert await c.stop_all()==1
        assert not c.repo.require('group',run['group_id'])['armed']
        await c.close();store.db.close()
    asyncio.run(scenario())
