"""Local-only fast-response fixtures comparing state waits to commit 1f637b4.

No retailer/provider requests or real purchases. Measures full fixture actions,
including browser protocol overhead; this is not a live checkout latency promise.
"""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patchright.async_api import async_playwright
from retail.amazon import Amazon


async def benchmark(samples):
    baseline = types.ModuleType('retail.amazon_baseline')
    baseline.__package__ = 'retail'
    baseline.__file__ = str(Path(__file__).resolve().parents[1] / 'retail' / 'amazon.py')
    source = subprocess.run(['git', 'show', '1f637b4:retail/amazon.py'], check=True, capture_output=True).stdout.decode('utf-8')
    exec(compile(source, baseline.__file__, 'exec'), baseline.__dict__)
    values = {}
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True)
        for mode, factory in [('fixed_waits', baseline.Amazon), ('state_waits', Amazon)]:
            durations = {'cart_and_verification': [], 'submit_and_confirmation': []}
            for _ in range(samples):
                page = await browser.new_page()
                carted = False
                async def route(request):
                    nonlocal carted
                    url = request.request.url
                    if '/add-item' in url:
                        carted = True
                        html = 'ok'
                    elif '/gp/cart/' in url:
                        html = '<div id="sc-active-cart">' + ('<div data-asin="B012345678" data-quantity="1">Fixture</div>' if carted else '') + '</div><span id="nav-cart-count">0</span>'
                    else:
                        html = '''<span id="nav-cart-count">0</span><button id="add-to-cart-button"
                            onclick="fetch('/add-item').then(()=>document.querySelector('#nav-cart-count').textContent='1')">Add to cart</button>'''
                    await request.fulfill(content_type='text/html', body=html)
                await page.route('**/*', route)
                await page.goto('https://www.amazon.com/dp/B012345678')
                adapter = factory(None)
                started = time.perf_counter()
                assert await adapter.cart(page, 1, 'B012345678') == 1
                durations['cart_and_verification'].append(round((time.perf_counter() - started) * 1000, 2))
                await page.goto('https://www.amazon.com/checkout/spc')
                await page.set_content('''<div id="spc-orders"><div data-asin="B012345678" data-quantity="1" data-seller="Amazon.com" data-condition="new"><span class="a-price"><span class="a-offscreen">$20.00</span></span></div></div>
                    <table id="subtotals-marketplace-table"><tr><td>Order total:</td><td>$20.00</td></tr></table>
                    <button onclick="this.insertAdjacentHTML('afterend','<h1>Order placed 123-1234567-1234567</h1>')">Place your order</button>''')
                await adapter.checkout_snapshot(page, 'B012345678', 1, 25)
                started = time.perf_counter()
                await adapter.submit_order(page)
                assert await adapter.confirmation(page) == '123-1234567-1234567'
                durations['submit_and_confirmation'].append(round((time.perf_counter() - started) * 1000, 2))
                await page.close()
            values[mode] = {key: {'median_ms': statistics.median(times), 'samples_ms': times} for key, times in durations.items()}
        await browser.close()
    return {'note': __doc__, 'results': values}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.samples <= 20:
        parser.error('samples must be between 1 and 20')
    result = json.dumps(asyncio.run(benchmark(args.samples)), indent=2)
    if args.output:
        args.output.write_text(result, encoding='utf-8')
    print(result)
