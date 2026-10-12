"""Read-only challenge classification and scoped signup token delivery.

Provider results are untrusted until the retailer advances the current page.
Detection supports more families than automatic submission intentionally.
"""
import asyncio
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

from patchright.async_api import TimeoutError as BrowserTimeoutError

from . import behavior
from .services import SolverService

LABELS = {
    'image': 'Image CAPTCHA', 'arkose': 'Arkose / FunCaptcha',
    'recaptcha': 'reCAPTCHA v2', 'recaptcha_enterprise': 'reCAPTCHA Enterprise',
    'hcaptcha': 'hCaptcha', 'turnstile': 'Cloudflare Turnstile',
    'geetest': 'GeeTest', 'aws': 'Amazon AWS WAF CAPTCHA', 'datadome': 'DataDome',
    'friendly': 'Friendly Captcha', 'tencent': 'Tencent CAPTCHA',
    'mtcaptcha': 'MTCaptcha', 'unknown': 'CAPTCHA / security puzzle',
}
IMAGE_FIELDS = '#captchacharacters:visible,input[name=captcha]:visible,input[name=captchaCode]:visible'

# No page prose is returned. Hidden scripts/badges alone are not challenges.
DETECT = r'''() => {
 const visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility==='visible'&&getComputedStyle(e).display!=='none'&&getComputedStyle(e).opacity!=='0';
 const nodes=s=>[...document.querySelectorAll(s)].filter(visible);
 const found=new Set();
 const identify=s=>{
   if(/arkoselabs|funcaptcha|arkose|\/fc\/(?:assets|gc|gt2)/i.test(s))return 'arkose';
   if(/recaptcha\/enterprise/i.test(s))return 'recaptcha_enterprise';
   if(/recaptcha/i.test(s))return 'recaptcha';
   if(/hcaptcha/i.test(s))return 'hcaptcha';
   if(/challenges\.cloudflare\.com|turnstile/i.test(s))return 'turnstile';
   if(/geetest/i.test(s))return 'geetest';
   if(/awswaf|aws-waf|captcha\.aws/i.test(s))return 'aws';
   if(/datadome|captcha-delivery\.com/i.test(s))return 'datadome';
   if(/friendlycaptcha|frc-captcha/i.test(s))return 'friendly';
   if(/tencent|turing\.captcha|tcaptcha/i.test(s))return 'tencent';
   if(/mtcaptcha/i.test(s))return 'mtcaptcha';
   return '';
 };
 for(const e of nodes('iframe')){
   const kind=identify([e.src,e.id,e.name,e.title].join(' '));
   if(kind)found.add(kind);
   else if(/captcha|challenge|enforcement|human verification/i.test([e.id,e.name,e.title].join(' ')))found.add('unknown');
 }
 for(const [kind,selector] of Object.entries({image:'#captchacharacters,input[name=captcha],input[name=captchaCode]',arkose:'#arkose,#arkose-container,#funcaptcha,[data-pkey]',recaptcha:'.g-recaptcha',hcaptcha:'.h-captcha',turnstile:'.cf-turnstile',geetest:'.geetest_panel,.geetest_holder',aws:'#aws-waf-captcha-container',friendly:'.frc-captcha',mtcaptcha:'.mtcaptcha',tencent:'#tcaptcha_transform'})){
   if(nodes(selector).length)found.add(kind);
 }
 // Frame URLs identify otherwise unnamed cross-origin puzzle documents.
 if(window!==window.top){const kind=identify(location.hostname+location.pathname);if(kind)found.add(kind);}
 for(const e of nodes('h1,h2,h3,[role=heading],#captcha-internal,#captcha-container')){
   if(/solve (?:this|the) puzzle|protect your account|verify (?:that )?you(?: are|'re) human|complete (?:the|this) (?:captcha|security check)/i.test(e.innerText||''))found.add('unknown');
 }
 if(found.has('recaptcha')&&document.querySelector('script[src*="recaptcha/enterprise"]')){found.delete('recaptcha');found.add('recaptcha_enterprise');}
 if(found.size>1)found.delete('unknown');
 return [...found].sort();
}'''


