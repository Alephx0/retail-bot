import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from patchright.async_api import async_playwright, TimeoutError as BrowserTimeoutError

from retail.account_care import BrowsingRoutine
from retail.amazon import Amazon
from retail.care_agent import CareAgent
from retail.care_product import ProductExplorer
from test_account_care import fixture, until


@pytest.mark.parametrize('mode', ['ajax', 'inline_radio', 'navigation', 'delayed', 'anchor_details', 'partial_details', 'missing_details', 'dropdown', 'dropdown_disabled', 'missing_transition',
                                 'cancel_reading', 'cancel_transition', 'backoff', 'backoff_ajax', 'changed_control', 'page_limit'])
def test_product_exploration_through_mcp(tmp_path, monkeypatch, mode):
    async def scenario():
        store, engine, (account, other) = fixture(tmp_path)
        engine.amazon = Amazon(store)
        care = engine.account_care
        connection = store.put('ai_connections', {'name': 'Fixture', 'model': 'fixture'})
        care.save(account['id'], BrowsingRoutine(mode='ai', interests='Coffee mugs', ai_connection_id=connection['id'], visit_limit=1))
        requests, events, observations, outputs, pauses = [], [], [], [], []
        reading, changing = asyncio.Event(), asyncio.Event()

        class Provider:
            calls = 0
            async def turn(self, instructions, history, tools):
                observation = json.loads(history[-1]['content'].split(': ', 1)[1])
                observations.append(observation)
                self.calls += 1
                if self.calls == 1:
                    name, args = 'open_product', {'ref': 'p1'}
                elif mode == 'page_limit':
                    name, args = 'open_product', {'ref': 'p1'}
                else:
                    if mode=='inline_radio' and self.calls==2:
                        with pytest.raises(BrowserTimeoutError, match='intercepts pointer events'):
                            await care.runs[account['id']]['page'].locator('#variation_color_name .a-button-text').first.click(trial=True,timeout=600)
                    kind = ['variant', 'details', 'reviews', 'variant'][self.calls-2]
                    option = next(x for x in observation['product_interactions'] if x['kind']==kind)
                    name, args = 'inspect_product', {'ref': option['ref']}
                    if mode == 'changed_control':
                        await care.runs[account['id']]['page'].locator('#blue a').evaluate("e=>e.href='/gp/cart/add.html'")
                return [{'id': str(self.calls), 'name': name, 'arguments': json.dumps(args)}]

            def tool_result(self, history, call, result):
                outputs.append(result)
                history.append({'role': 'user', 'content': json.dumps(result)})

        provider = Provider()
        monkeypatch.setattr(CareAgent, 'provider_factory', staticmethod(lambda connection: provider))
        async def pause(self, seconds):
            assert 3 <= seconds <= 8
            pauses.append(seconds)
            if mode == 'cancel_reading':
                reading.set()
                await asyncio.Event().wait()
        monkeypatch.setattr(ProductExplorer, 'read_pause', pause)
        care.dwell = AsyncMock()  # Navigation reading is covered by account-care tests.

        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            async def context(account, **kwargs):
                context = await browser.new_context(viewport={'width': 1100, 'height': 800})
                await context.expose_function('recordEvent', lambda value: events.append(value))
                await context.expose_function('variantStarted', changing.set)
                async def route(route):
                    path = urlsplit(route.request.url).path
                    requests.append((route.request.method, path))
                    if mode == 'backoff' and path.endswith('B000000002') or mode=='backoff_ajax' and path=='/variant-options':
                        await route.fulfill(status=429, headers={'Retry-After': '1800'}, body='Retailer limit')
                        return
                    asin = path.rsplit('/', 1)[-1] if '/dp/' in path else 'B000000001'
                    document = '''<html><body style="margin:30px">
                        <div id="nav-link-accountList"><span class="nav-line-1">Hello, Private Buyer</span></div>
                        <p>private-account@example.test</p>
                        <a href="/dp/B000000001">Coffee mug</a>
                        <h1 id="productTitle">Coffee mug</h1><input type="hidden" id="ASIN" value="ASIN_VALUE">
                        <ul id="variation_color_name" style="display:flex;gap:30px;padding:20px">
                          <li id="blue" data-asin="B000000002"><a href="/dp/B000000002">Blue</a></li>
                          <li data-asin="B000000003"><a href="/dp/B000000003">Green</a></li>
                          <li data-asin="B000000004"><a href="/dp/B000000004">Red</a></li>
                          <li data-asin="B000000005" data-dp-url="/dp/B000000005"><a href="/gp/cart/add.html">Cart trap</a></li>
                          <li class="swatchUnavailable" data-asin="B000000006"><a href="/dp/B000000006">Unavailable</a></li>
                          <li data-asin="B000000007"><a href="https://example.test/dp/B000000007">External trap</a></li>
                        </ul>
                        <div id="productOverview_feature_div"><div class="a-expander-container">
                          <a href="#" class="a-expander-header" aria-expanded="false" onclick="event.preventDefault();this.setAttribute('aria-expanded','true');this.nextElementSibling.style.display='block';recordEvent('details');">See more product details</a>
                          <div class="a-expander-content" style="display:none">Dishwasher-safe ceramic, 12 ounces.</div>
                        </div></div>
                        <div style="height:2600px"></div>
                        <section id="customerReviews" style="height:900px"><h2>Customer reviews</h2>
                          <span id="acrPopover" title="4.6 out of 5 stars"></span><span id="acrCustomerReviewText">123 ratings</span>
                          <p>Private Reviewer Name</p><p>Public review content</p>
                          <button onclick="fetch('/purchase',{method:'POST'})">Buy now</button>
                        </section><div style="height:900px"></div>
                        <script>
                        document.addEventListener('click',e=>recordEvent('click:'+e.target.textContent));
                        document.addEventListener('mousemove',()=>recordEvent('move'));
                        document.addEventListener('wheel',()=>recordEvent('wheel'));
                        document.querySelectorAll('#variation_color_name li').forEach(li=>{
                          if(li.dataset.asin===document.querySelector('#ASIN').value)li.classList.add('swatchSelect');
                        });
                        VARIANT_SCRIPT
                        </script></body></html>'''.replace('ASIN_VALUE', asin)
                    script = ''
                    if mode not in {'navigation', 'backoff'}:
                        update = "document.querySelectorAll('li').forEach(e=>e.classList.remove('swatchSelect'));li.classList.add('swatchSelect');document.querySelector('#ASIN').value=li.dataset.asin;"
                        if mode in {'missing_transition', 'cancel_transition', 'backoff_ajax'}:
                            update = "fetch('/variant-options')" if mode=='backoff_ajax' else ''
                        if mode == 'delayed':
                            update = 'setTimeout(()=>{'+update+'},250);'
                        script = "document.querySelectorAll('#variation_color_name li a').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();variantStarted();const li=a.parentElement;"+update+"}));"
                    document = document.replace('VARIANT_SCRIPT', script)
                    if mode == 'inline_radio':
                        start,end=document.index('<ul id="variation_color_name"'),document.index('<div id="productOverview_feature_div">')
                        swatches=''.join(f'''<li data-asin="{value}" style="position:relative;width:130px;height:60px" title="{label}">
                          <span class="a-button"><input type="submit" role="radio" aria-checked="false" style="position:absolute;inset:0;opacity:0;z-index:2;width:100%;height:100%" onclick="document.querySelectorAll('[role=radio]').forEach(e=>e.setAttribute('aria-checked','false'));this.setAttribute('aria-checked','true');recordEvent('variant-change')"><span class="a-button-text">{label}</span></span>
                          <span hidden><input role="radio" aria-checked="true"><span class="a-button-text">Fallback</span></span></li>''' for value,label in [('B000000002','Matte Black'),('B000000003','Green'),('B000000004','Red')])
                        document=document[:start]+'<ul id="variation_color_name" style="display:flex;gap:30px">'+swatches+'</ul>'+document[end:]
                    if mode in {'dropdown', 'dropdown_disabled'}:
                        start, end = document.index('<ul id="variation_color_name"'), document.index('<div id="productOverview_feature_div">')
                        document = document[:start]+'''<div id="variation_size_name"><select onchange="document.querySelector('#ASIN').value=this.value;recordEvent('variant-change')">
                          <option value="B000000001">Small</option><option value="B000000002">Medium</option>
                          <option value="B000000003">Large</option><option value="B000000004">Extra large</option>
                        </select></div>'''+document[end:]
                    if mode == 'partial_details':
                        document=document.replace('style="display:none"', 'style="display:block;max-height:16px;overflow:hidden"').replace("this.nextElementSibling.style.display='block';", "this.nextElementSibling.style.maxHeight='none';")
                    if mode == 'missing_details':
                        document=document.replace("this.setAttribute('aria-expanded','true');this.nextElementSibling.style.display='block';", '')
                    if mode == 'dropdown_disabled':
                        document = document.replace('<option value="B000000002">', '<option disabled value="B000000005">Unavailable</option><option value="B000000002">')
                    if mode == 'anchor_details':
                        start, end = document.index('<div id="productOverview_feature_div">'), document.index('<div style="height:2600px">')
                        document = document[:start]+'''<a id="seeMoreDetailsLink" href="#productDetails">See more product details</a>
                            <div style="height:700px"></div><section id="productDetails">Dishwasher-safe ceramic, 12 ounces.</section>'''+document[end:]
                    # Home never offers product interactions, even with similar markup.
                    await route.fulfill(content_type='text/html', body=document)
                await context.route('**/*', route)
                original_new_page = context.new_page
                async def new_page():
                    page = await original_new_page()
                    if mode in {'missing_transition', 'missing_details', 'backoff', 'backoff_ajax'}:
                        original_wait = page.wait_for_function
                        page.wait_for_function = lambda expression, **kwargs: original_wait(expression, **{**kwargs, 'timeout': 800})
                    return page
                context.new_page = new_page
                return context
            engine.amazon.context = context
            engine.amazon.expose = AsyncMock()
            started = time.perf_counter()
            await care.start(account['id'])
            if mode in {'cancel_reading', 'cancel_transition'}:
                await asyncio.wait_for((reading if mode=='cancel_reading' else changing).wait(), 10)
                await asyncio.wait_for(care.stop(account['id']), 3)
            else:
                async with asyncio.timeout(45):
                    await care.runs[account['id']]['job']
            await until(lambda: not care.runs)
            record = store.get('care_runs', 'care-run-'+account['id'])
            assert all(method=='GET' and '/cart' not in path and '/purchase' not in path for method, path in requests)
            assert store.get('accounts', other['id']) == other
            assert not engine.account_locks[account['id']].locked()
            assert engine.browser_slots._value == 1
            assert observations, record
            assert observations[0]['product_interactions'] == []
            if mode in {'ajax', 'inline_radio', 'navigation', 'delayed', 'anchor_details', 'partial_details', 'dropdown', 'dropdown_disabled'}:
                assert record['status'] == 'completed', record
                assert record['completed'] == 1  # Last-page interactions are not extra visits.
                assert record['interaction_count'] == 4
                assert [r['kind'] for r in record['history'][0]['interactions']] == ['variant','details','reviews','variant']
                assert provider.calls == 5 and len(pauses) == 4
                assert events.count('wheel') > 10 and events.count('move') > 10
                assert any('Dishwasher-safe ceramic' in json.dumps(result) for result in outputs)
                assert any('4.6 out of 5 stars' in json.dumps(result) for result in outputs)
                exported = json.dumps([observations, outputs])
                assert all(secret not in exported for secret in ['Private Buyer', 'private-account', 'Private Reviewer', 'External trap', 'Cart trap', 'Unavailable'])
                assert len(requests) == (4 if mode=='navigation' else 2)
                if mode in {'dropdown','dropdown_disabled','inline_radio'}:
                    assert events.count('variant-change') == 2
                if mode=='inline_radio':
                    assert record['last_interaction']['control']['role']=='radio'
                    assert record['last_interaction']['selection']=={'asin_matches':False,'url_matches':False,'selected_control':True}
                if mode == 'ajax':
                    output = Path('artifacts/account-care-review/product-exploration-measurement.json')
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps({'elapsed_seconds': round(time.perf_counter()-started, 3),
                        'model_calls': provider.calls, 'product_interactions': record['interaction_count'],
                        'wheel_events': events.count('wheel'), 'mouse_events': events.count('move'),
                        'requests': len(requests), 'purchase_requests': 0,
                        'limitations': 'Local routed Chromium fixture with simulated model decisions and skipped reading pauses. Actual paced input, MCP transport and CDP observation. Not a live retailer or AI latency benchmark.'}, indent=2))
            elif mode.startswith('cancel'):
                assert record['status'] == 'stopped', record
                assert record['interaction_count'] == 0
            elif mode in {'backoff','backoff_ajax'}:
                assert record['status'] == 'cooldown' and record['retry_at'], record
                assert len(requests) == 3
            else:
                assert record['status'] == 'attention', record
                assert record['interaction_count'] == (1 if mode=='missing_details' else 0)
                expected = {'missing_transition': 'timed out', 'missing_details': 'timed out', 'changed_control': 'option changed', 'page_limit': 'page limit reached'}[mode]
                assert expected in record['message']
                if mode=='missing_transition':
                    assert record['last_interaction']['phase']=='selection'
                    assert record['last_interaction']['click_completed']
                    assert record['last_interaction']['outcome']=='timeout'
                assert len(requests) == 2
            await browser.close()
        await engine.close()
        store.db.close()
    asyncio.run(scenario())
