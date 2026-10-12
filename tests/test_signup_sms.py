import asyncio
import json
import time
from urllib.parse import parse_qs
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from patchright.async_api import async_playwright

from retail.app import create_app
from retail import account_creation as creation
from retail.signup_controls import SignupControls, CLASSIFY
from retail.signup_sms import SMSPool, SignupSMS, SMSError
from retail.passkey_prompts import install
from retail.amazon import Amazon
from test_account_care import fixture


def provider():
    return {'id':'sms-fixture','name':'Fixture SMS','api_key':'FIXTURE_SECRET','max_price':.50,'timeout_seconds':30}


@pytest.mark.parametrize('mode',['success','uncertain','over_cap','wrong_country','cancel','expired','pending'])
def test_sms_orders_are_bounded_scoped_and_not_repeated(tmp_path,mode):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        requests=[]
        def reply(request):
            body=parse_qs(request.content.decode()) if request.method=='POST' else {}
            requests.append((request.url.path,body))
            if request.url.path=='/service/retrieve_all':return httpx.Response(200,json=[{'ID':7,'name':'Amazon / Amazon Web Services'},{'ID':8,'name':'Amazon AWS'}])
            assert body['key']==['FIXTURE_SECRET']
            if request.url.path=='/purchase/sms':
                assert body['max_price']==['0.5'] and body['quantity']==['1'] and body['service']==['7']
                if mode=='uncertain':raise httpx.ReadTimeout('SECRET upstream response')
                if mode=='cancel':raise asyncio.CancelledError()
                return httpx.Response(200,json={'success':1,'order_id':'OWNED_ORDER','number':'15551234567','cc':'44' if mode=='wrong_country' else '1','phonenumber':'5551234567','cost':'.60' if mode=='over_cap' else '.30','expires_in':300})
            assert body['orderid']==['OWNED_ORDER']
            if mode=='expired':return httpx.Response(200,json={'status':2})
            if mode=='pending' and sum(path=='/sms/check' for path,_ in requests)==1:return httpx.Response(200,json={'status':1})
            return httpx.Response(200,json={'status':3,'sms':'723456'})
        sms=SignupSMS(store,account,provider(),client=SMSPool(provider(),transport=httpx.MockTransport(reply)))
        if mode=='cancel':
            with pytest.raises(asyncio.CancelledError):await sms.number()
        elif mode in ('uncertain','over_cap','wrong_country'):
            with pytest.raises(SMSError):await sms.number()
        else:
            number=await sms.number()
            assert number['number']=='+15551234567'
            assert (await sms.number())['order_id']=='OWNED_ORDER'
            if mode=='expired':
                with pytest.raises(SMSError):await sms.code()
            else:
                assert await sms.code()=='723456'
                sms.submitted()
                with pytest.raises(SMSError):await sms.code()
        with pytest.raises(SMSError):await sms.number()
        assert sum(path=='/purchase/sms' for path,_ in requests)==1
        assert store.get('accounts',other['id'])==other
        assert '723456' not in json.dumps(store.all('signup_sms'))
        assert 'FIXTURE_SECRET' not in json.dumps(store.all('signup_sms'))
        await engine.close();store.db.close()
    asyncio.run(scenario())


def test_sms_connection_api_redacts_and_preserves_key(tmp_path,monkeypatch):
    with TestClient(create_app(tmp_path),headers={'X-Retail-Client':'dashboard'}) as client:
        created=client.post('/api/sms_providers',json={'name':'SMS fixture','api_key':'FIXTURE_SECRET','max_price':.5})
        assert created.status_code==200
        p=created.json();assert p['has_api_key'] and 'api_key' not in p
        assert client.put('/api/sms_providers/'+p['id'],json={'name':'Renamed','api_key':''}).status_code==200
        assert client.app.state.store.get('sms_providers',p['id'])['api_key']=='FIXTURE_SECRET'
        account=client.post('/api/accounts',json={'name':'Fixture','sms_provider_id':p['id']}).json()
        assert client.delete('/api/sms_providers/'+p['id']).status_code==409
        assert 'FIXTURE_SECRET' not in client.get('/api/state').text
        assert client.post('/api/accounts',json={'name':'Wrong','sms_provider_id':'missing'}).status_code==404
        assert client.post('/api/sms_providers',json={'name':'Wrong','api_key':'x','max_price':0}).status_code==422


