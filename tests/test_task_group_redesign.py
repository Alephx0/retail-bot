"""Controlled workflows only: no retailer traffic or real transactions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from retail.app import create_app
from retail.task_groups.domain import Plan, effective_plan, enabled_accounts
from retail.task_groups.repository import Conflict, Repository
from test_task_groups import setup, reserve, snapshot, plan, HEADERS
from test_task_group_execution import setup as execution_setup


def test_overrides_inherit_zero_false_and_remain_group_local():
    shared = plan(account_ids=['a', 'b'], allow_used=True, account_settings={
        'a': {'overrides': {'max_order_cents': 0, 'allow_used': False}},
        'b': {'enabled': False}})
    first = effective_plan(shared, 'a')
    assert first['max_order_cents'] == 0 and first['allow_used'] is False
    assert enabled_accounts(shared) == ['a']
    shared['units_per_order'] = 2
    assert effective_plan(shared, 'a')['units_per_order'] == 2
    assert effective_plan(shared, 'b')['max_order_cents'] == 9000
    first['products'][0]['max_unit_cents'] = 1
    assert shared['products'][0]['max_unit_cents'] == 8000
    assert effective_plan(plan(account_ids=['a']), 'a')['max_order_cents'] == 9000


@pytest.mark.parametrize('settings', [
    {'missing': {}}, {'a': {'overrides': {'product_ids': ['NOTINTARGET']}}},
    {'a': {'overrides': {'max_order_cents': 19000}}},
    {'a': {'overrides': {'password': 'never allowed'}}},
])
def test_invalid_assignment_configuration_is_rejected(settings):
    with pytest.raises(ValueError):
        plan(account_ids=['a'], account_settings=settings)


def test_independent_quantities_product_caps_and_disabled_assignment(tmp_path):
    store, repo, group, run = setup(tmp_path, account_ids=['a', 'b', 'disabled'],
        target_units=5, per_account_units=5, max_spend_cents=30000,
        account_settings={'a': {'overrides': {'units_per_order': 2, 'max_order_cents': 8000}},
                          'disabled': {'enabled': False}})
    a, b = reserve(repo, run, 'a'), reserve(repo, run, 'b')
    assert (a['units'], a['money_cents']) == (2, 8000)
    assert (b['units'], b['money_cents']) == (1, 9000)
    with pytest.raises(Conflict): reserve(repo, run, 'disabled')
    assert repo.progress(run)['reserved_units'] == 3
    repo.close(); store.db.close()


@pytest.mark.parametrize('mode,target,winners', [('first_success', 10, 1), ('multiple_success', 4, 4)])
def test_many_writers_cannot_exceed_order_goal(tmp_path, mode, target, winners):
    ids = [f'account-{i}' for i in range(16)]
    store, repo, group, run = setup(tmp_path, account_ids=ids, goal_mode=mode,
        target_orders=target, max_spend_cents=200000)
    def attempt(account):
        writer = Repository(store)
        try:
            return reserve(writer, run, account)
        except Conflict:
            return None
        finally:
            writer.close()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [a for a in pool.map(attempt, ids) if a]
    assert len(results) == winners
    for a in results:
        repo.intent(a['id'], snapshot())
        repo.finish(a['id'], 'confirmed', 'Fixture confirmation', a['id'], 8000)
    assert repo.progress(run)['confirmed_orders'] == winners
    assert repo.require('run', run['id'])['state'] == 'completed'
    with pytest.raises(Conflict, match='fulfilled'): repo.start(group['id'], 'again')
    repo.close(); store.db.close()


def test_uncertain_first_purchase_holds_order_slot_across_restart(tmp_path):
    store, repo, group, run = setup(tmp_path, account_ids=['a', 'b'], goal_mode='first_success')
    a = reserve(repo, run, 'a'); repo.intent(a['id'], snapshot()); repo.close()
    repo = Repository(store); repo.recover()
    repo.command(run['id'], 'stop'); repo.run_state(run['id'], 'stopped', 'Stopped')
    restarted = repo.start(group['id'], 'restart')
    with pytest.raises(Conflict, match='reserved'): reserve(repo, restarted, 'b')
    repo.finish(a['id'], 'cancelled', 'Verified no order', evidence='Checked retailer order history and cart')
    assert reserve(repo, restarted, 'b')['account_id'] == 'b'
    repo.close(); store.db.close()


def test_derived_counter_migration_and_transaction_rollback(tmp_path):
    store, repo, group, run = setup(tmp_path)
    a = reserve(repo, run); repo.intent(a['id'], snapshot())
    repo.finish(a['id'], 'confirmed', 'Fixture', 'ORDER', 8000)
    expected = repo.progress(run)
    with pytest.raises(RuntimeError):
        with repo.transaction():
            changed = {**a, 'state': 'confirmed', 'money_cents': 1}
            repo._put('attempt', changed, run['id'])
            raise RuntimeError('Rollback injected')
    assert repo.progress(run) == expected
    # Simulate the previous schema, preserving original encrypted records.
    with repo.transaction():
        repo.db.execute("DELETE FROM tg_records WHERE kind='quota'")
        repo.db.execute('DELETE FROM tg_status')
        repo.db.execute('DELETE FROM tg_schema')
    repo.close(); repo = Repository(store)
    assert (tmp_path / 'task-groups-before-v2.sqlite3').exists()
    assert repo.progress(run) == expected
    assert repo.active_runs()[0]['id'] == run['id']
    repo.close(); repo = Repository(store)
    assert repo.progress(run) == expected  # No double counting on another startup.
    repo.close(); store.db.close()


def test_slow_browser_creation_does_not_lock_unrelated_account(tmp_path):
    async def scenario():
        store, adapter, c, account, run = execution_setup(tmp_path, action='notify')
        other = store.put('accounts', {**account, 'name': 'Other'}, 'other')
        entered, release, second_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = adapter.context
        async def context(a):
            if a['id'] == account['id']:
                entered.set(); await release.wait()
            return await original(a)
        adapter.context = context
        async def lease(a):
            async with c.pool.lease(a):
                if a['id'] == 'other': second_ready.set()
        slow = asyncio.create_task(lease(account))
        try:
            await entered.wait()
            await asyncio.wait_for(lease(other), 1)
            assert second_ready.is_set() and not slow.done()
        finally:
            release.set(); await slow; await c.close(); store.db.close()
    asyncio.run(scenario())


def test_slow_account_does_not_hold_next_monitor_cycle(tmp_path):
    async def scenario():
        store, adapter, c, account, old = execution_setup(tmp_path, action='notify')
        await c.command(old['id'], 'stop')
        other = store.put('accounts', {**account}, 'other')
        group = c.repo.save_plan({**old['plan'], 'account_ids':[account['id'],other['id']]})
        run = c.repo.start(group['id'], 'start')
        slow_started, release, fast_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
        counts = {}
        async def inspect(current, id, target):
            counts[id] = counts.get(id, 0) + 1
            if id == account['id']:
                slow_started.set(); await release.wait()
            else:
                fast_seen.set()
        c.inspect = inspect
        try:
            await c.tick(); await slow_started.wait(); await fast_seen.wait()
            await asyncio.sleep(.01)
            c.next_scan[(run['id'], other['id'])] = 0
            fast_seen.clear(); await c.tick()
            await asyncio.wait_for(fast_seen.wait(), 1)
            assert counts == {account['id']:1, other['id']:2}
        finally:
            release.set(); await c.close(); store.db.close()
    asyncio.run(scenario())


def test_stop_before_executor_first_step_releases_claim(tmp_path):
    async def scenario():
        store, adapter, c, account, run = execution_setup(tmp_path)
        a = reserve(c.repo, run, account['id'])
        job = asyncio.create_task(c.executor.run(a)); c.jobs[a['id']] = job
        await c.command(run['id'], 'stop')
        assert not c.repo.claimed(account['id'])
        assert c.repo.require('attempt', a['id'])['state'] == 'cancelled'
        assert adapter.submissions == 0
        await c.close(); store.db.close()
    asyncio.run(scenario())


def test_account_pause_and_stop_are_isolated(tmp_path):
    async def scenario():
        store, adapter, c, account, old = execution_setup(tmp_path)
        await c.command(old['id'], 'stop')
        other = store.put('accounts', {**account}, 'other')
        group = c.repo.save_plan({**old['plan'], 'account_ids':[account['id'],other['id']],
            'goal_mode':'multiple_success','target_orders':2,'max_spend_cents':20000})
        run = c.repo.start(group['id'], 'start')
        await c.member_command(run['id'], account['id'], 'pause')
        with pytest.raises(Conflict): reserve(c.repo, run, account['id'])
        a = reserve(c.repo, run, other['id']); await c.executor.run(a)
        assert c.repo.require('attempt', a['id'])['state'] == 'confirmed'
        await c.member_command(run['id'], account['id'], 'resume')
        a = reserve(c.repo, run, account['id']); await c.executor.run(a)
        assert c.repo.require('run', run['id'])['state'] == 'completed'
        await c.close(); store.db.close()
    asyncio.run(scenario())


def test_group_settings_api_preserves_global_account_and_inheritance(tmp_path):
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        store = client.app.state.engine.store
        account = store.put('accounts', {'name':'Fixture','retailer':'amazon','region':'US'}, 'fixture')
        shared = plan(account_ids=[account['id']], account_settings={account['id']:{'overrides':{'units_per_order':2}}})
        response = client.post('/api/task-groups', json=shared)
        assert response.status_code == 200, response.text
        g = response.json(); shared['max_order_cents'] = 8500
        updated = client.patch('/api/task-groups/'+g['id'], json={'revision_id':g['revision_id'],'plan':shared})
        assert updated.status_code == 200
        detail = client.get('/api/task-groups/'+g['id']).json()
        member = detail['readiness']['accounts'][0]
        assert member['effective']['max_order_cents'] == 8500
        assert member['effective']['units_per_order'] == 2
        assert member['overrides'] == {'units_per_order':2}
        assert store.get('accounts', account['id']) == account
        run = client.post('/api/task-groups/'+g['id']+'/runs', json={'idempotency_key':'start'}).json()
        assert client.post('/api/group-runs/'+run['id']+'/accounts/fixture/pause',json={}).status_code == 200
        assert client.get('/api/task-groups/'+g['id']).json()['members'][0]['state'] == 'paused'
        assert client.patch('/api/task-groups/'+g['id'],json={'revision_id':updated.json()['revision_id'],'plan':shared}).status_code == 409


def test_hundred_accounts_complete_with_bounded_parallel_checkout(tmp_path):
    async def scenario():
        store, adapter, c, account, old = execution_setup(tmp_path)
        await c.command(old['id'], 'stop')
        shared = {**old['plan'], 'simulation':True, 'action':'automatic',
            'account_ids':[f'sim-{i}' for i in range(100)], 'goal_mode':'multiple_success',
            'target_orders':100, 'max_spend_cents':1000000, 'max_parallel_checkouts':10}
        group = c.repo.save_plan(shared); run = c.repo.start(group['id'], 'stress')
        active, peak = 0, 0
        original = c.executor.run
        async def execute(attempt):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            try: await original(attempt)
            finally: active -= 1
        c.executor.run = execute
        for _ in range(15):
            if c.repo.require('run', run['id'])['state'] == 'completed': break
            await c.scan(c.repo.require('run', run['id']))
            await asyncio.gather(*list(c.jobs.values()))
        assert c.repo.progress(run)['confirmed_orders'] == 100
        assert peak == 10 and not c.jobs
        assert not c.repo.db.execute('SELECT 1 FROM tg_claims').fetchone()
        assert adapter.submissions == 0  # Simulation never reaches the adapter.
        await c.close(); store.db.close()
    asyncio.run(scenario())


def test_retry_exhaustion_is_local_but_retailer_backoff_is_account_wide(tmp_path):
    from retail.amazon import BackoffRequired
    async def scenario():
        store, adapter, c, account, run = execution_setup(tmp_path, action='notify', max_read_errors=1)
        group = c.repo.save_plan({**run['plan'], 'name':'Other group'})
        other = c.repo.start(group['id'], 'other')
        original = adapter.inspect
        async def failure(*args): raise OSError('Fixture read failure')
        adapter.inspect = failure
        await c.scan(run)
        assert c.cooldowns[(run['id'],account['id'])] == float('inf')
        adapter.inspect = original
        await c.scan(other)
        assert c.repo.all('observation',other['id'])[0]['reason'] == 'eligible'
        assert (other['id'],account['id']) not in c.cooldowns
        async def backoff(*args):
            raise BackoffRequired('Fixture 429',status=429,retry_after_seconds=120)
        adapter.inspect = backoff; c.cache.clear()
        await c.scan(other)
        assert account['id'] in c.retailer_backoffs
        c.cooldowns.clear(); adapter.inspect = original
        before = adapter.inspections
        await c.scan(run)
        assert adapter.inspections == before  # Rechecking another group cannot bypass a 429.
        await c.close(); store.db.close()
    asyncio.run(scenario())


def test_read_timeout_retries_without_holding_the_account(tmp_path, monkeypatch):
    from retail.task_groups import coordinator
    monkeypatch.setattr(coordinator, 'effective_plan', lambda p,a:{**effective_plan(p,a),'read_timeout_seconds':.02})
    async def scenario():
        store, adapter, c, account, run = execution_setup(tmp_path, action='notify')
        async def stalled(*args): await asyncio.Event().wait()
        adapter.inspect = stalled
        await c.scan(run)
        assert c.repo.all('observation',run['id'])[0]['reason'] == 'retrying'
        assert not c.repo.claimed(account['id'])
        assert not c.engine.account_locks[account['id']].locked()
        assert not c.pool.sessions[account['id']]['busy']
        await c.close(); store.db.close()
    asyncio.run(scenario())


def test_concurrent_starts_resolve_to_one_run(tmp_path):
    store, repo, group, run = setup(tmp_path)
    repo.command(run['id'],'stop'); repo.run_state(run['id'],'stopped','Stopped')
    def start(index):
        writer=Repository(store)
        try: return writer.start(group['id'],str(index))['id']
        finally: writer.close()
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids=list(pool.map(start,range(16)))
    assert len(set(ids)) == 1
    assert len(repo.active_runs()) == 1
    repo.close(); store.db.close()


def test_checkout_timeout_quarantines_mutation_and_is_not_replayed(tmp_path, monkeypatch):
    from retail.task_groups import execution
    resolve = effective_plan
    # Inject a short deadline while exercising the real timeout and cleanup code.
    monkeypatch.setattr(execution, 'effective_plan', lambda p,a:{**resolve(p,a),'checkout_timeout_seconds':.05})
    async def scenario():
        store, adapter, c, account, run = execution_setup(tmp_path)
        async def stalled_cart(*args):
            adapter.carts += 1
            await asyncio.Event().wait()
        adapter.cart = stalled_cart
        a = reserve(c.repo, run, account['id'])
        await c.executor.run(a)
        assert c.repo.require('attempt',a['id'])['state'] == 'reconciliation_required'
        assert c.repo.claimed(account['id']) == a['id']
        assert not c.pool.sessions[account['id']]['busy']
        await c.scan(run)
        assert adapter.carts == 1 and adapter.submissions == 0
        await c.close(); store.db.close()
    asyncio.run(scenario())


def test_new_executor_supplies_modern_checkout_evidence(tmp_path):
    from patchright.async_api import async_playwright
    from retail.amazon import Amazon
    async def scenario():
        store, adapter, c, account, run = execution_setup(tmp_path, action='quote')
        title = 'Controlled fixture product'
        body = f'<ul><li>Items: $75.00</li><li>Order total: $80.00</li></ul><div>{title} $75.00<br>Ships from and sold by<br>Amazon.com</div><div role="group" aria-label="Change quantity of {title}">1 1</div><button>Place your order</button>'
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            async def context(account):
                ctx = await browser.new_context()
                await ctx.route('**/*',lambda route:route.fulfill(content_type='text/html',body=body))
                return ctx
            adapter.context = context
            async def cart(page, quantity, asin):
                page._retail_cart_title=title; page._retail_cart_asin=asin
                return quantity
            async def prepare(page, *args):
                await page.goto('https://www.amazon.com/checkout/p/fixture/spc')
            adapter.cart=cart; adapter.prepare_checkout=prepare
            adapter.checkout_snapshot=Amazon(None).checkout_snapshot
            a = reserve(c.repo, run, account['id'])
            await c.executor.run(a)
            assert c.repo.require('attempt',a['id'])['state'] == 'quoted'
            assert adapter.submissions == 0
            await c.close(); await browser.close()
        store.db.close()
    asyncio.run(scenario())
