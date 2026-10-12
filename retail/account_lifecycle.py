"""Verified account states and bounded handoffs; existence is never inferred from buttons."""
import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from .models import DOMAINS
from .store import now


class AccountState(StrEnum):
    UNKNOWN='unknown'
    AUTHENTICATED='authenticated'
    SIGNIN='signin_required'
    SIGNUP_ELIGIBLE='signup_eligible'
    SIGNUP='signup_in_progress'
    VERIFICATION='verification_required'
    RECOVERY='recovery_required'
    SUCCESS='successfully_authenticated'
    RECOVERABLE='recoverable_error'
    BLOCKED='authorization_required'


@dataclass(frozen=True)
class AccountObservation:
    state: AccountState
    evidence: str


SIGNALS=r'''() => {
 const visible=e=>e&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const has=s=>[...document.querySelectorAll(s)].some(visible);
 const alerts=[...document.querySelectorAll('#auth-error-message-box,#auth-warning-message-box,[role=alert],.a-alert-content,h1,h2,[role=heading]')].filter(visible).map(e=>e.innerText).join(' ');
 const exists=/already (?:exists|registered|associated)|email address.*already.*(?:use|account)|account.*already.*(?:email|phone)/i.test(alerts);
 const absent=/cannot find an account|couldn.t find an account|no account (?:was )?found|not (?:yet )?registered|looks like you.re new to amazon/i.test(alerts);
 return {exists,absent,error:has('#auth-error-message-box,#auth-warning-message-box,[role=alert]'),
   registration_internal_error:/^\/ap\/register\/?$/.test(location.pathname)&&has('form input[name=customerName],form #ap_customer_name')&&
     [...document.querySelectorAll('#auth-error-message-box,#auth-warning-message-box')].some(e=>visible(e)&&/internal error\b/i.test(e.innerText)),
   invalid:/incorrect password|password.*incorrect|invalid password/i.test(alerts),
   cooldown:/too many|try again later|temporarily unavailable/i.test(alerts),
   rate_limit:/too many|rate limit/i.test(alerts),
   recovery:/^\/ap\/(?:forgotpassword|accountrecovery)/i.test(location.pathname),
   verification:has('#captchacharacters,#auth-mfa-otpcode,#cvf-input-code,input[autocomplete=one-time-code],iframe[src*="arkoselabs"]')||/^\/ap\/cvf/.test(location.pathname),
   signup:has('input[name=customerName],#ap_customer_name,input[autocomplete=name]')&&has('input[type=password]'),
   signin:/^\/ap\/(?:signin|login)/.test(location.pathname)&&has('input[type=password],#ap_email,#ap_email_login'),
   signed:has('#nav-link-accountList .nav-line-1')&&[...document.querySelectorAll('#nav-link-accountList .nav-line-1')].some(e=>visible(e)&&e.innerText.trim()&&!/sign in/i.test(e.innerText))};
}'''


def classify(signals, *, origin_valid=True, session_verified=False):
    if not origin_valid:return AccountObservation(AccountState.BLOCKED,'unexpected_origin')
    if session_verified:return AccountObservation(AccountState.SUCCESS,'live_session_verified')
    if signals.get('verification'):return AccountObservation(AccountState.VERIFICATION,'website_verification')
    if signals.get('recovery'):return AccountObservation(AccountState.RECOVERY,'account_recovery')
    if signals.get('invalid'):return AccountObservation(AccountState.BLOCKED,'invalid_credentials')
    if signals.get('cooldown') and (not signals.get('registration_internal_error') or signals.get('rate_limit')):
        return AccountObservation(AccountState.BLOCKED,'retailer_cooldown')
    if signals.get('exists') and signals.get('absent'):return AccountObservation(AccountState.RECOVERABLE,'ambiguous_account_response')
    if signals.get('exists'):return AccountObservation(AccountState.SIGNIN,'explicit_existing_account')
    if signals.get('absent'):return AccountObservation(AccountState.SIGNUP_ELIGIBLE,'explicit_unregistered_account')
    if signals.get('registration_internal_error'):return AccountObservation(AccountState.RECOVERABLE,'registration_internal_error')
    if signals.get('error'):return AccountObservation(AccountState.RECOVERABLE,'website_error')
    if signals.get('signed'):return AccountObservation(AccountState.AUTHENTICATED,'session_candidate')
    if signals.get('signup'):return AccountObservation(AccountState.SIGNUP,'registration_form')
    if signals.get('signin'):return AccountObservation(AccountState.SIGNIN,'signin_form')
    return AccountObservation(AccountState.UNKNOWN,'unrecognized_page')


async def observe(page, account):
    origin=urlsplit(page.url)
    valid=origin.scheme=='https' and origin.netloc==DOMAINS[account.get('region','US')]
    if not valid:return classify({},origin_valid=False)
    return classify(await page.evaluate(SIGNALS))


def record(store, account_id, observation, *, intent=None):
    key='lifecycle-'+account_id
    old=store.get('account_lifecycle',key) or {}
    # Store categorical evidence only; registry identity and encrypted credentials stay canonical.
    event={'state':str(observation.state),'evidence':observation.evidence,'at':now()}
    history=old.get('history',[])
    if not history or (history[-1]['state'],history[-1]['evidence'])!=(event['state'],event['evidence']):history=[*history,event][-20:]
    return store.put('account_lifecycle',{**old,**event,'account_id':account_id,'intent':intent or old.get('intent','signin'),'history':history},key)


