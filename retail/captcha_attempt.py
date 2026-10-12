"""Durable paid-request phases, separate from retailer submission intent."""
import asyncio
from .services import SolverError
from .store import now


class CaptchaAttempt:
    def __init__(self, store, account_id, provider, kind, update, document):
        self.store, self.update = store, update
        self.record=store.put('captcha_attempts',dict(account_id=account_id,provider=provider,
            kind=kind,status='requesting',updated_at=now(),document=document))
        update('captcha_solving',f'Requesting {provider} to solve the verification challenge.',
               captcha_attempted=True,captcha_submitted=False,captcha_phase='requesting',
               captcha_error='',captcha_document=document,captcha_type=kind)

    def progress(self, phase, **private):
        self.record=self.store.put('captcha_attempts',{**self.record,**private,'status':phase,'updated_at':now()})
        messages={'provider_processing':'CapMonster is processing the challenge.' if self.record['provider']=='capmonster' else 'The assigned service is processing the challenge.',
                  'solution_received':'Solution received; validating the current verification form.',
                  'submitting':'Submitting the solution to the retailer.',
                  'submitted':'Solution submitted; waiting for the retailer to accept it.'}
        extra={'captcha_submission_intent':True} if phase=='submitting' else {'captcha_submitted':True} if phase=='submitted' else {}
        self.update('captcha_solving',messages[phase],captcha_phase=phase,**extra)

    def failed(self, error):
        code=(error.code if isinstance(error,SolverError) else 'CANCELLED' if isinstance(error,asyncio.CancelledError)
              else 'SOLVER_TIMEOUT' if isinstance(error,TimeoutError) else 'SOLUTION_NOT_APPLIED')
        self.record=self.store.put('captcha_attempts',{**self.record,'status':'failed','error_code':code,'updated_at':now()})
        # The watcher reads this durable detail on subsequent iterations.
        self.update('captcha_manual',f'{self.record["provider"]} could not complete verification ({code}). No automatic paid retry.',
                    captcha_phase='failed',captcha_error=code)


def paused_message(record, label, provider):
    code=record.get('captcha_error')
    if code:
        return f'{label} — {provider} failed ({code}). No automatic paid retry. Complete the open puzzle to resume.'
    phase=record.get('captcha_phase')
    if phase in ('requesting','provider_processing','solution_received'):
        return f'{label} — the previous solve was interrupted before confirmed submission. No duplicate paid request was sent.'
    if phase in ('submitting','submitted'):
        return f'{label} — a solution was submitted, but retailer acceptance is unconfirmed. It will not be submitted again.'
    return f'{label} — an earlier automatic attempt was recorded without its outcome. No duplicate paid request was sent.'
