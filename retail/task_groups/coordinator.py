"""Local group scheduler, account-scoped monitoring and bounded checkout dispatch."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

from ..account_consistency import PurchaseCooldown
from ..amazon import Attention, BackoffRequired
from ..store import now
from .browser_pool import BrowserPool
from .domain import Plan, qualifying, windows
from .execution import Executor
from .repository import ACTIVE_RUNS, Conflict, Repository


class Coordinator:
    def __init__(self, engine, clock=None):
        self.engine=engine; self.repo=Repository(engine.store)
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.pool=BrowserPool(engine,self.repo); self.executor=Executor(self)
        self.scans={}; self.jobs={}; self.pages={}; self.wakes={}; self.cache={}
        self.next_scan={}; self.cooldowns={}; self.read_errors={}; self.notifications=set()
        self.scheduler=None; self.closing=False

    async def boot(self):
        self.repo.recover()
        for attempt in self.repo.all('attempt'):
            if attempt['state']=='confirmed' and not attempt['simulation'] and not self.engine.store.get('checkouts','group-checkout-'+attempt['id']):
                plan=self.repo.require('run',attempt['run_id'])['plan']
                self.record_checkout(attempt['id'],attempt.get('observation',{}),
                    {'total':attempt['money_cents']/100,'currency':{'US':'USD','UK':'GBP','CA':'CAD'}[plan['region']]},attempt['order_id'])
        self.scheduler=asyncio.create_task(self.loop())

    def active(self):
        return [r for r in self.repo.all('run') if r['state'] in ACTIVE_RUNS]

    def busy_account(self, account_id):
        return bool(self.repo.claimed(account_id) or self.engine.account_locks.get(account_id,asyncio.Lock()).locked())

    def validate_cooldown(self, account):
        until=PurchaseCooldown(self.engine.store).eligible_at(account['id'],account.get('purchase_cooldown_days',0),self.clock())
        days=account.get('purchase_cooldown_days',0)
        if days:
            for a in self.repo.all('attempt'):
                if a['state']=='confirmed' and not a['simulation'] and a['account_id']==account['id']:
                    until=max(until,datetime.fromisoformat(a['updated_at'])+timedelta(days=days))
        if until>self.clock():
            raise Conflict('Account purchase cooldown until '+until.isoformat())

    def readiness(self, plan):
        plan=Plan.model_validate(plan).model_dump(mode='json'); errors=[]; accounts=[]
        for id in plan['account_ids']:
            a=self.engine.store.get('accounts',id)
            reason='Ready'
            if not a:
                reason='Account no longer exists'; errors.append(reason)
            elif a.get('retailer','amazon')!=plan['retailer'] or a.get('region','US')!=plan['region']:
                reason='Account retailer or region does not match'; errors.append(reason)
            elif not plan['simulation']:
                if self.busy_account(id):
                    reason='Busy in another browser or checkout'
                elif id in self.engine.amazon.logins or id in self.engine.amazon.fingerprint_tests:
                    reason='Close the account browser before starting'
                elif not (a.get('session') or a.get('password')):
                    reason='Sign-in required'
                elif self.cooldowns.get(id,0)>time.monotonic():
                    reason='Cooling down or verification required; inspect the account, then recheck'
                else:
                    try:
                        self.validate_cooldown(a)
                    except Conflict as exc:
                        reason=str(exc)
            accounts.append({'id':id,'name':a.get('name','Missing') if a else 'Missing','ready':reason=='Ready','reason':reason})
        if not plan['simulation'] and not any(a['ready'] for a in accounts):
            errors.append('No account is ready; sign in, free an account, or wait for its cooldown')
        upcoming=windows(plan['schedule'],self.clock())
        next_window=next((w for w in upcoming if w['end']>self.clock().isoformat()),None)
        if plan['schedule']['kind']!='manual' and next_window is None:
            errors.append('No valid upcoming window; check date, weekdays and daylight-saving changes')
        return {'plan':plan,'accounts':accounts,'errors':list(dict.fromkeys(errors)), 'next_window':next_window,
                'target_units':Plan.model_validate(plan).desired_units,'currency':{'US':'USD','UK':'GBP','CA':'CAD'}[plan['region']]}

    def start(self, group_id, key):
        if self.closing or self.engine.stopping_all:
            raise Conflict('The engine is stopping')
        group=self.repo.require('group',group_id)
        command=self.repo.db.execute('SELECT run_id FROM tg_commands WHERE command_key=?',(group_id+':'+key,)).fetchone()
        if command:
            return self.repo.require('run',command[0])
        preview=self.readiness(self.repo.require('revision',group['revision_id'])['plan'])
        if preview['errors']:
            raise Conflict('; '.join(preview['errors']))
        return self.repo.start(group_id,key,self.clock())

    async def command(self, run_id, action):
        run=self.repo.command(run_id,action)
        if action in ('pause','stop'):
            scan=self.scans.get(run_id)
            if scan:
                scan.cancel(); await asyncio.gather(scan,return_exceptions=True)
        if action=='stop':
            jobs=[job for id,job in self.jobs.items() if self.repo.require('attempt',id)['run_id']==run_id]
            for job in jobs:
                job.cancel()
            if jobs:
                await asyncio.gather(*jobs,return_exceptions=True)
            self.repo.run_state(run_id,'stopped','Stopped; any uncertain outcomes remain reserved for reconciliation')
            await self.pool.trim(all_idle=True)
        return self.repo.require('run',run_id)

    async def stop_all(self):
        runs={run['id']:run for run in self.active()}
        for group in self.repo.all('group'):
            if group.get('armed'):
                run=self.repo.get('run',group.get('active_run_id',''))
                if run:
                    runs[run['id']]=run
        for run in runs.values():
            await self.command(run['id'],'stop')
        return len(runs)

    async def loop(self):
        while not self.closing:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad group cannot stop the engine's scheduler.
                self.engine.store.event('task-groups','scheduler_error','Group scheduler recovered from an error; inspect group readiness')
            await asyncio.sleep(.25)

    async def tick(self):
        current=self.clock(); timestamp=current.isoformat()
        for run in self.repo.all('run'):
            if run['state'] not in ACTIVE_RUNS:
                continue
            window=run['window']
            if window and timestamp>=window['end']:
                group=self.repo.require('group',run['group_id']); armed=group.get('armed',False)
                await self.command(run['id'],'stop')
                if armed and run['plan']['schedule']['kind']=='weekly':
                    try:
                        self.repo.start(group['id'],'window-after-'+window['key'],current)
                    except Conflict:
                        pass
                continue
            if run['state']=='scheduled' and timestamp>=window['prepare']:
                run=self.repo.run_state(run['id'],'preparing','Preparing account sessions before the execution window')
            if run['state']=='preparing' and timestamp>=window['start']:
                run=self.repo.run_state(run['id'],'watching','Execution window is open')
            if run['state'] in ('watching','preparing') and time.monotonic()>=self.next_scan.get(run['id'],0):
                previous=self.scans.get(run['id'])
                if previous is None or previous.done():
                    self.next_scan[run['id']]=time.monotonic()+run['plan']['monitor_interval_ms']/1000
                    self.scans[run['id']]=asyncio.create_task(self.scan(run))
        # Schedule a subsequent weekly window after a per-window goal completes.
        for group in self.repo.all('group'):
            run=self.repo.get('run',group.get('active_run_id',''))
            if group.get('armed') and run and run['state']=='completed' and run['plan']['schedule']['kind']=='weekly' and run['plan']['schedule']['quota_scope']=='window' and timestamp>=run['window']['end']:
                try:
                    self.repo.start(group['id'],'window-after-'+run['window']['key'],current)
                except Conflict:
                    pass
        await self.pool.trim(all_idle=bool(self.engine.jobs))

    async def scan(self, run):
        plan=run['plan']; semaphore=asyncio.Semaphore(plan['monitor_concurrency'])
        async def check(account_id,target):
            async with semaphore:
                current=self.repo.require('run',run['id'])
                if current['state'] not in ('watching','preparing'):
                    return
                await self.inspect(current,account_id,target)
        roster=plan['account_ids'] or ['simulation']
        # Rotate initial account priority each cycle while retaining product priority.
        offset=int(time.monotonic()/(plan['monitor_interval_ms']/1000))%len(roster)
        roster=roster[offset:]+roster[:offset]
        await asyncio.gather(*(check(a,t) for t in plan['products'] for a in roster))

    async def inspect(self, run, account_id, target):
        plan=run['plan']; product=None; key=(account_id,target['product_id'],target['offer_id'],plan['region'],plan['simulation'])
        reason='unknown'; message='Observation unavailable'
        try:
            if self.cooldowns.get(account_id,0)>time.monotonic():
                raise Conflict('Account is cooling down or needs attention')
            if plan['simulation']:
                product={'asin':target['product_id'],'title':target['label'] or 'Simulation product',
                         'price':0 if plan.get('only_freebies') else min(29.99,target['max_unit_cents']/100),'original_price':100,'available':True,'amazon_seller':True,
                         'seller':'Amazon (simulation)','condition':'new','offer_id':target['offer_id']}
            else:
                account=self.engine.store.get('accounts',account_id)
                if not account:
                    raise Conflict('Account no longer exists')
                if account.get('region','US')!=plan['region'] or account.get('retailer','amazon')!=plan['retailer']:
                    raise Conflict('Account no longer matches this retailer and region')
                self.validate_cooldown(account)
                if not (account.get('session') or account.get('password')):
                    raise Conflict('Sign-in required')
                cache=self.cache.get(key)
                if cache and time.monotonic()-cache[0]<plan['monitor_interval_ms']/1000 and run['state']=='watching':
                    product=cache[1]
                else:
                    async with self.pool.lease(account) as session:
                        cache=self.cache.get(key)
                        if cache and time.monotonic()-cache[0]<plan['monitor_interval_ms']/1000 and run['state']=='watching':
                            product=cache[1]
                        else:
                            if time.monotonic()-session.get('authenticated_at',0)>60:
                                await self.engine.amazon.ensure_session(session['context'],account,session['page'])
                                session['authenticated_at']=time.monotonic()
                            if run['state']=='preparing':
                                reason,message='prepared','Account session prepared; waiting for the window'
                            else:
                                item={'asin':target['product_id'],'max_price':target['max_unit_cents']/100,'offer_id':target['offer_id']}
                                product=await self.engine.amazon.inspect(session['page'],item,plan['region'])
                                self.cache[key]=(time.monotonic(),product)
                                self.read_errors[account_id]=0
            if run['state']=='preparing':
                reason,message='prepared','Account session prepared; waiting for the window'
            elif product:
                eligible,reason,message=qualifying(product,target,plan)
                if eligible and plan['action']=='notify':
                    notification=(run['id'],account_id,target['product_id'],product.get('price'),product.get('offer_id'))
                    if notification not in self.notifications:
                        self.notifications.add(notification)
                        with self.repo.transaction():
                            self.repo.event(run['id'],'matching_stock','Matching stock found; notifications only, no checkout',product_id=target['product_id'])
                elif eligible:
                    if not plan['simulation']:
                        if self.engine.account_locks.get(account_id,asyncio.Lock()).locked():
                            raise Conflict('Waiting for this account browser to finish its read')
                        if any((self.engine.store.get('tasks',id) or {}).get('account_id')==account_id for id in self.engine.jobs):
                            raise Conflict('Waiting for this account legacy task')
                    running=sum(not job.done() and self.repo.require('attempt',id)['run_id']==run['id'] for id,job in self.jobs.items())
                    if running>=plan['max_parallel_checkouts']:
                        raise Conflict('Waiting for checkout capacity')
                    latest=self.repo.require('run',run['id'])
                    attempt=self.repo.reserve(run['id'],account_id,target['product_id'],latest['generation'],product)
                    self.jobs[attempt['id']]=asyncio.create_task(self.executor.run(attempt))
                    self.jobs[attempt['id']].add_done_callback(lambda _job,id=attempt['id']:self.jobs.pop(id,None))
                    reason,message='reserved','Quantity and order allowance reserved for checkout'
        except BackoffRequired as exc:
            self.cooldowns[account_id]=time.monotonic()+max(30,exc.retry_after_seconds or 0)
            reason,message='backoff','Retailer requested a cooldown before the next read-only check'
        except Attention:
            self.cooldowns[account_id]=float('inf')
            reason,message='attention','Account verification required; open the account, then recheck readiness'
        except Conflict as exc:
            reason,message='waiting',str(exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            errors=self.read_errors.get(account_id,0)+1; self.read_errors[account_id]=errors
            self.cooldowns[account_id]=float('inf') if errors>=plan['max_read_errors'] else time.monotonic()+min(120,5*2**errors)
            reason,message='read_error','Read-only browser check failed; cooldown applied'+('; recheck readiness to resume' if errors>=plan['max_read_errors'] else '')
        self.repo.observe(run['id'],{'account_id':account_id,'product_id':target['product_id'],'at':now(),
            'price':product.get('price') if product else None,'available':product.get('available') if product else None,
            'reason':reason,'message':message})

    def record_checkout(self, attempt_id, product, snapshot, order_id):
        a=self.repo.require('attempt',attempt_id); plan=self.repo.require('run',a['run_id'])['plan']
        self.engine.store.put('checkouts',{'task_id':attempt_id,'group_run_id':a['run_id'],'account_id':a['account_id'],
            'retailer':plan['retailer'],'asin':a['product_id'],'title':product.get('title',a['product_id']),
            'quantity':a['units'],'total':snapshot['total'],'currency':snapshot['currency'],'order_id':order_id,
            'simulation':False,'status':'confirmation_detected','at':a['updated_at']},'group-checkout-'+attempt_id)

    def summary(self, group):
        plan=self.repo.require('revision',group['revision_id'])['plan']
        run=self.repo.get('run',group.get('active_run_id',''))
        return {**group,'plan':plan,'run':run,'progress':self.repo.progress(run) if run else
                {'confirmed_units':0,'reserved_units':0,'spent_cents':0,'reserved_cents':0}}

    async def close(self):
        self.closing=True
        if self.scheduler:
            self.scheduler.cancel(); await asyncio.gather(self.scheduler,return_exceptions=True)
        for scan in self.scans.values():
            scan.cancel()
        await asyncio.gather(*self.scans.values(),return_exceptions=True)
        for job in list(self.jobs.values()):
            job.cancel()
        await asyncio.gather(*list(self.jobs.values()),return_exceptions=True)
        # Preserve a resumable paused run rather than silently rearming after restart.
        for run in self.active():
            if run['state']!='paused':
                self.repo.command(run['id'],'pause')
        await self.pool.close(); self.repo.close()
