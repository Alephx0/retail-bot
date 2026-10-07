"""Measure actual Patchright input events on an isolated local fixture, never a retailer."""
import argparse
import asyncio
from collections import defaultdict
import json
import math
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from retail import behavior
from retail.amazon import Amazon
from retail.store import Store
from scripts.analyze_behavior_datasets import distribution

FIXTURE = '''<!doctype html><html><body style="font:18px sans-serif;padding:40px">
<h1>Local interaction audit</h1><label>Email <input id="email" type="email"></label>
<label>Password <input id="password" type="password"></label><div style="height:1100px"></div>
<button id="continue" style="padding:20px" onclick="window.completed++">Continue fixture</button>
<script>window.completed=0;window.events=[];
for(const type of ['mousemove','mousedown','mouseup','wheel','keydown','keyup','input'])
document.addEventListener(type,e=>events.push({type,t:performance.now(),x:e.clientX,y:e.clientY,
 code:e.code,target:e.target.id || '',button:e.button,dy:e.deltaY,trusted:e.isTrusted}),true);
</script></body></html>'''


def samples(events):
    result = defaultdict(list)
    previous_move = None
    mouse_down = None
    pressed = {}
    strokes = []
    previous_wheel = None
    for event in events:
        t, kind = event['t'], event['type']
        if kind == 'mousemove':
            if previous_move:
                dt = t - previous_move['t']
                distance = math.hypot(event['x']-previous_move['x'], event['y']-previous_move['y'])
                if 0 < dt <= 1000 and distance > 0:
                    result['move_interval_ms'].append(dt)
                    result['speed_css_px_s'].append(distance / dt * 1000)
            previous_move = event
        elif kind == 'mousedown':
            mouse_down = t
        elif kind == 'mouseup' and mouse_down is not None:
            result['click_hold_ms'].append(t-mouse_down)
            mouse_down = None
        elif kind == 'keydown':
            pressed[event['code']] = event
        elif kind == 'keyup' and event['code'] in pressed:
            down = pressed.pop(event['code'])
            # Modifier overlap is not overlap between consecutive typed letters.
            # Keep it separate when comparing with character-timing datasets.
            if event['code'] in ('ShiftLeft','ShiftRight','ControlLeft','ControlRight',
                                  'AltLeft','AltRight','MetaLeft','MetaRight'):
                result['modifier_hold_ms'].append(t-down['t'])
            else:
                result['hold_ms'].append(t-down['t'])
                strokes.append({'down':down['t'], 'up':t, 'target':down.get('target')})
        elif kind == 'wheel':
            result['wheel_delta_css_px'].append(event['dy'])
            if previous_wheel is not None and 0 < t-previous_wheel <= 1000:
                result['wheel_interval_ms'].append(t-previous_wheel)
            previous_wheel = t
    strokes.sort(key=lambda s:s['down'])
    for previous, current in zip(strokes, strokes[1:]):
        if previous['target'] == current['target']:
            result['keydown_interval_ms'].append(current['down']-previous['down'])
            result['keyup_keydown_ms'].append(current['down']-previous['up'])
    return dict(result)


def metrics(events):
    return {key: distribution(value) for key, value in samples(events).items()}


async def audit(args):
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='retail-behavior-') as temp:
        store = Store(Path(temp))
        adapter = Amazon(store)
        account = store.put('accounts', {'name': 'Local input fixture', 'region': 'US'}, 'input-fixture')
        try:
            for case, mode in [('standard','off'), ('paced','paced'), ('restored','off')]:
                for repeat in range(1, args.repeats+1):
                    store.put('settings', {'browser_channel':'chrome', 'interaction_pacing':mode}, 'settings')
                    context = await adapter.context(account)
                    await context.route('**/*', lambda r:r.fulfill(content_type='text/html',body=FIXTURE))
                    page = await context.new_page()
                    row = {'case':case,'repeat':repeat,'browser':adapter.browser.version}
                    try:
                        await page.goto('https://behavior-audit.test/')
                        started = time.monotonic()
                        await behavior.fill(page, page.locator('#email'), 'fixture@example.test')
                        await behavior.fill(page, page.locator('#password'), 'Ab+9@_fixture')
                        await behavior.click(page, page.locator('#continue'))
                        row['elapsed_seconds'] = round(time.monotonic()-started,3)
                        row['completed_once'] = await page.evaluate('completed === 1', isolated_context=False)
                        row['values_correct'] = await page.locator('#email').input_value() == 'fixture@example.test' and await page.locator('#password').input_value() == 'Ab+9@_fixture'
                        events = await page.evaluate('events', isolated_context=False)
                        row['trusted_events'] = all(event['trusted'] for event in events)
                        row['counts'] = {kind:sum(e['type']==kind for e in events) for kind in {e['type'] for e in events}}
                        row['metrics'] = metrics(events)
                        row['scroll_y'] = await page.evaluate('scrollY')
                        (args.output/f'{case}-{repeat}-events.json').write_text(json.dumps(events,indent=2),encoding='utf-8')
                        if repeat == 1:
                            await page.screenshot(path=str(args.output/f'{case}.png'))
                    except Exception as error:
                        row['error'] = str(error)
                    finally:
                        await context.close()
                    rows.append(row)
                    (args.output/'results.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
                    print(case, repeat, row.get('elapsed_seconds'), row.get('error', 'complete'), flush=True)
        finally:
            await adapter.close()
            store.db.close()
    checks = {'complete': len(rows)==3*args.repeats and all(not r.get('error') and r.get('completed_once') and r.get('values_correct') and r.get('trusted_events') for r in rows),
              'paced_events_present': all(r.get('counts',{}).get('mousemove',0)>10 and r.get('counts',{}).get('keydown',0)>=30 and r.get('counts',{}).get('wheel',0)>=3 for r in rows if r['case']=='paced'),
              'restored_native_fill':all(r.get('counts',{}).get('keydown',0)==0 for r in rows if r['case']!='paced')}
    (args.output/'acceptance.json').write_text(json.dumps({'passed':all(checks.values()),'checks':checks},indent=2),encoding='utf-8')
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('artifacts/behavior-audit'))
    parser.add_argument('--repeats',type=int,choices=range(3,6),default=3)
    sys.exit(asyncio.run(audit(parser.parse_args())))
