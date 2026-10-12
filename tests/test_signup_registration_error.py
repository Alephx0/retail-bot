"""Synthetic retailer responses only; no paid tasks or live account submissions."""
import asyncio
import time
from urllib.parse import urlsplit
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

from retail import account_creation as creation, signup_recovery as recovery
from retail.account_lifecycle import observe
from retail.amazon import Amazon
from retail.services import SolverService
from retail.signup_controls import SignupControls
from test_account_care import fixture
from test_signup_cvf_captcha import FRAME, MARKUP
from cvf_bridge_fixture import BRIDGE_SCRIPT, WRAPPER_BRIDGE


REGISTRATION = '''<form id=ap_register_form action=/registration-submit method=post>
<input name=email type=hidden value=fixture@example.test>
<input id=ap_customer_name name=customerName value="Fixture User">
<input id=ap_password name=password type=password>
<input id=ap_password_check name=passwordCheck type=password>
<input id=continue type=submit value="Create account"></form>'''
ERROR = '<div id=auth-error-message-box><h2>There was a problem</h2>Internal error please try again.</div>'


@pytest.mark.parametrize('mode',['existing','new','repeated','cancel','failed','delayed','internal_later','repeated_later','retry_after','cooldown'])
def test_solver_submission_then_internal_error_checks_account_once(tmp_path,monkeypatch,mode):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        account=store.put('accounts',{**account,'name':'Fixture User','email':'fixture@example.test',
            'password':'private-fixture-password','auto_otp':False,'solver_id':'solver'})
        adapter=engine.amazon=Amazon(store);adapter.watch_login=AsyncMock()
        store.put('solvers',{'provider':'2captcha','api_key':'private-fixture-key','timeout_seconds':5},'solver')
        key='creation-'+account['id']
        store.put('account_creation',{'account_id':account['id'],'submitted':True,'password_submitted':True},key)
        retry_times={};updates=[];checks=asyncio.Event();terminal=asyncio.Event();requests=[]
        def update(status,message,**extra):
            updates.append(status)
            store.put('account_creation',{**store.get('account_creation',key),'status':status,'message':message,**extra},key)
            if status in ('retailer_error','email_manual'):terminal.set()
        solve=AsyncMock(return_value='fixture-solution')
        monkeypatch.setattr(SolverService,'solve_token',solve)
        takeover=AsyncMock(wraps=recovery.recover_signup)
        monkeypatch.setattr(creation,'recover_signup',takeover)
        mcp=AsyncMock(side_effect=AssertionError('Known pages must not need MCP'))
        monkeypatch.setattr(recovery,'ScopedMCP',mcp)
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            context=await browser.new_context();context._retail_signup_authorized=True
            context._retail_input_timing_preset='aggressive'
            context._retail_signin_signup_attempted=True  # prior resume already crossed into signup
            page=await context.new_page()
            async def route(r):
                path=urlsplit(r.request.url).path
                requests.append((r.request.method,path))
                if r.request.url==FRAME:body='<h2>Fixture puzzle</h2>'
                elif path=='/fixture-wrapper':body=f'<iframe id=aacb-arkose-frame src="{FRAME}"></iframe>'
                elif path=='/ap/cvf/request':body=MARKUP.split('<script>')[0]+BRIDGE_SCRIPT
                elif path=='/ap/cvf/verify':
                    # A real form navigation, with its original CSRF fields intact.
                    assert 'cvf_aamation_response_token=fixture-amazon-session' in r.request.post_data
                    await r.fulfill(body="<script>location.replace('/ap/register')</script>",content_type='text/html');return
                elif path=='/ap/register':
                    body=ERROR+REGISTRATION
                    if mode in ('internal_later','repeated_later','retry_after'):body=body.replace('please try again.','please try again later.')
                    if mode=='cooldown':body=body.replace('please try again.','Too many requests. Try again later.')
                    if mode=='delayed':body=f'<div id=root></div><script>setTimeout(()=>document.querySelector("#root").innerHTML={repr(body)},200)</script>'
                elif path=='/gp/your-account/order-history':
                    retry_times['check']=time.monotonic()
                    checks.set()
                    if mode=='cancel':await asyncio.Event().wait()
                    body='<form method=post action=/claim><input id=ap_email name=email><button id=continue>Continue</button></form>'
                elif path=='/claim':
                    assert 'fixture%40example.test' in r.request.post_data
                    if mode=='existing':body='<form method=post action=/signed><input id=ap_password type=password><button id=signInSubmit>Sign in</button></form>'
                    else:body="<h1>Looks like you're new to Amazon</h1><form method=post action=/registration><button id=createAccountSubmit>Proceed to create an account</button></form>"
                elif path=='/registration':body=REGISTRATION
                elif path=='/registration-submit':
                    if mode in ('repeated','repeated_later'):
                        await r.fulfill(body="<script>location.replace('/ap/register')</script>",content_type='text/html');return
                    body='<h1>Verify email</h1><input id=cvf-input-code autocomplete=one-time-code>'
                elif path=='/signed':body='<div id=nav-link-accountList><span class=nav-line-1>Hello, Fixture</span></div>'
                else:raise AssertionError(path)
                if path=='/fixture-wrapper':body+=WRAPPER_BRIDGE
                headers={}
                if path=='/ap/register' and mode=='retry_after':
                    headers={'Retry-After':'1'};retry_times['response']=time.monotonic()
                await r.fulfill(body=body,content_type='text/html',headers=headers)
            await page.route('**/*',route)
            await page.goto('https://www.amazon.com/ap/cvf/request')
            if mode=='failed':adapter.authenticate=AsyncMock(side_effect=ValueError('private-error'))
            controls=SignupControls(page,'www.amazon.com',update)
            job=asyncio.create_task(creation.verify_signup(engine,account,context,page,controls,update))
            try:
                if mode=='cancel':
                    await asyncio.wait_for(checks.wait(),8);job.cancel()
                    with pytest.raises(asyncio.CancelledError):await job
                elif mode=='existing':await asyncio.wait_for(job,20)
                else:await asyncio.wait_for(terminal.wait(),20)
                if mode in ('repeated','repeated_later','cancel'):
                    job.cancel();await asyncio.gather(job,return_exceptions=True)
                    if mode=='cancel':await page.goto('https://www.amazon.com/ap/register')
                    terminal.clear()
                    job=asyncio.create_task(creation.verify_signup(engine,account,context,page,controls,update))
                    await asyncio.wait_for(terminal.wait(),5)
                record=store.get('account_creation',key)
                assert bool(record.get('registration_error_recheck_attempted'))==(mode!='cooldown')
                solve.assert_awaited_once()
                assert requests.count(('POST','/ap/cvf/verify'))==1
                assert requests.count(('GET','/gp/your-account/order-history'))==(0 if mode=='cooldown' else 1)
                if mode=='retry_after':assert retry_times['check']-retry_times['response']>=1
                if mode=='cooldown':
                    assert 'asked to try again later' in record['message']
                    assert record['recovery_status']=='restricted'
                    assert not store.all('harvesters')
                if mode=='existing':
                    adapter.watch_login.assert_awaited_once()
                    assert requests.count(('POST','/signed'))==1
                    assert ('POST','/registration-submit') not in requests
                elif mode in ('new','repeated','delayed','internal_later','repeated_later','retry_after'):
                    assert requests.count(('POST','/claim'))==1
                    assert requests.count(('POST','/registration-submit'))==1
                    assert record['reconciled_attempts'][0]['submitted']
                if mode in ('existing','cancel','failed','cooldown'):
                    assert record['captcha_phase']=='submitted' and record['captcha_submitted']
                    assert record['captcha_retailer_outcome']=='registration_error'
                    assert record['submitted']
                assert store.all('captcha_attempts')[0]['status']=='submitted'
                assert 'captcha_manual' not in updates
                assert 'recovery_blocked' not in updates
                mcp.assert_not_called()
                assert store.get('accounts',other['id'])==other
                assert not store.all('signup_recovery_runs')
            finally:
                job.cancel();await asyncio.gather(job,return_exceptions=True)
                await browser.close()
        await engine.close();store.db.close()
    asyncio.run(scenario())


