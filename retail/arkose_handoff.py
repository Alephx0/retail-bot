"""Capture a fresh Arkose initialization before its single-use blob is consumed.

Only explicitly authorized Amazon signup pages with an assigned CapMonster
solver install this handler. Requests and secrets remain in browser memory.
"""
import asyncio
import re
from urllib.parse import parse_qs, urlsplit


INITIALIZE = re.compile(r'https://amazon-api\.arkoselabs\.com/fc/gt2/public_key/[A-Fa-f0-9-]+/?(?:\?.*)?$')


class ArkoseHandoff:
    def __init__(self, page, domain, timeout):
        self.page, self.domain = page, domain
        self.timeout = min(300, timeout) + 15
        self.selection_lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.release = asyncio.Event()
        self.parameters = None
        self.consumed = False
        self.discard = False

    @classmethod
    async def install(cls, page, domain, solver):
        current = getattr(page, '_retail_arkose_handoff', None)
        if current is not None or solver.get('provider') != 'capmonster':
            return current
        current = cls(page, domain, solver.get('timeout_seconds', 120))
        page._retail_arkose_handoff = current
        page.on('close',lambda *_:current.finish(submitted=True))
        await page.route(INITIALIZE, current.intercept)
        return current

    async def intercept(self, route):
        request = route.request
        origin = urlsplit(self.page.url)
        if (request.method != 'POST'
                or origin.scheme != 'https' or origin.netloc != self.domain
                or origin.path != '/ap/cvf/request'):
            return await route.fallback()
        async with self.selection_lock:
            if self.parameters is not None:
                payload = parse_qs(request.post_data or '')
                if not self.release.is_set() and payload.get('data[blob]') == [self.parameters['blob']]:
                    return await route.abort()
                return await route.fallback()
            # Check the live retailer-owned form, not arbitrary third-party frames.
            from .signup_challenges import CVF_FIELD, CVF_FORM
            field = self.page.locator(CVF_FIELD)
            if await field.count() != 1 or not await field.evaluate(CVF_FORM):
                return await route.fallback()
            if request.frame.page is not self.page:
                return await route.fallback()
            payload = parse_qs(request.post_data or '')
            blobs = payload.get('data[blob]', [])
            key = urlsplit(request.url).path.rstrip('/').rsplit('/', 1)[-1]
            if len(blobs) != 1 or not 0 < len(blobs[0]) <= 65536 or not re.fullmatch(r'[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}', key):
                return await route.fallback()
            self.parameters = dict(key=key, blob=blobs[0], document=await self.page.evaluate('()=>performance.timeOrigin'))
            self.ready.set()
        try:
            await asyncio.wait_for(self.release.wait(), self.timeout)
        except TimeoutError:
            pass
        finally:
            self.consumed = True
            try:
                if self.discard:
                    await route.abort()
                else:
                    # A failed solve leaves the browser's normal puzzle usable.
                    await route.fallback()
            except Exception:
                if not self.page.is_closed():
                    raise

    def finish(self, *, submitted=False):
        self.discard = self.discard or submitted
        self.release.set()

    async def fresh(self, params):
        try:
            await asyncio.wait_for(self.ready.wait(), 10)
        except TimeoutError:
            raise ValueError('Fresh Arkose initialization was not captured. No CapMonster task was requested for an already-loaded puzzle.') from None
        current = self.parameters
        if (self.consumed or self.release.is_set() or current['document'] != params['document']
                or current['key'].lower() != params['key'].lower() or current['blob'] != params['blob']):
            raise ValueError('The Arkose initialization changed or was already consumed. No new paid solve was requested.')
