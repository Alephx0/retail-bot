import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from retail.app import create_app
from retail.store import Store
from retail.task_groups.domain import Plan, Schedule, cents, qualifying, windows
from retail.task_groups.repository import Conflict, Repository
from retail.task_groups.workspace_lock import WorkspaceLock

HEADERS={'X-Retail-Client':'dashboard'}


def plan(**changes):
    return Plan(name='Two units',products=[{'product_id':'B012345678','max_unit_cents':8000}],
                **{'target_units':2,'per_account_units':2,'max_order_cents':9000,'max_spend_cents':18000,**changes}).model_dump(mode='json')


def setup(tmp_path, **changes):
    store=Store(tmp_path); repo=Repository(store)
    group=repo.save_plan(plan(**changes)); run=repo.start(group['id'],'start')
    return store,repo,group,run


def reserve(repo,run,account='simulation'):
    return repo.reserve(run['id'],account,'B012345678',run['generation'],{'price':75})


def snapshot():
    return {'asin':'B012345678','quantity':1,'currency':'USD','total':80}


def test_integer_money_and_schedule_dst():
    assert cents('19.99')==1999
    for value in ('NaN','Infinity','-1'):
        with pytest.raises(ValueError): cents(value)
    current=datetime(2026,3,7,tzinfo=timezone.utc)
    assert windows(Schedule(kind='once',date='2026-03-08',start='02:30',end='03:30').model_dump(),current)==[]
    fall=windows(Schedule(kind='once',date='2026-11-01',start='01:30',end='02:30').model_dump(),current)
    assert len(fall)==1
    assert fall[0]['start']=='2026-11-01T05:30:00+00:00'
    assert fall[0]['end']=='2026-11-01T07:30:00+00:00'


def test_atomic_goal_account_and_money_reservations(tmp_path):
    store,repo,group,run=setup(tmp_path)
    a=reserve(repo,run)
    with pytest.raises(Conflict,match='busy'): reserve(repo,run)
    assert repo.progress(run)=={'confirmed_units':0,'reserved_units':1,'spent_cents':0,'reserved_cents':9000}
    repo.intent(a['id'],snapshot())
    repo.finish(a['id'],'confirmed','confirmed','ORDER-1',8000)
    b=reserve(repo,run)
    repo.intent(b['id'],snapshot()); repo.finish(b['id'],'confirmed','confirmed','ORDER-2',8000)
    assert repo.require('run',run['id'])['state']=='completed'
    with pytest.raises(Conflict,match='fulfilled'): repo.start(group['id'],'another-start')
    assert repo.start(group['id'],'start')['id']==run['id']
    assert b'ORDER-1' not in b''.join(bytes(row[0]) for row in repo.db.execute('SELECT payload FROM tg_records'))
    repo.close(); store.db.close()


def test_two_writers_cannot_overspend_or_claim_last_unit(tmp_path):
    store,repo,group,run=setup(tmp_path,account_ids=['a','b'],target_units=1)
    def race(account):
        other=Repository(store)
        try:
            return other.reserve(run['id'],account,'B012345678',0,{'price':75})['id']
        except Conflict:
            return None
        finally: other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(race,['a','b']))
    assert len([r for r in results if r])==1
    assert repo.progress(run)['reserved_units']==1
    repo.close(); store.db.close()


def test_pause_generation_and_stop_preserve_progress(tmp_path):
    store,repo,group,run=setup(tmp_path)
    a=reserve(repo,run); repo.command(run['id'],'pause')
    with pytest.raises(Conflict):repo.gate(a['id'])
    repo.command(run['id'],'resume'); assert repo.gate(a['id'])['generation']==2
    repo.intent(a['id'],snapshot()); repo.finish(a['id'],'confirmed','ok','ORDER',8000)
    repo.command(run['id'],'stop'); repo.run_state(run['id'],'stopped','Stopped')
    resumed=repo.start(group['id'],'restart')
    assert repo.progress(resumed)['confirmed_units']==1
    assert repo.progress(resumed)['spent_cents']==8000
    repo.close(); store.db.close()


def test_crash_after_intent_retains_reservation_and_claim(tmp_path):
    store,repo,group,run=setup(tmp_path)
    a=reserve(repo,run); repo.intent(a['id'],snapshot()); repo.close()
    repo=Repository(store); repo.recover()
    assert repo.require('attempt',a['id'])['state']=='reconciliation_required'
    assert repo.claimed('simulation:simulation')==a['id']
    assert repo.progress(run)['reserved_cents']==8000
    with pytest.raises(Conflict,match='Resolve'):repo.command(run['id'],'resume')
    repo.finish(a['id'],'cancelled','Verified no order',evidence='Checked order history and emptied the matching cart')
    assert not repo.claimed('simulation:simulation')
    repo.command(run['id'],'resume')
    repo.close(); store.db.close()


def test_unknown_cart_and_duplicate_order_are_not_retried(tmp_path):
    store,repo,group,run=setup(tmp_path)
    a=reserve(repo,run); repo.stage(a['id'],'preparing','prepare'); repo.stage(a['id'],'carting','cart')
    result=repo.finish(a['id'],'failed','timeout')
    assert result['state']=='reconciliation_required'
    repo.finish(a['id'],'confirmed','Checked history','ORDER',8000,evidence='Verified the order total in retailer history')
    b=reserve(repo,run); repo.intent(b['id'],snapshot())
    with pytest.raises(Conflict,match='already recorded'):
        repo.finish(b['id'],'confirmed','Checked history','ORDER',8000)
    assert repo.claimed('simulation:simulation')==b['id']
    repo.close(); store.db.close()


