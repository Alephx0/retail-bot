"""Ready browsers keep account isolation and never authorize early purchases."""
import asyncio
from datetime import datetime,timezone,timedelta
from unittest.mock import AsyncMock

from retail.scheduling import preparations
from test_monitors import fixture,task_record,until


def setup(tmp_path,**settings):
    store,group,account,engine=fixture(tmp_path)
    store.put('settings',{'keep_accounts_ready':True,**settings},'settings')
    return store,group,account,engine


def restock(engine,asin='B012345678'):
    engine.amazon.stock.add(asin)
    for monitor in engine.monitors.active.values():
        if monitor.item['asin']==asin and monitor.product:
            monitor.product['available']=True
            monitor.sequence+=1
            monitor.update('in_stock','Fixture restock')


def test_standby_reuses_browser_skips_probe_and_reads_fresh_offer(tmp_path):
    async def scenario():
        store,group,account,engine=setup(tmp_path)
        engine.amazon.probe_session=AsyncMock(return_value='authenticated')
        task=task_record(store,group,account,use_account_proxy=True,monitor_asin='B012345678')
        try:
            await engine.start(task['id'])
            await until(lambda:bool(engine.ready_accounts.get(task['id'],{}).get('idle')))
            assert store.get('tasks',task['id'])['account_ready']
            assert engine.account_locks[account['id']].locked()
            restock(engine)
            await until(lambda:task['id'] not in engine.jobs)
            assert engine.amazon.authentications==[account['id']]
            assert engine.amazon.probe_session.await_count==0
            assert engine.amazon.carts==[(account['id'],'B012345678')]
            assert len([c for c in engine.amazon.contexts if c.identity['id']==account['id']])==1
            assert engine.browser_slots._value==10
            assert not engine.ready_accounts
        finally:
            await engine.close();store.db.close()
    asyncio.run(scenario())


def test_waiting_account_yields_the_only_slot_to_another_task(tmp_path):
    async def scenario():
        store,group,account,engine=setup(tmp_path,max_running_tasks=1)
        engine.browser_slots=asyncio.Semaphore(1)
        other=store.put('accounts',{**account,'id':'second','name':'Second'})
        a=task_record(store,group,account,use_account_proxy=True,monitor_asin='B012345678')
        b=task_record(store,group,other,use_account_proxy=True,monitor_asin='B087654321')
        try:
            await engine.start(a['id'])
            await until(lambda:bool(engine.ready_accounts.get(a['id'],{}).get('idle')))
            engine.amazon.stock.add('B087654321')
            await engine.start(b['id'])
            await until(lambda:b['id'] not in engine.jobs)
            assert engine.amazon.carts==[(other['id'],'B087654321')]
            assert not store.get('tasks',a['id'])['account_ready']
            assert not engine.account_locks[account['id']].locked()
            assert engine.browser_slots._value==1
            restock(engine)
            await until(lambda:a['id'] not in engine.jobs)
            assert engine.amazon.carts[-1]==(account['id'],'B012345678')
        finally:
            await engine.close();store.db.close()
    asyncio.run(scenario())


def test_account_change_invalidates_ready_browser_before_cart(tmp_path):
    async def scenario():
        store,group,account,engine=setup(tmp_path)
        task=task_record(store,group,account,use_account_proxy=True,monitor_asin='B012345678')
        try:
            await engine.start(task['id'])
            await until(lambda:bool(engine.ready_accounts.get(task['id'],{}).get('idle')))
            store.put('accounts',{**account,'fingerprint_seed':'new-fixture-seed'})
            restock(engine)
            await until(lambda:task['id'] not in engine.jobs)
            contexts=[c for c in engine.amazon.contexts if c.identity['id']==account['id']]
            assert len(contexts)==2 and contexts[0].closed
            assert contexts[1].identity['fingerprint_seed']=='new-fixture-seed'
            assert len(engine.amazon.carts)==1
        finally:
            await engine.close();store.db.close()
    asyncio.run(scenario())


def test_scheduled_readiness_never_carts_before_activation_and_stop_cleans_up(tmp_path):
    async def scenario():
        store,group,account,engine=setup(tmp_path)
        task=task_record(store,group,account,use_account_proxy=True,monitor_asin='B012345678')
        engine.amazon.stock.add('B012345678')
        due=datetime.now(timezone.utc)+timedelta(seconds=.4)
        checks=[]
        cart=engine.amazon.cart
        async def timed_cart(*args):
            checks.append(datetime.now(timezone.utc)>=due)
            return await cart(*args)
        engine.amazon.cart=timed_cart
        try:
            await engine.start(task['id'],not_before=due)
            await until(lambda:bool(engine.ready_accounts.get(task['id'],{}).get('idle')))
            assert not checks
            await until(lambda:task['id'] not in engine.jobs)
            assert checks==[True]
            assert not engine.activation_times
            second=task_record(store,group,account,use_account_proxy=True)
            await engine.start(second['id'],not_before=datetime.now(timezone.utc)+timedelta(seconds=60))
            await until(lambda:bool(engine.ready_accounts.get(second['id'],{}).get('idle')))
            await engine.stop(second['id'])
            assert not engine.ready_accounts and not engine.activation_times
            assert engine.browser_slots._value==10
        finally:
            await engine.close();store.db.close()
    asyncio.run(scenario())


