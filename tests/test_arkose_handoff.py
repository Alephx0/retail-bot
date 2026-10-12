import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

from retail.arkose_handoff import ArkoseHandoff
from retail import account_creation as creation
from retail.amazon import Amazon
from retail.services import SolverService, SolverError
from retail.signup_controls import SignupControls
from test_account_care import fixture
from test_signup_cvf_captcha import KEY, FRAME, MARKUP, SOLVER, PROXY
from cvf_bridge_fixture import WRAPPER_BRIDGE


@pytest.mark.parametrize('mode',['immediate','delayed','collapsed','unsolvable','timeout','cancel','wrong_blob','other_origin'])
def test_fresh_arkose_handoff_and_durable_outcome(tmp_path,monkeypatch,mode):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        adapter=engine.amazon=Amazon(store);adapter.watch_login=AsyncMock()
        account={**account,'solver_id':'assigned'};store.put('solvers',SOLVER,'assigned')
        key='creation-'+account['id']
        store.put('account_creation',{'account_id':account['id'],'submitted':True},key)
        def update(status,message,**extra):
            store.put('account_creation',{**store.get('account_creation',key),'status':status,'message':message,**extra},key)
        requests=[];entered=asyncio.Event();paid=asyncio.Event()
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            context=await browser.new_context();context._retail_signup_proxy=PROXY
            page=await context.new_page()
            async def route(r):
                url=r.request.url
                if '/fc/gt2/' in url:
                    requests.append('browser_initialization');entered.set()
                    return await r.fulfill(body='{}',content_type='application/json',headers={'Access-Control-Allow-Origin':'*'})
                if '/fixture-wrapper' in url:body=f'<iframe id=aacb-arkose-frame style="{"width:0;height:0;border:0" if mode=="collapsed" else ""}" src="{FRAME}"></iframe>'
                elif url.startswith('https://iframe.arkoselabs.com/'):
                    blob='changed' if mode=='wrong_blob' else 'fixture-blob'
                    delay=250 if mode=='delayed' else 0
                    body=f'''<h2>Fixture puzzle</h2><script>setTimeout(()=>fetch('https://amazon-api.arkoselabs.com/fc/gt2/public_key/{KEY}',
                        {{method:'POST',body:new URLSearchParams({{'data[blob]':'{blob}'}})}}),{delay})</script>'''
                else:body=MARKUP
                if '/fixture-wrapper' in url:body+=WRAPPER_BRIDGE
                await r.fulfill(body=body,content_type='text/html')
            await page.route('**/*',route)
            handoff=await ArkoseHandoff.install(page,'www.amazon.com',SOLVER)
            await page.goto('https://'+('foreign.example' if mode=='other_origin' else 'www.amazon.com')+'/ap/cvf/request')
            if mode=='other_origin':
                await asyncio.wait_for(entered.wait(),3)
                assert not handoff.ready.is_set()
                await context.close();await browser.close();await engine.close();store.db.close();return
            async def solve(config,task,**kw):
                paid.set()
                assert not requests, 'Browser consumed the blob before the solver'
                assert json.loads(task['data'])=={'blob':'fixture-blob'}
                assert store.get('account_creation',key)['captcha_attempted']
                assert not store.get('account_creation',key)['captcha_submitted']
                kw['progress']('provider_processing',task_id='private-task-id')
                if mode=='unsolvable':raise SolverError('ERROR_CAPTCHA_UNSOLVABLE')
                if mode=='timeout':raise TimeoutError()
                if mode=='cancel':await asyncio.Event().wait()
                kw['progress']('solution_received')
                return 'fixture-token'
            mocked=AsyncMock(side_effect=solve);monkeypatch.setattr(SolverService,'solve_token',mocked)
            adapter.logins[account['id']]=context
            controls=SignupControls(page,'www.amazon.com',update)
            job=asyncio.create_task(creation.verify_signup(engine,account,context,page,controls,update))
            try:
                if mode=='cancel':
                    await asyncio.wait_for(paid.wait(),5);job.cancel()
                    with pytest.raises(asyncio.CancelledError):await job
                    job=asyncio.create_task(creation.verify_signup(engine,account,context,page,controls,update))
                if mode in ('immediate','delayed','collapsed'):
                    await asyncio.wait_for(job,8)
                    assert await page.evaluate('window.submits',isolated_context=False)==1
                    assert store.get('account_creation',key)['captcha_phase']=='submitted'
                    assert not requests
                else:
                    async with asyncio.timeout(6):
                        while not store.all('harvesters'):await asyncio.sleep(.01)
                    await asyncio.wait_for(entered.wait(),3)
                    record=store.get('account_creation',key)
                    assert not record.get('captcha_submitted')
                    if mode!='wrong_blob':
                        assert record['captcha_phase']=='failed'
                        expected={'unsolvable':'ERROR_CAPTCHA_UNSOLVABLE','timeout':'SOLVER_TIMEOUT','cancel':'CANCELLED'}[mode]
                        assert record['captcha_error']==expected
                        assert expected in record['message']
                    assert await page.evaluate('window.submits',isolated_context=False)==0
                assert mocked.await_count==(0 if mode=='wrong_blob' else 1)
                assert 'private-' not in json.dumps(store.all('account_creation')+store.all('harvesters'))
                assert store.get('accounts',other['id'])==other
            finally:
                job.cancel();await asyncio.gather(job,return_exceptions=True)
                handoff.finish();await context.close();adapter.logins.clear();await browser.close()
        await engine.close();store.db.close()
    asyncio.run(scenario())


def test_provider_error_text_is_allowlisted(monkeypatch):
    import httpx
    class Client:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):
            return httpx.Response(200,json={'errorId':1,'errorCode':'PRIVATE_CREDENTIALS','errorDescription':'secret'},request=httpx.Request('POST','https://api.capmonster.cloud'))
    monkeypatch.setattr('retail.services.httpx.AsyncClient',lambda **kwargs:Client())
    async def scenario():
        with pytest.raises(SolverError,match='PROVIDER_ERROR') as error:
            await SolverService().call(SOLVER,'createTask')
        assert 'PRIVATE' not in str(error.value) and 'secret' not in str(error.value)
    asyncio.run(scenario())
