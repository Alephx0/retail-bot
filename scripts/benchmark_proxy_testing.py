"""Controlled async latency benchmark; no real proxy or retailer traffic."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from retail import proxy_testing
from retail.proxy_protocol import parse_proxy
from retail.services import ProxyHealth
from retail.store import Store, now


async def sample(concurrency):
    with tempfile.TemporaryDirectory() as folder:
        store=Store(Path(folder));store.put('settings',{'proxy_concurrency':concurrency},'settings')
        record=store.put('proxies',{'name':'Fixture','entries':'\n'.join(f'fixture{i}:8080' for i in range(100))})
        service=ProxyHealth(store);ticks=[]
        async def measured(line,*args,**kwargs):
            await asyncio.sleep(.02)
            return {**parse_proxy(line).public(),'status':'healthy','latency_ms':20,'observed_at':now()}
        async def heartbeat():
            last=time.perf_counter()
            while service.jobs:
                await asyncio.sleep(.005);tick=time.perf_counter();ticks.append((tick-last)*1000);last=tick
        original=proxy_testing.probe;proxy_testing.probe=measured
        tracemalloc.start();cpu=time.process_time();start=time.perf_counter()
        try:
            await service.start(record,'amazon');await asyncio.gather(*service.jobs.values(),heartbeat())
            return {'concurrency':concurrency,'count':100,'elapsed_ms':round((time.perf_counter()-start)*1000,2),
                'cpu_ms':round((time.process_time()-cpu)*1000,2),'python_peak_bytes':tracemalloc.get_traced_memory()[1],
                'heartbeat_max_ms':round(max(ticks),2),'completed':len(store.all('proxy_endpoints'))}
        finally:
            tracemalloc.stop();proxy_testing.probe=original;store.db.close()


async def main():
    report={'fixture':'100 synthetic 20ms probes; real encrypted persistence, no browser or network',
            'samples':[await sample(c) for c in (1,5,10)]}
    target=Path('artifacts/overhaul/proxy-performance.json');target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))

if __name__=='__main__':asyncio.run(main())
