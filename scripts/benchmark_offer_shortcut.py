"""Measure local full carting, excluding initial product/account verification."""
import asyncio
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from patchright.async_api import async_playwright
from retail.amazon import Amazon
from test_offer_shortcut import DirectFixture, TARGET


async def run(output_name):
    results = {}
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        for paced in (False, True):
            context = await browser.new_context()
            context._retail_paced_input = paced
            fixture = DirectFixture()
            dispatched = []
            async def observed_route(route):
                if route.request.method == 'POST':
                    dispatched.append(time.perf_counter())
                await fixture.route(route)
            await context.route('**/*', observed_route)
            samples = {'native': [], 'direct': [], 'endpoint': []}
            dispatch_samples = {'native': [], 'direct': [], 'endpoint': []}
            for run in range(14):
                modes = ['native', 'direct', 'endpoint']
                for mode in modes[run % 3:] + modes[:run % 3]:
                    page = await context.new_page()
                    await page.goto(f'https://www.amazon.com/dp/{TARGET}')
                    adapter = Amazon(None)
                    await adapter.read_product(page, {'asin': TARGET, 'offer_id_shortcut': mode != 'native',
                                                     'offer_id_endpoint': mode == 'endpoint'})
                    before = time.perf_counter()
                    await adapter.cart(page, 1, TARGET)
                    elapsed = (time.perf_counter() - before) * 1000
                    if run >= 2:
                        samples[mode].append(elapsed)
                        dispatch_samples[mode].append((dispatched[-1] - before) * 1000)
                    await page.close()
            assert len(fixture.posts) == 42 and fixture.cart_visits == 42
            results['paced' if paced else 'standard'] = {
                name: {'median_ms':round(statistics.median(values),2),
                       'p95_ms':round(sorted(values)[-1],2), 'samples_ms': values,
                       'dispatch_median_ms':round(statistics.median(dispatch_samples[name]),2),
                       'dispatch_samples_ms':dispatch_samples[name]}
                for name, values in samples.items()}
            await context.close()
        await browser.close()
    report = {'scope':'12 alternating measured samples per route/input mode after 2 warmups. Local intercepted POST acknowledgement plus cart navigation and quantity/offer verification. Excludes initial page/account verification, real retailer latency, backend persistence, checkout and order submission.',
              'results':results}
    output = ROOT / 'artifacts/offer-shortcut'
    output.mkdir(parents=True, exist_ok=True)
    (output / output_name).write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({mode:{name:{key:value for key,value in data.items() if not key.endswith('samples_ms')}
        for name,data in values.items()} for mode,values in results.items()},indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='benchmark.json')
    asyncio.run(run(parser.parse_args().output))
