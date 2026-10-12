"""Bounded, read-only HTTP measurements over negotiated proxy transports.

No browser automation, redirects, cookies, retailer recovery or credentials in results.
Timings are disjoint where possible: connect includes DNS; TTFB starts at request send.
"""
import asyncio
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urlsplit

import h11

from .proxy_protocol import parse_proxy

NEUTRAL = 'https://api.ipify.org/?format=json'
TRANSIENT = {'timeout', 'connection_failed', 'connection_reset'}


class ProbeError(Exception):
    pass


def destination(value):
    try:
        p = urlsplit(value)
        if p.scheme not in {'https','http'} or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError()
        if any(ord(c)<33 or ord(c)==127 for c in value) or len(value)>2048:
            raise ValueError()
        p.port
        return p
    except ValueError:
        raise ValueError('Use a permitted HTTP(S) diagnostic URL without credentials or a fragment.') from None


def retry_after(value):
    if not value:
        return None
    try:
        return max(0, float(value)) if value.isdigit() else max(0, (parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


async def headers(reader):
    raw = await reader.readuntil(b'\r\n\r\n')
    if len(raw)>16384:
        raise ProbeError('protocol_error')
    try:
        status = int(raw.split(b' ',2)[1])
    except (ValueError, IndexError):
        raise ProbeError('protocol_error') from None
    return status


async def negotiate(proxy, reader, writer, target):
    host, port = target.hostname, target.port or (443 if target.scheme=='https' else 80)
    authority = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
    if proxy.scheme in {'http','https'}:
        if target.scheme == 'http':
            return
        auth = b''
        if proxy.username:
            auth = b'Proxy-Authorization: Basic '+base64.b64encode(f'{proxy.username}:{proxy.password}'.encode())+b'\r\n'
        writer.write(f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n'.encode()+auth+b'\r\n')
        await writer.drain()
        status = await headers(reader)
        if status != 200:
            raise ProbeError('authentication_failed' if status==407 else 'proxy_rejected')
    elif proxy.scheme == 'socks5':
        writer.write(b'\x05\x01'+(b'\x02' if proxy.username else b'\x00'))
        await writer.drain()
        version, method = await reader.readexactly(2)
        if version!=5 or method not in (2 if proxy.username else 0,):
            raise ProbeError('authentication_failed' if method==255 or method==2 else 'protocol_error')
        if method == 2:
            user, password = proxy.username.encode(), proxy.password.encode()
            writer.write(b'\x01'+bytes([len(user)])+user+bytes([len(password)])+password)
            await writer.drain()
            if await reader.readexactly(2) != b'\x01\x00':
                raise ProbeError('authentication_failed')
        host_bytes = host.encode('idna')
        try:
            ip = ipaddress.ip_address(host)
            address = (b'\x01' if ip.version==4 else b'\x04')+ip.packed
        except ValueError:
            address = b'\x03'+bytes([len(host_bytes)])+host_bytes
        writer.write(b'\x05\x01\x00'+address+port.to_bytes(2,'big'))
        await writer.drain()
        version, reply, reserved, kind = await reader.readexactly(4)
        if version!=5 or reserved or reply:
            raise ProbeError('proxy_rejected')
        size = 4 if kind==1 else 16 if kind==4 else (await reader.readexactly(1))[0] if kind==3 else 0
        if not size:
            raise ProbeError('protocol_error')
        await reader.readexactly(size+2)
    else:
        # SOCKS4 uses IPv4 destination resolution locally (Chromium-compatible).
        resolved = await asyncio.get_running_loop().getaddrinfo(host,port,family=socket.AF_INET,type=socket.SOCK_STREAM)
        writer.write(b'\x04\x01'+port.to_bytes(2,'big')+socket.inet_aton(resolved[0][4][0])+proxy.username.encode()+b'\x00')
        await writer.drain()
        response = await reader.readexactly(8)
        if response[0]!=0 or response[1]!=90:
            raise ProbeError('authentication_failed' if response[1] in (92,93) else 'proxy_rejected')


@lru_cache(maxsize=1)
def default_tls():
    return ssl.create_default_context()


async def probe(line, url=NEUTRAL, *, timeout=15, throughput=False, tls_context=None):
    proxy, target = parse_proxy(line), destination(url)
    result = {**proxy.public(), 'transport':'asyncio', 'status':'unreachable',
              'connect_ms':None,'tls_ms':None,'proxy_tls_ms':None,'negotiation_ms':None,
              'ttfb_ms':None,'throughput_mbps':None,'exit_ip':None,'error':None,
              'observed_at':datetime.now(timezone.utc).isoformat()}
    start = time.perf_counter()
    writer = None
    def elapsed(s): return round((time.perf_counter()-s)*1000,2)
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(proxy.host,proxy.port,limit=16384)
            result['connect_ms']=elapsed(start)
            tls = tls_context or default_tls()
            if proxy.scheme=='https':
                tick=time.perf_counter()
                await writer.start_tls(tls,server_hostname=proxy.host)
                result['proxy_tls_ms']=elapsed(tick)
            tick=time.perf_counter()
            await negotiate(proxy,reader,writer,target)
            result['negotiation_ms']=elapsed(tick)
            if target.scheme=='https':
                tick=time.perf_counter()
                await writer.start_tls(tls,server_hostname=target.hostname)
                result['tls_ms']=elapsed(tick)
            connection=h11.Connection(h11.CLIENT,max_incomplete_event_size=16384)
            path=(target.path or '/')+('?' + target.query if target.query else '')
            forward=proxy.scheme in {'http','https'} and target.scheme=='http'
            request_headers=[('Host',target.netloc),('Connection','close'),('Accept-Encoding','identity')]
            if forward and proxy.username:
                request_headers.append(('Proxy-Authorization','Basic '+base64.b64encode(f'{proxy.username}:{proxy.password}'.encode()).decode()))
            tick=time.perf_counter()
            writer.write(connection.send(h11.Request(method='GET',target=url if forward else path,headers=request_headers)))
            writer.write(connection.send(h11.EndOfMessage()))
            await writer.drain()
            body=bytearray(); received=0; first=None; status=None; response_headers={}
            cap=1024*1024 if throughput else 8192
            while received<cap:
                event=connection.next_event()
                if event is h11.NEED_DATA:
                    data=await reader.read(min(8192,cap-received))
                    if data and first is None:
                        first=time.perf_counter();result['ttfb_ms']=elapsed(tick)
                    connection.receive_data(data)
                elif isinstance(event,h11.Response):
                    status=event.status_code
                    response_headers={k.decode().lower():v.decode('latin1') for k,v in event.headers}
                    if status==407:raise ProbeError('authentication_failed')
                    if status>=400:break
                elif isinstance(event,h11.Data):
                    received+=len(event.data)
                    if len(body)<8192:body.extend(event.data[:8192-len(body)])
                elif isinstance(event,(h11.EndOfMessage,h11.ConnectionClosed)):
                    break
                elif event is h11.PAUSED:
                    break
            if status is None:raise ProbeError('protocol_error')
            result.update(status='healthy',http_status=status,application_status='rate_limited' if status==429 else 'denied' if status==403 else 'http_error' if status>=400 else 'ok',
                          retry_after_seconds=retry_after(response_headers.get('retry-after')))
            if throughput and first and received:
                result['throughput_mbps']=round(received*8/max(time.perf_counter()-first,.000001)/1_000_000,3)
                result['download_bytes']=received
            if url==NEUTRAL and status==200:
                try: result['exit_ip']=str(ipaddress.ip_address(json.loads(body)['ip']))
                except (ValueError,KeyError,TypeError): pass
    except ProbeError as exc:
        result.update(error=str(exc),status='authentication_failed' if str(exc)=='authentication_failed' else 'unreachable')
    except TimeoutError:
        result['error']='timeout'
    except ssl.SSLError:
        result['error']='tls_failed'
    except (ConnectionResetError, asyncio.IncompleteReadError):
        result['error']='connection_reset'
    except OSError:
        result['error']='connection_failed'
    except (h11.ProtocolError,asyncio.LimitOverrunError,ValueError,UnicodeError):
        result['error']='protocol_error'
    finally:
        result['latency_ms']=elapsed(start)
        if writer:
            writer.close()
            try:
                async with asyncio.timeout(1):await writer.wait_closed()
            except (OSError,TimeoutError):pass
    return result
