"""Amazon cart layouts and autonomous reauthentication on local fixtures only."""
import asyncio
import json
from urllib.parse import urlparse

import pytest
from patchright.async_api import async_playwright

from retail.amazon import Amazon, Attention, AuthenticationRequired, ChallengeDetected
from retail.store import Store
from scripts.cart_fixture import CartFixture, TARGET, OTHER


@pytest.mark.parametrize('identification',['supplied_html','name','data_action'])
def test_delete_active_removes_all_unrelated_items_and_keeps_target(identification):
    async def scenario():
        # Preserve the supplied Amazon attributes, including HTML-encoded text.
        control='''<input name="submit.delete-active.88fca6ff-9075-4287-a4b3-c1075b9d5591" value="Delete" data-action="delete-active" aria-label="Delete Rizos Curls Multivitamin Leave-In Heat Protection Up to 450&amp;deg;F &amp;ndash; Strengthen, Repair &amp;amp; Add Shine to Straight, Wavy, Curly, Coily Hair Types 1a&amp;ndash;4c &amp;ndash; Sulfate &amp;amp; Paraben Free" type="submit" class="a-color-link" data-feature-id="item-delete-button">'''
        unrelated=[OTHER,'B000000002','B000000003']
        rows=''.join(f'<div data-asin="{asin}" data-quantity="{2 if asin==TARGET else 1}">{control}{control.replace("<input ","<input hidden ",1)}</div>' for asin in [OTHER,TARGET,*unrelated[1:]])
        body=f'<form><div id="sc-active-cart">{rows}</div><div id="sc-saved-cart"><div data-asin="B000000004" data-quantity="1">{control}</div></div></form>'
        body+='''<script>window.deleted=[];
            document.addEventListener('submit',e=>{e.preventDefault();
                const row=e.submitter.closest('[data-asin]');deleted.push(row.dataset.asin);
                row.remove();
                // Amazon rerenders remaining rows after cart changes.
                const active=document.querySelector('#sc-active-cart');active.innerHTML=active.innerHTML;
            });</script>'''
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            page=await browser.new_page()
            await page.route('**/*',lambda r:r.fulfill(body=body,content_type='text/html'))
            await page.goto('https://www.amazon.com/gp/cart/view.html')
            if identification!='supplied_html':
                await page.locator('input').evaluate_all('''(controls,mode)=>controls.forEach(e=>{
                    e.value='Remove';e.setAttribute('aria-label','Remove item');
                    e.removeAttribute(mode==='name'?'data-action':'name');
                })''',identification)
            adapter=Amazon(None)
            assert await adapter.cart(page,2,TARGET)==2
            assert await adapter.get_cart(page)==[{'asin':TARGET,'quantity':2}]
            assert await page.evaluate('deleted',isolated_context=False)==unrelated
            assert await page.locator('#sc-saved-cart [data-asin]').count()==1
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('layout',['hidden_copy','labelled_icon','nested_role','delayed','disabled_copy'])
def test_delete_uses_single_actionable_control(layout):
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            page=await browser.new_page()
            fixture=CartFixture(unrelated=1)
            await page.route('**/*',fixture.route)
            adapter=Amazon(None)
            navigate=adapter.navigate
            async def adjust(page,url,**kwargs):
                result=await navigate(page,url,**kwargs)
                if '/gp/cart/' in url:
                    await page.evaluate('''layout=>{
                        const e=document.querySelector("button[name^='submit.delete']");
                        if(layout==='hidden_copy'||layout==='disabled_copy') {
                            const copy=e.cloneNode(true);
                            if(layout==='hidden_copy')copy.hidden=true; else copy.disabled=true;
                            e.before(copy);
                        } else if(layout==='labelled_icon') {
                            e.removeAttribute('name'); e.textContent='';
                            const label=document.createElement('span');label.id='delete-description';label.hidden=true;label.textContent='Delete Fixture item';
                            e.before(label);e.setAttribute('aria-labelledby',label.id);e.innerHTML='<svg width="24" height="24"><path d="M0 0L20 20"/></svg>';
                        } else if(layout==='nested_role') {
                            e.setAttribute('onclick',e.getAttribute('onclick').replace('this.parentElement.remove()',"this.closest('[data-asin]').remove()"));
                            const wrapper=document.createElement('span');wrapper.setAttribute('role','button');wrapper.setAttribute('aria-label','Delete');
                            e.before(wrapper);wrapper.append(e);
                        } else {e.hidden=true;setTimeout(()=>e.hidden=false,150);}
                    }''',layout,isolated_context=False)
                return result
            adapter.navigate=adjust
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            assert await adapter.cart(page,1,TARGET)==1
            assert fixture.items=={TARGET:1} and fixture.events==['add','delete']
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('present',[True,False])
def test_reauthentication_reconciles_without_replaying_add(present):
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            page=await browser.new_page();page.set_default_timeout(300)
            fixture=CartFixture(quantity=1 if present else 0,unrelated=1)
            await page.route('**/*',fixture.route)
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            page._retail_reconcile_cart=True
            if present:
                assert await Amazon(None).cart(page,1,TARGET)==1
                assert fixture.events==['delete'] and fixture.items=={TARGET:1}
            else:
                with pytest.raises(Attention): await Amazon(None).cart(page,1,TARGET)
                assert fixture.events==[] and fixture.items=={OTHER:1}
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('layout',['missing_link','account_link','foreign_link','combined','delayed','email_login','rejected','captcha'])
def test_product_signed_out_automatically_signs_in_with_trusted_input(tmp_path,layout):
    async def scenario():
        store=Store(tmp_path)
        account=store.put('accounts',{'name':'Fixture','region':'US','email':'a@b.co','password':'Ab1!'})
        adapter=Amazon(store)
        signed_in=False
        requests=[]; events=[]; submissions=[]
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            page=await browser.new_page()
            await page.expose_function('recordInput',lambda kind,trusted:events.append((kind,trusted)))
            async def route(r):
                nonlocal signed_in
                path=urlparse(r.request.url).path
                requests.append(path)
                assert urlparse(r.request.url).hostname=='www.amazon.com'
                script="<script>for(const type of ['keydown','mousemove','mousedown'])document.addEventListener(type,e=>recordInput(type,e.isTrusted));</script>"
                if path.startswith('/dp/'):
                    href={'account_link':'/gp/css/homepage.html','foreign_link':'https://foreign.invalid/ap/signin'}.get(layout,'')
                    body=f'<a id="nav-link-accountList" href="{href}"><span class="nav-line-1">{ "Hello Fixture" if signed_in else "Hello, sign in"}</span></a><span id="productTitle">Fixture</span><button>Add to cart</button>'
                elif path=='/gp/your-account/order-history':
                    email=f'<input id="{"ap_email_login" if layout=="email_login" else "ap_email"}" type="email" name="email">'
                    password='<input id="ap_password" type="password" name="password">'
                    if layout=='combined':
                        body=f'<form action="/fixture/signin" method="post">{email}{password}<button id="signInSubmit">Sign in</button></form>'
                    else:
                        body=f'<form action="/fixture/password" method="post">{email}<button id="continue">Continue</button></form>'
                    if layout=='delayed':
                        body=f'<script>setTimeout(()=>document.body.insertAdjacentHTML("beforeend",{json.dumps(body)}),150)</script>'
                elif path=='/fixture/password':
                    assert r.request.post_data=='email=a%40b.co'
                    # Combined retained email is a common password-step variant.
                    body='<form action="/fixture/signin" method="post"><input id="ap_email" name="email" value="a@b.co"><input id="ap_password" type="password" name="password"><button id="signInSubmit">Sign in</button></form>'
                elif path=='/fixture/signin':
                    submissions.append(r.request.post_data)
                    if layout=='rejected':body='<div id="auth-error-message-box">Incorrect password</div><input id="ap_password">'
                    elif layout=='captcha':body='<p>Robot check</p><input id="captchacharacters">'
                    else:
                        signed_in=True
                        body='<div id="nav-link-accountList"><span class="nav-line-1">Hello Fixture</span></div>'
                else:body=''
                await r.fulfill(body=body+script,content_type='text/html')
            await page.route('**/*',route)
            page._retail_start_url=f'https://www.amazon.com/dp/{TARGET}'
            try:
                if layout in ('rejected','captcha'):
                    with pytest.raises(AuthenticationRequired if layout=='rejected' else ChallengeDetected):
                        await adapter.ensure_session(page.context,account,page)
                    assert not store.get('accounts',account['id']).get('logged_in')
                else:
                    await adapter.ensure_session(page.context,account,page)
                    assert store.get('accounts',account['id'])['logged_in']
                    assert page.url==page._retail_start_url
                assert len(submissions)==1 and 'password=Ab1%21' in submissions[0]
                assert all(trusted for _,trusted in events)
                assert sum(kind=='keydown' for kind,_ in events)>=10
                assert any(kind=='mousemove' for kind,_ in events)
                assert requests.count('/gp/your-account/order-history')==1
                assert not getattr(page.context,'_retail_paced_input',False)
            finally: await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_foreign_signin_form_never_receives_credentials(tmp_path):
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            page=await browser.new_page()
            await page.route('**/*',lambda r:r.fulfill(body='<form action="https://foreign.invalid/"><input id="ap_email"><button id="continue">Continue</button></form>',content_type='text/html'))
            await page.goto('https://www.amazon.com/ap/signin')
            with pytest.raises(AuthenticationRequired,match='leaves'):
                await Amazon(None).authenticate(page,{'region':'US','email':'fixture','password':'secret'})
            assert await page.locator('#ap_email').input_value()==''
            await browser.close()
    asyncio.run(scenario())


