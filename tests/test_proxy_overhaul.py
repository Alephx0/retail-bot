import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from retail.app import create_app
from retail.models import ProxyList
from retail.proxy_protocol import parse_proxy
from retail.proxy_probe import probe
from retail.proxy_pool import ProxyPool
from retail.services import ProxyHealth
from retail.store import Store, now


@pytest.mark.parametrize('line', ['http://u:SECRET@host:80/path','host:0','host:65536','host:abc',
    'ftp://host:80','http://host:80/?secret=x','host:80:u:bad\nvalue','socks4://u:password@host:80'])
def test_invalid_input_never_echoes_secrets(line):
    with pytest.raises(ValueError) as exc:parse_proxy(line)
    assert line not in str(exc.value)
    assert 'SECRET' not in str(exc.value)


def test_canonical_identity_and_protocol_compatibility():
    assert ProxyList(name='Pool',entries='HOST:80:u:p:a\nhttp://u:p%3Aa@host:80').entries=='host:80:u:p:a'
    assert parse_proxy('socks5://u:p@host:80').browser_compatible is False
    assert parse_proxy('socks4://host:80').browser_compatible
    assert parse_proxy('http://[::1]:80').config()['server']=='http://[::1]:80'
    assert 'SECRET' not in str(parse_proxy('host:80:u:SECRET').public())


@pytest.mark.parametrize('scheme,authenticated,status', [('http',False,200),('http',True,200),('http',True,407),
    ('http',False,403),('http',False,429),('socks5',False,200),('socks5',True,200),('socks5',True,407),('socks4',False,200)])
def test_actual_protocol_negotiation(scheme,authenticated,status):
    async def scenario():
        received=[]
        async def handler(reader,writer):
            try:
                if scheme=='socks5':
                    assert await reader.readexactly(3)==b'\x05\x01'+(b'\x02' if authenticated else b'\x00')
                    writer.write(b'\x05'+(b'\x02' if authenticated else b'\x00'));await writer.drain()
                    if authenticated:
                        assert await reader.readexactly(1)==b'\x01'
                        assert await reader.readexactly((await reader.readexactly(1))[0])==b'user'
                        assert await reader.readexactly((await reader.readexactly(1))[0])==b'SECRET'
                        writer.write(b'\x01'+(b'\x01' if status==407 else b'\x00'));await writer.drain()
                        if status==407:return
                    assert await reader.readexactly(4)==b'\x05\x01\x00\x01'
                    await reader.readexactly(6)
                    writer.write(b'\x05\x00\x00\x01'+b'\x00'*6);await writer.drain()
                elif scheme=='socks4':
                    assert (await reader.readexactly(8))[:2]==b'\x04\x01'
                    await reader.readuntil(b'\x00')
                    writer.write(b'\x00\x5a'+b'\x00'*6);await writer.drain()
                request=await reader.readuntil(b'\r\n\r\n');received.append(request)
                if authenticated and scheme=='http':assert b'Proxy-Authorization: Basic dXNlcjpTRUNSRVQ=' in request
                body=b'{"ip":"8.8.8.8"}'
                writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\nRetry-After: 120\r\n\r\n'.encode()+body)
                await writer.drain()
            finally:
                writer.close();await writer.wait_closed()
        server=await asyncio.start_server(handler,'127.0.0.1',0)
        async with server:
            port=server.sockets[0].getsockname()[1]
            line=f'{scheme}://'+('user:SECRET@' if authenticated else '')+f'127.0.0.1:{port}'
            result=await probe(line,'http://127.0.0.1:8080/',timeout=1)
        assert 'SECRET' not in json.dumps(result)
        if status==407:assert result['status']=='authentication_failed'
        else:
            assert result['status']=='healthy'
            assert result['connect_ms'] is not None and result['ttfb_ms'] is not None
            assert result['tls_ms'] is None
            if status==429:assert result['application_status']=='rate_limited' and result['retry_after_seconds']==120
            if status==403:assert result['application_status']=='denied'
        assert received or status==407
    asyncio.run(scenario())


