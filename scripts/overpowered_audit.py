"""Controlled, isolated OverpoweredJS measurements; never modifies site results."""
import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patchright.async_api import async_playwright
from retail.amazon import Amazon
import retail.amazon as amazon_module
from retail.models import Account
from retail.store import Store
from retail.worker_profiles import debugging_port
from fingerprint_differential import native_context, capture, PROBE

FLAGS = ('canvas', 'webgl', 'webgpu', 'audio', 'workers', 'fonts', 'navigator', 'screen')
CASES = {
    'chrome-direct': ('direct', (), {}),
    'native-direct': ('direct-native', (), {}),
    'chrome-direct-fixed': ('direct-fixed', (), {}),
    'native-direct-fixed': ('direct-native-fixed', (), {}),
    'js-disabled': ('javascript', (), {}),
    'js-webgl': ('javascript', ('webgl',), {}),
    'js-webgl-no-noise': ('javascript', ('webgl',), {'webgl_noise': 'off'}),
    'js-webgl-native-gpu': ('javascript', ('webgl',), {'webgl_noise': 'off', 'gpu': 'native'}),
    'js-webgpu': ('javascript', ('webgpu',), {}),
    'js-webgpu-native': ('javascript', ('webgpu',), {'webgpu_limits': 'native', 'gpu': 'native'}),
    'js-everything': ('javascript', FLAGS, {}),
    'js-preserve-gpu': ('javascript', FLAGS, {'webgl_noise': 'off', 'gpu': 'native', 'webgpu_limits': 'native'}),
    'js-native-gpu-noise': ('javascript', FLAGS, {'gpu': 'native', 'webgpu_limits': 'native'}),
    'js-native-gpu-limits': ('javascript', FLAGS, {'gpu': 'native', 'webgl_noise': 'off'}),
    **{f'js-{flag}': ('javascript', (flag,), {}) for flag in ('audio', 'fonts', 'navigator', 'screen')},
    **{f'native-{flag}': ('native', (flag,), {}) for flag in ('canvas', 'webgl', 'webgpu', 'navigator', 'screen', 'fonts', 'audio')},
    'native-disabled': ('native', (), {}),
    'native-everything': ('native', FLAGS, {}),
    'native-preserve-gpu': ('native', FLAGS, {'webgl_noise': 'off', 'gpu': 'native'}),
    'native-native-gpu-noise': ('native', FLAGS, {'gpu': 'native'}),
    'js-varied-gpu': ('javascript', FLAGS, {'gpu': 'family-2'}),
    'native-varied-gpu': ('native', FLAGS, {'gpu': 'family-2'}),
    'native-varied-gpu-subtle': ('native', FLAGS, {'gpu': 'family-2', 'webgl_noise': 'subtle'}),
    'js-varied-gpu-cpu8': ('javascript', FLAGS, {'gpu': 'family-2', 'cpu': '8', 'memory': '8'}),
    'js-varied-gpu-screen1080': ('javascript', FLAGS, {'gpu': 'family-2', 'screen': '1920x1080@1'}),
    'js-varied-desktop': ('javascript', FLAGS, {'gpu': 'family-2', 'cpu': '8', 'memory': '8', 'screen': '1920x1080@1'}),
    'native-varied-desktop': ('native', FLAGS, {'gpu': 'family-2', 'cpu': '8', 'memory': '8', 'screen': '1920x1080@1'}),
}


