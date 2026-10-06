import asyncio
import shutil

import pytest
from fastapi.testclient import TestClient

from retail.amazon import Amazon
from retail.app import create_app
from retail.fingerprint_suite import ROOT, build_profile
from retail.store import Store

installed = shutil.which('node') and (ROOT / 'node_modules/fingerprint-injector/package.json').is_file()


def test_suite_settings_are_explicit_and_reject_external_browser(tmp_path):
    with TestClient(create_app(tmp_path), headers={'X-Retail-Client': 'dashboard'}) as client:
        assert client.post('/api/settings', json={}).json()['fingerprint_backend'] == 'javascript'
        response = client.post('/api/settings', json={'fingerprint_backend': 'fingerprint-suite'})
        assert response.status_code == 200
        assert response.json()['fingerprint_backend'] == 'fingerprint-suite'
        assert client.post('/api/settings', json={'cdp_attach': True}).status_code == 422


def test_suite_missing_dependency_is_explicit(monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda _: None)
    with pytest.raises(ValueError, match='npm ci'):
        asyncio.run(build_profile(1, 'en-US', '154.0.0.0'))


@pytest.mark.skipif(not installed, reason='Optional fingerprint-suite packages not installed')
def test_suite_profiles_repeat_across_processes_and_match_headers():
    async def scenario():
        first = await build_profile(123, 'en-US', '154.0.0.0')
        repeated = await build_profile(123, 'en-US', '154.0.0.0')
        other = await build_profile(456, 'en-GB', '154.0.0.0')
        assert first == repeated
        assert first['digest'] != other['digest']
        for profile in [first, other]:
            options = profile['options']
            navigator = profile['fingerprint']['navigator']
            assert options['user_agent'] == navigator['userAgent'] == options['extra_http_headers']['user-agent']
            assert options['locale'] == navigator['language']
            assert options['extra_http_headers']['accept-language'].split(',')[0] == navigator['language']
            assert options['viewport']['width'] == profile['fingerprint']['screen']['width']
            assert not {'accept', 'sec-fetch-site', 'te'} & options['extra_http_headers'].keys()
    asyncio.run(scenario())


@pytest.mark.skipif(not installed, reason='Optional fingerprint-suite packages not installed')
def test_suite_uses_only_upstream_hooks_and_preserves_profile_viewport(tmp_path, monkeypatch):
    def custom_hooks_forbidden(*args, **kwargs):
        raise AssertionError('Custom hooks were composed with fingerprint-suite')
    monkeypatch.setattr('retail.amazon.build_scripts', custom_hooks_forbidden)

    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'browser_channel': 'chrome', 'fingerprint_backend': 'fingerprint-suite',
                              **{'fingerprint_' + key: True for key in ['canvas', 'webgl', 'webgpu', 'audio', 'workers']}}, 'settings')
        account = store.put('accounts', {'name': 'Suite test', 'region': 'US'}, 'suite-fixture')
        adapter = Amazon(store)
        try:
            context = await adapter.context(account)
            profile = context._retail_suite_profile
            assert not hasattr(context, '_retail_worker_profiles')
            assert not adapter.profile_browsers
            await context.route('https://suite.test/', lambda r: r.fulfill(body='<body>Suite fixture</body>'))
            page = await context.new_page()
            response = await page.goto('https://suite.test/')
            headers = await response.request.all_headers()
            observed = await page.evaluate('''() => ({ua:navigator.userAgent,language:navigator.language,
                renderer:(()=>{const g=document.createElement('canvas').getContext('webgl');
                    return g.getParameter(g.getExtension('WEBGL_debug_renderer_info').UNMASKED_RENDERER_WEBGL)})()})''', isolated_context=False)
            assert observed['ua'] == headers['user-agent'] == profile['options']['user_agent']
            assert observed['language'] == profile['options']['locale']
            assert observed['renderer'] == profile['fingerprint']['videoCard']['renderer']
            expected = profile['options']['viewport']
            await page.set_viewport_size({'width': 800, 'height': 600})
            report = await adapter.profiles.check(page, account)
            assert report['viewport_corrected'] and page.viewport_size == expected
            stored = adapter.profiles.get(account)
            assert 'observed_suite' in stored and 'observed' not in stored
        finally:
            await adapter.close()
            store.db.close()
    asyncio.run(asyncio.wait_for(scenario(), 60))
