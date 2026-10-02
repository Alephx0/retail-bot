"""Read-only live Amazon/CDP inspection, with an optional non-ordering Buy Now step.

This is an operator probe: it never invokes the final purchase action.
"""
import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from patchright.async_api import async_playwright

from retail.browser_mcp import BrowserTools, AMAZON_ACTIONS
from retail.store import Store


ASIN = 'B07ZLF9WQ5'
DOMAINS = {'www.amazon.com'}


def path(url):
    return urlsplit(url).path


async def observe(page, label):
    print('STEP', label, 'path', path(page.url))
    session = await page.context.new_cdp_session(page)
    try:
        result = await session.send('Runtime.evaluate', {'expression': r'''(() => ({
          title: document.querySelector('#productTitle')?.textContent?.trim().slice(0,100) || '',
          headings: [...document.querySelectorAll('h1,h2,h3,h4')].map(e=>e.textContent.trim().slice(0,80)).filter(e=>/checkout|review|need anything|grocery|cart/i.test(e)).slice(0,12),
          dialogs: [...document.querySelectorAll('[role=dialog],dialog[open],[aria-modal=true]')].filter(e=>e.offsetParent!==null).map(e=>e.getAttribute('aria-label') || e.getAttribute('id') || e.tagName).slice(0,5),
          frames: [...document.querySelectorAll('iframe')].map(e=>{try{return new URL(e.src).pathname}catch{return ''}}).slice(0,12),
          review: !!document.querySelector('#spc-orders,#checkout-item-block,#placeOrder,input[name=placeYourOrder1]'),
          productPrice: [...document.querySelectorAll('#corePrice_feature_div .a-offscreen,#corePriceDisplay_desktop_feature_div .a-offscreen,#priceblock_ourprice')].map(e=>e.textContent.trim()).slice(0,5),
          seller: document.querySelector('#sellerProfileTriggerId,#merchantInfoFeature_feature_div .offer-display-feature-text-message')?.textContent?.trim().slice(0,80) || '',
          quantityChoices: [...document.querySelectorAll('select#quantity option')].map(e=>e.value).slice(0,12),
          prices: [...document.querySelectorAll('tr,li,[data-testid]')].filter(e=>/^(order total|items? \(|shipping|delivery|tax|discount|promotion|fees?)\b/i.test(e.textContent.trim())).map(e=>({tag:e.tagName,id:e.id,text:e.textContent.trim().replace(/\s+/g,' ').slice(0,140)})).slice(0,24),
          summary: [...document.querySelectorAll('#subtotals-marketplace-table tr,#subtotals-marketplace-table li,li')].filter(e=>/^(order total|items? \(|shipping|delivery|estimated tax|tax|discount|promotion|fees?)\b/i.test(e.textContent.trim())).map(e=>({tag:e.tagName,id:e.id,role:e.getAttribute('role'),text:e.textContent.trim().replace(/\s+/g,' ').slice(0,140)})).slice(0,24),
          itemSummary: [...document.querySelectorAll('li,tr')].filter(e=>/^(items?|subtotal|promo|discount)/i.test(e.textContent.trim())).map(e=>({tag:e.tagName,id:e.id,text:e.textContent.trim().replace(/\s+/g,' ').slice(0,150)})).slice(0,20),
          quantityGroups: [...document.querySelectorAll('[role=group][aria-label^="Change quantity of"]')].map(e=>({label:e.getAttribute('aria-label')?.slice(0,100),text:e.textContent.trim().replace(/\s+/g,' ').slice(0,100)})).slice(0,5),
          cartAsins: [...document.querySelectorAll('#sc-active-cart [data-asin]')].map(e=>e.getAttribute('data-asin')),
          storageKeys: Object.keys(localStorage).filter(k=>/cart|checkout/i.test(k)).slice(0,12)
        }))()''', 'returnByValue': True})
        print('DOM', result.get('result', {}).get('value', {}))
        action = {'product':'ADD_TO_CART','cart':'BEGIN_CHECKOUT'}.get(label, 'SUBMIT_ORDER')
        tools = BrowserTools(page, action, DOMAINS, AMAZON_ACTIONS)
        controls = await tools.observe_controls()
        print('CONTROLS', [c for c in controls['controls'] if re.search(r'buy now|add to cart|proceed to checkout|place.*order|purchase|promo|coupon|apply', c['label'], re.I)][:20])
        try:
            ax = await tools.inspect_accessibility()
            print('AX_ACTIONS', [name for name in ax['button_names'] if re.search(r'buy now|checkout|order|purchase', name, re.I)][:20])
        except ValueError:
            print('MCP_PAGE_CHANGED_DURING_OBSERVATION', path(page.url))
        return tools
    finally:
        await session.detach()


