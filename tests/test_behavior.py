import asyncio

import pytest
from fastapi.testclient import TestClient
from patchright.async_api import async_playwright

from retail import behavior
from retail.amazon import Amazon, Attention
from retail.app import create_app
from scripts.analyze_behavior_datasets import mouse_metrics


def test_pacing_setting_validation(tmp_path):
    with TestClient(create_app(tmp_path), headers={'X-Retail-Client': 'dashboard'}) as client:
        assert client.post('/api/settings', json={'interaction_pacing': 'paced'}).status_code == 200
        assert client.post('/api/settings', json={'interaction_pacing': 'invalid'}).status_code == 422


def test_mouse_analysis_uses_client_seconds_and_excludes_duplicate_timestamps():
    raw = b'record timestamp,client timestamp,button,state,x,y\n0,0,NoButton,Move,0,0\n1,0.1,NoButton,Move,3,4\n2,0.1,NoButton,Move,6,8\n3,0.2,Left,Pressed,6,8\n4,0.3,Left,Released,6,8\n'
    metrics, counts = mouse_metrics(raw)
    assert metrics['speed_recorded_px_s'] == [50]
    assert metrics['move_interval_ms'] == [100]
    assert metrics['click_hold_ms'][0] == pytest.approx(100)
    assert counts['nonpositive_dt'] == 1


def test_paced_browser_input_scroll_and_cancellation():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(channel='chrome')
            context = await browser.new_context(viewport={'width': 1000, 'height': 700})
            context._retail_paced_input = True
            page = await context.new_page()
            await page.set_content('''<input id="text"><input id="unicode"><input id="number" type="number">
                <div style="height:1400px"></div><button id="target" onclick="window.clicks++">Target</button>
                <script>window.clicks=0;window.events=[];
                for(const type of ['mousemove','mousedown','mouseup','wheel','keydown','keyup','input'])
                  document.addEventListener(type,e=>events.push({type,t:performance.now(),trusted:e.isTrusted,x:e.clientX,y:e.clientY,code:e.code,key:e.key,shift:e.shiftKey}),true);
                </script>''')
            page._retail_paced_controller = behavior.PacedInput(page, seed=40)
            try:
                # Symbols must remain literal; no form submit from injected typos/Enter.
                await behavior.fill(page, page.locator('#text'), 'Ab+9@ _!?')
                assert await page.locator('#text').input_value() == 'Ab+9@ _!?'
                await behavior.fill(page, page.locator('#unicode'), 'Café 東京')
                assert await page.locator('#unicode').input_value() == 'Café 東京'
                await behavior.fill(page, page.locator('#number'), '123')
                assert await page.locator('#number').input_value() == '123'
                await behavior.click(page, page.locator('#target'))
                assert await page.evaluate('clicks', isolated_context=False) == 1
                events = await page.evaluate('events', isolated_context=False)
                assert all(event['trusted'] for event in events)
                assert sum(event['type']=='mousemove' for event in events) > 20
                assert sum(event['type']=='wheel' for event in events) >= 3
                keys = [event for event in events if event['type'] in ('keydown','keyup') and event['key'] != 'Shift']
                holds = [keys[i+1]['t']-keys[i]['t'] for i in range(0,len(keys),2)]
                assert len(holds) == 9 and min(holds) >= 25 and max(holds)-min(holds) > 5
                assert all(e['shift'] for e in keys if e['type']=='keydown' and e['key'] in 'A+@_!?')
                assert await page.evaluate('scrollY') > 0
                box = await page.locator('#target').bounding_box()
                assert 0 <= box['y'] and box['y'] + box['height'] <= 700
                # Preserve native trial semantics (which can include one move),
                # without a paced path, wheel input or a click.
                count = len(events)
                await behavior.click(page, page.locator('#target'), trial=True)
                assert await page.evaluate('clicks', isolated_context=False) == 1
                trial_events = (await page.evaluate('events', isolated_context=False))[count:]
                assert len(trial_events) <= 1 and all(e['type'] == 'mousemove' for e in trial_events)
                await page.evaluate('scrollTo(0,0)')
                task = asyncio.create_task(behavior.click(page, page.locator('#target')))
                await asyncio.sleep(.06)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.sleep(.1)
                assert await page.evaluate('clicks', isolated_context=False) == 1
                assert not behavior.controller(page).lock.locked()
            finally:
                await browser.close()
    asyncio.run(asyncio.wait_for(scenario(), 40))


