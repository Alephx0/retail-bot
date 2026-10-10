"""Compare full inspection and inventory scanning on local browser fixtures.

All requests are intercepted. AI recovery is a controlled 250 ms failed lookup;
it does not call a provider. Results are not retailer network latency estimates.
"""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patchright.async_api import async_playwright
from retail.amazon import Amazon
from retail.interactions import InteractionError
from retail.performance import Performance
from retail.store import Store


async def benchmark(samples):
    results = {}
    with tempfile.TemporaryDirectory(prefix='retail-monitor-benchmark-') as folder:
        store = Store(Path(folder))
        store.put('settings', {'agent_mode': 'recovery'}, 'settings')
        adapter = Amazon(store)
        async def recovery(*args, **kwargs):
            await asyncio.sleep(.25)
            raise InteractionError('Controlled recovery failure')
        adapter.agent.resolve = recovery
        recorder = Performance()
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            for name, content in {
                'restricted_in_stock': '<div id="availability">In Stock</div><div id="rightCol">Sign in to get started</div><button>Cart</button>',
                'out_of_stock': '<div id="availability">Currently unavailable</div><button>Cart</button>',
                'normal_offer': '<div id="availability">In Stock</div><button id="add-to-cart-button">Add to cart</button>',
            }.items():
                values = {'full_inspection': [], 'inventory_scan': []}
                for sample in range(samples):
                    methods = [('full_inspection', adapter.inspect), ('inventory_scan', adapter.inspect_stock)]
                    for mode, inspect in (methods if sample % 2 == 0 else methods[::-1]):
                        page = await browser.new_page()
                        html = '<span id="productTitle">Fixture</span><div id="merchant-info">Sold by Amazon.com</div>' + content
                        await page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
                        started = time.perf_counter()
                        with recorder.scope(mode, name):
                            product = await inspect(page, {'asin': 'B07ZLF9WQ5'}, 'US')
                        assert product['availability_status'] == ('unavailable' if name == 'out_of_stock' else 'available')
                        values[mode].append(round((time.perf_counter() - started) * 1000, 2))
                        await page.close()
                results[name] = {mode: {'samples_ms': times, 'median_ms': statistics.median(times),
                                        'stages': recorder.snapshot(mode, name)['summary']}
                                 for mode, times in values.items()}
            await browser.close()
        store.db.close()
    return {'note': __doc__, 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.samples <= 20:
        parser.error('samples must be between 1 and 20')
    print(json.dumps(asyncio.run(benchmark(args.samples)), indent=2))
