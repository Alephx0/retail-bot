"""Stopping overlapping requests must drain account ownership before restart."""
import asyncio

import pytest

from retail.task_groups.coordinator import Coordinator
from test_account_readiness import setup
from test_monitors import task_record, until


@pytest.mark.parametrize('second_stop', ['task', 'all', 'all_twice', 'cancel_request', 'cancel_all_request'])
def test_overlapping_stop_drains_browser_before_restart(tmp_path, second_stop):
    async def scenario():
        store, group, account, engine = setup(tmp_path)
        coordinator = Coordinator(engine)
        engine.group_coordinator = coordinator
        task = task_record(store, group, account, use_account_proxy=True)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            await engine.start(task['id'])
            await until(lambda: bool(engine.ready_accounts.get(task['id'], {}).get('idle')))
            context = next(c for c in engine.amazon.contexts if c.identity['id'] == account['id'])
            original_close = context.close

            async def delayed_close():
                entered.set()
                await release.wait()
                await original_close()

            context.close = delayed_close
            first = asyncio.create_task(engine.stop_all() if second_stop in ('all_twice', 'cancel_all_request') else engine.stop(task['id']))
            await asyncio.wait_for(entered.wait(), 2)
            if second_stop in ('cancel_request', 'cancel_all_request'):
                first.cancel()
            second = asyncio.create_task(engine.stop_all() if second_stop in ('all', 'all_twice', 'cancel_all_request') else engine.stop(task['id']))
            await asyncio.sleep(.02)
            assert not second.done(), 'Stop must wait for the account browser to close'
            assert coordinator.busy_account(account['id'])
            with pytest.raises(ValueError, match='stopping'):
                await engine.start(task['id'])
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            assert context.closed
            assert not coordinator.busy_account(account['id'])
            assert engine.browser_slots._value == 10
            assert not engine.ready_accounts and not engine.jobs
            await engine.start(task['id'])
            await until(lambda: bool(engine.ready_accounts.get(task['id'], {}).get('idle')))
            await engine.stop_all()
            assert not coordinator.busy_account(account['id'])
        finally:
            release.set()
            await engine.close()
            coordinator.repo.close()
            store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['monitor', 'trace', 'natural_completion'])
def test_cleanup_errors_and_stop_during_natural_teardown_release_ownership(tmp_path, failure):
    async def scenario():
        store, group, account, engine = setup(tmp_path)
        task = task_record(store, group, account, use_account_proxy=True)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            await engine.start(task['id'])
            await until(lambda: bool(engine.ready_accounts.get(task['id'], {}).get('idle')))
            context = next(c for c in engine.amazon.contexts if c.identity['id'] == account['id'])
            original_unsubscribe = engine.monitors.unsubscribe

            async def unsubscribe(*args):
                await original_unsubscribe(*args)
                if failure == 'monitor':
                    raise RuntimeError('Fixture monitor cleanup failed')

            async def finish_trace(*args):
                if failure == 'trace':
                    raise RuntimeError('Fixture trace cleanup failed')

            engine.monitors.unsubscribe = unsubscribe
            engine.diagnostics.finish_trace = finish_trace
            if failure == 'natural_completion':
                from test_account_readiness import restock
                original_close = context.close

                async def close():
                    entered.set()
                    await release.wait()
                    await original_close()

                context.close = close
                restock(engine)
                await asyncio.wait_for(entered.wait(), 2)
                stopping = asyncio.create_task(engine.stop(task['id']))
                await asyncio.sleep(.02)
                assert not stopping.done()
                release.set()
                await stopping
            else:
                await engine.stop(task['id'])
            assert context.closed
            assert not engine.account_locks[account['id']].locked()
            assert engine.browser_slots._value == 10
            assert not engine.finishing_tasks and not engine.stop_operations
        finally:
            release.set()
            await engine.close()
            store.db.close()
    asyncio.run(scenario())
