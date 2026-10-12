import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from patchright.async_api import async_playwright

from retail.account_care import BrowsingRoutine
from retail.amazon import Amazon
from retail.care_agent import CareAgent
from test_account_care import fixture, until


class Provider:
    def __init__(self, calls):
        self.calls = iter(calls)
        self.requests = []

    async def turn(self, instructions, history, tools):
        self.requests.append(json.loads(json.dumps(history)))
        assert {tool['name'] for tool in tools} == {'search_products', 'open_product', 'inspect_product', 'finish_browsing'}
        name, args = next(self.calls)
        return [{'id': 'fixture-call', 'name': name, 'arguments': json.dumps(args)}]

    def tool_result(self, history, call, result):
        history.append({'role': 'user', 'content': json.dumps(result)})


def test_ai_missing_connection_does_not_reserve_account(tmp_path):
    async def scenario():
        store, engine, (account, _) = fixture(tmp_path)
        engine.account_care.save(account['id'], BrowsingRoutine(mode='ai', interests='coffee'))
        with pytest.raises(ValueError, match='AI connection'):
            await engine.account_care.start(account['id'])
        assert not engine.account_care.runs and not engine.account_locks
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['search_and_product', 'search_ref_and_product', 'shifted_links',
    'forbidden_tool', 'cancel_thinking', 'retailer_backoff', 'unsupported_form', 'missing_search',
    'missing_navigation', 'delayed_navigation', 'cancel_navigation', 'changed_link'])
