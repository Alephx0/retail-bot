"""Allowlisted product exploration for the account-owned browsing page."""
import asyncio
import json
import random
from urllib.parse import urlparse
from patchright.async_api import TimeoutError as BrowserTimeoutError

from . import behavior
from .performance import stage


class ProductInteractionError(ValueError):
    pass


CONTROLS = ','.join((
    '[id^="variation_"] li[data-asin]', '[id^="variation_"] li[data-defaultasin]',
    '[id^="variation_"] select',
    '#twister li[data-asin]', '#twister_feature_div li[data-asin]',
    '#twister-plus-inline-twister li[data-asin]', '#seeMoreDetailsLink',
    '#productOverview_feature_div .a-expander-header', '#feature-bullets .a-expander-header',
    '#productDetails_feature_div .a-expander-header', '#productDescription .a-expander-header',
    '#customerReviews', '#reviewsMedley', '#cm-cr-dp-review-list',
))


VARIANT_SELECTED = r'''e => {
    const visible=n=>n.getClientRects().length && getComputedStyle(n).visibility!=='hidden';
    return [e,...e.querySelectorAll('.a-button-selected,[aria-checked="true"],[aria-selected="true"],input[type="radio"]')]
        .some(n=>visible(n) && (/(?:^|\s)(?:swatchSelect|a-button-selected)(?:\s|$)/.test(n.className)
            || n.getAttribute('aria-checked')==='true' || n.getAttribute('aria-selected')==='true'
            || n.matches('input[type="radio"]') && n.checked));
}'''

VARIANT_STATE = r'''asin => {
    const current=document.querySelector('input#ASIN')?.value?.toUpperCase();
    const path=location.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})(?:[/?#]|$)/i)?.[1]?.toUpperCase();
    const selected=[...document.querySelectorAll('[data-asin],[data-defaultasin]')].some(e =>
        (e.dataset.asin || e.dataset.defaultasin)?.toUpperCase()===asin && __SELECTED__(e));
    return {asin_matches:current===asin, url_matches:path===asin, selected_control:selected};
}'''.replace('__SELECTED__', '('+VARIANT_SELECTED+')')

