"""The email-claim interstitial must advance once, including late rendering."""
import asyncio
from unittest.mock import AsyncMock

from patchright.async_api import async_playwright
import pytest

import retail.account_creation as creation
from retail.amazon import Amazon
from retail.signup_controls import SignupControls
from test_account_care import fixture


@pytest.mark.parametrize('mode',['immediate','late','missing','cancel','ambiguous','offsite','already_clicked'])
def test_watcher_recovers_proceed_without_replaying_claim(tmp_path,monkeypatch,mode):
    async def scenario():
        store,engine,(account,other)=fixture(tmp_path)
        adapter=engine.amazon=Amazon(store)
        key='creation-'+account['id']
        store.put('account_creation',{'account_id':account['id'],'claim_submitted':True,
                  'new_account_submitted':mode=='already_clicked','submitted':False},key)
        updates=[]
        def update(status,message,**extra):
            updates.append((status,message))
            store.put('account_creation',{**store.get('account_creation',key),'status':status,'message':message,**extra},key)
        original_changed=creation.changed
        async def bounded_changed(page,before,timeout=15000):
            # Shorten only the expected missing-transition deadline in this fixture.
            return await original_changed(page,before,timeout=250 if mode=='missing' and timeout==15000 else timeout)
        monkeypatch.setattr(creation,'changed',bounded_changed)
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            context=await browser.new_context()
            context._retail_paced_input=True
            context._retail_input_timing_preset='aggressive'
            page=await context.new_page()
            clicked=asyncio.Event()
            await page.expose_function('proceeded',clicked.set)
            requests=[]
            async def route(route):
                requests.append(route.request.url)
                await route.fulfill(body='''<h1>Looks like you're new to Amazon</h1><div id=entry></div>
                    <script>window.clicks=0;window.submits=0;</script>''',content_type='text/html')
            await page.route('**/*',route)
            await page.goto('https://www.amazon.com/ax/claim')
            async def show_entry():
                await page.evaluate('''mode=>{
                    const target=mode==='offsite'?'https://example.test/collect':'/ap/register';
                    document.querySelector('#entry').innerHTML=`<form action="${target}"><button aria-labelledby="proceed-label"><span id=proceed-label>Proceed to create an account</span></button></form>`;
                    if(mode==='ambiguous')document.querySelector('form').insertAdjacentHTML('beforeend','<button>Proceed to create an account</button>');
                    document.querySelector('form').onsubmit=e=>{
                        e.preventDefault();window.clicks++;window.proceeded();
                        if(['missing','cancel'].includes(mode))return;
                        document.querySelector('#entry').innerHTML=`<form><input id=ap_customer_name name=customerName><input id=ap_email name=email><input id=ap_password type=password><input id=ap_password_check type=password><button id=continue>Create account</button></form>`;
                        document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submits++;document.querySelector('#entry').innerHTML='<input id=cvf-input-code>';};
                    };
                }''',mode,isolated_context=False)
            if mode!='late':await show_entry()
            controls=SignupControls(page,'www.amazon.com',update)
            adapter.logins[account['id']]=context
            job=asyncio.create_task(creation.verify_signup(engine,account,context,page,controls,update))
            adapter.login_watchers[account['id']]=job
            try:
                if mode=='late':
                    async with asyncio.timeout(3):
                        while store.get('account_creation',key).get('status')!='recovery_blocked':await asyncio.sleep(.01)
                    await show_entry()
                if mode=='cancel':
                    await asyncio.wait_for(clicked.wait(),5)
                    job.cancel()
                    with pytest.raises(asyncio.CancelledError):await job
                elif mode in ('missing','ambiguous','offsite'):
                    await asyncio.wait_for(job,8)
                elif mode in ('immediate','late'):
                    async with asyncio.timeout(15):
                        while not store.get('account_creation',key).get('submitted'):await asyncio.sleep(.01)
                    await page.locator('#cvf-input-code').wait_for(state='visible',timeout=5000)
                else:
                    async with asyncio.timeout(3):
                        while store.get('account_creation',key).get('status')!='recovery_blocked':await asyncio.sleep(.01)
                count=await page.evaluate('clicks',isolated_context=False)
                assert count==(0 if mode in ('ambiguous','offsite','already_clicked') else 1)
                assert await page.evaluate('submits',isolated_context=False)==(1 if mode in ('immediate','late') else 0)
                assert store.get('account_creation',key)['new_account_submitted']==(mode not in ('ambiguous','offsite'))
                if mode in ('immediate','late'):assert any(status=='recovering' for status,_ in updates)
                assert requests==['https://www.amazon.com/ax/claim']
                assert store.get('accounts',other['id'])==other
            finally:
                job.cancel()
                await asyncio.gather(job,return_exceptions=True)
                # Start a second watcher on an uncertain clicked screen: no repeated input.
                if mode in ('missing','cancel','already_clicked'):
                    again=asyncio.create_task(creation.verify_signup(engine,account,context,page,controls,update))
                    async with asyncio.timeout(3):
                        while store.get('account_creation',key).get('status')!='recovery_blocked':await asyncio.sleep(.01)
                    assert await page.evaluate('clicks',isolated_context=False)==count
                    again.cancel()
                    await asyncio.gather(again,return_exceptions=True)
                await context.close()
                adapter.logins.clear()
                await browser.close()
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_proceed_intent_blocks_a_new_signup_after_restart(tmp_path):
    async def scenario():
        store,engine,(account,_)=fixture(tmp_path)
        store.put('account_creation',{'account_id':account['id'],'new_account_submitted':True,
                  'status':'opening','message':'Proceed clicked'},'creation-'+account['id'])
        with pytest.raises(ValueError,match='already submitted'):
            await creation.create_account(engine,account)
        assert creation.snapshot(engine)[0]['status']=='review'
        assert not engine.amazon.contexts
        await engine.close()
        store.db.close()
    asyncio.run(scenario())
