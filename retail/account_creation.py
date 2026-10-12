"""Single-account signup with paced input and a durable no-replay boundary."""
import asyncio
import time
from urllib.parse import urlparse

from patchright.async_api import Error as BrowserError

from . import behavior
from .models import DOMAINS
from .store import now
from .signup_controls import SignupControls, CLASSIFY
from .signup_sms import SignupSMS, SMSError
from .services import SolverService, PROVIDERS
from .account_consistency import AccountBrowserProfiles
from .amazon import BackoffRequired, AccessDenied
from . import signup_challenges as challenges
from . import account_lifecycle as lifecycle
from .signup_recovery import recover_signup
from .proxy_pool import ProxySelectionError
from .arkose_handoff import ArkoseHandoff
from .captcha_attempt import CaptchaAttempt, paused_message


SIGNUP_STATE = r'''() => {
    const visible=s=>[...document.querySelectorAll(s)].some(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
    return {document:performance.timeOrigin, path:location.pathname,
        name:[...document.querySelectorAll('input')].some(e=>(__CLASSIFY__)(e,'name')),
        email:[...document.querySelectorAll('input')].some(e=>(__CLASSIFY__)(e,'email')),
        phone:[...document.querySelectorAll('input')].some(e=>(__CLASSIFY__)(e,'phone')),
        password:[...document.querySelectorAll('input')].some(e=>(__CLASSIFY__)(e,'password')),
        passkey_skip:[...document.querySelectorAll('a,button,input[type=submit]')].some(e=>(__CLASSIFY__)(e,'passkey_skip')),
        otp_channel:(()=>{const kind=(document.querySelector('input[name=verificationPageContactType]')?.value||'').toLowerCase();if(/phone|mobile|sms/.test(kind))return 'phone';if(/email/.test(kind))return 'email';const heading=[...document.querySelectorAll('h1,h2,h3,label')].map(e=>e.innerText).join(' ');return /(?:verify|verification).*(?:phone|mobile)|(?:phone|mobile).*verification|SMS code/i.test(heading)?'phone':/verify email|email verification/i.test(heading)?'email':'';})(),
        new_account:[...document.querySelectorAll('a,button,input[type=submit]')].some(e=>(__CLASSIFY__)(e,'new_account')),
        otp:visible('#cvf-input-code,#auth-mfa-otpcode,input[autocomplete=one-time-code]')||[...document.querySelectorAll('input')].some(e=>(__CLASSIFY__)(e,'otp')),
        otp_fields:[...document.querySelectorAll('input')].filter(e=>(__CLASSIFY__)(e,'otp')).length,
        otp_buttons:[...document.querySelectorAll('button,input[type=submit]')].filter(e=>(__CLASSIFY__)(e,'otp_submit')).length,
        captcha:(__DETECT__)().length>0,
        captcha_ready:(__CVF_READY__)(),
        error:visible('#auth-error-message-box,#auth-warning-message-box'),
        signed:visible('#nav-link-accountList .nav-line-1')&&!/sign in/i.test(document.querySelector('#nav-link-accountList .nav-line-1')?.innerText||'')};
}'''.replace('__CLASSIFY__', CLASSIFY).replace('__DETECT__', challenges.DETECT).replace('__CVF_READY__', challenges.CVF_READY)


async def signup_state(page):
    # A redirect can replace the execution context between condition polls.
    # Re-read state only; never replay the action which caused navigation.
    async with asyncio.timeout(5):
        while True:
            try:
                await page.wait_for_load_state('domcontentloaded',timeout=5000)
                state = await page.evaluate(SIGNUP_STATE)
                state['captcha_types'] = await challenges.detect(page)
                state['captcha'] = bool(state['captcha_types'])
                if state['document']!=await page.evaluate('()=>performance.timeOrigin'):
                    continue  # Never combine old form fields with new frame evidence.
                return state
            except BrowserError as exc:
                if page.is_closed() or not any(text in str(exc).lower() for text in ('execution context was destroyed', 'cannot find context with specified id')):
                    raise
                await page.wait_for_load_state('domcontentloaded',timeout=5000)
                await asyncio.sleep(.05)


async def changed(page, before, timeout=15000):
    async with asyncio.timeout(timeout/1000):
        while True:
            current = await signup_state(page)
            if any(current.get(k)!=v for k,v in before.items()):
                return
            # Condition polling also observes nested frame updates, without navigation.
            await asyncio.sleep(.25)


