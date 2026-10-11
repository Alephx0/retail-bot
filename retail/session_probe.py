"""Bounded, read-only saved-session probe. Never authorizes a purchase."""
import asyncio
from html.parser import HTMLParser
from http.cookiejar import Cookie
import re
import time
from urllib.parse import urljoin, urlparse

import httpx

from .models import DOMAINS, proxy_config
from .performance import timed


class SessionMarkup(HTMLParser):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.text = []
        self.ignored = 0
        self.signed_out = False
        self.logout = False
        self.orders = False
        self.blocked = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self.ignored += 1
        if self.ignored:
            return
        ident = attrs.get('id', '')
        self.signed_out |= ident in ('ap_email', 'ap_password')
        self.blocked |= ident in ('captchacharacters', 'auth-mfa-otpcode', 'cvf-input-code')
        self.orders |= ident in ('yourOrders', 'ordersContainer') or 'your-orders-content-container' in attrs.get('class', '').split()
        if tag == 'a':
            url = urlparse(urljoin(f'https://{self.host}/', attrs.get('href', '')))
            self.logout |= url.scheme == 'https' and url.netloc == self.host and 'sign-out' in url.path

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.ignored = max(0, self.ignored - 1)

    def handle_data(self, data):
        if not self.ignored:
            self.text.append(data)

    def result(self):
        text = ' '.join(self.text)
        if self.blocked or re.search(r'captcha|robot check|access denied|verify your identity|verify it.s you|continue shopping|additional verification', text, re.I):
            return 'unknown'
        if self.signed_out:
            return 'signed_out'
        if self.logout and self.orders and re.search(r'\byour orders\b', text, re.I):
            return 'authenticated'
        return 'unknown'


def session_cookies(account, host):
    jar = httpx.Cookies()
    allowed = {host, host.removeprefix('www.')}
    for value in (account.get('session') or {}).get('cookies', []):
        domain = value.get('domain', '')
        if domain.lstrip('.') not in allowed or not value.get('name') or value.get('partitionKey'):
            continue
        expires = value.get('expires', -1)
        if expires and expires > 0 and expires <= time.time():
            continue
        path = value.get('path', '/')
        if not path.startswith('/') or any(c in value['name'] + value.get('value', '') for c in '\r\n'):
            continue
        jar.jar.set_cookie(Cookie(0, value['name'], value.get('value', ''), None, False,
            domain, domain.startswith('.'), domain.startswith('.'), path, True,
            bool(value.get('secure', True)), int(expires) if expires and expires > 0 else None,
            not expires or expires <= 0, None, None, {}, False))
    return jar


@timed('Browser-free session check')
async def probe_session(account, connection, *, response_guard, transport=None, timeout=5):
    host = DOMAINS[account['region']]
    cookies = session_cookies(account, host)
    if not len(cookies):
        return 'unknown'
    config = proxy_config(connection or '')
    proxy = httpx.Proxy(config['server'], auth=(config['username'], config['password']) if 'username' in config else None) if config else None
    url = f'https://{host}/gp/your-account/order-history'
    try:
        async with asyncio.timeout(timeout), httpx.AsyncClient(
                cookies=cookies, proxy=proxy, transport=transport, trust_env=False,
                timeout=timeout, follow_redirects=False, headers={'Accept': 'text/html'}) as client:
            for _ in range(4):
                async with client.stream('GET', url) as response:
                    response_guard(response)
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = urlparse(urljoin(url, response.headers.get('location', '')))
                        if target.scheme != 'https' or target.netloc != host or target.username:
                            return 'unknown'
                        if target.path.startswith(('/ap/signin', '/gp/sign-in')):
                            return 'signed_out'
                        # Only read account/order pages; never follow an arbitrary action URL.
                        if target.path.rstrip('/') not in ('/gp/your-account/order-history', '/your-orders/orders', '/gp/css/order-history'):
                            return 'unknown'
                        url = target.geturl()
                        continue
                    if response.status_code != 200 or 'text/html' not in response.headers.get('content-type', ''):
                        return 'unknown'
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            return 'unknown'
                    parser = SessionMarkup(host)
                    parser.feed(body.decode(response.encoding or 'utf-8', errors='replace'))
                    return parser.result()
    except (httpx.HTTPError, TimeoutError, UnicodeError, ValueError):
        return 'unknown'
    return 'unknown'