async def measure(context, folder, docs=False, fit_screen=False):
    page = await context.new_page()
    if fit_screen:
        screen = await page.evaluate('({width:screen.width,height:screen.height,dpr:devicePixelRatio})')
        viewport = {'width': screen['width']-48, 'height': screen['height']-160}
        await page.set_viewport_size(viewport)
        metrics = await context.new_cdp_session(page)
        await metrics.send('Emulation.setDeviceMetricsOverride', {**viewport, 'mobile': False,
            'deviceScaleFactor': screen['dpr'], 'screenWidth': screen['width'], 'screenHeight': screen['height']})
    requests, responses, tasks = [], [], []
    submitted = []
    def request_sent(request):
        if urlsplit(request.url).path == '/fp/collect' and request.method == 'POST':
            try:
                payload = request.post_data_json
                data = payload.get('d') if isinstance(payload, dict) else None
                submitted.append({'incognito_detected': data.get('inc', False) if isinstance(data, dict) else None,
                    'payload_keys': sorted(payload) if isinstance(payload, dict) else [],
                    'data_keys': sorted(data) if isinstance(data, dict) else []})
                (folder/f'submitted-{len(submitted)}.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
            except Exception:
                pass
    page.on('request', request_sent)
    async def response_received(response):
        url = urlsplit(response.url)
        requests.append({'path': url.path, 'host': url.hostname, 'status': response.status})
        if 'application/json' in response.headers.get('content-type', '') and url.hostname == 'overpoweredjs.bot':
            try:
                value = await response.json()
                responses.append({'path': url.path, 'body': value})
            except Exception:
                pass
    page.on('response', lambda response: tasks.append(asyncio.create_task(response_received(response))))
    await page.goto('https://overpoweredjs.bot/', wait_until='domcontentloaded', timeout=45000)
    body = ''
    for _ in range(30):
        await page.wait_for_timeout(1000)
        body = await page.locator('body').inner_text()
        score = re.search(r'BOT SCORE\s+(\w+)\s+(\d+\.\d+)', body)
        if score:
            break
    result = {'score': float(score[2]) if score else None, 'band': score[1] if score else None}
    result['submitted'] = submitted
    result['checks'] = dict(re.findall(r'(Automated browser|Anti-detect browser|Fake user agent|Developer tools attached)\s+(FLAGGED|CLEAR)', body))
    await page.screenshot(path=str(folder/'bot.png'))
    if tasks:
        await asyncio.gather(*tasks)
    result['decisions'] = [item['body'] for item in responses if isinstance(item['body'], dict) and ('components' in item['body'] or 'outcome' in item['body'])]
    (folder/'network.json').write_text(json.dumps({'requests': requests, 'responses': responses}, indent=2), encoding='utf-8')
    (folder/'bot.txt').write_text(body, encoding='utf-8')
    result['probe'] = await page.evaluate(PROBE, isolated_context=False)
    result['browser_identity'] = await page.evaluate('({ua:navigator.userAgent,brands:navigator.userAgentData?.toJSON(),cores:navigator.hardwareConcurrency,memory:navigator.deviceMemory,webdriver:navigator.webdriver,geometry:{outerWidth,outerHeight,innerWidth,innerHeight,screenWidth:screen.width,screenHeight:screen.height,availableHeight:screen.availHeight,dpr:devicePixelRatio}})', isolated_context=False)
    if docs:
        await page.goto('https://overpoweredjs.bot/docs/scores', wait_until='domcontentloaded')
        await page.wait_for_timeout(1500)
        (folder/'score-docs.txt').write_text(await page.locator('body').inner_text(), encoding='utf-8')
    await page.close()
    return result


async def main(args):
    if args.native_noise_multipliers:
        original_launch = amazon_module.native_launch_options
        def audit_launch(*positional, **keywords):
            options = original_launch(*positional, **keywords)
            options['args'].append('--webgl-shader-noise-k='+args.native_noise_multipliers)
            return options
        amazon_module.native_launch_options = audit_launch
    output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
    results_path = output/'results.json'
    results = json.loads(results_path.read_text(encoding='utf-8')) if results_path.exists() else []
    for case in args.cases.split(','):
        backend, flags, values = CASES[case]
        for repeat in range(args.repeats):
            folder = output/f'{case}-{repeat+1}'; folder.mkdir(exist_ok=True)
            row = {'case': case, 'repeat': repeat+1, 'backend': backend, 'flags': flags, 'values': values}
            if args.native_noise_multipliers:
                row['native_noise_multipliers'] = args.native_noise_multipliers
            print('START', case, repeat+1, flush=True)
            with tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp)); adapter = None; process = None
                try:
                    if backend.startswith('direct'):
                        async with async_playwright() as driver:
                            browser, context, process = await native_context(driver, Path(tmp), True, 'native' if 'native' in backend else 'javascript', debugging_port=debugging_port() if 'fixed' in backend else 0)
                            row.update(await measure(context, folder, docs=case == 'chrome-direct'))
                            if args.creepjs:
                                row['creepjs'] = await capture(context, 'creepjs', folder)
                            await browser.close()
                    else:
                        settings = {'browser_channel': args.identity, 'show_browser_window': True,
                            'browser_incognito': args.incognito, 'fingerprint_backend': backend,
                            'fingerprint_proxy_location': False,
                            **{'fingerprint_'+flag: flag in flags for flag in FLAGS}}
                        row['settings'] = settings
                        store.put('settings', settings, 'settings')
                        account = store.put('accounts', Account(name='Isolated measurement', fingerprint_values=values).model_dump(), args.profile_key)
                        adapter = Amazon(store)
                        context = await adapter.context(account)
                        row.update(await measure(context, folder, fit_screen=args.fit_screen))
                        if args.visits > 1:
                            row['visits'] = [{key: row.get(key) for key in ('score', 'band', 'checks', 'submitted')}]
                            for visit in range(2, args.visits+1):
                                visit_folder = folder/f'visit-{visit}'; visit_folder.mkdir(exist_ok=True)
                                measured = await measure(context, visit_folder, fit_screen=args.fit_screen)
                                row['visits'].append({key: measured.get(key) for key in ('score', 'band', 'checks', 'submitted')})
                        if args.creepjs:
                            row['creepjs'] = await capture(context, 'creepjs', folder)
                        await context.close()
                except Exception as exc:
                    row['error'] = str(exc)
                finally:
                    if adapter:
                        await adapter.close()
                    if process and process.poll() is None:
                        process.terminate(); process.wait(timeout=10)
                    store.db.close()
            (folder/'result.json').write_text(json.dumps(row, indent=2), encoding='utf-8')
            results = [old for old in results if (old['case'], old['repeat']) != (case, repeat+1)]
            results.append(row); results_path.write_text(json.dumps(results, indent=2), encoding='utf-8')
            print(json.dumps({key: row.get(key) for key in ('case','score','band','checks','error')}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--cases', default='chrome-direct,js-disabled,js-webgl,js-webgl-no-noise,js-webgl-native-gpu,js-webgpu,js-webgpu-native,js-preserve-gpu,native-direct,native-navigator,native-webgl,native-webgpu')
    parser.add_argument('--repeats', type=int, choices=range(1, 4), default=1)
    parser.add_argument('--incognito', action='store_true')
    parser.add_argument('--identity', choices=['chrome', 'chromium', 'msedge'], default='chrome')
    parser.add_argument('--profile-key', default='public-audit-fixed-v1', help='Deterministic profile key; no account credentials or session are loaded')
    parser.add_argument('--fit-screen', action='store_true')
    parser.add_argument('--creepjs', action='store_true')
    parser.add_argument('--visits', type=int, choices=range(1,5), default=1)
    parser.add_argument('--native-noise-multipliers', help='Experimental native shader coefficients; recorded in the result')
    args = parser.parse_args()
    unknown = set(args.cases.split(',')) - CASES.keys()
    if unknown:
        parser.error('Unknown cases: '+', '.join(sorted(unknown)))
    asyncio.run(main(args))