def test_workspace_single_owner(tmp_path):
    first=WorkspaceLock(tmp_path)
    with pytest.raises(RuntimeError,match='Another'): WorkspaceLock(tmp_path)
    first.close(); second=WorkspaceLock(tmp_path); second.close()


def test_deal_filters_require_verified_discount_and_allow_free_orders(tmp_path):
    p=plan(min_discount_percent=20.5,min_savings_cents=2000)
    target=p['products'][0]
    item={'available':True,'price':79.5,'original_price':100,'amazon_seller':True,'condition':'new'}
    assert qualifying(item,target,p)[0]
    assert qualifying({**item,'price':79.51},target,p)[1]=='discount_too_small'
    assert qualifying({**item,'original_price':None},target,p)[1]=='unknown_reference_price'
    assert qualifying({**item,'price':float('nan')},target,p)[1]=='unknown_price'
    store,repo,group,run=setup(tmp_path,max_order_cents=0,max_spend_cents=0,only_freebies=True)
    assert qualifying({**item,'price':0},target,run['plan'])[0]
    assert qualifying(item,target,run['plan'])[1]=='not_free'
    a=reserve(repo,run)
    repo.intent(a['id'],{**snapshot(),'total':0})
    repo.finish(a['id'],'confirmed','Free simulated item','FREE',0)
    assert repo.progress(run)['confirmed_units']==1
    assert repo.progress(run)['spent_cents']==0
    repo.close(); store.db.close()


def test_prior_run_uncertainty_remains_visible_and_records_actual_quantity(tmp_path):
    with TestClient(create_app(tmp_path),headers=HEADERS) as client:
        repo=client.app.state.group_coordinator.repo
        group=repo.save_plan(plan())
        run=repo.start(group['id'],'first')
        a=reserve(repo,run)
        repo.stage(a['id'],'preparing','prepare'); repo.stage(a['id'],'carting','cart')
        repo.finish(a['id'],'failed','Lost browser')
        repo.command(run['id'],'stop'); repo.run_state(run['id'],'stopped','Stopped')
        next_run=repo.start(group['id'],'second')
        repo.command(next_run['id'],'pause')
        detail=client.get('/api/task-groups/'+group['id']).json()
        assert detail['run']['id']==next_run['id']
        assert detail['attempts'][0]['id']==a['id']
        assert detail['attempts'][0]['state']=='reconciliation_required'
        payload={'outcome':'confirmed','product_id':'B012345678','units':3,'order_id':'MANUAL',
                 'total_cents':24000,'evidence':'Verified three units and final total in order history'}
        result=client.post('/api/group-attempts/'+a['id']+'/resolve',json=payload)
        assert result.status_code==200,result.text
        assert repo.progress(next_run)['confirmed_units']==3
        assert repo.progress(next_run)['spent_cents']==24000
        assert repo.require('run',next_run['id'])['state']=='completed'
        assert not repo.claimed('simulation:simulation')
        details=client.get('/api/group-attempts/'+a['id']).json()
        assert details['plan']['target_units']==2
        assert details['events']


def test_api_plan_revision_security_and_simulation_run(tmp_path):
    import time
    with TestClient(create_app(tmp_path)) as client:
        assert client.post('/api/task-groups',json=plan()).status_code==403
        client.headers.update(HEADERS)
        created=client.post('/api/task-groups',json=plan(target_units=1,action='automatic')).json()
        assert created['plan']['simulation']
        wrong=client.patch('/api/task-groups/'+created['id'],json={'revision_id':'stale','plan':plan()})
        assert wrong.status_code==409
        started=client.post('/api/task-groups/'+created['id']+'/runs',json={'idempotency_key':'one'}).json()
        assert started['state']=='watching'
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=client.get('/api/task-groups/'+created['id']).json()
            if result['run']['state']=='completed': break
            time.sleep(.05)
        assert result['run']['state']=='completed',result
        assert result['progress']['confirmed_units']==1
        assert len(result['attempts'])==1
        assert 'intent' not in result['attempts'][0]
        assert client.app.state.engine.amazon.browser is None
        assert client.post('/api/task-groups/'+created['id']+'/runs',json={'idempotency_key':'one'}).json()['id']==started['id']


def test_migration_keeps_old_tasks_but_disables_old_engine(tmp_path):
    with TestClient(create_app(tmp_path),headers=HEADERS) as client:
        old=client.post('/api/groups',json={'name':'Old','products':'B012345678;80'}).json()
        task=client.post('/api/tasks',json={'group_id':old['id']}).json()
        preview=client.post('/api/task-group-migrations/'+old['id']+'/preview').json()
        result=client.post('/api/task-group-migrations/'+old['id']+'/apply',json=preview['plan'])
        assert result.status_code==200,result.text
        assert client.post('/api/tasks/'+task['id']+'/start').status_code==409
        assert list((tmp_path/'migration-backups').glob('*.sqlite3'))
        assert client.get('/api/state').json()['tasks'][0]['id']==task['id']
