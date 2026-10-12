"""Account discoveries remain visible and never relax explicit offer filters."""
import asyncio
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from patchright.async_api import async_playwright, expect

from retail.app import create_app
from retail.store import now
from test_monitors import fixture, task_record, until


@pytest.mark.parametrize('filter_id,current_id', [('', 'current-token'), ('copied-token', 'current-token'), ('copied-token', '')])
@pytest.mark.parametrize('notify_offer', [False, True])
def test_account_offer_is_published_without_overwriting_monitor_or_settings(tmp_path, filter_id, current_id, notify_offer):
    async def scenario():
        store, group, account, engine = fixture(tmp_path)
        group = store.put('groups', {**group, 'products': 'B012345678', 'offer_id': filter_id, 'notify_offer': notify_offer})
        task = task_record(store, group, account)
        store.put('tasks', {**task, 'offer_usage': {'offer_id': 'previous-run', 'form': 'matched_clicked'}})
        engine.notify = AsyncMock()

        async def inspect(page, item, region):
            anonymous = page.context.identity['id'].startswith('monitor-')
            return {**item, 'offer_id': '' if anonymous else current_id,
                    'offer_id_status': 'discovered' if not anonymous and current_id else 'missing',
                    'title': 'Banana fixture', 'price': .99,
                    'available': not anonymous, 'availability_status': 'available',
                    'amazon_seller': True, 'seller': 'Amazon.com', 'condition': 'new'}

        engine.amazon.inspect = inspect
        try:
            await engine.start(task['id'])
            if filter_id:
                await until(lambda: 'no cart action was sent' in store.get('tasks', task['id']).get('message', ''))
                message = store.get('tasks', task['id'])['message']
                assert ('differs from the current account offer' if current_id else 'could not be verified') in message
                assert not engine.amazon.carts
            else:
                await until(lambda: task['id'] not in engine.jobs)
                assert engine.amazon.carts == [(account['id'], 'B012345678')]
            observations = store.all('feed')
            account_offer = next(p for p in observations if p.get('source') == 'account')
            assert account_offer['offer_id'] == current_id
            assert account_offer['account_id'] == account['id']
            assert account_offer['task_id'] == task['id']
            assert next(p for p in observations if p.get('source') != 'account')['offer_id'] == ''
            assert store.get('groups', group['id'])['offer_id'] == filter_id
            assert store.get('tasks', task['id'])['offer_usage'] is None  # Old run is not current proof.
            events = [e for e in store.all('events') if e['status'] == 'offer_found']
            notices = [call for call in engine.notify.await_args_list if 'Offer ID found' in call.args[1]]
            assert len(events) == len(notices) == int(notify_offer and bool(current_id))
            if events:
                assert 'assigned account' in events[0]['message']
                assert current_id not in events[0]['message']
        finally:
            await engine.close()
            store.db.close()
    asyncio.run(scenario())


