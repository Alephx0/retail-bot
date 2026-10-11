"""Read-only browser predicates for Amazon transitions; no timed settle sleeps."""

PAGE_STATE = r"""args => {
    const visible = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
    const nodes = selector => [...document.querySelectorAll(selector)].filter(visible);
    const text = e => (e?.innerText || '').replace(/\s+/g, ' ').trim();
    const body = document.body?.innerText || '';
    const label = e => e.getAttribute('aria-label') || e.value || text(e);
    // Ignore unrelated recommendation/ad loaders. Only purchasing regions can
    // block an otherwise verified cart or checkout state.
    const regions = '#sc-active-cart, #spc-orders, #checkout-item-block, #subtotals-marketplace-table, #attach-added-to-cart-message, #sw-atc-confirmation, main';
    const busy = nodes(regions).some(root => root.getAttribute('aria-busy') === 'true' ||
        [...root.querySelectorAll('[aria-busy="true"], .a-spinner-wrapper, .a-spinner')].some(visible));
    if (!['www.amazon.com', 'amazon.com', 'www.amazon.co.uk', 'www.amazon.ca'].includes(location.hostname))
        return {error: 'Checkout left the permitted retailer; review the browser'};
    if (nodes('#captchacharacters, #ap_password, #ap_email, #ap_email_login, #auth-mfa-otpcode, #cvf-input-code').length ||
        /access denied|robot check|verify your identity|verify it's you|additional verification required|click the button below to continue shopping|enter the characters you see|make sure you(?:'re| are) not a robot/i.test(body))
        return {error: 'Amazon interrupted the operation; review the task browser',
            continuation: /click the button below to continue shopping/i.test(body)};
    const errors = nodes('#auth-error-message-box, #auth-warning-message-box, #add-to-cart-error, #attach-error-message, .a-alert-error')
        .some(e => text(e)) || nodes('[role="alert"]').some(e => /error|unable|failed|declined|invalid|try again/i.test(text(e)));
    if (errors) return {error: 'Amazon reported a problem with this operation; review the task browser'};

    const order = body.match(/\b\d{3}-\d{7}-\d{7}\b/);
    const success = /order placed|order has been placed|order confirmed|thank you, your order/i.test(body) ||
        nodes('h1,h2,h3,h4,h5,h6,[role="heading"]').some(e => /order placed|order confirmed|thank you/i.test(text(e)));
    const confirmed = order && success ? {order: order[0]} : false;
    const cvvSelector = "input[name='cvv'], input[name='CVV'], input[name='addCreditCardVerificationNumber']";
    const payment = nodes(cvvSelector + ', iframe[src*="3ds"]').length > 0 ||
        /payment verification required|verify your payment|verify your card|approve this payment|payment revision needed/i.test(body);
    if (args.state === 'confirmation' || args.state === 'submission')
        return confirmed || (payment ? {payment: true} : false);

    if (args.state === 'cart' || args.state === 'quantity' || args.state === 'cart_items' || args.state === 'cart_removed') {
        const selector = document.querySelector('#sc-active-cart') ? '#sc-active-cart [data-asin]' :
            '[data-asin][data-quantity]:not(#sc-saved-cart *):not(#sc-saved-cart-items *)';
        const lines = nodes(selector);
        const matching = lines.filter(e => e.getAttribute('data-asin') === args.asin);
        const quantity = e => e.getAttribute('data-quantity') ?? e.querySelector("select[name='quantity']")?.value;
        if (args.state === 'cart_removed') return !busy && !!document.querySelector('#sc-active-cart') && matching.length === 0;
        if (args.state === 'cart_items') return !busy && matching.length > 0 && matching.every(e => /^\d+$/.test(quantity(e) || ''));
        const valid = matching.length === 1 && quantity(matching[0]) === String(args.quantity);
        if (args.state === 'quantity') return !busy && lines.length === 1 && valid;
        const count = text(document.querySelector('#nav-cart-count'));
        const added = nodes('#NATC_SMART_WAGON_CONF_MSG_SUCCESS, #huc-v2-order-row-confirm-text, #attach-added-to-cart-message, #sw-atc-confirmation')
            .map(text).filter(t => /added to (?:your )?cart/i.test(t)).join('|');
        const current = {count, added};
        if (args.snapshot) return current;
        const before = args.before;
        const cartPage = /^\/gp\/cart\/(?:view(?:\.html)?|desktop\/go-to-cart\.html)\/?$/.test(location.pathname);
        const cartTarget = cartPage && matching.length > 0 && matching.every(e => /^\d+$/.test(quantity(e) || ''));
        return !busy && (cartTarget || (added && added !== before.added) ||
            (/^\d+$/.test(count) && /^\d+$/.test(before.count) && Number(count) > Number(before.count)));
    }

    if (args.state === 'checkout') {
        const dialogs = nodes('[role="dialog"],dialog[open],[aria-modal="true"]');
        const review = nodes("#spc-orders [data-asin], #checkout-item-block [data-asin], input[name='placeYourOrder1'], #placeOrder");
        const next = nodes('a,button,input[type="submit"],[role="button"]')
            .filter(e => /^continue to checkout$/i.test(label(e)) && !e.disabled);
        let result = dialogs.length ? {kind: 'dialog', key: dialogs.map(text).join('|')} :
            review.length ? {kind: 'review', key: 'review'} :
            next.length ? {kind: 'continue', key: next.map(e => label(e) + ':' + (e.getAttribute('href') || '')).join('|')} : null;
        if (!result || busy) return false;
        result.key = location.href + '|' + result.kind + '|' + result.key;
        return !args.before || result.key !== args.before.key ? result : false;
    }

    if (args.state === 'shipping') {
        const rows = nodes('#subtotals-marketplace-table tr').filter(e => /shipping|delivery/i.test(text(e)));
        if (!rows.length || busy) return false;
        return rows.every(e => {
            const value = text(e);
            const charges = [...value.matchAll(/\$\s*([\d,.]+)/g)].map(m => Number(m[1].replaceAll(',', '')));
            return charges.length ? charges.every(n => n === 0) : /\bfree\b/i.test(value);
        });
    }
    if (args.state === 'cvv') {
        if (nodes(cvvSelector).length || busy) return false;
        if (confirmed) return confirmed;
        return payment || /card (?:has been )?verified|payment (?:has been )?verified|verification (?:complete|successful)/i.test(body) ||
            nodes("input[name='placeYourOrder1'], #placeOrder, #spc-orders [data-asin], #checkout-item-block [data-asin]").length > 0;
    }
    throw new Error('Unknown Amazon transition');
}"""
