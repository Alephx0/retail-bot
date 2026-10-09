"""Repeatable encrypted-ledger and mocked browser-launch benchmarks. No network I/O.

Run from the repository root: python -m scripts.benchmark_task_groups
The comparison executes the original pool from the committed baseline, using
the same fake browser launch delay and resource bounds as the new pool.
"""
import asyncio
import json
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from retail.store import Store
from retail.task_groups.browser_pool import BrowserPool
from retail.task_groups.domain import Plan
from retail.task_groups.repository import Repository


def measure_progress():
    results = []
    for count in (10, 100, 1000):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)); repo = Repository(store)
            plan = Plan(name='Benchmark', products=[{'product_id':'B012345678','max_unit_cents':100}],
                        target_units=1000, per_account_units=1000).model_dump(mode='json')
            group = repo.save_plan(plan); run = repo.start(group['id'], 'benchmark')
            with repo.transaction():
                for i in range(count):
                    repo._put('attempt', {'id':f'bench-{i}','run_id':run['id'],'group_id':group['id'],
                        'account_id':f'a-{i%100}','product_id':'B012345678','state':'confirmed',
                        'units':1,'money_cents':100,'simulation':True}, run['id'])
            timings = []
            for _ in range(30):
                start = time.perf_counter(); value = repo.progress(run)
                timings.append((time.perf_counter()-start)*1000)
            assert value['confirmed_units'] == count and value['confirmed_orders'] == count
            results.append({'attempts':count,'median_progress_ms':round(statistics.median(timings),4),
                            'p95_progress_ms':round(sorted(timings)[28],4)})
            repo.close(); store.db.close()
    return results


async def measure_pool(pool_type, count):
    class Context:
        async def new_page(self): return SimpleNamespace(is_closed=lambda:False)
        async def close(self): pass
    launches = {'active':0, 'peak':0}
    async def context(account):
        launches['active'] += 1
        launches['peak'] = max(launches['peak'], launches['active'])
        await asyncio.sleep(.05)
        launches['active'] -= 1
        return Context()
    engine = SimpleNamespace(store=SimpleNamespace(get=lambda *args:None), jobs={}, account_locks={},
        browser_slots=asyncio.Semaphore(10), amazon=SimpleNamespace(logins={},fingerprint_tests={},context=context))
    pool = pool_type(engine, SimpleNamespace(claimed=lambda id:None))
    slots = asyncio.Semaphore(10)
    async def account(i):
        async with slots:
            async with pool.lease({'id':str(i)}): pass
    started = time.perf_counter()
    await asyncio.gather(*(account(i) for i in range(count)))
    elapsed = time.perf_counter()-started
    await pool.close()
    return {'accounts':count,'elapsed_ms':round(elapsed*1000,2),'peak_parallel_launches':launches['peak']}


async def main():
    original = subprocess.run(['git','show','fc64488:retail/task_groups/browser_pool.py'],
                              check=True,capture_output=True,text=True).stdout
    namespace = {'__package__':'retail.task_groups'}
    exec(compile(original, 'baseline_browser_pool.py', 'exec'), namespace)
    results = {'progress':measure_progress(), 'browser_launch':[]}
    for count in (10, 50, 100):
        results['browser_launch'].append({'accounts':count,
            'before':await measure_pool(namespace['BrowserPool'],count),
            'after':await measure_pool(BrowserPool,count)})
    output = Path('artifacts/task-group-build/redesign-performance.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results,indent=2))


if __name__ == '__main__':
    asyncio.run(main())
