"""Timing controls tested on local pages; no retailer requests or purchases."""
import asyncio
import json
from pathlib import Path
import time

import pytest
from patchright.async_api import async_playwright

from retail import behavior
from retail.amazon import Amazon
from retail.models import Account
from retail.store import Store


def test_presets_change_input_duration_without_changing_text_or_click_count():
    async def scenario():
        results={}
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            try:
                for preset in behavior.TIMING_PRESETS:
                    context=await browser.new_context(viewport={'width':800,'height':600})
                    context._retail_paced_input=True
                    context._retail_input_timing_preset=preset
                    page=await context.new_page()
                    await page.set_content('''<input style="margin:50px" id="text"><button>Finish</button>
                        <script>window.events=[];document.addEventListener('keydown',e=>events.push(e.key));
                        window.clicks=0;document.querySelector('button').onclick=()=>clicks++;</script>''')
                    page._retail_paced_controller=behavior.PacedInput(page,seed=41)
                    started=time.perf_counter()
                    await behavior.fill(page,page.locator('#text'),'Ab+2! xy')
                    await behavior.click(page,page.locator('button'))
                    results[preset]=round((time.perf_counter()-started)*1000,2)
                    assert await page.locator('#text').input_value()=='Ab+2! xy'
                    assert await page.evaluate('clicks',isolated_context=False)==1
                    assert 'Enter' not in await page.evaluate('events',isolated_context=False)
                    # A page's controller stays fixed for its session.
                    context._retail_input_timing_preset='slow' if preset!='slow' else 'fast'
                    assert behavior.controller(page).preset==preset
                    await context.close()
            finally:
                await browser.close()
        assert results['slow'] > results['balanced'] > results['fast'] > results['aggressive']
        artifact=Path('artifacts/input-timing/durations.json')
        artifact.parent.mkdir(parents=True,exist_ok=True)
        artifact.write_text(json.dumps({'fixture_ms':results,'note':'Single local sample per preset; includes browser overhead, excludes retailer network.'},indent=2))
    asyncio.run(scenario())


def test_new_browser_inherits_preset_and_existing_browser_keeps_it(tmp_path):
    async def scenario():
        store=Store(tmp_path)
        settings={'show_browser_window':False,'browser_channel':'chromium',
                  'fingerprint_proxy_location':False,'interaction_pacing':'off','input_timing_preset':'fast',
                  'show_mouse_cursor':True}
        store.put('settings',settings,'settings')
        account=store.put('accounts',Account(name='Fixture').model_dump())
        adapter=Amazon(store)
        try:
            context=await adapter.context(account)
            page=await context.new_page()
            assert context._retail_input_timing_preset=='fast'
            await page.goto('data:text/html,<button>Fixture</button>')
            assert await page.locator('#__retail_visible_mouse').count()==1
            assert behavior.controller(page) is None  # Standard clicks stay standard.
            # Credential entry constructs PacedInput even when ordinary input is off.
            assert behavior.PacedInput(page).preset=='fast'
            store.put('settings',{**settings,'input_timing_preset':'slow','show_mouse_cursor':False},'settings')
            assert behavior.PacedInput(page).preset=='fast'
            assert await page.locator('#__retail_visible_mouse').count()==1
            await context.close()
            if adapter.profile_close_tasks:await asyncio.gather(*adapter.profile_close_tasks)
            newer=await adapter.context(account)
            assert newer._retail_input_timing_preset=='slow'
            new_page=await newer.new_page()
            await new_page.goto('data:text/html,<button>Fixture</button>')
            assert await new_page.locator('#__retail_visible_mouse').count()==0
            await newer.close()
        finally:
            await adapter.close();store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('preset',list(behavior.TIMING_PRESETS))
def test_cancelling_movement_never_clicks(preset):
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch()
            try:
                page=await browser.new_page()
                page.context._retail_paced_input=True
                page.context._retail_input_timing_preset=preset
                await page.set_content('<button style="margin:200px" onclick="window.clicked=true">Fixture</button>')
                moved=asyncio.Event()
                original=page.mouse.move
                async def first_move(*args,**kwargs):
                    await original(*args,**kwargs)
                    moved.set()
                    await asyncio.Event().wait()
                page.mouse.move=first_move
                operation=asyncio.create_task(behavior.click(page,page.locator('button')))
                await asyncio.wait_for(moved.wait(),5)
                operation.cancel()
                with pytest.raises(asyncio.CancelledError):await operation
                assert not await page.evaluate('Boolean(window.clicked)',isolated_context=False)
                assert not behavior.controller(page).lock.locked()
            finally:
                await browser.close()
    asyncio.run(scenario())