@pytest.mark.parametrize('phone_type',['actual','renamed','tel_otp'])
def test_email_named_phone_is_never_an_email_field(phone_type):
    async def scenario():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True);page=await browser.new_page()
            await page.set_content('<form><label for=ap_phone_number>Mobile number</label><input id=ap_phone_number name=email type=tel autocomplete=tel></form>')
            if phone_type=='renamed':await page.locator('input').evaluate("e=>{e.id='contact';e.type='text';e.autocomplete='tel';}")
            if phone_type=='tel_otp':await page.locator('input').evaluate("e=>{e.id='cvf-input-code';e.name='code';e.autocomplete='one-time-code';}")
            e=page.locator('input')
            assert not await e.evaluate('(e)=>('+CLASSIFY+')(e,"email")')
            assert await e.evaluate('(e)=>('+CLASSIFY+')(e,"phone")') == (phone_type!='tel_otp')
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['sms','saved_phone','missing_phone'])
def test_phone_first_signup_then_sms_and_password(tmp_path,mode):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        account={**account,'name':'Fixture Name','password':'fixture-secret','phone':'+15551234567' if mode=='saved_phone' else ''}
        adapter=engine.amazon=Amazon(store);adapter.watch_login=AsyncMock()
        key='creation-'+account['id'];store.put('account_creation',{'account_id':account['id']},key)
        def update(status,message,**extra):store.put('account_creation',{**store.get('account_creation',key),'status':status,'message':message,**extra},key)
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True);context=await browser.new_context()
            context._retail_paced_input=True;context._retail_input_timing_preset='aggressive'
            sms=type('Sms',(),{})()
            sms.number=AsyncMock(return_value={'number':'+15551234567','national':'5551234567','country':'US'})
            sms.code=AsyncMock(return_value='723456');sms.submitted=lambda:None;sms.record=lambda:{'number':'+15551234567'}
            if mode=='sms':context._retail_signup_sms=sms
            page=await context.new_page()
            await page.route('**/*',lambda route:route.fulfill(body='<main></main>',content_type='text/html'))
            await page.goto('https://www.amazon.com/ap/register')
            await page.set_content('''<form action="/ap/register"><input type=hidden name=action value=register>
                <select id=auth-country-picker name=countryCode><option value=GB>GB +44</option><option value=US>US +1</option></select>
                <label for=ap_phone_number>Mobile number</label><input id=ap_phone_number name=email type=tel autocomplete=tel>
                <label for=ap_customer_name>Your name</label><input id=ap_customer_name name=customerName autocomplete=name>
                <input id=continue type=submit value=Continue></form>
                <script>window.sent=[];document.addEventListener('submit',e=>{e.preventDefault();const f=e.target;
                if(f.querySelector('#ap_phone_number')){window.sent.push({phone:f.querySelector('#ap_phone_number').value,name:f.querySelector('#ap_customer_name').value,country:f.querySelector('select').value});document.body.innerHTML='<h1>Verify mobile number</h1><form><input type=hidden name=verificationPageContactType value=Phone><input id=cvf-input-code name=code autocomplete=one-time-code><button id=auth-signin-button>Verify</button></form>';}
                else if(f.querySelector('#cvf-input-code')){window.sent.push({code:f.querySelector('#cvf-input-code').value});document.body.innerHTML='<form><input id=ap_password name=password type=password><input id=ap_password_check name=passwordCheck type=password><button id=continue>Continue</button></form>';}
                else{window.sent.push({password:f.querySelector('#ap_password').value,confirmation:f.querySelector('#ap_password_check').value});document.body.innerHTML='<div id=nav-link-accountList><span class=nav-line-1>Hello, Fixture</span></div>';}});</script>''')
            controls=SignupControls(page,'www.amazon.com',update)
            if mode=='missing_phone':
                with pytest.raises(SMSError):await creation.complete_signup_form(adapter,account,page,controls,update)
                assert await page.locator('#ap_phone_number').input_value()==''
                sms.number.assert_not_awaited()
            else:
                await creation.complete_signup_form(adapter,account,page,controls,update)
                if mode=='saved_phone':
                    sms.number.assert_not_awaited()
                    assert (await creation.signup_state(page))['otp_channel']=='phone'
                else:
                    adapter.logins[account['id']]=context
                    await asyncio.wait_for(creation.verify_signup(engine,account,context,page,controls,update),15)
                    sent=await page.evaluate('window.sent',isolated_context=False)
                    assert sent==[{'phone':'5551234567','name':'Fixture Name','country':'US'},{'code':'723456'},{'password':'fixture-secret','confirmation':'fixture-secret'}]
                    assert store.get('account_creation',key)['sms_otp_submitted']
                    sms.number.assert_awaited_once();sms.code.assert_awaited_once();adapter.watch_login.assert_awaited_once()
                    assert not store.get('account_creation',key).get('otp_submitted')
            assert store.get('accounts',other['id'])==other
            await context.close();adapter.logins.clear();await browser.close()
        await engine.close();store.db.close()
    asyncio.run(scenario())


def test_passkey_prompts_declined_across_navigations_and_frames():
    async def scenario():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True);context=await browser.new_context()
            await install(context);page=await context.new_page()
            await page.route('**/*',lambda route:route.fulfill(body='<iframe src="/frame"></iframe>' if route.request.url.endswith('/first') else '<main>Fixture</main>',content_type='text/html'))
            for path in ('first','second'):
                await page.goto('https://www.amazon.com/'+path)
                for frame in page.frames:
                    result=await frame.evaluate('''async()=>({available:await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable(),create:await navigator.credentials.create({publicKey:{}}).then(()=> 'unexpected',e=>e.name),get:await navigator.credentials.get({publicKey:{}}).then(()=> 'unexpected',e=>e.name)})''',isolated_context=False)
                    assert result=={'available':False,'create':'NotAllowedError','get':'NotAllowedError'}
            await browser.close()
    asyncio.run(scenario())
