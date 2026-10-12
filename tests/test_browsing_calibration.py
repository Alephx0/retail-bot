import asyncio
import copy
import json
from pathlib import Path
import random
import statistics
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from patchright.async_api import async_playwright
import pytest

from retail import behavior
from retail.account_care import reading_scroll_times
from retail.app import create_app
from retail.models import InputCalibration, Settings
from test_account_care import fixture, until


CALIBRATION = {
    'key_hold_ms': {'low':51.1, 'median':83.3, 'high':103},
    'key_gap_ms': {'low':39.4, 'median':80.6, 'high':179.8},
    'click_hold_ms': {'low':76, 'median':110.4, 'high':161.4},
    'key_samples':36, 'click_samples':24,
    'pointer':None,
}


def test_calibration_validation_scope_and_focused_settings_edits(tmp_path):
    with TestClient(create_app(tmp_path), headers={'X-Retail-Client':'dashboard'}) as client:
        assert client.post('/api/settings',json={'input_calibration_scope':'browsing'}).status_code==422
        assert client.post('/api/settings',json={'input_calibration':CALIBRATION,'input_calibration_scope':'browsing'}).status_code==200
        assert client.post('/api/settings',json={'sound_volume':.7}).status_code==200
        saved = client.get('/api/state').json()['settings'][0]
        assert saved['input_calibration']==CALIBRATION and saved['input_calibration_scope']=='browsing'
        assert behavior.calibration_for(saved) is None  # Never reaches purchasing contexts.
        sample = behavior.calibration_for(saved,browsing=True)
        sample['key_hold_ms']['median']=100
        assert saved['input_calibration']==CALIBRATION
        assert client.post('/api/settings',json={'input_calibration_scope':'all'}).status_code==422
        assert client.post('/api/settings',json={'input_calibration_scope':'off'}).status_code==200
        saved = client.get('/api/state').json()['settings'][0]
        assert saved['input_calibration']==CALIBRATION
        assert behavior.calibration_for(saved,browsing=True) is None
    for patch in ({'key_samples':1}, {'key_gap_ms':{'low':200,'median':100,'high':300}},
                  {'click_hold_ms':{'low':20,'median':50,'high':1000}}):
        with pytest.raises(ValueError):
            InputCalibration.model_validate({**CALIBRATION, **patch})


def test_calibration_is_frozen_at_start_even_while_browser_queued(tmp_path):
    async def scenario():
        store, engine, (a,b) = fixture(tmp_path)
        saved = store.get('settings','settings')
        store.put('settings',{**saved,'input_calibration':CALIBRATION,'input_calibration_scope':'browsing'})
        care=engine.account_care
        care.dwell=AsyncMock()
        await engine.browser_slots.acquire()
        await care.start(a['id'])
        store.put('settings',{**saved,'input_calibration':CALIBRATION,'input_calibration_scope':'off'})
        engine.browser_slots.release()
        await until(lambda:not care.runs)
        assert engine.amazon.contexts[0]._retail_input_calibration==CALIBRATION
        await care.start(b['id'])
        await until(lambda:not care.runs)
        assert engine.amazon.contexts[1]._retail_input_calibration is None
        assert all(c._retail_browsing_input for c in engine.amazon.contexts)
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_calibration_preserves_standard_typing_and_scales_all_profiles():
    text='coffee grinder and travel mug'
    expected=[]
    rng=random.Random(21)
    for char,interval in zip(text,behavior.keystroke_intervals(text,rng)):
        hold=min(rng.triangular(35,125,65),interval*1000-25)
        expected.append((char,hold,interval-hold/1000))
    standard=list(behavior._typing_timings(text,random.Random(21),'balanced'))
    for actual,original in zip(standard,expected):
        assert actual[0]==original[0]
        assert actual[1:]==pytest.approx(original[1:])
    learned=list(behavior._typing_timings(text,random.Random(21),'balanced',CALIBRATION))
    assert statistics.median(r[1] for r in learned)>statistics.median(r[1] for r in standard)
    assert len(set(round(r[1],2) for r in learned))>15
    for preset,scale in behavior.TIMING_PRESETS.items():
        result=list(behavior._typing_timings(text,random.Random(21),preset,CALIBRATION))
        for row,base in zip(result,learned):
            assert row[1:]==pytest.approx([v*scale for v in base[1:]])