def test_slow_retailer_does_not_turn_submitted_solution_into_solver_failure(tmp_path,monkeypatch):
    from test_signup_cvf_captcha import setup
    async def scenario():
        store,engine,(account,_)=fixture(tmp_path);adapter=engine.amazon=Amazon(store)
        account={**account,'solver_id':'solver'}
        store.put('solvers',{'provider':'2captcha','api_key':'private-fixture-key'},'solver')
        key='creation-'+account['id']
        store.put('account_creation',{'account_id':account['id'],'submitted':True},key)
        paused=asyncio.Event()
        def update(status,message,**extra):
            store.put('account_creation',{**store.get('account_creation',key),'status':status,'message':message,**extra},key)
            if status=='captcha_manual':paused.set()
        original=creation.changed
        async def slow(page,before,timeout=15000):
            if timeout==15000:raise TimeoutError()
            return await original(page,before,timeout)
        monkeypatch.setattr(creation,'changed',slow)
        solve=AsyncMock(return_value='fixture-token');monkeypatch.setattr(SolverService,'solve_token',solve)
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True);context,page=await setup(browser)
            await page.locator('form').evaluate("e=>e.onsubmit=event=>event.preventDefault()",isolated_context=False)
            job=asyncio.create_task(creation.verify_signup(engine,account,context,page,SignupControls(page,'www.amazon.com',update),update))
            try:
                await asyncio.wait_for(paused.wait(),8)
                record=store.get('account_creation',key)
                assert record['captcha_phase']=='submitted' and record['captcha_error']==''
                assert 'acceptance is unconfirmed' in record['message']
                assert store.all('captcha_attempts')[0]['status']=='submitted'
                solve.assert_awaited_once()
            finally:
                job.cancel();await asyncio.gather(job,return_exceptions=True);await browser.close()
        await engine.close();store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['internal','hidden','cooldown','foreign','signin','exhausted'])
