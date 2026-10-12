"""One read-only connection per active email wait; no background scheduler."""
import asyncio
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from imapclient import IMAPClient


class NotificationClient(IMAPClient):
    def idle(self):
        # IMAP permits unsolicited mailbox updates before the continuation.
        # IMAPClient 3.0.1 expects '+' first. Retain those updates in imaplib
        # while waiting for the acknowledgement, and detect tagged rejection.
        self._idle_tag = self._imap._command('IDLE')
        while self._imap._get_response() is not None:
            if self._imap.tagged_commands.get(self._idle_tag) is not None:
                raise self.Error('Mailbox rejected new-message notifications')


class MailboxSession:
    # Safety deadline for stalled I/O, not a readiness delay.
    IO_TIMEOUT = 15

    def __init__(self, mailbox):
        self.mailbox = mailbox
        self.client = None
        self.stopped = threading.Event()
        self.seen = set()
        self.uidvalidity = None
        self.supports_idle = False
        self.last_scan_seconds = 0
        self.worker = None

    def interrupt(self):
        self.stopped.set()
        if self.client is not None:
            try:
                # Abort only: never reuse this connection after interruption.
                # Wake a blocked read before closing its buffered file object.
                self.client.socket().shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    async def call(self, function, *args):
        def run():
            try:
                self.check_stopped()
                return function(*args)
            finally:
                if self.stopped.is_set() and self.client is not None:
                    self.dispose()
        worker = self.worker = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            self.interrupt()
            # Windows may retain a blocked TLS read until its I/O deadline.
            # Its owner disposes it on exit; it cannot return/consume a code.
            # Do not block browser cancellation on that socket timeout.
            worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            raise

    def check_stopped(self):
        if self.stopped.is_set():
            raise InterruptedError('Mailbox wait stopped')

    def dispose(self):
        try:
            self.client.shutdown()
        except OSError:
            pass

    def open(self):
        m = self.mailbox
        self.client = NotificationClient(m['host'], port=m['port'], ssl=True, timeout=self.IO_TIMEOUT)
        self.check_stopped()
        self.client.normalise_times = False
        self.client.login(m['username'], m['password'])
        self.check_stopped()
        selected = self.client.select_folder(m['folder'], readonly=True)
        self.check_stopped()
        self.uidvalidity = selected[b'UIDVALIDITY']
        self.supports_idle = self.client.has_capability('IDLE')
        self.take_changes()  # EXAMINE's initial EXISTS is not a new arrival.

    def take_changes(self):
        # IMAPClient 3.0.1 retains unsolicited responses received during SEARCH
        # or FETCH in imaplib. Drain just arrival signals so the scan -> IDLE
        # handoff cannot lose a message delivered while a command was running.
        responses = self.client._imap.untagged_responses
        exists, recent = responses.pop('EXISTS', []), responses.pop('RECENT', [])
        return bool(exists or recent)

    def scan(self, since, used):
        start = time.monotonic()
        day = datetime.fromtimestamp(since, timezone.utc) - timedelta(days=1)
        uids = sorted(self.client.search(['SINCE', day.date()]))[-40:]
        self.check_stopped()
        self.seen.intersection_update(uids)
        prefix = str([str(self.uidvalidity).encode()]) + ':'  # Existing receipt format.
        pending = [uid for uid in uids if uid not in self.seen and prefix + str(uid) not in used]
        metadata = self.client.fetch(pending, ['INTERNALDATE']) if pending else {}
        self.check_stopped()
        fresh = [uid for uid in pending if uid in metadata and metadata[uid].get(b'INTERNALDATE')
                 and metadata[uid][b'INTERNALDATE'].timestamp() >= since]
        bodies = self.client.fetch(fresh, ['BODY.PEEK[]']) if fresh else {}
        self.check_stopped()
        self.seen.update(metadata)
        self.last_scan_seconds = time.monotonic() - start
        return [(prefix + str(uid), {**metadata[uid], **bodies[uid]}) for uid in reversed(fresh) if uid in bodies]

    def wait_for_change(self, remaining):
        if self.take_changes():
            return
        if not self.supports_idle:
            # Servers without push need status requests. Pace them by observed
            # command time rather than an arbitrary fixed polling interval.
            self.stopped.wait(min(self.last_scan_seconds, remaining))
            if self.stopped.is_set():
                raise InterruptedError('Mailbox wait stopped')
            self.client.noop()
            return
        self.client.idle()
        if self.take_changes():
            self.client.idle_done()
            return
        deadline = time.monotonic() + remaining
        while not self.stopped.is_set():
            responses = self.client.idle_check(timeout=max(0, deadline - time.monotonic()))
            if not responses or any(len(r) > 1 and r[1] in (b'EXISTS', b'RECENT') for r in responses):
                break
            if any(r and r[0] == b'BYE' for r in responses):
                raise ConnectionError('Mailbox connection closed')
            if time.monotonic() >= deadline:
                break
        if self.stopped.is_set():
            raise InterruptedError('Mailbox wait stopped')
        self.client.idle_done()

    async def close(self):
        if self.stopped.is_set() and self.worker is not None and not self.worker.done():
            return  # The interrupted worker owns cleanup, bounded by IO_TIMEOUT.
        if self.client is not None:
            await asyncio.to_thread(self.dispose)
