"""Explicit commands and compact read models for the group workspace."""
import asyncio
import json
import sqlite3
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..models import inputs
from .domain import Plan, cents, enabled_accounts, effective_plan
from .repository import Conflict

router=APIRouter()


def service(request):
    return request.app.state.group_coordinator


def public_attempt(a):
    return {k:v for k,v in a.items() if k not in ('intent','snapshot','observation','claim_key','browser_profile')}


def invalid(exc):
    return HTTPException(409 if isinstance(exc,Conflict) else 422,str(exc))


@router.get('/api/task-groups')
async def groups(request:Request):
    c=service(request)
    return {'groups':[c.summary(g) for g in c.repo.all('group')]}


@router.post('/api/task-groups/preview')
async def preview(request:Request):
    try:
        return service(request).readiness(await request.json())
    except ValueError as exc:
        raise invalid(exc) from exc


@router.post('/api/task-groups')
async def create(request:Request):
    c=service(request)
    try:
        data=await request.json(); preview=c.readiness(data)
        # Unready accounts may be saved; reference errors must be corrected.
        if any(not c.engine.store.get('accounts',id) for id in preview['plan']['account_ids']):
            raise ValueError('Every selected account must exist')
        return c.summary(c.repo.save_plan(data))
    except ValueError as exc:
        raise invalid(exc) from exc


@router.patch('/api/task-groups/{id}')
async def update(id:str,request:Request):
    c=service(request)
    try:
        data=await request.json(); preview=c.readiness(data['plan'])
        if any(not c.engine.store.get('accounts',id) for id in preview['plan']['account_ids']):
            raise ValueError('Every selected account must exist')
        return c.summary(c.repo.save_plan(data['plan'],id,data.get('revision_id')))
    except (ValueError,KeyError) as exc:
        raise invalid(exc) from exc


@router.post('/api/task-groups/{id}/duplicate')
async def duplicate(id:str,request:Request):
    c=service(request)
    try:
        group=c.repo.require('group',id); plan=c.repo.require('revision',group['revision_id'])['plan']
        plan['name']=(plan['name']+' copy')[:100]
        plan['simulation']=True
        return c.summary(c.repo.save_plan(plan))
    except ValueError as exc:
        raise invalid(exc) from exc


@router.get('/api/task-groups/{id}')
async def detail(id:str,request:Request):
    c=service(request)
    try:
        result=c.summary(c.repo.require('group',id))
        run=c.repo.require('run',result['run']['id']) if result['run'] else None
        result['readiness']=c.readiness(result['plan'])
        result['members']=[c.repo.member_state(run['id'],a) for a in enabled_accounts(run['plan'])] if run else []
        result['observations']=c.repo.all('observation',run['id']) if run else []
        history=c.repo.group_attempts(id,limit=100)
        recent={a['id']:a for a in history[-100:]}
        recent.update({a['id']:a for a in history if a['state']=='reconciliation_required'})
        result['attempts']=[public_attempt(a) for a in recent.values()]
        result['events']=c.repo.group_events(id)
        result['runs']=[{k:r[k] for k in ('id','state','created_at','revision_id')} for r in c.repo.all('run',id,limit=50)]
        return result
    except ValueError as exc:
        raise HTTPException(404,str(exc)) from exc


@router.post('/api/task-groups/{id}/runs')
async def start(id:str,request:Request):
    try:
        data=await request.json(); key=data.get('idempotency_key','')
        if not isinstance(key,str) or not 1<=len(key)<=128:
            raise ValueError('Supply an idempotency key')
        return service(request).start(id,key)
    except ValueError as exc:
        raise invalid(exc) from exc


@router.post('/api/task-groups/{id}/recheck')
async def recheck(id:str,request:Request):
    c=service(request)
    try:
        group=c.repo.require('group',id); plan=c.repo.require('revision',group['revision_id'])['plan']
        for account_id in plan['account_ids']:
            key=(group.get('active_run_id',''),account_id)
            c.cooldowns.pop(key,None); c.read_errors.pop(key,None)
        return c.readiness(plan)
    except ValueError as exc:
        raise invalid(exc) from exc


@router.post('/api/group-runs/{id}/accounts/{account_id}/{action}')
async def member_command(id:str,account_id:str,action:str,request:Request):
    try:
        return await service(request).member_command(id,account_id,action)
    except ValueError as exc:
        raise invalid(exc) from exc