async def signup_handoff(adapter, account, page, update):
    """Return True after one existing-account sign-in handoff in this same context."""
    observation=await observe(page,account)
    record(adapter.store,account['id'],observation,intent='signup')
    if observation.state!=AccountState.SIGNIN:return False
    context=page.context
    if getattr(context,'_retail_signup_signin_attempted',False):return True
    context._retail_signup_signin_attempted=True
    update('signin','Amazon presented an existing-account sign-in step. Signing in with this account.',signin_attempted=True)
    if observation.evidence=='explicit_existing_account':
        await adapter.navigate(page,f"https://{DOMAINS[account['region']]}/gp/your-account/order-history",wait_until='domcontentloaded')
    await adapter.authenticate(page,account)
    return True


async def authorized_signup(adapter, account, page):
    """Only an explicit signup operation may cross from sign-in into registration."""
    if not getattr(page.context,'_retail_signup_authorized',False):
        return False
    if getattr(page.context,'_retail_signin_signup_attempted',False):
        return False
    observation=await observe(page,account)
    if observation.state!=AccountState.SIGNUP_ELIGIBLE:
        return False
    # A negative account-existence response only reconciles this account after
    # its identifier was submitted in this browser. Never trust a generic button.
    identifier=account.get('signin_identifier') or account.get('email','')
    if not identifier or getattr(page.context,'_retail_signup_identifier',None)!=identifier:
        return False
    page.context._retail_signin_signup_attempted=True
    from .account_creation import complete_signup_form
    from .signup_controls import SignupControls
    def update(status,message,**extra):
        key='creation-'+account['id'];old=adapter.store.get('account_creation',key) or {}
        adapter.store.put('account_creation',{**old,'account_id':account['id'],'status':status,'message':message,'updated_at':now(),**extra},key)
    old=adapter.store.get('account_creation','creation-'+account['id']) or {}
    flags=('submitted','claim_submitted','new_account_submitted','password_submitted','phone_submitted',
           'otp_submitted','sms_otp_submitted','captcha_submitted','signin_attempted')
    history=old.get('reconciled_attempts',[])
    history=[*history,{'at':now(),'evidence':observation.evidence,**{k:bool(old.get(k)) for k in flags}}][-20:]
    update('signup_eligible','Amazon confirmed this identifier has no account. Continuing signup.',
           **{k:False for k in flags},captcha_attempted=False,captcha_phase='',captcha_error='',captcha_type='',reconciled_attempts=history)
    record(adapter.store,account['id'],observation,intent='signup')
    # Follow the observed interstitial. Do not discard its retailer flow state.
    await complete_signup_form(adapter,account,page,SignupControls(page,DOMAINS[account['region']],update),update)
    return True


async def reconcile_registration_error(adapter, account, page, update):
    """One account check per explicit signup/resume; never replay registration on an error."""
    key='creation-'+account['id']
    old=adapter.store.get('account_creation',key) or {}
    extra={'captcha_retailer_outcome':'registration_error'} if old.get('captcha_submitted') else {}
    if (not getattr(page.context,'_retail_signup_authorized',False)
            or old.get('registration_error_recheck_attempted')):
        update('retailer_error','Amazon returned an internal registration error. The automatic account check has not resolved it. Resume signup to check the account again.',
               recovery_status='retailer_error',**extra)
        return False
    # A Retry-After deadline comes from the retailer response, not a settle
    # timer. Do not navigate early, or retry against a page the user changed.
    deadline=old.get('registration_retry_at',0)
    if deadline>time.time():
        document=await page.evaluate('()=>performance.timeOrigin')
        update('retry_waiting','Amazon requested a retry delay. One automatic account check will run when it expires.',recovery_status='retry_scheduled')
        await asyncio.sleep(deadline-time.time())
        if page.is_closed():raise asyncio.CancelledError()
        if await page.evaluate('()=>performance.timeOrigin')!=document:return True
    # Persist before navigation so cancellation/recreated watchers cannot repeat
    # this check. Submission guards survive until an explicit negative response
    # to this account's identifier is observed by authorized_signup.
    update('reconciling','Amazon returned an internal registration error. Checking whether the account was created before continuing.',
           registration_error_recheck_attempted=True,recovery_status='checking_account',**extra)
    page.context._retail_signup_identifier=None
    page.context._retail_signin_signup_attempted=False
    page.context._retail_signup_signin_attempted=False
    from .amazon import BackoffRequired, AccessDenied
    try:
        await adapter.navigate(page,f"https://{DOMAINS[account['region']]}/gp/your-account/order-history",wait_until='domcontentloaded')
        await adapter.authenticate(page,account)
    except (asyncio.CancelledError,BackoffRequired,AccessDenied):
        raise
    except Exception:
        from .account_creation import signup_state
        current=await signup_state(page)
        if current['captcha'] or current['otp'] or current['signed'] and not current['error']:
            return True  # The watcher owns verification and live session checks.
        update('retailer_error','Amazon returned an internal registration error and the account check could not finish. Previous submissions remain protected; resume signup to check again.',
               recovery_status='account_check_incomplete')
        return False
    update('resuming','Account check finished; checking the current signup or verification step.',recovery_status='account_checked')
    return True


def observe_signup_responses(page, store, account_id):
    """Retain explicit retry deadlines and HTTP restrictions for this owned page."""
    if getattr(page,'_retail_signup_response_observer',None):return
    def response_received(response):
        target=urlsplit(response.url)
        if target.scheme!='https' or target.netloc!=urlsplit(page.url).netloc:return
        if not (response.request.is_navigation_request() and response.frame is page.main_frame):return
        from .amazon import Amazon
        delay=Amazon._retry_after_seconds(response)
        key='creation-'+account_id
        old=store.get('account_creation',key) or {}
        extra={'signup_http_status':response.status}
        if delay is not None:
            extra['registration_retry_at']=max(old.get('registration_retry_at',0),time.time()+delay)
        store.put('account_creation',{**old,**extra},key)
    page._retail_signup_response_observer=response_received
    page.on('response',response_received)
