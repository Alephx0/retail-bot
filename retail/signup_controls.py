"""Account-owned, local MCP/CDP control recovery; no source or model execution."""
import asyncio
import json
from datetime import timedelta
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session


CLASSIFY = r'''function classifySignup(e,kind) {
    if(!e.getClientRects().length||getComputedStyle(e).visibility!=='visible'||e.disabled)return false;
    const labelled=(e.getAttribute('aria-labelledby')||'').split(/\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' ');
    const label=(labelled.trim()||e.getAttribute('aria-label')||(e.labels?[...e.labels].map(l=>l.innerText).join(' '):'')||e.innerText||(e.type==='submit'?e.value:'')||'').replace(/\s+/g,' ').trim();
    const name=(e.name||'').toLowerCase(),type=(e.type||'').toLowerCase();
    if(kind==='country')return e.matches('select')&&(e.id==='auth-country-picker'||name==='countrycode'||e.id==='cvf_phone_cc_native'&&name==='cvf_phone_cc');
    if(kind==='passkey_skip')return e.matches('a,button,input[type=submit]')&&/^(?:not now|skip|maybe later)$/i.test(label)&&[...document.querySelectorAll('h1,h2,h3,[role=heading]')].some(h=>h.getClientRects().length&&/passkey/i.test(h.innerText));
    if(kind==='menu')return e.matches('a,button,[role=button]')&&/^(hello,? sign in\s*)?account\s*(?:&|and)\s*lists$|^expand account and lists$/i.test(label);
    if(kind==='signup')return e.matches('a[href]')&&new URL(e.href).origin===location.origin&&new URL(e.href).pathname==='/ap/register'&&/start here|create (?:your |an? )?account/i.test(label);
    if(kind==='new_account')return e.matches('a,button,input[type=submit]')&&/^(?:proceed to )?create (?:your |an? )?(?:amazon )?account\.?$/i.test(label);
    if(kind==='submit'&&e.matches('input[type=submit]')&&name==='cvf_action'&&/^add mobile number$/i.test(label)&&e.form?.querySelector('input#cvfPhoneNumber[type=tel]'))return true;
    if(kind==='submit'||kind==='otp_submit')return e.matches('button,input[type=submit]')&&/^(?:continue|verify(?: (?:email(?: address)?|code|otp))?(?: button)?|create (?:your |an? )?(?:amazon )?account|verify and create(?: account)?)\.?$/i.test(label)&&!!e.form&&(kind!=='otp_submit'||[...e.form.querySelectorAll('input')].some(field=>classifySignup(field,'otp')));
    if(!e.matches('input')||['hidden','submit','button'].includes(type)||!e.form)return false;
    if(kind==='name')return type!=='password'&&(e.id==='ap_customer_name'||e.autocomplete==='name'||/^(customername|fullname|name)$/.test(name)||/^your name$/i.test(label));
    const phone=type==='tel'||e.autocomplete.startsWith('tel')||/^(ap_phone_number|auth-pv-enter-phone-number)$/.test(e.id)||/^(?:enter (?:your |a )?)?(?:mobile|phone)(?: number)?\s*:?$/i.test(label);
    if(kind==='phone')return type!=='password'&&phone&&!classifySignup(e,'otp');
    if(kind==='email')return type!=='password'&&!phone&&(/^(ap_email|ap_email_login)$/.test(e.id)||name==='email'||e.autocomplete==='email'||/^(enter )?(mobile number or )?email(?: address)?$/i.test(label));
    if(kind==='confirmation')return type==='password'&&/check|confirm|re.?enter|again/i.test(name+' '+label);
    if(kind==='password')return type==='password'&&!/check|confirm|re.?enter|again/i.test(name+' '+label);
    if(kind==='otp'){
        const identity=(name+' '+(e.id||'')).toLowerCase();
        if(!['text','tel','number','search'].includes(type)||/cvv|cvc|card/.test(identity)||e.autocomplete.startsWith('cc-'))return false;
        return e.autocomplete==='one-time-code'||/^(code|otpcode|otp|verificationcode|securitycode)$/.test(name.replace(/[-_]/g,''))||/^(cvf-input-code|auth-mfa-otpcode|cvf-otp-code|auth-email-code)$/.test(e.id)||/^(?:enter (?:the |your )?)?(?:otp|one[ -]time (?:password|code)|verification code|security code)\s*:?$/i.test(label);
    }
    return false;
}'''
SELECTORS = {
    'menu': '#nav-link-accountList:visible',
    'signup': '#nav-flyout-ya-newCust a[href*="/ap/register"]:visible',
    'new_account': '#createAccountSubmit:visible',
    'name': '#ap_customer_name:visible', 'email': '#ap_email:visible, #ap_email_login:visible',
    'password': '#ap_password:visible', 'confirmation': '#ap_password_check:visible',
    'submit': '#continue:visible, #auth-create-account-btn:visible, #cvf-submit-otp-button input:visible, #auth-signin-button:visible, form:has(input#cvfPhoneNumber[type=tel]) input[name=cvf_action][type=submit]:visible',
    'otp': '#cvf-input-code:visible, #auth-mfa-otpcode:visible',
    'phone': '#ap_phone_number:visible, #auth-pv-enter-phone-number:visible, #cvfPhoneNumber:visible',
    'country': '#auth-country-picker:visible, #cvf_phone_cc_native:visible',
    'passkey_skip': '#passkey-skip:visible',
    'otp_submit': '#cvf-submit-otp-button input:visible, #auth-signin-button:visible, #continue:visible',
}