async def wait_signup_email(identities, account, page, since, validate):
    job=asyncio.create_task(identities.wait_code(account,since=since,before_claim=validate))
    def closed(*_):
        job.cancel()
    def navigated(frame):
        if frame is page.main_frame:job.cancel()
    page.on('close',closed)
    page.on('framenavigated',navigated)
    try:
        return await job
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():raise
        raise ValueError('Email verification page changed while waiting.') from None
    finally:
        page.remove_listener('close',closed)
        page.remove_listener('framenavigated',navigated)


async def signup_stage(page, kinds, timeout=15000):
    """Wait for actionable controls, including controls rendered after navigation."""
    async with asyncio.timeout(timeout/1000):
        while True:
            state = await signup_state(page)
            if any(state[k] for k in kinds):
                return state
            await asyncio.sleep(.25)


async def open_signup(adapter, account, page, controls, update):
    domain = DOMAINS[account['region']]
    await adapter.navigate(page, f'https://{domain}/', wait_until='domcontentloaded', timeout=20000)
    update('opening', 'Opening Account & Lists, then Start here.')
    await page.locator('header, #navbar, #nav-link-accountList').first.wait_for(state='visible',timeout=10000)
    await behavior.prepare(page, await controls.find('menu'))
    await page.wait_for_function('''() => [...document.querySelectorAll('a[href*="/ap/register"]')].some(e=>e.getClientRects().length&&getComputedStyle(e).visibility==='visible')''',timeout=8000)
    target = await controls.find('signup')
    href = urlparse(await target.get_attribute('href'))
    if href.scheme!='https' or href.netloc!=domain or href.path!='/ap/register':
        raise ValueError('The observed signup link is not in this account region.')
    await behavior.click(page,target,timeout=10000)
    await page.wait_for_function('''() => location.pathname.startsWith('/ap/')||location.pathname.startsWith('/ax/')''',timeout=15000)
    await page.wait_for_load_state('domcontentloaded',timeout=15000)
    await signup_stage(page, ['name','email','phone','password','new_account','otp','captcha','error'])


async def complete_signup_form(adapter, account, page, controls, update, *, progress=None):
    progress = progress or {}
    if await lifecycle.signup_handoff(adapter,account,page,update):return
    before = await signup_state(page)
    if not before['name'] and before['email'] and not before['phone'] and not before['password']:
        if progress.get('claim_submitted'):
            raise ValueError('The email step was already submitted. Review the current browser.')
        update('filling','Entering email on Sign in or create account.')
        email = await controls.find('email')
        await validate_target(page,email,account)
        await behavior.fill(page,email,account['email'])
        submit = await controls.find('submit')
        await validate_target(page,submit,account)
        update('claim','Continuing from the email step.', claim_submitted=True, otp_since=time.time())
        page.context._retail_signup_identifier = account['email']
        await behavior.click(page,submit,timeout=10000)
        await changed(page,before)
        await page.wait_for_load_state('domcontentloaded',timeout=15000)
    if await lifecycle.signup_handoff(adapter,account,page,update):return
    before = await signup_stage(page, ['name','phone','password','new_account','otp','captcha','error'])
    if before['new_account'] and not any(before[k] for k in ('name','otp','captcha','error')):
        if progress.get('new_account_submitted'):
            raise ValueError('Proceed to create an account was already clicked. Review the current browser.')
        ingress = await controls.find('new_account')
        await validate_target(page,ingress,account)
        # Persist intent before input; a timeout or cancellation must never replay it.
        update('opening','Proceeding to the account registration form.',new_account_submitted=True)
        await behavior.click(page,ingress,timeout=10000)
        await changed(page,before)
        await page.wait_for_load_state('domcontentloaded',timeout=15000)
        before = await signup_stage(page, ['name','phone','password','otp','captcha','error'])
    if not any(before[k] for k in ('name','phone','password')) or any(before[k] for k in ('otp','captcha','error')):
        return
    if before['phone'] and progress.get('phone_submitted') or before['password'] and progress.get('password_submitted'):
        raise ValueError('This signup step was already submitted. Review the browser; it will not be repeated.')
    update('filling','Matching the current signup fields to the saved account details.')
    targets={}
    for kind in ('name','email','phone','password','confirmation'):
        target=await controls.find(kind,optional=True)
        if target is not None:
            await validate_target(page,target,account)
            targets[kind]=target
    submit=await controls.find('submit')
    await validate_target(page,submit,account)
    for target in targets.values():
        if not await target.evaluate('(e,b)=>!!e.form&&e.form===b.form',submit):
            raise ValueError('Signup fields belong to different forms.')
    values={kind:account.get(kind,'') for kind in ('name','email','password')}
    values['confirmation']=account.get('password','')
    if 'phone' in targets:
        sms=getattr(page.context,'_retail_signup_sms',None)
        if sms and not account.get('phone'):
            update('sms_ordering','Reserving one SMSPool number within the saved price cap.')
            order=await sms.number()
            phone,country,national=order['number'],order['country'],order['national']
        else:
            phone=account.get('phone','')
            country='GB' if account['region']=='UK' else account['region']
            prefix='+44' if country=='GB' else '+1'
            if not phone or not phone.startswith(prefix):
                raise SMSError('Save a mobile number for this account region or select an SMSPool connection. An email address will not be entered into a phone field.')
            national=phone[len(prefix):]
        country_control=await controls.find('country',optional=True)
        if country_control is not None:
            await validate_target(page,country_control,account)
            await behavior.prepare(page,country_control)
            await country_control.select_option(country)
        values['phone']=national if country_control is not None else phone
    for kind,target in targets.items():
        value=values[kind]
        if not value:
            raise ValueError('The current signup step needs account details that have not been provided.')
        if not await target.is_editable():
            if await target.input_value()!=value:
                raise ValueError('Signup contains different locked account details.')
        else:
            await behavior.fill(page,target,value)
    before=await signup_state(page)
    flags={'submitted':True,'otp_since':time.time()}
    if 'phone' in targets:flags.update(phone_submitted=True,last_contact_kind='phone')
    if 'email' in targets:flags.update(last_contact_kind='email',registration_used_email=True)
    if 'password' in targets:flags['password_submitted']=True
    update('submitted','Signup step submitted; waiting for the next form or verification.',**flags)
    await behavior.click(page,submit,timeout=10000)
    await changed(page,before)
    update('verification','Checking the next signup or verification step. The verified session saves automatically.')



