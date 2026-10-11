"""Controlled cart server for tests/benchmarks. All browser requests are intercepted."""
import asyncio
import json
import time
from urllib.parse import urlparse, parse_qs

TARGET = 'B012345678'
OTHER = 'B000000001'


class CartFixture:
    def __init__(self, *, quantity=0, unrelated=0, latency=0, direct=False,
                 delay=0, stall='', duplicates=False):
        self.items = ({TARGET: quantity} if quantity else {}) | ({OTHER: unrelated} if unrelated else {})
        self.latency, self.direct, self.delay = latency, direct, delay
        self.stall, self.duplicates = stall, duplicates
        self.events = []
        self.navigations = 0
        self.first_add = None

    async def route(self, route):
        path = urlparse(route.request.url).path
        query = parse_qs(urlparse(route.request.url).query)
        if route.request.is_navigation_request():
            self.navigations += 1
            if self.latency:
                await asyncio.sleep(self.latency)  # Controlled network cost, fixture only.
        if path == '/fixture/add':
            self.events.append('add')
            self.first_add = time.perf_counter()
            self.items[TARGET] = self.items.get(TARGET, 0) + int(query['qty'][0])
            return await route.fulfill(json={'count': sum(self.items.values())})
        if path in ('/fixture/delete', '/fixture/save'):
            asin = query['asin'][0]
            self.events.append('delete' if path.endswith('delete') else 'save')
            assert asin == OTHER, 'The target must never be deleted'
            self.items.pop(asin, None)
            return await route.fulfill(body='ok')
        if path == '/fixture/quantity':
            self.events.append('quantity')
            self.items[TARGET] = int(query['qty'][0])
            return await route.fulfill(body='ok')
        if path.startswith('/gp/cart/'):
            rows = []
            for asin, qty in self.items.items():
                controls = (f'''<select name="quantity" onchange="fetch('/fixture/quantity?qty='+this.value).then(()=>setTimeout(()=>this.parentElement.dataset.quantity=this.value,{self.delay}))">
                    {''.join(f'<option value="{n}" {"selected" if n == qty else ""}>{n}</option>' for n in range(1, 31))}</select>''') if asin == TARGET else f'''
                    <button name="submit.delete.fixture" onclick="fetch('/fixture/delete?asin={asin}').then(()=>{{{'void 0' if self.stall == 'delete' else f'setTimeout(()=>this.parentElement.remove(),{self.delay})'}}})">Delete</button>
                    <button name="submit.save-for-later.fixture" onclick="fetch('/fixture/save?asin={asin}').then(()=>{{document.querySelector('#sc-saved-cart').append(this.parentElement);document.querySelector('#nav-cart-count').textContent=Number(document.querySelector('#nav-cart-count').textContent)-{qty}}})">Save for later</button>'''
                rows.append(f'<div data-asin="{asin}" data-quantity="{qty}"><span class="sc-product-title">Fixture {asin}</span>{controls}</div>')
            if self.duplicates and rows:
                rows.append(rows[0])
            markup = ''.join(rows)
            body = f'<span id="nav-cart-count">{sum(self.items.values())}</span><div id="sc-active-cart">{markup if not self.delay else ""}</div><div id="sc-saved-cart"></div>'
            if self.delay:
                body += f'<script>setTimeout(()=>document.querySelector("#sc-active-cart").innerHTML={json.dumps(markup)},{self.delay})</script>'
        else:
            ack = "location.href='/gp/cart/view.html'" if self.direct else "document.querySelector('#nav-cart-count').textContent=data.count;document.querySelector('#sw-atc-confirmation').textContent='Added to cart'"
            if self.stall == 'add':
                ack = 'void 0'
            body = f'''<span id="nav-cart-count">{sum(self.items.values())}</span>
                <select id="quantity">{''.join(f'<option value="{n}">{n}</option>' for n in range(1,31))}</select>
                <button id="add-to-cart-button" onclick="fetch('/fixture/add?qty='+document.querySelector('#quantity').value).then(r=>r.json()).then(data=>setTimeout(()=>{{{ack}}},{self.delay}))">Add to cart</button>
                <div id="sw-atc-confirmation"></div>'''
        await route.fulfill(body=body, content_type='text/html')
