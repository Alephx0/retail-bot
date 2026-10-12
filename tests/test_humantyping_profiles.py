import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from patchright.async_api import async_playwright
import pytest

from retail import behavior
from retail.amazon import Amazon


def test_profiles_scale_pointer_and_click_timings_without_changing_endpoint(monkeypatch):
    async def scenario():
        rows = {}
        for profile in behavior.TIMING_PRESETS:
            page = SimpleNamespace(context=SimpleNamespace(_retail_input_timing_preset=profile),
                                   viewport_size={'width': 1000, 'height': 700},
                                   mouse=SimpleNamespace(move=AsyncMock()))
            clock = [0.0]
            async def sleep(seconds):
                clock[0] += seconds
            monkeypatch.setattr(behavior.asyncio, 'sleep', sleep)
            monkeypatch.setattr(behavior, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
            controller = behavior.PacedInput(page, seed=40)
            await controller.move(800, 500)
            rows[profile] = (clock[0],
                             controller.rng.triangular(35, 130, 65) * controller.duration_scale, page.mouse.move.await_args_list)
        balanced = rows['balanced']
        for profile, scale in behavior.TIMING_PRESETS.items():
            travel, click_hold, path = rows[profile]
            assert travel == pytest.approx(balanced[0] * scale)
            assert click_hold == pytest.approx(balanced[1] * scale)
            assert path[-1] == balanced[2][-1]
            assert len(path) == pytest.approx(len(balanced[2]) * scale, abs=1)
    asyncio.run(scenario())


def test_click_points_stay_inside_and_follow_moving_targets():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(channel='chrome')
            try:
                context = await browser.new_context(viewport={'width': 900, 'height': 700})
                context._retail_paced_input = True
                context._retail_input_timing_preset = 'aggressive'
                page = await context.new_page()
                await page.set_content('''<style>
                    button {position:absolute;left:180px;top:100px;width:200px;height:80px;
                            border:8px solid black; padding:10px;box-sizing:border-box}
                    #tiny{left:60px;top:300px;width:6px;height:6px;border:1px solid black;padding:0}
                    #clipped{position:fixed;left:-100px;top:400px}
                    </style><button id="target">Target</button><button id="tiny"></button>
                    <button id="clipped">Clipped</button><script>
                    window.points=[];window.moves=[];
                    document.addEventListener('mousemove',e=>moves.push([e.clientX,e.clientY]));
                    document.addEventListener('click',e=>{
                        const r=e.target.getBoundingClientRect();
                        points.push({id:e.target.id,x:e.clientX-r.x,y:e.clientY-r.y,
                                     width:r.width,height:r.height, trusted:e.isTrusted});
                    });</script>''')
                page._retail_paced_controller = behavior.PacedInput(page, seed=123)
                target = page.locator('#target')
                for _ in range(10):
                    await behavior.click(page, target)
                points = await page.evaluate('points', isolated_context=False)
                assert len(points) == 10
                assert len({(p['x'], p['y']) for p in points}) > 7
                assert all(p['trusted'] and 8 < p['x'] < 192 and 8 < p['y'] < 72 for p in points)
                assert any(abs(p['x'] - 100) > 5 for p in points)
                # The paced path and actual locator click use the same offset.
                moves = await page.evaluate('moves', isolated_context=False)
                assert abs(moves[-1][0] - (180 + points[-1]['x'])) <= 1
                assert abs(moves[-1][1] - (100 + points[-1]['y'])) <= 1
                # Preserve caller coordinates and dwell time.
                await behavior.click(page, target, position={'x': 15, 'y': 17}, delay=1)
                explicit = (await page.evaluate('points', isolated_context=False))[-1]
                assert explicit['x'] == 23 and explicit['y'] == 25
                await behavior.click(page, await page.locator('#tiny').element_handle())
                await behavior.click(page, page.locator('#clipped'))
                recent = (await page.evaluate('points', isolated_context=False))[-2:]
                assert [p['id'] for p in recent] == ['tiny', 'clipped']
                assert all(0 < p['x'] < p['width'] and 0 < p['y'] < p['height'] for p in recent)
                # A layout shift during travel must still click the target once.
                await page.evaluate("document.addEventListener('mousemove',()=>document.querySelector('#target').style.left='450px',{once:true})")
                await behavior.click(page, target)
                assert len(await page.evaluate('points', isolated_context=False)) == 14
                assert (await page.evaluate('points', isolated_context=False))[-1]['id'] == 'target'
                # Native actionability prevents obstructed/disabled clicks.
                await target.evaluate('e=>e.disabled=true')
                with pytest.raises(Exception, match='Timeout'):
                    await behavior.click(page, target, timeout=100)
                await target.evaluate('e=>e.disabled=false')
                await page.evaluate("document.body.insertAdjacentHTML('beforeend','<div style=\"position:fixed;inset:0;z-index:99\">Blocked</div>')")
                with pytest.raises(Exception, match='Timeout'):
                    await behavior.click(page, target, timeout=100)
                assert len(await page.evaluate('points', isolated_context=False)) == 14
            finally:
                await browser.close()
    asyncio.run(asyncio.wait_for(scenario(), 30))


@pytest.mark.parametrize('profile', ['slow', 'balanced', 'fast', 'aggressive'])
def test_automatic_signin_uses_paced_profile_on_fixture(profile):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(channel='chrome')
            try:
                context = await browser.new_context()
                context._retail_paced_input = True
                context._retail_input_timing_preset = profile
                page = await context.new_page()
                await page.route('**/*', lambda route: route.fulfill(body='<body></body>', content_type='text/html'))
                await page.goto('https://www.amazon.com/ap/signin')
                await page.set_content('''<style>input,button{display:block;margin:15px;width:220px;height:36px}</style>
                    <input id="ap_email"><button id="continue">Continue</button>
                    <input id="ap_password" type="password" hidden style="display:none">
                    <button id="signInSubmit" hidden style="display:none">Sign in</button>
                    <script>window.submits=0;window.values=[];window.keys=[];window.clicks=[];
                    document.addEventListener('keydown',e=>keys.push(e.key));
                    document.addEventListener('click',e=>{const r=e.target.getBoundingClientRect();
                        clicks.push({id:e.target.id,x:e.clientX-r.x,y:e.clientY-r.y,w:r.width,h:r.height});},true);
                    document.querySelector('#continue').onclick=()=>{
                        submits++;values.push(document.querySelector('#ap_email').value);
                        document.querySelector('#ap_email').remove();document.querySelector('#continue').remove();
                        for(const id of ['ap_password','signInSubmit']){const e=document.getElementById(id);e.hidden=false;e.style.display='block';}
                    };
                    document.querySelector('#signInSubmit').onclick=()=>{
                        submits++;values.push(document.querySelector('#ap_password').value);
                        document.querySelector('#ap_password').remove();document.querySelector('#signInSubmit').remove();
                        document.body.insertAdjacentHTML('beforeend','<div id="nav-link-accountList"><span class="nav-line-1">Hello, Fixture</span></div>');
                    };</script>''')
                page._retail_paced_controller = behavior.PacedInput(page, seed=42)
                await Amazon(None).authenticate(page, {'email': 'a@b.test', 'password': 'Ab+9!'})
                assert behavior.controller(page).duration_scale == behavior.TIMING_PRESETS[profile]
                assert await page.evaluate('values', isolated_context=False) == ['a@b.test', 'Ab+9!']
                assert await page.evaluate('submits', isolated_context=False) == 2
                keys = await page.evaluate('keys', isolated_context=False)
                assert len(keys) >= 13 and 'Enter' not in keys and keys.count('Backspace') <= 1
                clicks = await page.evaluate('clicks', isolated_context=False)
                assert len(clicks) == 4
                assert all(0 < c['x'] < c['w'] and 0 < c['y'] < c['h'] for c in clicks)
            finally:
                await browser.close()
    asyncio.run(asyncio.wait_for(scenario(), 30))
