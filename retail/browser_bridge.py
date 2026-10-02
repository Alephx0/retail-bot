"""Optional CDP inspection of a user-selected, local Chromium session."""
from urllib.parse import urlparse
from patchright.async_api import async_playwright

def validate_endpoint(endpoint):
    url=urlparse(endpoint)
    if url.scheme not in ('http','https') or url.hostname not in ('127.0.0.1','localhost','::1') or url.username or url.password:
        raise ValueError('Use a local Chromium debugging HTTP endpoint')


async def inspect_session(endpoint):
    validate_endpoint(endpoint)
    async with async_playwright() as driver:
        browser=await driver.chromium.connect_over_cdp(endpoint,timeout=10000)
        # Disconnect through Playwright teardown. Do not close the user's browser.
        return {'connected':True,'contexts':len(browser.contexts),'pages':sum(len(c.pages) for c in browser.contexts),'version':browser.version}
