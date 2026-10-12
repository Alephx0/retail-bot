"""Read offer identity from the active native purchase form, never guess tokens."""

from .amazon_states import PAGE_STATE

ENDPOINT_TIMEOUT_MS = 15000

# Runs in the account browser; neither response scripts nor redirect targets
# are executed. Return bounded evidence, never response HTML or session fields.
ENDPOINT_POST = r'''async (action, entries, args) => {
    const controller = new AbortController();
    globalThis.__retailOfferRequest = controller;
    const timer = setTimeout(() => controller.abort(), args.endpoint_timeout_ms);
    try {
        const fields = entries.map(pair => pair.map(value => value.replace(/\r\n|\r|\n/g, '\r\n')));
        const response = await fetch(action.href, {method:'POST', body:new URLSearchParams(fields),
            credentials:'same-origin', mode:'same-origin', redirect:'manual', cache:'no-store', signal:controller.signal});
        // Fetch hides a manual redirect's URL/status. Do not follow it or replay
        // its POST; the caller may only inspect the existing same-origin cart.
        if (response.type === 'opaqueredirect') return {status:'redirected'};
        const meta = {http_status:response.status, retry_after:response.headers.get('retry-after')};
        if (!response.ok) return {status:'response_error', ...meta};
        if (!(response.headers.get('content-type') || '').toLowerCase().includes('text/html'))
            return {status:'unconfirmed', ...meta};
        const reader = response.body?.getReader();
        if (!reader) return {status:'unconfirmed', ...meta};
        const decoder = new TextDecoder();
        let size = 0, html = '';
        while (true) {
            const chunk = await reader.read();
            if (chunk.done) break;
            size += chunk.value.byteLength;
            if (size > 2 * 1024 * 1024) {
                controller.abort();
                return {status:'unconfirmed', ...meta};
            }
            html += decoder.decode(chunk.value, {stream:true});
        }
        html += decoder.decode();
        // A detached template cannot run scripts or load response subresources.
        const template = document.createElement('template');
        template.innerHTML = html;
        const root = template.content;
        root.querySelectorAll('script,style,noscript,template').forEach(e=>e.remove());
        const body = root.textContent || '';
        if (root.querySelector('#ap_password,#ap_email,#ap_email_login,#auth-mfa-otpcode,#cvf-input-code,#captchacharacters') ||
            /access denied|robot check|verify your identity|verify it's you|additional verification required|enter the characters you see|make sure you(?:'re| are) not a robot/i.test(body))
            return {status:'interrupted', ...meta};
        if ([...root.querySelectorAll('#add-to-cart-error,#attach-error-message,.a-alert-error,[role="alert"]')]
            .some(e => e.textContent.trim())) return {status:'unconfirmed', ...meta};
        const acknowledged = [...root.querySelectorAll('#NATC_SMART_WAGON_CONF_MSG_SUCCESS,#huc-v2-order-row-confirm-text,#attach-added-to-cart-message,#sw-atc-confirmation')]
            .some(e => /added to (?:your )?cart/i.test(e.textContent));
        const targets = [...root.querySelectorAll('#sc-active-cart [data-asin]')]
            .filter(e=>e.getAttribute('data-asin') === args.asin && !e.closest('[data-removed="true"]'));
        const cartTarget = targets.length === 1 && targets[0].getAttribute('data-quantity') === String(args.quantity);
        return {status:acknowledged || cartTarget ? 'acknowledged' : 'unconfirmed', ...meta};
    } catch (_) {
        return {status:'uncertain'};
    } finally {
        clearTimeout(timer);
        if (globalThis.__retailOfferRequest === controller) delete globalThis.__retailOfferRequest;
    }
}'''

