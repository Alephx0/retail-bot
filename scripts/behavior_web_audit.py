"""Compare existing input modes on allowlisted public behavioral test demos.

Uses temporary accounts, synthetic text, and ordinary browser input. Does not
modify detector code, scores, request bodies, or application pacing parameters.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from retail import behavior
from retail.amazon import Amazon
from retail.store import Store

SITES = {
    'sentinel': 'https://sentinel-bot-detector.vercel.app/',
    'webdecoy': 'https://webdecoy.com/product/fcaptcha-demo/',
    'incolumitas': 'https://bot.incolumitas.com/',
    'detectionlab': 'https://www.detectionlab.app/challenge/login',
    'apivoid': 'https://www.apivoid.com/tools/bot-detection-test/',
}
TEXT = 'Public demo input test with synthetic text.'
EMAIL = 'fixture@example.test'


async def sentinel(page, row):
    area = page.get_by_text('Move your mouse or finger here', exact=True).locator('..')
    await area.wait_for(state='visible')
    box = await area.bounding_box()
    # Fixed task coordinates, identical for both modes. No detector feedback.
    points = [(.15,.3),(.8,.6),(.25,.7),(.7,.25),(.4,.55),(.85,.75),
              (.2,.2),(.6,.65),(.3,.45),(.75,.4),(.45,.75),(.7,.6)]
    paced = behavior.controller(page)
    for fx, fy in points:
        x, y = box['x']+box['width']*fx, box['y']+box['height']*fy
        if paced:
            await paced.move(x, y)
        else:
            await page.mouse.move(x, y)
    row['after_mouse'] = await page.locator('body').inner_text()
    first = page.get_by_placeholder('Type something here...', exact=True)
    second = page.get_by_placeholder('your@email.com', exact=True)
    await behavior.fill(page, first, TEXT)
    await behavior.fill(page, second, EMAIL)
    row['values_correct'] = await first.input_value() == TEXT and await second.input_value() == EMAIL
    await asyncio.sleep(.5)
    body = await page.locator('body').inner_text()
    match = re.search(r'ANALYSIS DASHBOARD\s+(\d+)\s+HUMAN SCORE\s+(\w+)', body)
    row['score'] = int(match[1]) if match else None
    row['verdict'] = match[2] if match else None
    row['result_text'] = body
    row['score_direction'] = 'higher is more human; missing signals excluded'


async def webdecoy(page, row):
    await page.locator('.fcaptcha-widget').wait_for(state='attached')
    first, second = page.locator('#demo-email'), page.locator('#demo-message')
    await behavior.fill(page, first, EMAIL)
    await behavior.fill(page, second, TEXT)
    row['values_correct'] = await first.input_value() == EMAIL and await second.input_value() == TEXT
    await behavior.click(page, page.locator('#invisible-form button'))
    await page.wait_for_function("""() => {
      const e = document.querySelector('#invisible-status');
      return e && /allowed|blocked|unavailable/i.test(e.textContent);
    }""", timeout=30000)
    row['verdict'] = await page.locator('#invisible-status').inner_text()
    row['result_text'] = await page.locator('#invisible-result').inner_text()
    score = await page.locator('#score-value').text_content()
    row['score'] = float(score) if score and re.fullmatch(r'\d+(?:\.\d+)?', score.strip()) else None
    row['score_direction'] = 'lower is less bot-like'


async def incolumitas(page, row):
    page.on('dialog', lambda dialog: dialog.accept())
    first, second = page.locator('[name=userName]'), page.locator('[name=eMail]')
    await behavior.fill(page, first, 'PublicDemoTest')
    await behavior.fill(page, second, EMAIL)
    row['values_correct'] = await first.input_value() == 'PublicDemoTest' and await second.input_value() == EMAIL
    await page.locator('[name=cookies]').select_option(index=1)
    await behavior.click(page, page.locator('[name=terms]'))
    await behavior.click(page, page.locator('#smolCat'))
    await behavior.click(page, page.locator('#submit'))
    # The demo reveals a local table. Interact only with its price-update buttons.
    for button in await page.locator('table button').all():
        if await button.is_visible():
            await behavior.click(page, button)
    row['table_text'] = await page.locator('table').all_inner_texts()
    # Published last score update is at 15s after navigation.
    remaining = 17 - await page.evaluate('performance.now() / 1000')
    if remaining > 0:
        await asyncio.sleep(remaining)
    row['result_text'] = await page.locator('#behavioralScore').inner_text()
    match = re.search(r'\b(?:0(?:\.\d+)?|1(?:\.0+)?)\b', row['result_text'])
    row['score'] = float(match[0]) if match else None
    row['verdict'] = 'score available' if match else 'no behavioral score returned'
    row['score_direction'] = 'higher is more human'


async def detectionlab(page, row):
    # Public sandbox form: generate input telemetry without submitting a login.
    first, second = page.locator('#email'), page.locator('#password')
    password = 'SyntheticDemo42!'
    await behavior.fill(page, first, EMAIL)
    await behavior.fill(page, second, password)
    row['values_correct'] = await first.input_value() == EMAIL and await second.input_value() == password
    await behavior.click(page, page.locator('label.login-checkbox'))
    # The site's collector uploads periodically and flushes on navigation.
    # Use the same settling interval for both modes, without changing telemetry.
    await asyncio.sleep(3)
    await behavior.click(page, page.get_by_role('link',name='Report',exact=True))
    await page.wait_for_function("""() => {
      const e = document.querySelector('#score-number');
      return e && /^\\d/.test(e.textContent);
    }""",timeout=30000)
    row['score'] = float(await page.locator('#score-number').inner_text())
    row['verdict'] = await page.locator('#score-verdict').inner_text()
    row['result_text'] = await page.locator('#report-content').inner_text()
    row['signals'] = await page.locator('#signal-list .score-signal').evaluate_all("""es => es.map(e => ({
      name:e.querySelector('.score-signal__name').textContent,
      score:Number(e.querySelector('.score-signal__value').textContent)
    }))""")
    row['score_direction'] = 'higher is more human'
    row['task'] = 'Fill sandbox email/password, click Remember me, open Report; no login submitted.'


async def apivoid(page, row):
    # APIVoid evaluates browser/network properties automatically, not typing.
    row['values_correct'] = True  # This site has no input task.
    row['task'] = 'Wait for the public automatic browser risk assessment.'
    for _ in range(30):
        body = await page.locator('body').inner_text()
        match = re.search(r'\b(\d{1,3})\s*(?:/\s*100\s*)?Risk Score\b', body, re.I)
        if match and 0 <= int(match[1]) <= 100:
            row['score'] = int(match[1])
            row['verdict'] = 'browser risk score returned'
            row['result_text'] = body
            row['score_direction'] = 'lower is less bot-like'
            return
        await asyncio.sleep(1)
    raise RuntimeError('APIVoid returned no recognizable risk score; not counted as a pass')


async def audit(args):
    args.output.mkdir(parents=True, exist_ok=True)
    settings = {'browser_channel':'chrome', 'fingerprint_backend':'javascript'}
    for surface in ('canvas','webgl','webgpu','audio','workers','fonts','navigator','screen'):
        settings['fingerprint_'+surface] = args.profile == 'modified'
    manifest = {'sites': {s:SITES[s] for s in args.sites}, 'repeats':args.repeats,
                'settings':settings, 'proxy':None, 'headless':True,
                'notes':['Fresh context per run; same temporary account profile.',
                         'Standard/paced/restored repeated as blocks; no human control.',
                         'No score optimization or detector mutation.'],
                'source_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in (Path(__file__), ROOT/'retail/behavior.py')}}
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    rows = []
    with tempfile.TemporaryDirectory(prefix='retail-public-input-') as temp:
        store = Store(Path(temp))
        adapter = Amazon(store)
        account = store.put('accounts', {'name':'Public input demo', 'region':'US'}, 'public-input')
        try:
            for site in args.sites:
                for repeat in range(1, args.repeats+1):
                    for case, mode in [('standard','off'),('paced','paced'),('restored','off')]:
                        store.put('settings',dict(settings,interaction_pacing=mode),'settings')
                        context = await adapter.context(account)
                        page = await context.new_page()
                        page.set_default_timeout(15000)
                        errors, failures = [], []
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        page.on('requestfailed',lambda r:failures.append({'url':r.url.split('?')[0], 'failure':r.failure}))
                        row = {'site':site,'case':case,'repeat':repeat,'browser':adapter.browser.version}
                        stem = f'{site}-{case}-{repeat}'
                        try:
                            response = await page.goto(SITES[site],wait_until='domcontentloaded',timeout=30000)
                            row['http_status'] = response.status if response else None
                            if response and response.status >= 400:
                                row['result_text'] = await page.locator('body').inner_text()
                                await page.screenshot(path=str(args.output/f'{stem}.png'))
                                raise RuntimeError(f'HTTP {response.status}: test page unavailable; no score')
                            await asyncio.sleep(2)
                            row['before'] = await page.locator('body').inner_text()
                            started = time.monotonic()
                            await globals()[site](page,row)
                            row['interaction_seconds'] = round(time.monotonic()-started,3)
                            # DOM text is evidence; observation does not change scoring.
                            row['after'] = await page.locator('body').inner_text()
                            await page.screenshot(path=str(args.output/f'{stem}.png'),full_page=True)
                        except Exception as error:
                            row['error'] = str(error)
                        finally:
                            row['page_errors'], row['failed_requests'] = errors, failures
                            await context.close()
                        rows.append(row)
                        (args.output/'results.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
                        print(json.dumps({k:row[k] for k in ('site','case','repeat','score','verdict','error') if k in row}),flush=True)
        finally:
            await adapter.close()
            store.db.close()
    checks = {
        'input_completed': all(not r.get('error') and r.get('values_correct') for r in rows),
        'all_sites_returned_scores': all(r.get('score') is not None for r in rows),
    }
    (args.output/'acceptance.json').write_text(json.dumps({
        'checks':checks, 'sessions':len(rows),
        'note':'A completed input task and an available score do not imply human equivalence.',
    },indent=2),encoding='utf-8')
    return int(not all(checks.values()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('artifacts/behavior-web-audit'))
    parser.add_argument('--repeats',type=int,choices=range(1,6),default=3)
    parser.add_argument('--sites',nargs='+',choices=list(SITES),default=list(SITES))
    parser.add_argument('--profile',choices=['baseline','modified'],default='modified')
    sys.exit(asyncio.run(audit(parser.parse_args())))