class SignupControls:
    def __init__(self, page, domain, update):
        self.page, self.context, self.domain, self.update = page, page.context, domain, update
        self.recovered = None
        self.server = FastMCP('retail-signup-recovery')
        self.server.tool()(self.recover_control)

    def guard(self):
        target = urlparse(self.page.url)
        if self.page.is_closed() or self.page.context is not self.context or target.scheme!='https' or target.netloc!=self.domain:
            raise ValueError('Signup left its account browser or retailer region.')

    async def recover_control(self, kind: str) -> dict:
        """Recover one recognized signup control using current CDP evidence."""
        if kind not in SELECTORS:
            raise ValueError('Unsupported signup control')
        self.guard()
        cdp = await self.context.new_cdp_session(self.page)
        try:
            expression = 'JSON.stringify([...document.querySelectorAll("a,button,input,select,[role=button]")].map((e,index)=>({e,index})).filter(({e})=>('+CLASSIFY+')(e,'+json.dumps(kind)+')).map(({index})=>index))'
            result = await cdp.send('Runtime.evaluate', {'expression':expression,'returnByValue':True})
            indexes = json.loads(result['result']['value'])
            self.guard()
            if len(indexes)!=1:
                return {'recovered':False}
            target = await self.page.locator('a,button,input,select,[role=button]').nth(indexes[0]).element_handle()
            if target and await target.evaluate('(e,kind)=>('+CLASSIFY+')(e,kind)', kind):
                self.recovered = target
                return {'recovered':True}
            return {'recovered':False}
        finally:
            await cdp.detach()

    async def inspect_loading(self):
        """Inspect a stalled verification document once without puzzle input."""
        self.guard()
        document=await self.page.evaluate('()=>performance.timeOrigin')
        if getattr(self,'_loading_document',None)==document:
            return 'already_inspected'
        self._loading_document=document
        if not getattr(self.context,'_retail_recovery_endpoint',''):
            return 'connection_unavailable'
        from .devtools_recovery import ScopedMCP
        try:
            async with asyncio.timeout(15), ScopedMCP(self.page) as scope:
                # Snapshot stays local; no challenge contents or account text
                # are exported. A model cannot create a missing provider frame.
                await scope.controls(r'(?:Proceed to )?[Cc]reate (?:an? |your )?account')
            return 'inspected'
        except Exception:
            return 'inspection_interrupted'

    async def find(self, kind, *, optional=False):
        self.guard()
        locator = self.page.locator(SELECTORS[kind])
        if await locator.count()==1:
            target = await locator.element_handle()
            if kind not in ('name','email','phone','password','confirmation','otp','otp_submit','country','passkey_skip') or await target.evaluate('(e,kind)=>('+CLASSIFY+')(e,kind)',kind):
                return target
        self.recovered = None
        async with asyncio.timeout(5):
            async with create_connected_server_and_client_session(self.server, read_timeout_seconds=timedelta(seconds=4)) as session:
                await session.call_tool('recover_control', {'kind':kind})
        target, self.recovered = self.recovered, None
        if target:
            # Production browsers use the pinned Chrome DevTools MCP to verify
            # recovered action controls against their accessibility snapshot.
            # Input values stay local; MCP never receives saved credentials.
            if kind in ('menu','signup','new_account','submit','otp_submit','passkey_skip') and getattr(self.context,'_retail_recovery_endpoint',''):
                import re
                from .devtools_recovery import ScopedMCP
                label=await target.evaluate(r"""e=>{
                    const labelled=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
                        .map(id=>document.getElementById(id)?.innerText||'').join(' ').trim();
                    return (labelled||e.getAttribute('aria-label')||e.innerText||e.value||'').replace(/\s+/g,' ').trim();
                }""")
                async with asyncio.timeout(15), ScopedMCP(self.page) as scope:
                    candidates=await scope.controls(re.escape(label))
                    if len(candidates)!=1:
                        raise ValueError('The recovered signup action is missing or ambiguous in Chrome DevTools.')
                self.guard()
                if not await target.evaluate('(e,kind)=>e.isConnected&&('+CLASSIFY+')(e,kind)',kind):
                    raise ValueError('The recovered signup action changed before input.')
            self.update('recovering', 'Recovered a signup control through MCP/CDP.')
            return target
        if optional:
            return None
        raise ValueError('The signup control is missing or ambiguous. Review the browser.')
