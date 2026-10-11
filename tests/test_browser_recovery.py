import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

from retail.amazon import Amazon
from retail.browser_agent import BrowserAgent
from retail.browser_mcp import AMAZON_ACTIONS, BrowserTools
from retail.browser_recovery import Budgets, RecoveryController, execution_deadline
from retail.diagnostics import Diagnostics
from retail.interactions import InteractionError
from retail.models import AIConnection
from retail.recovery_evidence import approved, page_key
from retail.store import Store


@pytest.mark.parametrize('mode', ['off', 'recovery', 'agent'])
def test_all_modes_keep_successful_path_deterministic(tmp_path, monkeypatch, mode):
    async def run():
        store = Store(tmp_path)
        store.put('settings', {'agent_mode': mode}, 'settings')
        adapter = Amazon(store)
        node = object()
        resolver = AsyncMock(return_value=node)
        monkeypatch.setattr('retail.amazon.resolve', resolver)
        adapter.recovery.resolve = AsyncMock(side_effect=AssertionError('Recovery on fast path'))
        adapter.agent.resolve = AsyncMock(side_effect=AssertionError('AI on fast path'))
        assert await adapter.resolve_action(SimpleNamespace(), 'ADD_TO_CART') is node
        assert not store.all('recovery_incidents') and not store.all('agent_runs')
        store.db.close()
    asyncio.run(run())


def test_scoped_structural_recovery_and_optional_overlays(tmp_path):
    async def run():
        store = Store(tmp_path)
        adapter = Amazon(store)
        adapter.agent.resolve = AsyncMock(side_effect=AssertionError('Deterministic recovery called AI'))
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            context = await browser.new_context()
            context._retail_task_id = 'account-one'
            await context.route('**/*', lambda r: r.fulfill(body='<body></body>', content_type='text/html'))
            page = await context.new_page()
            await page.goto('https://www.amazon.com/dp/B012345678')
            await page.set_content('<button>Add item to cart</button><button>Jane secret@example.test</button>')
            before = len(browser.contexts)
            assert await adapter.resolve_action(page, 'ADD_TO_CART')
            assert await adapter.resolve_action(page, 'ADD_TO_CART'), 'A validated handle can be rechecked without another recovery'
            assert len(browser.contexts) == before
            assert not store.all('agent_runs')
            tools = BrowserTools(page, 'ADD_TO_CART', {'www.amazon.com'}, AMAZON_ACTIONS)
            evidence = await tools.observe_controls()
            assert 'secret' not in json.dumps(evidence)
            await page.locator('body').evaluate("e=>e.insertAdjacentHTML('beforeend','<button>Add to bag</button>')")
            assert not (await tools.validate_control('1'))['validated'], 'New competing controls invalidate old evidence'
            await page.get_by_role('button', name='Add to bag', exact=True).evaluate('e=>e.remove()')
            context._retail_task_id = 'different-owner'
            with pytest.raises(ValueError):
                await tools.validate_control('1')
            context._retail_task_id = 'account-one'

            # Safe dismissal runs before dispatch of the original action, exactly once.
            await adapter.recovery.install(page)
            await page.set_content("""<button onclick="window.clicks=(window.clicks||0)+1">Add to cart</button>
                <dialog open style="position:fixed;inset:0;background:white;z-index:99">
                <h2>Newsletter</h2><button onclick="this.closest('dialog').remove()">No thanks</button></dialog>""")
            await (await adapter.resolve_action(page, 'ADD_TO_CART')).click(timeout=4000)
            assert await page.evaluate('window.clicks', isolated_context=False) == 1
            assert not await page.locator('dialog').count()
            assert adapter.recovery.counts['recovered'] >= 2
            # Direct recovery also dismisses once when the expected control is
            # created only after the overlay closes (no handler recursion).
            await page.goto('https://www.amazon.com/dp/B000000005')
            await page.set_content("""<dialog open><h2>Newsletter</h2><button onclick="this.closest('dialog').outerHTML='<button>Add to bag</button>'">No thanks</button></dialog>""")
            assert await adapter.resolve_action(page, 'ADD_TO_CART')

            for text in ['Choose delivery location', 'Accept terms', 'Cookie consent', 'Verify your identity']:
                await page.set_content('<dialog open><h2>Newsletter</h2><p>'+text+'</p><button>No thanks</button></dialog>')
                with pytest.raises(InteractionError, match='user decision'):
                    await adapter.recovery.dismiss(page)
                assert await page.locator('dialog').is_visible()

            # Closed shadow roots are supported by installed Patchright.
            await page.goto('https://www.amazon.com/dp/B000000002')
            await page.set_content("<div id=host></div><script>document.querySelector('#host').attachShadow({mode:'open'}).innerHTML='<button>Add to bag</button>'</script>")
            assert await adapter.resolve_action(page, 'ADD_TO_CART')

            await page.goto('https://www.amazon.com/dp/B000000003')
            await page.set_content('<iframe src="/widget"></iframe>')
            frame = page.frames[1]
            await frame.wait_for_url('**/widget')
            await frame.set_content('<button>Add to basket</button>')
            assert await adapter.resolve_action(page, 'ADD_TO_CART')
            await frame.goto('https://other.example/widget')
            with pytest.raises(InteractionError):
                await adapter.resolve_action(page, 'ADD_TO_CART')

            # Declared loading uses a condition wait; no model request.
            await page.goto('https://www.amazon.com/dp/B000000004')
            await page.set_content("<div aria-busy=true id=loading></div><script>setTimeout(()=>{document.querySelector('#loading').outerHTML='<button>Add to bag</button>'},50)</script>")
            assert await adapter.resolve_action(page, 'ADD_TO_CART')

            # Returning to the same URL is a new document with new handles.
            await page.goto('https://www.amazon.com/dp/B000000002')
            await page.set_content('<button>Add to bag</button>')
            assert await adapter.resolve_action(page, 'ADD_TO_CART')

            # Redirects never give recovery ownership of a new tab or domain.
            await page.goto('https://other.example/product')
            with pytest.raises(InteractionError, match='ownership|location'):
                await adapter.resolve_action(page, 'ADD_TO_CART')
            await browser.close()
        store.db.close()
    asyncio.run(run())


