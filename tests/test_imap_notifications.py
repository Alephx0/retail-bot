"""Actual IMAP wire exchange on localhost; synthetic mail and no retailer calls."""
import asyncio
import time
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from retail import imap_wait
from retail.identity import IdentityService
from retail.store import Store


def mail(recipient='buyer@example.test', code='123456', sender='account-update@amazon.com', age=0):
    message = EmailMessage()
    message['From'], message['To'] = sender, recipient
    message.set_content('Your verification code is ' + code)
    return {'raw': message.as_bytes(), 'received': time.time() - age}


class MailServer:
    def __init__(self, *, idle=True):
        self.supports_idle = idle
        self.messages = {}
        self.connections = set()
        self.idlers = set()
        self.idling = asyncio.Event()
        self.login_started = asyncio.Event()
        self.release_login = asyncio.Event()
        self.release_login.set()
        self.commands = []
        self.logins = 0
        self.fetches = []
        self.arrive_during_fetch = None
        self.arrive_before_idle_ack = None
        self.idle_rejected = False
        self.validity = 123
        self.tasks = set()
        self.searched = asyncio.Event()
        self.nooped = asyncio.Event()
        self.command_latency = 0

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        return self.server.sockets[0].getsockname()[1]

    def add(self, message):
        uid = max(self.messages, default=0) + 1
        self.messages[uid] = message
        for writer in self.idlers:
            writer.write(f'* {len(self.messages)} EXISTS\r\n'.encode())
        return uid

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.connections.add(writer)
        writer.write(b'* OK local fixture\r\n')
        idle_tag = None
        try:
            while line := await reader.readline():
                if line == b'DONE\r\n':
                    if self.command_latency:await asyncio.sleep(self.command_latency)
                    self.idlers.discard(writer)
                    writer.write(idle_tag + b' OK IDLE finished\r\n')
                    continue
                tag, command, *args = line.strip().split()
                command = command.upper()
                self.commands.append(command)
                if self.command_latency:await asyncio.sleep(self.command_latency)
                if command == b'CAPABILITY':
                    writer.write(b'* CAPABILITY IMAP4rev1' + (b' IDLE' if self.supports_idle else b'') + b'\r\n')
                elif command == b'LOGIN':
                    self.logins += 1
                    self.login_started.set()
                    await self.release_login.wait()
                elif command == b'EXAMINE':
                    writer.write(f'* {len(self.messages)} EXISTS\r\n* FLAGS (\\Seen)\r\n* 0 RECENT\r\n* OK [UIDVALIDITY {self.validity}] valid\r\n'.encode())
                elif command == b'UID' and args[0].upper() == b'SEARCH':
                    writer.write(b'* SEARCH ' + b' '.join(str(uid).encode() for uid in self.messages) + b'\r\n')
                    self.searched.set()
                elif command == b'UID' and args[0].upper() == b'FETCH':
                    uids = [int(uid) for uid in args[1].split(b',')]
                    self.fetches.append(uids)
                    assert b'BODY[' not in line or b'BODY.PEEK[]' in line
                    for uid in uids:
                        if uid not in self.messages:
                            continue
                        message = self.messages[uid]
                        stamp = datetime.fromtimestamp(message['received'], timezone.utc).strftime('%d-%b-%Y %H:%M:%S +0000')
                        raw = message['raw']
                        if b'BODY.PEEK[]' in line:
                            writer.write(f'* {uid} FETCH (UID {uid} INTERNALDATE "{stamp}" BODY[] {{{len(raw)}}}\r\n'.encode() + raw + b')\r\n')
                        else:
                            writer.write(f'* {uid} FETCH (UID {uid} INTERNALDATE "{stamp}")\r\n'.encode())
                    if self.arrive_during_fetch:
                        self.add(self.arrive_during_fetch)
                        self.arrive_during_fetch = None
                        writer.write(f'* {len(self.messages)} EXISTS\r\n'.encode())
                elif command == b'IDLE':
                    if self.idle_rejected:
                        writer.write(tag + b' NO rejected\r\n')
                        continue
                    if self.arrive_before_idle_ack:
                        self.add(self.arrive_before_idle_ack)
                        self.arrive_before_idle_ack = None
                        writer.write(f'* {len(self.messages)} EXISTS\r\n'.encode())
                    idle_tag = tag
                    writer.write(b'+ idling\r\n')
                    self.idlers.add(writer)
                    self.idling.set()
                    continue
                elif command == b'NOOP':
                    self.nooped.set()
                elif command != b'LOGOUT':
                    raise AssertionError('Unexpected fixture command')
                writer.write(tag + b' OK complete\r\n')
                if command == b'LOGOUT':
                    break
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.idlers.discard(writer)
            self.connections.discard(writer)
            writer.close()
            await writer.wait_closed()
            self.tasks.discard(task)

    async def close(self):
        self.release_login.set()
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.connections):
            writer.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def setup(tmp_path, monkeypatch, *, idle=True):
    server = MailServer(idle=idle)
    port = await server.start()
    client = imap_wait.NotificationClient
    monkeypatch.setattr(imap_wait, 'NotificationClient', lambda host, **kw: client(host, **{**kw, 'ssl': False}))
    store = Store(tmp_path)
    mailbox = store.put('mailboxes', {'host': '127.0.0.1', 'port': port, 'username': 'fixture',
                                     'password': 'fixture', 'folder': 'INBOX', 'max_age_seconds': 300})
    account = {'email': 'buyer@example.test', 'mailbox_id': mailbox['id']}
    return server, store, IdentityService(store), account


