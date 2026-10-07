import asyncio
import hashlib

import pytest
from fastapi.testclient import TestClient

from retail.amazon import Amazon
from retail.app import create_app
from retail.store import Store
from retail.us_fingerprint import SCREEN_PROFILES, context_options
from scripts.fingerprint_differential import IDENTITY_PROBE


def test_us_screen_profiles_repeat_and_do_not_mutate_existing_profile():
    settings = {'fingerprint_screen': True, 'fingerprint_timezone': 'America/Phoenix'}
    seen = set()
    for index in range(100):
        profile = {'seed': hashlib.sha256(str(index).encode()).hexdigest()}
        value = context_options(profile, settings, 'US')
        assert value == context_options(profile, settings, 'US')
        assert value['locale'] == 'en-US' and value['timezone_id'] == 'America/Phoenix'
        assert value['viewport']['height'] < value['screen']['height']
        seen.add((value['screen']['width'], value['screen']['height'], value['device_scale_factor']))
        assert list(profile) == ['seed']
    assert seen == set(SCREEN_PROFILES)
    assert context_options(profile, {}, 'US') == {}
    assert context_options(profile, {**settings, 'fingerprint_backend': 'fingerprint-suite'}, 'US') == {}
    with pytest.raises(ValueError, match='US account'):
        context_options(profile, settings, 'UK')


def test_us_settings_and_timezone_validation(tmp_path):
    with TestClient(create_app(tmp_path), headers={'X-Retail-Client': 'dashboard'}) as client:
        response = client.post('/api/settings', json={'fingerprint_navigator': True,
            'fingerprint_fonts': True, 'fingerprint_screen': True, 'fingerprint_timezone': 'America/Chicago'})
        assert response.status_code == 200
        assert response.json()['fingerprint_timezone'] == 'America/Chicago'
        assert client.post('/api/settings', json={'fingerprint_timezone': 'Europe/London'}).status_code == 422
        assert client.post('/api/settings', json={'cdp_attach': True}).status_code == 422


