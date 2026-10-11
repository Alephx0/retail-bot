"""Controlled local browser benchmark; no retailer/provider traffic or purchases.

Alternates baseline/current samples in one warmed browser. Reports nearest-rank
p50/p95/p99 for resolution + three fixture actions, not live checkout latency.
"""
import argparse
import asyncio
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time
import types

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from patchright.async_api import async_playwright
from retail.amazon import Amazon
from retail.browser_mcp import BrowserTools, AMAZON_ACTIONS


def load_previous(ref, file):
    module=types.ModuleType('retail.baseline_'+Path(file).stem)
    module.__package__='retail'
    module.__file__=str(ROOT/file)
    text=subprocess.run(['git','show',ref+':'+file],cwd=ROOT,check=True,capture_output=True).stdout.decode('utf-8')
    exec(compile(text,module.__file__,'exec'),module.__dict__)
    return module


def summarize(values):
    ordered=sorted(values)
    return {**{f'p{p}_ms':round(ordered[math.ceil(len(ordered)*p/100)-1],3) for p in (50,95,99)},
            'mean_ms':round(statistics.mean(values),3),'count':len(values)}


async def benchmark(samples, baseline):
    old=load_previous(baseline,'retail/amazon.py')
    old_tools=load_previous(baseline,'retail/browser_mcp.py')
    values={'baseline':[],'current':[]}
    validation={'baseline':[],'current':[]}
    added_contexts={'baseline':0,'current':0}
    cpu=time.process_time()
    started=time.perf_counter()
    async with async_playwright() as driver:
        browser=await driver.chromium.launch(headless=True)
        pages={}
        adapters={'baseline':old.Amazon(None),'current':Amazon(None)}
        markup="""<button onclick="window.actions=(window.actions||0)+1">Add to cart</button>
        <button onclick="window.actions=(window.actions||0)+1">Proceed to checkout</button>
        <button onclick="window.actions=(window.actions||0)+1">Place your order</button>"""
        for mode in values:
            page=await browser.new_page()
            await page.route('**/*',lambda r:r.fulfill(body=markup,content_type='text/html'))
            await page.goto('https://www.amazon.com/dp/B012345678')
            if mode=='current': await adapters[mode].recovery.install(page)
            pages[mode]=page
        for index in range(samples+5):
            order=['baseline','current'] if index%2 else ['current','baseline']
            for mode in order:
                tick=time.perf_counter()
                for action in ('ADD_TO_CART','BEGIN_CHECKOUT','SUBMIT_ORDER'):
                    node=await adapters[mode].resolve_action(pages[mode],action)
                    await node.click()
                if index>=5: values[mode].append((time.perf_counter()-tick)*1000)
        for mode in values:
            assert await pages[mode].evaluate('window.actions',isolated_context=False)==(samples+5)*3
        # Validation measures removal of synthetic runtime browser replay.
        original=browser.new_context
        mode='baseline'
        async def counted(*args,**kwargs):
            added_contexts[mode]+=1
            return await original(*args,**kwargs)
        browser.new_context=counted
        for mode,factory in [('baseline',old_tools.BrowserTools),('current',BrowserTools)]:
            page=pages[mode]
            await page.set_content('<button>Add item to cart</button>')
            for _ in range(10):
                tools=factory(page,'ADD_TO_CART',{'www.amazon.com'},AMAZON_ACTIONS)
                tick=time.perf_counter()
                await tools.observe_controls()
                assert (await tools.validate_control('1'))['validated']
                validation[mode].append((time.perf_counter()-tick)*1000)
        await browser.close()
    summaries={mode:summarize(v) for mode,v in values.items()}
    ratio=summaries['current']['p95_ms']/summaries['baseline']['p95_ms']
    return {'baseline':baseline,'fast_path':summaries,'p95_change_percent':round((ratio-1)*100,2),
            'accepted':ratio<1.05,'validation':{m:summarize(v) for m,v in validation.items()},
            'validation_new_contexts':added_contexts,'python_cpu_seconds':round(time.process_time()-cpu,3),
            'wall_seconds':round(time.perf_counter()-started,3),
            'limitations':'Local warmed fixtures; no fingerprint setup, live network, provider latency or full transaction engine. Python CPU excludes Chromium/Node. p99 needs larger samples for tail confidence.',
            'samples_ms':values}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',default='d0800a9')
    parser.add_argument('--samples',type=int,default=100)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if not 10<=args.samples<=1000: parser.error('samples must be 10..1000')
    result=asyncio.run(benchmark(args.samples,args.baseline))
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='samples_ms'},indent=2))
