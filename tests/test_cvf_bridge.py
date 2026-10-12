import asyncio
from urllib.parse import urlsplit,parse_qs
import pytest
from patchright.async_api import async_playwright
from retail.signup_challenges import prepare_token,apply_token
from retail.account_creation import validate_target
from test_signup_cvf_captcha import MARKUP,FRAME
from cvf_bridge_fixture import BRIDGE_SCRIPT,WRAPPER_BRIDGE


@pytest.mark.parametrize('mode',['success','level_2','level_3','level_12','delayed','rejected','session_changed','action_changed','wrong_family','bridge_missing','foreign_endpoint','duplicate'])
def test_arkose_answer_must_pass_amazon_exchange_before_cvf(mode):
    async def scenario():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True);page=await browser.new_page()
            answers=[];submits=[];received=asyncio.Event();done=asyncio.Event();release=asyncio.Event()
            async def route(r):
                path=urlsplit(r.request.url).path
                if path=='/fixture-wrapper':body=f'<iframe id=aacb-arkose-frame src="{FRAME}"></iframe>'+WRAPPER_BRIDGE
                elif r.request.url==FRAME:body='<h2>Fixture Arkose</h2>'
                elif path.startswith('/aaut/verify/'):
                    answers.append(r.request.post_data_json);received.set()
                    if mode=='delayed':await release.wait()
                    await r.fulfill(json={'actionType':'ARKOSE_LEVEL_1' if mode=='rejected' else 'PASS','sessionToken':'retailer-only-token'})
                    return
                elif path=='/ap/cvf/verify':
                    submits.append(parse_qs(r.request.post_data));done.set()
                    body='<h1>Verify email</h1><input id=cvf-input-code autocomplete=one-time-code>'
                else:
                    body=MARKUP.split('<script>')[0]+BRIDGE_SCRIPT+'''<script>
                    window.fixtureAmazonVerify=async payload=>{
                     const response=await fetch('/aaut/verify/cvf/fixture-session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
                     return response.json();
                    };</script>'''
                await r.fulfill(body=body,content_type='text/html')
            await page.route('**/*',route);await page.goto('https://www.amazon.com/ap/cvf/request')
            account={'region':'US'};solver={'provider':'2captcha'}
            if mode.startswith('level_'):
                await page.evaluate('(action)=>acic.currentAAmationResult.actionTypeValue=action','ARKOSE_'+mode.upper(),isolated_context=False)
            if mode in ('bridge_missing','foreign_endpoint','wrong_family'):
                await page.evaluate("()=>{if(MODE==='bridge_missing')delete window.acic;else if(MODE==='wrong_family')acic.currentAAmationResult.actionTypeValue='PASS';else window.acic.serviceEndpoint='https://foreign.test/aaut/verify/'}".replace('MODE',repr(mode)),isolated_context=False)
                with pytest.raises(ValueError,match='bridge'):await prepare_token(page,'arkose',account,solver,validate_target)
                assert not answers and not submits
            else:
                prepared=await prepare_token(page,'arkose',account,solver,validate_target)
                if mode in ('session_changed','action_changed'):
                    await page.evaluate("acic.currentAAmationResult.sessionTokenValue='changed'" if mode=='session_changed' else "acic.currentAAmationResult.actionTypeValue='ARKOSE_LEVEL_3'",isolated_context=False)
                    with pytest.raises(ValueError,match='session changed'):await apply_token(page,'arkose',prepared,'provider-only-token',account,validate_target)
                    assert not answers and not submits
                else:
                    await apply_token(page,'arkose',prepared,'provider-only-token',account,validate_target)
                    await asyncio.wait_for(received.wait(),3)
                    if mode=='delayed':
                        assert not submits
                        assert await page.locator('#cvf_aamation_response_token').input_value()==''
                        release.set()
                    if mode=='duplicate':
                        with pytest.raises(ValueError,match='already dispatched'):await apply_token(page,'arkose',prepared,'provider-only-token',account,validate_target)
                    if mode!='rejected':
                        await asyncio.wait_for(done.wait(),3)
                        assert len(submits)==1 and submits[0]['cvf_aamation_response_token']==['retailer-only-token']
                        assert submits[0]['anti-csrftoken-a2z']==['fixture-csrf']
                    else:
                        await page.wait_for_function("()=>fixtureAnswers.length===1")
                        assert not submits and await page.locator('#cvf_aamation_response_token').input_value()==''
                    assert answers==[{'sessionToken':'provider-only-token'}]
            await browser.close()
    asyncio.run(scenario())