def test_provider_validation_candidate_and_legacy_nonpromotion(tmp_path):
    async def run():
        store = Store(tmp_path)
        connection = store.put('ai_connections', AIConnection(name='fixture').model_dump())
        store.put('settings', {'ai_connection_id': connection['id']}, 'settings')
        class Provider:
            def __init__(self, connection): self.count = 0
            async def turn(self, instructions, history, tools):
                assert 'PRIVATE' not in json.dumps(history)
                self.count += 1
                return [{'id': str(self.count), 'name': 'observe_controls' if self.count == 1 else 'validate_control',
                         'arguments': '{}' if self.count == 1 else '{"ref":"1"}'}]
            def tool_result(self, history, call, result): history.append(result)
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(body='<body></body>', content_type='text/html'))
            await page.goto('https://www.amazon.com/dp/PRIVATE?token=PRIVATE')
            await page.set_content('<button>Add to bag</button><button>PRIVATE</button><input value=PRIVATE>')
            agent = BrowserAgent(store, Provider)
            assert await agent.resolve(page, 'ADD_TO_CART', {'www.amazon.com'}, AMAZON_ACTIONS)
            candidate = store.all('repair_recipes')[0]
            assert candidate['status'] == 'candidate' and not approved(candidate)
            assert not await agent.reuse(page, 'ADD_TO_CART', {'www.amazon.com'}, AMAZON_ACTIONS)
            candidate.update(status='approved', review_commit='test-reviewed-commit', test_evidence='fixture-report')
            store.put('repair_recipes', candidate)
            assert await agent.reuse(page, 'ADD_TO_CART', {'www.amazon.com'}, AMAZON_ACTIONS)
            record = await Diagnostics(store).capture({'id':'test'}, page, 'ADD_TO_CART', ValueError('PRIVATE'))
            assert 'PRIVATE' not in json.dumps(record)
            assert 'screenshot' not in record and 'accessibility' not in record
            assert page_key(page.url) == ('www.amazon.com', 'product')
            await browser.close()
        store.db.close()
    asyncio.run(run())


def test_deadlines_backpressure_cancellation_and_circuit(tmp_path, monkeypatch):
    async def run():
        store = Store(tmp_path)
        controller = RecoveryController(store, Budgets(total=.4, structural=.1, queue=.01, minimum_ai=.001, workers=1))
        def page():
            return SimpleNamespace(url='https://www.amazon.com/dp/B012345678', context=SimpleNamespace(_retail_task_id='one'),
                                   is_closed=lambda:False, evaluate=AsyncMock(return_value=False),
                                   locator=lambda _:SimpleNamespace(count=AsyncMock(return_value=0)))
        controller.dismiss = AsyncMock(return_value=False)
        class EmptyTools:
            def __init__(self, *args): pass
            async def observe_controls(self): return {'controls':[]}
        monkeypatch.setattr('retail.browser_recovery.BrowserTools', EmptyTools)
        entered = asyncio.Event()
        class SlowAgent:
            async def resolve(self, *args):
                entered.set()
                await asyncio.Event().wait()
        first = asyncio.create_task(controller.resolve(page(), 'ADD_TO_CART', SlowAgent(), allow_ai=True))
        await entered.wait()
        with pytest.raises(InteractionError, match='busy'):
            await controller.resolve(page(), 'ADD_TO_CART', SlowAgent(), allow_ai=True)
        # Healthy tasks never acquire the recovery pool, even while it is full.
        adapter = Amazon(store)
        adapter.recovery = controller
        monkeypatch.setattr('retail.amazon.resolve', AsyncMock(return_value='healthy'))
        assert await asyncio.wait_for(adapter.resolve_action(page(), 'ADD_TO_CART'), .03) == 'healthy'
        first.cancel()
        with pytest.raises(asyncio.CancelledError): await first
        assert not controller.active and not controller.slots.locked()
        assert controller.counts['cancelled'] == 1 and controller.counts['busy'] == 1
        # Parent deadline bounds all child work, including model calls.
        token = execution_deadline.set(time.monotonic()+.015)
        started = time.monotonic()
        with pytest.raises(InteractionError, match='timed out'):
            await controller.resolve(page(), 'BUY_NOW', SlowAgent(), allow_ai=True)
        assert time.monotonic()-started < .15
        execution_deadline.reset(token)
        class FailingAgent:
            calls = 0
            async def resolve(self, *args):
                self.calls += 1
                raise InteractionError('provider unavailable')
        agent = FailingAgent()
        for _ in range(5):
            with pytest.raises(InteractionError):
                await controller.resolve(page(), 'BEGIN_CHECKOUT', agent, allow_ai=True)
        assert agent.calls == 3
        assert len(store.all('recovery_incidents')) == 3, 'Failures deduplicate across task IDs'
        store.db.close()
    asyncio.run(run())
