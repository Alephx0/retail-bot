"""Public fingerprint consistency audit; isolated data, no retailer traffic.

Run with .venv/Scripts/python scripts/fingerprint_differential.py.
Artifacts contain browser/device fingerprints; keep them local.
"""
import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patchright.async_api import async_playwright
from retail.amazon import Amazon
from retail.store import Store
from retail.native_fingerprint import launch_options as native_launch_options

SURFACES = ('canvas', 'webgl', 'webgpu', 'audio', 'workers')
SITES = {
    'creepjs': 'https://abrahamjuliot.github.io/creepjs/',
    'browserleaks': 'https://browserleaks.com/webgl',
    'amiunique': 'https://www.amiunique.org/fingerprint',
}
PROBE = r"""async () => {
 const c = document.createElement('canvas'); c.width=200; c.height=100;
 const ctx=c.getContext('2d'); const g=ctx.createLinearGradient(0,0,200,100);
 g.addColorStop(0,'white');g.addColorStop(.5,'gray');g.addColorStop(1,'black');
 ctx.fillStyle=g;ctx.fillRect(0,0,200,100);
 const digest=async x=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(x)))).map(x=>x.toString(16).padStart(2,'0')).join('');
 const gl=document.createElement('canvas').getContext('webgl');
 const ext=gl?.getExtension('WEBGL_debug_renderer_info');
 const gpu=gl?{vendor:ext?gl.getParameter(ext.UNMASKED_VENDOR_WEBGL):null,renderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null,version:gl.getParameter(gl.VERSION),shading:gl.getParameter(gl.SHADING_LANGUAGE_VERSION),extensions:gl.getSupportedExtensions(),limits:Object.fromEntries(['MAX_TEXTURE_SIZE','MAX_RENDERBUFFER_SIZE','MAX_VERTEX_ATTRIBS','MAX_COMBINED_TEXTURE_IMAGE_UNITS','MAX_VARYING_VECTORS'].map(k=>[k,gl.getParameter(gl[k])])),attributes:gl.getContextAttributes()}:null;
 let webgpu=null;try {const a=await navigator.gpu?.requestAdapter();if(a)webgpu={info:Object.fromEntries(['vendor','architecture','device','description','subgroupMinSize','subgroupMaxSize'].map(k=>[k,a.info[k]])),features:[...a.features].sort(),limits:Object.fromEntries(Object.keys(Object.getPrototypeOf(a.limits)).map(k=>[k,a.limits[k]]))};}catch(e){webgpu={error:String(e)}}
 const ac=new AudioContext();const audio={sampleRate:ac.sampleRate,baseLatency:ac.baseLatency,outputLatency:ac.outputLatency,maxChannelCount:ac.destination.maxChannelCount};await ac.close();
 const worker=await new Promise(resolve=>{const url=URL.createObjectURL(new Blob([`const gl=new OffscreenCanvas(10,10).getContext('webgl');const e=gl?.getExtension('WEBGL_debug_renderer_info');postMessage({ua:navigator.userAgent,platform:navigator.platform,language:navigator.language,renderer:e?gl.getParameter(e.UNMASKED_RENDERER_WEBGL):null});`],{type:'text/javascript'}));let w;const timer=setTimeout(()=>{w?.terminate();URL.revokeObjectURL(url);resolve({error:'timeout'})},5000);try {w=new Worker(url);w.onmessage=e=>{clearTimeout(timer);w.terminate();URL.revokeObjectURL(url);resolve(e.data)};w.onerror=e=>{clearTimeout(timer);w.terminate();URL.revokeObjectURL(url);resolve({error:e.message})}}catch(e){clearTimeout(timer);URL.revokeObjectURL(url);resolve({error:String(e)})}});
 return {canvas:await digest(c.toDataURL()),gpu,webgpu,audio,worker,ua:navigator.userAgent,platform:navigator.platform,language:navigator.language,languages:navigator.languages,screen:{width:screen.width,height:screen.height,availWidth:screen.availWidth,availHeight:screen.availHeight,dpr:devicePixelRatio},timezone:Intl.DateTimeFormat().resolvedOptions().timeZone};
}"""

def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')

