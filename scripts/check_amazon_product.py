"""Read-only public product diagnostic. Never logs in, carts, or purchases."""
import asyncio
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patchright.async_api import async_playwright
from retail.amazon import Amazon


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('asin')
    parser.add_argument('--task', help='Use the saved session of this local task for a read-only check')
    args = parser.parse_args()
    options, region = {}, 'US'
    if args.task:
        from retail.store import Store
        store = Store(Path(__file__).resolve().parents[1] / 'data')
        try:
            task = store.get('tasks', args.task)
            account = store.get('accounts', task['account_id'])
            if account.get('session'): options['storage_state'] = account['session']
            region = account['region']
        finally:
            store.db.close()
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True)
        try:
            context = await browser.new_context(**options)
            page = await context.new_page()
            adapter = Amazon(None)
            product = await adapter.inspect(page, {'asin': args.asin}, region)
            print(json.dumps({k: v for k, v in product.items() if k not in ('offer_id', 'image')}))
            print(json.dumps(await page.locator('#merchantInfoFeature_feature_div,#merchant-info,#tabular-buybox').all_inner_texts()))
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
