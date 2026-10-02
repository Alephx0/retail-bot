"""Task-scoped MCP tools sharing the orchestrator's Playwright page.

The SDK memory transport keeps browser capabilities local. The provider receives
tool schemas/results; it never connects to the debugging port or gets credentials.
New retailer adapters supply their own domains and semantic action policies.
"""
import html
import re
from urllib.parse import urlsplit, urlunsplit

from mcp.server.fastmcp import FastMCP


AMAZON_ACTIONS = {
    'ADD_TO_CART': r'^(?:add (?:this item |item )?to (?:shopping )?cart|add to basket|add to bag)(?:\s*\(\d+\))?$',
    'BUY_NOW': r'^(?:buy(?: it)? now|purchase(?: now)?|get it now)$',
    'BEGIN_CHECKOUT': r'^(?:proceed to checkout|continue to checkout|checkout|check out)(?:\s*\(\d+ items?\))?$',
    'CONTINUE_CHECKOUT': r'^(?:continue to checkout|proceed to checkout|continue with checkout|skip and continue to checkout)$',
    'DISMISS_CHECKOUT_OFFER': r'^(?:no thanks|no, thanks|not now|skip|skip this offer|continue without (?:adding|this offer))$',
    'SUBMIT_ORDER': r'^(?:place (?:your )?order|confirm (?:and place |your )?order|complete purchase|submit order|purchase)(?:\s*\(.*\))?$',
}


class BrowserTools:
    def __init__(self, page, action, domains, policies):
        if action not in policies:
            raise ValueError('Retailer does not support this agent action')
        self.page, self.action, self.domains, self.policies = page, action, domains, policies
        self.url = page.url
        self.nodes = {}
        self.chosen = None
        self.evidence = []
        self.server = FastMCP('retail-browser')
        self.server.tool()(self.observe_controls)
        self.server.tool()(self.inspect_accessibility)
        self.server.tool()(self.validate_control)

    def check_page(self):
        if self.page.url != self.url or urlsplit(self.page.url).hostname not in self.domains:
            raise ValueError('Page changed or left the retailer domain; observe again in a new action')

    async def observe_controls(self) -> dict:
        """Observe visible action buttons and links; account data and cookies are excluded."""
        self.check_page()
        self.nodes.clear()
        self.chosen = None
        controls = []
        root = self.page.locator('[role=dialog],dialog[open],[aria-modal=true]') if self.action == 'DISMISS_CHECKOUT_OFFER' else self.page
        for node in (await root.locator('button,input[type=submit],input[type=button],[role=button],a[href],[role=link]').element_handles())[:500]:
            if not await node.is_visible() or not await node.is_enabled():
                continue
            metadata = await node.evaluate("e => { const a=e.closest('a[href]'); const u=a ? new URL(a.href, location.href) : null; return {tag:e.tagName.toLowerCase(),label:(e.getAttribute('aria-label') || (e.matches('input') ? e.value : e.innerText) || '').trim().slice(0,120),href:u && u.origin === location.origin ? u.pathname : ''}; }")
            ref = str(len(controls) + 1)
            self.nodes[ref] = (node, metadata)
            controls.append({'ref': ref, **metadata})
        self.evidence = controls
        u = urlsplit(self.url)
        return {'url': urlunsplit((u.scheme, u.netloc, u.path, '', '')), 'expected_action': self.action, 'controls': controls}

    async def inspect_accessibility(self) -> dict:
        """Read button and link names through Chromium CDP; excludes text fields and customer text."""
        self.check_page()
        session = await self.page.context.new_cdp_session(self.page)
        try:
            result = await session.send('Accessibility.getFullAXTree')
            names = [n.get('name', {}).get('value', '')[:120] for n in result.get('nodes', [])
                     if not n.get('ignored') and n.get('role', {}).get('value') in ('button', 'link')]
            return {'button_names': names[:150]}
        finally:
            await session.detach()

    async def validate_control(self, ref: str) -> dict:
        """Propose an observed ref for the expected action. Offline replay and live actionability must pass."""
        self.check_page()
        self.chosen = None
        if ref not in self.nodes:
            return {'validated': False, 'reason': 'Observe controls first; use an observed ref'}
        node, metadata = self.nodes[ref]
        pattern = self.policies[self.action]
        eligible = [x for x in self.evidence if re.fullmatch(pattern, x['label'], re.I)]
        if not any(x['ref'] == ref for x in eligible):
            return {'validated': False, 'reason': 'Action meaning is unsupported or ambiguous; human review required'}
        if self.action == 'CONTINUE_CHECKOUT':
            if not metadata.get('href') or 'checkout' not in urlsplit(metadata['href']).path.lower():
                return {'validated': False, 'reason': 'Selected link is not a checkout continuation'}
        # Replay only the bounded button metadata, with scripts and network absent.
        replay_context = await self.page.context.browser.new_context()
        try:
            replay = await replay_context.new_page()
            await replay.route('**/*', lambda route: route.abort())
            replay_labels = [metadata['label']] if self.action == 'CONTINUE_CHECKOUT' else [c['label'] for c in self.evidence]
            await replay.set_content('<body>' + ''.join('<button>' + html.escape(label) + '</button>' for label in replay_labels) + '</body>')
            candidate = replay.get_by_role('button', name=metadata['label'], exact=True)
            if await candidate.count() != 1:
                return {'validated': False, 'reason': 'Offline replay is ambiguous'}
            await candidate.click(trial=True, timeout=3000)
        finally:
            await replay_context.close()
        self.check_page()
        current = await node.evaluate("e => { const a=e.closest('a[href]'); const u=a ? new URL(a.href, location.href) : null; return {label:(e.getAttribute('aria-label') || (e.matches('input') ? e.value : e.innerText) || '').trim().slice(0,120),href:u && u.origin === location.origin ? u.pathname : ''}; }")
        if current['label'] != metadata['label'] or current['href'] != metadata.get('href','') or not await node.is_visible() or not await node.is_enabled():
            return {'validated': False, 'reason': 'Control changed after observation'}
        href = await node.evaluate("e => e.closest('a[href]')?.href || ''")
        if href and (urlsplit(href).scheme != 'https' or urlsplit(href).hostname not in self.domains):
            return {'validated': False, 'reason': 'Link leaves the permitted retailer'}
        await node.click(trial=True, timeout=3000)
        self.chosen = node
        return {'validated': True, 'ref': ref, 'action': self.action, 'scope': 'current page only; not a permanent repair'}


