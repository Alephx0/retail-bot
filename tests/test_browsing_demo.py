import asyncio
import json
from pathlib import Path

from patchright.async_api import async_playwright, expect

from scripts.capture_browsing_demo import sanitize


def test_demo_sanitizer_drops_text_and_unexpected_fields():
    result=sanitize({'type':'key','page':'search','t':123,'hold':60,'category':'character',
                     'key':'secret','code':'KeyS','value':'password','url':'https://private.test'})
    assert result=={'type':'key','page':'search','t':123,'hold':60,'category':'character'}
    assert sanitize({'type':'key','page':'excluded'}) is None
    assert sanitize({'type':'password','page':'home'}) is None


def test_demo_real_input_privacy_pause_finish_and_navigation():
    async def scenario():
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            context=await browser.new_context(viewport={'width':1200,'height':900})
            await context.add_init_script(path=str(Path('scripts/browser_demo_capture.js').resolve()))
            page=await context.new_page()
            await page.route('**/*',lambda route:route.fulfill(content_type='text/html',body='''<html><body style="height:3000px">
                <input id="twotabsearchtextbox"><input type="password" id="private-password"><a href="/dp/B000000001">Product</a>
                </body></html>'''))
            cdp=await context.new_cdp_session(page)
            async def state(recording=True,paused=False):
                await cdp.send('Runtime.evaluate',{'expression':'window.__retailDemoSetState('+json.dumps({'recording':recording,'paused':paused})+')'})
            async def drain():
                result=await cdp.send('Runtime.evaluate',{'expression':'window.__retailDemoDrain()','returnByValue':True})
                return result['result']['value']
            await page.goto('https://www.amazon.com/')
            await state()
            await expect(page.locator('#retail-demo-recorder')).to_contain_text('Recording mouse')
            await page.locator('#twotabsearchtextbox').press_sequentially('coffee',delay=40)
            await page.mouse.move(100,100)
            await page.mouse.move(250,180,steps=10)
            await page.mouse.wheel(0,300)
            await page.get_by_role('link',name='Product').click()
            await state()
            await page.locator('#twotabsearchtextbox').press_sequentially('mug',delay=40)
            captured=await drain()
            assert sum(e['type']=='key' for e in captured['events'])==3
            assert 'mug' not in json.dumps(captured) and 'coffee' not in json.dumps(captured)
            await page.locator('#private-password').press_sequentially('SECRET',delay=30)
            assert not any(e['type']=='key' for e in (await drain())['events'])
            await page.get_by_role('button',name='Pause',exact=True).click()
            assert (await drain())['commands']==['pause']
            await state(paused=True)
            await page.mouse.move(300,400)
            assert not (await drain())['events']
            await page.goto('https://www.amazon.com/s?k=not-recorded')
            await state(paused=True)
            await expect(page.get_by_role('button',name='Resume',exact=True)).to_be_visible()
            await page.get_by_role('button',name='Resume',exact=True).click()
            assert (await drain())['commands']==['resume']
            await state()
            await page.mouse.move(300,100)
            await page.mouse.wheel(0,120)
            await page.wait_for_function('scrollY > 0',timeout=2000)
            events=(await drain())['events']
            assert any(e['type']=='move' for e in events) and any(e['type']=='wheel' for e in events)
            await page.get_by_role('button',name='Finish capture').click()
            assert (await drain())['commands']==['finish']
            await state(recording=False)
            await page.locator('#twotabsearchtextbox').press_sequentially('ignored')
            assert not (await drain())['events']
            await page.goto('https://www.amazon.com/ap/signin')
            await state()
            await page.locator('#twotabsearchtextbox').press_sequentially('ignored')
            await page.mouse.move(30,30)
            assert not (await drain())['events']
            await browser.close()
    asyncio.run(scenario())
