import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from patchright.async_api import async_playwright
import pytest

from retail import behavior, visible_mouse
from retail.care_reading import READING_POINT, read_with_pointer
from retail.care_product import ProductExplorer


DOCUMENT = '''<style>
body{margin:0;background:#15191f;color:#ebeff5;font:18px system-ui}
header{padding:16px;background:#252c35} main{margin:28px auto;padding:0 24px;max-width:800px}
h1{font-size:28px}p{line-height:1.8}button{padding:12px}section{min-height:460px}
</style><header>Controlled browsing fixture</header><main>
<section><h1>Travel coffee mug</h1><p>Inspect the finish, dimensions and materials.</p>
<p>This reusable insulated mug has a comfortable handle and a fitted lid.</p>
<button id=buy>Add to Cart</button></section>
<section><h2>Product details</h2><p>Stainless steel. Hand wash the lid.</p></section>
<section><h2>Customer reviews</h2><p data-hook=review-body>Comfortable to hold on a morning walk.</p></section>
</main><script>
window.events=[];for(const type of ['mousemove','wheel','click','keydown'])
addEventListener(type,e=>{events.push({type,t:performance.now(),x:e.clientX,y:e.clientY,dy:e.deltaY,trusted:e.isTrusted});
if(type==='mousemove')document.body.dataset.moves=String((Number(document.body.dataset.moves)||0)+1);
if(type==='wheel')document.body.dataset.wheel=String((Number(document.body.dataset.wheel)||0)+e.deltaY);},true);
</script>'''


