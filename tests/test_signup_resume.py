import asyncio
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright
from retail import account_creation as creation
from retail.amazon import Amazon
from retail.signup_controls import SignupControls
from retail.worker_profiles import debugging_port
from test_account_care import fixture


@pytest.mark.parametrize('mode',['new','existing','ambiguous','cancel','uncertain','open'])
def test_resume_reconciles_identifier_before_registration(tmp_path,mode):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        account=store.put('accounts',{**account,'name':'Test User','email':'test@example.test','password':'secret1'})
        engine.amazon=adapter=Amazon(store)
        adapter.expose=AsyncMock(); adapter.watch_login=AsyncMock()
        key='creation-'+account['id']
        store.put('account_creation',{'account_id':account['id'],'status':'captcha_manual','message':'Old captcha',
            'submitted':True,'password_submitted':True,'claim_submitted':True,'new_account_submitted':True,'captcha_submitted':True},key)
        counts={'identifier':0,'registration':0,'signin':0,'proceed':0}
        registration=asyncio.Event()
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            ctx=await browser.new_context();ctx._retail_input_timing_preset='aggressive'
            async def route(r):
                path=r.request.url.split('www.amazon.com')[-1]
                if path in ('/ap/signin','/gp/your-account/order-history'):
                    body='<form action=/ax/claim method=post><input id=ap_email name=email><button id=continue>Continue</button></form>'
                elif path=='/ax/claim':
                    counts['identifier']+=1
                    assert 'test%40example.test' in r.request.post_data
                    if mode=='existing':
                        body='<form action=/signed method=post><input id=ap_password type=password><button id=signInSubmit>Sign in</button></form>'
                    elif mode=='uncertain':body='<h1>Additional verification required</h1>'
                    else:
                        body="<h1>Looks like you&#8217;re new to Amazon</h1><form action=/registration method=post><button>Proceed to create an account</button>"+('<button>Proceed to create an account</button>' if mode=='ambiguous' else '')+'</form>'
                elif path=='/registration':
                    counts['proceed']+=1
                    body='<form action=/verify method=post><input id=ap_customer_name name=customerName><input id=ap_email name=email><input id=ap_password name=password type=password><button id=continue>Create account</button></form>'
                elif path=='/verify':
                    counts['registration']+=1;registration.set()
                    if mode=='cancel':await asyncio.Event().wait()
                    body='<h1>Verify email</h1><input id=cvf-input-code autocomplete=one-time-code>'
                else:
                    counts['signin']+=1
                    body='<div id=nav-link-accountList><span class=nav-line-1>Hello, Test</span></div>'
                await r.fulfill(body=body,content_type='text/html')
            await ctx.route('**/*',route)
            async def context(*args,**kw):return ctx
            adapter.context=context
            if mode=='open':
                page=await ctx.new_page();await page.goto('https://www.amazon.com/ax/claim/intent')
                adapter.logins[account['id']]=ctx
                await engine.browser_slots.acquire()
            job=asyncio.create_task(creation.create_account(engine,account,resume=True))
            try:
                if mode=='cancel':
                    await asyncio.wait_for(registration.wait(),15);job.cancel()
                    with pytest.raises(asyncio.CancelledError):await job
                else:await asyncio.wait_for(job,20)
                record=store.get('account_creation',key)
                if mode in ('new','open','cancel'):
                    assert counts['registration']==1 and counts['proceed']==1
                    assert record['submitted'] and record['password_submitted']
                    assert record['reconciled_attempts'][0]['submitted']
                    assert not record['captcha_submitted']
                elif mode=='existing':assert counts['signin']==1 and counts['registration']==0
                elif mode=='uncertain':assert record['submitted'] and not record.get('reconciled_attempts')
                else:assert counts['registration']==0 and counts['proceed']==0
                assert counts['identifier']==1
                assert store.get('accounts',other['id'])==other
                assert len(browser.contexts)<=1
            finally:
                for watcher in list(adapter.login_watchers.values()):watcher.cancel()
                await asyncio.gather(*adapter.login_watchers.values(),return_exceptions=True)
                await ctx.close();adapter.logins.clear();await browser.close()
        await engine.close();store.db.close()
    asyncio.run(scenario())


def test_signup_action_fallback_uses_real_chrome_devtools_mcp():
    async def scenario():
        async with async_playwright() as p:
            port=debugging_port()
            browser=await p.chromium.launch(headless=True,args=[f'--remote-debugging-port={port}','--remote-debugging-address=127.0.0.1'])
            context=await browser.new_context();context._retail_recovery_endpoint=f'http://127.0.0.1:{port}'
            page=await context.new_page()
            await page.route('**/*',lambda r:r.fulfill(body='<h1>Looks like you&#8217;re new to Amazon</h1><form><button id=renamed>Proceed to create an account</button></form>'))
            await page.goto('https://www.amazon.com/ax/claim')
            updates=[]
            controls=SignupControls(page,'www.amazon.com',lambda *args:updates.append(args))
            target=await controls.find('new_account')
            assert await target.get_attribute('id')=='renamed'
            assert updates and not getattr(context,'_retail_mcp_active',False)
            assert await controls.inspect_loading()=='inspected'
            assert await controls.inspect_loading()=='already_inspected'
            assert not getattr(context,'_retail_mcp_active',False)
            assert len(browser.contexts)==1
            await browser.close()
    asyncio.run(scenario())


def test_new_account_response_cannot_reset_another_identifier(tmp_path):
    from retail.account_lifecycle import authorized_signup
    async def scenario():
        store,engine,(account,_)=fixture(tmp_path)
        adapter=Amazon(store)
        key='creation-'+account['id']
        original={'account_id':account['id'],'submitted':True,'password_submitted':True}
        store.put('account_creation',original,key)
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            context=await browser.new_context();context._retail_signup_authorized=True
            page=await context.new_page()
            await page.route('**/*',lambda r:r.fulfill(body="<h1>Looks like you're new to Amazon</h1><button>Proceed to create an account</button>"))
            await page.goto('https://www.amazon.com/ax/claim')
            for identity in (None,'different@example.test'):
                context._retail_signup_identifier=identity
                assert not await authorized_signup(adapter,account,page)
                assert store.get('account_creation',key)['submitted']
                assert not store.get('account_creation',key).get('reconciled_attempts')
            await browser.close()
        await engine.close();store.db.close()
    asyncio.run(scenario())
