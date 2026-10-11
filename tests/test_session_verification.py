"""Read-only verification; all HTTP and purchasing operations are controlled fixtures."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from retail.amazon import Amazon, AccessDenied, BackoffRequired
from retail.app import create_app
from retail.models import Settings
from retail.session_probe import probe_session
from test_monitors import fixture, task_record, until

SIGNED_IN = '<a href="/gp/flex/sign-out.html">Sign out</a><div id="yourOrders"><h1>Your Orders</h1></div>'


def account_cookie(**changes):
    return {'region':'US', 'session':{'cookies':[{'name':'session-token','value':'fixture',
        'domain':'.amazon.com','path':'/','secure':True,'expires':time.time()+60, **changes}]}}


def guard(response):
    Amazon._raise_for_response(SimpleNamespace(status=response.status_code, headers=response.headers))


@pytest.mark.parametrize('html,result', [(SIGNED_IN,'authenticated'),
    ('<h1>Your Orders</h1>','unknown'), ('<input id="ap_password">','signed_out'),
    (SIGNED_IN+'<input id="captchacharacters">','unknown'),
    (SIGNED_IN+'<p>Continue shopping</p>','unknown'),
    ('<script>'+SIGNED_IN+'</script>','unknown')])
def test_probe_requires_live_protected_page(html,result):
    async def scenario():
        calls=[]
        def respond(request):
            calls.append(request)
            assert request.method=='GET' and request.headers['cookie']=='session-token=fixture'
            return httpx.Response(200, text=html, headers={'content-type':'text/html'})
        assert await probe_session(account_cookie(), '', response_guard=guard,
            transport=httpx.MockTransport(respond))==result
        assert len(calls)==1
    asyncio.run(scenario())


@pytest.mark.parametrize('location,result', [('/ap/signin','signed_out'),
    ('https://untrusted.invalid/your-orders/orders','unknown'),
    ('http://www.amazon.com/gp/your-account/order-history','unknown'),
    ('/gp/your-account/delete','unknown')])
def test_redirect_never_leaks_cookies_or_follows_actions(location,result):
    async def scenario():
        calls=[]
        def respond(request):
            calls.append(request)
            return httpx.Response(302, headers={'location':location})
        assert await probe_session(account_cookie(), '', response_guard=guard,
            transport=httpx.MockTransport(respond))==result
        assert len(calls)==1
    asyncio.run(scenario())


@pytest.mark.parametrize('cookie', [{'expires':time.time()-60},{'domain':'.untrusted.invalid'},{'partitionKey':'https://other.invalid'}])
def test_unusable_cookies_do_not_create_false_session_proof(cookie):
    async def scenario():
        def no_request(request):
            pytest.fail('No usable cookies must skip HTTP')
        assert await probe_session(account_cookie(**cookie), '', response_guard=guard,
            transport=httpx.MockTransport(no_request))=='unknown'
    asyncio.run(scenario())


@pytest.mark.parametrize('status,error', [(429,BackoffRequired),(503,BackoffRequired),(403,AccessDenied)])
def test_retailer_restrictions_are_not_browser_fallbacks(status,error):
    async def scenario():
        with pytest.raises(error) as raised:
            await probe_session(account_cookie(), '', response_guard=guard,
                transport=httpx.MockTransport(lambda r:httpx.Response(status,headers={'retry-after':'7200'})))
        if status!=403:
            assert raised.value.retry_after_seconds==7200
    asyncio.run(scenario())


def test_bounded_read_and_cancellation():
    async def scenario():
        entered=asyncio.Event()
        async def stall(request):
            entered.set()
            await asyncio.Event().wait()
        assert await probe_session(account_cookie(), '', response_guard=guard,
            transport=httpx.MockTransport(stall),timeout=.02)=='unknown'
        entered.clear()
        task=asyncio.create_task(probe_session(account_cookie(), '', response_guard=guard,
            transport=httpx.MockTransport(stall)))
        await entered.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())


@pytest.mark.parametrize('mode,result,initial_contexts', [('http','authenticated',0),
    ('http','unknown',1),('http','signed_out',1),('browser','authenticated',1),('headless','authenticated',1)])
def test_startup_modes_release_workers_and_always_verify_before_cart(tmp_path,mode,result,initial_contexts):
    async def scenario():
        store,group,account,engine=fixture(tmp_path)
        store.put('settings',{'session_verification_mode':mode,'show_browser_window':True},'settings')
        engine.amazon.probe_session=AsyncMock(return_value=result)
        create=engine.amazon.context
        launches=[]
        async def context(identity, proxy='', solver='', **options):
            if identity['id']==account['id']: launches.append(options)
            return await create(identity,proxy,solver)
        engine.amazon.context=context
        task=task_record(store,group,account,use_account_proxy=True,monitor_asin='B012345678')
        try:
            await engine.start(task['id'])
            await until(lambda: store.get('tasks',task['id'])['status']=='waiting'
                and not engine.account_locks[account['id']].locked())
            assert len(launches)==initial_contexts
            assert engine.browser_slots._value==10
            assert engine.amazon.probe_session.await_count==(1 if mode=='http' else 0)
            if mode=='headless': assert launches==[{'headless':True}]
            engine.amazon.stock.add('B012345678')
            monitor=next(iter(engine.monitors.active.values()))
            monitor.product['available']=True; monitor.sequence+=1; monitor.update('in_stock','Fixture restock')
            await until(lambda:task['id'] not in engine.jobs)
            assert store.get('tasks',task['id'])['status']=='completed'
            assert len(engine.amazon.authentications)==initial_contexts+1
            assert launches[-1]=={}  # Checkout obeys normal window settings.
            assert engine.amazon.carts==[(account['id'],'B012345678')]
            assert all(c.closed for c in engine.amazon.contexts)
        finally:
            await engine.close(); store.db.close()
    asyncio.run(scenario())


def test_cancel_browser_free_preparation_releases_account(tmp_path):
    async def scenario():
        store,group,account,engine=fixture(tmp_path)
        entered=asyncio.Event()
        async def probe(*args):
            entered.set(); await asyncio.Event().wait()
        engine.amazon.probe_session=probe
        task=task_record(store,group,account,use_account_proxy=True)
        try:
            await engine.start(task['id']); await entered.wait(); await engine.stop(task['id'])
            assert not engine.account_locks[account['id']].locked()
            assert engine.browser_slots._value==10
            assert not engine.amazon.authentications
        finally:
            await engine.close(); store.db.close()
    asyncio.run(scenario())


def test_default_and_global_setting_validation_and_runtime_lock(tmp_path):
    assert Settings().session_verification_mode=='http'
    with TestClient(create_app(tmp_path),headers={'X-Retail-Client':'dashboard'}) as client:
        assert client.get('/api/state').json()['settings'][0]['session_verification_mode']=='http'
        for mode in ('headless','browser','http'):
            assert client.post('/api/settings',json={'session_verification_mode':mode}).status_code==200
        assert client.post('/api/settings',json={'session_verification_mode':'invalid'}).status_code==422
        assert client.post('/api/settings',json={'session_verification_mode':'headless','cdp_attach':True}).status_code==422
        client.app.state.engine.jobs['fixture']=object()
        try:
            assert client.post('/api/settings',json={'session_verification_mode':'browser'}).status_code==409
        finally: client.app.state.engine.jobs.clear()


@pytest.mark.parametrize('backend',['javascript','native'])
def test_headless_preserves_account_profile_and_does_not_change_window_default(tmp_path,backend):
    from retail.models import Account
    from retail.store import Store
    from retail.native_fingerprint import DEFAULT_DIRECTORY
    if backend=='native' and not (DEFAULT_DIRECTORY/'chrome.exe').is_file():
        pytest.skip('Native Chromium is not installed')
    async def scenario():
        store=Store(tmp_path)
        store.put('settings',{'show_browser_window':True,'fingerprint_backend':'javascript',
            'fingerprint_proxy_location':False},'settings')
        account=store.put('accounts',Account(name='Headless fixture',fingerprint_seed='abcd'*8,
            fingerprint_overrides={'fingerprint_backend':backend,'fingerprint_navigator':True,
                'fingerprint_screen':True,'fingerprint_timezone':'America/Chicago','fingerprint_proxy_location':False},
            fingerprint_values={'cpu':'2','memory':'8','screen':'1920x1080@1','gpu':'native'}).model_dump())
        adapter=Amazon(store)
        try:
            context=await adapter.context(account,headless=True)
            assert context._retail_headless
            await context.route('**/*',lambda route:route.fulfill(body='<h1>Fixture</h1>',content_type='text/html'))
            page=await context.new_page(); await page.goto('https://fixture.test/')
            actual=await page.evaluate('''async()=>({cpu:navigator.hardwareConcurrency,
                memory:navigator.deviceMemory,screen:[screen.width,screen.height,devicePixelRatio],
                zone:Intl.DateTimeFormat().resolvedOptions().timeZone,
                worker:await new Promise(resolve=>{const w=new Worker(URL.createObjectURL(new Blob([
                'postMessage(navigator.hardwareConcurrency)'],{type:'application/javascript'})));
                w.onmessage=e=>{w.terminate();resolve(e.data)}})})''',isolated_context=False)
            assert actual=={'cpu':2,'memory':8,'screen':[1920,1080,1],'zone':'America/Chicago','worker':2}
            assert store.get('settings','settings')['show_browser_window'] is True
            assert store.get('accounts',account['id'])['fingerprint_seed']=='abcd'*8
            await context.close()
            if adapter.profile_close_tasks: await asyncio.gather(*adapter.profile_close_tasks)
            # A shared headless process must not silently swallow the subsequent visible launch.
            visible=await adapter.context(account)
            assert not visible._retail_headless
            await visible.close()
        finally:
            await adapter.close(); store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode,result,contexts',[('http','authenticated',0),('http','unknown',1),('headless','authenticated',1),('browser','authenticated',1)])
def test_scheduled_plan_preparation_respects_mode_then_uses_task_browser(tmp_path,mode,result,contexts):
    from datetime import datetime, timezone
    from test_task_group_execution import setup
    async def scenario():
        moment=[datetime(2026,10,9,13,56,tzinfo=timezone.utc)]
        store,adapter,c,account,run=setup(tmp_path,action='notify',clock=lambda:moment[0],
            schedule={'kind':'once','date':'2026-10-09','start':'10:00','end':'10:30','timezone':'America/New_York','prepare_minutes':5})
        store.put('settings',{'session_verification_mode':mode},'settings')
        adapter.probe_session=AsyncMock(return_value=result)
        adapter.account_proxy=lambda account:''
        create=adapter.context
        launches=[]
        async def context(account,**options):
            launches.append(options); return await create(account)
        adapter.context=context
        try:
            await c.tick(); await asyncio.gather(*c.scans.values())
            assert c.repo.require('run',run['id'])['state']=='preparing'
            assert adapter.contexts==contexts
            assert not adapter.inspections and not adapter.carts
            assert not c.engine.account_locks[account['id']].locked()
            if mode=='headless': assert launches==[{'headless':True}]
            assert c.next_scan[(run['id'],account['id'])]>time.monotonic()+55
            moment[0]=datetime(2026,10,9,14,0,tzinfo=timezone.utc)
            await c.tick(); await asyncio.gather(*c.scans.values())
            assert adapter.inspections==1 and adapter.authentications>=1
            assert launches[-1]=={}
            assert c.engine.browser_slots._value==9
        finally:
            await c.close(); store.db.close()
        assert c.engine.browser_slots._value==10
    asyncio.run(scenario())


def test_native_headless_changes_window_mode_only(tmp_path):
    from retail.native_fingerprint import launch_options
    binary=tmp_path/'chrome.exe'; binary.touch()
    settings=Settings(fingerprint_backend='native',native_browser_executable=str(binary),
        fingerprint_navigator=True,fingerprint_canvas=True,fingerprint_webgl=True,
        fingerprint_webgpu=True,fingerprint_fonts=True,fingerprint_screen=True,
        fingerprint_timezone='America/Chicago').model_dump()
    values={'cpu':2,'memory':8,'screen':{'width':1920,'height':1080},'device_scale_factor':1}
    visible=launch_options(settings,123,values)
    headless=launch_options({**settings,'show_browser_window':False},123,values)
    assert visible.pop('headless') is False and headless.pop('headless') is True
    assert visible==headless


def test_pool_reuses_headless_preparation_when_execution_is_also_headless(tmp_path):
    from test_task_group_execution import setup
    async def scenario():
        store,adapter,c,account,_=setup(tmp_path,action='notify')
        store.put('settings',{'show_browser_window':False},'settings')
        create=adapter.context
        async def context(account,**options): return await create(account)
        adapter.context=context
        try:
            async with c.pool.lease(account,headless=True) as prepared:
                pass
            async with c.pool.lease(account) as execution:
                assert execution is prepared
            assert adapter.contexts==1
        finally:
            await c.close(); store.db.close()
    asyncio.run(scenario())


def test_preparation_finishing_after_window_opens_does_not_delay_execution(tmp_path):
    from datetime import datetime, timezone
    from test_task_group_execution import setup
    async def scenario():
        moment=[datetime(2026,10,9,13,56,tzinfo=timezone.utc)]
        store,adapter,c,account,run=setup(tmp_path,action='notify',clock=lambda:moment[0],
            schedule={'kind':'once','date':'2026-10-09','start':'10:00','end':'10:30','timezone':'America/New_York','prepare_minutes':5})
        entered,release=asyncio.Event(),asyncio.Event()
        async def probe(*args):
            entered.set(); await release.wait(); return 'authenticated'
        adapter.probe_session=probe; adapter.account_proxy=lambda a:''
        try:
            await c.tick(); await entered.wait()
            moment[0]=datetime(2026,10,9,14,0,tzinfo=timezone.utc)
            await c.tick(); release.set(); await asyncio.gather(*c.scans.values())
            assert c.next_scan[(run['id'],account['id'])]==0
            await c.tick(); await asyncio.gather(*c.scans.values())
            assert adapter.inspections==1
        finally:
            await c.close(); store.db.close()
    asyncio.run(scenario())