@pytest.mark.parametrize('mode', ['immediate', 'push', 'during_fetch', 'before_ack', 'no_idle', 'batch'])
def test_notification_delivery_is_immediate_scoped_and_bounded(tmp_path, monkeypatch, mode):
    async def scenario():
        server, store, service, account = await setup(tmp_path, monkeypatch, idle=mode != 'no_idle')
        job = None
        try:
            since = time.time() - 1
            if mode in ('immediate', 'batch'):
                server.add(mail())
                if mode == 'batch':
                    for _ in range(39):server.add(mail(recipient='other@example.test'))
            elif mode == 'during_fetch':
                server.add(mail(recipient='other@example.test'))
                server.arrive_during_fetch = mail()
            elif mode == 'before_ack':
                server.arrive_before_idle_ack = mail()
            job = asyncio.create_task(service.wait_code(account, since=since, timeout=3))
            if mode == 'push':
                await asyncio.wait_for(server.idling.wait(), 2)
                assert not job.done()
                # Keepalive is not permission to rescan or consume unrelated mail.
                for writer in server.idlers:writer.write(b'* OK still here\r\n')
                server.add(mail())
            elif mode == 'no_idle':
                await asyncio.wait_for(server.nooped.wait(), 2)
                server.add(mail())
            delivered = time.perf_counter()
            result = await asyncio.wait_for(job, 2)
            assert time.perf_counter() - delivered < 1  # Fails the former 3-second sleep.
            assert result['code'] == '123456'
            assert server.logins == 1
            assert b'SELECT' not in server.commands and b'EXAMINE' in server.commands
            assert all(len(batch) <= 40 for batch in server.fetches)
            assert len(server.fetches) == (4 if mode == 'during_fetch' else 2)
            assert len(store.all('otp_receipts')[0]['tokens']) == 1
            assert '123456' not in str(store.all('otp_receipts'))
        finally:
            if job:
                job.cancel();await asyncio.gather(job, return_exceptions=True)
            await server.close();store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['cancel_idle', 'cancel_login', 'deadline', 'disconnect', 'rejected', 'stale', 'used', 'wrong_sender'])