async def capture(context, name, folder):
    page = await context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    result = {'url': SITES[name]}
    try:
        response = await page.goto(SITES[name], wait_until='domcontentloaded', timeout=45000)
        result['status'] = response.status if response else None
        if response:
            headers = await response.request.all_headers()
            result['request_headers'] = {k: v for k, v in headers.items() if k in ('user-agent','accept-language') or k.startswith('sec-ch-ua')}
        if name == 'creepjs':
            for _ in range(90):
                if await page.evaluate('!!window.Fingerprint', isolated_context=False):
                    break
                await page.wait_for_timeout(500)
            else:
                raise TimeoutError('CreepJS did not export window.Fingerprint within 45 seconds')
            result['fingerprint'] = await page.evaluate('JSON.parse(JSON.stringify(window.Fingerprint))', isolated_context=False)
        else:
            await page.wait_for_timeout(8000)
        result['text'] = await page.locator('body').inner_text(timeout=10000)
        if name == 'browserleaks':
            result['tables'] = await page.locator('table').all_inner_texts()
    except Exception as exc:
        result['error'] = str(exc)
        try:
            result['text'] = await page.locator('body').inner_text(timeout=5000)
        except Exception:
            pass
    try:
        await page.screenshot(path=str(folder / f'{name}.png'), full_page=True, timeout=15000)
    except Exception as exc:
        result['screenshot_error'] = str(exc)
    result['page_errors'] = errors
    save(folder / f'{name}.json', result)
    await page.close()
    return {
        'status': result.get('status'), 'error': result.get('error'),
        'page_errors': errors, 'screenshot_error': result.get('screenshot_error'),
        **({'total_lies': result.get('fingerprint', {}).get('lies', {}).get('totalLies')}
           if name == 'creepjs' else {}),
        **({'warning_count': len(result['fingerprint']['trash']['trashBin']),
            'captured_error_count': len(result['fingerprint']['capturedErrors']['data']),
            'worker_renderer': result['fingerprint'].get('workerScope', {}).get('webglRenderer')}
           if name == 'creepjs' and result.get('fingerprint', {}).get('trash')
              and result['fingerprint'].get('capturedErrors') else {}),
    }


def zero_lie_failures(records, expected_runs):
    """Missing results and failed captures must never count as zero lies."""
    failures = []
    if len(records) != expected_runs:
        failures.append(f'Expected {expected_runs} runs, received {len(records)}')
    for row in records:
        creep = row.get('sites', {}).get('creepjs', {})
        count = creep.get('total_lies')
        if (row.get('error') or creep.get('status') != 200 or creep.get('error')
                or creep.get('page_errors') or creep.get('screenshot_error')
                or type(count) is not int or count != 0):
            failures.append({'case': row['case'], 'repeat': row['repeat'],
                             'error': row.get('error'), 'creepjs': creep})
    return failures


def zero_warning_failures(records, expected_runs):
    """Require complete clean captures and agreement in both worker probes."""
    failures = zero_lie_failures(records, expected_runs)
    for row in records:
        creep = row.get('sites', {}).get('creepjs', {})
        probe = row.get('probe', {})
        renderer = (probe.get('gpu') or {}).get('renderer')
        worker = (probe.get('worker') or {}).get('renderer')
        if (type(creep.get('warning_count')) is not int or creep['warning_count'] != 0
                or type(creep.get('captured_error_count')) is not int or creep['captured_error_count'] != 0
                or not renderer or creep.get('worker_renderer') != renderer or worker != renderer
                or row.get('worker_profile_errors')):
            failures.append({'case': row['case'], 'repeat': row['repeat'],
                             'creepjs': creep, 'main_renderer': renderer,
                             'dedicated_renderer': worker,
                             'worker_profile_errors': row.get('worker_profile_errors', [])})
    return failures

async def native_context(driver, folder, headed, backend='javascript', executable=''):
    chrome = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    extra_args = []
    if backend == 'native':
        options = native_launch_options({'native_browser_executable': executable})
        chrome = Path(options['executable_path'])
        extra_args = options['args']
    profile = folder / 'native-profile'
    profile.mkdir()
    args = [str(chrome), f'--user-data-dir={profile.resolve()}', '--remote-debugging-port=0', '--no-first-run', '--no-default-browser-check', *extra_args]
    if not headed:
        args.append('--headless=new')
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = profile / 'DevToolsActivePort'
        for _ in range(100):
            if port_file.exists():
                break
            await asyncio.sleep(.1)
        port = port_file.read_text().splitlines()[0]
        browser = await driver.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
        return browser, browser.contexts[0], process
    except BaseException:
        process.terminate()
        process.wait(timeout=10)
        raise

