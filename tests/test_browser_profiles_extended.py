import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from patchright.async_api import async_playwright, expect

from retail.amazon import Amazon
from retail.app import create_app
from retail.browser_runtime import browser_options
from retail.fingerprint_profiles import generated, gpu_choices
from retail.fingerprint import build_scripts
from retail.models import Account, Settings
from retail.native_fingerprint import launch_options, DEFAULT_DIRECTORY
from retail.store import Store

HEADERS = {'X-Retail-Client': 'dashboard'}


def test_presets_are_repeatable_and_reject_impossible_hardware():
    hardware = {'cpu': 8, 'memory': 8, 'renderer': 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x1234) Direct3D11)',
                'fonts': ['Arial', 'Calibri', 'Consolas', 'Unusual Font']}
    profile = {'seed': 'abcd'*16}
    values = {'cpu': '4', 'memory': '8', 'gpu': 'family-1', 'screen': '1920x1080@1', 'fonts': 'core'}
    result = generated(profile, values, hardware)
    assert result == generated(profile, values, hardware)
    assert result['cpu'] == 4 and result['memory'] == 8
    assert result['fonts'] == ['Arial']
    assert result['viewport']['height'] < result['screen']['height']
    with pytest.raises(ValueError, match='actual browser hardware'):
        generated(profile, {**values, 'cpu': '16'}, hardware)
    with pytest.raises(ValueError, match='compatible'):
        generated(profile, values, {**hardware, 'renderer': 'Unknown GPU'})
    assert gpu_choices('Unknown GPU') == [('native', 'Unknown GPU')]
    aliases = dict(gpu_choices(hardware['renderer']))
    assert aliases['family-1'] == hardware['renderer']
    assert '(0x00002486)' in aliases['family-0']
    assert generated(profile, {}, hardware)['gpu'] != hardware['renderer']
    small = {**hardware, 'cpu': 1, 'memory': 4}
    assert generated(profile, {}, small)['memory'] == 4
    with pytest.raises(ValueError, match='actual browser hardware'):
        generated(profile, {'cpu': '2'}, small)


def test_native_navigator_and_timezone_controls_and_real_brand_constraints(tmp_path):
    binary = tmp_path/'chrome.exe'; binary.touch()
    settings = {'fingerprint_backend': 'native', 'native_browser_executable': str(binary),
                'fingerprint_navigator': True, 'fingerprint_timezone': 'America/Chicago'}
    options = launch_options(settings, 42, {'cpu': 4, 'memory': 8})
    assert '--fingerprint-hardware-concurrency=4' in options['args']
    assert '--fingerprint-device-memory=8' in options['args']
    assert '--fingerprint-timezone=America/Chicago' in options['args']
    for identity in ('msedge', 'brave', 'opera'):
        with pytest.raises(ValueError, match='JavaScript'):
            Settings(fingerprint_backend='native', browser_identity=identity)
    assert browser_options({'browser_identity': 'msedge'}) == {'channel': 'msedge'}
    with pytest.raises(ValueError, match='not installed'):
        browser_options({'browser_identity': 'brave', 'brave_executable': str(tmp_path/'missing.exe')})


def test_extension_registry_random_assignment_and_removal(tmp_path):
    extension = tmp_path/'fixture'; extension.mkdir()
    (extension/'manifest.json').write_text(json.dumps({'manifest_version': 3, 'name': 'Fixture', 'version': '1.0'}))
    with TestClient(create_app(tmp_path/'data'), headers=HEADERS) as client:
        added = client.post('/api/browser_extensions', json={'name': 'Fixture', 'path': str(extension)}).json()
        assert 'id' in added
        account = client.post('/api/accounts', json={'name': 'Random'}).json()
        assert account['fingerprint_overrides']['browser_extension_ids'] == [added['id']]
        explicit = client.post('/api/accounts', json={'name': 'None', 'fingerprint_overrides': {'browser_extension_ids': []}}).json()
        assert explicit['fingerprint_overrides']['browser_extension_ids'] == []
        assert client.post('/api/settings', json={'browser_extension_ids': [added['id']]}).status_code == 200
        assert client.delete('/api/browser_extensions/'+added['id']).status_code == 200
        state = client.get('/api/state').json()
        assert not state['settings'][0]['browser_extension_ids']
        assert all(not account['fingerprint_overrides']['browser_extension_ids'] for account in state['accounts'])
        assert client.post('/api/browser_extensions', json={'name': 'Bad', 'path': str(tmp_path/'missing')}).status_code == 422


