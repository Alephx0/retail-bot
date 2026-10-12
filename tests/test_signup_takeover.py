import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright
from retail.amazon import Amazon, BackoffRequired
from retail.browser_agent import BrowserAgent
from retail.signup_recovery import recover_signup
from retail.worker_profiles import debugging_port
from retail import account_creation as creation
from test_account_care import fixture


class NavigationProvider:
    def __init__(self,connection):self.step=0
    async def turn(self,instructions,history,tools):
        assert 'navigation' in instructions
        self.step+=1
        return [{'id':str(self.step),'name':'observe_controls' if self.step==1 else 'validate_control',
                 'arguments':'{}' if self.step==1 else '{"ref":"1"}'}]
    def tool_result(self,history,call,result):
        assert 'SECRET' not in str(result)
        history.append({'role':'user','content':json.dumps(result)})


@pytest.mark.parametrize('mode',['immediate','delayed','captcha','otp','foreign','ambiguous','loop','missing','cancel','cooldown'])
def test_mcp_takes_over_until_checkpoint_without_submission(tmp_path,mode):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        adapter=engine.amazon=Amazon(store)
        store.put('settings',{'agent_mode':'recovery','ai_connection_id':'fixture','agent_timeout_seconds':15 if mode!='missing' else 3,'agent_max_steps':4},'settings')
        store.put('ai_connections',{'model':'fixture'},'fixture')
        adapter.agent=BrowserAgent(store,provider_factory=NavigationProvider)
        old={'account_id':account['id'],'submitted':True,'password_submitted':True,'captcha_submitted':True}
        store.put('account_creation',old,'creation-'+account['id'])
        updates=[];requests=[];clicked=asyncio.Event()
        async with async_playwright() as p:
            port=debugging_port();browser=await p.chromium.launch(headless=True,args=[f'--remote-debugging-port={port}','--remote-debugging-address=127.0.0.1'])
            context=await browser.new_context();context._retail_recovery_endpoint=f'http://127.0.0.1:{port}'
            page=await context.new_page()
            async def route(r):
                requests.append((r.request.method,r.request.url))
                path=r.request.url.split('www.amazon.com')[-1]
                status=200
                if path=='/ap/signin':
                    href='https://foreign.example/' if mode=='foreign' else '/'
                    body=f"<h1>Looking for Something?</h1><p>We're sorry. The Web address you entered is not a functioning page on our site.</p><a href='{href}'>Home Page</a>"
                    if mode=='ambiguous':body+=f"<a href='{href}'>Home Page</a>"
                    if mode=='missing':body='<h1>Looking for Something?</h1>'
                elif path=='/':
                    clicked.set()
                    if mode=='cancel':await asyncio.Event().wait()
                    body='<a href="/ap/signin?flow=observed">Sign in</a>'
                    if mode=='loop':body='<a href="/ap/signin">Sign in</a>'
                    if mode=='cooldown':status=429;body='<h1>Too many requests</h1>'
                else:
                    body='<form><input id=ap_email name=email><button id=continue>Continue</button></form>'
                    if mode=='delayed':body="<div id=root></div><script>setTimeout(()=>document.querySelector('#root').innerHTML='<form><input id=ap_email name=email><button id=continue>Continue</button></form>',500)</script>"
                    if mode=='captcha':body='<input id=captchacharacters>'
                    if mode=='otp':body='<input id=cvf-input-code autocomplete=one-time-code>'
                await r.fulfill(status=status,body='<title>Amazon Sign-In (local fixture)</title>'+body,content_type='text/html',headers={'Retry-After':'121'} if status==429 else {})
            await page.route('**/*',route);await page.goto('https://www.amazon.com/ap/signin')
            job=asyncio.create_task(recover_signup(adapter,account,page,lambda *a,**kw:updates.append((a,kw))))
            try:
                if mode=='cancel':
                    await asyncio.wait_for(clicked.wait(),15);job.cancel()
                    with pytest.raises(asyncio.CancelledError):await job
                elif mode in ('foreign','ambiguous','loop','missing'):
                    with pytest.raises(Exception):await job
                elif mode=='cooldown':
                    with pytest.raises(BackoffRequired) as exc:await job
                    assert exc.value.retry_after_seconds==121
                else:
                    result=await job
                    assert result==('captcha' if mode=='captcha' else 'otp' if mode=='otp' else 'signin')
                    assert updates[-1][1]['recovery_status']=='checkpoint_reached'
                    assert store.all('signup_recovery_runs')[-1]['steps']==2
                    assert len(store.all('agent_runs'))==2
                assert not getattr(context,'_retail_mcp_active',False)
                assert not store.all('harvesters')
                assert store.get('account_creation','creation-'+account['id'])['submitted']
                assert store.get('accounts',other['id'])==other
                assert all(method=='GET' and url.startswith('https://www.amazon.com/') for method,url in requests)
                assert len(browser.contexts)==1
                if mode in ('foreign','ambiguous','missing'):assert len(requests)==1
            finally:
                job.cancel();await asyncio.gather(job,return_exceptions=True);await browser.close()
        await engine.close();store.db.close()
    asyncio.run(scenario())


