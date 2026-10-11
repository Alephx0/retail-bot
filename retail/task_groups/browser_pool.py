"""Bounded reusable account sessions; checkout ownership is held in the ledger."""
import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager

from .repository import Conflict


class BrowserPool:
    def __init__(self, engine, repository):
        self.engine, self.repo = engine, repository
        self.sessions = {}
        self.lock = asyncio.Lock()

    async def _close_entry(self, entry):
        if entry:
            try:
                await entry['context'].close()
                owner = getattr(entry['context'], '_retail_close_task', None)
                if owner:
                    await asyncio.gather(owner, return_exceptions=True)
            finally:
                self.engine.browser_slots.release()

    @asynccontextmanager
    async def lease(self, account, attempt_id='', *, headless=None, probe_only=False):
        account_id=account['id']
        identity={k:account.get(k) for k in ('region','retailer','email','password','proxy','proxy_list_id','fingerprint_values','fingerprint_overrides','fingerprint_seed')}
        settings=self.engine.store.get('settings','settings') or {}
        window_headless=headless if headless is not None else not settings.get('show_browser_window',True)
        profile_key=hashlib.sha256(json.dumps({'account':identity,'settings':settings,'headless':window_headless},sort_keys=True).encode()).hexdigest()
        if account_id in self.engine.amazon.logins or account_id in self.engine.amazon.fingerprint_tests:
            raise Conflict('Close this account browser before using the group')
        if any((self.engine.store.get('tasks',id) or {}).get('account_id')==account_id for id in self.engine.jobs):
            raise Conflict('Account is busy in a legacy task')
        claim=self.repo.claimed(account_id)
        if claim and claim!=attempt_id:
            raise Conflict('Account is reserved by another attempt')
        lock=self.engine.account_locks.setdefault(account_id,asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(),timeout=.05)
        except TimeoutError:
            raise Conflict('Account browser is busy') from None
        entry=None
        try:
            claim=self.repo.claimed(account_id)
            if claim and claim!=attempt_id:
                raise Conflict('Account is reserved by another attempt')
            if probe_only:
                yield None
                return
            stale=None
            async with self.lock:
                entry=self.sessions.get(account_id)
                if entry and (entry['page'].is_closed() or entry['profile_key']!=profile_key):
                    stale=self.sessions.pop(account_id); entry=None
                if entry:
                    entry['busy']=True
            await self._close_entry(stale)
            if not entry:
                evicted=None
                async with self.lock:
                    if self.engine.browser_slots.locked():
                        idle=[(k,v) for k,v in self.sessions.items() if not v['busy'] and not self.repo.claimed(k)]
                        if idle:
                            evicted=self.sessions.pop(min(idle,key=lambda p:p[1]['used'])[0])
                await self._close_entry(evicted)
                # No browser I/O under the pool lock. The account lock owns this creation.
                if self.engine.browser_slots.locked():
                    raise Conflict('Waiting for a browser slot')
                await self.engine.browser_slots.acquire()
                context=None
                try:
                    context=await self.engine.amazon.context(account, **({'headless': headless} if headless is not None else {}))
                    page=await context.new_page()
                    entry={'context':context,'page':page,'busy':True,'used':time.monotonic(),'profile_key':profile_key}
                    self.sessions[account_id]=entry
                except BaseException:
                    try:
                        if context:
                            await context.close()
                    finally:
                        self.engine.browser_slots.release()
                    raise
            yield entry
        finally:
            if entry:
                entry['busy']=False; entry['used']=time.monotonic()
            lock.release()

    async def trim(self, all_idle=False):
        async with self.lock:
            entries=[self.sessions.pop(key) for key,entry in list(self.sessions.items())
                     if not entry['busy'] and not self.repo.claimed(key)
                     and (all_idle or time.monotonic()-entry['used']>60)]
        await asyncio.gather(*(self._close_entry(entry) for entry in entries))

    async def discard(self, account_id):
        async with self.lock:
            if self.sessions.get(account_id,{}).get('busy'):
                raise Conflict('Account browser operation is still running')
            entry=self.sessions.pop(account_id,None)
        await self._close_entry(entry)

    async def close(self):
        async with self.lock:
            entries=list(self.sessions.values()); self.sessions.clear()
        await asyncio.gather(*(self._close_entry(entry) for entry in entries))
