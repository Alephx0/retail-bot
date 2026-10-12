import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

from retail import account_creation as creation
from retail import signup_challenges as challenges
from retail.amazon import Amazon
from retail.services import SolverService
from retail.signup_controls import SignupControls
from test_account_care import fixture


def test_detection_covers_nested_frames_and_ignores_hidden_challenges():
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            page=await browser.new_page()
            async def route(route):
                body='<iframe src="https://client-api.arkoselabs.com/fc/assets/puzzle"></iframe>' if route.request.url.endswith('/wrapper') else '<h1>Solve this puzzle to protect your account</h1>'
                await route.fulfill(body=body,content_type='text/html')
            await page.route('**/*',route)
            await page.goto('https://www.amazon.com/ap/verify')
            cases=[
                ('<input id=captchacharacters>',['image']),
                ('<iframe src="https://client-api.arkoselabs.com/fc/assets/puzzle"></iframe>',['arkose']),
                ('<div class=g-recaptcha data-sitekey=test>Verify</div>',['recaptcha']),
                ('<script src="https://www.google.com/recaptcha/enterprise.js"></script><div class=g-recaptcha>Verify</div>',['recaptcha_enterprise']),
                ('<div class=h-captcha>Verify</div>',['hcaptcha']),
                ('<div class=cf-turnstile>Verify</div>',['turnstile']),
                ('<div class=geetest_panel>Verify</div>',['geetest']),
                ('<div id=aws-waf-captcha-container>Verify</div>',['aws']),
                ('<iframe src="https://geo.captcha-delivery.com/captcha"></iframe>',['datadome']),
                ('<div class=frc-captcha>Verify</div>',['friendly']),
                ('<div id=tcaptcha_transform>Verify</div>',['tencent']),
                ('<div class=mtcaptcha>Verify</div>',['mtcaptcha']),
                ('<iframe src="https://www.amazon.com/wrapper"></iframe>',['arkose']),
                ('<h1>Solve this puzzle to protect your account</h1>',['unknown']),
                ('<iframe title="Enforcement frame" srcdoc="Puzzle"></iframe>',['unknown']),
                ('<iframe hidden src="https://www.amazon.com/wrapper"></iframe>',[]),
                ('<div hidden><div class=h-captcha>Hidden</div></div>',[]),
                ('<script src="https://js.hcaptcha.com/1/api.js"></script><h1>Enter email verification code</h1>',[]),
            ]
            for markup,expected in cases:
                await page.set_content(markup,wait_until='load')
                assert await challenges.detect(page)==expected,markup
            await page.set_content('<div id=wrapper></div>')
            before=await creation.signup_state(page)
            waiter=asyncio.create_task(creation.changed(page,before,timeout=3000))
            await page.evaluate("document.querySelector('#wrapper').innerHTML='<iframe src=\"https://www.amazon.com/wrapper\"></iframe>'")
            await waiter
            assert (await creation.signup_state(page))['captcha']
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('provider',['2captcha','capmonster'])
@pytest.mark.parametrize('kind',['arkose','recaptcha','turnstile'])
def test_provider_token_tasks_use_documented_types_and_current_proxy(provider,kind):
    params={'key':'public-key','blob':'private-blob','subdomain':'https://client-api.arkoselabs.com','userAgent':'fixture-agent','cdata':'challenge-data','action':'verify'}
    proxy={'server':'http://proxy.example.test:8080','username':'owner','password':'private-proxy-password'}
    solver={'provider':provider,'api_key':'private-key','timeout_seconds':10}
    task=SolverService.token_task(solver,kind,'https://www.amazon.com/ap/verify',params,proxy)
    assert task['type']=={'arkose':'FunCaptchaTask','recaptcha':'RecaptchaV2Task','turnstile':'TurnstileTask'}[kind]
    assert task['proxyPassword']=='private-proxy-password' and task['proxyPort']==8080
    assert task.get('websitePublicKey',task.get('websiteKey'))=='public-key'
    if kind=='arkose':assert json.loads(task['data'])=={'blob':'private-blob'}
    if provider=='capmonster' and kind=='arkose':
        with pytest.raises(ValueError,match='same proxy'):SolverService.token_task(solver,kind,'https://www.amazon.com/',params)
    else:
        plain=SolverService.token_task(solver,kind,'https://www.amazon.com/',params)
        assert plain['type']==task['type']+('Proxyless' if provider=='2captcha' else '')