OFFER_EVIDENCE = r'''control => {
    const empty = status => ({id:'', asin:'', status});
    if (!control || !control.isConnected || !control.getClientRects().length ||
        getComputedStyle(control).visibility === 'hidden' || control.matches(':disabled') ||
        control.closest('[aria-disabled="true"]')) return empty('unavailable');
    const form = control.form;
    if (!form) return empty('unsupported');
    const destination = new URL(control.getAttribute('formaction') || form.getAttribute('action') || location.href, location.href);
    if (destination.protocol !== 'https:' || destination.origin !== location.origin)
        return empty('unsupported');
    const fields = [...form.elements].filter(e => !e.disabled);
    const values = names => [...new Set(fields.filter(e => names.includes(e.name.toLowerCase()))
        .map(e => e.value).filter(Boolean))];
    const asins = values(['asin', 'asin.1']);
    const offers = values(['offerlistingid','offerlistingid.1','offeringid.1']);
    if (asins.length !== 1 || !/^[A-Z0-9]{10}$/.test(asins[0])) return empty('ambiguous');
    if (!offers.length) return empty('missing');
    if (offers.length !== 1 || offers[0].length > 4096 || /[\s\x00-\x1f\x7f]/.test(offers[0]))
        return empty('ambiguous');
    return {id:offers[0], asin:asins[0], status:'discovered'};
}'''

# Only inspect the already verified target row. Never borrow offer fields from
# recommendations, saved items, unrelated forms, or another product's row.
ROW_OFFER_EVIDENCE = r'''row => {
    const tokens = [];
    for (const key of ['data-offer-id','data-offer-listing-id','data-offering-id']) {
        const value = row.getAttribute(key);
        if (value) tokens.push(value);
    }
    for (const field of row.querySelectorAll('input[name]')) {
        if (field.disabled || field.closest('[data-asin]') !== row) continue;
        if (['offerlistingid','offerlistingid.1','offeringid.1'].includes(field.name.toLowerCase()) && field.value)
            tokens.push(field.value);
    }
    return [...new Set(tokens)];
}'''

