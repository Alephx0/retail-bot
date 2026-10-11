"""Alternate old/new cart flows on local fixtures; never contact a retailer."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from patchright.async_api import async_playwright
from retail.amazon import Amazon
from scripts.benchmark_recovery import load_previous, summarize
from scripts.cart_fixture import CartFixture, TARGET


async def run(samples, latency):
    baseline = load_previous('f479afc', 'retail/amazon.py').Amazon
    result = {}
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        for scenario in ('empty', 'unrelated', 'existing_target'):
            result[scenario] = {}
            pages, adapters, values = {}, {}, {}
            for name, factory in [('baseline', baseline), ('current', Amazon)]:
                pages[name] = await browser.new_page()
                adapters[name] = factory(None)
                values[name] = {'cart': [], 'first_add': [], 'navigations': [], 'adds': []}
            for i in range(samples + 3):
                for name in (['baseline', 'current'] if i % 2 else ['current', 'baseline']):
                    page = pages[name]
                    fixture = CartFixture(quantity=1 if scenario == 'existing_target' else 0,
                                          unrelated=1 if scenario == 'unrelated' else 0, latency=latency)
                    await page.unroute('**/*')
                    await page.route('**/*', fixture.route)
                    await page.goto(f'https://www.amazon.com/dp/{TARGET}')
                    fixture.navigations = 0
                    start = time.perf_counter()
                    assert await adapters[name].cart(page, 1, TARGET) == 1
                    elapsed = (time.perf_counter() - start) * 1000
                    assert fixture.items == {TARGET: 1}
                    if i >= 3:
                        values[name]['cart'].append(elapsed)
                        values[name]['navigations'].append(fixture.navigations)
                        values[name]['adds'].append(fixture.events.count('add'))
                        if fixture.first_add:
                            values[name]['first_add'].append((fixture.first_add-start)*1000)
            for name, measurements in values.items():
                result[scenario][name] = {key: summarize(value) if key in ('cart', 'first_add') and value else value
                                         for key, value in measurements.items()}
                await pages[name].close()
        await browser.close()
    return {'samples': samples, 'fixture_document_latency_ms': latency*1000, 'results': result}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=25)
    parser.add_argument('--latency-ms', type=float, default=100)
    parser.add_argument('--output', default='artifacts/cart-speed/benchmark.json')
    args = parser.parse_args()
    result = asyncio.run(run(args.samples, args.latency_ms/1000))
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