def test_account_offer_detects_lost_session_but_anonymous_stock_still_works():
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            page=await browser.new_page()
            await page.route('**/*',lambda r:r.fulfill(body='''<div id="nav-link-accountList"><span class="nav-line-1">Hello, sign in</span></div>
                <span id="productTitle">Fixture</span><div id="availability">In Stock</div>
                <div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$10.00</span></span></div>
                <button id="add-to-cart-button">Add to cart</button>''',content_type='text/html'))
            adapter=Amazon(None)
            with pytest.raises(AuthenticationRequired):
                await adapter.inspect(page,{'asin':TARGET},'US')
            stock=await adapter.inspect_stock(page,{'asin':TARGET},'US')
            assert stock['availability_status']=='available'
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('multiple_products',[False,True])
def test_task_recovers_authentication_without_resume_and_reconciles_cart(tmp_path,multiple_products):
    from test_monitors import fixture, task_record, until
    async def scenario():
        store,group,account,engine=fixture(tmp_path)
        engine.amazon.stock.add(TARGET)
        if multiple_products: engine.amazon.stock.add('B087654321')
        calls=[]
        async def cart(page,quantity,asin):
            assert asin==TARGET
            calls.append(getattr(page,'_retail_reconcile_cart',False))
            if len(calls)==1: raise AuthenticationRequired('Fixture sign-in redirect after add')
            return quantity
        engine.amazon.cart=cart
        task=task_record(store,group,account,use_account_proxy=True,monitor_asin='' if multiple_products else TARGET)
        try:
            await engine.start(task['id'])
            await until(lambda:task['id'] not in engine.jobs)
            assert store.get('tasks',task['id'])['status']=='completed'
            assert calls==[False,True]
            assert engine.amazon.authentications==[account['id'],account['id']]
        finally:
            await engine.close();store.db.close()
    asyncio.run(scenario())