async def main(args):
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    matrix = [('native-headed', True, ()), ('native-headless', False, ()), ('bot-off', False, ())]
    matrix += [(s+'-only', False, (s,)) for s in SURFACES]
    matrix += [('everything', False, SURFACES), ('restored-off', False, ())]
    if args.all_configurations:
        for size in range(2, len(SURFACES)):
            matrix += [('mixed-' + '-'.join(enabled), False, enabled)
                       for enabled in combinations(SURFACES, size)]
    if args.cases:
        unknown = set(args.cases.split(',')) - {row[0] for row in matrix}
        if unknown:
            raise ValueError(f'Unknown cases: {sorted(unknown)}')
        matrix = [row for row in matrix if row[0] in args.cases.split(',')]
    records = []
    async with async_playwright() as driver:
        with tempfile.TemporaryDirectory(prefix='retail-fingerprint-') as temp:
            store = Store(Path(temp))
            account = store.put('accounts', {'name': 'Differential audit', 'region': 'US'}, args.account_id)
            try:
                for name, headed, enabled in matrix:
                    for repeat in range(1, args.repeats+1):
                        folder = out / name / str(repeat)
                        folder.mkdir(parents=True, exist_ok=True)
                        adapter = Amazon(store)
                        browser = process = None
                        record = {'case': name, 'repeat': repeat, 'enabled': enabled, 'at': datetime.now(timezone.utc).isoformat()}
                        print(f'START {name} {repeat}', flush=True)
                        try:
                            store.put('settings', {'browser_channel': 'chrome', 'show_browser_window': headed,
                                'fingerprint_backend': args.backend, 'native_browser_executable': args.native_browser_executable,
                                **{'fingerprint_'+s:s in enabled for s in SURFACES}}, 'settings')
                            record['backend'] = args.backend
                            if name.startswith('native-'):
                                browser, context, process = await native_context(driver, folder, headed, args.backend, args.native_browser_executable)
                            else:
                                context = await adapter.context(account)
                                browser = adapter.browser
                            record['browser_version'] = browser.version
                            record['profile'] = adapter.profiles.get(account)
                            # Intercept only a dedicated synthetic HTTPS origin for a CSP-free probe.
                            await context.route('https://fingerprint-audit.test/', lambda route: route.fulfill(content_type='text/html', body='<title>Fingerprint audit</title>'))
                            probe = await context.new_page()
                            await probe.goto('https://fingerprint-audit.test/')
                            record['probe'] = await probe.evaluate(PROBE, isolated_context=False)
                            await probe.close()
                            record['sites'] = {}
                            for site in args.sites:
                                record['sites'][site] = await capture(context, site, folder)
                                print(f'  {site}: {record["sites"][site]}', flush=True)
                            bridge = getattr(context, '_retail_worker_profiles', None)
                            if bridge:
                                record['worker_profile_errors'] = list(bridge.errors)
                                record['worker_profile_initializations'] = dict(bridge.initialized)
                            await context.close()
                        except Exception as exc:
                            record['error'] = str(exc)
                            print(f'ERROR {exc}', flush=True)
                        finally:
                            await adapter.close()
                            if name.startswith('native-') and browser:
                                await browser.close()
                            if process and process.poll() is None:
                                process.terminate()
                                process.wait(timeout=10)
                        save(folder / 'result.json', record)
                        records.append(record)
                        save(out / 'results.json', records)
                        print(f'DONE {name} {repeat}', flush=True)
            finally:
                store.db.close()
    if args.require_zero_lies or args.require_zero_warnings:
        check = zero_warning_failures if args.require_zero_warnings else zero_lie_failures
        failures = check(records, len(matrix) * args.repeats)
        save(out / 'zero-lies-check.json', {'passed': not failures, 'failures': failures})
        label = 'Zero-warning and worker-consistency' if args.require_zero_warnings else 'Zero-lie'
        if args.require_zero_warnings:
            save(out / 'zero-warnings-check.json', {'passed': not failures, 'failures': failures})
        print(f'{label} check: {"FAIL" if failures else "PASS"}', flush=True)
        return 1 if failures else 0
    return 0

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='artifacts/fingerprint-differential')
    parser.add_argument('--repeats', type=int, default=3, choices=range(1, 6))
    parser.add_argument('--cases', default='')
    parser.add_argument('--sites', nargs='+', choices=tuple(SITES), default=list(SITES))
    parser.add_argument('--require-zero-lies', action='store_true')
    parser.add_argument('--require-zero-warnings', action='store_true', help='Also require no warning-bin entries/errors and matching worker identities')
    parser.add_argument('--backend', choices=('javascript', 'native'), default='javascript')
    parser.add_argument('--native-browser-executable', default='')
    parser.add_argument('--all-configurations', action='store_true', help='Test all 32 combinations of the five fingerprint flags, plus baselines')
    parser.add_argument('--account-id', default='differential-fixed-account-v1', help='Synthetic account identifier used to derive the repeatable seed')
    args = parser.parse_args()
    if (args.require_zero_lies or args.require_zero_warnings) and 'creepjs' not in args.sites:
        parser.error('Strict fingerprint checks require creepjs in --sites')
    sys.exit(asyncio.run(main(args)))
