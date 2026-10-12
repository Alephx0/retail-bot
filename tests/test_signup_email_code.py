import asyncio
import json
import time
from email.message import EmailMessage
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from patchright.async_api import async_playwright

from retail import account_creation as creation
from retail.identity import IdentityService, extract_otp
from retail.signup_controls import SignupControls, CLASSIFY
from retail.amazon import Amazon
from test_account_care import fixture


@pytest.mark.parametrize('transition', ['close', 'navigate'])
def test_email_wait_stops_on_browser_lifecycle_signal(transition):
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            page=await browser.new_page()
            await page.route('**/*',lambda route:route.fulfill(body='<h1>Verify email</h1>',content_type='text/html'))
            await page.goto('https://www.amazon.com/ap/cvf/verify')
            entered,stopped=asyncio.Event(),asyncio.Event()
            async def waiting(*args,**kwargs):
                entered.set()
                try:await asyncio.Event().wait()
                finally:stopped.set()
            service=SimpleNamespace(wait_code=AsyncMock(side_effect=waiting))
            job=asyncio.create_task(creation.wait_signup_email(service,{},page,0,AsyncMock()))
            try:
                await asyncio.wait_for(entered.wait(),2)
                if transition=='close':await page.close()
                else:await page.goto('https://www.amazon.com/')
                with pytest.raises(ValueError,match='page changed'):await asyncio.wait_for(job,2)
                assert stopped.is_set()
                service.wait_code.assert_awaited_once()
            finally:
                job.cancel();await asyncio.gather(job,return_exceptions=True)
                await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['shadowed_action','delayed_email','security_label','otp_aria_label','otp_labelledby','name_code','late','late_button','missing_mailbox','disabled','already_submitted','ambiguous','offsite','cancel','changed'])
