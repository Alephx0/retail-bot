import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from retail.amazon import Amazon
from retail.app import create_app
from retail.store import Store
from fastapi.testclient import TestClient


@pytest.mark.parametrize('flag', ['canvas', 'webgl', 'webgpu'])
def test_graphics_profiles_require_owned_browser(tmp_path, flag):
    with TestClient(create_app(tmp_path), headers={'X-Retail-Client': 'dashboard'}) as client:
        before = client.get('/api/state').json()['settings']
        response = client.post('/api/settings', json={
            'fingerprint_backend': 'javascript', 'cdp_attach': True,
            'fingerprint_' + flag: True,
        })
        assert response.status_code == 422, response.text
        assert 'app-managed browser' in response.text
        assert client.get('/api/state').json()['settings'] == before


@pytest.fixture
def worker_origin():
    capture = '''const capture=()=>{
        const c=new OffscreenCanvas(32,16),ctx=c.getContext('2d');
        const gradient=ctx.createLinearGradient(0,0,32,16);
        gradient.addColorStop(0,'white');gradient.addColorStop(1,'black');
        ctx.fillStyle=gradient;ctx.fillRect(0,0,32,16);
        const g=new OffscreenCanvas(1,1).getContext('webgl');
        const renderer=g.getParameter(g.getExtension('WEBGL_debug_renderer_info').UNMASKED_RENDERER_WEBGL);
        return {renderer,pixels:[...ctx.getImageData(0,0,32,16).data],
            identity:{ua:navigator.userAgent,hints:navigator.userAgentData?.toJSON(),
                cpu:navigator.hardwareConcurrency,memory:navigator.deviceMemory,
                language:navigator.language,languages:[...navigator.languages],
                timezone:Intl.DateTimeFormat().resolvedOptions().timeZone}};
    };'''

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/dependency.js':
                body = capture + 'export const first=capture();'
            elif path.endswith('.js'):
                body = ('import {first} from "./dependency.js";' if 'module' in path
                        else capture + 'const first=capture();')
                body += 'const result={...first,url:self.location.href};'
                if 'service' in path:
                    body += '''self.addEventListener('install',()=>self.skipWaiting());
                        self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
                        self.addEventListener('message',e=>e.source.postMessage(result));
                        self.addEventListener('fetch',e=>{
                            if(new URL(e.request.url).pathname==='/intercepted')
                                e.respondWith(Promise.resolve(new Response(JSON.stringify(result))));
                        });'''
                elif 'shared' in path:
                    body += 'onconnect=e=>e.ports[0].postMessage(result);'
                elif 'nested' in path:
                    body += "const w=new Worker('/dedicated.js');w.onmessage=e=>{postMessage(e.data);w.terminate()};"
                else:
                    body += 'postMessage(result);'
            else:
                body = '<body>Isolated worker profile fixture</body>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/javascript' if path.endswith('.js') else 'text/html')
            self.send_header('Content-Security-Policy', "default-src 'self'; worker-src 'self'; script-src 'self'")
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body.encode())))
            self.end_headers()
            self.wfile.write(body.encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/', capture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('kind', ['classic', 'module'])
@pytest.mark.parametrize('us_profile', [False, True])
def test_worker_startup_imports_restart_and_account_isolation(tmp_path, worker_origin, kind, us_profile):
    async def scenario():
        origin, capture = worker_origin
        store = Store(tmp_path)
        store.put('settings', {'browser_channel': 'chrome', 'fingerprint_canvas': True,
                              'fingerprint_webgl': True, 'fingerprint_webgpu': True,
                              'fingerprint_navigator': us_profile, 'fingerprint_fonts': us_profile,
                              'fingerprint_screen': us_profile, 'fingerprint_timezone': 'America/Los_Angeles',
                              'fingerprint_workers': False}, 'settings')
        adapter = Amazon(store)
        contexts = []
        try:
            outcomes = []
            for account_id in ['javascript-profile-0', 'javascript-profile-7']:
                account = store.put('accounts', {'name': 'Worker test', 'region': 'US'}, account_id)
                context = await adapter.context(account)
                contexts.append(context)
                page = await context.new_page()
                await page.goto(origin)
                main = await page.evaluate('() => {' + capture + 'return capture();}', isolated_context=False)
                if us_profile:
                    assert 'HeadlessChrome/' not in main['identity']['ua']
                    assert main['identity']['hints']['brands']
                result = await page.evaluate('''async kind => {
                    const suffix=kind==='module'?'-module':'';
                    const options={type:kind};
                    const dedicated=await new Promise((resolve,reject)=>{
                        const w=new Worker('/dedicated'+suffix+'.js',options);
                        w.onmessage=e=>{w.terminate();resolve(e.data)};w.onerror=reject;
                    });
                    const shared=await new Promise((resolve,reject)=>{
                        const w=new SharedWorker('/shared'+suffix+'.js',options);
                        w.port.onmessage=e=>{w.port.close();resolve(e.data)};w.onerror=reject;
                    });
                    const nested=await new Promise((resolve,reject)=>{
                        const w=new Worker('/nested.js');
                        w.onmessage=e=>{w.terminate();resolve(e.data)};w.onerror=reject;
                    });
                    await navigator.serviceWorker.register('/service'+suffix+'.js',options);
                    await navigator.serviceWorker.ready;
                    if(!navigator.serviceWorker.controller) await new Promise(resolve=>{
                        navigator.serviceWorker.addEventListener('controllerchange',resolve,{once:true});
                    });
                    const service=await (await fetch('/intercepted')).json();
                    return {dedicated,shared,nested,service};
                }''', kind, isolated_context=False)
                session = await context.new_cdp_session(page)
                await session.send('ServiceWorker.enable')
                await session.send('ServiceWorker.stopAllWorkers')
                result['restarted'] = await page.evaluate("async()=>await (await fetch('/intercepted')).json()", isolated_context=False)
                await session.detach()
                for realm, value in result.items():
                    assert value['renderer'] == main['renderer'], (realm, value, main)
                    assert value['pixels'] == main['pixels'], realm
                    if us_profile:
                        assert value['identity'] == main['identity'], (realm, value['identity'], main['identity'])
                    assert value['url'].startswith(origin), value['url']
                assert result['service'] == result['restarted']
                assert not context._retail_worker_profiles.errors
                outcomes.append(main)
            assert outcomes[0]['pixels'] != outcomes[1]['pixels']
            assert len(adapter.profile_browsers) == 2
            assert contexts[0].browser is not contexts[1].browser
        finally:
            await adapter.close()
            assert not adapter.profile_browsers
            assert not adapter.profile_close_tasks
            store.db.close()
    asyncio.run(asyncio.wait_for(scenario(), 90))