@pytest.mark.parametrize('incognito', [True, False])
def test_runtime_extension_is_native_and_sessions_close_automatically(tmp_path, incognito):
    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'show_browser_window': False, 'browser_incognito': incognito}, 'settings')
        account = store.put('accounts', Account(name='Runtime').model_dump())
        adapter = Amazon(store)
        try:
            context = await adapter.context(account)
            await context.route('https://runtime.test/', lambda route: route.fulfill(body='<h1>Fixture</h1>', content_type='text/html'))
            page = await context.new_page(); await page.goto('https://runtime.test/')
            result = await page.evaluate('({runtime:!!chrome.runtime, source:String(chrome.runtime.sendMessage), id:chrome.runtime.id})', isolated_context=False)
            assert result['runtime'] and '[native code]' in result['source']
            assert not result.get('id')  # A website is not falsely made an extension context.
            adapter.logins[account['id']] = context
            await context.close()
            assert account['id'] not in adapter.logins
            if adapter.profile_close_tasks:
                await asyncio.gather(*adapter.profile_close_tasks)
        finally:
            await adapter.close(); store.db.close()
    asyncio.run(scenario())


def test_new_account_generated_tab_and_native_toggles(tmp_path):
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        async def scenario():
            async with async_playwright() as driver:
                browser = await driver.chromium.launch(headless=True)
                page = await browser.new_page(); errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                async def route(request_route):
                    from urllib.parse import urlsplit
                    request = request_route.request; url = urlsplit(request.url)
                    response = client.request(request.method, url.path, content=request.post_data, headers=HEADERS)
                    await request_route.fulfill(status=response.status_code, body=response.content, headers={'content-type': response.headers.get('content-type', 'text/plain')})
                await page.route('**/*', route)
                await page.goto('http://127.0.0.1/'); await page.locator('[data-view=accounts]').click()
                await page.locator('#primary').click()
                await page.locator('[name=email]').fill('profile@example.com')
                await page.locator('.account-fingerprint-fields summary').click()
                await page.locator('[name=account_fingerprint_backend]').select_option('native')
                for surface in ['fonts','navigator','screen','proxy_location','timezone']:
                    await expect(page.locator('[name=account_fingerprint_'+surface+']')).to_be_enabled()
                # Preview standard Chromium in CI; native availability is tested separately.
                await page.locator('[name=account_fingerprint_backend]').select_option('javascript')
                await page.locator('[data-fingerprint-tab=generated]').click()
                await expect(page.locator('[data-preview-status]')).to_contain_text('Compatible presets generated', timeout=30000)
                await page.locator('[name=generated_screen]').select_option('1920x1080@1')
                await page.locator('[name=generated_fonts]').select_option('core')
                await page.locator('[data-preserve-gpu]').click()
                await expect(page.locator('[data-preview-status]')).to_contain_text('Compatible presets generated', timeout=30000)
                screenshots = Path('artifacts/fingerprint-expansion/ui'); screenshots.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshots/'generated-profile.png'))
                await page.get_by_role('button', name='Create Account', exact=True).click()
                await expect(page.locator('#modal')).not_to_be_visible()
                account = client.get('/api/state').json()['accounts'][0]
                assert account['fingerprint_values']['screen'] == '1920x1080@1'
                assert account['fingerprint_values']['fonts'] == 'core'
                assert account['fingerprint_values']['gpu'] == 'native'
                assert account['fingerprint_values']['webgl_noise'] == 'off'
                assert account['fingerprint_values']['webgpu_limits'] == 'native'
                assert len(account['fingerprint_seed']) == 32
                await expect(page.locator('[data-fingerprint-close]')).to_have_count(0)
                await expect(page.locator('[data-account-more]')).to_have_count(1)
                assert not errors, errors
                await browser.close()
        asyncio.run(scenario())


