import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from retail.amazon import Amazon
from retail.app import create_app
from retail.models import Settings
from retail.native_fingerprint import DEFAULT_DIRECTORY, launch_options
from retail.store import Store


def test_native_backend_settings_require_idle_and_reject_external_attachment(tmp_path):
    with TestClient(create_app(tmp_path), headers={'X-Retail-Client': 'dashboard'}) as client:
        engine = client.app.state.engine
        engine.jobs['fixture'] = None
        try:
            response = client.post('/api/settings', json={'fingerprint_backend': 'native'})
            assert response.status_code == 409
        finally:
            engine.jobs.pop('fixture')
        response = client.post('/api/settings', json={'fingerprint_backend': 'native', 'cdp_attach': True})
        assert response.status_code == 422
        response = client.post('/api/settings', json={'fingerprint_backend': 'native', 'fingerprint_canvas': True})
        assert response.status_code == 200, response.text
        assert response.json()['fingerprint_backend'] == 'native'


def test_native_launch_requires_binary_and_owns_gpu_identity(tmp_path):
    with pytest.raises(ValueError, match='missing'):
        launch_options({'native_browser_executable': str(tmp_path / 'missing.exe')}, 1)
    executable = tmp_path / 'chrome.exe'
    executable.touch()
    settings = {'native_browser_executable': str(executable), 'fingerprint_webgpu': True}
    result = launch_options(settings, 0xffffffff)
    assert result['headless']
    assert '--fingerprint-gpu-renderer=seed:-1' in result['args']
    assert '--fingerprint-webgpu-vendor=seed:-1' in result['args']
    assert not any('canvas' in arg or 'audio-context' in arg for arg in result['args'])
    with pytest.raises(ValueError, match='app-managed'):
        launch_options({**settings, 'cdp_attach': True}, 1)
    with pytest.raises(ValueError, match='app-managed'):
        Settings(fingerprint_backend='native', cdp_attach=True)


@pytest.mark.skipif(not (DEFAULT_DIRECTORY / 'chrome.exe').exists(), reason='Optional native browser is not installed')
def test_native_profiles_are_isolated_repeatable_and_preserve_exact_pixels(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'fingerprint_backend': 'native', 'fingerprint_canvas': True,
                              'fingerprint_webgl': True, 'fingerprint_webgpu': True,
                              'fingerprint_audio': True}, 'settings')
        adapter = Amazon(store)
        contexts = []
        try:
            accounts = [store.put('accounts', {'name': 'Test', 'region': 'US'}, name)
                        for name in ['native-test-a', 'native-test-b']]
            async def probe(account):
                context = await adapter.context(account)
                contexts.append(context)
                await context.route('https://native.test/', lambda r:r.fulfill(content_type='text/html', body='<body>Test</body>'))
                page = await context.new_page()
                await page.goto('https://native.test/')
                return context, await page.evaluate(r'''async () => {
                    const c=document.createElement('canvas');c.width=64;c.height=32;
                    const ctx=c.getContext('2d');
                    let seed=123456;
                    const byte=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed>>>24;};
                    const expected=[];
                    for(let y=0;y<32;y++) for(let x=0;x<64;x++) {
                        const rgb=[byte(),byte(),byte()];expected.push(...rgb,255);
                        ctx.fillStyle=`rgb(${rgb.join(',')})`;ctx.fillRect(x,y,1,1);
                    }
                    const exact=ctx.getImageData(0,0,64,32).data.every((v,i)=>v===expected[i]);
                    const g=ctx.createLinearGradient(0,0,64,32);
                    g.addColorStop(0,'white');g.addColorStop(.5,'gray');g.addColorStop(1,'black');
                    ctx.fillStyle=g;ctx.fillRect(0,0,64,32);
                    const before=Array.from(ctx.getImageData(0,0,64,32).data);
                    const canvas=c.toDataURL();
                    const image=await createImageBitmap(c);
                    const copy=new OffscreenCanvas(64,32).getContext('2d');copy.drawImage(image,0,0);image.close();
                    const copiesAgree=copy.getImageData(0,0,64,32).data.every((v,i)=>v===before[i]);
                    const gl=document.createElement('canvas').getContext('webgl');
                    const ext=gl.getExtension('WEBGL_debug_renderer_info');
                    const renderer=gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
                    const gpu=await navigator.gpu.requestAdapter();
                    const getter=Object.getOwnPropertyDescriptor(GPUAdapterInfo.prototype,'vendor').get;
                    const webgpu=getter.call(gpu.info);
                    const errors=[];
                    for(const fn of [CanvasRenderingContext2D.prototype.getImageData,
                        HTMLCanvasElement.prototype.toDataURL,WebGLRenderingContext.prototype.getParameter,GPU.prototype.requestAdapter]) {
                        try {Object.create(fn).toString();} catch(e) {
                            if(!e.stack.split('\n')[1].includes('Function.toString')) errors.push(e.stack);
                        }
                    }
                    const audio=new AudioContext({sampleRate:44100});
                    const audioRate=audio.sampleRate;await audio.close();
                    return {exact,copiesAgree,canvas,renderer,webgpu,errors,audioRate};
                }''', isolated_context=False)
            a, first = await probe(accounts[0])
            b, other = await probe(accounts[1])
            assert a.browser is not b.browser
            assert a.browser is not adapter.browser
            await a.close()
            c, repeat = await probe(accounts[0])
            assert first == repeat
            assert first['canvas'] != other['canvas']
            for result in [first, other, repeat]:
                assert result['exact'] and result['copiesAgree'], result
                assert not result['errors'], result
                assert result['audioRate'] == 44100
                assert result['webgpu'].lower() in result['renderer'].lower(), result
        finally:
            await adapter.close()
            assert not adapter.profile_browsers
            assert not adapter.profile_close_tasks
            assert all(not context.browser.is_connected() for context in contexts)
            store.db.close()
    asyncio.run(scenario())