class PriceTools:
    """Read-only MCP evidence for a changed final-review total label."""

    def __init__(self, page, domains):
        self.page, self.domains = page, domains
        self.url = page.url
        self.rows = {}
        self.chosen = None
        self.server = FastMCP('retail-checkout-price')
        self.server.tool()(self.observe_price_rows)
        self.server.tool()(self.inspect_price_accessibility)
        self.server.tool()(self.validate_total)

    def check_page(self):
        if self.page.url != self.url or urlsplit(self.page.url).hostname not in self.domains:
            raise ValueError('Checkout page changed; observe again')

    @staticmethod
    def parse_row(text):
        compact = ' '.join(text.split())[:160]
        match = re.fullmatch(r'([^:]{2,65}):\s*(-?\s*(?:US\$|CA\$|CDN\$|[$£])\s*[\d,]+(?:\.\d{2})?)', compact, re.I)
        if not match:
            return None
        label = match[1].strip()
        if re.search(r'card|visa|mastercard|address|phone|email', label, re.I):
            return None
        raw = re.search(r'([\d,]+(?:\.\d{2})?)', match[2])
        if not raw:
            return None
        amount = float(raw[1].replace(',', ''))
        if '-' in match[2]:
            amount = -amount
        return label, amount

    async def observe_price_rows(self) -> dict:
        """List sanitized visible checkout-summary labels and amounts; no account data."""
        self.check_page()
        self.rows.clear()
        self.chosen = None
        candidates = []
        for node in (await self.page.locator('#subtotals-marketplace-table tr,li,[role=row]').element_handles())[:250]:
            if not await node.is_visible():
                continue
            parsed = self.parse_row(await node.inner_text())
            if not parsed:
                continue
            label, amount = parsed
            ref = str(len(candidates) + 1)
            self.rows[ref] = (node, label, amount)
            candidates.append({'ref': ref, 'label': label, 'amount': amount})
            if len(candidates) == 60:
                break
        u = urlsplit(self.url)
        return {'url': urlunsplit((u.scheme, u.netloc, u.path, '', '')), 'price_rows': candidates}

    async def inspect_price_accessibility(self) -> dict:
        """Inspect Chromium AX names for total labels, excluding customer fields."""
        self.check_page()
        session = await self.page.context.new_cdp_session(self.page)
        try:
            result = await session.send('Accessibility.getFullAXTree')
            names = [str(n.get('name', {}).get('value', ''))[:100] for n in result.get('nodes', [])
                     if not n.get('ignored') and re.search(r'\b(?:total|amount due|payable)\b', str(n.get('name', {}).get('value', '')), re.I)]
            return {'total_names': names[:25]}
        finally:
            await session.detach()

    async def validate_total(self, ref: str) -> dict:
        """Validate one observed final total without clicking or editing checkout."""
        self.check_page()
        self.chosen = None
        if ref not in self.rows:
            return {'validated': False, 'reason': 'Observe price rows first'}
        if not urlsplit(self.page.url).path.endswith('/spc') or not await self.page.locator("input[name='placeYourOrder1']:visible, #placeOrder:visible").count():
            return {'validated': False, 'reason': 'Not a final Amazon order review'}
        node, label, amount = self.rows[ref]
        if not re.search(r'\b(?:total|amount due|amount payable)\b', label, re.I) or re.search(r'\b(?:sub.?total|item|shipping|tax|fee|discount|saving)\b', label, re.I):
            return {'validated': False, 'reason': 'Row is not an order-total label'}
        if amount < 0 or amount > 1_000_000:
            return {'validated': False, 'reason': 'Total amount is invalid'}
        if not await node.is_visible() or self.parse_row(await node.inner_text()) != (label, amount):
            return {'validated': False, 'reason': 'Price row changed after observation'}
        # Identical top/bottom review summaries are allowed; conflicting totals are not.
        totals = [value for _, row_label, value in self.rows.values()
                  if re.search(r'\b(?:total|amount due|amount payable)\b', row_label, re.I)
                  and not re.search(r'\b(?:sub.?total|item|shipping|tax|fee|discount|saving)\b', row_label, re.I)]
        if not totals or any(value != amount for value in totals):
            return {'validated': False, 'reason': 'Conflicting final total candidates'}
        self.chosen = {'label': label, 'amount': amount}
        return {'validated': True, 'total': amount, 'label': label, 'scope': 'current final review only'}
