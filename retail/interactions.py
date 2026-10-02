"""Semantic actions with strict uniqueness and stable-attribute fallbacks."""
import re


class InteractionError(ValueError):
    pass


ACTIONS={
    'ADD_TO_CART':('button',r'^add to (?:shopping )?cart$',('#add-to-cart-button',"input[name='submit.add-to-cart']")),
    'BUY_NOW':('button',r'^(?:buy(?: it)? now|purchase(?: now)?|get it now)$',('#buy-now-button',"input[name='submit.buy-now']")),
    'BEGIN_CHECKOUT':('button',r'^proceed to checkout(?:.*)$',("input[name='proceedToRetailCheckout']",'#sc-buy-box-ptc-button input')),
    'CONTINUE_CHECKOUT':('button',r'^continue to checkout$',()),
    'DISMISS_CHECKOUT_OFFER':('button',r'^(?:no thanks|no, thanks|not now|skip this offer)$',()),
    'SUBMIT_ORDER':('button',r'^(?:place (?:your )?order|confirm (?:and place |your )?order|complete purchase|submit order|purchase)(?:.*)$',("input[name='placeYourOrder1']",'#submitOrderButtonId input')),
}


async def resolve(page, action):
    page._retail_expected_action=action
    role,label,fallbacks=ACTIONS[action]
    if action == 'DISMISS_CHECKOUT_OFFER':
        page = page.locator('[role=dialog],dialog[open],[aria-modal=true]')
    candidates=[page.get_by_role(role,name=re.compile(label,re.I)),page.get_by_label(re.compile(label,re.I))]+[page.locator(css) for css in fallbacks]
    # Amazon's /checkout/byg interstitial renders the continuation as an
    # anchor, not a button. Resolve that known safe transition locally before
    # asking the model; this avoids an unnecessary model failure on a simple
    # semantic variation.
    if action == 'CONTINUE_CHECKOUT':
        candidates.insert(0, page.get_by_role('link', name=re.compile(label, re.I)))
        candidates.append(page.locator("a[href*='checkout']"))
    if action == 'CONTINUE_CHECKOUT':
        # Amazon sometimes renders the same CTA twice in the interstitial
        # (top and bottom). Treat identical visible links as one control, but
        # keep genuinely different destinations ambiguous.
        grouped = {}
        for locator in candidates:
            for node in await locator.all():
                if not await node.is_visible() or not await node.is_enabled():
                    continue
                text = (await node.inner_text()).strip()
                if not re.fullmatch(label, text, re.I):
                    continue
                href = await node.evaluate("e => e.closest('a[href]')?.href || ''")
                key = (text.casefold(), href.split('?', 1)[0])
                grouped.setdefault(key, node)
        if len(grouped) == 1:
            page._retail_previous_locator = 'deduplicated checkout continuation link'
            return next(iter(grouped.values()))
        if len(grouped) > 1:
                # Recovery mode may let the bounded MCP agent choose between
                # distinct same-site checkout continuations using their
                # sanitized destinations. Do not make this an automatic stop.
                raise InteractionError('CONTINUE_CHECKOUT: multiple eligible controls; agent disambiguation required')
    for locator in candidates:
        visible=[]
        for node in await locator.all():
            if await node.is_visible() and await node.is_enabled(): visible.append(node)
        if len(visible)>1:
            if action == 'SUBMIT_ORDER':
                signatures = [await node.evaluate("e => ({tag:e.tagName, type:e.getAttribute('type') || '', name:e.getAttribute('name') || '', label:(e.getAttribute('aria-label') || (e.matches('input') ? e.value : e.innerText) || '').trim()})") for node in visible]
                if all(signature == signatures[0] for signature in signatures):
                    page._retail_previous_locator = 'equivalent order submission controls'
                    return visible[0]
            raise InteractionError(f'{action}: multiple eligible controls; manual review required')
        if len(visible)==1:
            page._retail_previous_locator=str(locator)
            return visible[0]
    raise InteractionError(f'{action}: expected control was not found')
