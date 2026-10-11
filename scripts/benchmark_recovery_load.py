"""Controlled concurrent account contexts; provider latency is simulated, never billed."""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from patchright.async_api import async_playwright
from retail.amazon import Amazon
from retail.browser_recovery import Budgets
from retail.interactions import InteractionError
from scripts.benchmark_recovery import summarize


def python_memory():
    if os.name!='nt': return None
    class Counters(ctypes.Structure):
        _fields_=[('cb',ctypes.c_ulong),('faults',ctypes.c_ulong)]+[(n,ctypes.c_size_t) for n in
            ('peak','working','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile')]
    kernel=ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype=ctypes.c_void_p
    value=Counters(); value.cb=ctypes.sizeof(value)
    fn=ctypes.windll.psapi.GetProcessMemoryInfo
    fn.argtypes=[ctypes.c_void_p,ctypes.POINTER(Counters),ctypes.c_ulong]
    if not fn(kernel.GetCurrentProcess(),ctypes.byref(value),value.cb): return None
    return round(value.working/1024/1024,2)


async def main():
    report=[]
    async with async_playwright() as driver:
        browser=await driver.chromium.launch()
        for count in (2,8,16):
            adapter=Amazon(None)
            adapter.recovery.budgets=Budgets(total=1.0,structural=.5,queue=.05,minimum_ai=.01)
            pages=[]
            for index in range(count):
                context=await browser.new_context()
                context._retail_task_id=str(index)
                await context.route('**/*',lambda r:r.fulfill(body='<button>Add to cart</button>',content_type='text/html'))
                page=await context.new_page()
                await page.goto('https://www.amazon.com/dp/B012345678')
                await page.evaluate('value=>sessionStorage.setItem("owner",value)',str(index),isolated_context=False)
                pages.append(page)
            healthy=[]; results=[]; peak=0
            class SlowProvider:
                async def resolve(self,*args):
                    await asyncio.sleep(.3)  # Controlled provider latency, not production pacing.
                    raise InteractionError('Fixture provider unavailable')
            async def work(index,page):
                nonlocal peak
                begin=time.perf_counter()
                if index%2:
                    await page.set_content('<button>Unsupported action</button>')
                    try: await adapter.recovery.resolve(page,'ADD_TO_CART',SlowProvider(),allow_ai=True)
                    except InteractionError: results.append('attention')
                else:
                    for _ in range(3):
                        await adapter.resolve_action(page,'ADD_TO_CART')
                    healthy.append((time.perf_counter()-begin)*1000)
                    results.append('healthy')
                peak=max(peak,len(adapter.recovery.active))
                assert await page.evaluate('sessionStorage.getItem("owner")',isolated_context=False)==str(index)
            start=time.perf_counter(); cpu=time.process_time(); memory=python_memory()
            await asyncio.gather(*(work(i,p) for i,p in enumerate(pages)))
            assert len(browser.contexts)==count and not adapter.recovery.active and not adapter.recovery.slots.locked()
            report.append({'contexts':count,'healthy':results.count('healthy'),'attention':results.count('attention'),
                'healthy_three_reads':summarize(healthy),'wall_ms':round((time.perf_counter()-start)*1000,2),
                'python_cpu_seconds':round(time.process_time()-cpu,3),'python_working_set_mb_before':memory,
                'python_working_set_mb_after':python_memory(),'recovery_counts':adapter.recovery.counts})
            await asyncio.gather(*(p.context.close() for p in pages))
            assert not browser.contexts
        await browser.close()
    value={'cases':report,'limitations':'One controlled sample per context; not a statistical p95/p99 claim. Independent sessionStorage verified. Python memory/CPU only; excludes Chromium and Node. Simulated 300ms provider failure.'}
    path=ROOT/'artifacts/recovery-overhaul/load.json'; path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2),encoding='utf-8')
    print(json.dumps(value,indent=2))

if __name__=='__main__': asyncio.run(main())
