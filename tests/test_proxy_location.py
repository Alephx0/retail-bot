import asyncio
import base64
import json
import socketserver
import ssl
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from retail import proxy_location as location
from retail.amazon import Amazon
from retail.store import Store
from scripts.fingerprint_differential import IDENTITY_PROBE


def us_response(**changes):
    return {'success': True, 'ip': '8.8.8.8', 'country_code': 'US',
            'timezone': {'id': 'America/Los_Angeles'}, 'latitude': 34.05,
            'longitude': -118.24, 'region': 'California', 'city': 'Los Angeles', **changes}


@pytest.mark.parametrize('changes', [
    {'success': False}, {'ip': '127.0.0.1'}, {'ip': 'not-an-ip'}, {'country_code': 'CA'},
    {'timezone': {'id': 'Europe/London'}}, {'timezone': {'id': 'America/Toronto'}},
    {'timezone': {'id': 'America/Imaginary'}}, {'timezone': None},
    {'latitude': float('nan')}, {'longitude': 181}, {'latitude': True},
])
def test_proxy_location_rejects_invalid_exits(changes):
    with pytest.raises(location.ProxyLocationError):
        location.validate_location(us_response(**changes))


def test_proxy_location_accepts_regional_iana_zones_and_rejects_rotation():
    first = location.validate_location(us_response(timezone={'id': 'America/Indiana/Indianapolis'}))
    assert location.context_options(first)['timezone_id'] == 'America/Indiana/Indianapolis'
    assert 'ip' not in location.summary(first)
    location.ensure_same_route(first, first)
    for changes in ({'ip': '1.1.1.1'}, {'timezone': 'America/New_York'}):
        with pytest.raises(location.ProxyLocationError, match='sticky US'):
            location.ensure_same_route(first, {**first, **changes})


@pytest.mark.parametrize('status,body', [(429, b'quota'), (200, b'not json'), (200, b'x' * 65537)],
                         ids=['quota', 'invalid-json', 'oversized'])
def test_proxy_lookup_fails_closed_and_closes_temporary_context(status, body):
    async def scenario():
        response = AsyncMock(status=status)
        response.body.return_value = body
        page = AsyncMock()
        page.goto.return_value = response
        context = AsyncMock()
        context.new_page.return_value = page
        browser = AsyncMock()
        browser.new_context.return_value = context
        proxy = {'server': 'http://proxy.test:80', 'username': 'user', 'password': 'secret'}
        with pytest.raises(location.ProxyLocationError) as error:
            await location.bootstrap_location(browser, proxy)
        assert 'secret' not in str(error.value)
        browser.new_context.assert_awaited_once_with(proxy=proxy, locale='en-US')
        page.close.assert_awaited_once()
        context.close.assert_awaited_once()
    asyncio.run(scenario())