def test_wait_cleanup_never_consumes_an_unusable_code(tmp_path, monkeypatch, mode):
    async def scenario():
        server, store, service, account = await setup(tmp_path, monkeypatch)
        job = None
        try:
            if mode == 'cancel_login':server.release_login.clear()
            if mode in ('stale', 'used', 'wrong_sender'):
                server.add(mail(age=600 if mode == 'stale' else 0,
                                sender='attacker@example.test' if mode == 'wrong_sender' else 'account-update@amazon.com'))
            if mode == 'used':store.put('otp_receipts', {'tokens': ["[b'123']:1"]}, 'otp-' + account['mailbox_id'])
            before = store.all('otp_receipts')
            if mode == 'rejected':server.idle_rejected = True
            job = asyncio.create_task(service.wait_code(account, since=time.time()-1, timeout=.15 if mode == 'deadline' else 3))
            if mode in ('deadline', 'rejected'):
                with pytest.raises((TimeoutError, imap_wait.IMAPClient.Error)):
                    await asyncio.wait_for(job, 2)
            else:
                event = server.login_started if mode == 'cancel_login' else server.idling
                await asyncio.wait_for(event.wait(), 2)
                start = time.perf_counter()
                if mode == 'disconnect':
                    for writer in list(server.connections):writer.close()
                    with pytest.raises((OSError, imap_wait.IMAPClient.Error)):
                        await asyncio.wait_for(job, 1)
                else:
                    job.cancel()
                    with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(job, 1)
                assert time.perf_counter()-start < 1
            assert store.all('otp_receipts') == before
        finally:
            if job:
                job.cancel();await asyncio.gather(job, return_exceptions=True)
            await server.close();store.db.close()
    asyncio.run(scenario())


def test_shared_mailbox_waiters_do_not_block_or_claim_each_others_codes(tmp_path, monkeypatch):
    async def scenario():
        server, store, service, account = await setup(tmp_path, monkeypatch)
        jobs = []
        try:
            jobs.append(asyncio.create_task(service.wait_code(account, since=time.time()-1, timeout=3)))
            await asyncio.wait_for(server.idling.wait(), 2)
            server.add(mail(recipient='second@example.test', code='654321'))
            result = await service.wait_code({**account, 'email': 'second@example.test'}, since=time.time()-1, timeout=2)
            assert result['code'] == '654321' and not jobs[0].done()
            server.add(mail())
            assert (await asyncio.wait_for(jobs[0], 2))['code'] == '123456'
            assert len(store.all('otp_receipts')[0]['tokens']) == 2
        finally:
            for job in jobs:job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await server.close();store.db.close()
    asyncio.run(scenario())


def test_atomic_claim_and_uidvalidity_preserve_existing_receipts(tmp_path, monkeypatch):
    async def scenario():
        server, store, service, account = await setup(tmp_path, monkeypatch)
        jobs = []
        try:
            # UID reuse after a mailbox reset is a different message.
            store.put('otp_receipts', {'tokens': ["[b'122']:1"]}, 'otp-' + account['mailbox_id'])
            server.add(mail())
            reached, release = asyncio.Event(), asyncio.Event()
            calls = 0
            async def before_claim():
                nonlocal calls
                calls += 1
                if calls == 2:reached.set()
                await release.wait()
            jobs = [asyncio.create_task(service.wait_code(account, since=time.time()-1, timeout=3, before_claim=before_claim)) for _ in range(2)]
            await asyncio.wait_for(reached.wait(), 2)
            release.set()
            done, pending = await asyncio.wait(jobs, timeout=2, return_when=asyncio.FIRST_COMPLETED)
            assert len(done) == len(pending) == 1
            assert done.pop().result()['code'] == '123456'
            assert store.all('otp_receipts')[0]['tokens'] == ["[b'122']:1", "[b'123']:1"]
        finally:
            for job in jobs:job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await server.close();store.db.close()
    asyncio.run(scenario())


def test_page_validation_failure_does_not_consume_mail(tmp_path, monkeypatch):
    async def scenario():
        server, store, service, account = await setup(tmp_path, monkeypatch)
        try:
            server.add(mail())
            async def changed():raise ValueError('Verification form changed')
            with pytest.raises(ValueError, match='form changed'):
                await service.wait_code(account, since=time.time()-1, before_claim=changed)
            assert not store.all('otp_receipts')
        finally:
            await server.close();store.db.close()
    asyncio.run(scenario())
