import asyncio
import json
from pathlib import Path
import statistics

from patchright.async_api import async_playwright
import pytest

from retail import behavior
from test_browsing_calibration import CALIBRATION
from test_mouse_calibration import POINTER


def test_fitted_browser_motion_and_cancellation():
    async def scenario():
        measurements={}
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            for recorded in (False,True):
                context=await browser.new_context(viewport={'width':1100,'height':800})
                context._retail_paced_input=True
                context._retail_browsing_input=True
                context._retail_input_calibration={**CALIBRATION,'pointer':POINTER} if recorded else None
                page=await context.new_page()
                await page.set_content('''<button style="position:absolute;left:200px;top:200px;width:180px;height:50px" onclick="window.clicks++">Fixture</button>
                    <script>window.moves=[];window.clicks=0;addEventListener('mousemove',e=>moves.push({t:performance.now(),x:e.clientX,y:e.clientY,trusted:e.isTrusted}));</script>''')
                paced=page._retail_paced_controller=behavior.PacedInput(page,seed=20)
                await page.mouse.move(100,100)
                paced.position=(100,100)
                await page.evaluate('moves=[]',isolated_context=False)
                await paced.move(900,650)
                moves=await page.evaluate('moves',isolated_context=False)
                assert len(moves)>20 and all(p['trusted'] for p in moves)
                assert (moves[-1]['x'],moves[-1]['y'])==(900,650)
                gaps=sorted(b['t']-a['t'] for a,b in zip(moves,moves[1:]))
                middle=(moves[0]['t']+moves[-1]['t'])/2
                point=min(moves,key=lambda p:abs(p['t']-middle))
                progress=((point['x']-100)*800+(point['y']-100)*550)/(800**2+550**2)
                measurements['fitted' if recorded else 'standard']={
                    'frames':len(moves),'travel_ms':round(moves[-1]['t']-moves[0]['t'],2),
                    'gap_median_ms':round(statistics.median(gaps),2),'gap_p95_ms':round(gaps[int((len(gaps)-1)*.95)],2),
                    'gap_max_ms':round(max(gaps),2),'half_time_progress':round(progress,4),'endpoint_error_px':0}
                if recorded:
                    entered=asyncio.Event()
                    original=page.mouse.move
                    async def observed_move(*args,**kwargs):
                        await original(*args,**kwargs)
                        entered.set()
                    page.mouse.move=observed_move
                    job=asyncio.create_task(behavior.click(page,page.locator('button')))
                    await asyncio.wait_for(entered.wait(),3)
                    job.cancel()
                    with pytest.raises(asyncio.CancelledError): await job
                    before=await page.evaluate('moves.length',isolated_context=False)
                    await asyncio.sleep(.05)
                    assert await page.evaluate('moves.length',isolated_context=False)==before
                    assert await page.evaluate('clicks',isolated_context=False)==0
                    assert not paced.lock.locked()
                await context.close()
            await browser.close()
        assert measurements['fitted']['half_time_progress']>measurements['standard']['half_time_progress']+.08
        assert measurements['fitted']['travel_ms']>measurements['standard']['travel_ms']
        output=Path('artifacts/account-care-review/pointer-motion-measurement.json')
        output.write_text(json.dumps({'measurements':measurements,'limitations':'One seeded 971px local Chromium motion per mode. Browser/OS scheduling affects frame gaps; these are observations, not a refresh-rate guarantee. Cancellation emits no click. No retailer requests or purchases.'},indent=2))
    asyncio.run(scenario())