def test_browser_account_offer_and_inventory_sources(tmp_path):
    headers = {'X-Retail-Client': 'dashboard'}
    app = create_app(tmp_path)
    with TestClient(app, headers=headers) as client:
        store = app.state.store
        account = store.put('accounts', {'name': 'Fixture buyer', 'region': 'US'})
        group = client.post('/api/groups', json={'name': 'Offer UI', 'products': 'B012345678'}).json()
        task = client.post('/api/tasks', json={'group_id': group['id'], 'account_id': account['id']}).json()
        base = {'asin': 'B012345678', 'title': 'Banana fixture', 'price': .99,
                'seller': 'Amazon.com', 'condition': 'new', 'at': now(), 'simulation': False}
        store.put('feed', {**base, 'offer_id': '', 'offer_id_status': 'missing', 'available': False,
                          'availability_message': 'In stock; Sign in required for this offer'})
        store.put('feed', {**base, 'offer_id': 'current-token', 'offer_id_status': 'discovered',
                          'available': True, 'source': 'account', 'account_id': account['id'], 'task_id': task['id']})

        async def scenario():
            async with async_playwright() as driver:
                browser = await driver.chromium.launch()
                page = await browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                async def route_local(route):
                    request = route.request
                    url = urlsplit(request.url)
                    response = client.request(request.method, url.path, content=request.post_data, headers=headers)
                    await route.fulfill(status=response.status_code, body=response.content,
                                        headers={'content-type': response.headers.get('content-type', 'text/plain')})
                await page.route('**/*', route_local)
                await page.goto('http://127.0.0.1/')
                await page.locator('[data-view=checkouts]').click()
                await page.locator('[data-context-view=feed]').click()
                for width in (1440, 390):
                    await page.set_viewport_size({'width': width, 'height': 900})
                    row = page.locator('tr').filter(has_text='Account offer')
                    await expect(row).to_contain_text('Fixture buyer')
                    await expect(row).to_contain_text('Offer ID discovered')
                    await expect(row.locator('[data-copy]')).to_have_attribute('data-copy', 'B012345678;0.99;current-token')
                    await expect(page.get_by_text('Inventory monitor', exact=True)).to_be_visible()
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    artifacts = Path('artifacts/offer-discovery'); artifacts.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(artifacts / f'account-observations-{width}.png'), full_page=True)
                await page.locator('[data-view=task_groups]').click()
                await page.locator(f'[data-work-open="{group["id"]}"]').click()
                await expect(page.locator(f'[data-task-row="{task["id"]}"]')).to_contain_text('Offer ID found in last account check')
                for width in (1440, 390):
                    await page.set_viewport_size({'width': width, 'height': 900})
                    await page.locator(f'[data-work-details="{task["id"]}"]').click()
                    await expect(page.get_by_label('Discovered Offer ID')).to_have_value('current-token')
                    await expect(page.get_by_label('Discovered Offer ID')).to_have_attribute('readonly', '')
                    await expect(page.get_by_role('button', name='Copy Offer ID')).to_have_attribute('data-copy', 'current-token')
                    await expect(page.get_by_text('This is an observation;', exact=False)).to_be_visible()
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await page.screenshot(path=str(artifacts / f'task-offer-{width}.png'), full_page=True)
                    await page.locator('[data-task-close]').click()
                store.put('tasks', {**store.get('tasks', task['id']), 'offer_usage': {
                    'offer_id': 'current-token', 'asin': base['asin'], 'action': 'add_to_cart',
                    'form': 'matched_clicked', 'cart': 'matched', 'checkout': 'not_exposed', 'at': now()}})
                await page.reload()
                await page.locator('[data-view=task_groups]').click()
                group_button = page.locator(f'[data-work-open="{group["id"]}"]')
                if await group_button.count():
                    await group_button.click()
                await expect(page.locator(f'[data-task-row="{task["id"]}"]')).to_contain_text('Offer ID matched in cart')
                for width in (1440, 390):
                    await page.set_viewport_size({'width': width, 'height': 900})
                    await page.locator(f'[data-work-details="{task["id"]}"]').click()
                    await expect(page.get_by_label('Offer ID checked for this action')).to_have_value('current-token')
                    await expect(page.get_by_text('Checkout: Not exposed', exact=False)).to_be_visible()
                    await expect(page.get_by_text('It does not prove server acceptance.', exact=False)).to_be_visible()
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await page.screenshot(path=str(artifacts / f'offer-validation-{width}.png'), full_page=True)
                    await page.locator('[data-task-close]').click()
                current_task = store.get('tasks', task['id'])
                store.put('tasks', {**current_task, 'offer_usage': {**current_task['offer_usage'],
                    'route': 'direct_form', 'shortcut': 'enabled', 'form': 'matched_posted'}})
                await page.reload()
                await page.locator('[data-view=task_groups]').click()
                group_button = page.locator(f'[data-work-open="{group["id"]}"]')
                if await group_button.count():
                    await group_button.click()
                for width in (1440, 390):
                    await page.set_viewport_size({'width':width, 'height':900})
                    await page.locator(f'[data-work-details="{task["id"]}"]').click()
                    await expect(page.get_by_text('Direct Offer ID carting for', exact=False)).to_be_visible()
                    await expect(page.get_by_text('Matched; direct form POST completed', exact=False)).to_be_visible()
                    await page.screenshot(path=str(artifacts / f'offer-shortcut-{width}.png'), full_page=True)
                    await page.locator('[data-task-close]').click()
                current_task = store.get('tasks', task['id'])
                store.put('tasks', {**current_task, 'offer_usage': {**current_task['offer_usage'],
                    'route':'direct_endpoint', 'form':'endpoint_acknowledged'}})
                await page.reload()
                await page.locator('[data-view=task_groups]').click()
                group_button = page.locator(f'[data-work-open="{group["id"]}"]')
                if await group_button.count():
                    await group_button.click()
                for width in (1440, 390):
                    await page.set_viewport_size({'width':width, 'height':900})
                    await page.locator(f'[data-work-details="{task["id"]}"]').click()
                    await expect(page.get_by_text('Direct Offer ID endpoint request for', exact=False)).to_be_visible()
                    await expect(page.get_by_text('Matched; endpoint acknowledged Add to Cart', exact=False)).to_be_visible()
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await page.screenshot(path=str(artifacts / f'offer-endpoint-{width}.png'), full_page=True)
                    await page.locator('[data-task-close]').click()
                assert not errors
                await browser.close()
        asyncio.run(scenario())