def test_mcp_cdp_agent_fixture(tmp_path, monkeypatch, mode):
    async def scenario():
        store, engine, (account, _) = fixture(tmp_path)
        engine.amazon = Amazon(store)
        connection = store.put('ai_connections', {'name': 'Fixture AI', 'protocol': 'chat', 'model': 'fixture', 'api_key': 'private'})
        care = engine.account_care
        care.save(account['id'], BrowsingRoutine(mode='ai', interests='coffee equipment', ai_connection_id=connection['id'], visit_limit=2, seconds_per_page=15))
        requests, inputs, readings = [], [], []
        provider = Provider([('search_products', {'query': 'coffee mug'}), ('open_product', {'ref': 'p1'})])
        if mode == 'search_ref_and_product':
            provider = Provider([('search_products', {'query': "kids' mugs under $20"}), ('open_product', {'ref': 'p1'})])
        if mode in {'shifted_links', 'changed_link'}:
            original_turn = provider.turn
            async def turn(*args):
                if provider.requests:
                    page = care.runs[account['id']]['page']
                    if mode == 'shifted_links':
                        await page.evaluate('''() => {const a=document.createElement('a');a.href='/gp/cart/add.html';a.textContent='Unexpected cart link';document.body.prepend(a);}''')
                    else:
                        await page.locator('a[href="/dp/B012345678"]').evaluate("e => e.href='/gp/cart/add.html'")
                return await original_turn(*args)
            provider.turn = turn
        if mode == 'forbidden_tool':
            provider = Provider([('evaluate_javascript', {'code': 'checkout()'})])
        thinking = asyncio.Event()
        navigating = asyncio.Event()
        if mode == 'cancel_thinking':
            async def think(*args):
                thinking.set()
                await asyncio.Event().wait()
            provider.turn = think
        monkeypatch.setattr(CareAgent, 'provider_factory', staticmethod(lambda connection: provider))
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            async def context(account, **kwargs):
                assert kwargs['interactive'] and kwargs['headless'] is False
                context = await browser.new_context(viewport={'width': 1000, 'height': 800})
                await context.expose_function('recordInput', lambda kind: inputs.append(kind))
                await context.add_init_script("""addEventListener('DOMContentLoaded',()=>{
                    document.addEventListener('keydown',()=>recordInput('key'));
                    document.addEventListener('mousemove',()=>recordInput('move'));
                    document.addEventListener('click',()=>recordInput('click'));
                });""")
                async def route(request):
                    requests.append((request.request.method, request.request.url))
                    searching = urlsplit(request.request.url).path.startswith('/s')
                    if searching and mode == 'delayed_navigation':
                        await asyncio.sleep(.25)
                    if searching and mode == 'cancel_navigation':
                        navigating.set()
                        await asyncio.Event().wait()
                    status = 429 if mode == 'retailer_backoff' and searching else 200
                    document = '''<html><body>
                        <div id="nav-link-accountList"><span class="nav-line-1">Hello, Private Buyer</span></div>
                        <p>private-account@example.test</p>
                        <form action="/s" method="get"><input id="twotabsearchtextbox" name="k"><input id="nav-search-submit-button" type="submit" value="Search"></form>
                        <a href="/dp/B012345678">Coffee mug</a>
                        <a href="/gp/cart/add.html">Add to Cart</a><a href="https://example.test/dp/B087654321">External product</a>
                        <button onclick="fetch('/purchase',{method:'POST'})">Buy now</button>
                        </body></html>'''
                    if mode == 'search_ref_and_product':
                        document = document.replace('action="/s"', 'action="/s/ref=nb_sb_noss"')
                    if mode == 'unsupported_form':
                        document = document.replace('action="/s"', 'action="/gp/cart/add.html"')
                    if mode == 'missing_search':
                        document = document.replace('id="twotabsearchtextbox"', 'id="missing-search"')
                    if mode == 'missing_navigation':
                        document = document.replace('<form action=', '<form onsubmit="event.preventDefault()" action=')
                    await request.fulfill(status=status, headers={'Retry-After': '1800'}, content_type='text/html', body=document)
                await context.route('**/*', route)
                if mode == 'missing_navigation':
                    new_page = context.new_page
                    async def page_with_short_timeout():
                        page = await new_page()
                        original_expect = page.expect_navigation
                        page.expect_navigation = lambda **kwargs: original_expect(**{**kwargs, 'timeout': 1500})
                        return page
                    context.new_page = page_with_short_timeout
                return context
            engine.amazon.context = context
            engine.amazon.expose = AsyncMock()
            async def dwell(page, seconds, domain):
                assert page.context._retail_paced_input is True
                assert 15 <= seconds <= 19
                readings.append(page.url)
            care.dwell = dwell
            await care.start(account['id'])
            if mode == 'cancel_thinking':
                await asyncio.wait_for(thinking.wait(), 5)
                await asyncio.wait_for(care.stop(account['id']), 3)
            elif mode == 'cancel_navigation':
                await asyncio.wait_for(navigating.wait(), 10)
                await asyncio.wait_for(care.stop(account['id']), 3)
            else:
                async with asyncio.timeout(20):
                    await care.runs[account['id']]['job']
            await until(lambda: not care.runs)
            record = store.get('care_runs', 'care-run-'+account['id'])
            assert all(method == 'GET' for method, _ in requests)
            assert not any('/purchase' in url or '/cart' in url or 'example.test' in url for _, url in requests)
            if mode in {'search_and_product', 'search_ref_and_product', 'shifted_links', 'delayed_navigation'}:
                assert record['status'] == 'completed', record
                assert record['completed'] == 2
                assert len(readings) == 2
                assert inputs.count('key') >= len('coffee mug')
                assert inputs.count('move') > 5 and inputs.count('click') >= 3
                exported = json.dumps(provider.requests)
                assert 'Private Buyer' not in exported and 'private-account' not in exported
                assert 'Coffee mug' in exported and 'External product' not in exported
                assert all(15 <= h['reading_seconds'] <= 19 for h in record['history'])
                if mode == 'search_ref_and_product':
                    assert '/s/ref=nb_sb_noss?' in requests[1][1]
            elif mode == 'forbidden_tool':
                assert record['status'] == 'attention'
                assert len(requests) == 1 and not inputs
            elif mode == 'retailer_backoff':
                assert record['status'] == 'cooldown', record
                assert len(requests) == 2
            elif mode in {'unsupported_form', 'missing_search', 'missing_navigation', 'changed_link'}:
                assert record['status'] == 'attention', record
                reason = {'unsupported_form': 'unsupported search form', 'missing_search': 'search box or button',
                          'missing_navigation': 'timed out while opening Search results', 'changed_link': 'link changed or disappeared'}[mode]
                assert reason in record['message'], record
                assert len(requests) == (2 if mode == 'changed_link' else 1)
                assert not store.all('sessions')  # An AI/tool failure must not invalidate login.
            else:
                assert record['status'] == 'stopped'
                assert len(requests) == (2 if mode == 'cancel_navigation' else 1)
                if mode == 'cancel_thinking':
                    assert not inputs
            assert not engine.account_locks[account['id']].locked()
            await browser.close()
        await engine.close()
        store.db.close()
    asyncio.run(scenario())
