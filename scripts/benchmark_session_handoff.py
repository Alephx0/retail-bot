"""Local session + offer comparison; no authentication or retailer requests."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from patchright.async_api import async_playwright
from retail.amazon import Amazon
from retail.store import Store
from scripts.benchmark_recovery import load_previous, summarize

TARGET = 'https://www.amazon.com/dp/B012345678'


async def run(samples, latency):
    old = load_previous('d0d4660', 'retail/amazon.py').Amazon
    results = {}
    with tempfile.TemporaryDirectory() as directory:
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            for decorated in (False, True):
                label = 'canonical_query' if decorated else 'exact_url'
                measurements = {name: {'durations': [], 'navigations': []} for name in ('baseline', 'current')}
                for i in range(samples+3):
                    for name, factory in ([('baseline',old),('current',Amazon)] if i%2 else [('current',Amazon),('baseline',old)]):
                        store = Store(Path(directory)/name)
                        account = store.put('accounts', {'name':'Fixture','region':'US'}, 'fixture')
                        adapter = factory(store)
                        page = await browser.new_page()
                        count = 0
                        async def route(r):
                            nonlocal count
                            count += 1
                            if latency:
                                await asyncio.sleep(latency)  # Fixture network model only.
                            script = "history.replaceState(null,'','/Fixture/dp/B012345678?th=1&psc=1');" if decorated else ''
                            await r.fulfill(content_type='text/html',body=f'''<script>{script}</script>
                                <div id="nav-link-accountList"><span class="nav-line-1">Hello Fixture</span></div>
                                <span id="productTitle">Fixture product</span><button>Add to cart</button>''')
                        await page.route('**/*',route)
                        page._retail_start_url = TARGET
                        start = time.perf_counter()
                        await adapter.ensure_session(page.context, account, page)
                        await adapter.inspect(page, {'asin':'B012345678'}, 'US')
                        elapsed = (time.perf_counter()-start)*1000
                        assert count == (3 if decorated and name == 'baseline' else 1)
                        if i >= 3:
                            measurements[name]['durations'].append(elapsed)
                            measurements[name]['navigations'].append(count)
                        await page.context.close()
                        store.db.close()
                results[label] = {name:{'timing':summarize(value['durations']),'navigations':value['navigations']} for name,value in measurements.items()}
            await browser.close()
    return {'samples':samples,'fixture_navigation_latency_ms':latency*1000,'results':results}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--samples',type=int,default=25)
    parser.add_argument('--latency-ms',type=float,default=100)
    parser.add_argument('--output',default='artifacts/session-speed/benchmark.json')
    args=parser.parse_args()
    result=asyncio.run(run(args.samples,args.latency_ms/1000))
    path=Path(args.output)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