@pytest.mark.parametrize('width', [1100,390])
def test_reading_hover_scroll_and_stop_in_browser(width):
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            context=await browser.new_context(viewport={'width':width,'height':800})
            context._retail_paced_input=context._retail_browsing_input=True
            await visible_mouse.install(context)
            page=await context.new_page()
            requests=[]
            page.on('request',lambda r:requests.append(r.method))
            await page.set_content(DOCUMENT)
            paced=page._retail_paced_controller=behavior.PacedInput(page,seed=19)
            guard=AsyncMock()
            explorer=ProductExplorer(SimpleNamespace(page=page,guard=guard))
            started=time.monotonic()
            await explorer.read_pause(3.5)
            elapsed=time.monotonic()-started
            events=await page.evaluate('events',isolated_context=False)
            assert len(events)>12 and all(e['type']=='mousemove' and e['trusted'] for e in events)
            assert guard.await_count>=2 and 3.4<=elapsed<4.2
            assert await page.evaluate('scrollY')==0  # Keep the inspected section.
            assert len({(e['x'],e['y']) for e in events})>10
            output=Path('artifacts/account-care-review')
            output.mkdir(parents=True,exist_ok=True)
            await page.screenshot(path=str(output/f'reading-pointer-{width}.png'))

            start=len(events)
            await paced.scroll(320)
            await page.wait_for_function('Number(document.body.dataset.wheel)>=319.99')
            scroll=(await page.evaluate('events',isolated_context=False))[start:]
            wheels=[e for e in scroll if e['type']=='wheel']
            moves=[e for e in scroll if e['type']=='mousemove']
            assert len(moves)>4 and all(0<=e['x']<width and 0<=e['y']<800 for e in moves)
            assert any(wheels[0]['t']<e['t']<wheels[-1]['t'] for e in moves)
            assert sum(e['dy'] for e in wheels)==pytest.approx(320,abs=1)

            # Stop in the middle of a real movement; no lingering background input.
            original_move=page.mouse.move
            dispatches=[]
            async def tracked_move(x,y):
                dispatches.append((x,y))
                await original_move(x,y)
            page.mouse.move=tracked_move
            before=int(await page.locator('body').get_attribute('data-moves'))
            job=asyncio.create_task(read_with_pointer(page,30,guard))
            await page.wait_for_function('n=>Number(document.body.dataset.moves)>n+3',arg=before)
            assert paced.lock.locked()
            stopped=time.monotonic()
            job.cancel()
            with pytest.raises(asyncio.CancelledError):await job
            stop_ms=(time.monotonic()-stopped)*1000
            assert stop_ms<250 and not paced.lock.locked()
            sent=len(dispatches)
            last=await page.evaluate('events.length',isolated_context=False)
            await asyncio.sleep(.12)
            assert len(dispatches)==sent
            # Chromium may deliver the one command already in flight at Stop.
            assert await page.evaluate('events.length',isolated_context=False)<=last+1
            assert not any(e['type'] in ('click','keydown') for e in await page.evaluate('events',isolated_context=False))
            assert requests==[]
            gaps=sorted(b['t']-a['t'] for a,b in zip(moves,moves[1:]))
            (output/f'reading-pointer-{width}.json').write_text(json.dumps({
                'width':width,'reading_seconds':round(elapsed,3),'reading_mouse_events':len(events),
                'scroll_mouse_events':len(moves),'scroll_wheel_events':len(wheels),
                'scroll_frame_gap_median_ms':round(gaps[len(gaps)//2],2),'stop_ms':round(stop_ms,2),
                'limitations':'One seeded local Chromium fixture. No retailer, provider or purchase traffic; Stop measures input cancellation, not browser teardown.'},indent=2))
            await browser.close()
    asyncio.run(scenario())


def test_reading_excludes_controls_obscured_content_and_rechecks_live_geometry():
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            page=await browser.new_page()
            await page.set_content('''<main style="margin-top:100px">
                <nav><p>Account</p></nav><form><p>Payment</p></form>
                <button><p>Buy now</p></button><a href="/cart"><h2>Cart</h2></a>
                <p id=hidden style="visibility:hidden">Hidden</p>
                <p id=content>Readable description</p></main>''')
            args={'u':.5,'v':.5,'pick':0,'previous':[0,0]}
            point=await page.evaluate(READING_POINT,args)
            assert point
            assert await page.evaluate('p=>document.elementFromPoint(p.x,p.y).id',point)=='content'
            await page.locator('#content').evaluate('e=>e.remove()')
            assert await page.evaluate(READING_POINT,args) is None
            await page.set_content('<p style="margin-top:150px">Covered text</p><div style="position:fixed;inset:0;background:white"></div>')
            assert await page.evaluate(READING_POINT,args) is None
            await browser.close()
    asyncio.run(scenario())


def test_reading_scope_guard_failure_and_lock_exclusion():
    async def scenario():
        page=SimpleNamespace(context=SimpleNamespace(_retail_paced_input=True,_retail_browsing_input=False),evaluate=AsyncMock())
        paced=page._retail_paced_controller=behavior.PacedInput(page,seed=1)
        paced.move=AsyncMock()
        guard=AsyncMock()
        await read_with_pointer(page,.03,guard)
        page.evaluate.assert_not_awaited()
        paced.browsing=True
        async with paced.lock:
            await read_with_pointer(page,.03,guard)
        page.evaluate.assert_not_awaited()
        guard.side_effect=ValueError('Page left the browsing boundary')
        with pytest.raises(ValueError,match='boundary'):
            await read_with_pointer(page,3,guard)
        page.evaluate.assert_not_awaited()
        paced.move.assert_not_awaited()
        assert not paced.lock.locked()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure',[False,True])
def test_scroll_joins_both_inputs_on_stop_or_failure(failure):
    async def scenario():
        moving,wheeling=asyncio.Event(),asyncio.Event()
        finished=[]
        async def move(*_):
            moving.set()
            try:await asyncio.Event().wait()
            finally:finished.append('move')
        async def wheel(*_):
            wheeling.set()
            try:
                await moving.wait()
                if failure:raise ValueError('Disconnected')
                await asyncio.Event().wait()
            finally:finished.append('wheel')
        page=SimpleNamespace(context=SimpleNamespace(_retail_browsing_input=True),
            viewport_size={'width':1000,'height':800},mouse=SimpleNamespace(move=move,wheel=wheel))
        paced=behavior.PacedInput(page,seed=1)
        paced.position=(500,400)
        job=asyncio.create_task(paced.scroll(300))
        await wheeling.wait()
        await moving.wait()
        if not failure:job.cancel()
        with pytest.raises(ValueError if failure else asyncio.CancelledError):await job
        assert sorted(finished)==['move','wheel']
        assert not paced.lock.locked()
    asyncio.run(scenario())
