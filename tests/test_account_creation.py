import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from patchright.async_api import async_playwright, expect
import pytest

from retail.account_creation import create_account, snapshot
from retail.amazon import Amazon
from retail.app import create_app
from test_account_care import fixture, Adapter


@pytest.mark.parametrize('mode',['verification','delayed','offsite','cancel','missing','email_first','recovery','proceed','proceed_delayed'])
def test_signup_fixture_submits_once_and_retains_account_ownership(tmp_path,mode):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        account=store.put('accounts',{**account,'name':'Sam','email':'sam@example.test','password':'secret1','proxy':''})
        engine.amazon=adapter=Amazon(store)
        contexts,requests,submitted=[],[],asyncio.Event()
        adapter.expose=AsyncMock()
        adapter.watch_login=AsyncMock(side_effect=lambda *_:None)
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            async def context(*args,**kwargs):
                assert kwargs['headless'] is False and kwargs['interactive']
                assert args[0]['session'] is None and args[0]['session_storage']=={}
                ctx=await browser.new_context(viewport={'width':1100,'height':800})
                ctx._retail_input_timing_preset='aggressive'
                contexts.append(ctx)
                await ctx.expose_function('submitted',submitted.set)
                async def route(route):
                    requests.append((route.request.method,route.request.url))
                    if urlsplit(route.request.url).path=='/':
                        menu_id='changed-menu' if mode=='recovery' else 'nav-link-accountList'
                        popup_id='changed-popup' if mode=='recovery' else 'nav-flyout-ya-newCust'
                        await route.fulfill(body=f'''<style>#popup{{display:none}}header:hover #popup{{display:block}}</style>
                          <header style="height:160px"><a id="{menu_id}" href="#">Account &amp; Lists</a>
                          <div id="popup"><div id="{popup_id}"><a aria-label="New to Amazon? Start here to create an account" href="https://www.amazon.com/ap/register?fixture=menu">Start here.</a></div></div></header>''',content_type='text/html')
                        return
                    if mode in ('email_first','proceed','proceed_delayed') and urlsplit(route.request.url).path=='/ap/register':
                        await route.fulfill(body='''<form method=post action="/ax/claim"><input id=ap_email_login name=email aria-label="Enter mobile number or email"><button>Continue</button></form>''',content_type='text/html')
                        return
                    if mode in ('proceed','proceed_delayed') and urlsplit(route.request.url).path=='/ax/claim':
                        delay=500 if mode=='proceed_delayed' else 0
                        await route.fulfill(body=f'''<h1>Looks like you're new to Amazon</h1>
                            <script>setTimeout(()=>document.body.insertAdjacentHTML('beforeend',
                            '<form method=post action=/ap/full-registration><button aria-labelledby="proceed-label"><span id="proceed-label">Proceed to create an account</span></button></form>'),{delay});</script>''',content_type='text/html')
                        return
                    action='https://example.test/collect' if mode=='offsite' else '/ap/register'
                    body=f'''<form method=post action="{action}" style="width:350px;margin:50px">
                    <label>Name<input id=ap_customer_name name=name></label>
                    <label>Email<input id=ap_email name=email></label>
                    <label>Password<input id=ap_password name=password type=password></label>
                    <label>Confirm<input id=ap_password_check name=confirmation type=password></label>
                    <button id=continue>Create account</button></form><script>
                    window.clicks=0;window.keys=0;addEventListener('keydown',()=>keys++);
                    document.querySelector('form').onsubmit=e=>{{e.preventDefault();clicks++;window.submitted();
                    {"" if mode in ('missing','cancel') else "setTimeout(()=>{document.querySelector('form').remove();document.body.insertAdjacentHTML('beforeend','<input id=cvf-input-code>');},"+('500' if mode=='delayed' else '0')+");"}
                    }};</script>'''
                    if mode=='recovery':
                        body=body.replace('id=ap_customer_name name=name','id=changed_name name=customerName autocomplete=name').replace('id=ap_email','id=changed_email').replace('id=ap_password_check','id=changed_check').replace('id=ap_password','id=changed_password').replace('id=continue','id=changed_submit')
                    await route.fulfill(body=body,content_type='text/html')
                await ctx.route('**/*',route)
                return ctx
            adapter.context=context
            job=asyncio.create_task(create_account(engine,account))
            if mode=='cancel':
                await asyncio.wait_for(submitted.wait(),20)
                job.cancel()
                with pytest.raises(asyncio.CancelledError):await job
                assert engine.browser_slots._value==1 and account['id'] not in adapter.logins
            else:
                await job
                page=contexts[0].pages[0]
                assert engine.browser_slots.locked() and account['id'] in adapter.logins
                clicks=await page.evaluate('clicks',isolated_context=False)
                keys=await page.evaluate('keys',isolated_context=False)
                assert clicks==(0 if mode=='offsite' else 1)
                assert keys==0 if mode=='offsite' else keys>0
                record=store.get('account_creation','creation-'+account['id'])
                assert record.get('submitted') is (mode!='offsite')
                if mode!='offsite':
                    with pytest.raises(ValueError,match='already submitted'):
                        await create_account(engine,account)
                    assert await page.evaluate('clicks',isolated_context=False)==1
                assert len(contexts)==1
                assert all(url.startswith('https://www.amazon.com/') for method,url in requests)
                assert [method for method,_ in requests]==(['GET','GET','POST','POST'] if mode in ('proceed','proceed_delayed') else ['GET','GET','POST'] if mode=='email_first' else ['GET','GET'])
                if mode in ('proceed','proceed_delayed'):
                    assert record['claim_submitted'] and record['new_account_submitted']
                assert requests[0][1]=='https://www.amazon.com/' and 'fixture=menu' in requests[1][1]
                await contexts[0].close()
                adapter.logins.pop(account['id'],None)
                assert engine.browser_slots._value==1
                assert snapshot(engine)[0]['status']==('draft' if mode=='offsite' else 'review')
            assert store.get('accounts',other['id'])==other
            assert not store.get('accounts',account['id']).get('logged_in')
            assert 'secret1' not in json.dumps(store.all('account_creation'))
            from retail.account_consistency import AccountBrowserProfiles
            profiles=AccountBrowserProfiles(store)
            first=profiles.get(account)
            assert first['seed']==profiles.get(account)['seed'] and first['seed']!=profiles.get(other)['seed']
            await browser.close()
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_creation_capacity_and_durable_replay_guard(tmp_path):
    async def scenario():
        store,engine,(account,_)=fixture(tmp_path)
        engine.group_coordinator=SimpleNamespace(pool=SimpleNamespace(trim=AsyncMock()))
        engine.evict_ready=AsyncMock()
        await engine.browser_slots.acquire()
        with pytest.raises(ValueError,match='slots are busy'):await create_account(engine,account)
        assert not engine.amazon.contexts
        engine.browser_slots.release()
        store.put('account_creation',{'account_id':account['id'],'submitted':True,'status':'submitted','message':'Submitted'},'creation-'+account['id'])
        with pytest.raises(ValueError,match='already submitted'):await create_account(engine,account)
        assert snapshot(engine)[0]['status']=='review'
        store.put('accounts',{**account,'logged_in':True})
        assert snapshot(engine)[0]['status']=='completed'
        assert engine.browser_slots._value==1
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_browser_launch_failure_releases_capacity_without_submission(tmp_path):
    async def scenario():
        store,engine,(account,_)=fixture(tmp_path)
        engine.amazon.context=AsyncMock(side_effect=RuntimeError('Private launch detail'))
        with pytest.raises(ValueError,match='could not open'):
            await create_account(engine,account)
        assert engine.browser_slots._value==1 and not engine.amazon.logins
        record=store.get('account_creation','creation-'+account['id'])
        assert not record['submitted'] and 'Private' not in json.dumps(record)
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_signup_cooldown_prevents_new_requests(tmp_path):
    import time
    async def scenario():
        store,engine,(account,_)=fixture(tmp_path)
        store.put('account_creation',{'account_id':account['id'],'status':'review','message':'Retailer pause','retry_at':time.time()+1800},'creation-'+account['id'])
        with pytest.raises(ValueError,match='cooldown'):await create_account(engine,account)
        assert not engine.amazon.contexts and snapshot(engine)[0]['status']=='cooldown'
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_signup_api_respects_active_tasks_and_retailer_support(tmp_path,monkeypatch):
    import retail.account_creation as creation
    signup=AsyncMock()
    monkeypatch.setattr(creation,'create_account',signup)
    with TestClient(create_app(tmp_path),headers={'X-Retail-Client':'dashboard'}) as client:
        engine=client.app.state.engine
        account=client.post('/api/accounts',json={'name':'Fixture','email':'fixture@example.test','password':'secret1'}).json()
        client.app.state.store.put('tasks',{'id':'active-signup-fixture','account_id':account['id']},'active-signup-fixture')
        engine.jobs['active-signup-fixture']=object()
        try:
            for action in ('register-auto','register-resume'):
                response=client.post(f"/api/accounts/{account['id']}/{action}")
                assert response.status_code==409
            signup.assert_not_awaited()
        finally:
            engine.jobs.pop('active-signup-fixture')
        second=client.post('/api/accounts',json={'name':'Other retailer','retailer':'walmart'}).json()
        assert client.post(f"/api/accounts/{second['id']}/register-auto").status_code==409
        signup.assert_not_awaited()


