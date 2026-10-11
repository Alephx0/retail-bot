import asyncio


def test_task_cleanup_does_not_wait_for_an_unrelated_browser():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from retail.runner import TaskRunner

    async def scenario():
        unrelated = asyncio.create_task(asyncio.Event().wait())
        own = asyncio.create_task(asyncio.sleep(0))
        context = SimpleNamespace(close=AsyncMock(), _retail_close_task=own)
        adapter = SimpleNamespace(profile_close_tasks={own, unrelated})
        try:
            await asyncio.wait_for(TaskRunner(None).close_context(adapter, context), 1)
            assert own.done() and not unrelated.done()
            context.close.assert_awaited_once()
        finally:
            unrelated.cancel()
            await asyncio.gather(unrelated, return_exceptions=True)
    asyncio.run(scenario())

import pytest
from patchright.async_api import async_playwright

from retail.amazon import Amazon
from retail.engine import Engine
from retail.models import Group, Task
from retail.store import Store


def test_headless_account_session_storage_is_isolated_and_restored(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'show_browser_window': False}, 'settings')
        adapter = Amazon(store)
        account = store.put('accounts', {'name': 'First', 'region': 'US', 'session': {'cookies': []}})
        second = store.put('accounts', {'name': 'Second', 'region': 'US', 'session': {'cookies': []}})
        context = await adapter.context(account)
        page = await context.new_page()
        await page.route('https://www.amazon.com/**', lambda route: route.fulfill(body='<h1>Fixture</h1>', content_type='text/html'))
        await page.goto('https://www.amazon.com/dp/B012345678')
        await page.evaluate("sessionStorage.setItem('fixture_key', 'first-only')", isolated_context=False)
        account['session'] = await context.storage_state()
        account['session_storage'] = await adapter.capture_session_storage(page)
        store.put('accounts', account)
        await context.close()

        restored = await adapter.context(store.get('accounts', account['id']))
        restored_page = await restored.new_page()
        await restored_page.route('https://www.amazon.com/**', lambda route: route.fulfill(body='<h1>Fixture</h1>', content_type='text/html'))
        await restored_page.goto('https://www.amazon.com/dp/B012345678')
        assert await restored_page.evaluate("sessionStorage.getItem('fixture_key')", isolated_context=False) == 'first-only'
        other = await adapter.context(second)
        other_page = await other.new_page()
        await other_page.route('https://www.amazon.com/**', lambda route: route.fulfill(body='<h1>Fixture</h1>', content_type='text/html'))
        await other_page.goto('https://www.amazon.com/dp/B012345678')
        assert await other_page.evaluate("sessionStorage.getItem('fixture_key')", isolated_context=False) is None
        await adapter.close()
        store.db.close()

    asyncio.run(scenario())


def test_headless_take_control_uses_existing_paused_page(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        engine = Engine(store)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content('<input id="login"><button id="go" onclick="window.clicked=true">Continue</button>')
            task = store.put('tasks', {'id': 'fixture', 'status': 'attention'})
            engine.pages[task['id']] = [page]
            engine.wakes[task['id']] = asyncio.Event()
            assert (await engine.browser_frame('tasks', task['id']))[:2] == b'\xff\xd8'
            bounds = await page.locator('#login').bounding_box()
            await engine.browser_input('tasks', task['id'], {'kind': 'click', 'x': bounds['x'] + 5, 'y': bounds['y'] + 5})
            await engine.browser_input('tasks', task['id'], {'kind': 'text', 'text': 'fixture@example.com'})
            assert await page.locator('#login').input_value() == 'fixture@example.com'
            assert not await page.evaluate('Boolean(window.clicked)', isolated_context=False)
            store.put('tasks', {**task, 'status': 'monitoring'})
            with pytest.raises(ValueError, match='paused'):
                await engine.browser_input('tasks', task['id'], {'kind': 'key', 'key': 'Enter'})
            with pytest.raises(ValueError, match='No browser page'):
                await engine.browser_input('accounts', 'other-account', {'kind': 'text', 'text': 'x'})
            await browser.close()
        store.db.close()

    asyncio.run(scenario())


def test_shared_monitor_keeps_checkout_context_capacity_available(tmp_path):
    class Page:
        def __init__(self, context):
            self.context = context

        async def close(self):
            pass

    class Context:
        def __init__(self, adapter):
            self.adapter = adapter

        async def new_page(self):
            return Page(self)

        async def close(self):
            self.adapter.open_contexts -= 1

    class Adapter:
        def __init__(self):
            self.open_contexts = 0
            self.max_contexts = 0
            self.release_first = asyncio.Event()
            self.inspections = 0

        async def context(self, *args):
            self.open_contexts += 1
            self.max_contexts = max(self.max_contexts, self.open_contexts)
            return Context(self)

        async def ensure_session(self, *args):
            pass

        async def inspect(self, *args):
            self.inspections += 1
            if self.inspections == 1:
                await self.release_first.wait()
            return {'asin': 'B012345678', 'title': 'Fixture item', 'price': 20, 'original_price': 25,
                    'available': True, 'amazon_seller': True, 'condition': 'new', 'offer_id': ''}

        async def cart(self, *args):
            return 1

        async def prepare_checkout(self, *args):
            pass

        async def checkout_snapshot(self, *args, **kwargs):
            return {'total': 21, 'quantity': 1, 'asin': 'B012345678', 'currency': 'USD'}

        async def close(self):
            pass

    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'max_running_tasks': 1}, 'settings')
        group = store.put('groups', Group(name='Queue', products='B012345678;25').model_dump())
        accounts = [store.put('accounts', {'name': str(i), 'region': 'US', 'session': {'cookies': []}}) for i in range(2)]
        tasks = [store.put('tasks', Task(group_id=group['id'], account_id=a['id'], simulation=False,
                                          checkout_mode='quote').model_dump(mode='json')) for a in accounts]
        engine = Engine(store)
        adapter = Adapter()
        engine.amazon = adapter
        await engine.start(tasks[0]['id'])
        for _ in range(100):
            if adapter.inspections:
                break
            await asyncio.sleep(.01)
        waiting = asyncio.Event()
        original_status = engine.status
        def observe_status(id, status, message, **metadata):
            original_status(id, status, message, **metadata)
            if id == tasks[1]['id'] and status == 'waiting':
                waiting.set()
        engine.status = observe_status
        await engine.start(tasks[1]['id'])
        await asyncio.wait_for(waiting.wait(), 5)
        assert store.get('tasks', tasks[1]['id'])['status'] == 'waiting'
        assert all(not lock.locked() for lock in engine.account_locks.values())
        assert adapter.open_contexts == 1  # Sign-in preflights release their workers.
        assert adapter.max_contexts == 2
        adapter.release_first.set()
        await asyncio.wait_for(asyncio.gather(*list(engine.jobs.values())), 10)
        assert adapter.max_contexts == 2  # One shared monitor plus one checkout worker.
        assert all(store.get('tasks', t['id'])['status'] == 'completed' for t in tasks)
        await engine.close()
        store.db.close()

    asyncio.run(scenario())