def test_preparation_windows_include_upcoming_but_not_active_occurrence():
    anchor=datetime.now().astimezone().replace(hour=8,minute=0,second=0,microsecond=0)
    schedule={'configured_at':anchor.isoformat(),'slots':[{'start':'10:00','stop':'10:30'}]}
    assert not preparations(schedule,anchor.replace(hour=9,minute=58))
    result=preparations(schedule,anchor.replace(hour=9,minute=59,second=30))
    assert len(result)==1 and datetime.fromisoformat(result[0][1]).hour==10
    assert not preparations(schedule,anchor.replace(hour=10))


def test_same_account_tasks_cannot_purchase_concurrently(tmp_path):
    async def scenario():
        store,group,account,engine=setup(tmp_path)
        a=task_record(store,group,account,use_account_proxy=True,monitor_asin='B012345678')
        b=task_record(store,group,account,use_account_proxy=True,monitor_asin='B087654321')
        try:
            await engine.start(a['id'])
            await until(lambda:bool(engine.ready_accounts.get(a['id'],{}).get('idle')))
            engine.amazon.stock.add('B087654321')
            await engine.start(b['id'])
            await until(lambda:b['id'] not in engine.jobs)
            assert engine.amazon.carts==[(account['id'],'B087654321')]
            assert not engine.ready_accounts
            await engine.stop(a['id'])
            assert engine.browser_slots._value==10
        finally:
            await engine.close();store.db.close()
    asyncio.run(scenario())


def test_monitor_group_waiters_do_not_hold_global_scan_slots(tmp_path):
    async def scenario():
        store,group,account,engine=setup(tmp_path)
        group=store.put('groups',{**group,'monitor_concurrency':1})
        other=store.put('groups',{**group,'id':'other-group'})
        hold=asyncio.Event()
        inspected=[]
        original=engine.amazon.inspect
        async def inspect(page,item,region):
            inspected.append(item['asin'])
            if item['asin']!='B999999999':await hold.wait()
            return await original(page,item,region)
        engine.amazon.inspect=inspect
        items=[{'asin':f'B{i:09d}','offer_id':'','max_price':None} for i in range(12)]
        monitors=engine.monitors.subscribe({'id':'first','simulation':False},group,items,'US')
        try:
            await until(lambda:len(inspected)==1)
            others=engine.monitors.subscribe({'id':'second','simulation':False},other,[{'asin':'B999999999'}],'US')
            await until(lambda:'B999999999' in inspected)
            assert engine.monitors.scan_slots._value>=8
            await engine.monitors.unsubscribe('second',others)
        finally:
            await engine.monitors.unsubscribe('first',monitors)
            await engine.close();store.db.close()
    asyncio.run(scenario())


def test_prepared_browser_rechecks_live_header_and_does_not_trust_old_handoff():
    from patchright.async_api import async_playwright
    from retail.amazon import Amazon,AuthenticationRequired
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            page=await browser.new_page()
            await page.route('**/*',lambda r:r.fulfill(body='''<div id="nav-link-accountList"><span class="nav-line-1">Hello, sign in</span></div>
                <span id="productTitle">Fixture</span><button>Add to cart</button>''',content_type='text/html'))
            page._retail_ready_recheck=True
            try:
                await Amazon(None).inspect(page,{'asin':'B012345678'},'US')
            except AuthenticationRequired:
                pass
            else:
                raise AssertionError('An expired ready session must require login')
            await browser.close()
    asyncio.run(scenario())


def test_closed_prepared_browser_releases_capacity_before_restock(tmp_path):
    async def scenario():
        store,group,account,engine=setup(tmp_path)
        task=task_record(store,group,account,use_account_proxy=True,monitor_asin='B012345678')
        try:
            await engine.start(task['id'])
            await until(lambda:bool(engine.ready_accounts.get(task['id'],{}).get('idle')))
            # The real browser's close listener sets this event.
            engine.ready_accounts[task['id']]['evict'].set()
            await until(lambda:task['id'] not in engine.ready_accounts)
            assert engine.browser_slots._value==10
            assert not store.get('tasks',task['id'])['account_ready']
            restock(engine)
            await until(lambda:task['id'] not in engine.jobs)
            assert len(engine.amazon.carts)==1
        finally:
            await engine.close();store.db.close()
    asyncio.run(scenario())