def test_email_verification_screen_routes_to_imap(tmp_path,monkeypatch,mode):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        adapter=engine.amazon=Amazon(store)
        account={**account,'mailbox_id':'' if mode=='missing_mailbox' else 'assigned','auto_otp':mode!='disabled'}
        key='creation-'+account['id']
        store.put('account_creation',{'account_id':account['id'],'submitted':True,'otp_since':time.time()-1,'otp_submitted':mode=='already_submitted'},key)
        updates=[]
        def update(status,message,**extra):
            updates.append(status)
            store.put('account_creation',{**store.get('account_creation',key),'status':status,'message':message,**extra},key)
        adapter.watch_login=AsyncMock()
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            context=await browser.new_context(viewport={'width':800,'height':700})
            context._retail_paced_input=True
            context._retail_input_timing_preset='aggressive'
            page=await context.new_page()
            await page.route('**/*',lambda route:route.fulfill(body='<main></main>',content_type='text/html'))
            await page.goto('https://www.amazon.com/ap/cvf/verify')
            body='''<h1>Verify email address</h1><p>To verify your email, we've sent a One Time Password (OTP).</p>
              <form action="ACTION"><label for=security-field>Enter security code</label><input id=security-field name=verification_response>
              <button type=submit>Verify</button></form><a href="#">Resend code</a>
              <script>window.submits=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submits++;if(document.querySelector('input').value!=='723456')throw Error('Wrong OTP');document.body.innerHTML='<div id=nav-link-accountList><span class=nav-line-1>Hello, Fixture</span></div>';};</script>'''
            body=body.replace('ACTION','https://example.test/collect' if mode=='offsite' else '/ap/cvf/verify')
            if mode in ('otp_aria_label','otp_labelledby'):
                label='aria-label="Verify OTP Button"' if mode=='otp_aria_label' else 'aria-labelledby="verify-label"'
                body=body.replace('<button type=submit>Verify</button>',f'<span id=cvf-submit-otp-button><input type=submit value=Verify {label}></span><span id=verify-label hidden>Verify OTP Button</span>')
            if mode in ('shadowed_action','delayed_email'):
                body=body.replace('<form action="/ap/cvf/verify">','<form action="verify"><input type=hidden name=action value=verify>')
                body=body.replace('id=security-field name=verification_response','id=cvf-input-code name=code maxlength=6 autocomplete=off')
                body=body.replace('<button type=submit>Verify</button>','<span id=cvf-submit-otp-button><input type=submit aria-label="Verify OTP Button" value=Verify></span>')
                body=body.replace("document.querySelector('input').value","document.querySelector('#cvf-input-code').value")
            if mode=='name_code':body=body.replace('Enter security code','').replace('name=verification_response','name=code')
            if mode=='ambiguous':body=body.replace('<button type=submit>Verify</button>','<button type=submit>Verify</button><button type=submit>Verify</button>')
            if mode=='late_button':body=body.replace('<button type=submit>','<button disabled type=submit>')
            if mode!='late':await page.set_content(body)
            entered=asyncio.Event()
            delivered=asyncio.Event()
            attempts=0
            async def get_code(*args,**kwargs):
                nonlocal attempts
                attempts+=1
                entered.set()
                if mode=='delayed_email' and attempts==1:
                    assert store.get('account_creation',key)['status']=='email_waiting'
                    assert not store.all('harvesters')
                    await delivered.wait()
                if mode=='cancel':await asyncio.Event().wait()
                if mode=='changed':await page.set_content('<h1>Verification changed</h1>')
                return {'code':'723456','source':'imap'}
            adapter.identities.wait_code=AsyncMock(side_effect=get_code)
            adapter.logins[account['id']]=context
            job=asyncio.create_task(creation.verify_signup(engine,account,context,page,SignupControls(page,'www.amazon.com',update),update))
            adapter.login_watchers[account['id']]=job
            try:
                if mode=='delayed_email':
                    await asyncio.wait_for(entered.wait(),3)
                    assert not job.done()
                    delivered.set()
                if mode=='late':
                    async with asyncio.timeout(3):
                        while (store.get('account_creation',key) or {}).get('status') not in ('recovering','recovery_blocked','verification','email_manual','email_blocked'):await asyncio.sleep(.01)
                    await page.set_content(body)
                if mode=='late_button':
                    async with asyncio.timeout(3):
                        while (store.get('account_creation',key) or {}).get('status') not in ('recovering','recovery_blocked','verification','email_manual','email_blocked'):await asyncio.sleep(.01)
                    adapter.identities.wait_code.assert_not_awaited()
                    await page.locator('button').evaluate('e=>e.disabled=false')
                if mode in ('shadowed_action','delayed_email','security_label','otp_aria_label','otp_labelledby','name_code','late','late_button'):
                    await asyncio.wait_for(job,10)
                    assert adapter.identities.wait_code.await_count==1
                    requested=adapter.identities.wait_code.await_args
                    assert requested.args[0]['email']==account['email'] and requested.args[0]['mailbox_id']=='assigned'
                    assert requested.kwargs['since']==store.get('account_creation',key)['otp_since']
                    assert await page.evaluate('submits',isolated_context=False)==1
                    assert 'email_waiting' in updates and 'email_submitted' in updates
                    assert store.get('account_creation',key)['otp_submitted']
                    adapter.watch_login.assert_awaited_once()
                elif mode=='cancel':
                    await asyncio.wait_for(entered.wait(),5)
                    job.cancel()
                    with pytest.raises(asyncio.CancelledError):await job
                    assert not store.get('account_creation',key).get('otp_submitted')
                    assert await page.evaluate('submits',isolated_context=False)==0
                else:
                    async with asyncio.timeout(5):
                        while (store.get('account_creation',key) or {}).get('status') not in ('recovering','recovery_blocked','verification','email_manual','email_blocked'):await asyncio.sleep(.01)
                    expected_status='email_manual' if mode in ('disabled','missing_mailbox') else 'email_blocked'
                    assert expected_status in updates
                    if expected_status=='email_blocked':assert not store.all('harvesters')
                    message=store.get('account_creation',key)['message']
                    expected={'missing_mailbox':'no IMAP mailbox','disabled':'disabled','already_submitted':'already submitted','ambiguous':'missing or ambiguous','offsite':'destination is outside','changed':'page changed'}[mode]
                    assert expected in message
                    if mode=='changed':adapter.identities.wait_code.assert_awaited_once()
                    else:
                        adapter.identities.wait_code.assert_not_awaited()
                        assert await page.evaluate('submits',isolated_context=False)==0
                    await page.set_content('<div id=nav-link-accountList><span class=nav-line-1>Hello, Fixture</span></div>')
                    await asyncio.wait_for(job,5)
                assert store.get('accounts',other['id'])==other
                assert '723456' not in json.dumps(store.all('account_creation')+store.all('harvesters'))
            finally:
                job.cancel()
                await asyncio.gather(job,return_exceptions=True)
                await context.close()
                adapter.logins.clear()
                await browser.close()
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_security_code_classifier_does_not_use_payment_fields():
    async def scenario():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            page=await browser.new_page()
            await page.set_content('<form><label>Enter security code<input name=cvv autocomplete=cc-csc></label><button>Verify</button></form>')
            assert not await page.locator('input').evaluate('(e)=>('+CLASSIFY+')(e,"otp")')
            assert not await page.locator('button').evaluate('(e)=>('+CLASSIFY+')(e,"otp_submit")')
            await browser.close()
    asyncio.run(scenario())


def test_parenthesized_amazon_otp_and_forwarded_recipient():
    mail=EmailMessage()
    mail['From']='account-update@amazon.com'
    mail['To']='mailbox-owner@example.test'
    mail['X-Original-To']='signup@example.test'
    mail.set_content('<p>To verify your email, use this One Time Password (OTP):</p><b>&nbsp;723456&nbsp;</b>',subtype='html')
    raw=mail.as_bytes()
    assert extract_otp(raw,'signup@example.test',('amazon.com',))=='723456'
    assert extract_otp(raw,'different-account@example.test',('amazon.com',)) is None
    mail.set_content('Your order number (OTP shipping reference) is 723456.')
    assert extract_otp(mail.as_bytes(),'signup@example.test',('amazon.com',)) is None