def test_us_browser_screen_media_navigator_and_health(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        store.put('settings', {'browser_channel': 'chrome', 'fingerprint_navigator': True,
            'fingerprint_screen': True, 'fingerprint_timezone': 'America/Chicago'}, 'settings')
        adapter = Amazon(store)
        outcomes = []
        try:
            for identifier in ['us-profile-a', 'us-profile-b', 'us-profile-a']:
                account = store.put('accounts', {'name': 'US fixture', 'region': 'US'}, identifier)
                context = await adapter.context(account)
                await context.route('https://identity.test/', lambda r: r.fulfill(body='<body>Identity</body>'))
                page = await context.new_page()
                response = await page.goto('https://identity.test/')
                values = await page.evaluate(IDENTITY_PROBE, isolated_context=False)
                assert values['main'] == values['worker'] == values['iframe']
                assert 'HeadlessChrome/' not in values['main']['ua']
                assert 'Chrome/' + adapter.browser.version.split('.')[0] + '.' in values['main']['ua']
                assert (await response.request.all_headers())['user-agent'] == values['main']['ua']
                hints = await page.evaluate('navigator.userAgentData.toJSON()')
                assert hints['brands'] and hints['platform'] == 'Windows'
                assert values['main']['timezone'] == 'America/Chicago'
                assert values['main']['language'] == 'en-US'
                assert (await response.request.all_headers())['accept-language'].startswith('en-US')
                assert all(values['screen'][key] for key in ['cssDpr', 'cssWidth', 'cssScreen'])
                expected = context._retail_us_profile_options
                assert values['screen']['width'] == expected['screen']['width']
                assert values['screen']['dpr'] == expected['device_scale_factor']
                assert values['screen']['innerHeight'] < values['screen']['height']
                getter_checks = await page.evaluate('''() => ['hardwareConcurrency','deviceMemory'].map(name=>{
                    const getter=Object.getOwnPropertyDescriptor(Navigator.prototype,name).get;
                    let error;try{getter.call({})}catch(e){error=e.name}
                    return [getter.call(navigator)===navigator[name],error];
                })''', isolated_context=False)
                assert getter_checks == [[True, 'TypeError']] * 2
                # Health correction must respect the selected profile.
                await page.set_viewport_size({'width': 800, 'height': 600})
                assert (await adapter.profiles.check(page, account))['viewport_corrected']
                assert page.viewport_size == expected['viewport']
                corrected_screen = await page.evaluate('({width:screen.width,height:screen.height})')
                assert corrected_screen == expected['screen']
                outcomes.append(values)
                assert not context._retail_worker_profiles.errors
                await context.close()
            assert outcomes[0] == outcomes[2]
            assert outcomes[0] != outcomes[1]
        finally:
            await adapter.close()
            store.db.close()
    asyncio.run(asyncio.wait_for(scenario(), 60))


def test_font_enumeration_preserves_permissions_native_objects_and_rendering(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        async def capture(enabled, identifier):
            store.put('settings', {'browser_channel': 'chrome', 'fingerprint_fonts': enabled}, 'settings')
            account = store.put('accounts', {'name': 'Font fixture', 'region': 'US'}, identifier)
            context = await adapter.context(account)
            try:
                await context.route('https://font.test/', lambda r: r.fulfill(body='<body>Fonts</body>'))
                page = await context.new_page()
                await page.goto('https://font.test/')
                session = await context.new_cdp_session(page)
                target = await session.send('Target.getTargetInfo')
                permission = {'permission': {'name': 'local-fonts'}, 'origin': 'https://font.test',
                              'browserContextId': target['targetInfo']['browserContextId']}
                await session.send('Browser.setPermission', {**permission, 'setting': 'denied'})
                denied = await page.evaluate('''async()=>{
                    const permission=(await navigator.permissions.query({name:'local-fonts'})).state;
                    try{return {permission,count:(await queryLocalFonts()).length}}catch(e){return {permission,error:e.name}}
                }''', isolated_context=False)
                assert denied['permission'] == 'denied'
                assert denied.get('count') == 0 or denied.get('error') == 'NotAllowedError'
                await session.send('Browser.setPermission', {**permission, 'setting': 'granted'})
                values = await page.evaluate('''async()=>{
                    const fonts=await queryLocalFonts();
                    const repeat=await queryLocalFonts();
                    const records=fonts.map(f=>({family:f.family,name:f.postscriptName}));
                    const targeted=fonts.length?await queryLocalFonts({postscriptNames:[fonts[0].postscriptName]}):[];
                    const blob=fonts.length?await fonts[0].blob():null;
                    const c=document.createElement('canvas');c.width=400;c.height=80;
                    const x=c.getContext('2d');x.font='20px Arial';x.fillText('US font test 123',0,30);
                    const metrics=x.measureText('US font test 123').width;
                    const local=new FontFace('FixtureArial','local("Arial")');await local.load();
                    return {records,repeat:repeat.map(f=>f.postscriptName),
                        targeted:targeted.map(f=>f.postscriptName),
                        native:fonts.every(f=>f instanceof FontData),blobSize:blob?.size,
                        rendering:c.toDataURL(),metrics,localStatus:local.status};
                }''', isolated_context=False)
                assert values['records'] and values['native'] and values['blobSize'] > 0
                assert values['repeat'] == [font['name'] for font in values['records']]
                assert values['targeted'] == [values['records'][0]['name']]
                assert values['localStatus'] == 'loaded'
                await session.detach()
                return {**values, 'denied': denied}
            finally:
                await context.close()
        try:
            native = await capture(False, 'font-native')
            first = await capture(True, 'font-a')
            other = await capture(True, 'font-b')
            repeated = await capture(True, 'font-a')
            assert first == repeated
            native_names = {f['name'] for f in native['records']}
            assert {f['name'] for f in first['records']} < native_names
            assert {f['name'] for f in other['records']} < native_names
            assert first['records'] != other['records']
            for actual in [first, other]:
                assert actual['denied'] == native['denied']
                assert actual['rendering'] == native['rendering'] and actual['metrics'] == native['metrics']
                families = {f['family'] for f in actual['records']}
                assert actual['records'] == [f for f in native['records'] if f['family'] in families]
        finally:
            await adapter.close()
            store.db.close()
    asyncio.run(asyncio.wait_for(scenario(), 90))
