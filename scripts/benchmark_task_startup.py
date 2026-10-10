"""Local-only startup benchmark; synthetic session, no retailer requests/orders."""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retail.amazon import Amazon
from retail.models import Account
from retail.store import Store
from patchright.async_api import async_playwright


async def benchmark(samples):
    with tempfile.TemporaryDirectory(prefix='retail-startup-bench-') as folder:
        store = Store(Path(folder))
        store.put('settings', {'browser_channel': 'chrome', 'show_browser_window': False,
                              'fingerprint_navigator': True, 'fingerprint_screen': True}, 'settings')
        account = store.put('accounts', Account(name='Benchmark', email='fixture@example.com',
                                                password='fixture-password').model_dump())
        adapter = Amazon(store)
        launches, logins = [], []
        try:
            await adapter.ready()
            await adapter.hardware_profile()
            for _ in range(samples):
                started = time.perf_counter()
                context = await adapter.context(account)
                page = await context.new_page()
                await page.set_content('<h1>Local startup fixture</h1>')
                launches.append(round((time.perf_counter() - started) * 1000, 1))
                await context.close()
                await asyncio.gather(*adapter.profile_close_tasks, return_exceptions=True)
            async with async_playwright() as driver:
                browser = await driver.chromium.launch(headless=True)
                for _ in range(samples):
                    page = await browser.new_page()
                    async def local(route):
                        if '/password' in route.request.url:
                            body = '<form action="/verified"><input id="ap_password" type="password"><button id="signInSubmit">Sign in</button></form>'
                        elif '/verified' in route.request.url:
                            body = '<div id="nav-link-accountList"><span class="nav-line-1" id="signed-in">Hello, Fixture</span></div>'
                        else:
                            body = '<form action="/password"><input id="ap_email"><button id="continue">Continue</button></form>'
                        await route.fulfill(body=body, content_type='text/html')
                    await page.route('**/*', local)
                    await page.goto('https://www.amazon.com/ap/signin')
                    started = time.perf_counter()
                    await adapter.authenticate(page, account)
                    assert await page.locator('#signed-in').count() == 1
                    logins.append(round((time.perf_counter() - started) * 1000, 1))
                    await page.close()
                await browser.close()
        finally:
            await adapter.close()
            store.db.close()
    return {name: {'samples_ms': values, 'median_ms': statistics.median(values)}
            for name, values in [('warmed_browser_page', launches), ('local_email_password_login', logins)]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.samples <= 20:
        parser.error('samples must be between 1 and 20')
    print(json.dumps(asyncio.run(benchmark(args.samples)), indent=2))