async def visible_frame(frame):
    current = frame
    while current.parent_frame:
        element = await current.frame_element()
        try:
            if not await element.evaluate("e=>!!e.getClientRects().length&&getComputedStyle(e).visibility==='visible'&&getComputedStyle(e).display!=='none'&&getComputedStyle(e).opacity!=='0'"):
                return False
        finally:
            await element.dispose()
        current = current.parent_frame
    return True


async def detect(page):
    kinds = set()
    async with asyncio.timeout(5):
        for frame in page.frames[:32]:
            try:
                if await visible_frame(frame):
                    kinds.update(await frame.evaluate(DETECT))
            except Exception:
                if frame.is_detached():
                    continue
                raise
    if len(kinds)>1:
        kinds.discard('unknown')
    return sorted(kinds)


TOKEN_FIELDS = {
    'arkose': 'input[name="fc-token"],input#FunCaptcha-Token,input#verification-token',
    'recaptcha': 'textarea[name="g-recaptcha-response"],input[name="g-recaptcha-response"]',
    'turnstile': 'input[name="cf-turnstile-response"]',
}

# Amazon CVF submits its own hidden response field from a completion handler.
# It has no visible submit button. Do not generalize this to arbitrary forms.
CVF_FIELD = 'input#cvf_aamation_response_token[name="cvf_aamation_response_token"]'
CVF_FORM = r'''e => {
 const form=e.form;
 if(!e.isConnected || e.type!=='hidden' || e.value || !form || form.id!=='cvf-aamation-challenge-form')return false;
 const action=new URL(Object.getOwnPropertyDescriptor(HTMLFormElement.prototype,'action').get.call(form));
 return location.pathname==='/ap/cvf/request' && form.method.toLowerCase()==='post'
   && action.origin===location.origin && action.pathname==='/ap/cvf/verify'
   && document.querySelectorAll('#cvf-aamation-challenge-form').length===1
   && document.querySelectorAll('[name="cvf_aamation_response_token"]').length===1
   && !form.querySelector('input[type=password],input[name=email],#ap_customer_name');
}'''

CVF_READY_TIMEOUT_MS = 10000
# The Arkose token is an answer to Amazon's ACIC challenge, not the Amazon
# sessionToken consumed by /ap/cvf/verify. Use the observed wrapper callback;
# Amazon's own /aaut/verify exchange decides whether to submit its CVF form.
CVF_BRIDGE = r'''args => {
 const top=window.parent;
 if(top===window || location.origin!==top.location.origin)return null;
 const outer=top.document.querySelectorAll('#cvf-aamation-challenge-iframe');
 const inner=document.querySelectorAll('#aacb-arkose-frame');
 const field=top.document.querySelector('#cvf_aamation_response_token');
 const form=field?.form, acic=top.acic;
 if(outer.length!==1 || outer[0].contentWindow!==window || inner.length!==1 || !field || field.value
    || form?.id!=='cvf-aamation-challenge-form' || form.method.toLowerCase()!=='post'
    || top.location.pathname!=='/ap/cvf/request'
    || typeof postChallengeEvent!=='function' || !acic?.currentAAmationResult
    || acic.clientRequest?.clientReferenceIdValue!=='cvf'
    || typeof acic.handleIframeMessage!=='function' || typeof acic.sendArnoldHttpRequest!=='function')return null;
 const action=new URL(Object.getOwnPropertyDescriptor(top.HTMLFormElement.prototype,'action').get.call(form),top.location.href);
 const endpoint=new URL(acic.serviceEndpoint,top.location.href);
 if(action.origin!==top.location.origin || action.pathname!=='/ap/cvf/verify'
    || endpoint.origin!==top.location.origin || endpoint.pathname!=='/aaut/verify/')return null;
 const result=acic.currentAAmationResult;
 // Amazon selects the challenge level. All numeric Arkose levels use this
 // same native exchange; the exact action remains bound below for delivery.
 if(!/^ARKOSE_LEVEL_[1-9][0-9]*$/.test(result.actionTypeValue) || !result.sessionTokenValue)return null;
 const binding={session:result.sessionTokenValue,action:result.actionTypeValue,
    document:top.performance.timeOrigin,wrapper:performance.timeOrigin,frame:inner[0].getAttribute('src')};
 if(!args)return binding;
 if(JSON.stringify(binding)!==JSON.stringify(args.binding))throw Error('Amazon verification session changed');
 if(window.__retailArkoseAnswerSession===binding.session)throw Error('Amazon verification answer already dispatched');
 window.__retailArkoseAnswerSession=binding.session;
 postChallengeEvent('aa-challenge-complete',{sessionToken:args.token});
 return true;
}'''
# The same observable condition drives preparation and the manual-mode watcher.
# Amazon inserts the inner iframe before expanding it to its rendered size.
CVF_READY = r'''(initializing=false) => {
 const visible=e=>!!e && e.isConnected && !!e.getClientRects().length
   && e.getBoundingClientRect().width>0 && e.getBoundingClientRect().height>0
   && getComputedStyle(e).visibility==='visible' && getComputedStyle(e).display!=='none';
 if(!document.querySelector('#cvf_aamation_response_token'))return false;
 const outer=document.querySelectorAll('iframe#cvf-aamation-challenge-iframe');
 if(outer.length!==1 || !visible(outer[0]))return false;
 try {
   const doc=outer[0].contentDocument;
   if(!doc || doc.location.origin!==location.origin)return false;
   const frames=doc.querySelectorAll('iframe#aacb-arkose-frame');
   if(frames.length!==1 || (!initializing && !visible(frames[0])))return false;
   const url=new URL(frames[0].getAttribute('src'));
   const blobs=url.searchParams.getAll('data');
   return url.protocol==='https:' && url.host==='iframe.arkoselabs.com'
     && /^\/[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}\/index\.html$/.test(url.pathname)
     && blobs.length===1 && blobs[0].length>0 && blobs[0].length<=65536;
 } catch {return false;}
}'''


