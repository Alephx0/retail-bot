"""Controlled restock-to-cart benchmark: mocked retailer, no live transactions."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_monitors import fixture,task_record,until
from scripts.benchmark_recovery import summarize


async def run(samples,cost):
    values={'resource_saving':[],'ready':[]}
    counts={name:[] for name in values}
    for sample in range(samples):
        for ready in ([False,True] if sample%2 else [True,False]):
            with tempfile.TemporaryDirectory() as folder:
                store,group,account,engine=fixture(Path(folder))
                store.put('settings',{'keep_accounts_ready':ready},'settings')
                create,verify,cart=engine.amazon.context,engine.amazon.ensure_session,engine.amazon.cart
                arrivals=[]
                async def context(identity,*args):
                    if identity['id']==account['id']:await asyncio.sleep(cost)
                    return await create(identity,*args)
                async def session(*args):
                    await asyncio.sleep(cost)
                    return await verify(*args)
                async def added(*args):
                    arrivals.append(time.perf_counter())
                    return await cart(*args)
                engine.amazon.context=context;engine.amazon.ensure_session=session;engine.amazon.cart=added
                task=task_record(store,group,account,use_account_proxy=True,monitor_asin='B012345678')
                try:
                    await engine.start(task['id'])
                    await until(lambda:bool(engine.ready_accounts.get(task['id'],{}).get('idle')) if ready else
                        store.get('tasks',task['id'])['status']=='waiting' and not engine.account_locks[account['id']].locked())
                    before=len(engine.amazon.authentications)
                    engine.amazon.stock.add('B012345678')
                    monitor=next(iter(engine.monitors.active.values()))
                    monitor.product['available']=True;monitor.sequence+=1
                    start=time.perf_counter();monitor.update('in_stock','Controlled restock')
                    await until(lambda:task['id'] not in engine.jobs)
                    assert len(arrivals)==1 and store.get('tasks',task['id'])['status']=='completed'
                    key='ready' if ready else 'resource_saving'
                    values[key].append((arrivals[0]-start)*1000)
                    counts[key].append(len(engine.amazon.authentications)-before)
                finally:
                    await engine.close();store.db.close()
    return {'samples':samples,'synthetic_setup_ms':cost*1000,'synthetic_verification_ms':cost*1000,
            'restock_to_cart':{k:summarize(v) for k,v in values.items()},'post_signal_verifications':counts,
            'note':'Synthetic setup costs; actual account offer read still runs. Not live retailer latency.'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--samples',type=int,default=10)
    p.add_argument('--cost-ms',type=float,default=100)
    a=p.parse_args()
    if not 1<=a.samples<=100 or not 0<=a.cost_ms<=1000:p.error('Invalid benchmark bounds')
    result=asyncio.run(run(a.samples,a.cost_ms/1000))
    path=ROOT/'artifacts/readiness/benchmark.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
