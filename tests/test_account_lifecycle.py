import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

from retail.account_lifecycle import AccountState as S, classify, observe, record, signup_handoff, authorized_signup
from retail.store import Store


@pytest.mark.parametrize('signals,expected', [({},S.UNKNOWN),({'signed':True},S.AUTHENTICATED),
    ({'signin':True},S.SIGNIN),({'signup':True},S.SIGNUP),({'absent':True},S.SIGNUP_ELIGIBLE),
    ({'exists':True},S.SIGNIN),({'exists':True,'absent':True},S.RECOVERABLE),
    ({'invalid':True},S.BLOCKED),({'verification':True,'signed':True},S.VERIFICATION),
    ({'recovery':True},S.RECOVERY),({'cooldown':True},S.BLOCKED)])
def test_explicit_states(signals,expected):assert classify(signals).state==expected


def test_buttons_do_not_establish_account_existence_and_handoff_stays_owned(tmp_path):
    async def scenario():
        store=Store(tmp_path)
        account={'id':'first','region':'US','email':'fixture@example.com','password':'LOCAL_SECRET'}
        store.put('accounts',account)
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True);context=await browser.new_context()
            page=await context.new_page();fixture='<button>Create account</button><button>Sign in</button>'
            async def route(r):await r.fulfill(body=fixture,content_type='text/html')
            await page.route('**/*',route);await page.goto('https://www.amazon.com/ap/register')
            assert (await observe(page,account)).state==S.UNKNOWN
            fixture='<div id="auth-error-message-box">This email address is already in use by an account.</div>'
            await page.reload()
            async def navigate(page,url,**kw):await page.goto(url)
            adapter=SimpleNamespace(store=store,navigate=navigate,authenticate=AsyncMock())
            updates=[]
            assert await signup_handoff(adapter,account,page,lambda *a,**kw:updates.append((a,kw)))
            assert await signup_handoff(adapter,account,page,lambda *a,**kw:updates.append((a,kw)))
            adapter.authenticate.assert_awaited_once_with(page,account)
            assert page.context is context and len(browser.contexts)==1
            assert len(store.all('accounts'))==1
            assert 'LOCAL_SECRET' not in str(store.all('account_lifecycle'))
            # An ordinary sign-in cannot silently become account creation.
            assert await authorized_signup(adapter,account,page) is False
            other=await browser.new_context();other_page=await other.new_page()
            await other_page.route('**/*',lambda r:r.fulfill(body='<div id="auth-error-message-box">We cannot find an account with that email.</div>'))
            await other_page.goto('https://www.amazon.com/ap/signin')
            assert (await observe(other_page,{'id':'second','region':'US'})).state==S.SIGNUP_ELIGIBLE
            assert not getattr(other,'_retail_signup_signin_attempted',False)
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_only_live_session_verification_establishes_success(tmp_path):
    store=Store(tmp_path)
    record(store,'account',classify({'signed':True}))
    assert store.get('account_lifecycle','lifecycle-account')['state']==S.AUTHENTICATED
    record(store,'account',classify({},session_verified=True))
    assert store.get('account_lifecycle','lifecycle-account')['state']==S.SUCCESS
    # A later expired session supersedes old success; no saved flag authorizes a purchase.
    record(store,'account',classify({'signin':True}))
    assert store.get('account_lifecycle','lifecycle-account')['state']==S.SIGNIN
    assert len(store.get('account_lifecycle','lifecycle-account')['history'])==3
    store.db.close()
