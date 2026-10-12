"""Local browser fixtures only; the marker must not change input targets."""
import asyncio
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from patchright.async_api import async_playwright, expect

from retail import behavior, visible_mouse


def test_visible_mouse_events_navigation_frames_and_popups():
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto('data:text/html,<button>Existing document</button>')
            await visible_mouse.install(context)
            await visible_mouse.install(context)  # Existing document stays idempotent.
            assert await page.locator('#__retail_visible_mouse').count() == 1
            artifacts = Path('artifacts/visible-mouse')
            artifacts.mkdir(parents=True, exist_ok=True)
            for width in (1000, 390):
                await page.set_viewport_size({'width':width,'height':700})
                await page.goto(url)
                cursor = page.locator('#__retail_visible_mouse .cursor')
                await expect(cursor).to_have_count(1)
                await page.mouse.move(60, 120)
                await expect(cursor).to_be_visible()
                await expect(cursor).to_have_attribute('data-pressed', 'false')
                assert await page.evaluate("document.elementFromPoint(60,120).tagName") == 'BUTTON'
                await page.mouse.down()
                await expect(cursor).to_have_attribute('data-pressed', 'true')
                await page.screenshot(path=str(artifacts/f'pressed-{width}.png'))
                await page.mouse.up()
                await expect(cursor).to_have_attribute('data-pressed', 'false')
                await expect(page.locator('button')).to_have_attribute('data-clicks', '1')
                page.context._retail_paced_input = True
                await behavior.click(page, page.locator('button'))
                await expect(page.locator('button')).to_have_attribute('data-clicks', '2')
                await behavior.fill(page, page.get_by_label('Exact text'), 'Ab+2!')
                await expect(page.get_by_label('Exact text')).to_have_value('Ab+2!')
                await page.screenshot(path=str(artifacts/f'cursor-{width}.png'))
                frame = page.frame_locator('iframe')
                await frame.get_by_role('button').click()
                await expect(frame.locator('#__retail_visible_mouse .cursor')).to_be_visible()
                await page.reload()
                assert await page.locator('#__retail_visible_mouse').count() == 1
            async with page.expect_popup() as popup_info:
                await page.evaluate("url => window.open(url)", url)
            popup = await popup_info.value
            await popup.wait_for_load_state()
            await popup.mouse.move(75, 125)
            await expect(popup.locator('#__retail_visible_mouse .cursor')).to_be_visible()
            await browser.close()
    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'''<body style="margin:32px;background:#172033;color:white">
                <h1>Visible mouse fixture</h1><button style="width:200px;height:70px"
                onclick="this.dataset.clicks=String(Number(this.dataset.clicks||0)+1)">Click target</button>
                <input aria-label="Exact text" style="display:block;margin-top:20px">
                <iframe title="Frame" srcdoc="<button>Frame target</button>" style="margin-top:30px"></iframe>''')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    url = f'http://127.0.0.1:{server.server_port}/'
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

