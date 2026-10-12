import asyncio
import json
from pathlib import Path

from patchright.async_api import async_playwright, expect

from scripts.capture_mouse_study import sanitize_trial


def test_mouse_study_captures_trusted_paths_pause_and_finish():
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            page=await browser.new_page(viewport={'width':1280,'height':800})
            await page.set_content(Path('scripts/mouse_study.html').read_text(encoding='utf-8'))
            cdp=await page.context.new_cdp_session(page)
            async def read(expression):
                result=await cdp.send('Runtime.evaluate',{'expression':expression,'returnByValue':True})
                return result.get('result',{}).get('value')
            await expect(page.get_by_role('button',name='Start study')).to_be_disabled()
            await read('window.__mouseStudyConnect()')
            await page.get_by_role('button',name='Start study').click()
            captured=[]
            for index in range(24):
                await expect(page.locator('#status')).to_have_text(f'{index} / 24 captured')
                target=page.get_by_role('button',name='Highlighted target')
                box=await target.bounding_box()
                assert box and box['x']>=0 and box['y']>=0
                await page.mouse.move(box['x']+box['width']*.45,box['y']+box['height']*.55,steps=20)
                await page.mouse.down()
                await page.mouse.up()
                if index==23:
                    await expect(page.locator('#status')).to_have_text('Capture finished')
                else:
                    await expect(page.locator('#status')).to_have_text(f'{index+1} / 24 captured')
                data=await read('window.__mouseStudyDrain()')
                assert len(data['trials'])==1
                trial=sanitize_trial(data['trials'][0])
                assert trial and trial['index']==index and len(trial['points'])>=10
                assert all(p['t']>=0 for p in trial['points'])
                assert 0<=trial['click']['hold']<1000
                captured.append(trial)
                if index==2:
                    await page.get_by_role('button',name='Pause',exact=True).click()
                    await expect(target).to_be_disabled()
                    await page.mouse.move(40,400,steps=15)
                    assert not (await read('window.__mouseStudyDrain()'))['trials']
                    await page.get_by_role('button',name='Resume',exact=True).click()
                    await expect(target).to_be_enabled()
                    output=Path('artifacts/account-care-review')
                    output.mkdir(parents=True,exist_ok=True)
                    await page.screenshot(path=str(output/'mouse-study-desktop.png'))
                    await page.set_viewport_size({'width':390,'height':844})
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    await page.screenshot(path=str(output/'mouse-study-narrow.png'))
                    await page.set_viewport_size({'width':1280,'height':800})
            assert data['done'] and data['completed']==24
            assert data['commands']==['finish']
            assert len(captured)==24
            # The sanitizer removes all unexpected content, including raw text.
            dirty={**data['trials'][0],'url':'secret','typed_text':'secret'}
            assert 'secret' not in json.dumps(sanitize_trial(dirty))
            assert sanitize_trial({'index':99}) is None
            dirty['points'][0]['x']=float('nan')
            assert sanitize_trial(dirty) is None
            await browser.close()
    asyncio.run(scenario())
