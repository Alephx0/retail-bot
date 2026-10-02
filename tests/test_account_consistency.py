import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from retail.account_consistency import AccountBrowserProfiles, PurchaseCooldown
from retail.amazon import Amazon
from retail.engine import Engine
from retail.models import Account, Group, Task
from retail.store import Store


def test_profiles_persist_supported_settings(tmp_path):
    store = Store(tmp_path)
    account = {'id': 'one', 'region': 'US'}
    first = AccountBrowserProfiles(store).get(account)
    store.db.close()
    store = Store(tmp_path)
    profiles = AccountBrowserProfiles(store)
    assert profiles.get(account) == first
    assert profiles.get({'id': 'two', 'region': 'US'})['seed'] != first['seed']
    assert set(profiles.options(account)) == {'locale', 'viewport', 'screen', 'device_scale_factor'}
    store.db.close()


def test_cooldown_uses_only_real_account_orders(tmp_path):
    store = Store(tmp_path)
    current = datetime(2026, 9, 30, tzinfo=timezone.utc)
    policy = PurchaseCooldown(store)
    for account, simulation, status in [('other', False, 'confirmation_detected'),
                                         ('one', True, 'confirmation_detected'),
                                         ('one', False, 'failed')]:
        store.put('checkouts', {'account_id': account, 'simulation': simulation,
                               'status': status, 'at': current.isoformat()})
    assert policy.eligible_at('one', 2, current) == current
    store.put('checkouts', {'account_id': 'one', 'status': 'payment_verification',
                           'at': (current - timedelta(days=1)).isoformat()})
    assert policy.eligible_at('one', 2, current) == current + timedelta(days=1)
    assert policy.eligible_at('one', 0, current) == current
    store.put('checkouts', {'account_id': 'one', 'status': 'confirmation_detected', 'at': 'bad'})
    with pytest.raises(ValueError, match='invalid timestamp'):
        policy.eligible_at('one', 2, current)
    with pytest.raises(ValueError):
        Account(name='Invalid', purchase_cooldown_days=1)
    store.db.close()


def test_cooldown_stops_before_browser_allocation(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        engine = Engine(store)
        account = store.put('accounts', {'name': 'Fixture', 'region': 'US', 'purchase_cooldown_days': 2})
        group = store.put('groups', Group(name='Fixture', products='B012345678;25').model_dump())
        task = store.put('tasks', Task(group_id=group['id'], account_id=account['id'],
                                      simulation=False, checkout_mode='automatic').model_dump(mode='json'))
        store.put('checkouts', {'account_id': account['id'], 'status': 'confirmation_detected',
                               'at': datetime.now(timezone.utc).isoformat()})
        from retail.runner import TaskRunner
        await TaskRunner(engine).run(task['id'])
        assert store.get('tasks', task['id'])['status'] == 'stopped'
        assert engine.amazon.browser is None
        assert not engine.account_locks[account['id']].locked()
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_real_indexeddb_roundtrip_and_health(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        account = store.put('accounts', {'name': 'Fixture', 'region': 'US'})
        try:
            context = await adapter.context(account)
            page = await context.new_page()
            async def fixture(route):
                await route.fulfill(body='<div id="nav-link-accountList"><span class="nav-line-1">Hello Fixture</span></div>', content_type='text/html')
            await context.route('https://www.amazon.com/**', fixture)
            await page.goto('https://www.amazon.com/')
            initial_webdriver = await page.evaluate('navigator.webdriver')
            await page.evaluate('''() => new Promise((resolve, reject) => {
                const r = indexedDB.open('fixture', 1);
                r.onupgradeneeded = () => r.result.createObjectStore('state');
                r.onerror = () => reject(r.error);
                r.onsuccess = () => {
                    const tx = r.result.transaction('state', 'readwrite');
                    tx.objectStore('state').put('genuine-value', 'key');
                    tx.oncomplete = () => {r.result.close(); resolve();};
                    tx.onerror = () => reject(tx.error);
                };
            })''')
            await adapter.ensure_session(context, account, page)
            await page.set_viewport_size({'width': 900, 'height': 600})
            report = await adapter.profiles.check(page, account)
            assert report['viewport_corrected']
            assert report['status'] == 'consistent'
            await context.close()
            restored = await adapter.context(store.get('accounts', account['id']))
            await restored.route('https://www.amazon.com/**', fixture)
            page = await restored.new_page()
            await page.goto('https://www.amazon.com/')
            value = await page.evaluate('''() => new Promise((resolve, reject) => {
                const r = indexedDB.open('fixture');
                r.onerror = () => reject(r.error);
                r.onsuccess = () => {
                    const q = r.result.transaction('state').objectStore('state').get('key');
                    q.onsuccess = () => {r.result.close(); resolve(q.result);};
                    q.onerror = () => reject(q.error);
                };
            })''')
            assert value == 'genuine-value'
            assert await page.evaluate('navigator.webdriver') == initial_webdriver
        finally:
            await adapter.close()
            store.db.close()
    asyncio.run(scenario())