class SignupDestinationError(ValueError):
    pass


async def validate_target(page, target, account):
    destination = await target.evaluate("e=>e.href || (e.hasAttribute('formaction')?e.formAction:(((e.form||e.closest('form'))?Object.getOwnPropertyDescriptor(HTMLFormElement.prototype,'action').get.call(e.form||e.closest('form')):location.href)))")
    for url in (page.url,destination):
        parsed=urlparse(url)
        if parsed.scheme!='https' or parsed.netloc!=DOMAINS[account['region']]:
            raise SignupDestinationError('Signup control leaves the selected retailer region.')


async def verify_signup(engine, account, context, page, controls, update):
    adapter, store = engine.amazon, engine.store
    key, queue = 'creation-'+account['id'], 'signup-'+account['id']
    lifecycle.observe_signup_responses(page,store,account['id'])
    def manual(message, captcha_type='', *, otp=False):
        if otp and account.get('auto_otp',True) and account.get('mailbox_id'):
            update('email_blocked',message.replace('Email verification needs your input', 'Automatic email verification paused'),captcha_type='')
            return
        update('captcha_manual' if captcha_type else 'email_manual' if otp else 'verification',message,captcha_type=captcha_type)
        store.put('harvesters',{'account_id':account['id'],'status':'waiting','challenge_type':captcha_type,'message':message,'at':now()},queue)
    try:
        async with asyncio.timeout(1800):
            for _ in range(12):
                controls.guard()
                observed=await signup_state(page)
                record=store.get('account_creation',key) or {}
                store.delete('harvesters',queue)
                if not observed['captcha'] and record.get('status','').startswith('captcha_'):
                    update('verification','Verification page changed. Checking the current signup step.',captcha_type='')
                observation=await lifecycle.observe(page,account)
                lifecycle.record(store,account['id'],observation,intent='signup')
                if (record.get('new_account_submitted') and not record.get('submitted')
                        and observed['new_account'] and not any(observed[k] for k in ('name','phone','otp','captcha'))):
                    update('recovery_blocked','Proceed to create an account was already submitted. Waiting for the registration form; the click will not be repeated.',recovery_status='uncertain_submission')
                    await changed(page,observed,timeout=1700000)
                    continue
                if observation.state in (lifecycle.AccountState.BLOCKED,lifecycle.AccountState.RECOVERY) and not observed['captcha'] and not observed['otp']:
                    registration_error=observed['path']=='/ap/register' and observed['name'] and observed['error']
                    if observation.evidence=='retailer_cooldown':
                        message=('Amazon returned a registration error and asked to try again later. ' if registration_error else 'Amazon asked to try again later. ')
                        message+='Automatic signup is paused; no submission or paid solve will be repeated.'
                    else:
                        message='Amazon requires an account action before signup can continue. Review the current browser message.'
                    update('retailer_error' if registration_error else 'review',message,recovery_status='restricted',
                           **({'captcha_retailer_outcome':'registration_error'} if registration_error and record.get('captcha_submitted') else {}))
                    await changed(page,observed,timeout=1700000)
                    continue
                if observation.evidence=='registration_internal_error' and not observed['captcha'] and not observed['otp']:
                    if record.get('signup_http_status') in (403,429,503):
                        update('retailer_error','Amazon refused or rate-limited registration. The HTTP restriction must clear before signup can resume.',recovery_status='restricted')
                        await changed(page,observed,timeout=1700000)
                        continue
                    if await lifecycle.reconcile_registration_error(adapter,account,page,update):continue
                    # The check may have navigated. Wait on its current page,
                    # not the earlier error document, without replaying it.
                    current=await signup_state(page)
                    await changed(page,current,timeout=1700000)
                    continue
                if observation.state==lifecycle.AccountState.RECOVERABLE and not observed['captcha'] and not observed['otp']:
                    update('retailer_error','Amazon returned an account form error. Review the message in the browser; automation resumes when the page changes.',recovery_status='retailer_error')
                    await changed(page,observed,timeout=1700000)
                    continue
                if observation.state==lifecycle.AccountState.SIGNUP_ELIGIBLE:
                    if await lifecycle.authorized_signup(adapter,account,page):continue
                if not record.get('signin_attempted') and await lifecycle.signup_handoff(adapter,account,page,update):
                    continue
                if observed['signed'] and not any(observed[k] for k in ('otp','captcha','error')):
                    if record.get('phone_submitted') and not record.get('registration_used_email'):
                        sms=getattr(context,'_retail_signup_sms',None)
                        number=(sms.record().get('number','') if sms else account.get('phone',''))
                        current=store.get('accounts',account['id'])
                        if current and number:
                            store.put('accounts',{**current,'phone':number,'signin_identifier':number})
                    await adapter.watch_login(account['id'],context,page)
                    return
                if observed['passkey_skip'] and (store.get('settings','settings') or {}).get('suppress_passkey_prompts',True):
                    skip=await controls.find('passkey_skip')
                    await validate_target(page,skip,account)
                    update('verification','Skipping the optional passkey offer.')
                    await behavior.click(page,skip,timeout=10000)
                    await changed(page,observed)
                    continue
                continuation=(observed['phone'] and not record.get('phone_submitted') or observed['password'] and not record.get('password_submitted') and record.get('phone_submitted'))
                if (not record.get('submitted') or continuation) and not any(observed[k] for k in ('otp','captcha','error')):
                    if (observed['name'] or observed['phone'] or observed['password'] or observed['email'] and not record.get('claim_submitted')
                            or observed['new_account'] and not record.get('new_account_submitted')):
                        await complete_signup_form(adapter,account,page,controls,update,progress=record)
                        continue
                    if observed['path']=='/' and not record.get('claim_submitted'):
                        await open_signup(adapter,account,page,controls,update)
                        await complete_signup_form(adapter,account,page,controls,update,progress=record)
                        continue
                sms=getattr(context,'_retail_signup_sms',None)
                phone_otp=observed['otp_channel']=='phone' or not observed['otp_channel'] and record.get('last_contact_kind')=='phone'
                if observed['otp'] and not observed['captcha'] and phone_otp:
                    if not account.get('auto_otp',True) or not sms:
                        update('sms_manual','Enter the SMS code for this account in the browser. No SMSPool order is linked to this step.')
                        store.put('harvesters',{'account_id':account['id'],'status':'waiting','challenge_type':'sms','message':'SMS verification requires the code for this account.','at':now()},queue)
                    elif record.get('sms_otp_submitted'):
                        update('sms_blocked','An SMS code was already submitted. It will not be sent again automatically.')
                    else:
                        try:
                            field=await controls.find('otp')
                            submit=await controls.find('otp_submit')
                            await validate_target(page,field,account)
                            await validate_target(page,submit,account)
                            if not await field.evaluate('(e,b)=>!!e.form&&e.form===b.form',submit):
                                raise SMSError('SMS controls belong to different forms.')
                            update('sms_waiting',"Waiting for the code on this account's SMSPool order.")
                            code=await sms.code()
                            controls.guard()
                            if (await signup_state(page))['document']!=observed['document']:
                                raise SMSError('The SMS verification document changed while waiting for the code.')
                            await validate_target(page,field,account)
                            await validate_target(page,submit,account)
                            if not await field.evaluate('e=>e.isConnected') or not await submit.evaluate('e=>e.isConnected'):
                                raise SMSError('The SMS verification form changed while waiting for the code.')
                            await behavior.fill(page,field,code)
                            sms.submitted()
                            update('sms_submitted','SMS code submitted; checking the next step.',sms_otp_submitted=True)
                            await behavior.click(page,submit,timeout=10000)
                            await changed(page,observed)
                            continue
                        except SMSError as exc:
                            update('sms_blocked',str(exc))
                        except Exception:
                            update('sms_blocked','SMS verification paused because the form changed or could not advance. No automatic resubmission.')
                elif observed['otp'] and not observed['captcha'] and account.get('auto_otp',True) and account.get('mailbox_id') and not record.get('otp_submitted'):
                    try:
                        # Resolve controls before consuming an email. Both must belong to the same form.
                        field=await controls.find('otp')
                        submit=await controls.find('otp_submit')
                        await validate_target(page,field,account)
                        await validate_target(page,submit,account)
                        if not await field.evaluate('(e,button)=>!!e.form&&e.form===button.form',submit):
                            raise ValueError('Email verification controls belong to different forms.')
                    except SignupDestinationError:
                        manual('Automatic email verification paused: the form destination is outside this account\'s retailer region. No mailbox code was consumed.',otp=True)
                    except Exception:
                        manual('Email verification needs your input: the code field or Verify button is missing or ambiguous. No mailbox code was consumed.',otp=True)
                    else:
                        try:
                            update('email_waiting','Waiting for a fresh matching email code through IMAP.',captcha_type='')
                            async def validate_email():
                                controls.guard()
                                if (await signup_state(page))['document']!=observed['document']:
                                    raise ValueError('Email verification page changed while checking the mailbox.')
                                await validate_target(page,field,account)
                                await validate_target(page,submit,account)
                                if not await field.evaluate('e=>e.isConnected') or not await submit.evaluate('e=>e.isConnected'):
                                    raise ValueError('Email verification form changed while checking the mailbox.')
                            code=await wait_signup_email(adapter.identities,{**account,'totp_secret':''},page,
                                record.get('otp_since',time.time()-10),validate_email)
                            await validate_email()
                            await behavior.fill(page,field,code['code'])
                            update('email_submitted','Email code submitted; checking the next step.',otp_submitted=True)
                            await behavior.click(page,submit,timeout=10000)
                            await changed(page,observed)
                            continue
                        except Exception:
                            if (store.get('account_creation',key) or {}).get('otp_submitted'):
                                manual('Email code was submitted but verification did not advance. Review Amazon\'s response in the browser; the code will not be submitted again automatically.',otp=True)
                            else:
                                manual('Email verification needs your input. No matching fresh code was retrieved, the mailbox connection failed, or the verification page changed. Check the linked mailbox and browser.',otp=True)
                elif observed['otp'] and not observed['captcha']:
                    if record.get('otp_submitted'):
                        manual('Email verification needs your input: a code was already submitted. Review Amazon\'s response; it will not be submitted again automatically.',otp=True)
                    elif not account.get('auto_otp',True):
                        manual('Email verification needs your input: automatic code entry is disabled for this account.',otp=True)
                    else:
                        manual('Email verification needs your input: no IMAP mailbox is linked to this account. Select Verification mailbox in Edit details before starting its browser.',otp=True)
                elif observed['captcha']:
                    kinds=observed['captcha_types']
                    kind=kinds[0] if len(kinds)==1 else 'unknown'
                    label=challenges.LABELS[kind]
                    solver=store.get('solvers',account.get('solver_id','')) or {}
                    field=page.locator(challenges.IMAGE_FIELDS)
                    handoff=getattr(page,'_retail_arkose_handoff',None)
                    if (handoff and handoff.ready.is_set() and not handoff.consumed and not handoff.release.is_set()
                            and handoff.parameters['document']==observed['document']
                            and record.get('captcha_document')!=observed['document']):
                        # A newly intercepted initialization proves this is a fresh
                        # challenge. Registration/OTP submission guards stay intact.
                        update('captcha_solving','Fresh verification challenge detected.',captcha_attempted=False,
                               captcha_submitted=False,captcha_phase='',captcha_error='',captcha_document=observed['document'])
                        record=store.get('account_creation',key)
                    if record.get('captcha_submitted') or record.get('captcha_attempted'):
                        manual(paused_message(record,label,solver.get('provider','the assigned service')),kind)
                    elif not solver or solver.get('provider')=='manual':
                        manual(f'{label} — user input required. Solve it in the signup browser; automation resumes after completion.',kind)
                    elif kind=='image' and solver.get('provider') in PROVIDERS and await field.count()==1:
                        try:
                            await validate_target(page,field,account)
                            form=field.locator('xpath=ancestor::form')
                            picture=form.locator('img[src*="captcha"]:visible,img[alt*="captcha" i]:visible')
                            if await picture.count()!=1:
                                raise ValueError('Unsupported CAPTCHA image')
                            image_source=await picture.get_attribute('src')
                            update('captcha_solving',f'{label} — solving with {solver["provider"]}.',captcha_submitted=True,captcha_type=kind)
                            async with asyncio.timeout(min(300,solver.get('timeout_seconds',120))+10):
                                answer=await SolverService().solve_image(solver,await picture.screenshot())
                            controls.guard()
                            if await picture.get_attribute('src')!=image_source:
                                raise ValueError('CAPTCHA changed while solving')
                            await validate_target(page,field,account)
                            await behavior.fill(page,field,answer)
                            submit=form.locator('button[type=submit]:visible,input[type=submit]:visible')
                            if await submit.count()!=1:raise ValueError('Ambiguous CAPTCHA control')
                            await validate_target(page,submit,account)
                            await behavior.click(page,submit,timeout=10000)
                            await changed(page,observed)
                            continue
                        except Exception:
                            manual(f'{label} — user input required. The assigned service could not complete this challenge. Solve it in the signup browser.',kind)
                    elif kind in challenges.TOKEN_FIELDS:
                        try:
                            controls.guard()
                            prepared=await challenges.prepare_token(page,kind,account,solver,validate_target)
                        except challenges.ChallengeNotReady:
                            update('recovering','Inspecting the stalled verification page through MCP/CDP.',captcha_type=kind)
                            inspected=await controls.inspect_loading()
                            update('captcha_loading',f'{label} - waiting for the verification frame to load. No paid solve requested.',captcha_type=kind,recovery_status=inspected)
                        except ValueError as exc:
                            # These are fixed local adapter errors, never provider text.
                            if handoff:handoff.finish()
                            manual(f'{label} — user input required. {exc}',kind)
                        else:
                            attempt=CaptchaAttempt(store,account['id'],solver['provider'],kind,update,observed['document'])
                            try:
                                token=await SolverService().solve_token(solver,prepared['task'],user_agent=prepared['params']['userAgent'],progress=attempt.progress)
                                controls.guard()
                                if (await challenges.detect(page))!=kinds:
                                    raise ValueError('The challenge changed while solving.')
                                await challenges.apply_token(page,kind,prepared,token,account,validate_target,before_submit=lambda:attempt.progress('submitting'))
                                attempt.progress('submitted')
                                if handoff:handoff.finish(submitted=True)
                            except asyncio.CancelledError as exc:
                                attempt.failed(exc)
                                if handoff:handoff.finish()
                                raise
                            except Exception as exc:
                                attempt.failed(exc)
                                if handoff:handoff.finish(submitted=bool(prepared.get('applied')))
                                manual(paused_message(store.get('account_creation',key),label,solver['provider']),kind)
                            else:
                                # A slow retailer response is not a solver failure.
                                # Keep the submitted phase and never send another token.
                                try:await changed(page,observed)
                                except TimeoutError:pass
                                continue
                    else:
                        manual(f'{label} — user input required. The assigned provider has no supported adapter for this challenge. Solve it in the signup browser.',kind)
                else:
                    try:
                        reached=await recover_signup(adapter,account,page,update)
                        if reached=='signin':
                            document=await page.evaluate('()=>performance.timeOrigin')
                            attempted=getattr(page,'_retail_signup_signin_documents',set())
                            if document in attempted:
                                raise ValueError('Sign-in did not advance from its recovered checkpoint')
                            attempted.add(document);page._retail_signup_signin_documents=attempted
                            await adapter.authenticate(page,account)
                        elif reached=='retailer_error':
                            # Re-read the error in the deterministic handler.
                            continue
                        elif reached=='restricted':
                            update('recovery_blocked','Amazon requires an account decision or a cooldown. Automatic recovery is paused.',recovery_status='restricted')
                            await changed(page,observed,timeout=1700000)
                        elif reached in ('registration','new_account') and record.get('submitted'):
                            if not await lifecycle.authorized_signup(adapter,account,page):
                                update('recovery_blocked','The current registration step has an unresolved previous submission. No duplicate submission was sent.',recovery_status='uncertain_submission')
                                await changed(page,observed,timeout=1700000)
                        continue
                    except (BackoffRequired,AccessDenied):
                        raise
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        update('recovery_blocked','Automatic signup recovery could not reach a supported checkpoint. No verification challenge was detected; waiting for the page to change.',recovery_status='exhausted')
                # A changed lifecycle/control state resumes verification, never signup.
                await changed(page,observed,timeout=1700000)
            update('review','Verification step limit reached. Review the browser and save the signed-in session.')
    except asyncio.CancelledError:
        raise
    except (BackoffRequired,AccessDenied) as exc:
        update('review','Amazon requested a pause. No automatic retry.',retry_at=time.time()+max(60,getattr(exc,'retry_after_seconds',None) or 60))
    except SMSError as exc:
        update('sms_blocked',str(exc))
    except Exception:
        if not page.is_closed():
            try:
                reached=await recover_signup(adapter,account,page,update)
                if reached=='restricted':
                    update('recovery_blocked','Amazon requires an account decision or a cooldown. Automatic recovery is paused.',recovery_status='restricted')
                else:
                    if reached=='signin':await adapter.authenticate(page,account)
                    # Transfer watcher ownership only after recovery releases MCP.
                    adapter.login_watchers[account['id']]=asyncio.create_task(
                        verify_signup(engine,account,context,page,controls,update))
            except (BackoffRequired,AccessDenied) as exc:
                update('review','Amazon requested a pause. No automatic retry.',retry_at=time.time()+max(60,getattr(exc,'retry_after_seconds',None) or 60))
            except asyncio.CancelledError:
                raise
            except Exception:
                update('recovery_blocked','Automatic signup recovery exhausted the supported actions. Review the browser; no submission was repeated.',recovery_status='exhausted')
    finally:
        store.delete('harvesters',queue)
        if adapter.login_watchers.get(account['id']) is asyncio.current_task():
            adapter.login_watchers.pop(account['id'],None)