def test_pacing_keeps_native_target_checks_and_does_not_retry():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(channel='chrome')
            context = await browser.new_context()
            context._retail_paced_input = True
            page = await context.new_page()
            try:
                await page.set_content('''<button onclick="window.clicks++">Target</button>
                    <div style="position:fixed;inset:0;z-index:9">Overlay</div>
                    <script>window.clicks=0</script>''')
                with pytest.raises(Exception, match='intercepts pointer events|Timeout'):
                    await behavior.click(page, page.locator('button'), timeout=200)
                assert await page.evaluate('clicks', isolated_context=False) == 0
                await page.locator('div').evaluate('e=>e.remove()')
                await behavior.click(page, await page.locator('button').element_handle())
                assert await page.evaluate('clicks', isolated_context=False) == 1
                context._retail_paced_input = False
                await behavior.click(page, page.locator('button'))
                assert await page.evaluate('clicks', isolated_context=False) == 2
            finally:
                await browser.close()
    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_paced_typing_checks_overlay_and_emits_balanced_us_modifiers():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(channel='chrome')
            context = await browser.new_context()
            context._retail_paced_input = True
            page = await context.new_page()
            try:
                await page.set_content('''<input id="text"><input id="locked" readonly>
                    <div id="cover" style="position:fixed;inset:0;z-index:9">Overlay</div>
                    <script>window.events=[]; for(const type of ['click','keydown','keyup'])
                    document.addEventListener(type,e=>events.push({type,key:e.key,code:e.code,shift:e.shiftKey}),true);</script>''')
                with pytest.raises(Exception, match='intercepts pointer events|Timeout'):
                    await behavior.fill(page, page.locator('#text'), 'Secret42!')
                assert await page.locator('#text').input_value() == ''
                assert await page.evaluate('events.length', isolated_context=False) == 0
                await page.locator('#cover').evaluate('e=>e.remove()')
                with pytest.raises(Exception, match='not editable'):
                    await behavior.fill(page, page.locator('#locked'), 'x')
                text = 'AZaz~!@#$%^&*()_+{}|:"<>?[]\\;\',./`-='
                await behavior.fill(page, await page.locator('#text').element_handle(), text)
                assert await page.locator('#text').input_value() == text
                events = await page.evaluate('events', isolated_context=False)
                assert events[0]['type'] == 'click'
                pressed = set()
                for e in events[1:]:
                    if e['type'] == 'keydown':
                        assert e['code'] not in pressed
                        pressed.add(e['code'])
                        if e['key'] in 'AZ~!@#$%^&*()_+{}|:"<>?':
                            assert e['shift']
                    elif e['type'] == 'keyup':
                        assert e['code'] in pressed
                        pressed.remove(e['code'])
                assert not pressed
                # Shift must not remain held after the final chord.
                await page.locator('#text').press('a')
                assert (await page.locator('#text').input_value()).endswith('a')
            finally:
                await browser.close()
    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_submission_revalidates_price_after_paced_movement():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(channel='chrome')
            context = await browser.new_context()
            context._retail_paced_input = True
            page = await context.new_page()
            try:
                await page.route('https://www.amazon.com/checkout/spc', lambda r:r.fulfill(body='<body></body>'))
                await page.goto('https://www.amazon.com/checkout/spc')
                await page.set_content('''<div id="spc-orders"><div data-asin="B012345678" data-quantity="1" data-condition="new" data-seller="Amazon.com">
                    Sold by: Amazon.com <span class="a-price"><span class="a-offscreen">$20.00</span></span></div></div>
                    <table id="subtotals-marketplace-table"><tr><td>Order total:</td><td id="total">$21.20</td></tr></table>
                    <input name="placeYourOrder1" type="button" value="Place order" onclick="window.submits++">
                    <script>window.submits=0;</script>''')
                adapter = Amazon(None)
                await adapter.checkout_snapshot(page, 'B012345678', 1, 25, max_unit_price=20)
                await page.evaluate("document.addEventListener('mousemove',()=>document.querySelector('#total').textContent='$100.00',{once:true})")
                with pytest.raises(Attention, match='budget'):
                    await adapter.submit_order(page)
                assert await page.evaluate('submits', isolated_context=False) == 0
            finally:
                await browser.close()
    asyncio.run(asyncio.wait_for(scenario(), 20))