def test_account_creation_tab_and_new_record_flow(tmp_path,monkeypatch):
    import retail.account_creation as creation
    calls=[]
    async def signup(engine,account,*,resume=False):
        calls.append(account['id'])
        engine.store.put('account_creation',{'account_id':account['id'],'submitted':True,'status':'verification','message':'Complete verification in the browser.'},'creation-'+account['id'])
        engine.amazon.logins[account['id']]=SimpleNamespace(close=AsyncMock())
        return {'ok':True,'message':'Complete verification in the browser.'}
    monkeypatch.setattr(creation,'create_account',signup)
    with TestClient(create_app(tmp_path),headers={'X-Retail-Client':'dashboard'}) as client:
        client.app.state.engine.amazon=Adapter()
        mailbox=client.post('/api/mailboxes',json={'name':'Signup inbox','host':'imap.example.test','username':'owner@example.test','password':'fixture-mail-secret'}).json()
        solver=client.post('/api/solvers',json={'name':'Image service','provider':'2captcha','api_key':'fixture-solver-secret'}).json()
        sms=client.post('/api/sms_providers',json={'name':'Signup SMS','api_key':'fixture-sms-secret','max_price':.50}).json()
        async def scenario():
            async with async_playwright() as driver:
                browser=await driver.chromium.launch(headless=True)
                page=await browser.new_page(viewport={'width':1440,'height':1000})
                errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                async def route(route):
                    url=urlsplit(route.request.url)
                    response=client.request(route.request.method,url.path,content=route.request.post_data)
                    await route.fulfill(status=response.status_code,body=response.content,headers={'content-type':response.headers.get('content-type','text/plain')})
                await page.route('**/*',route)
                await page.goto('http://127.0.0.1/')
                await page.locator('[data-view=accounts]').click()
                await page.get_by_role('tab',name='Sessions & relationships').click()
                await page.get_by_role('tab',name='Sessions & relationships').press('ArrowRight')
                await expect(page.get_by_role('tab',name='Account creation',exact=True)).to_be_focused()
                await expect(page.get_by_role('heading',name='Create your first account')).to_be_visible()
                await page.locator('#primary').click()
                await page.get_by_label('Your name',exact=True).fill('Signup fixture')
                await page.get_by_label('Email address',exact=True).fill('new@example.test')
                await page.get_by_label('Password',exact=True).fill('fixture-secret')
                await page.get_by_text('Region, connection & verification',exact=True).click()
                await page.get_by_label('Verification mailbox (IMAP)').select_option(mailbox['id'])
                await page.get_by_label('CAPTCHA handling').select_option(solver['id'])
                await page.get_by_label('Phone verification',exact=True).select_option(sms['id'])
                await page.get_by_role('button',name='Save & start signup').click()
                await expect(page.locator('.creation-table')).to_contain_text('Browser open')
                assert len(calls)==1
                account=client.app.state.store.all('accounts')[0]
                assert account['password']=='fixture-secret' and not account.get('logged_in')
                assert account['mailbox_id']==mailbox['id'] and account['solver_id']==solver['id']
                assert account['sms_provider_id']==sms['id']
                await expect(page.locator('.creation-table')).to_contain_text('Email codes: Signup inbox')
                response=client.get('/api/state').json()
                assert 'password' not in response['accounts'][0]
                assert 'fixture-secret' not in json.dumps(response)
                assert 'fixture-sms-secret' not in json.dumps(response)
                output=Path('artifacts/account-creation-review');output.mkdir(parents=True,exist_ok=True)
                for width in (1440,390):
                    await page.set_viewport_size({'width':width,'height':1000})
                    await page.screenshot(path=str(output/f'workspace-{width}.png'),full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    await page.locator('#primary').click()
                    await page.get_by_label('Your name',exact=True).fill('Another account')
                    await page.get_by_label('Email address',exact=True).fill('new@example.test')
                    await page.get_by_label('Password',exact=True).fill('another-secret')
                    await page.get_by_role('button',name='Save & start signup').click()
                    await expect(page.locator('.creation-form .form-error')).to_contain_text('already has an account record')
                    await page.get_by_text('Region, connection & verification',exact=True).click()
                    await page.screenshot(path=str(output/f'form-{width}.png'),full_page=True)
                    await page.get_by_role('button',name='Cancel',exact=True).click()
                assert len(calls)==1 and len(client.app.state.store.all('accounts'))==1
                client.app.state.store.put('harvesters',{'account_id':account['id'],'status':'waiting','message':'Signup challenge','at':'2026-10-11T12:00:00+00:00'},'signup-'+account['id'])
                client.app.state.store.put('account_creation',{'account_id':account['id'],'submitted':True,'status':'captcha_manual','captcha_type':'arkose','message':'Arkose / FunCaptcha — user input required. Solve it in the signup browser.'},'creation-'+account['id'])
                await page.locator('[data-view=tools]').click()
                await page.get_by_role('tab',name='Verification',exact=True).click()
                await expect(page.get_by_role('button',name='Open signup browser',exact=True)).to_be_visible()
                await expect(page.get_by_role('heading',name='Signup SMS verification')).to_be_visible()
                await expect(page.get_by_text('$0.50 per verification',exact=False)).to_be_visible()
                for width in (1440,390):
                    await page.set_viewport_size({'width':width,'height':1000})
                    await page.screenshot(path=str(output/f'sms-connections-{width}.png'),full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                await page.get_by_role('button',name='Account creation',exact=True).click()
                await expect(page.get_by_role('heading',name='Account creation',exact=True)).to_be_visible()
                await expect(page.locator('.creation-table')).to_contain_text('CAPTCHA — user input required')
                for width in (1440,390):
                    await page.set_viewport_size({'width':width,'height':1000})
                    await page.screenshot(path=str(output/f'captcha-{width}.png'),full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                for status,label in [('retry_waiting','Retry scheduled'),('reconciling','Checking account'),('retailer_error','Amazon registration error'),('recovering','Recovering signup'),('resuming','Resuming automation'),('recovery_blocked','Recovery paused'),('captcha_loading','Loading verification'),('email_waiting','Waiting for email code'),('email_manual','Email code needed'),('email_blocked','Automatic email verification paused')]:
                    messages={'retry_waiting':'Amazon requested a retry delay. One automatic account check will run when it expires.','reconciling':'Amazon returned an internal registration error. Checking whether the account was created before continuing.','retailer_error':'Amazon returned an internal registration error. The automatic account check has not resolved it. Resume signup to check the account again.','recovering':'MCP/CDP is recovering signup navigation.','resuming':'Automation checkpoint reached; resuming signup.','recovery_blocked':'Automatic recovery exhausted the supported actions.','captcha_loading':'Waiting for the verification frame to load.','email_waiting':'Waiting for a fresh matching email code through IMAP.','email_blocked':'Automatic email verification paused: the verification form is not ready.','email_manual':'Email verification needs your input.'}
                    client.app.state.store.put('account_creation',{'account_id':account['id'],'submitted':True,'status':status,'message':messages[status]},'creation-'+account['id'])
                    await page.reload()
                    await page.locator('[data-view=accounts]').click()
                    await page.get_by_role('tab',name='Account creation',exact=True).click()
                    await expect(page.locator('.creation-table')).to_contain_text(label)
                    for width in (1440,390):
                        await page.set_viewport_size({'width':width,'height':1000})
                        await page.screenshot(path=str(output/f'{status}-{width}.png'),full_page=True)
                        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                client.app.state.engine.amazon.logins.pop(account['id'],None)
                await page.reload()
                await page.locator('[data-view=accounts]').click()
                await page.get_by_role('tab',name='Account creation',exact=True).click()
                await expect(page.get_by_role('button',name='Resume signup',exact=True)).to_be_visible()
                assert await page.get_by_role('button',name='Open sign-in',exact=True).count()==0
                for width in (1440,390):
                    await page.set_viewport_size({'width':width,'height':1000})
                    await page.screenshot(path=str(output/f'resume-{width}.png'),full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert not errors
                await browser.close()
        asyncio.run(scenario())
