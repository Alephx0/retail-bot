import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from patchright.async_api import async_playwright
from retail.account_creation import complete_signup_form,signup_state
from retail.signup_controls import SignupControls
from retail.amazon import Amazon
from test_account_care import fixture


def test_cvf_add_mobile_after_captcha_uses_assigned_sms_once(tmp_path):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path);adapter=engine.amazon=Amazon(store)
        account={**account,'phone':''};key='creation-'+account['id']
        store.put('account_creation',{'account_id':account['id'],'submitted':True,'otp_submitted':True,'captcha_submitted':True},key)
        def update(status,message,**extra):store.put('account_creation',{**store.get('account_creation',key),'status':status,'message':message,**extra},key)
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True);page=await browser.new_page()
            context=page.context;context._retail_input_timing_preset='aggressive'
            sms=context._retail_signup_sms=SimpleNamespace(number=AsyncMock(return_value={'number':'+15551234567','national':'5551234567','country':'US'}))
            await page.route('**/*',lambda r:r.fulfill(body='''<form method=post action=/ap/cvf/verify>
            <select id=cvf_phone_cc_native name=cvf_phone_cc><option value=GB>GB +44</option><option value=US>US +1</option></select>
            <input id=cvfPhoneNumber name=cvf_phone_num type=tel>
            <input name=cvf_action type=submit value="Add mobile number"></form>
            <script>window.submits=[];document.querySelector('form').onsubmit=e=>{e.preventDefault();submits.push(Object.fromEntries(new FormData(e.target)));document.body.innerHTML='<h1>Verify mobile number</h1><form><input id=cvf-input-code name=code autocomplete=one-time-code></form>'}</script>''',content_type='text/html'))
            await page.goto('https://www.amazon.com/ap/cvf/verify')
            controls=SignupControls(page,'www.amazon.com',update)
            await complete_signup_form(adapter,account,page,controls,update,progress=store.get('account_creation',key))
            assert await page.evaluate('submits',isolated_context=False)==[{'cvf_phone_cc':'US','cvf_phone_num':'5551234567'}]
            assert (await signup_state(page))['otp_channel']=='phone'
            record=store.get('account_creation',key)
            assert record['phone_submitted'] and record['last_contact_kind']=='phone' and record['otp_submitted']
            await complete_signup_form(adapter,account,page,controls,update,progress=record)
            sms.number.assert_awaited_once()
            assert store.get('accounts',other['id'])==other
            await browser.close()
        await engine.close();store.db.close()
    asyncio.run(scenario())