def test_reading_scroll_schedule_varies_within_saved_budget():
    for seconds in (15,30,120):
        plans=[list(reading_scroll_times(seconds,random.Random(seed))) for seed in range(20)]
        assert len({tuple(p) for p in plans})==20
        for plan in plans:
            assert 2<=len(plan)<=12
            assert 0<plan[0]<plan[-1]<seconds
            gaps=[b-a for a,b in zip([0,*plan], [*plan,seconds])]
            assert max(gaps)-min(gaps)>.1


def test_cancellation_during_reading_does_not_scroll(tmp_path):
    async def scenario():
        store,engine,_=fixture(tmp_path)
        care=engine.account_care
        context=SimpleNamespace(_retail_paced_input=True)
        page=SimpleNamespace(context=context)
        paced=behavior.PacedInput(page,seed=1)
        paced.scroll=AsyncMock()
        page._retail_paced_controller=paced
        checked=asyncio.Event()
        async def inspect(*_): checked.set()
        care.inspect=inspect
        job=asyncio.create_task(care.dwell(page,30,'www.amazon.com'))
        await checked.wait()
        job.cancel()
        with pytest.raises(asyncio.CancelledError): await job
        paced.scroll.assert_not_awaited()
        await engine.close()
        store.db.close()
    asyncio.run(scenario())


def test_calibrated_input_in_real_browser_and_moving_scroll():
    async def scenario():
        rows={}
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            for calibrated in (False,True):
                context=await browser.new_context(viewport={'width':1000,'height':800})
                context._retail_paced_input=True
                context._retail_browsing_input=True
                context._retail_input_calibration=copy.deepcopy(CALIBRATION) if calibrated else None
                page=await context.new_page()
                await page.set_content('''<body style="height:4000px"><input style="position:fixed;left:200px;top:200px;width:300px" id="search">
                    <button style="position:fixed;left:200px;top:300px;width:200px;height:50px" id="details">Details</button>
                    <script>window.events=[];for(const type of ['mousemove','mousedown','mouseup','keydown','keyup','wheel'])
                    addEventListener(type,e=>{events.push({type,t:performance.now(),trusted:e.isTrusted,x:e.clientX,y:e.clientY,dy:e.deltaY});
                    if(type==='wheel')document.body.dataset.wheel=String((Number(document.body.dataset.wheel)||0)+e.deltaY);},true);</script>''')
                paced=page._retail_paced_controller=behavior.PacedInput(page,seed=21)
                await behavior.fill(page,page.locator('#search'),'coffee grinder and travel mug')
                for _ in range(4): await behavior.click(page,page.locator('#details'))
                events=await page.evaluate('events',isolated_context=False)
                down=[e for e in events if e['type']=='keydown']
                up=[e for e in events if e['type']=='keyup']
                md=[e for e in events if e['type']=='mousedown']
                mu=[e for e in events if e['type']=='mouseup']
                assert await page.locator('#search').input_value()=='coffee grinder and travel mug'
                assert len(down)==len(up)==29
                assert len(md)==len(mu)==5
                assert all(e['trusted'] for e in events)
                assert len({(e['x'],e['y']) for e in md[1:]})>=3
                rows['calibrated' if calibrated else 'standard']={
                    'characters':len(down), 'clicks':len(md),
                    'key_hold_median_ms':round(statistics.median(b['t']-a['t'] for a,b in zip(down,up)),2),
                    'click_hold_median_ms':round(statistics.median(b['t']-a['t'] for a,b in zip(md,mu)),2),
                }
                start=len(events)
                await paced.scroll(300)
                await page.wait_for_function('scrollY > 0')
                await page.wait_for_function('Number(document.body.dataset.wheel) >= 299.99',timeout=2000)
                extra=(await page.evaluate('events',isolated_context=False))[start:]
                assert sum(e['type']=='mousemove' for e in extra)>=3
                wheel_times=[e['t'] for e in extra if e['type']=='wheel']
                assert any(wheel_times[0]<e['t']<wheel_times[-1] for e in extra if e['type']=='mousemove')
                assert sum(e.get('dy',0) for e in extra if e['type']=='wheel')==pytest.approx(300,abs=2)
                await context.close()
            await browser.close()
        output=Path('artifacts/account-care-review/calibrated-input-measurement.json')
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps({'samples':rows,'limitations':'One seeded local Chromium fixture, 29 characters and 5 clicks per mode. Timing includes browser/OS overhead. No retailer traffic, real purchases or live AI calls.'},indent=2))
    asyncio.run(scenario())
