"""Original group/task workflow with shared scheduling and transaction safeguards."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from retail.app import create_app
from retail.engine import Engine
from retail.store import Store

HEADERS={'X-Retail-Client':'dashboard'}


def test_batch_independence_and_group_limit_inheritance(tmp_path):
    with TestClient(create_app(tmp_path),headers=HEADERS) as c:
        group=c.post('/api/groups',json={'name':'Restock','products':'B012345678','max_total':100}).json()
        accounts=[c.post('/api/accounts',json={'name':name}).json() for name in ('One','Two')]
        tasks=c.post('/api/task-batches/create',json={'group_id':group['id'],'account_ids':[a['id'] for a in accounts]}).json()['created']
        assert len(tasks)==2 and all(t['max_total'] is None for t in tasks)
        assert c.put('/api/tasks/'+tasks[0]['id'],json={'max_total':75,'quantity':2}).status_code==200
        assert c.put('/api/groups/'+group['id'],json={'max_total':125}).status_code==200
        data=c.get('/api/task-workspace').json()
        assert data['groups'][0]['max_total']==125
        by_id={t['id']:t for t in data['tasks']}
        assert (by_id[tasks[0]['id']]['max_total'],by_id[tasks[0]['id']]['quantity'])==(75,2)
        assert (by_id[tasks[1]['id']]['max_total'],by_id[tasks[1]['id']]['quantity'])==(None,1)
        assert c.get('/api/state').json()['accounts'][0]['name']=='One'
        # Atomic validation: an invalid account never leaves a partial task batch.
        assert c.post('/api/task-batches/create',json={'group_id':group['id'],'account_ids':[accounts[0]['id'],'missing']}).status_code==404
        assert len(c.get('/api/task-workspace').json()['tasks'])==2


def test_duplicate_clears_execution_state_and_schedule(tmp_path):
    with TestClient(create_app(tmp_path),headers=HEADERS) as c:
        group=c.post('/api/groups',json={'name':'Restock','products':'B012345678','schedule':{'slots':[{'start':'10:00','stop':'10:30'}]}}).json()
        c.post('/api/tasks',json={'group_id':group['id'],'quantity':3,'max_total':80})
        copy=c.post('/api/task-workspace/'+group['id']+'/duplicate').json()
        assert copy['id']!=group['id'] and copy['schedule']['slots']==[]
        tasks=c.get('/api/task-workspace').json()['tasks']
        new=next(t for t in tasks if t['group_id']==copy['id'])
        assert new['simulation'] and new['status']=='idle' and new['scheduled_at'] is None
        assert new['quantity']==3 and new['max_total']==80


def test_running_group_frozen_and_start_idempotent(tmp_path):
    with TestClient(create_app(tmp_path),headers=HEADERS) as c:
        group=c.post('/api/groups',json={'name':'Restock','products':'B012345678'}).json()
        task=c.post('/api/tasks',json={'group_id':group['id'],'checkout_mode':'monitor'}).json()
        url='/api/tasks/'+task['id']
        assert c.post(url+'/start').status_code==200
        assert c.post(url+'/start').status_code==200
        assert len(c.get('/api/task-workspace').json()['active'])==1
        assert c.put('/api/groups/'+group['id'],json={'max_total':1}).status_code==409
        assert c.put(url,json={'quantity':3}).status_code==409
        assert c.post(url+'/stop').status_code==200
        assert c.put('/api/groups/'+group['id'],json={'max_total':90}).status_code==200


def test_task_cap_used_by_execution_without_changing_group(tmp_path):
    with TestClient(create_app(tmp_path),headers=HEADERS) as c:
        group=c.post('/api/groups',json={'name':'Caps','products':'B012345678','max_total':100}).json()
        task=c.post('/api/tasks',json={'group_id':group['id'],'max_total':0}).json()
        c.post('/api/tasks/'+task['id']+'/start')
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            data=c.get('/api/task-workspace').json()
            if data['tasks'][0]['status']=='attention':break
            time.sleep(.05)
        assert data['tasks'][0]['status']=='attention'
        assert '0.00' in data['tasks'][0]['message']
        assert data['groups'][0]['max_total']==100
        assert not c.get('/api/state').json()['checkouts']


def test_pending_order_protects_account_even_after_task_deleted(tmp_path):
    store=Store(tmp_path);engine=Engine(store)
    store.put('submissions',{'task_id':'deleted','account_id':'a','status':'submitting'})
    with pytest.raises(ValueError,match='pending order'):engine.check_pending_order('a')
    engine.check_pending_order('b')
    store.db.close()


def test_start_rejected_while_stop_cleanup_pending(tmp_path):
    async def scenario():
        store=Store(tmp_path);engine=Engine(store);entered=asyncio.Event();release=asyncio.Event()
        async def worker():
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:
                entered.set();await release.wait()
        engine.jobs['task']=asyncio.create_task(worker());await asyncio.sleep(0)
        stop=asyncio.create_task(engine.stop('task'));await entered.wait()
        with pytest.raises(ValueError,match='stopping'):await engine.start('task')
        release.set();await stop
        assert not engine.jobs and not engine.stopping_tasks
        store.db.close()
    asyncio.run(scenario())
