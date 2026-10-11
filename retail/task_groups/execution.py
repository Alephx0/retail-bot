"""One account-specific attempt. No automatic replay after a mutating boundary."""
import asyncio
import uuid
import time
from ..browser_recovery import execution_deadline

from ..amazon import Attention
from ..models import DOMAINS
from .domain import cents, qualifying, effective_plan
from .repository import Conflict, MUTATED


class Executor:
    def __init__(self, coordinator):
        self.c=coordinator; self.repo=coordinator.repo

    async def gate(self, attempt_id):
        while True:
            attempt=self.repo.require('attempt',attempt_id)
            run=self.repo.require('run',attempt['run_id'])
            if run['state']=='paused' or self.repo.member_state(attempt['run_id'],attempt['account_id'])['state']=='paused':
                await asyncio.sleep(.2)
                continue
            return self.repo.gate(attempt_id)

    async def wait_user(self, attempt_id, page, message):
        self.repo.stage(attempt_id,'waiting_user',message)
        event=self.c.wakes.setdefault(attempt_id,asyncio.Event()); event.clear()
        try:
            await self.c.engine.amazon.expose(page)
        except Exception:
            pass
        await event.wait()

    async def run(self, attempt):
        plan=effective_plan(self.repo.require('run',attempt['run_id'])['plan'],attempt['account_id'])
        token = execution_deadline.set(time.monotonic()+plan.get('checkout_timeout_seconds',300))
        try:
            await asyncio.wait_for(self.execute(attempt,plan),plan.get('checkout_timeout_seconds',300))
        except TimeoutError:
            # execute's cancellation handler records a safe or uncertain outcome before returning.
            return
        finally:
            execution_deadline.reset(token)

    async def execute(self, attempt, plan):
        id=attempt['id']; run=self.repo.require('run',attempt['run_id'])
        target=next(p for p in plan['products'] if p['product_id']==attempt['product_id'])
        try:
            await self.gate(id)
            if plan['simulation']:
                self.repo.stage(id,'preparing','Simulating account preparation; no live connection')
                await asyncio.sleep(.15)
                await self.gate(id)
                total=cents(attempt['observation']['price'])*attempt['units']
                if total>attempt['money_cents']:
                    self.repo.finish(id,'rejected','Simulated total exceeds the order allowance'); return
                if plan['action']=='quote':
                    self.repo.stage(id,'reviewing','Simulating checkout total')
                    self.repo.finish(id,'quoted',f'Simulated checkout total: {total/100:.2f}'); return
                snapshot={'asin':attempt['product_id'],'quantity':attempt['units'],'total':total/100,
                          'currency':{'US':'USD','UK':'GBP','CA':'CAD'}[plan['region']]}
                if plan['action']=='review':
                    await self.wait_user(id,None,'Simulation ready for review. Check outcome to confirm the simulated order; no browser or purchase is involved.')
                    await self.gate(id)
                self.repo.intent(id,snapshot)
                self.repo.finish(id,'confirmed','Simulated order; no purchase placed','SIM-'+uuid.uuid4().hex,total)
                confirmed=self.repo.require('attempt',id)
                self.c.record_checkout(id,attempt['observation'],snapshot,confirmed['order_id'])
                return
            account=self.c.engine.store.get('accounts',attempt['account_id'])
            if not account:
                raise ValueError('Account no longer exists')
            if account.get('region','US')!=plan['region'] or account.get('retailer','amazon')!=plan['retailer']:
                raise Conflict('Account no longer matches this retailer and region')
            self.c.validate_cooldown(account)
            async with self.c.pool.lease(account,id) as session:
                page=session['page']; adapter=self.c.engine.amazon
                self.c.pages[id]=page
                page._retail_agent_attempts=set()
                page._retail_recovered_controls={}
                page._retail_submit_control=None
                page._retail_review=None
                page._retail_submit_gate=lambda:self.repo.submit_gate(id)
                page._retail_task_id=id
                session['context']._retail_task_id=id
                page._retail_recovery_deadline = execution_deadline.get() or float('inf')
                if hasattr(adapter, 'recovery'):
                    await adapter.recovery.install(page)
                self.repo.stage(id,'preparing','Verifying account session and offer')
                page._retail_start_url = f"https://{DOMAINS[plan['region']]}/dp/{target['product_id']}"
                await adapter.ensure_session(session['context'],account,page)
                item={'asin':target['product_id'],'max_price':target['max_unit_cents']/100,'offer_id':target['offer_id']}
                product=await adapter.inspect(page,item,plan['region'])
                page._retail_product_condition=product.get('condition','')
                ok,reason,message=qualifying(product,target,plan)
                if not ok:
                    self.repo.finish(id,'rejected','Fresh account check: '+message); return
                if cents(product['price'])*attempt['units']>attempt['money_cents']:
                    self.repo.finish(id,'rejected','Requested quantity exceeds the order allowance'); return
                await self.gate(id)
                self.repo.stage(id,'carting','Adding the verified product to the account cart')
                quantity=await adapter.cart(page,attempt['units'],target['product_id'])
                if quantity!=attempt['units']:
                    raise Conflict('Cart quantity differs from the reserved quantity')
                await self.gate(id)
                self.repo.stage(id,'reviewing','Checking exact cart and final order total')
                await adapter.prepare_checkout(page,item['asin'],quantity)
                snapshot=await adapter.checkout_snapshot(page,item['asin'],quantity,plan['max_order_cents']/100,
                    max_unit_price=target['max_unit_cents']/100,allow_third_party=plan['allow_third_party'],allow_used=plan['allow_used'])
                if plan['action']=='quote':
                    self.repo.finish(id,'quoted',f"Verified checkout total {snapshot['total']:.2f}; no order submitted",
                                     evidence='Exact target cart verified; quote did not submit an order')
                    return
                if plan['action']=='review':
                    self.repo.stage(id,'reviewing','Checkout snapshot verified',snapshot=snapshot)
                    await self.wait_user(id,page,'Review the checkout in the browser. Complete manually, then Check outcome. No automatic submission.')
                else:
                    await self.gate(id)
                    # submit_order revalidates its saved checkout snapshot again.
                    self.repo.intent(id,snapshot)
                    await adapter.submit_order(page)
                order_id=await adapter.confirmation(page)
                if not order_id:
                    raise Conflict('Order confirmation is not verified')
                if await adapter.payment_verification(page):
                    raise Conflict('Order confirmation found but payment verification remains outstanding')
                # Review-mode totals cannot be assumed unchanged after manual edits.
                if plan['action']=='review':
                    self.repo.finish(id,'reconciliation_required','Order found; record its verified final total before releasing the reservation')
                    return
                self.repo.finish(id,'confirmed','Retailer order confirmation verified',order_id,cents(snapshot['total']))
                self.c.record_checkout(id,product,snapshot,order_id)
        except asyncio.CancelledError:
            current=self.repo.require('attempt',id)
            if current['state'] in ('confirmed','quoted','cancelled','rejected','failed'):
                return
            self.repo.finish(id,'reconciliation_required' if current['state'] in MUTATED else 'cancelled',
                             'Execution interrupted; uncertain cart or order changes are not retried')
            raise
        except Exception as exc:
            current=self.repo.require('attempt',id)
            if current['state'] in ('confirmed','quoted','cancelled','rejected','failed'):
                self.c.engine.store.event(id,'recording_error','Attempt outcome is durable; supplemental history could not be updated')
                return
            safe=isinstance(exc,(ValueError,Attention))
            self.repo.finish(id,'reconciliation_required' if current['state'] in MUTATED else 'failed',
                             str(exc) if safe else 'Browser operation failed; inspect account and diagnostics')
            if current['state'] not in MUTATED and not isinstance(exc,Conflict):
                import time
                self.c.cooldowns[(attempt['run_id'],attempt['account_id'])]=time.monotonic()+30
        finally:
            self.c.pages.pop(id,None); self.c.wakes.pop(id,None)