def test_checkpoint_precedes_mcp_and_preserves_restrictions(tmp_path,monkeypatch,mode):
    async def scenario():
        store,engine,(account,_)=fixture(tmp_path);adapter=engine.amazon=Amazon(store)
        mcp=AsyncMock(side_effect=AssertionError('No MCP for a known page'))
        monkeypatch.setattr(recovery,'ScopedMCP',mcp)
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True);page=await browser.new_page()
            body=ERROR+REGISTRATION
            if mode=='hidden':body=ERROR.replace('<div ','<div hidden ')+REGISTRATION
            if mode=='cooldown':body=body.replace('Internal error please try again.','Too many requests. Try again later.')
            if mode=='signin':body='<form><input id=ap_email name=email></form>'
            await page.route('**/*',lambda r:r.fulfill(body=body,content_type='text/html'))
            await page.goto('https://'+('foreign.test' if mode=='foreign' else 'www.amazon.com')+'/ap/register')
            if mode=='exhausted':page._retail_signup_recovery_documents={await page.evaluate('performance.timeOrigin')}
            if mode=='foreign':
                with pytest.raises(Exception,match='region'):await recovery.recover_signup(adapter,account,page,lambda *a,**kw:None)
            else:
                expected='registration' if mode=='hidden' else 'restricted' if mode=='cooldown' else 'signin' if mode=='signin' else 'retailer_error'
                assert await recovery.recover_signup(adapter,account,page,lambda *a,**kw:None)==expected
                if mode in ('internal','exhausted'):assert (await observe(page,account)).evidence=='registration_internal_error'
            mcp.assert_not_called()
            assert not store.all('signup_recovery_runs')
            await browser.close()
        await engine.close();store.db.close()
    asyncio.run(scenario())
