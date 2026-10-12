"""Local-only browser check of import, filters, selection and responsive layout."""
import asyncio
from pathlib import Path
import socket
import threading
import time

import uvicorn
from patchright.async_api import async_playwright

from retail.app import create_app


def test_proxy_workspace_browser(tmp_path):
    app=create_app(tmp_path)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started:
        assert time.monotonic()<deadline
        time.sleep(.01)
    async def scenario():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            page=await browser.new_page(viewport={'width':1440,'height':1000})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            await page.goto(f'http://127.0.0.1:{port}')
            await page.get_by_role('button',name='Proxies',exact=True).click()
            await page.locator('#primary').click()
            await page.locator('#modal input[name=name]').fill('Local fixture group')
            await page.locator('#proxy-entries').fill('proxy.example:8080:user:SECRET\nsocks5://second.example:1080\nhttp://user:SECRET@proxy.example:8080')
            await page.locator('#editor button[type=submit]').click()
            await page.get_by_text('2 connections',exact=False).first.wait_for()
            assert 'SECRET' not in await page.locator('#screen').inner_text()
            await page.get_by_role('button',name='Import',exact=True).click()
            await page.locator('#proxy-import-text').fill('third.example:80')
            await page.locator('dialog[open]').get_by_role('button',name='Save',exact=True).click()
            await page.locator('[data-proxy-row]').nth(2).wait_for()
            await page.locator('#proxy-search').fill('third')
            assert await page.locator('[data-proxy-row]').count()==1
            await page.get_by_role('button',name='Select visible').click()
            assert await page.get_by_role('button',name='Test Selected (1)').is_enabled()
            await page.locator('#proxy-search').fill('')
            assert await page.get_by_role('button',name='Test Selected (0)').is_disabled()
            await page.get_by_role('button',name='Latency',exact=True).click()
            folder=Path('artifacts/overhaul');folder.mkdir(parents=True,exist_ok=True)
            await page.screenshot(path=str(folder/'proxies-desktop.png'),full_page=True)
            await page.set_viewport_size({'width':390,'height':844})
            await page.screenshot(path=str(folder/'proxies-narrow.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not errors
            await browser.close()
    try:asyncio.run(scenario())
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()
