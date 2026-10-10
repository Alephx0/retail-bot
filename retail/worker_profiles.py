"""Initialize worker realms before startup in an app-owned Chromium process.

Uses the supported DevTools protocol on a loopback-only endpoint. Worker
scripts, URLs, service-worker registrations, caches and CSP stay intact.
"""
import asyncio
import json
import socket
from http.client import HTTPConnection
from collections import Counter
from urllib.parse import urlsplit

from websockets.asyncio.client import connect


def debugging_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def debugger_endpoint(port):
    """Read only the allocated loopback endpoint; no proxies or redirects."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Invalid local debugger port')
    connection = HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        connection.request('GET', '/json/version')
        response = connection.getresponse()
        body = response.read(65537)
        if response.status != 200 or len(body) > 65536:
            raise RuntimeError('Local browser debugger returned an invalid response')
        endpoint = json.loads(body)['webSocketDebuggerUrl']
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'ws' or parsed.hostname != '127.0.0.1' or parsed.port != port
                or parsed.username or parsed.password):
            raise RuntimeError('Worker profile endpoint must use the allocated loopback port')
        return endpoint
    finally:
        connection.close()


class WorkerProfiles:
    FILTER = [{'type': name} for name in ('page', 'iframe', 'worker', 'shared_worker', 'service_worker')]

    def __init__(self, connection, script):
        self.connection = connection
        self.script = script
        self.pending = {}
        self.tasks = set()
        self.errors = []
        self.initialized = Counter()
        self.targets = {}
        self.sequence = 0
        self.closed = False
        self.reader = asyncio.create_task(self._read())

    @classmethod
    async def start(cls, browser, port, script):
        # Confirm that the loopback endpoint belongs to the browser owning
        # the newly created context before enabling any target interception.
        session = await browser.new_browser_cdp_session()
        try:
            expected = await session.send('Target.createTarget', {'url': 'about:blank'})
        finally:
            await session.detach()
        # Avoid constructing a TLS client/certificate store for plain loopback
        # HTTP. Keep socket I/O off the scheduling/event loop.
        endpoint = await asyncio.to_thread(debugger_endpoint, port)
        instance = cls(await connect(endpoint, proxy=None, max_size=8 * 1024 * 1024), script)
        try:
            actual = await instance.send('Target.getTargetInfo', {'targetId': expected['targetId']})
            if actual['targetInfo']['targetId'] != expected['targetId']:
                raise RuntimeError('Worker profile endpoint does not match the owned browser')
            await instance.send('Target.closeTarget', {'targetId': expected['targetId']})
            await instance.send('Target.setAutoAttach', {
                'autoAttach': True, 'waitForDebuggerOnStart': True,
                'flatten': True, 'filter': cls.FILTER,
            })
            return instance
        except BaseException:
            await instance.close()
            raise

    async def send(self, method, params=None, session=None):
        self.sequence += 1
        identifier = self.sequence
        message = {'id': identifier, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.connection.send(json.dumps(message))
            return await asyncio.wait_for(future, 10)
        except BaseException:
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()
            raise
        finally:
            self.pending.pop(identifier, None)

    async def _read(self):
        try:
            async for raw in self.connection:
                message = json.loads(raw)
                if 'id' in message:
                    future = self.pending.get(message['id'])
                    if future is not None and not future.done():
                        if 'error' in message:
                            future.set_exception(RuntimeError(message['error']['message']))
                        else:
                            future.set_result(message.get('result', {}))
                elif message.get('method') == 'Target.attachedToTarget':
                    task = asyncio.create_task(self._attached(message['params']))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                elif message.get('method') == 'Debugger.paused':
                    task = asyncio.create_task(self._paused(message['sessionId']))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                elif message.get('method') == 'Target.detachedFromTarget':
                    self.targets.pop(message['params']['sessionId'], None)
        except Exception as exc:
            if not self.closed:
                self.errors.append(f'Worker profile connection: {exc}')
        finally:
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError('Worker profile connection closed'))

    async def _attached(self, event):
        session = event['sessionId']
        kind = event['targetInfo']['type']
        self.targets[session] = event['targetInfo']['targetId']
        try:
            # Auto-attachment isn't recursive; subscribe in pages/iframes and
            # workers as well so nested and dedicated workers are covered.
            await self.send('Target.setAutoAttach', {
                'autoAttach': True, 'waitForDebuggerOnStart': True,
                'flatten': True, 'filter': [{'type': 'worker'}, {'type': 'iframe'}],
            }, session)
            if kind in ('worker', 'shared_worker', 'service_worker') and self.script:
                result = await self.send('Runtime.evaluate', {
                    'expression': self.script, 'returnByValue': True, 'disableBreaks': True,
                }, session)
                if result.get('exceptionDetails'):
                    raise RuntimeError(result['exceptionDetails'].get('text', 'Worker initialization failed'))
                self.initialized[kind] += 1
                await self.send('Debugger.enable', session=session)
                # A service-worker version can reuse its target/session with
                # a fresh JS global after stop/start. Instrumentation survives
                # that lifecycle and initializes each script before it runs.
                await self.send('Debugger.setInstrumentationBreakpoint', {
                    'instrumentation': 'beforeScriptExecution',
                }, session)
        except Exception as exc:
            if not self.closed and not self._target_closed(exc):
                self.errors.append(f'{kind}: {type(exc).__name__}: {exc}')
                await self._close_target(session)
        finally:
            # Never leave a worker suspended, including initialization failure.
            try:
                await self.send('Runtime.runIfWaitingForDebugger', session=session)
            except Exception:
                pass  # Target may have closed while attachment was in flight.

    async def _paused(self, session):
        try:
            result = await self.send('Runtime.evaluate', {
                'expression': self.script, 'returnByValue': True, 'disableBreaks': True,
            }, session)
            if result.get('exceptionDetails'):
                raise RuntimeError(result['exceptionDetails'].get('text', 'Worker initialization failed'))
        except Exception as exc:
            if not self.closed and not self._target_closed(exc):
                self.errors.append(f'Worker restart: {type(exc).__name__}: {exc}')
                await self._close_target(session)
        finally:
            try:
                await self.send('Debugger.resume', session=session)
            except Exception:
                pass

    @staticmethod
    def _target_closed(exc):
        return any(text in str(exc).lower() for text in (
            'no session with given id', 'session with given id not found', 'session closed', 'target closed',
            'target was closed', 'no target with given id',
        ))

    async def _close_target(self, session):
        # A failed bootstrap must not release an uninitialized worker into
        # application code. Surface the error and close this owned target.
        target = self.targets.get(session)
        if target:
            try:
                await self.send('Target.closeTarget', {'targetId': target})
            except Exception:
                pass

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.connection.close()
        await asyncio.gather(self.reader, return_exceptions=True)
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