@pytest.fixture
def proxy_fixture(tmp_path):
    """An authenticated local CONNECT proxy serving HTTPS fixtures, never forwarding traffic."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Proxy test fixture')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'fixture.pem', tmp_path / 'fixture.key'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    state = {'lookups': [], 'rotate': False, 'response': us_response()}
    expected_auth = 'Basic ' + base64.b64encode(b'fixture-user:fixture-password').decode()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            sock = self.request
            sock.settimeout(5)
            try:
                data = b''
                while b'\r\n\r\n' not in data and len(data) < 16384:
                    chunk = sock.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                headers = data.decode('latin1')
                fields = dict(line.split(': ', 1) for line in headers.split('\r\n')[1:] if ': ' in line)
                auth = next((value for key, value in fields.items() if key.lower() == 'proxy-authorization'), None)
                if auth != expected_auth:
                    sock.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="fixture"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    return
                host = headers.split(' ', 2)[1]
                if not headers.startswith('CONNECT '):
                    return
                sock.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                with tls.wrap_socket(sock, server_side=True) as connection:
                    request = b''
                    while b'\r\n\r\n' not in request and len(request) < 16384:
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        request += chunk
                    if host == 'ipwho.is:443' and request.startswith(b'GET /?fields='):
                        state['lookups'].append(host)
                        payload = dict(state['response'])
                        if state['rotate'] and len(state['lookups']) % 2 == 0:
                            payload['ip'] = '1.1.1.1'
                        body, mime = json.dumps(payload).encode(), 'application/json'
                    else:
                        body, mime = b'<body>Local proxy fixture</body>', 'text/html'
                    connection.sendall(f'HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
            except (OSError, ssl.SSLError):
                pass  # Browser speculative connections can close before a request.

    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'127.0.0.1:{server.server_address[1]}:fixture-user:fixture-password', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_actual_browser_proxy_alignment_rotation_and_restoration(tmp_path, proxy_fixture, monkeypatch):
    async def scenario():
        from patchright.async_api import BrowserType
        launch = BrowserType.launch

        async def fixture_launch(self, **options):
            # Trust the fixture's self-signed certificate only in this test.
            options['args'] = [*options.get('args', []), '--ignore-certificate-errors']
            return await launch(self, **options)

        monkeypatch.setattr(BrowserType, 'launch', fixture_launch)
        proxy, fixture = proxy_fixture
        store = Store(tmp_path / 'store')
        settings = {'browser_channel': 'chrome', 'fingerprint_fonts': True,
                    'fingerprint_navigator': True, 'fingerprint_screen': True,
                    'fingerprint_timezone': 'America/New_York'}
        store.put('settings', settings, 'settings')
        account = store.put('accounts', {'name': 'Proxy fixture', 'region': 'US', 'proxy': proxy}, 'proxy-fixture')
        adapter = Amazon(store)
        try:
            context = await adapter.context(account)
            assert len(fixture['lookups']) == 2
            assert context._retail_us_profile_options['timezone_id'] == 'America/Los_Angeles'
            page = await context.new_page()
            await page.goto('https://identity.test/')
            identity = await page.evaluate(IDENTITY_PROBE, isolated_context=False)
            assert identity['main'] == identity['worker'] == identity['iframe']
            assert identity['main']['timezone'] == 'America/Los_Angeles'
            assert identity['main']['languages'] == ['en-US']
            assert await page.evaluate("async()=>(await navigator.permissions.query({name:'geolocation'})).state") == 'prompt'
            await context.grant_permissions(['geolocation'], origin='https://identity.test')
            coords = await page.evaluate('''()=>new Promise((resolve,reject)=>navigator.geolocation.getCurrentPosition(
                p=>resolve({latitude:p.coords.latitude,longitude:p.coords.longitude,accuracy:p.coords.accuracy}),reject))''')
            assert coords == {'latitude': 34.05, 'longitude': -118.24, 'accuracy': 50000}
            health = await adapter.profiles.check(page, account)
            assert health['proxy_location']['city'] == 'Los Angeles'
            assert 'ip' not in health['proxy_location']
            await context.close()

            # Fresh resolution, even for a reused gateway; a new exit can change zone.
            fixture['response']['timezone']['id'] = 'America/Chicago'
            second = await adapter.context(account)
            assert second._retail_us_profile_options['timezone_id'] == 'America/Chicago'
            await second.close()
            assert len(fixture['lookups']) == 4

            fixture['rotate'] = True
            with pytest.raises(location.ProxyLocationError, match='sticky US'):
                await adapter.context(account)
            assert not adapter.context_accounts
            fixture['rotate'] = False
            fixture['response']['country_code'] = 'CA'
            with pytest.raises(location.ProxyLocationError, match='US exit'):
                await adapter.context(account)

            # Explicitly disabling auto matching restores the manual timezone and makes no lookup.
            count = len(fixture['lookups'])
            store.put('settings', {**settings, 'fingerprint_proxy_location': False}, 'settings')
            manual = await adapter.context(account)
            assert manual._retail_us_profile_options['timezone_id'] == 'America/New_York'
            assert len(fixture['lookups']) == count
            await manual.close()
            store.put('settings', settings, 'settings')
            with pytest.raises(location.ProxyLocationError, match='no proxy route'):
                await adapter.context(account, proxy='')
        finally:
            await adapter.close()
            assert not adapter.profile_browsers
            store.db.close()
    asyncio.run(asyncio.wait_for(scenario(), 100))