def test_batch_backpressure_partial_failure_cancellation_and_cache(tmp_path,monkeypatch):
    async def scenario():
        store=Store(tmp_path);store.put('settings',{'proxy_concurrency':3},'settings')
        pool=store.put('proxies',{'name':'Pool','entries':'\n'.join(f'host{i}:80' for i in range(30))})
        ProxyPool(store).sync();health=ProxyHealth(store)
        active=peak=calls=0
        async def measured(line,*args,**kw):
            nonlocal active,peak,calls
            active+=1;peak=max(peak,active);calls+=1
            try:
                await asyncio.sleep(.01)
                bad=line.startswith('host0:')
                return {**parse_proxy(line).public(),'status':'unreachable' if bad else 'healthy',
                        'error':'timeout' if bad else None,'latency_ms':10,'observed_at':now()}
            finally:active-=1
        monkeypatch.setattr('retail.proxy_testing.probe',measured)
        ticks=0
        async def heartbeat():
            nonlocal ticks
            while health.jobs:ticks+=1;await asyncio.sleep(.005)
        await health.start(pool,'amazon');await asyncio.gather(*health.jobs.values(),heartbeat())
        assert peak<=3 and ticks>=5 and calls==31
        rows=store.get('proxy_health','health-'+pool['id'])['results']
        assert len(rows)==30 and sum(r['status']=='healthy' for r in rows)==29
        await health.start(pool,'amazon',force=False);await asyncio.gather(*health.jobs.values())
        assert calls==31
        await health.start(pool,'amazon');await asyncio.sleep(.005);await health.cancel(pool['id'])
        assert not health.jobs and active==0
        assert store.get('proxy_health','health-'+pool['id'])['status']=='cancelled'
        store.db.close()
    asyncio.run(scenario())


def test_route_remains_bound_after_health_change(tmp_path):
    store=Store(tmp_path);pool=store.put('proxies',{'name':'Pool','entries':'first:80\nsecond:80'})
    selector=ProxyPool(store);selector.sync();chosen=selector.choose(pool['id'],'account')
    item=store.get('proxy_endpoints','endpoint-'+parse_proxy(chosen).fingerprint)
    store.put('proxy_endpoints',{**item,'status':'unreachable','history':[{}]})
    assert selector.choose(pool['id'],'account')==chosen
    store.db.close()


def test_idle_health_yields_and_does_not_cancel_later_manual_job(tmp_path,monkeypatch):
    async def scenario():
        store=Store(tmp_path);store.put('settings',{'proxy_background_checks':True},'settings')
        pool=store.put('proxies',{'name':'Pool','entries':'host:80'})
        entered=asyncio.Event();release=asyncio.Event()
        async def measured(line,*args,**kw):
            entered.set();await release.wait()
            return {**parse_proxy(line).public(),'status':'healthy','latency_ms':1,'observed_at':now()}
        monkeypatch.setattr('retail.proxy_testing.probe',measured)
        health=ProxyHealth(store)
        await health.maintenance(idle=True);await entered.wait()
        await health.maintenance(idle=False)
        assert not health.jobs and health.background_group is None
        await health.start(pool,'amazon');await health.maintenance(idle=False)
        assert health.jobs
        release.set();await asyncio.gather(*health.jobs.values())
        store.db.close()
    asyncio.run(scenario())


def test_api_import_remove_and_redaction(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.headers['X-Retail-Client']='dashboard'
        response=client.post('/api/proxies',json={'name':'Pool','entries':'HOST:80:u:SECRET\nhttp://u:SECRET@host:80\nsocks5://second:80'})
        assert response.status_code==200
        pool=response.json();assert pool['count']==2 and 'SECRET' not in response.text
        row=parse_proxy('host:80:u:SECRET').fingerprint
        response=client.post(f"/api/proxies/{pool['id']}/connections",json={'remove':[row],'add':'third:80'})
        assert response.status_code==200 and response.json()['count']==2
        public=client.get('/api/state').text
        assert 'SECRET' not in public and '"history"' not in public


@pytest.mark.parametrize('proxy_tls,target_tls',[(False,True),(True,False),(True,True)])
def test_actual_tls_and_connect_timings(tmp_path,proxy_tls,target_tls):
    import ssl
    from datetime import datetime,timedelta,timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
    stamp=datetime.now(timezone.utc)
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(stamp-timedelta(days=1)).not_valid_after(stamp+timedelta(days=1)).sign(key,hashes.SHA256())
    pem=tmp_path/'certificate.pem';pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private=tmp_path/'private.pem';private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    server_tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_tls.load_cert_chain(pem,private)
    client_tls=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);client_tls.check_hostname=False;client_tls.verify_mode=ssl.CERT_NONE
    async def scenario():
        async def handler(reader,writer):
            try:
                if target_tls:
                    request=await reader.readuntil(b'\r\n\r\n');assert request.startswith(b'CONNECT ')
                    writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n');await writer.drain()
                    await writer.start_tls(server_tls)
                await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK');await writer.drain()
            finally:writer.close()
        server=await asyncio.start_server(handler,'127.0.0.1',0,ssl=server_tls if proxy_tls else None)
        async with server:
            result=await probe(f'{"https" if proxy_tls else "http"}://127.0.0.1:{server.sockets[0].getsockname()[1]}',
                f'{"https" if target_tls else "http"}://localhost/',tls_context=client_tls,timeout=3)
        assert result['status']=='healthy',result
        assert (result['tls_ms'] is not None)==target_tls
        assert (result['proxy_tls_ms'] is not None)==proxy_tls
        assert result['ttfb_ms'] is not None
    asyncio.run(scenario())