def test_shared_mailbox_concurrent_accounts_keep_their_codes(tmp_path,monkeypatch):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        box=store.put('mailboxes',{'name':'Shared inbox','max_age_seconds':300})
        expected={account['email']:'723456',other['email']:'834567'}
        reads=[]
        def read(mailbox,recipient,since,used):
            token='uid-'+recipient
            reads.append((recipient,list(used)))
            return None if token in used else {'code':expected[recipient],'token':token}
        monkeypatch.setattr('retail.identity.read_code',read)
        identities=IdentityService(store)
        results=await asyncio.gather(*(identities.code({**a,'totp_secret':'','mailbox_id':box['id']}) for a in (account,other)))
        assert [r['code'] for r in results]==[expected[a['email']] for a in (account,other)]
        assert len(reads[1][1])==1
        with pytest.raises(ValueError,match='No fresh'):await identities.code({**account,'totp_secret':'','mailbox_id':box['id']})
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['redirect','closed','unrelated_error','cancel'])
def test_email_state_navigation_recovery_is_read_only(monkeypatch,mode):
    from types import SimpleNamespace
    from patchright.async_api import Error
    async def scenario():
        state={'otp':True,'captcha':False}
        error=Error('Execution context was destroyed' if mode!='unrelated_error' else 'Unrelated browser failure')
        page=SimpleNamespace(evaluate=AsyncMock(side_effect=[error,state]),is_closed=lambda:mode=='closed',wait_for_load_state=AsyncMock())
        monkeypatch.setattr(creation.challenges,'detect',AsyncMock(return_value=[]))
        if mode=='cancel':page.wait_for_load_state.side_effect=asyncio.CancelledError()
        if mode=='redirect':
            assert (await creation.signup_state(page))['otp']
            assert page.evaluate.await_count==2
            page.wait_for_load_state.assert_awaited_once()
        else:
            with pytest.raises(asyncio.CancelledError if mode=='cancel' else Error):await creation.signup_state(page)
            assert page.evaluate.await_count==1
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['valid','wrong_recipient','unrelated_sender','lookalike_domain','missing_hme','relay_failed','original_failed','different_original','reversed_auth'])
def test_icloud_hide_my_email_preserves_amazon_code_scope(mode):
    mail=EmailMessage()
    mail['From']='account-update_at_amazon_com_fixture_1234@icloud.com'
    mail['To']='signup@example.test'
    mail['X-ICLOUD-HME']='yes'
    relay='mx.example.test; dkim=pass header.i=@icloud.com; dkim=fail header.i=@amazon.com'
    original='relay.example.test; dkim=pass header.d=amazon.com'
    if mode=='unrelated_sender':mail.replace_header('From','unrelated@icloud.com')
    if mode=='lookalike_domain':mail.replace_header('From','account-update_at_amazon_com_fixture_1234@icloud.com.attacker.test')
    if mode=='missing_hme':del mail['X-ICLOUD-HME']
    if mode=='relay_failed':relay=relay.replace('dkim=pass','dkim=fail')
    if mode=='original_failed':original=original.replace('dkim=pass','dkim=fail')
    if mode=='different_original':original=original.replace('amazon.com','example.test')
    for result in ([original,relay] if mode=='reversed_auth' else [relay,original]):mail['Authentication-Results']=result
    mail.set_content('One Time Password (OTP): 723456')
    recipient='other@example.test' if mode=='wrong_recipient' else 'signup@example.test'
    assert extract_otp(mail.as_bytes(),recipient,('amazon.com',))==('723456' if mode=='valid' else None)


@pytest.mark.parametrize('mode',['relative','offsite','submit_override','base_offsite','empty_action'])
def test_shadowed_form_destination_keeps_region_checks(tmp_path,mode):
    async def scenario():
        store,engine,(account,_)=fixture(tmp_path)
        adapter=Amazon(store)
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            page=await browser.new_page()
            await page.route('**/*',lambda route:route.fulfill(body='<main></main>',content_type='text/html'))
            await page.goto('https://www.amazon.com/ap/cvf/request')
            action='https://example.test/collect' if mode=='offsite' else '' if mode=='empty_action' else 'verify'
            base='<base href="https://example.test/">' if mode in ('base_offsite','empty_action') else ''
            override='formaction="https://example.test/collect"' if mode=='submit_override' else ''
            await page.set_content(f'{base}<form action="{action}"><input type=hidden name=action value=verify><input id=cvf-input-code name=code><input type=submit aria-label="Verify OTP Button" {override}></form>')
            assert await page.locator('form').evaluate('f=>f.action instanceof Element')
            target=page.locator('input[type=submit]')
            if mode in ('relative','empty_action'):
                await creation.validate_target(page,target,account)
                await adapter.validate_signin_destination(target,account)
            else:
                with pytest.raises(creation.SignupDestinationError):await creation.validate_target(page,target,account)
                from retail.amazon import AuthenticationRequired
                with pytest.raises(AuthenticationRequired):await adapter.validate_signin_destination(target,account)
            await browser.close()
        await engine.close()
        store.db.close()
    asyncio.run(scenario())