@router.post('/api/group-runs/{id}/{action}')
async def command(id:str,action:str,request:Request):
    try:
        return await service(request).command(id,action)
    except ValueError as exc:
        raise invalid(exc) from exc


@router.get('/api/group-runs/{id}/attempts')
async def attempts(id:str,request:Request,cursor:int=0):
    c=service(request); c.repo.require('run',id)
    cursor=max(0,cursor); rows=c.repo.attempts(id)
    return {'attempts':[public_attempt(a) for a in rows[cursor:cursor+50]],
            'next_cursor':cursor+50 if len(rows)>cursor+50 else None}


@router.post('/api/group-attempts/{id}/{action}')
async def attempt_action(id:str,action:str,request:Request):
    c=service(request)
    try:
        a=c.repo.require('attempt',id)
        if action=='open':
            page=c.pages.get(id)
            if a['state']=='reconciliation_required' and (not page or page.is_closed()) and not a['simulation']:
                account=c.engine.store.get('accounts',a['account_id'])
                if not account:
                    raise Conflict('Account no longer exists')
                async with c.pool.lease(account,id) as session:
                    page=session['page']; c.pages[id]=page
                    if page.url=='about:blank':
                        from ..models import DOMAINS
                        await page.goto('https://'+DOMAINS[account['region']]+'/gp/your-account/order-history',wait_until='domcontentloaded')
            if not page:
                raise Conflict('This attempt has no open browser; use account tools to inspect the saved session')
            await c.engine.amazon.expose(page)
        elif action=='check-outcome':
            if a['state']!='waiting_user' or id not in c.wakes:
                raise Conflict('This attempt is not waiting for a manual checkout')
            if c.repo.require('run',a['run_id'])['state']!='watching':
                raise Conflict('Resume the group before checking the outcome')
            c.wakes[id].set()
        elif action=='resolve':
            if a['state']!='reconciliation_required':
                raise Conflict('Only uncertain attempts can be reconciled')
            data=await request.json(); evidence=data.get('evidence','').strip()
            if len(evidence)<12 or len(evidence)>2000:
                raise ValueError('Record how cart and order history were checked (12–2000 characters)')
            outcome=data.get('outcome')
            if outcome not in ('confirmed','no_order'):
                raise ValueError('Choose confirmed order or verified no order')
            total=data.get('total_cents')
            if outcome=='confirmed' and (data.get('product_id')!=a['product_id'] or type(data.get('units')) is not int or not 1<=data['units']<=1000):
                raise ValueError('Verify the matching product ID and actual purchased quantity')
            await c.pool.discard(a['account_id'])
            c.pages.pop(id,None)
            result=c.repo.finish(id,'confirmed' if outcome=='confirmed' else 'cancelled',
                'Outcome reconciled by the user',data.get('order_id',''),total,evidence,
                actual_units=data.get('units') if outcome=='confirmed' else None)
            if outcome=='confirmed' and not a['simulation']:
                plan=c.repo.require('run',a['run_id'])['plan']
                c.record_checkout(id,{'title':a['product_id']},{'total':total/100,'currency':{'US':'USD','UK':'GBP','CA':'CAD'}[plan['region']]},data['order_id'])
            return public_attempt(result)
        else:
            raise ValueError('Unknown attempt action')
        return {'ok':True}
    except (ValueError,KeyError) as exc:
        raise invalid(exc) from exc


@router.get('/api/group-attempts/{id}')
async def attempt_details(id:str,request:Request):
    c=service(request)
    try:
        a=c.repo.require('attempt',id)
        return {'attempt':public_attempt(a),'plan':effective_plan(c.repo.require('run',a['run_id'])['plan'],a['account_id']),
                'browser_profile':a.get('browser_profile',{}),'events':[e for e in c.repo.events(a['run_id'],limit=500) if e['attempt_id']==id]}
    except ValueError as exc:
        raise invalid(exc) from exc


