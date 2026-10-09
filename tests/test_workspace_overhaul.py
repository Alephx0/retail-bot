"""Configuration inheritance, deletion and the single-scheduler boundary."""
import json

import pytest
from fastapi.testclient import TestClient

from retail.app import create_app
from retail.store import Store
from retail.task_groups.domain import effective_plan
from retail.task_groups.repository import Repository, Conflict
from test_task_groups import HEADERS, plan, reserve, snapshot


def test_sparse_global_inheritance_survives_restart_and_freezes_runs(tmp_path):
    store=Store(tmp_path); repo=Repository(store)
    config=plan(inherited_fields=['action','allow_used'],account_ids=['a','b'],
                account_settings={'a':{'overrides':{'allow_used':False}}})
    group=repo.save_plan(config)
    row=repo.db.execute('SELECT payload FROM tg_records WHERE id=?',(group['revision_id'],)).fetchone()
    saved=json.loads(store.cipher.decrypt(row[0]))['plan']
    assert 'action' not in saved and 'allow_used' not in saved
    run=repo.start(group['id'],'first')
    store.put('task_group_defaults',{'values':{'action':'notify','allow_used':True}},'defaults')
    resolved=repo.require('revision',group['revision_id'])['plan']
    assert resolved['action']=='notify'
    assert effective_plan(resolved,'a')['allow_used'] is False
    assert effective_plan(resolved,'b')['allow_used'] is True
    assert repo.require('run',run['id'])['plan']['action']=='review'
    repo.close(); store.db.close()
    store=Store(tmp_path); repo=Repository(store)
    assert repo.require('revision',group['revision_id'])['plan']['action']=='notify'
    assert repo.require('run',run['id'])['plan']['action']=='review'
    repo.close(); store.db.close()


def test_explicit_existing_plans_do_not_acquire_inheritance(tmp_path):
    store=Store(tmp_path); repo=Repository(store)
    group=repo.save_plan(plan(action='review'))
    store.put('task_group_defaults',{'values':{'action':'automatic'}},'defaults')
    assert repo.require('revision',group['revision_id'])['plan']['action']=='review'
    assert repo.start(group['id'],'run')['plan']['action']=='review'
    repo.close(); store.db.close()


def test_reset_account_settings_removes_redundant_assignment_values(tmp_path):
    store=Store(tmp_path);repo=Repository(store)
    config=plan(account_ids=['a','b'],account_settings={'a':{'overrides':{}},'b':{'enabled':False}})
    group=repo.save_plan(config)
    saved=repo.require('revision',group['revision_id'])['plan']
    assert saved['account_settings']=={'b':{'enabled':False,'overrides':{}}}
    repo.close();store.db.close()


@pytest.mark.parametrize('inherited',[['password'],['action','action'],None,'action',[{}]])
def test_invalid_inheritance_is_validation_error(tmp_path,inherited):
    with TestClient(create_app(tmp_path),headers=HEADERS) as client:
        config=plan();config['inherited_fields']=inherited
        assert client.post('/api/task-groups',json=config).status_code==422


def test_defaults_and_active_read_model_use_same_frozen_plan(tmp_path):
    with TestClient(create_app(tmp_path),headers=HEADERS) as client:
        config=plan(inherited_fields=['action'],action='notify')
        client.put('/api/task-group-defaults',json={'action':'notify'})
        a=client.post('/api/accounts',json={'name':'A','email':'a@example.invalid'}).json()
        config['account_ids']=[a['id']]
        group=client.post('/api/task-groups',json=config).json()
        run=client.post('/api/task-groups/'+group['id']+'/runs',json={'idempotency_key':'run'}).json()
        assert client.put('/api/task-group-defaults',json={'action':'review'}).status_code==200
        detail=client.get('/api/task-groups/'+group['id']).json()
        assert detail['plan']['action']=='notify'
        assert detail['readiness']['accounts'][0]['effective']['action']=='notify'
        client.post('/api/group-runs/'+run['id']+'/stop',json={})
        assert client.get('/api/task-groups/'+group['id']).json()['plan']['action']=='review'
        assert client.put('/api/task-group-defaults',json={'units_per_order':3,'per_account_units':1}).status_code==422
        assert client.put('/api/task-group-defaults',json={'password':'no'}).status_code==422


def test_delete_stops_group_preserves_history_and_prevents_restart(tmp_path):
    with TestClient(create_app(tmp_path),headers=HEADERS) as client:
        group=client.post('/api/task-groups',json=plan(action='notify')).json()
        run=client.post('/api/task-groups/'+group['id']+'/runs',json={'idempotency_key':'run'}).json()
        assert client.delete('/api/task-groups/'+group['id']).status_code==200
        assert client.get('/api/task-groups').json()['groups']==[]
        detail=client.get('/api/task-groups/'+group['id']).json()
        assert detail['archived'] and detail['run']['state']=='stopped'
        assert detail['runs'][0]['id']==run['id']
        assert client.post('/api/task-groups/'+group['id']+'/runs',json={'idempotency_key':'new'}).status_code==409
        assert client.patch('/api/task-groups/'+group['id'],json={'revision_id':group['revision_id'],'plan':plan()}).status_code==409


def test_uncertain_outcome_blocks_archive_until_verified(tmp_path):
    store=Store(tmp_path); repo=Repository(store)
    group=repo.save_plan(plan());run=repo.start(group['id'],'run')
    attempt=reserve(repo,run);repo.intent(attempt['id'],snapshot())
    repo.finish(attempt['id'],'reconciliation_required','Network interrupted')
    repo.run_state(run['id'],'stopped','Stopped')
    with pytest.raises(Conflict,match='Resolve pending'):
        repo.archive(group['id'])
    assert repo.progress(run)['reserved_orders']==1
    assert repo.claimed('simulation:simulation')
    repo.close();store.db.close()


def test_original_tasks_and_saved_plans_share_one_scheduler(tmp_path):
    with TestClient(create_app(tmp_path),headers=HEADERS) as client:
        old=client.post('/api/groups',json={'name':'Saved plan','products':'B012345678'}).json()
        task=client.post('/api/tasks',json={'group_id':old['id']}).json()
        assert client.post('/api/tasks/'+task['id']+'/start').status_code==200
        assert client.app.state.engine.scheduler is not None
        assert client.app.state.group_coordinator.scheduler is None
        assert client.get('/api/state').json()['groups'][0]['id']==old['id']


def test_simulated_confirmation_appears_once_in_orders_and_restart_recovery(tmp_path):
    import time
    with TestClient(create_app(tmp_path),headers=HEADERS) as client:
        g=client.post('/api/task-groups',json=plan(action='automatic',goal_mode='first_success')).json()
        client.post('/api/task-groups/'+g['id']+'/runs',json={'idempotency_key':'start'})
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            orders=client.get('/api/state').json()['checkouts']
            if orders:break
            time.sleep(.05)
        assert len(orders)==1 and orders[0]['simulation'] is True
        assert orders[0]['order_id'].startswith('SIM-')
        order_id=orders[0]['id']
        client.app.state.store.delete('checkouts',order_id)
    with TestClient(create_app(tmp_path),headers=HEADERS) as client:
        orders=client.get('/api/state').json()['checkouts']
        assert len(orders)==1 and orders[0]['id']==order_id
        assert orders[0]['simulation'] is True