# Used for observation AND again immediately before input. No model-provided
# selectors, URLs or scripts are accepted. Metadata excludes account/reviewer data.
DESCRIBE = r'''(e, choice=null) => {
    if (!e.isConnected || !e.getClientRects().length || e.closest('[hidden],[inert]')) return null;
    const visible = getComputedStyle(e);
    if (visible.visibility==='hidden' || visible.display==='none') return null;
    if (e.matches('#customerReviews,#reviewsMedley,#cm-cr-dp-review-list'))
        return {kind:'reviews', key:'reviews', label:'Read customer reviews', id:e.id};
    if (e.matches('select')) {
        if (e.disabled || e.multiple || e.size>1) return null;
        const choices=[...e.options].map((o,index)=>({value:o.value,index,
            asin:o.dataset.asin || o.value.match(/(?:^|,)([A-Z0-9]{10})$/i)?.[1],
            label:o.label.trim().slice(0,120),disabled:o.disabled || o.parentElement.disabled}))
            .filter(o=>o.asin && /^[A-Z0-9]{10}$/i.test(o.asin) && !o.disabled && o.index!==e.selectedIndex && o.index<30);
        if(choice===null) return {kind:'variant-list', choices};
        const option=choices.find(o=>o.value===choice);
        if(!option) return null;
        return {kind:'variant', key:'variant:'+option.asin.toUpperCase(), label:option.label,
            asin:option.asin.toUpperCase(), select:true, value:option.value, index:option.index,
            steps:[...e.options].filter(o=>!o.disabled && !o.parentElement.disabled).findIndex(o=>o.value===choice)};
    }
    const li=e.matches('li') ? e : null;
    const a=e.matches('a') ? e : e.querySelector('a');
    const href=a?.getAttribute('href') || '';
    if (a && ((a.target && a.target!=='_self') || a.hasAttribute('download'))) return null;
    if (e.matches('button,input') && e.type!=='button' && e.form) return null;
    if (li) {
        const asin=li.dataset.asin || li.dataset.defaultasin || '';
        if (!/^[A-Z0-9]{10}$/i.test(asin) || /swatchUnavailable|swatchDisabled/i.test(li.className)
            || __SELECTED__(li) || li.matches('[aria-disabled="true"]')
            || li.querySelector('[disabled],[aria-disabled="true"]')) return null;
        const url=li.dataset.dpUrl || '';
        for(const candidate of [url,href]) {
            if (!candidate || candidate==='#' || candidate==='javascript:void(0)' || candidate==='javascript:void(0);') continue;
            const u=new URL(candidate,location.href);
            const matched=u.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})(?:[/?#]|$)/i);
            if(u.origin!==location.origin || u.username || u.password || matched?.[1].toUpperCase()!==asin.toUpperCase()) return null;
        }
        if ([...li.querySelectorAll('button,input')].some(b=>b.form && b.type==='submit')) return null;
        const label=(li.getAttribute('title') || li.querySelector('img')?.alt || li.innerText || 'Product variant').trim().slice(0,120);
        return {kind:'variant', key:'variant:'+asin.toUpperCase(), label, asin:asin.toUpperCase(), href, url};
    }
    if(e.id==='seeMoreDetailsLink') {
        const u=new URL(href,location.href);
        if(u.origin!==location.origin || u.pathname!==location.pathname || u.search!==location.search
            || !/^#(?:productDetails[\w-]*|detailBullets[\w-]*|productOverview[\w-]*|technicalSpecifications[\w-]*)$/.test(u.hash)) return null;
        const section=document.getElementById(u.hash.slice(1));
        if(!section || !section.getClientRects().length) return null;
        return {kind:'details', key:'details', label:'See more product details', href, section:section.id};
    }
    const container=e.closest('.a-expander-container');
    const content=container?.querySelector('.a-expander-content');
    const expanded=e.getAttribute('aria-expanded') || content?.getAttribute('aria-expanded');
    if (!content || expanded==='true' || (expanded!=='false'
        && !container.classList.contains('a-expander-partial-collapse-container')
        && getComputedStyle(content).display!=='none' && content.getClientRects().length)) return null;
    if(href && href!=='#' && href!=='javascript:void(0)' && href!=='javascript:void(0);') return null;
    return {kind:'details', key:'details', label:'Expand product details', href, expand:true};
}'''.replace('__SELECTED__', '('+VARIANT_SELECTED+')')