@pytest.mark.skipif(not (DEFAULT_DIRECTORY/'chrome.exe').is_file(), reason='Optional native Chromium is not installed')
def test_native_surface_controls_apply_to_real_browser(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'show_browser_window': False, 'fingerprint_backend': 'native',
            'fingerprint_navigator': True, 'fingerprint_fonts': True, 'fingerprint_screen': True,
            'fingerprint_timezone': 'America/Chicago', 'fingerprint_proxy_location': False}, 'settings')
        account = store.put('accounts', Account(name='Native controls', fingerprint_values={
            'cpu': '2', 'memory': '8', 'screen': '1920x1080@1', 'fonts': 'core', 'gpu': 'native'}).model_dump())
        adapter = Amazon(store)
        try:
            context = await adapter.context(account)
            await context.route('https://native.test/', lambda route: route.fulfill(body='<h1>Native</h1>', content_type='text/html'))
            await context.grant_permissions(['local-fonts'], origin='https://native.test')
            page = await context.new_page(); await page.goto('https://native.test/')
            actual = await page.evaluate('''async () => ({cpu:navigator.hardwareConcurrency,
                memory:navigator.deviceMemory, timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
                screen:[screen.width,screen.height,devicePixelRatio],
                fonts:[...new Set((await queryLocalFonts()).map(font=>font.family))],
                worker:await new Promise(resolve=>{const worker=new Worker(URL.createObjectURL(new Blob([
                  'postMessage({cpu:navigator.hardwareConcurrency,memory:navigator.deviceMemory})'
                ],{type:'application/javascript'})));worker.onmessage=event=>{worker.terminate();resolve(event.data)}})
            })''', isolated_context=False)
            assert actual['cpu'] == actual['worker']['cpu'] == 2
            assert actual['memory'] == actual['worker']['memory'] == 8
            assert actual['timezone'] == 'America/Chicago'
            assert actual['screen'] == [1920, 1080, 1]
            assert 'Arial' in actual['fonts']
            assert set(actual['fonts']) <= {'Arial', 'Times New Roman', 'Courier New', 'Segoe UI', 'Segoe UI Emoji'}
            await context.close()
        finally:
            await adapter.close(); store.db.close()
    asyncio.run(scenario())


def test_normal_profile_preserves_newer_storage_after_initial_session_import(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'show_browser_window': False, 'browser_incognito': False}, 'settings')
        cookie = {'name': 'session', 'value': 'imported', 'domain': 'storage.test', 'path': '/',
                  'expires': 2147483647, 'httpOnly': False, 'secure': True, 'sameSite': 'Lax'}
        account = store.put('accounts', {**Account(name='Persistent').model_dump(),
            'session': {'cookies': [cookie], 'origins': []}})
        adapter = Amazon(store)
        try:
            for index in range(2):
                context = await adapter.context(account)
                await context.route('https://storage.test/', lambda route: route.fulfill(body='<h1>Storage</h1>', content_type='text/html'))
                page = await context.new_page(); await page.goto('https://storage.test/')
                cookies = {item['name']: item['value'] for item in await context.cookies()}
                assert cookies['session'] == ('newer' if index else 'imported')
                if not index:
                    await context.add_cookies([{**cookie, 'value': 'newer'}])
                    await page.evaluate("localStorage.setItem('new-data','preserved')")
                else:
                    assert await page.evaluate("localStorage.getItem('new-data')") == 'preserved'
                await context.close()
                if adapter.profile_close_tasks:
                    await asyncio.gather(*adapter.profile_close_tasks)
        finally:
            await adapter.close(); store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('incognito', [True, False])
