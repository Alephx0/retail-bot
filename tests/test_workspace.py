from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from retail.app import create_app
from retail.models import Group
from retail.scheduling import occurrences

HEADERS = {"X-Retail-Client": "dashboard"}


def test_empty_group_and_atomic_mass_accounts(tmp_path):
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        group = client.post('/api/groups', json={'name': 'New group', 'retailer': 'amazon'}).json()
        assert group['products'] == '' and group['delay_ms'] == 4500
        task = client.post('/api/tasks', json={'group_id': group['id']}).json()
        assert client.post(f"/api/tasks/{task['id']}/start").status_code == 409
        bad = client.post('/api/account-batches/create', json={'retailer': 'amazon', 'text': 'first:password\nbad-line'})
        assert bad.status_code == 422
        assert client.get('/api/state').json()['accounts'] == []
        result = client.post('/api/account-batches/create', json={'retailer': 'amazon', 'text': 'first:pass:word;;;123\nsecond:pass;localhost:8080;JBSWY3DPEHPK3PXP;1234'})
        assert result.status_code == 200, result.text
        accounts = result.json()['created']
        assert accounts[0]['has_cvv'] and accounts[1]['has_totp'] and accounts[1]['has_proxy']
        assert not any(key in accounts[0] for key in ['password','cvv','proxy','totp_secret'])
        batch = client.post('/api/task-batches/create', json={'group_id': group['id'], 'account_ids': [a['id'] for a in accounts], 'task_count': 3, 'use_account_proxy': True, 'retry_delay_ms': 5500})
        assert len(batch.json()['created']) == 6
        assert all(t['use_account_proxy'] and t['retry_delay_ms']==5500 for t in batch.json()['created'])
        assert client.post('/api/task-batches/create', json={'group_id': group['id'], 'account_ids': [a['id'] for a in accounts], 'task_count': 51}).status_code == 422


def test_schedule_local_next_occurrence_and_overnight():
    anchor = datetime(2030, 1, 7, 10, 0).astimezone()  # Monday
    schedule = {'configured_at': anchor.isoformat(), 'days': [], 'slots': [{'start':'09:00','stop':'10:00'}]}
    assert occurrences(schedule, anchor + timedelta(minutes=1)) == []
    assert len(occurrences(schedule, anchor + timedelta(days=1, minutes=-30))) == 1
    assert occurrences(schedule, anchor + timedelta(days=2, minutes=-30)) == []
    weekly = {**schedule, 'days':[0], 'slots':[{'start':'23:00','stop':'01:00'}]}
    assert len(occurrences(weekly, anchor + timedelta(hours=14))) == 1
    assert occurrences(weekly, anchor + timedelta(hours=15)) == []
    assert len(occurrences(weekly, anchor + timedelta(days=7, hours=14))) == 1


def test_schedule_validation():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Group(name='bad', schedule={'days':[7]})
    with pytest.raises(ValidationError):
        Group(name='bad', schedule={'slots':[{'start':'24:00','stop':'10:00'}]})


def test_schedule_occurrence_is_not_restarted_after_poll_or_engine_restart(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from retail.engine import Engine
    from retail.store import Store

    async def scenario():
        store = Store(tmp_path)
        group = store.put('groups', Group(name='schedule').model_dump(mode='json'))
        monkeypatch.setattr('retail.engine.occurrences', lambda *_: [('occurrence-1','')])
        for expected in [1, 0]:
            engine = Engine(store)
            engine.start_group = AsyncMock()
            engine.scheduler = asyncio.create_task(engine.schedule())
            await asyncio.sleep(.6)
            assert engine.start_group.await_count == expected
            await engine.close()
        assert store.get('schedule_runs', 'schedule-'+group['id'])['seen'] == ['occurrence-1']
        store.db.close()
    asyncio.run(scenario())


def test_cvv_verification_does_not_submit_orders_or_bank_forms():
    import asyncio
    from patchright.async_api import async_playwright
    from retail.amazon import Amazon, Attention

    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.route('https://www.amazon.com/**', lambda route: route.fulfill(body='<body></body>',content_type='text/html'))
            await page.goto('https://www.amazon.com/verify')
            adapter = Amazon(None)
            await page.set_content('<form action="https://bank.example/verify"><input name="cvv"><button>Verify</button></form>')
            assert not await adapter.verify_cvv(page, '123')
            await page.set_content('<form><input name="cvv"><button>Place order</button></form>')
            assert not await adapter.verify_cvv(page, '123')
            await page.set_content('<form onsubmit="event.preventDefault();this.remove()"><input name="cvv"><button>Verify card</button></form>')
            assert await adapter.verify_cvv(page, '123')
            assert not await page.locator('input[name=cvv]').count()
            await page.set_content('<table id="subtotals-marketplace-table"><tr><td>Shipping</td><td>$0.00</td></tr></table>')
            await adapter.free_shipping(page)
            await page.set_content('<table id="subtotals-marketplace-table"><tr><td>Shipping</td><td>$5.00</td></tr></table>')
            import pytest
            with pytest.raises(Attention):
                await adapter.free_shipping(page)
            await browser.close()
    asyncio.run(scenario())
