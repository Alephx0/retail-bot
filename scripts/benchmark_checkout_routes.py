"""Compare cart and Buy Now up to verified review on intercepted pages only."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from patchright.async_api import async_playwright
from retail.amazon import Amazon
from scripts.benchmark_recovery import summarize

ASIN='B012345678'
PRODUCT='''<span id="productTitle">Fixture product</span>
<button id="add-to-cart-button" onclick="location.href='/gp/cart/view.html'">Add to cart</button>
<button id="buy-now-button" onclick="location.href='/checkout/review'">Buy Now</button>'''
CART=f'''<div id="sc-active-cart"><div data-asin="{ASIN}" data-quantity="1">
<span class="sc-product-title">Fixture product</span></div></div>
<button onclick="location.href='/checkout/review'">Proceed to checkout</button>'''
REVIEW=f'''<div id="spc-orders"><div data-asin="{ASIN}" data-quantity="1"
data-seller="Amazon.com" data-condition="new">Fixture product
<span class="a-price"><span class="a-offscreen">$20.00</span></span></div></div>
<table id="subtotals-marketplace-table"><tr><td>Order total:</td><td>$20.00</td></tr></table>
<button onclick="window.unwanted=true">Place your order</button>'''


async def run(samples,network_ms):
    values={'cart':[],'buy_now':[]}
    navigations={key:[] for key in values}
    async with async_playwright() as driver:
        browser=await driver.chromium.launch()
        page=await browser.new_page()
        requests=[]
        async def route(r):
            path=urlparse(r.request.url).path
            bodies={'/dp/'+ASIN:PRODUCT,'/gp/cart/view.html':CART,'/checkout/review':REVIEW}
            assert path in bodies, 'Unexpected request from controlled fixture'
            requests.append(path)
            await asyncio.sleep(network_ms/1000)
            await r.fulfill(body=bodies[path],content_type='text/html')
        await page.route('**/*',route)
        adapter=Amazon(None)
        for sample in range(samples+2):
            for mode in (['cart','buy_now'] if sample%2 else ['buy_now','cart']):
                await page.goto('https://www.amazon.com/dp/'+ASIN)
                requests.clear()
                start=time.perf_counter()
                if mode=='buy_now':
                    assert await adapter.buy_now(page,1,ASIN)
                else:
                    assert await adapter.cart(page,1,ASIN)==1
                    await adapter.prepare_checkout(page,ASIN,1)
                snapshot=await adapter.checkout_snapshot(page,ASIN,1,25,max_unit_price=20)
                duration=(time.perf_counter()-start)*1000
                assert snapshot['total']==20 and snapshot['quantity']==1
                assert not await page.evaluate('Boolean(window.unwanted)',isolated_context=False)
                if sample>=2:
                    values[mode].append(duration)
                    navigations[mode].append(len(requests))
        await browser.close()
    return {'samples':samples,'synthetic_document_delay_ms':network_ms,
            'to_verified_review':{key:summarize(v) for key,v in values.items()},
            'document_requests':navigations,
            'scope':'Verified product to verified review; no order submission, real network, input pacing, login or offer read.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples',type=int,default=10)
    parser.add_argument('--network-ms',type=float,default=100)
    args=parser.parse_args()
    if not 1<=args.samples<=100 or not 0<=args.network_ms<=1000:parser.error('Invalid benchmark bounds')
    result=asyncio.run(run(args.samples,args.network_ms))
    path=ROOT/'artifacts/readiness/checkout-routes.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
