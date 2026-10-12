"""Compare native control resolution with a discovered offer on a local fixture."""
import asyncio
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from patchright.async_api import async_playwright
from retail.amazon import Amazon
from retail import behavior
from retail.interactions import resolve


async def run():
    samples = {'normal':[], 'discovered':[]}
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        page = await browser.new_page()
        markup = '''<h1 id="productTitle">Fixture</h1>
        <div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$20</span></span></div>
        <div id="merchant-info">Ships from and sold by Amazon.com.</div>
        <form onsubmit="event.preventDefault();window.adds=(window.adds||0)+1">
        <input type=hidden name=ASIN value=B012345678><input type=hidden name=offeringID.1 value=fixture-token>
        <button>Add to cart</button></form>'''
        await page.route('**/*',lambda r:r.fulfill(body=markup,content_type='text/html'))
        await page.goto('https://www.amazon.com/dp/B012345678')
        adapter = Amazon(None)
        for n in range(23):
            for mode in (['normal','discovered'] if n%2 else ['discovered','normal']):
                await adapter.read_product(page, {'asin':'B012345678'})
                started = time.perf_counter()
                if mode == 'normal':
                    await behavior.click(page, await resolve(page,'ADD_TO_CART'))
                else:
                    await adapter.click_product_offer(page,'B012345678')
                if n >= 3:
                    samples[mode].append((time.perf_counter()-started)*1000)
        assert await page.evaluate('window.adds', isolated_context=False) == 46
        await browser.close()
    report = {'scope':'20 alternating samples after 3 warmups; standard input, local headless Chromium; control resolution and click only, no retailer network, no order submission.',
              'results':{mode:{'median_ms':round(statistics.median(values),2),
                               'p95_ms':round(sorted(values)[18],2)} for mode,values in samples.items()},
              'samples':samples}
    output=Path('artifacts/offer-discovery');output.mkdir(parents=True,exist_ok=True)
    (output/'benchmark.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report['results'],indent=2))


if __name__ == '__main__':
    asyncio.run(run())
