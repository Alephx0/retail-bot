import asyncio
import sys

import pytest
from patchright.async_api import async_playwright

from retail.browser_visibility import set_visible


@pytest.mark.skipif(sys.platform != 'win32', reason='Requires an interactive Windows desktop')
def test_same_chromium_page_survives_hide_and_show():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=False, args=['--window-position=-32000,-32000', '--start-minimized'])
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.set_content('<h1>Same session</h1>')
                await page.evaluate("window.sessionMarker = 'preserved'")
                await context.add_cookies([{'name': 'fixture_session', 'value': 'kept', 'url': 'https://www.amazon.com/'}])
                session = await context.new_cdp_session(page)
                try:
                    initial = await session.send('Browser.getWindowForTarget')
                finally:
                    await session.detach()
                assert initial['bounds'].get('left', 0) < -1000, initial['bounds']
                await set_visible(page, False)
                session = await context.new_cdp_session(page)
                try:
                    hidden = await session.send('Browser.getWindowForTarget')
                finally:
                    await session.detach()
                assert hidden['bounds']['windowState'] == 'minimized'
                assert (await page.screenshot(type='jpeg'))[:2] == b'\xff\xd8'
                await set_visible(page, True)
                session = await context.new_cdp_session(page)
                try:
                    shown = await session.send('Browser.getWindowForTarget')
                finally:
                    await session.detach()
                assert shown['windowId'] == hidden['windowId']
                assert shown['bounds']['windowState'] == 'normal'
                assert shown['bounds'].get('left', -32000) >= 0
                assert await page.evaluate('window.sessionMarker') == 'preserved'
                assert any(cookie['name'] == 'fixture_session' and cookie['value'] == 'kept'
                           for cookie in (await context.storage_state())['cookies'])
            finally:
                await browser.close()

    asyncio.run(scenario())