class ProductExplorer:
    limit = 4

    def __init__(self, agent):
        self.agent = agent
        self.page = agent.page
        self.nodes = {}
        self.used = set()
        self.count = 0
        self.variant_count = 0

    async def clear(self, *, reset=False):
        nodes, self.nodes = self.nodes, {}
        for target, *_ in nodes.values():
            await target.dispose()
        if reset:
            self.used.clear()
            self.count = self.variant_count = 0

    async def observe(self):
        await self.clear()
        if self.count >= self.limit:
            return []
        expression = f'''JSON.stringify(Array.from(document.querySelectorAll({json.dumps(CONTROLS)}))
            .flatMap((e,index)=>{{const meta=({DESCRIBE})(e);return !meta?[]:meta.kind==='variant-list'
                ? meta.choices.map(o=>({{index,meta:({DESCRIBE})(e,o.value)}})) : [{{index,meta}}];}}).filter(r=>r.meta)
            .sort((a,b)=>(a.meta.kind==='variant')-(b.meta.kind==='variant')).slice(0,60))'''
        result = await self.agent.cdp.send('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
        if result.get('exceptionDetails'):
            raise ProductInteractionError('Could not inspect product options on this page.')
        public, seen = [], set()
        for row in json.loads(result['result']['value']):
            meta = row['meta']
            if meta['key'] in self.used or meta['key'] in seen or (meta['kind']=='variant' and self.variant_count>=2):
                continue
            # Reserve slots for details/reviews even when a product has many colors.
            if meta['kind']=='variant' and sum(r['kind']=='variant' for r in public)>=8:
                continue
            target = await self.page.locator(CONTROLS).nth(row['index']).element_handle()
            if target is None:
                continue
            if await target.evaluate(DESCRIBE, meta.get('value')) != meta:
                await target.dispose()
                continue
            seen.add(meta['key'])
            ref = 'i'+str(len(public)+1)
            self.nodes[ref] = (target, meta, self.page.url)
            public.append({'ref': ref, 'kind': meta['kind'], 'label': meta['label']})
        return public

    async def scroll_to(self, target):
        paced = behavior.controller(self.page)
        if paced is None:
            raise ProductInteractionError('Product exploration requires paced input.')
        async with asyncio.timeout(30):
            for _ in range(40):
                await self.agent.guard()
                box = await target.bounding_box()
                height = (self.page.viewport_size or await self.page.evaluate('() => ({height:innerHeight})'))['height']
                if not box:
                    raise ProductInteractionError('The selected product section disappeared.')
                if 0 <= box['y'] <= height*.75 or box['y'] < 0 and box['y']+box['height'] > 120:
                    return
                await paced.scroll(max(-900, min(900, box['y']-height*.3)))
        raise ProductInteractionError('Could not scroll to the selected product section.')

    async def read_pause(self, seconds):
        from .care_reading import read_with_pointer
        await read_with_pointer(self.page, seconds, self.agent.guard)

    async def select_variant(self, target, meta):
        # Open the native menu, move its highlight and commit once. No DOM value
        # assignment or synthetic change event; all input uses real paced keys.
        paced = behavior.controller(self.page)
        await behavior.click(self.page, target, timeout=10000)
        for key in ['Home', *(['ArrowDown']*meta['steps']), 'Enter']:
            await target.press(key, delay=paced.rng.uniform(35, 90)*paced.duration_scale, timeout=3000)
            await asyncio.sleep(paced.rng.uniform(.04, .12)*paced.duration_scale)

    async def variant_click_target(self, target):
        # Modern inline twisters place a transparent radio input above the text.
        # Clicking the covered span times out on actionability, even though the
        # swatch looks visible. Prefer the actual visible interactive control.
        for selector in ('input[role="radio"],input[type="radio"]', '[role="radio"]', 'button', 'a', '.a-button-text'):
            for candidate in await target.query_selector_all(selector):
                if await candidate.is_visible() and await candidate.is_enabled():
                    return candidate
        return target

    async def interact(self, ref):
        await self.agent.guard()
        if ref not in self.nodes or self.count >= self.limit:
            raise ProductInteractionError('Choose a currently available product interaction.')
        target, meta, origin = self.nodes[ref]
        if self.page.url != origin or await target.evaluate(DESCRIBE, meta.get('value')) != meta:
            raise ProductInteractionError('The selected product option changed. Start again to refresh its choices.')
        self.agent.action_stage = 'scrolling to '+meta['label']
        self.agent.care.phase(self.agent.account_id, 'acting', meta['label']+' with paced input')
        await self.scroll_to(target)
        # Scrolling can trigger lazy replacement of controls. Recheck before clicking.
        if self.page.url != origin or await target.evaluate(DESCRIBE, meta.get('value')) != meta:
            raise ProductInteractionError('The selected product option changed while scrolling.')
        self.used.add(meta['key'])
        self.count += 1
        trace = {'kind': meta['kind'], 'label': meta['label'], 'phase': 'click', 'click_completed': False}
        responses = []
        def response_seen(response):
            if (response.frame == self.page.main_frame and urlparse(response.url).netloc == self.agent.domain
                    and response.request.resource_type in {'document', 'xhr', 'fetch'}):
                responses.append(response)
        self.page.on('response', response_seen)
        try:
            if meta['kind'] == 'variant':
                self.variant_count += 1
                self.agent.action_stage = 'clicking the '+meta['label']+' variant'
                with stage('Product option click'):
                    if meta.get('select'):
                        await self.select_variant(target, meta)
                    else:
                        clickable = await self.variant_click_target(target)
                        trace['control'] = await clickable.evaluate("e => ({tag:e.tagName,role:e.getAttribute('role')||'',type:e.type||''})")
                        await behavior.click(self.page, clickable, timeout=10000)
                trace.update(phase='selection', click_completed=True)
                self.agent.action_stage = 'waiting for the '+meta['label']+' variant to become selected'
                # Variants may navigate OR update the current document via AJAX.
                with stage('Product option selection readiness'):
                    await self.page.wait_for_function('asin => Object.values(('+VARIANT_STATE+')(asin)).some(Boolean)', arg=meta['asin'], timeout=12000)
                    trace['selection'] = await self.page.evaluate(VARIANT_STATE, meta['asin'])
                    trace['phase'] = 'document'
                    self.agent.action_stage = 'waiting for the selected product document'
                    await self.page.wait_for_load_state('domcontentloaded', timeout=12000)
                self.agent.visited.add(meta['asin'])
            elif meta['kind'] == 'details':
                self.agent.action_stage = 'opening '+meta['label']
                before = await target.evaluate('''e => {
                    const c=e.closest('.a-expander-container')?.querySelector('.a-expander-content');
                    return {visible:!!c?.getClientRects().length && getComputedStyle(c).display!=='none',height:c?.getBoundingClientRect().height||0};
                }''') if meta.get('expand') else None
                await behavior.click(self.page, target, timeout=10000)
                trace.update(phase='details', click_completed=True)
                if meta.get('expand'):
                    await target.wait_for_element_state('visible', timeout=5000)
                    await self.page.wait_for_function('''({e,before}) => {
                        const c=e.closest('.a-expander-container')?.querySelector('.a-expander-content');
                        return c && c.getClientRects().length && getComputedStyle(c).display!=='none'
                            && (!before.visible || e.getAttribute('aria-expanded')==='true' || c.getAttribute('aria-expanded')==='true'
                                || c.getBoundingClientRect().height > before.height+1);
                    }''', arg={'e': target, 'before': before}, timeout=8000)
                else:
                    await self.scroll_to(self.page.locator('[id='+json.dumps(meta['section'])+']'))
            for response in responses:
                self.agent.care.engine.amazon._raise_for_response(response)
            await self.agent.guard()
            seconds = round(random.uniform(3, 8), 2)
            trace['phase'] = 'reading'
            self.agent.action_stage = 'reading '+meta['label']
            self.agent.care.phase(self.agent.account_id, 'browsing', f"{meta['label']} / reading for about {seconds:.0f}s")
            # Stay at the inspected section; this is an intentional, cancellable
            # reading pause after readiness, not a substitute for observing it.
            await self.read_pause(seconds)
            for response in responses:
                self.agent.care.engine.amazon._raise_for_response(response)
            await self.agent.guard()
            result = {'inspected': meta['kind'], 'label': ('Variant: ' if meta['kind']=='variant' else '')+meta['label'], 'reading_seconds': seconds}
            if meta['kind'] == 'details':
                if meta.get('expand'):
                    result['product_details'] = await target.evaluate("e => e.closest('.a-expander-container').querySelector('.a-expander-content').innerText.slice(0,1600)")
                else:
                    result['product_details'] = await self.page.locator('[id='+json.dumps(meta['section'])+']').inner_text(timeout=3000)
                    result['product_details'] = result['product_details'][:1600]
            elif meta['kind'] == 'reviews':
                result['review_summary'] = await self.page.evaluate("() => ({rating:document.querySelector('#acrPopover')?.getAttribute('title')?.slice(0,80)||'',count:document.querySelector('#acrCustomerReviewText')?.innerText.slice(0,100)||''})")
            self.agent.care.update(self.agent.account_id, last_interaction={**trace, 'phase': 'completed'})
            return result
        except (BrowserTimeoutError, TimeoutError):
            if meta['kind']=='variant':
                try:
                    async with asyncio.timeout(1):
                        trace['selection'] = await self.page.evaluate(VARIANT_STATE, meta['asin'])
                except Exception:
                    pass
            self.agent.care.update(self.agent.account_id, last_interaction={**trace, 'outcome': 'timeout'})
            # A denied navigation may never show the selected variant. Preserve
            # the retailer response/cooldown rather than relabeling it a timeout.
            for response in responses:
                self.agent.care.engine.amazon._raise_for_response(response)
            await self.agent.guard()
            raise
        finally:
            self.page.remove_listener('response', response_seen)
