"""Retailer-independent recovery contracts; deterministic execution owns transactions."""
import asyncio
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import re
import time
from urllib.parse import urlsplit

from .devtools_recovery import ScopedMCP, MCPBrowserTools
from .interactions import InteractionError
from .store import now


class FailureKind(StrEnum):
    MISSING='missing_control'
    AMBIGUOUS='ambiguous_control'
    STRUCTURE='changed_structure'
    NAVIGATION='unexpected_navigation'
    TRANSITION='invalid_transition'
    DIALOG='unexpected_dialog'
    SESSION='expired_session'
    NETWORK='network_failure'
    VERIFICATION='verification_required'
    UNCERTAIN='uncertain_mutation'


@dataclass(frozen=True)
class RecoveryRequest:
    action: str
    domains: frozenset[str]
    policies: dict[str,str]
    failure: FailureKind = FailureKind.MISSING
    mutation_started: bool = False
    deadline_seconds: float = 15

    def validate(self,page):
        if self.mutation_started or self.failure in {FailureKind.UNCERTAIN,FailureKind.VERIFICATION,FailureKind.SESSION} or self.action=='SUBMIT_ORDER':
            raise InteractionError('This state requires verification or reconciliation; recovery cannot replay its action')
        if self.action not in self.policies or not re.fullmatch(r'[A-Z_]{2,40}',self.action):
            raise InteractionError('The workflow has not authorized this action')
        origin=urlsplit(page.url)
        if origin.scheme!='https' or origin.hostname not in self.domains:
            raise InteractionError('The workflow left its authorized origin')


async def inspect_recovery(agent,page,request, *, execute=False, verify=None):
    """One model proposal; optional reversible dispatch and explicit outcome verification.

    Purchase adapters use the returned handle with their own durable mutation guard.
    Reversible workflows can dispatch through MCP here with a trusted verify callback.
    """
    request.validate(page)
    if execute and verify is None:raise InteractionError('Recovery requires an expected outcome')
    started=time.monotonic()
    attempted=getattr(page,'_retail_mcp_attempts',set())
    key=(page.url,request.action)
    if key in attempted:raise InteractionError('Recovery already attempted this checkpoint')
    attempted.add(key);page._retail_mcp_attempts=attempted
    # Browser-side validation is local and never forwarded as arbitrary page text.
    blocked=await page.evaluate("""() => !!document.querySelector('#captchacharacters,#auth-mfa-otpcode,input[autocomplete=one-time-code],iframe[src*=arkoselabs],iframe[src*=captcha]')""")
    if blocked:raise InteractionError('Verification requires the authorized user')
    outcome='failed';label=None
    try:
        async with asyncio.timeout(request.deadline_seconds), agent.recovery_slots, ScopedMCP(page) as scope:
            browser=MCPBrowserTools(page,request.action,request.domains,request.policies,scope)
            chosen=await agent._run(page,browser,request.action,('observe_controls','inspect_accessibility','validate_control'))
            ref=next(ref for ref,(node,meta) in browser.nodes.items() if node==chosen)
            metadata=browser.nodes[ref][1];label=metadata['label']
            if execute:
                await browser.click_validated(ref)
                if not await verify(page):raise InteractionError('Recovery action has no verified outcome; it will not be repeated')
            outcome='recovered' if execute else 'validated'
            return chosen
    finally:
        host=urlsplit(page.url).hostname
        pattern_key=hashlib.sha256(f'{host}|{request.action}|{request.failure}|{label or chr(0)}'.encode()).hexdigest()[:24]
        previous=agent.store.get('workflow_incidents',pattern_key) or {}
        value={**previous,'action':request.action,'failure':str(request.failure),'host':host,
            'status':outcome,'count':previous.get('count',0)+1,'at':now(),
            'recovery_ms':round((time.monotonic()-started)*1000,2),'transport':'chrome-devtools-mcp'}
        # Only a policy-approved label may become repair evidence. No DOM or user text.
        if label and re.fullmatch(request.policies[request.action],label,re.I):value['control_label']=label
        agent.store.put_bounded('workflow_incidents',value,pattern_key)
        if outcome=='validated':
            pending=getattr(page,'_retail_pending_repairs',{})
            pending[request.action]=(pattern_key,value)
            page._retail_pending_repairs=pending
        if outcome=='recovered':
            enqueue_repair(agent.store,value,pattern_key)


def enqueue_repair(store,incident,key):
    """Deduplicate evidence ready for the separate offline repair worker."""
    old=store.get('repair_queue','repair-'+key)
    if old:return old
    return store.put_bounded('repair_queue',{'incident_id':key,'status':'pending','at':now(),
        'action':incident['action'],'host':incident['host'],'control_label':incident.get('control_label',''),
        'failure':incident['failure'],'attempts':0},'repair-'+key)


def confirm_recovery(store,page,actions):
    pending=getattr(page,'_retail_pending_repairs',{})
    for action in actions:
        if action in pending:
            key,value=pending.pop(action)
            value={**value,'status':'recovered','verified_at':now()}
            store.put_bounded('workflow_incidents',value,key)
            enqueue_repair(store,value,key)