# Submit only the observed Amazon ATC form, preserving successful fields and
# submitter semantics. No guessed endpoint, global token, or request replay.
DIRECT_OFFER_FORM = r'''(control, args) => {
    const current = (__OFFER_EVIDENCE__)(control);
    if (location.href !== args.url || current.id !== args.id || current.asin !== args.asin)
        return {status:'changed'};
    const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    if ([...document.querySelectorAll('dialog[open],[aria-modal="true"]')].some(visible))
        return {status:'unsupported', reason:'dialog'};
    const form = control.form;
    const action = new URL(control.getAttribute('formaction') || form.action, location.href);
    const method = (control.getAttribute('formmethod') || form.method).toLowerCase();
    const encoding = control.getAttribute('formenctype') || form.enctype;
    const target = control.getAttribute('formtarget') || form.target;
    if (method !== 'post' || encoding !== 'application/x-www-form-urlencoded' ||
        (target && target !== '_self') || action.origin !== location.origin || action.protocol !== 'https:' ||
        action.username || action.password || !/^\/gp\/product\/handle-buy-box(?:\/|$)/.test(action.pathname) ||
        control.name !== 'submit.add-to-cart' || control.type !== 'submit')
        return {status:'unsupported', reason:'form'};
    if (!HTMLFormElement.prototype.checkValidity.call(form))
        return {status:'unsupported', reason:'validation'};
    // The direct route serializes the requested quantity without triggering
    // dropdown/change handlers. Only an explicitly supported choice is valid.
    let quantityPicker = null;
    if (args.prepare_quantity) {
        const pickers = [...document.querySelectorAll('select#quantity')];
        if (pickers.length === 0 && args.quantity !== 1) return {status:'changed'};
        if (pickers.length) {
            if (pickers.length !== 1 || pickers[0].form !== form || pickers[0].matches(':disabled') ||
                ![...pickers[0].options].some(o => o.value === String(args.quantity) &&
                    !o.disabled && !o.closest('optgroup[disabled]')))
                return {status:'changed'};
            quantityPicker = pickers[0];
            if (quantityPicker.multiple || quantityPicker.name.toLowerCase() !== 'quantity')
                return {status:'unsupported', reason:'quantity'};
        }
    }
    // Build successful fields without firing page formdata/submit handlers.
    // Browser serialization then escapes percent-encoded OIDs exactly once.
    const entries = [];
    for (const field of form.elements) {
        if (!field.name || field.matches(':disabled')) continue;
        // Amazon nests Buy Now-only hidden flags in the same ATC form.
        // They belong to a different action and must never enter this POST.
        if (field.closest('#buyNow_feature_div, #buyNow')) continue;
        if (field.type === 'file') return {status:'unsupported', reason:'file'};
        if (['submit','button','reset','image'].includes(field.type) && field !== control) continue;
        if (['checkbox','radio'].includes(field.type) && !field.checked) continue;
        if (field.tagName === 'SELECT') {
            if (field === quantityPicker) entries.push([field.name,String(args.quantity)]);
            else for (const option of field.selectedOptions)
                if (!option.disabled && !option.closest('optgroup[disabled]')) entries.push([field.name,option.value]);
        } else if (['INPUT','TEXTAREA','BUTTON'].includes(field.tagName)) entries.push([field.name,field.value]);
    }
    const values = names => entries.filter(([name])=>names.includes(name.toLowerCase())).map(([,v])=>v);
    const ids = values(['offerlistingid','offerlistingid.1','offeringid.1','items[0.base][offerlistingid]']);
    const asins = values(['asin','asin.1','items[0.base][asin]']);
    const quantities = values(['quantity','quantity.1','items[0.base][quantity]']);
    const csrf = values(['anti-csrftoken-a2z']);
    if (!csrf.length || csrf.some(v=>!v) || new Set(csrf).size !== 1)
        return {status:'unsupported', reason:'session_fields'};
    if (!ids.length || !asins.length || ids.some(v=>v !== args.id) || asins.some(v=>v !== args.asin) ||
        quantities.some(v=>v !== String(args.quantity)) || (!quantities.length && args.quantity !== 1))
        return {status:'changed'};
    if (values(['isbuynow']).some(v=>!['','0','false'].includes(v.toLowerCase())) ||
        entries.some(([name])=>/^items\[(?!0\.base\])/.test(name)))
        return {status:'unsupported', reason:'multiple_actions'};
    if (form.dataset.retailOfferDispatched === 'true') return {status:'already_sent'};
    if (!args.submit) return {status:'ready'};
    form.dataset.retailOfferDispatched = 'true';
    if (args.endpoint) return (__ENDPOINT_POST__)(action, entries, args);
    const post = document.createElement('form');
    post.method = 'post'; post.action = action.href; post.target = '_self';
    post.enctype = encoding; post.acceptCharset = form.acceptCharset || 'UTF-8';
    post.hidden = true;
    for (const [name,value] of entries) {
        const input = document.createElement('input');
        input.type = 'hidden'; input.name = name; input.value = value; post.append(input);
    }
    document.body.append(post);
    HTMLFormElement.prototype.submit.call(post);
    return {status:'sent'};
}'''.replace('__OFFER_EVIDENCE__', OFFER_EVIDENCE).replace('__ENDPOINT_POST__', ENDPOINT_POST)

# Resolve, snapshot, validate and dispatch in one browser turn. No cached form
# fields or UI dropdown events intervene between the check and the POST.
DIRECT_OFFER_CART = r'''async args => {
    const before = (__PAGE_STATE__)({state:'cart', snapshot:true, asin:args.asin, quantity:args.quantity});
    if (before.error) return {status:'blocked', error:before.error};
    const matches = [...document.querySelectorAll('button,input[type="submit"],input[type="image"],#add-to-cart-button')]
        .filter(control => {
            const label = control.getAttribute('aria-label') || control.value || control.textContent || '';
            if (control.id !== 'add-to-cart-button' && control.name !== 'submit.add-to-cart' &&
                !/^add to (?:shopping )?cart$/i.test(label.trim())) return false;
            const current = (__OFFER_EVIDENCE__)(control);
            return current.id === args.id && current.asin === args.asin;
        });
    if (matches.length !== 1) return {status:'changed'};
    const result = await (__DIRECT_OFFER_FORM__)(matches[0], {...args, prepare_quantity:true});
    return {...result, before};
}'''.replace('__PAGE_STATE__', PAGE_STATE).replace('__OFFER_EVIDENCE__', OFFER_EVIDENCE).replace('__DIRECT_OFFER_FORM__', DIRECT_OFFER_FORM)
