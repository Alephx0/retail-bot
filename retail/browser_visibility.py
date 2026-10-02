"""Show or minimize an existing Chromium window without replacing its context.

Headless Chromium cannot be made headed in place. Desktop tasks therefore use
a real Chromium window, initially minimized, and CDP changes only its window
state. Cookies, tabs, in-flight navigation and the Playwright Page stay intact.
"""


async def set_visible(page, visible):
    session = await page.context.new_cdp_session(page)
    try:
        window = await session.send('Browser.getWindowForTarget')
        window_id = window['windowId']
        bounds = window.get('bounds', {})
        if visible:
            # Chromium requires leaving minimized state before focusing a tab.
            await session.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'windowState': 'normal'}})
            if bounds.get('left', 0) < -1000 or bounds.get('top', 0) < -1000:
                # Background windows start off-screen to avoid a visible flash.
                await session.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'left': 80, 'top': 80}})
            await page.bring_to_front()
        elif bounds.get('windowState') != 'minimized':
            await session.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'windowState': 'minimized'}})
    finally:
        await session.detach()