def test_headed_browser_restores_origins_after_loading_extensions(tmp_path, incognito):
    async def scenario():
        store = Store(tmp_path)
        extension = tmp_path / 'extension'; extension.mkdir()
        (extension / 'manifest.json').write_text(json.dumps({'manifest_version': 3, 'name': 'Startup fixture', 'version': '1.0'}))
        store.put('browser_extensions', {'path': str(extension)}, 'fixture-extension')
        store.put('settings', {'show_browser_window': True, 'browser_incognito': incognito,
                              'browser_extension_ids': ['fixture-extension']}, 'settings')
        cookie = {'name': 'fixture-session', 'value': 'saved', 'domain': 'startup.test', 'path': '/',
                  'expires': 2147483647, 'httpOnly': False, 'secure': True, 'sameSite': 'Lax'}
        account = store.put('accounts', {**Account(name='Startup').model_dump(), 'session': {
            'cookies': [cookie], 'origins': [{'origin': 'https://startup.test',
                                            'localStorage': [{'name': 'session-marker', 'value': 'preserved'}]}]}})
        adapter = Amazon(store)
        try:
            async with asyncio.timeout(15):
                context = await adapter.context(account)
                await context.route('**/*', lambda route: route.fulfill(body='<h1>Local fixture</h1>', content_type='text/html'))
                page = await context.new_page()
                await page.goto('https://startup.test/')
                assert await page.evaluate("localStorage.getItem('session-marker')") == 'preserved'
                assert any(c['name'] == 'fixture-session' and c['value'] == 'saved' for c in await context.cookies())
                assert await page.evaluate('!!chrome.runtime', isolated_context=False)
                await context.close()
        finally:
            await adapter.close()
            store.db.close()
    asyncio.run(scenario())


def test_preserved_gpu_keeps_real_api_functions_and_native_launch_flags(tmp_path):
    values = {'gpu': 'Actual renderer', 'gpu_choice': 'native', 'webgl_noise': 0,
              'webgpu_limits': 'native'}
    binary = tmp_path/'chrome.exe'; binary.touch()
    settings = {'native_browser_executable': str(binary), 'fingerprint_webgl': True,
                'fingerprint_webgpu': True, 'fingerprint_canvas': True}
    options = launch_options(settings, 42, values)
    assert not any(arg.startswith(('--fingerprint-gpu-', '--fingerprint-webgpu-', '--webgl-shader-noise=')) for arg in options['args'])
    assert any(arg.startswith('--canvas-gradient-noise=') for arg in options['args'])

    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            page = await browser.new_page()
            result = await page.evaluate("""script => {
                const gl = WebGLRenderingContext.prototype;
                const gl2 = WebGL2RenderingContext.prototype;
                const info = globalThis.GPUAdapterInfo?.prototype;
                const getter = () => info && Object.getOwnPropertyDescriptor(info, 'description').get;
                const before = [gl.getParameter, gl.readPixels, gl2.getParameter, gl2.readPixels, getter()];
                const canvasBefore = CanvasRenderingContext2D.prototype.createLinearGradient;
                (0, eval)(script);
                return {preserved:before.every((fn,i)=>fn === [gl.getParameter, gl.readPixels, gl2.getParameter, gl2.readPixels, getter()][i]),
                    canvasVaries:canvasBefore !== CanvasRenderingContext2D.prototype.createLinearGradient};
            }""", build_scripts(123, spoof_webgl=True, spoof_webgpu=True,
                                 perturb_canvas=True, profile_values=values)[0], isolated_context=False)
            assert result == {'preserved': True, 'canvasVaries': True}
            await browser.close()
    asyncio.run(scenario())