@router.get('/api/group-runs/{id}/events')
async def event_stream(id:str,request:Request):
    c=service(request)
    try:
        c.repo.require('run',id)
        cursor=max(0,int(request.headers.get('last-event-id','0')))
    except ValueError as exc:
        raise invalid(exc) from exc
    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            rows=c.repo.events(id,cursor,500)
            if len(rows)==500:
                yield 'event: resync\ndata: {}\n\n'
            for event in reversed(rows):
                cursor=event['seq']
                yield f'id: {cursor}\ndata: {json.dumps(event)}\n\n'
            yield ': heartbeat\n\n'
            await asyncio.sleep(1)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache'})


@router.post('/api/task-group-migrations/{id}/preview')
async def migration_preview(id:str,request:Request):
    c=service(request); store=c.engine.store
    old=store.get('groups',id)
    if not old:
        raise HTTPException(404,'Legacy group not found')
    if old.get('task_group_id'):
        raise HTTPException(409,'This legacy group is already migrated')
    tasks=[t for t in store.all('tasks') if t['group_id']==id]
    source=store.get('input_lists',old.get('input_list_id','')) or old
    try:
        rows=inputs(source['products'])
        plan=Plan(name=old['name'],products=[{'product_id':p['asin'],'offer_id':p['offer_id'],
                    'max_unit_cents':cents(p['max_price'] if p['max_price'] is not None else old.get('max_price') or old.get('max_total',100))} for p in rows],
                    account_ids=list(dict.fromkeys(t['account_id'] for t in tasks if t.get('account_id'))),
                    max_order_cents=cents(old.get('max_total',100)),max_spend_cents=cents(old.get('max_total',100)),
                    min_unit_cents=cents(old.get('min_price',0)),
                    min_discount_percent=float(old.get('min_discount',0)) if old.get('mode')=='deals' else 0,
                    min_savings_cents=cents(old.get('min_savings',0)) if old.get('mode')=='deals' else 0,
                    only_freebies=bool(old.get('only_freebies')) if old.get('mode')=='deals' else False,
                    allow_third_party=old.get('allow_third_party',False),allow_used=old.get('allow_used',False))
        return {'plan':plan.model_dump(mode='json'),'warnings':[
            'Confirm desired units and the group spend cap. Legacy task counts are not purchase goals.',
            'Migration creates a stopped simulation plan. Review action, accounts and schedule before live use.',
            'Legacy tasks and order records remain available as history; the old group can no longer start.'],
            'legacy_tasks':len(tasks)}
    except ValueError as exc:
        raise invalid(exc) from exc


@router.post('/api/task-group-migrations/{id}/apply')
async def migration_apply(id:str,request:Request):
    c=service(request); store=c.engine.store
    old=store.get('groups',id)
    if not old:
        raise HTTPException(404,'Legacy group not found')
    if old.get('task_group_id'):
        return c.summary(c.repo.require('group',old['task_group_id']))
    tasks=[t for t in store.all('tasks') if t['group_id']==id]
    if any(t['id'] in c.engine.jobs for t in tasks):
        raise HTTPException(409,'Stop legacy tasks before migrating')
    if any(s.get('task_id') in {t['id'] for t in tasks} and s.get('status')!='confirmed' for s in store.all('submissions')):
        raise HTTPException(409,'Reconcile legacy submission records before migration')
    try:
        plan=Plan.model_validate(await request.json())
        if any(not store.get('accounts',account_id) for account_id in plan.account_ids):
            raise ValueError('Every selected account must exist')
        if not plan.simulation:
            raise ValueError('Migration starts in simulation; explicitly edit after reviewing the conversion')
        backup=store.folder/'migration-backups'; backup.mkdir(exist_ok=True)
        destination=sqlite3.connect(backup/('before-task-groups-'+uuid.uuid4().hex+'.sqlite3'))
        try:
            store.db.backup(destination)
        finally:
            destination.close()
        group=next((g for g in c.repo.all('group') if g.get('legacy_id')==id),None)
        group=group or c.repo.save_plan(plan.model_dump(mode='json'),legacy_id=id)
        store.put('groups',{**old,'task_group_id':group['id'],'schedule':{**old.get('schedule',{}),'auto_start':False,'slots':[]}},id)
        for task in tasks:
            if task.get('status')=='scheduled':
                store.put('tasks',{**task,'status':'stopped','state':'STOPPED','scheduled_at':None,
                                   'message':'Schedule disabled during task-group migration'},task['id'])
        return c.summary(group)
    except ValueError as exc:
        raise invalid(exc) from exc