class ChallengeNotReady(ValueError):
    """Transient frame loading; no paid solve or user handoff has occurred."""


async def wait_cvf_ready(page, *, initializing=False):
    try:
        handle=await page.wait_for_function(CVF_READY,arg=initializing,timeout=CVF_READY_TIMEOUT_MS)
        await handle.dispose()
    except BrowserTimeoutError:
        raise ChallengeNotReady('The Amazon Arkose frame did not become ready in time. Watching for it to finish loading; no paid solve was requested.') from None


async def cvf_parameters(page, prepared=None, *, initializing=False):
    """Read only the observed CVF wrapper; never scrape unrelated frame state."""
    outer=page.locator('iframe#cvf-aamation-challenge-iframe')
    if await outer.count()!=1 or not await outer.is_visible():
        raise ValueError('The Amazon verification frame is missing or ambiguous.')
    wrapper=await (await outer.element_handle()).content_frame()
    origin=urlsplit(page.url)
    if wrapper is None or not await wrapper.evaluate(
        'origin=>location.origin===origin', f'{origin.scheme}://{origin.netloc}'
    ):
        raise ValueError('The Amazon verification frame changed origin.')
    inner=wrapper.locator('iframe#aacb-arkose-frame')
    if await inner.count()!=1 or (not initializing and not await inner.is_visible()):
        raise ValueError('The Amazon Arkose frame is missing or ambiguous.')
    handle=await inner.element_handle()
    src=await handle.get_attribute('src') or ''
    url=urlsplit(src)
    match=re.fullmatch(r'/([A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12})/index\.html',url.path)
    values=parse_qs(url.query)
    blobs=values.get('data',[])
    if (url.scheme!='https' or url.netloc!='iframe.arkoselabs.com' or not match
            or len(blobs)!=1 or not blobs[0] or len(blobs[0])>65536):
        raise ValueError('Required Amazon Arkose parameters are missing or ambiguous.')
    if prepared:
        old=prepared['challenge_frame']
        if wrapper is not prepared['wrapper'] or not await old.evaluate('(e,current)=>e.isConnected && e===current',handle):
            raise ValueError('The Amazon verification frame was replaced.')
    params=await page.evaluate('() => ({document:performance.timeOrigin,userAgent:navigator.userAgent})')
    wrapper_state=await wrapper.evaluate('()=>({document:performance.timeOrigin,bridge:('+CVF_BRIDGE+')(null)})',isolated_context=False)
    params.update(key=match[1],blob=blobs[0],subdomain='https://amazon-api.arkoselabs.com',
                  wrapper_document=wrapper_state['document'],frame_source=src,bridge=wrapper_state['bridge'])
    return params,wrapper,handle


