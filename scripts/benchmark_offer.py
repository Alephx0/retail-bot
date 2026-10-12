"""Alternate offer reads on controlled pages; no retailer/provider traffic."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from patchright.async_api import async_playwright
from retail.amazon import Amazon
from retail.interactions import InteractionError
from scripts.benchmark_recovery import load_previous, summarize


async def run(samples,trace):
    previous=load_previous('82cf59a','retail/amazon.py').Amazon
    results={}
    async def no_recovery(*args,**kwargs):
        raise InteractionError('Controlled fixture: no external recovery')
    async with async_playwright() as driver:
        browser=await driver.chromium.launch()
        context=await browser.new_context()
        if trace:await context.tracing.start(screenshots=True,snapshots=True,sources=False)
        page=await context.new_page()
        await page.route('**/*',lambda r:r.fulfill(body='<body></body>',content_type='text/html'))
        await page.goto('https://www.amazon.com/dp/B012345678')
        adapters={'baseline':previous(None),'current':Amazon(None)}
        for a in adapters.values():a.resolve_action=no_recovery
        for layout,markup in {
            'normal':'<div id="merchantInfoFeature_feature_div"><span class="offer-display-feature-text-message">Amazon.com</span></div><button id="add-to-cart-button">Add to cart</button>',
            'legacy_seller':'<div id="merchant-info">Ships from and sold by Amazon.com.</div><button>Add to cart</button>',
            'out_of_stock':'<div id="merchant-info">Sold by Amazon.com</div><div id="availability">Currently unavailable</div>',
        }.items():
            await page.set_content('<span id="productTitle">Fixture</span><div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$20.00</span></span></div>'+markup)
            times={mode:[] for mode in adapters}
            for i in range(samples+3):
                products={}
                for mode in (['baseline','current'] if i%2 else ['current','baseline']):
                    start=time.perf_counter()
                    products[mode]=await adapters[mode].read_product(page,{'asin':'B012345678'})
                    duration=(time.perf_counter()-start)*1000
                    if i>=3:times[mode].append(duration)
                for product in products.values():
                    product.pop('agent_error',None)
                    product.pop('offer_id_status',None)
                    product.pop('use_offer_id',None)
                assert products['baseline']==products['current'],(layout,products)
            results[layout]={mode:summarize(values) for mode,values in times.items()}
        if trace:await context.tracing.stop()
        await browser.close()
    return {'samples':samples,'trace':trace,'baseline':'82cf59a','scope':'Offer read only; excludes navigation, login and checkout. Local intercepted pages; no model calls.', 'results':results}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples',type=int,default=25)
    parser.add_argument('--trace',action='store_true')
    parser.add_argument('--output',default='artifacts/offer-speed/benchmark.json')
    args=parser.parse_args()
    if not 1<=args.samples<=100:parser.error('samples must be between 1 and 100')
    result=asyncio.run(run(args.samples,args.trace))
    path=Path(args.output)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
