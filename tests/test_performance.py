import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from retail.app import create_app
from retail.performance import Performance, elapsed, stage


def test_timings_isolate_concurrent_runs_and_clear_cancelled_stages():
    async def scenario():
        recorder = Performance(limit=3)
        entered = asyncio.Event()
        async def waiting():
            with recorder.scope('monitor', 'one'), stage('Waiting'):
                entered.set()
                await asyncio.Future()
        job = asyncio.create_task(waiting())
        await entered.wait()
        assert recorder.snapshot('monitor', 'one')['active'][0]['stage'] == 'Waiting'
        with recorder.scope('task', 'two'):
            with pytest.raises(ValueError), stage('Failure'):
                raise ValueError('private credential must not appear')
            with stage('Read'):
                await asyncio.sleep(0)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert not recorder.active
        assert recorder.snapshot('monitor', 'one')['recent'][0]['outcome'] == 'cancelled'
        assert {r['stage'] for r in recorder.snapshot('task', 'two')['recent']} == {'Read', 'Failure'}
        assert recorder.snapshot('task', 'two')['summary'][0]['count'] == 1
        with stage('Outside scope'):
            pass
        assert len(recorder.samples) == 3
        with recorder.scope('task', 'two'):
            elapsed('Observation age', 0)
        assert len(recorder.samples) == 3
        assert 'private credential' not in json.dumps(recorder.snapshot())
    asyncio.run(scenario())


def test_performance_endpoint_is_filtered_and_does_not_change_state(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        with app.state.engine.performance.scope('task', 'one'), stage('Offer check'):
            pass
        assert client.get('/api/performance?kind=monitor&id=one').json()['summary'] == []
        report = client.get('/api/performance?kind=task&id=one').json()
        assert report['summary'][0]['stage'] == 'Offer check'
        assert report['capacity'] == 2048
        assert not client.get('/api/state').json()['active']


def test_monitor_never_uses_purchase_control_recovery(tmp_path, monkeypatch):
    from patchright.async_api import async_playwright
    from retail.amazon import Amazon, ChallengeDetected, BackoffRequired
    from retail.store import Store

    async def forbidden(*args, **kwargs):
        raise AssertionError('Inventory scans must not resolve checkout actions')
    monkeypatch.setattr('retail.amazon.resolve', forbidden)

    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'agent_mode': 'agent'}, 'settings')
        adapter = Amazon(store)
        adapter.agent.resolve = forbidden
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            page = await browser.new_page()
            item = {'asin': 'B07ZLF9WQ5'}
            for fragment, status in [
                ('<div id="availability">In Stock</div><button>Cart</button>', 'available'),
                ('<div id="availability">Currently unavailable</div><button>Cart</button>', 'unavailable'),
                ('<div hidden id="availability">In Stock</div><button>Cart</button>', 'unknown'),
                ('<button disabled id="add-to-cart-button">Add to cart</button>', 'unknown'),
                ('<button id="add-to-cart-button">Add to cart</button>', 'available'),
            ]:
                html = '<h1 id="productTitle">Fixture</h1><div id="rightCol">' + fragment + '</div>'
                await page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
                result = await adapter.inspect_stock(page, item, 'US')
                assert result['availability_status'] == status
                assert result['agent_error'] == ''
                await page.unroute_all()
            await page.route('**/*', lambda route: route.fulfill(body='Robot check', content_type='text/html'))
            with pytest.raises(ChallengeDetected):
                await adapter.inspect_stock(page, item, 'US')
            await page.unroute_all()
            await page.route('**/*', lambda route: route.fulfill(status=429, headers={'retry-after': '90'}, body='Wait'))
            with pytest.raises(BackoffRequired) as caught:
                await adapter.inspect_stock(page, item, 'US')
            assert caught.value.retry_after_seconds == 90
            await browser.close()
        store.db.close()
    asyncio.run(scenario())


def test_percentiles_are_computed_only_on_snapshot():
    recorder = Performance()
    for duration in range(1, 101):
        recorder.samples.append({'kind':'task','id':'one','stage':'Read','duration_ms':duration,'outcome':'ok'})
    row = recorder.snapshot()['summary'][0]
    assert (row['p50_ms'], row['p95_ms'], row['p99_ms']) == (50, 95, 99)
    assert 'durations' not in row


def test_recovery_read_model_matches_compact_workspace(tmp_path):
    app=create_app(tmp_path)
    with TestClient(app) as client:
        app.state.engine.amazon.recovery.active[1]={'task_id':'one','status':'Recovering','action':'ADD_TO_CART'}
        full=client.get('/api/state').json()
        compact=client.get('/api/task-workspace').json()
        assert full['browser_recovery']==compact['browser_recovery']
        assert compact['browser_recovery']['active'][0]['task_id']=='one'
        assert not full['tasks'], 'Recovery telemetry must not create or rewrite task state'
        assert client.get('/api/recovery-incidents').json()==[]