async def main():
    store = Store(Path(os.environ.get('RETAIL_DATA', 'data')))
    account = next(a for a in store.all('accounts') if a.get('region') == 'US' and a.get('session'))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=account['session'])
        page = await context.new_page()
        network = []
        session = await context.new_cdp_session(page)
        await session.send('Network.enable')
        session.on('Network.responseReceived', lambda event: network.append((event['response']['status'], path(event['response']['url']), event.get('type', ''))) if urlsplit(event['response']['url']).hostname == 'www.amazon.com' else None)
        try:
            if os.environ.get('RETAIL_PROBE_CLEANUP') == '1':
                from retail.amazon import Amazon
                await page.goto('https://www.amazon.com/gp/cart/view.html', wait_until='domcontentloaded', timeout=45000)
                adapter = Amazon(store)
                active = await adapter.get_cart(page)
                if active != [{'asin': ASIN, 'quantity': 1}]:
                    print('CLEANUP_REFUSED_UNEXPECTED_CART', active)
                    return
                await adapter.save_unrelated_cart_items(page, 'B000000000')
                print('CLEANUP_ACTIVE_CART', await adapter.get_cart(page))
                print('CLEANUP_SAVED_TARGET', await page.locator(f'#sc-saved-cart [data-asin="{ASIN}"]').count())
                return
            if os.environ.get('RETAIL_PROBE_SAVE_ROUNDTRIP') == '1':
                from retail.amazon import Amazon
                await page.goto('https://www.amazon.com/gp/cart/view.html', wait_until='domcontentloaded', timeout=45000)
                adapter = Amazon(store)
                saved = page.locator(f'#sc-saved-cart [data-asin="{ASIN}"]')
                if await adapter.get_cart(page) or await saved.count() != 1:
                    print('ROUNDTRIP_REFUSED_UNEXPECTED_CART')
                    return
                move = saved.locator("input[name^='submit.move-to-cart']:visible")
                if await move.count() != 1 or not await move.is_visible():
                    print('ROUNDTRIP_MOVE_CONTROL_MISSING')
                    print('SAVED_ROW', await saved.evaluate("e => ({tag:e.tagName,id:e.id,classes:e.className,controls:[...e.querySelectorAll('input,button,a')].slice(0,15).map(x=>({tag:x.tagName,name:x.getAttribute('name'),label:x.getAttribute('aria-label') || x.value || x.textContent?.trim().slice(0,60)}))})"))
                    print('SAVED_MOVE_GLOBAL', await page.locator("input[name^='submit.move-to-cart']").evaluate_all("els=>els.slice(0,8).map(e=>({name:e.name,ancestorAsin:e.closest('[data-asin]')?.getAttribute('data-asin')}))"))
                    return
                await move.click()
                await page.reload(wait_until='domcontentloaded')
                try:
                    await page.locator(f'#sc-active-cart [data-asin="{ASIN}"]').wait_for(state='visible', timeout=10000)
                except Exception:
                    print('ROUNDTRIP_MOVE_UNVERIFIED')
                    return
                print('ROUNDTRIP_MOVED_TO_CART', await adapter.get_cart(page))
                await adapter.save_unrelated_cart_items(page, 'B000000000')
                print('ROUNDTRIP_ACTIVE_CART', await adapter.get_cart(page))
                print('ROUNDTRIP_SAVED_TARGET', await page.locator(f'#sc-saved-cart [data-asin="{ASIN}"]').count())
                print('ROUNDTRIP_NETWORK', network[-18:])
                return
            response = await page.goto(f'https://www.amazon.com/dp/{ASIN}', wait_until='domcontentloaded', timeout=45000)
            print('PRODUCT_HTTP', response.status if response else None)
            try:
                await page.locator('#productTitle, #captchacharacters').first.wait_for(state='visible', timeout=12000)
            except Exception:
                print('PRODUCT_STATE_TIMEOUT')
            controls = await observe(page, 'product')
            print('NETWORK', network[-20:])
            if os.environ.get('RETAIL_PROBE_ADAPTER') == '1':
                from retail.amazon import Amazon
                adapter = Amazon(store)
                quantity = int(os.environ.get('RETAIL_PROBE_QUANTITY', '1'))
                try:
                    item = await adapter.inspect(page, {'asin': ASIN, 'offer_id': ''}, 'US')
                    print('ADAPTER_PRODUCT', {key: item.get(key) for key in ('asin', 'price', 'seller', 'condition', 'available')})
                    page._retail_product_condition = item['condition']
                    quantity = await adapter.cart(page, quantity, ASIN)
                    print('ADAPTER_CART', quantity, await adapter.get_cart(page))
                    await adapter.prepare_checkout(page, ASIN, quantity)
                    print('ADAPTER_REVIEW_PATH', path(page.url))
                    snapshot = await adapter.checkout_snapshot(page, ASIN, quantity, 100, max_unit_price=2)
                    print('ADAPTER_QUOTE', snapshot)
                except Exception as exc:
                    print('ADAPTER_FAILED', type(exc).__name__, str(exc), 'path', path(page.url))
                    await observe(page, 'adapter_failure')
                finally:
                    if quantity != 1:
                        try:
                            await page.goto(f'https://www.amazon.com/dp/{ASIN}', wait_until='domcontentloaded', timeout=45000)
                            restored = await adapter.cart(page, 1, ASIN)
                            print('ADAPTER_RESTORED_QUANTITY', restored)
                        except Exception as exc:
                            print('ADAPTER_RESTORE_FAILED', type(exc).__name__, str(exc))
                return
            if os.environ.get('RETAIL_PROBE_EXISTING') == '1':
                await page.goto('https://www.amazon.com/gp/cart/view.html', wait_until='domcontentloaded', timeout=45000)
                try:
                    await page.locator(f'#sc-active-cart [data-asin="{ASIN}"]').wait_for(state='visible', timeout=12000)
                except Exception:
                    print('TARGET_NOT_IN_ACTIVE_CART')
                await observe(page, 'cart')
                cart_title = (await page.locator(f'#sc-active-cart [data-asin="{ASIN}"] .sc-product-title').all_inner_texts())
                print('CART_TITLE_VERIFIED', bool(cart_title and cart_title[0].strip()))
                cart = BrowserTools(page, 'BEGIN_CHECKOUT', DOMAINS, AMAZON_ACTIONS)
                observed = await cart.observe_controls()
                candidates = [c for c in observed['controls'] if re.fullmatch(AMAZON_ACTIONS['BEGIN_CHECKOUT'], c['label'], re.I)]
                if len(candidates) != 1:
                    print('CHECKOUT_CANDIDATES', candidates)
                    return
                verdict = await cart.validate_control(candidates[0]['ref'])
                print('MCP_CHECKOUT_VALIDATION', verdict)
                if not verdict.get('validated'):
                    return
                await cart.chosen.click()
                await page.wait_for_url(re.compile(r'/checkout/'), timeout=15000)
                try:
                    await page.get_by_role('link', name=re.compile('Continue to checkout', re.I)).first.wait_for(state='visible', timeout=15000)
                except Exception:
                    print('CONTINUATION_NOT_VISIBLE')
                await observe(page, 'upsell_hydrated')
                continuation = BrowserTools(page, 'CONTINUE_CHECKOUT', DOMAINS, AMAZON_ACTIONS)
                candidates = await continuation.observe_controls()
                eligible = [c for c in candidates['controls'] if re.fullmatch(AMAZON_ACTIONS['CONTINUE_CHECKOUT'], c['label'], re.I)]
                print('CONTINUE_CANDIDATES', eligible)
                if os.environ.get('RETAIL_PROBE_CONTINUE') == '1' and eligible:
                    if len(set(c['href'] for c in eligible)) != 1:
                        print('CONTINUE_DESTINATIONS_DIFFER; STOPPED')
                        return
                    verdict = await continuation.validate_control(eligible[0]['ref'])
                    print('MCP_CONTINUE_VALIDATION', verdict)
                    if verdict.get('validated'):
                        await continuation.chosen.click()
                        try:
                            await page.locator("#placeOrder, input[name='placeYourOrder1'], #spc-orders, [role=group][aria-label^='Change quantity of']").first.wait_for(state='visible', timeout=20000)
                        except Exception:
                            print('REVIEW_STATE_TIMEOUT')
                        await observe(page, 'after_continue')
                        if os.environ.get('RETAIL_PROBE_SNAPSHOT') == '1':
                            from retail.amazon import Amazon
                            page._retail_cart_title = cart_title[0].splitlines()[0].strip() if cart_title else ''
                            page._retail_cart_asin = ASIN
                            page._retail_product_condition = 'new'
                            try:
                                snapshot = await Amazon(store).checkout_snapshot(page, ASIN, 1, 100, max_unit_price=2)
                                print('AUTOMATION_SNAPSHOT', snapshot)
                            except Exception as exc:
                                print('AUTOMATION_SNAPSHOT_FAILED', type(exc).__name__, str(exc))
                        print('NETWORK_REVIEW', network[-35:])
                print('NETWORK_UPSELL', network[-25:])
                return
            if os.environ.get('RETAIL_PROBE_ADD') == '1':
                controls = BrowserTools(page, 'ADD_TO_CART', DOMAINS, AMAZON_ACTIONS)
                observed = await controls.observe_controls()
                candidates = [c for c in observed['controls'] if re.fullmatch(AMAZON_ACTIONS['ADD_TO_CART'], c['label'], re.I)]
                print('ADD_CANDIDATES', candidates[:5])
                if len(candidates) != 1:
                    return
                verdict = await controls.validate_control(candidates[0]['ref'])
                print('MCP_ADD_VALIDATION', verdict)
                if not verdict.get('validated'):
                    return
                await controls.chosen.click()
                print('AFTER_ADD_PATH', path(page.url))
                await page.goto('https://www.amazon.com/gp/cart/view.html', wait_until='domcontentloaded', timeout=45000)
                try:
                    await page.locator(f'#sc-active-cart [data-asin="{ASIN}"]').wait_for(state='visible', timeout=12000)
                except Exception:
                    print('TARGET_NOT_IN_ACTIVE_CART')
                cart = await observe(page, 'cart')
                print('NETWORK_CART', network[-25:])
                if os.environ.get('RETAIL_PROBE_CHECKOUT') == '1':
                    active = await page.locator('#sc-active-cart [data-asin]').evaluate_all('els=>els.map(e=>e.getAttribute("data-asin"))')
                    if active != [ASIN]:
                        print('CART_NOT_EXACT_TARGET', active)
                        return
                    cart = BrowserTools(page, 'BEGIN_CHECKOUT', DOMAINS, AMAZON_ACTIONS)
                    observed = await cart.observe_controls()
                    candidates = [c for c in observed['controls'] if re.fullmatch(AMAZON_ACTIONS['BEGIN_CHECKOUT'], c['label'], re.I)]
                    print('CHECKOUT_CANDIDATES', candidates[:5])
                    if len(candidates) != 1:
                        return
                    verdict = await cart.validate_control(candidates[0]['ref'])
                    print('MCP_CHECKOUT_VALIDATION', verdict)
                    if not verdict.get('validated'):
                        return
                    await cart.chosen.click()
                    try:
                        await page.wait_for_url(re.compile(r'/checkout/'), timeout=15000)
                    except Exception:
                        print('CHECKOUT_URL_TIMEOUT', path(page.url))
                    await observe(page, 'checkout')
                    print('NETWORK_CHECKOUT', network[-35:])
                return
            if os.environ.get('RETAIL_PROBE_CART') == '1':
                await page.goto('https://www.amazon.com/gp/cart/view.html', wait_until='domcontentloaded', timeout=45000)
                await page.locator('#sc-active-cart, #sc-saved-cart, #nav-cart-count').first.wait_for(state='attached', timeout=15000)
                await observe(page, 'cart')
                print('NETWORK_CART', network[-20:])
                return
            if os.environ.get('RETAIL_PROBE_BUY_NOW') == '1':
                controls = BrowserTools(page, 'BUY_NOW', DOMAINS, AMAZON_ACTIONS)
                observed = await controls.observe_controls()
                candidates = [c for c in observed['controls'] if re.fullmatch(AMAZON_ACTIONS['BUY_NOW'], c['label'], re.I)]
                if len(candidates) != 1:
                    print('BUY_NOW_NOT_UNIQUE', len(candidates))
                    return
                verdict = await controls.validate_control(candidates[0]['ref'])
                print('MCP_VALIDATION', verdict)
                if not verdict.get('validated'):
                    return
                node = controls.chosen
                intent = await node.evaluate("e => ({tag:e.tagName,id:e.id,name:e.getAttribute('name'),type:e.getAttribute('type'),formAction:e.form?.action || '',onclick:e.getAttribute('onclick') || ''})")
                print('BUY_NOW_ATTRIBUTES', {**intent, 'formAction': path(intent['formAction'])})
                if re.search(r'one.click|place.?order|submit.?order', str(intent), re.I):
                    print('BUY_NOW_MAY_PURCHASE_DIRECTLY; STOPPED')
                    return
                await node.click()
                await page.wait_for_load_state('domcontentloaded', timeout=15000)
                await observe(page, 'after_buy_now')
                print('NETWORK_AFTER', network[-30:])
        finally:
            await session.detach()
            await browser.close()
            store.db.close()


if __name__ == '__main__':
    asyncio.run(main())
