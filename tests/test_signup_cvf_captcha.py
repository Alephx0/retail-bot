"""Observed Amazon CVF markup, synthetic state and mocked paid requests only."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

from retail import account_creation as creation, signup_challenges as challenges
from retail.amazon import Amazon
from retail.services import SolverService
from retail.signup_controls import SignupControls
from test_account_care import fixture
from cvf_bridge_fixture import BRIDGE_SCRIPT, WRAPPER_BRIDGE


KEY='12345678-1234-1234-1234-123456789012'
FRAME=f'https://iframe.arkoselabs.com/{KEY}/index.html?data=fixture-blob&mkt=en'
SOLVER={'provider':'capmonster','api_key':'private-fixture-key','timeout_seconds':5}
PROXY={'server':'http://proxy.example.test:8080','username':'fixture','password':'private-proxy'}
MARKUP='''<!doctype html><html><body>
<h1>Solve this puzzle to protect your account</h1>
<form id="cvf-aamation-challenge-form" action="/ap/cvf/verify" method="post">
 <input type="hidden" name="anti-csrftoken-a2z" value="fixture-csrf">
 <input type="hidden" id="cvf_aamation_response_token" name="cvf_aamation_response_token">
 <input type="hidden" name="action" value="verify">
 <input type="hidden" name="clientContext" value="fixture-context">
</form>
<iframe id="cvf-aamation-challenge-iframe" title="verification puzzle" src="/fixture-wrapper"></iframe>
<script>window.submits=0;document.querySelector('form').onsubmit=e=>{
 e.preventDefault();window.submits++;
 window.delivered=Object.fromEntries(new FormData(e.target));
 document.body.innerHTML='<div id="nav-link-accountList"><span class="nav-line-1">Hello, Fixture</span></div>';
};</script></body></html>'''


MARKUP=MARKUP.replace('<script>window.submits',BRIDGE_SCRIPT+'<script>window.submits')


async def setup(browser, *, proxy=True, wrapper_body=None):
    context=await browser.new_context()
    if proxy:context._retail_signup_proxy=PROXY
    page=await context.new_page()
    async def route(r):
        url=r.request.url
        body=(f'<iframe id="aacb-arkose-frame" title="Challenge Verification" src="{FRAME}"></iframe>'
              if url.endswith('/fixture-wrapper') else '<h2>Fixture puzzle</h2>'
              if url.startswith('https://iframe.arkoselabs.com/') else MARKUP)
        if url.endswith('/fixture-wrapper') and wrapper_body is not None:body=wrapper_body
        if url.endswith('/fixture-wrapper'):body+=WRAPPER_BRIDGE
        await r.fulfill(body=body,content_type='text/html')
    await page.route('**/*',route)
    await page.goto('https://www.amazon.com/ap/cvf/request')
    return context,page


@pytest.mark.parametrize('mode',['success','level_3','late_frame','missing_proxy','provider_failure','cancel','changed','replaced_form','offsite','duplicate','populated'])
def test_cvf_signup_durable_solve_and_native_verification(tmp_path,monkeypatch,mode):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        adapter=engine.amazon=Amazon(store)
        adapter.watch_login=AsyncMock()
        account={**account,'solver_id':'assigned'}
        store.put('solvers',SOLVER,'assigned')
        key='creation-'+account['id']
        store.put('account_creation',{'account_id':account['id'],'submitted':True},key)
        def update(status,message,**extra):
            store.put('account_creation',{**store.get('account_creation',key),'status':status,'message':message,**extra},key)
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            context,page=await setup(browser,proxy=mode!='missing_proxy',wrapper_body=f'<iframe id="aacb-arkose-frame" style="width:0;height:0;border:0" src="{FRAME}"></iframe>' if mode=='late_frame' else None)
            if mode=='late_frame':monkeypatch.setattr(challenges,'CVF_READY_TIMEOUT_MS',100)
            if mode=='level_3':
                await page.evaluate("acic.currentAAmationResult.actionTypeValue='ARKOSE_LEVEL_3'",isolated_context=False)
            if mode=='offsite':await page.locator('form').evaluate("e=>e.action='https://foreign.example/collect'")
            if mode=='duplicate':await page.locator(challenges.CVF_FIELD).evaluate('e=>e.after(e.cloneNode(true))')
            if mode=='populated':await page.locator(challenges.CVF_FIELD).evaluate("e=>e.value='already-submitted'")
            entered=asyncio.Event()
            inspected_ready=asyncio.Event()
            async def solve(solver,task,**kwargs):
                assert store.get('account_creation',key)['captcha_attempted']
                assert not store.get('account_creation',key).get('captcha_submitted')
                assert task['type']=='FunCaptchaTask' and task['websitePublicKey']==KEY
                assert json.loads(task['data'])=={'blob':'fixture-blob'}
                assert task['funcaptchaApiJSSubdomain']=='amazon-api.arkoselabs.com'
                assert task['proxyPassword']==PROXY['password']
                entered.set()
                if mode=='late_frame':await inspected_ready.wait()
                if mode=='provider_failure':raise ValueError('private-provider-data')
                if mode=='cancel':await asyncio.Event().wait()
                if mode=='changed':
                    wrapper=await (await page.locator('#cvf-aamation-challenge-iframe').element_handle()).content_frame()
                    await wrapper.locator('#aacb-arkose-frame').evaluate("e=>e.src=e.src.replace('fixture-blob','changed-blob')")
                if mode=='replaced_form':await page.locator('form').evaluate('e=>e.replaceWith(e.cloneNode(true))')
                return 'fixture-solved-token'
            mocked=AsyncMock(side_effect=solve)
            monkeypatch.setattr(SolverService,'solve_token',mocked)
            adapter.logins[account['id']]=context
            controls=SignupControls(page,'www.amazon.com',update)
            job=asyncio.create_task(creation.verify_signup(engine,account,context,page,controls,update))
            adapter.login_watchers[account['id']]=job
            try:
                if mode=='late_frame':
                    async with asyncio.timeout(6):
                        while store.get('account_creation',key).get('status')!='captcha_loading':await asyncio.sleep(.01)
                    assert not store.all('harvesters')
                    assert not store.get('account_creation',key).get('captcha_submitted')
                    assert 'waiting for the verification frame' in store.get('account_creation',key)['message']
                    mocked.assert_not_awaited()
                    before=await creation.signup_state(page)
                    wrapper=await (await page.locator('#cvf-aamation-challenge-iframe').element_handle()).content_frame()
                    await wrapper.locator('#aacb-arkose-frame').evaluate("e=>e.style='width:324px;height:311px'")
                    after=await creation.signup_state(page)
                    assert before['document']==after['document'] and before['captcha_types']==after['captcha_types']
                    assert not before['captcha_ready'] and after['captcha_ready']
                    inspected_ready.set()
                if mode in ('success','level_3','late_frame'):
                    await asyncio.wait_for(job,10)
                    mocked.assert_awaited_once()
                    delivered=await page.evaluate('window.delivered',isolated_context=False)
                    assert delivered['cvf_aamation_response_token']=='fixture-amazon-session'
                    assert await page.evaluate('fixtureAnswers',isolated_context=False)==['fixture-solved-token']
                    assert delivered['anti-csrftoken-a2z']=='fixture-csrf'
                    assert delivered['clientContext']=='fixture-context' and delivered['action']=='verify'
                    assert await page.evaluate('window.submits',isolated_context=False)==1
                    adapter.watch_login.assert_awaited_once()
                else:
                    if mode=='cancel':
                        await asyncio.wait_for(entered.wait(),5)
                        job.cancel()
                        with pytest.raises(asyncio.CancelledError):await job
                        job=asyncio.create_task(creation.verify_signup(engine,account,context,page,controls,update))
                    async with asyncio.timeout(6):
                        while not store.all('harvesters'):await asyncio.sleep(.01)
                    assert store.get('account_creation',key)['status']=='captcha_manual'
                    assert await page.evaluate('window.submits',isolated_context=False)==0
                    adapter.watch_login.assert_not_awaited()
                    if mode=='missing_proxy':
                        assert 'no proxy' in store.get('account_creation',key)['message']
                        assert not store.get('account_creation',key).get('captcha_submitted')
                    if mode in ('missing_proxy','offsite','duplicate','populated'):mocked.assert_not_awaited()
                    else:mocked.assert_awaited_once()
                assert store.get('accounts',other['id'])==other
                assert 'private-' not in json.dumps(store.all('account_creation')+store.all('harvesters'))
            finally:
                job.cancel();await asyncio.gather(job,return_exceptions=True)
                await context.close();adapter.logins.clear();await browser.close()
        await engine.close();store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('transition',['insert','show','parameters','missing','cancel','replaced_form','ambiguous'])
def test_cvf_waits_for_observable_readiness(monkeypatch,transition):
    async def scenario():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            markup='<p>Loading puzzle</p>'
            if transition=='show':markup=f'<iframe id="aacb-arkose-frame" style="width:0;height:0;border:0" src="{FRAME}"></iframe>'
            if transition=='parameters':markup='<iframe id="aacb-arkose-frame" src="about:blank"></iframe>'
            if transition=='ambiguous':markup=2*f'<iframe id="aacb-arkose-frame" src="{FRAME}"></iframe>'
            context,page=await setup(browser,wrapper_body=markup)
            wrapper=await (await page.locator('#cvf-aamation-challenge-iframe').element_handle()).content_frame()
            entered=asyncio.Event()
            original=page.wait_for_function
            async def waiting(expression,**kwargs):
                if expression==challenges.CVF_READY:entered.set()
                return await original(expression,**kwargs)
            monkeypatch.setattr(page,'wait_for_function',waiting)
            monkeypatch.setattr(challenges,'CVF_READY_TIMEOUT_MS',200 if transition in ('missing','ambiguous') else 3000)
            job=asyncio.create_task(challenges.prepare_token(page,'arkose',{'region':'US'},SOLVER,creation.validate_target))
            try:
                await asyncio.wait_for(entered.wait(),3)
                assert not job.done()
                if transition=='cancel':
                    job.cancel()
                    with pytest.raises(asyncio.CancelledError):await job
                elif transition in ('missing','ambiguous'):
                    with pytest.raises(ValueError,match='did not become ready'):await job
                else:
                    if transition=='replaced_form':await page.locator('form').evaluate('e=>e.replaceWith(e.cloneNode(true))')
                    if transition=='show':await wrapper.locator('#aacb-arkose-frame').evaluate("e=>e.style='width:324px;height:311px'")
                    elif transition=='parameters':await wrapper.locator('#aacb-arkose-frame').evaluate('(e,src)=>e.src=src',FRAME)
                    else:await wrapper.evaluate('(src)=>document.body.innerHTML=`<iframe id="aacb-arkose-frame" src="${src}"></iframe>`',FRAME)
                    if transition=='replaced_form':
                        with pytest.raises(ValueError):await job
                    else:
                        prepared=await asyncio.wait_for(job,4)
                        assert prepared['delivery']=='amazon_cvf'
                        assert prepared['params']['blob']=='fixture-blob'
                assert await page.evaluate('window.submits',isolated_context=False)==0
            finally:
                job.cancel();await asyncio.gather(job,return_exceptions=True)
                await context.close();await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['same_frame','wrapper','foreign_frame','changed_action','duplicate_name','replay','invalid_token'])
def test_cvf_revalidates_ownership_and_never_replays(change):
    async def scenario():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            context,page=await setup(browser)
            account={'region':'US'}
            prepared=await challenges.prepare_token(page,'arkose',account,SOLVER,creation.validate_target)
            if change in ('same_frame','foreign_frame'):
                wrapper=prepared['wrapper']
                await wrapper.locator('#aacb-arkose-frame').evaluate('e=>e.replaceWith(e.cloneNode(true))')
                if change=='foreign_frame':await wrapper.locator('#aacb-arkose-frame').evaluate("e=>e.src='https://iframe.arkoselabs.com.evil.test/puzzle'")
            elif change=='wrapper':await page.locator('#cvf-aamation-challenge-iframe').evaluate('e=>e.replaceWith(e.cloneNode(true))')
            elif change=='changed_action':await page.locator('form').evaluate("e=>e.action='/ap/register'")
            elif change=='duplicate_name':await page.locator('form').evaluate("e=>e.insertAdjacentHTML('beforeend','<input type=hidden name=cvf_aamation_response_token>')")
            if change=='replay':
                await challenges.apply_token(page,'arkose',prepared,'fixture-token',account,creation.validate_target)
                with pytest.raises(ValueError,match='already dispatched'):
                    await challenges.apply_token(page,'arkose',prepared,'fixture-token',account,creation.validate_target)
                assert await page.evaluate('window.submits',isolated_context=False)==1
            else:
                with pytest.raises(Exception):
                    await challenges.apply_token(page,'arkose',prepared,'' if change=='invalid_token' else 'fixture-token',account,creation.validate_target)
                assert await page.evaluate('window.submits',isolated_context=False)==0
            await context.close();await browser.close()
    asyncio.run(scenario())