async def prepare_cvf(page, account, solver, validate):
    field=page.locator(CVF_FIELD)
    if await field.count()!=1:
        raise ValueError('A unique Amazon CAPTCHA response field is unavailable.')
    target=await field.element_handle()
    await validate(page,target,account)
    if not await target.evaluate(CVF_FORM):
        raise ValueError('A separate Amazon verification form is unavailable; signup will not be resubmitted.')
    handoff=getattr(page,'_retail_arkose_handoff',None) if solver.get('provider')=='capmonster' else None
    if handoff is not None:
        try:
            await asyncio.wait_for(handoff.ready.wait(),CVF_READY_TIMEOUT_MS/1000)
        except TimeoutError:
            raise ChallengeNotReady('Waiting for fresh Arkose initialization; no paid solve was requested.') from None
    initializing=bool(handoff and handoff.ready.is_set() and not handoff.consumed and not handoff.release.is_set())
    await wait_cvf_ready(page,initializing=initializing)
    # Readiness can span iframe loads or a page replacement. Revalidate the
    # original field before preparing a paid request, never retarget silently.
    await validate(page,target,account)
    if not await target.evaluate(CVF_FORM):
        raise ValueError('The Amazon verification form changed while waiting.')
    params,wrapper,frame=await cvf_parameters(page,initializing=initializing)
    bridge=params['bridge']
    if not bridge:
        raise ValueError('The Amazon verification bridge is not ready; no raw Arkose token will be submitted to registration.')
    if solver.get('provider')=='capmonster' and handoff is not None:
        await handoff.fresh(params)
    task=SolverService.token_task(solver,'arkose',page.url,params,getattr(page.context,'_retail_signup_proxy',None))
    return {'field':target,'form':await target.evaluate_handle('e=>e.form'),
            'params':params,'signature':hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest(),
            'task':task,'url':page.url,'delivery':'amazon_cvf','wrapper':wrapper,'challenge_frame':frame,'handoff':handoff,'bridge':bridge}

PARAMETERS = r'''kind=>{
 const widgets=[...document.querySelectorAll(kind==='arkose'?'[data-pkey]':kind==='turnstile'?'.cf-turnstile[data-sitekey]':'.g-recaptcha[data-sitekey]')];
 const keys=new Set(widgets.map(e=>e.getAttribute(kind==='arkose'?'data-pkey':'data-sitekey')).filter(Boolean));
 let blob='',subdomain='',action='',cdata='',dataS='',invisible=false;
 for(const e of widgets){
   blob=e.getAttribute('data-blob')||blob;action=e.getAttribute('data-action')||action;
   cdata=e.getAttribute('data-cdata')||cdata;dataS=e.getAttribute('data-s')||dataS;
   invisible=e.getAttribute('data-size')==='invisible'||invisible;
 }
 if(kind==='arkose')for(const e of document.querySelectorAll('input[name="fc-token"],#FunCaptcha-Token,#verification-token')){
   for(const p of (e.value||'').split('|')){const n=p.indexOf('=');if(n<0)continue;const k=p.slice(0,n),v=p.slice(n+1);if(k==='pk')keys.add(v);if(k==='surl')subdomain=decodeURIComponent(v);}
 }
 for(const e of document.querySelectorAll('iframe[src]')){
   const u=new URL(e.src,location.href);
   if(kind==='recaptcha'&&/\/(?:recaptcha)\/(?:api2)\//.test(u.pathname)&&u.searchParams.get('k'))keys.add(u.searchParams.get('k'));
   if(kind==='arkose'&&/arkoselabs\.com$/.test(u.hostname)){
     const key=u.searchParams.get('pk')||u.pathname.match(/\/([A-F0-9-]{36})\//i)?.[1];if(key)keys.add(key);
     blob=u.searchParams.get('data[blob]')||blob;
   }
 }
 return {key:keys.size===1?[...keys][0]:'',blob,subdomain,action,cdata,dataS,invisible,document:performance.timeOrigin,userAgent:navigator.userAgent};
}'''


