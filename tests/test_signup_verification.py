import asyncio
import time
from unittest.mock import AsyncMock

from patchright.async_api import async_playwright
import pytest

from retail.account_creation import verify_signup
from retail.signup_controls import SignupControls
from retail.services import SolverService
from retail.amazon import Amazon
from test_account_care import fixture


@pytest.mark.parametrize('kind',['imap','image_service','service_failure','manual','unsupported_service','cancel'])
def test_signup_verification_is_scoped_and_single_use(tmp_path,monkeypatch,kind):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        adapter=engine.amazon=Amazon(store)
        account={**account,'mailbox_id':'linked-mailbox','solver_id':'linked-solver'}
        store.put('solvers',{'provider':'2captcha','api_key':'private-fixture-key','timeout_seconds':10},'linked-solver')
        adapter.identities.wait_code=AsyncMock(return_value={'code':'123456','source':'imap'})
        adapter.watch_login=AsyncMock()
        solve=AsyncMock(return_value='TEST')
        if kind=='service_failure':solve.side_effect=ValueError('Fixture service failure')
        monkeypatch.setattr(SolverService,'solve_image',solve)
        key='creation-'+account['id']
        store.put('account_creation',{'account_id':account['id'],'submitted':True,'otp_since':time.time()-1},key)
        def update(status,message,**extra):
            store.put('account_creation',{**store.get('account_creation',key),'status':status,'message':message,**extra},key)
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            context=await browser.new_context()
            context._retail_paced_input=True
            context._retail_input_timing_preset='aggressive'
            page=await context.new_page()
            requests=[]
            async def route(route):
                requests.append(route.request.method)
                if kind=='imap':
                    fields='<input id=cvf-input-code name=code autocomplete=one-time-code><button id=auth-signin-button>Verify email</button>'
                elif kind in ('image_service','service_failure','manual','cancel'):
                    fields='<img src="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22120%22 height=%2250%22%3E%3Ctext x=%225%22 y=%2230%22%3ETEST captcha%3C/text%3E%3C/svg%3E#captcha"><input id=captchacharacters name=captcha><button type=submit>Continue</button>'
                else:
                    fields='<iframe title="Security captcha challenge" srcdoc="Manual challenge"></iframe>'
                await route.fulfill(body=f'''<form method=post action=/ap/verify style="margin:50px">{fields}</form>
                    <script>window.submits=0;document.querySelector('form').onsubmit=e=>{{e.preventDefault();submits++;document.querySelector('form').remove();document.body.insertAdjacentHTML('beforeend','<div id=nav-link-accountList><span class=nav-line-1>Hello, Fixture</span></div>');history.replaceState(null,'','/');}};</script>''',content_type='text/html')
            await page.route('https://www.amazon.com/**',route)
            await page.goto('https://www.amazon.com/ap/verify')
            adapter.logins[account['id']]=context
            if kind in ('manual','cancel'):account['solver_id']=''
            controls=SignupControls(page,'www.amazon.com',update)
            job=asyncio.create_task(verify_signup(engine,account,context,page,controls,update))
            adapter.login_watchers[account['id']]=job
            if kind in ('manual','unsupported_service','service_failure','cancel'):
                async with asyncio.timeout(3):
                    while not store.all('harvesters'):await asyncio.sleep(.01)
                row=store.all('harvesters')[0]
                assert row['account_id']==account['id'] and 'task_id' not in row
                if kind=='cancel':
                    job.cancel()
                    with pytest.raises(asyncio.CancelledError):await job
                else:
                    await page.set_content('<div id=nav-link-accountList><span class=nav-line-1>Hello, Fixture</span></div>')
                    await page.evaluate("history.replaceState(null,'','/')")
                    await asyncio.wait_for(job,5)
                if kind=='service_failure':solve.assert_awaited_once()
                else:solve.assert_not_awaited()
            else:
                await asyncio.wait_for(job,12)
                assert await page.evaluate('submits',isolated_context=False)==1
                if kind=='imap':
                    assert adapter.identities.wait_code.await_args.args[0]['email']==account['email']
                    assert adapter.identities.wait_code.await_args.kwargs['since']==store.get('account_creation',key)['otp_since']
                    assert store.get('account_creation',key)['otp_submitted']
                    solve.assert_not_awaited()
                else:
                    solve.assert_awaited_once()
                    assert solve.await_args.args[1].startswith(b'\x89PNG')
                    assert store.get('account_creation',key)['captcha_submitted']
            if kind!='cancel':adapter.watch_login.assert_awaited_once()
            assert requests==['GET'] and store.get('accounts',other['id'])==other
            assert not store.all('harvesters')
            await context.close()
            adapter.logins.clear()
            await browser.close()
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_mcp_recovery_rejects_ambiguity_and_other_origins():
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            page=await browser.new_page()
            await page.route('**/*',lambda route:route.fulfill(body='<form><input name=email><input name=email></form>',content_type='text/html'))
            await page.goto('https://www.amazon.com/ap/register')
            controls=SignupControls(page,'www.amazon.com',lambda *_:None)
            with pytest.raises(ValueError,match='ambiguous'):await controls.find('email')
            await page.goto('https://example.test/')
            with pytest.raises(ValueError,match='region'):await controls.find('email')
            await browser.close()
    asyncio.run(scenario())