@pytest.mark.parametrize('outcome',['ready','rejected','identity_mismatch','cancel','timeout'])
def test_token_provider_never_recreates_a_paid_task(monkeypatch,outcome):
    calls=[]
    async def scenario():
        provider={'provider':'2captcha','api_key':'private-key','timeout_seconds':.05 if outcome=='timeout' else 10}
        service=SolverService()
        async def call(solver,method,payload):
            calls.append(method)
            if outcome=='rejected':raise ValueError('Provider rejected')
            if method=='createTask':return {'taskId':123,'status':'processing'}
            if outcome=='timeout':return {'status':'processing'}
            return {'status':'ready','solution':{'token':'fixture-token','userAgent':'different' if outcome=='identity_mismatch' else 'fixture'}}
        service.call=call
        original=asyncio.sleep
        async def short_poll(_):await original(.01)
        monkeypatch.setattr('retail.services.asyncio.sleep',short_poll)
        job=asyncio.create_task(service.solve_token(provider,{'type':'TurnstileTaskProxyless'},user_agent='fixture'))
        if outcome=='cancel':
            while not calls:await original(.001)
            job.cancel()
            with pytest.raises(asyncio.CancelledError):await job
        elif outcome=='timeout':
            with pytest.raises(TimeoutError):await job
        elif outcome in ('identity_mismatch','rejected'):
            with pytest.raises(ValueError):await job
        else:assert await job=='fixture-token'
        assert calls.count('createTask')==1
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['manual','auto','auto_capmonster','auto_recaptcha','auto_turnstile','provider_failure','missing_parameters','changed','unsupported','cancel','offsite'])
def test_signup_challenge_routing_and_recovery(tmp_path,monkeypatch,mode):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        adapter=engine.amazon=Amazon(store)
        account={**account,'solver_id':'assigned'}
        store.put('solvers',{'provider':'manual' if mode=='manual' else 'capmonster' if mode=='auto_capmonster' else '2captcha','api_key':'private-fixture-key','timeout_seconds':10},'assigned')
        key='creation-'+account['id']
        store.put('account_creation',{'account_id':account['id'],'submitted':True},key)
        updates=[]
        def update(status,message,**extra):
            updates.append(status)
            store.put('account_creation',{**store.get('account_creation',key),'status':status,'message':message,**extra},key)
        adapter.watch_login=AsyncMock()
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            context=await browser.new_context()
            context._retail_paced_input=True
            context._retail_input_timing_preset='aggressive'
            context._retail_signup_proxy={'server':'http://proxy.example.test:8080','username':'fixture','password':'fixture-proxy'}
            page=await context.new_page()
            await page.route('**/*',lambda r:r.fulfill(body='<html></html>',content_type='text/html'))
            await page.goto('https://www.amazon.com/ap/verify')
            widget='<div class=h-captcha>Challenge</div>' if mode=='unsupported' else '<div id=arkose data-pkey="fixture-public-key" data-blob="private-blob">Puzzle</div>'
            if mode=='missing_parameters':widget='<div id=arkose>Puzzle</div>'
            field_name='fc-token'
            if mode=='auto_recaptcha':
                widget='<div class=g-recaptcha data-sitekey=fixture-key>Verify</div>'
                field_name='g-recaptcha-response'
            if mode=='auto_turnstile':
                widget='<div class=cf-turnstile data-sitekey=fixture-key>Verify</div>'
                field_name='cf-turnstile-response'
            action='https://example.test/collect' if mode=='offsite' else '/ap/verify'
            await page.set_content(f'''{widget}<form action="{action}"><input type=hidden name="{field_name}"><button type=submit>Submit</button></form>
              <script>window.submits=0;document.querySelector('form').onsubmit=e=>{{e.preventDefault();window.submits++;if(document.querySelector('[name="{field_name}"]').value!=='fixture-token')throw Error('Wrong response');document.body.innerHTML='<div id=nav-link-accountList><span class=nav-line-1>Hello, Fixture</span></div>';}};</script>''')
            entered=asyncio.Event()
            async def solve(*args,**kwargs):
                entered.set()
                if mode=='provider_failure':raise ValueError('Do not log provider content or secrets')
                if mode=='changed':await page.locator('#arkose').evaluate("e=>e.dataset.blob='replaced-blob'")
                if mode=='cancel':await asyncio.Event().wait()
                return 'fixture-token'
            mocked=AsyncMock(side_effect=solve)
            monkeypatch.setattr(SolverService,'solve_token',mocked)
            adapter.logins[account['id']]=context
            job=asyncio.create_task(creation.verify_signup(engine,account,context,page,SignupControls(page,'www.amazon.com',update),update))
            adapter.login_watchers[account['id']]=job
            try:
                if mode.startswith('auto'):
                    await asyncio.wait_for(job,10)
                    mocked.assert_awaited_once()
                    assert await page.evaluate('submits',isolated_context=False)==1
                    adapter.watch_login.assert_awaited_once()
                    assert 'captcha_solving' in updates
                elif mode=='cancel':
                    await asyncio.wait_for(entered.wait(),5)
                    job.cancel()
                    with pytest.raises(asyncio.CancelledError):await job
                    assert store.get('account_creation',key)['captcha_attempted']
                    assert not store.get('account_creation',key)['captcha_submitted']
                    resumed=asyncio.create_task(creation.verify_signup(engine,account,context,page,SignupControls(page,'www.amazon.com',update),update))
                    try:
                        async with asyncio.timeout(5):
                            while not store.all('harvesters'):await asyncio.sleep(.01)
                        assert store.get('account_creation',key)['status']=='captcha_manual'
                        mocked.assert_awaited_once()
                    finally:
                        resumed.cancel()
                        await asyncio.gather(resumed,return_exceptions=True)
                else:
                    async with asyncio.timeout(5):
                        while not store.all('harvesters'):await asyncio.sleep(.01)
                    record=store.get('account_creation',key)
                    assert record['status']=='captcha_manual'
                    assert ('SOLUTION_NOT_APPLIED' in record['message'] if mode in ('provider_failure','changed') else 'user input required' in record['message'])
                    assert record['captcha_type']==('hcaptcha' if mode=='unsupported' else 'arkose')
                    assert await page.evaluate('submits',isolated_context=False)==0
                    if mode in ('provider_failure','changed'):mocked.assert_awaited_once()
                    else:mocked.assert_not_awaited()
                    await page.set_content('<div id=nav-link-accountList><span class=nav-line-1>Hello, Fixture</span></div>')
                    await asyncio.wait_for(job,5)
                    adapter.watch_login.assert_awaited_once()
                assert store.get('accounts',other['id'])==other
                assert 'private-' not in json.dumps(store.all('account_creation')+store.all('harvesters'))
            finally:
                job.cancel()
                await asyncio.gather(job,return_exceptions=True)
                await context.close()
                adapter.logins.clear()
                await browser.close()
        await engine.close()
        store.db.close()
    asyncio.run(scenario())