def test_watcher_recovers_unknown_page_before_manual_handoff(tmp_path,monkeypatch):
    from retail.signup_controls import SignupControls
    async def scenario():
        store,engine,(account,_)=fixture(tmp_path);adapter=engine.amazon=Amazon(store)
        store.put('account_creation',{'account_id':account['id'],'submitted':True},'creation-'+account['id'])
        updates=[]
        def update(status,message,**kw):updates.append(status)
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True);context=await browser.new_context();page=await context.new_page()
            await page.route('**/*',lambda r:r.fulfill(body='<h1>Looking for Something?</h1>'))
            await page.goto('https://www.amazon.com/ap/signin')
            async def recover(*args):
                assert not store.all('harvesters')
                await page.set_content('<form><input id=ap_email name=email></form>')
                return 'signin'
            takeover=AsyncMock(side_effect=recover);monkeypatch.setattr(creation,'recover_signup',takeover)
            entered=asyncio.Event()
            async def authenticate(*args):entered.set();await asyncio.Event().wait()
            adapter.authenticate=AsyncMock(side_effect=authenticate);adapter.logins[account['id']]=context
            job=asyncio.create_task(creation.verify_signup(engine,account,context,page,SignupControls(page,'www.amazon.com',update),update))
            await asyncio.wait_for(entered.wait(),5)
            takeover.assert_awaited_once();assert not store.all('harvesters')
            assert not any(s in ('captcha_manual','email_manual') for s in updates)
            job.cancel();await asyncio.gather(job,return_exceptions=True)
            await browser.close();adapter.logins.clear()
        await engine.close();store.db.close()
    asyncio.run(scenario())


def test_resume_error_page_recovers_and_returns_to_registration(tmp_path):
    async def scenario():
        store,engine,(account,_)=fixture(tmp_path)
        account=store.put('accounts',{**account,'name':'Fixture User','email':'fixture@example.test','password':'SECRET1','auto_otp':False})
        adapter=engine.amazon=Amazon(store);adapter.expose=AsyncMock()
        adapter.agent=BrowserAgent(store,provider_factory=NavigationProvider)
        store.put('settings',{'agent_mode':'recovery','ai_connection_id':'fixture','agent_timeout_seconds':30,'agent_max_steps':4},'settings')
        store.put('ai_connections',{'model':'fixture'},'fixture')
        key='creation-'+account['id']
        store.put('account_creation',{'account_id':account['id'],'submitted':True,'password_submitted':True,'new_account_submitted':True},key)
        submissions=[]
        async with async_playwright() as p:
            port=debugging_port();browser=await p.chromium.launch(headless=True,args=[f'--remote-debugging-port={port}','--remote-debugging-address=127.0.0.1'])
            context=await browser.new_context();context._retail_recovery_endpoint=f'http://127.0.0.1:{port}'
            context._retail_input_timing_preset='aggressive'
            async def route(r):
                path=r.request.url.split('www.amazon.com')[-1]
                if r.request.method=='POST':submissions.append(path)
                if path=='/gp/your-account/order-history':body='<h1>Looking for Something?</h1><a href=/>Home Page</a>'
                elif path=='/':body='<a href="/ap/signin?valid=fixture">Sign in</a>'
                elif path.startswith('/ap/signin?'):body='<form method=post action=/claim><input id=ap_email name=email><button id=continue>Continue</button></form>'
                elif path=='/claim':body="<h1>Looks like you're new to Amazon</h1><form method=post action=/register><input type=submit aria-labelledby=proceed><span id=proceed>Proceed to create an account</span></form>"
                elif path=='/register':body='<form method=post action=/verify><input id=ap_customer_name name=customerName><input id=ap_email name=email><input id=ap_password name=password type=password><button id=continue>Create account</button></form>'
                elif path=='/verify':body='<h1>Verify email</h1><input id=cvf-input-code autocomplete=one-time-code>'
                else:raise AssertionError('Unexpected route')
                await r.fulfill(body='<title>Amazon Sign-In (local fixture)</title>'+body,content_type='text/html')
            await context.route('**/*',route)
            adapter.context=AsyncMock(return_value=context)
            try:
                await asyncio.wait_for(creation.create_account(engine,account,resume=True),35)
                async with asyncio.timeout(5):
                    while store.get('account_creation',key).get('status')!='email_manual':await asyncio.sleep(.01)
                assert submissions==['/claim','/register','/verify']
                assert store.get('account_creation',key)['recovery_checkpoint']=='signin'
                assert store.get('account_creation',key)['reconciled_attempts'][0]['submitted']
                assert store.all('signup_recovery_runs')[0]['steps']==2
                assert len(store.all('agent_runs'))==2
                assert store.all('harvesters')[0]['challenge_type']==''
                assert not getattr(context,'_retail_mcp_active',False)
            finally:
                for job in list(adapter.login_watchers.values()):job.cancel()
                await asyncio.gather(*adapter.login_watchers.values(),return_exceptions=True)
                await context.close();adapter.logins.clear();await browser.close()
        await engine.close();store.db.close()
    asyncio.run(scenario())
