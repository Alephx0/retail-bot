"""Task-scoped evidence and validation, with optional MCP exposure for development.

Runtime calls these bounded methods directly. Providers receive only allowlisted
labels and references; retailer adapters alone execute purchasing actions.
"""
import re
from urllib.parse import urlsplit

from .recovery_evidence import public_url, PROTECTED_DIALOG

from mcp.server.fastmcp import FastMCP


AMAZON_ACTIONS = {
    'ADD_TO_CART': r'^(?:add (?:this item |item )?to (?:shopping )?cart|add to basket|add to bag)(?:\s*\(\d+\))?$',
    'BUY_NOW': r'^(?:buy(?: it)? now|purchase(?: now)?|get it now)$',
    'BEGIN_CHECKOUT': r'^(?:proceed to checkout|continue to checkout|checkout|check out)(?:\s*\(\d+ items?\))?$',
    'CONTINUE_CHECKOUT': r'^(?:continue to checkout|proceed to checkout|continue with checkout|skip and continue to checkout)$',
    'DISMISS_CHECKOUT_OFFER': r'^(?:no thanks|no, thanks|not now|skip|skip this offer|continue without (?:adding|this offer))$',
    'SUBMIT_ORDER': r'^(?:place (?:your )?order|confirm (?:and place |your )?order|complete purchase|submit order|purchase)(?:\s*\(\d+ items?\))?$',
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
        self.context = page.context
        self.owner = getattr(self.context, '_retail_task_id', '')
        self._server = None

    @property
    def server(self):
        # MCP remains available to development tools; runtime uses direct methods.
        if self._server is None:
            self._server = FastMCP('retail-browser')
            for method in (self.observe_controls, self.inspect_accessibility, self.validate_control):
                self._server.tool()(method)
        return self._server

    def check_page(self):
        if (self.page.is_closed() or self.page.context is not self.context
                or getattr(self.context, '_retail_task_id', '') != self.owner
                or self.page.url != self.url or urlsplit(self.page.url).hostname not in self.domains):
            raise ValueError('Page changed or left its account scope; review the task browser')

    async def observe_controls(self) -> dict:
        """Observe only allowlisted action names, never arbitrary page/customer prose."""
        self.check_page()
        self.nodes.clear()
        self.chosen = None
        controls = []
        pattern = re.compile(self.policies[self.action], re.I)
        frames = [self.page.main_frame] + [f for f in self.page.frames
                  if f is not self.page.main_frame and urlsplit(f.url).hostname == urlsplit(self.url).hostname][:4]
        for frame in frames:
            root = frame.locator('[role=dialog],dialog[open],[aria-modal=true]') if self.action == 'DISMISS_CHECKOUT_OFFER' else frame
            if self.action == 'DISMISS_CHECKOUT_OFFER' and await root.filter(has_text=PROTECTED_DIALOG).count():
                raise ValueError('Checkout dialog requires a user decision')
            # Role locators pierce shadow roots. Limit handles before transfer.
            candidates = root.get_by_role('button', name=pattern).or_(root.get_by_role('link', name=pattern))
            count = await candidates.count()
            if count > 60:
                raise ValueError('Too many possible controls; narrow the action before recovery')
            for node in await candidates.element_handles():
                if not await node.is_visible() or not await node.is_enabled():
                    continue
                metadata = await node.evaluate("e => { const a=e.closest('a[href]'); const u=a ? new URL(a.href, location.href) : null; return {tag:e.tagName.toLowerCase(),label:(e.getAttribute('aria-label') || (e.matches('input') ? e.value : e.innerText) || '').trim().slice(0,120),href:u && u.origin === location.origin ? u.pathname : ''}; }")
                if not pattern.fullmatch(metadata['label']):
                    continue
                # Keep the exact path only in local validation state.
                metadata['frame'] = frame
                metadata['frame_url'] = frame.url
                ref = str(len(controls) + 1)
                self.nodes[ref] = (node, metadata)
                controls.append({'ref': ref, 'tag': metadata['tag'], 'label': metadata['label'],
                                 'href': '/checkout' if 'checkout' in metadata['href'].lower() else ''})
        self.evidence = controls
        self.check_page()
        return {'url': public_url(self.url), 'expected_action': self.action, 'controls': controls}

    async def inspect_accessibility(self) -> dict:
        """Targeted role names from the current observation; no full AX tree."""
        observed = await self.observe_controls()
        return {'button_names': [c['label'] for c in observed['controls']]}

    async def validate_control(self, ref: str) -> dict:
        """Propose an observed ref for the expected action. Live uniqueness, ownership and actionability must pass."""
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
        if len(eligible) != 1:
            return {'validated': False, 'reason': 'Multiple eligible controls; human review required'}
        live_count = 0
        frames = [self.page.main_frame] + [f for f in self.page.frames if f is not self.page.main_frame
                  and urlsplit(f.url).hostname == urlsplit(self.url).hostname][:4]
        for frame in frames:
            root = frame.locator('[role=dialog],dialog[open],[aria-modal=true]') if self.action == 'DISMISS_CHECKOUT_OFFER' else frame
            names = re.compile(pattern, re.I)
            live_count += await root.get_by_role('button', name=names).or_(root.get_by_role('link', name=names)).count()
        if live_count != 1:
            return {'validated': False, 'reason': 'Control uniqueness changed after observation'}
        if metadata['frame'].url != metadata['frame_url']:
            return {'validated': False, 'reason': 'Frame changed after observation'}
        self.check_page()
        current = await node.evaluate("e => { const a=e.closest('a[href]'); const u=a ? new URL(a.href, location.href) : null; return {label:(e.getAttribute('aria-label') || (e.matches('input') ? e.value : e.innerText) || '').trim().slice(0,120),href:u && u.origin === location.origin ? u.pathname : ''}; }")
        if current['label'] != metadata['label'] or current['href'] != metadata.get('href','') or not await node.is_visible() or not await node.is_enabled():
            return {'validated': False, 'reason': 'Control changed after observation'}
        href = await node.evaluate("e => e.closest('a[href]')?.href || ''")
        if href and (urlsplit(href).scheme != 'https' or urlsplit(href).hostname not in self.domains):
            return {'validated': False, 'reason': 'Link leaves the permitted retailer'}
        await node.click(trial=True, timeout=3000)
        self.check_page()
        self.chosen = node
        return {'validated': True, 'ref': ref, 'action': self.action, 'scope': 'current page only; not a permanent repair'}


class PriceTools:
    """Read-only MCP evidence for a changed final-review total label."""

    def __init__(self, page, domains):
        self.page, self.domains = page, domains
        self.url = page.url
        self.rows = {}
        self.chosen = None
        self.context = page.context
        self.owner = getattr(self.context, '_retail_task_id', '')
        self._server = None

    @property
    def server(self):
        if self._server is None:
            self._server = FastMCP('retail-checkout-price')
            for method in (self.observe_price_rows, self.inspect_price_accessibility, self.validate_total):
                self._server.tool()(method)
        return self._server

    def check_page(self):
        if (self.page.is_closed() or self.page.context is not self.context or getattr(self.context, '_retail_task_id', '') != self.owner
                or self.page.url != self.url or urlsplit(self.page.url).hostname not in self.domains):
            raise ValueError('Checkout page changed; observe again')

    @staticmethod
    def parse_row(text):
        compact = ' '.join(text.split())[:160]
        match = re.fullmatch(r'([^:]{2,65}):\s*(-?\s*(?:US\$|CA\$|CDN\$|[$£])\s*[\d,]+(?:\.\d{2})?)', compact, re.I)
        if not match:
            return None
        label = match[1].strip()
        if not re.fullmatch(r'(?:order total|grand total|total due|amount due|amount payable|subtotal|item subtotal|shipping|delivery|tax|discount|total)', label, re.I):
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
        return {'url': public_url(self.url), 'price_rows': candidates}

    async def inspect_price_accessibility(self) -> dict:
        """Inspect Chromium AX names for total labels, excluding customer fields."""
        self.check_page()
        observed = await self.observe_price_rows()
        return {'total_names': [r['label'] for r in observed['price_rows']]}

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