async def prepare_token(page, kind, account, solver, validate):
    """Require a unique, retailer-owned verification form before any paid request."""
    if kind not in TOKEN_FIELDS:
        raise ValueError('This CAPTCHA family needs manual completion in this integration.')
    if solver.get('provider')=='capmonster' and kind=='arkose' and not getattr(page.context,'_retail_signup_proxy',None):
        raise ValueError('CapMonster FunCaptcha requires the same proxy as the account browser. This browser has no proxy; no paid solve was requested.')
    if kind=='arkose' and await page.locator(CVF_FIELD).count():
        return await prepare_cvf(page,account,solver,validate)
    field = page.locator(TOKEN_FIELDS[kind])
    if await field.count()!=1:
        raise ValueError('A unique retailer CAPTCHA response field is unavailable.')
    target = await field.element_handle()
    await validate(page,target,account)
    valid = await target.evaluate("""e=>!!e.form && !e.form.querySelector('input[type=password],input[name=email],#ap_customer_name')""")
    if not valid:
        raise ValueError('A separate verification form is unavailable; signup will not be resubmitted.')
    form = field.locator('xpath=ancestor::form')
    submit = form.locator('button[type=submit]:visible,input[type=submit]:visible,button:not([type]):visible')
    if await submit.count()!=1:
        raise ValueError('A unique verification submit control is unavailable.')
    button = await submit.element_handle()
    await validate(page,button,account)
    params = await page.evaluate(PARAMETERS,kind)
    if not params['key']:
        raise ValueError('Required CAPTCHA parameters are missing or ambiguous.')
    task = SolverService.token_task(solver,kind,page.url,params,getattr(page.context,'_retail_signup_proxy',None))
    signature = hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()
    return {'field':target,'button':button,'params':params,'signature':signature,'task':task,'url':page.url}


async def apply_token(page, kind, prepared, token, account, validate, *, before_submit=None):
    if prepared.get('applied'):
        raise ValueError('This CAPTCHA response was already dispatched; it will not be replayed.')
    if not isinstance(token,str) or not token or len(token)>65536:
        raise ValueError('The CAPTCHA response token is invalid.')
    if page.url!=prepared['url']:
        raise ValueError('The verification page changed while solving.')
    if prepared.get('delivery')=='amazon_cvf':
        handoff=prepared.get('handoff')
        current,_,_=await cvf_parameters(page,prepared,initializing=bool(handoff and not handoff.consumed and not handoff.release.is_set()))
        if handoff:await handoff.fresh(current)
        if current['bridge']!=prepared['bridge']:
            raise ValueError('The Amazon verification session changed while solving.')
    else:
        current = await page.evaluate(PARAMETERS,kind)
    if hashlib.sha256(json.dumps(current,sort_keys=True).encode()).hexdigest()!=prepared['signature']:
        raise ValueError('The CAPTCHA changed while solving.')
    field=prepared['field']
    await validate(page,field,account)
    if prepared.get('delivery')=='amazon_cvf':
        if not await field.evaluate('(e,form)=>('+CVF_FORM+')(e)&&e.form===form',prepared['form']):
            raise ValueError('The Amazon verification form changed.')
        prepared['applied']=True
        if before_submit:before_submit()
        accepted=await prepared['wrapper'].evaluate(CVF_BRIDGE,{'binding':prepared['bridge'],'token':token},isolated_context=False)
        if not accepted:raise ValueError('The Amazon verification bridge changed before delivery.')
        return
    button=prepared['button']
    await validate(page,button,account)
    if not await field.evaluate('e=>e.isConnected') or not await button.evaluate('e=>e.isConnected'):
        raise ValueError('The verification form was replaced.')
    await field.evaluate('''(e,token)=>{const setter=Object.getOwnPropertyDescriptor(e instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set;setter.call(e,token);}''',token)
    # Only the existing verification form is submitted; no guessed callbacks or messages.
    prepared['applied']=True
    if before_submit:before_submit()
    await behavior.click(page,button,timeout=10000)