def snapshot(engine):
    result = []
    accounts = {a['id']: a for a in engine.store.all('accounts')}
    for record in engine.store.all('account_creation'):
        account = accounts.get(record['account_id'])
        if not account:
            continue
        opened = record['account_id'] in engine.amazon.logins
        lock = engine.account_locks.get(record['account_id'])
        status, message = record['status'], record['message']
        if account.get('logged_in'):
            status, message = 'completed', 'Account session verified and saved.'
        elif record.get('retry_at',0)>time.time():
            status='cooldown'
        elif not opened and status!='startup_failed' and not (lock and lock.locked()):
            status = 'review' if any(record.get(k) for k in ('submitted','claim_submitted','new_account_submitted')) else 'draft'
            message = ('Signup was interrupted. Resume to check whether Amazon requires sign-in or continued registration.'
                       if any(record.get(k) for k in ('submitted','claim_submitted','new_account_submitted')) else 'Ready to open signup.')
        result.append({**record, 'status': status, 'message': message, 'browser_open': opened})
    return result


async def create_account(engine, account, *, resume=False):
    adapter, store = engine.amazon, engine.store
    account_id = account['id']
    key = 'creation-'+account_id
    old = store.get('account_creation', key) or {}
    if max(old.get('retry_at',0),old.get('registration_retry_at',0))>time.time():
        raise ValueError('Amazon requested a cooldown. Wait before starting signup again.')
    if not resume and any(old.get(k) for k in ('submitted','claim_submitted','new_account_submitted')):
        raise ValueError('Registration was already submitted. Use Resume signup to check the account before continuing.')
    if account.get('logged_in'):
        raise ValueError('This account already has a saved session. Use Accounts to open it.')
    if account.get('retailer', 'amazon') != 'amazon':
        raise ValueError('Automatic account creation currently supports Amazon only.')
    if not all(account.get(k) for k in ('name', 'email', 'password')):
        raise ValueError('Save your name, email and password before starting signup.')
    if account_id in adapter.logins and not resume:
        raise ValueError('Close the existing account browser first.')
    if account_id not in adapter.logins and len(adapter.logins) >= min(5, (store.get('settings', 'settings') or {}).get('max_running_tasks', 10)):
        raise ValueError('Finish or close another account browser first.')
    existing=adapter.logins.get(account_id) if resume else None
    if resume and not old:
        raise ValueError('Start signup for this account before resuming it.')
    if not existing and engine.browser_slots.locked():
        await engine.group_coordinator.pool.trim(all_idle=True)
        await engine.evict_ready()
    if not existing and engine.browser_slots.locked():
        raise ValueError('All browser slots are busy. Stop a task or close a browser first.')
    if not existing:await engine.browser_slots.acquire()
    released = bool(existing)
    def release(*_):
        nonlocal released
        if not released:
            released = True
            engine.browser_slots.release()
    def closed(*_):
        watcher=adapter.login_watchers.get(account_id)
        if watcher and watcher is not asyncio.current_task():
            watcher.cancel()
        owner = getattr(context, '_retail_close_task', None)
        if owner and not owner.done():
            owner.add_done_callback(release)
        else:
            release()
    def update(status, message, **extra):
        current = store.get('account_creation', key) or {}
        store.put('account_creation', {**current, 'account_id': account_id, 'status': status,
                  'message': message, 'updated_at': now(), **extra}, key)

    context = page = controls = None
    startup_stage='Preparing signup'
    def progress(message):
        nonlocal startup_stage
        startup_stage=message
        update('opening',message)
    try:
        AccountBrowserProfiles(store).get(account)
        previous=getattr(adapter,'login_watchers',{}).pop(account_id,None)
        if previous:
            previous.cancel()
            await asyncio.gather(previous,return_exceptions=True)
        update('opening', 'Checking the current account state.' if resume else 'Opening a visible signup browser.', startup_error='', startup_stage='', registration_error_recheck_attempted=False, **({} if resume else {'submitted':False}))
        async with asyncio.timeout(90):
            connection={}
            if not existing and not account.get('proxy') and account.get('proxy_list_id'):
                connection['proxy']=await engine.proxy_health.ready_route(account['proxy_list_id'],account_id,progress=progress)
            context = existing or await adapter.context(account if resume else {**account, 'session': None, 'session_storage': {}},
                                            interactive=True, headless=False, progress=progress, **connection)
            if not existing:context.on('close', closed)
            adapter.logins[account_id] = context
            context._retail_paced_input = True
            context._retail_signup_authorized = True
            if resume:
                context._retail_signin_signup_attempted = False
                context._retail_signup_signin_attempted = False
            if account.get('sms_provider_id'):
                provider=store.get('sms_providers',account['sms_provider_id'])
                if not provider:raise SMSError('The selected SMS connection no longer exists.')
                context._retail_signup_sms=SignupSMS(store,account,provider)
            pages=[p for p in context.pages if not p.is_closed() and urlparse(p.url).netloc==DOMAINS[account['region']]]
            if existing and len(pages)>1:
                raise ValueError('Multiple account pages are open. Keep one signup page before resuming.')
            page = pages[-1] if existing and pages else await context.new_page()
            lifecycle.observe_signup_responses(page,store,account_id)
            await ArkoseHandoff.install(page,DOMAINS[account['region']],store.get('solvers',account.get('solver_id','')) or {})
            await adapter.expose(page)
            domain = DOMAINS[account['region']]
            controls=SignupControls(page,domain,update)
            if resume:
                # Reconcile through this account's identifier before permitting a
                # fresh registration. A previous submit remains guarded meanwhile.
                await adapter.navigate(page,f'https://{domain}/gp/your-account/order-history',wait_until='domcontentloaded')
                await adapter.authenticate(page,account)
            else:
                await open_signup(adapter,account,page,controls,update)
                await complete_signup_form(adapter,account,page,controls,update)
    except SMSError as exc:
        update('sms_blocked',str(exc))
    except asyncio.CancelledError:
        update('review', 'Signup interrupted. Review the browser before continuing.')
        if context:
            await context.close()
            adapter.logins.pop(account_id, None)
        raise
    except (BackoffRequired,AccessDenied) as exc:
        controls=None
        update('review','Amazon requested a pause. No automatic retry.',retry_at=time.time()+max(60,getattr(exc,'retry_after_seconds',None) or 60))
    except Exception as exc:
        if page is None:
            # Never expose raw browser exceptions: launch arguments can contain
            # profile paths, proxy credentials and extension configuration.
            code='browser_startup_failed'
            message=f'The signup browser could not open during: {startup_stage}. Check browser settings and try again.'
            if isinstance(exc,ProxySelectionError):
                code,message=exc.code,str(exc)
            elif isinstance(exc,TimeoutError):
                code='browser_startup_timeout'
                message=f'Signup startup timed out during: {startup_stage}. Check the connection and browser settings before retrying.'
            update('startup_failed',message,startup_error=code,startup_stage=startup_stage)
            if context:
                await context.close()
            adapter.logins.pop(account_id, None)
            raise ValueError(message) from None
        try:current=await signup_state(page)
        except Exception:current={}
        if current.get('captcha'):
            update('captcha_loading','Verification detected. Preparing the assigned solver.')
        else:
            update('resuming', 'Checking the current signup step before continuing.')
    finally:
        if context is None:
            release()
        if controls is not None and not page.is_closed() and adapter.logins.get(account_id) is context:
            adapter.login_watchers[account_id] = asyncio.create_task(verify_signup(engine, account, context, page, controls, update))
    return {'ok': True, 'message': (store.get('account_creation', key) or {})['message']}