def test_scheduled_http_preparation_releases_unused_resources(tmp_path):
    async def scenario():
        store,group,account,engine=setup(tmp_path,keep_accounts_ready=False)
        engine.amazon.probe_session=AsyncMock(return_value='authenticated')
        task=task_record(store,group,account,use_account_proxy=True)
        try:
            await engine.start(task['id'],not_before=datetime.now(timezone.utc)+timedelta(seconds=60))
            await until(lambda:'scheduled start' in store.get('tasks',task['id']).get('message',''))
            assert engine.amazon.probe_session.await_count==1
            assert not engine.account_locks[account['id']].locked()
            assert engine.browser_slots._value==10
            assert not engine.amazon.carts
            await engine.stop(task['id'])
            assert not engine.activation_times
        finally:
            await engine.close();store.db.close()
    asyncio.run(scenario())


def test_readiness_is_bounded_and_disabled_mode_keeps_no_reservations(tmp_path):
    async def scenario():
        store,_,account,engine=setup(tmp_path)
        try:
            assert engine.reserve_readiness('one',account['id'])
            assert engine.reserve_readiness('duplicate',account['id']) is None
            for i in range(3):assert engine.reserve_readiness(str(i),str(i))
            assert engine.reserve_readiness('overflow','fifth') is None
            engine.forget_ready('one')
            store.put('settings',{'keep_accounts_ready':False},'settings')
            assert engine.reserve_readiness('disabled','fifth') is None
        finally:
            await engine.close();store.db.close()
    asyncio.run(scenario())


def test_next_schedule_preparation_stops_previous_window_in_the_gap(tmp_path,monkeypatch):
    async def scenario():
        store,group,account,engine=setup(tmp_path)
        task=task_record(store,group,account)
        start=datetime.now().astimezone()+timedelta(seconds=45)
        old={'seen':['previous'],'active':True,'preparing_until':(start-timedelta(minutes=5)).isoformat()}
        store.put('schedule_runs',old,'schedule-'+group['id'])
        engine.jobs[task['id']]=asyncio.create_task(asyncio.Event().wait())
        engine.start_group=AsyncMock()
        monkeypatch.setattr('retail.engine.occurrences',lambda *_:[])
        monkeypatch.setattr('retail.engine.preparations',lambda *_:[('next',start.isoformat())])
        try:
            engine.scheduler=asyncio.create_task(engine.schedule())
            await until(lambda:engine.start_group.await_count==1)
            assert task['id'] not in engine.jobs
            assert store.get('tasks',task['id'])['status']=='stopped'
            engine.start_group.assert_awaited_once_with(group['id'],not_before=start)
            # The occurrence is durable; a poll or restart cannot start it twice.
            await engine.close()
            engine.scheduler=asyncio.create_task(engine.schedule())
            await asyncio.sleep(.55)
            assert engine.start_group.await_count==1
        finally:
            await engine.close();store.db.close()
    asyncio.run(scenario())


def test_readiness_setting_default_persistence_and_active_task_guard(tmp_path):
    from fastapi.testclient import TestClient
    from retail.app import create_app
    from retail.models import Settings
    assert Settings().keep_accounts_ready is True
    app=create_app(tmp_path)
    with TestClient(app,headers={'X-Retail-Client':'dashboard'}) as client:
        assert client.post('/api/settings',json={'keep_accounts_ready':False}).status_code==200
        assert app.state.store.get('settings','settings')['keep_accounts_ready'] is False
        app.state.engine.jobs['fixture']=object()
        try:
            assert client.post('/api/settings',json={'keep_accounts_ready':True}).status_code==409
            assert app.state.store.get('settings','settings')['keep_accounts_ready'] is False
        finally:
            app.state.engine.jobs.pop('fixture')


def test_immediate_start_stop_leaves_no_scheduled_activation(tmp_path):
    async def scenario():
        store,group,account,engine=setup(tmp_path)
        task=task_record(store,group,account)
        try:
            due=datetime.now(timezone.utc)+timedelta(seconds=60)
            await engine.start(task['id'],not_before=due)
            await engine.stop(task['id'])
            assert not engine.activation_times and not engine.ready_accounts and not engine.jobs
            assert not engine.amazon.contexts and engine.browser_slots._value==10
        finally:
            await engine.close();store.db.close()
    asyncio.run(scenario())


def test_available_shared_observation_skips_redundant_http_probe(tmp_path):
    from retail.monitors import monitor_items
    async def scenario():
        store,group,account,engine=setup(tmp_path,keep_accounts_ready=False)
        engine.amazon.probe_session=AsyncMock(return_value='authenticated')
        engine.amazon.stock.add('B012345678')
        task=task_record(store,group,account,use_account_proxy=True,monitor_asin='B012345678')
        monitors=engine.monitors.subscribe({**task,'id':'existing-reader'},group,
            monitor_items(store,group,task),'US')
        try:
            await until(lambda:all(m.product for m in monitors))
            await engine.start(task['id'])
            await until(lambda:task['id'] not in engine.jobs)
            assert engine.amazon.probe_session.await_count==0
            assert engine.amazon.authentications==[account['id']]
            assert engine.amazon.carts==[(account['id'],'B012345678')]
        finally:
            await engine.monitors.unsubscribe('existing-reader',monitors)
            await engine.close();store.db.close()
    asyncio.run(scenario())
