"""Per-group offer choice preserves saved IDs and frozen run behavior."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from patchright.async_api import async_playwright

from retail.amazon import Amazon
from retail.app import create_app
from retail.models import Group, eligible, rejection_reasons
from retail.monitors import monitor_items
from retail.interactions import resolve
from test_offer_discovery import BASE, TARGET, form
from test_monitors import fixture, task_record, until


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('buy_now', [False, True])
def test_switch_controls_binding_and_native_action_but_keeps_discovery(enabled, buy_now):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            markup = BASE + form().replace('</form>', '<button id=buy-now-button>Buy Now</button></form>')
            await page.route('**/*', lambda r:r.fulfill(body=markup, content_type='text/html'))
            await page.goto(f'https://www.amazon.com/dp/{TARGET}')
            adapter = Amazon(None)
            page._retail_offer_usage = {'offer_id': 'older-run'}
            product = await adapter.read_product(page, {'asin': TARGET, 'use_offer_id': enabled})
            assert product['offer_id'] == 'token%2B+/='
            assert product['use_offer_id'] is enabled
            assert bool(page._retail_discovered_offer) is enabled
            async def resolver(p, action):
                return await resolve(p, action)
            adapter.resolve_action = AsyncMock(side_effect=resolver)
            adapter.advance_checkout = AsyncMock()
            if buy_now:
                await adapter.buy_now(page, 1, TARGET)
            else:
                await adapter.click_product_offer(page, TARGET)
                assert adapter.resolve_action.await_count == (0 if enabled else 1)
            assert await page.evaluate('window.adds', isolated_context=False) == 1
            assert bool(page._retail_offer_usage) is enabled
            await browser.close()
    asyncio.run(scenario())


def test_disabled_ignores_saved_group_and_per_product_ids_without_erasing_them(tmp_path):
    store, group, account, engine = fixture(tmp_path)
    group = {**group, 'products': TARGET+';25;per-product-token', 'offer_id': 'group-token', 'use_offer_id': False, 'offer_id_shortcut': True, 'offer_id_endpoint': True}
    item = monitor_items(store, group)[0]
    assert item['offer_id'] == '' and item['use_offer_id'] is False
    assert item['offer_id_shortcut'] is False
    assert item['offer_id_endpoint'] is False
    product = {'price': 20, 'available': True, 'amazon_seller': True, 'condition': 'new', 'offer_id': 'current-token'}
    assert eligible(product, {**item, 'offer_id': 'saved-token'}, group)
    assert not rejection_reasons(product, {**item, 'offer_id': 'saved-token'}, group)
    restored = monitor_items(store, {**group, 'use_offer_id': True})[0]
    assert restored['offer_id'] == 'per-product-token'
    assert restored['offer_id_shortcut'] is True
    assert restored['offer_id_endpoint'] is True
    assert monitor_items(store, {**group, 'use_offer_id':True, 'offer_id_shortcut':False})[0]['offer_id_endpoint'] is False
    assert group['offer_id'] == 'group-token'
    assert Group(name='Legacy').use_offer_id is True
    assert Group(name='Legacy').offer_id_endpoint is False
    store.db.close()


def test_toggle_is_frozen_for_running_task_and_new_run_uses_new_value(tmp_path):
    async def scenario():
        store, group, account, engine = fixture(tmp_path)
        group = store.put('groups', {**group, 'products': TARGET, 'use_offer_id': False, 'offer_id_shortcut':True, 'offer_id_endpoint':True})
        task = task_record(store, group, account)
        original = engine.amazon.inspect
        checks = []
        async def inspect(page, item, region):
            if not page.context.identity['id'].startswith('monitor-'):
                checks.append((item['use_offer_id'], item['offer_id_endpoint']))
            return await original(page, item, region)
        engine.amazon.inspect = inspect
        engine.amazon.account_gate = asyncio.Event()
        engine.amazon.stock.add(TARGET)
        try:
            await engine.start(task['id'])
            await until(lambda: store.get('tasks', task['id'])['status'] == 'authenticating')
            # Simulate a persisted edit outside the API; the captured run stays unchanged.
            store.put('groups', {**group, 'use_offer_id': True})
            engine.amazon.account_gate.set()
            await until(lambda: task['id'] not in engine.jobs)
            assert checks == [(False, False)]
            await engine.start(task['id'])
            await until(lambda: task['id'] not in engine.jobs)
            assert checks == [(False, False), (True, True)]
        finally:
            await engine.close()
            store.db.close()
    asyncio.run(scenario())


def test_partial_settings_edits_preserve_switch_and_saved_ids(tmp_path):
    with TestClient(create_app(tmp_path), headers={'X-Retail-Client':'dashboard'}) as client:
        first = client.post('/api/groups', json={'name':'First', 'products':TARGET, 'offer_id':'saved', 'use_offer_id':False, 'offer_id_endpoint':True}).json()
        second = client.post('/api/groups', json={'name':'Second', 'products':TARGET}).json()
        changed = client.put('/api/groups/'+first['id'], json={'name':'Renamed'}).json()
        assert changed['use_offer_id'] is False and changed['offer_id'] == 'saved'
        assert changed['offer_id_endpoint'] is True
        changed = client.put('/api/groups/'+first['id'], json={'use_offer_id':True}).json()
        assert changed['offer_id'] == 'saved'
        assert next(g for g in client.get('/api/state').json()['groups'] if g['id']==second['id']) == second
