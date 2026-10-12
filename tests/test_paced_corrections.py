import asyncio
import math
import pytest
from patchright.async_api import async_playwright
from retail.behavior import PacedInput

@pytest.mark.parametrize('width',[1100,390])
def test_corrected_typing_and_visible_pointer_curve(width,tmp_path):
    async def scenario():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            context=await browser.new_context(viewport={'width':width,'height':800})
            context._retail_input_timing_preset='aggressive'
            page=await context.new_page()
            await page.set_content("""<style>label{display:block;margin:24px}input{display:block}</style>
                <label>Name<input id=name name=customerName></label><label>Password<input id=password type=password></label>
                <label>Code<input id=code autocomplete=one-time-code></label>
                <script>window.events=[];document.addEventListener('keydown',e=>events.push([e.target.id,e.key]));</script>""")
            paced=PacedInput(page,seed=40)
            for field,value in [('name','Example Name'),('password','SecretValue'),('code','ABCDEF')]:
                await paced.fill(page.locator('#'+field),value)
                assert await page.locator('#'+field).input_value()==value
            events=await page.evaluate('events',isolated_context=False)
            assert [field for field,key in events if key=='Backspace']==['name']
            assert all(key!='Enter' for field,key in events)
            await page.evaluate('window.points=[];addEventListener("mousemove",e=>points.push([e.clientX,e.clientY]))',isolated_context=False)
            paced.position=(20,400);await page.mouse.move(20,400)
            end=(width-20,650);await paced.move(*end)
            points=await page.evaluate('points',isolated_context=False)
            dx,dy=end[0]-20,250;distance=math.hypot(dx,dy)
            assert max(abs(dx*(y-400)-dy*(x-20))/distance for x,y in points)>25
            assert paced.position==end
            await page.screenshot(path=str(tmp_path/f'paced-{width}.png'))
            await browser.close()
    asyncio.run(scenario())
