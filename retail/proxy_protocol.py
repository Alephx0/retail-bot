"""Canonical proxy identities. Secrets never form part of public summaries."""
from dataclasses import dataclass
import hashlib
import ipaddress
import re
from urllib.parse import quote, unquote, urlsplit


@dataclass(frozen=True, repr=False)
class Proxy:
    scheme: str
    host: str
    port: int
    username: str = ''
    password: str = ''

    @property
    def authority(self):
        return f'[{self.host}]:{self.port}' if ':' in self.host else f'{self.host}:{self.port}'

    @property
    def url(self):
        auth = f'{quote(self.username, safe="")}:{quote(self.password, safe="")}@' if self.username else ''
        return f'{self.scheme}://{auth}{self.authority}'

    @property
    def identity(self):
        # Preserve historic HTTP fingerprints and encrypted route bindings.
        if self.scheme == 'http' and ':' not in self.host:
            return self.authority + (f':{self.username}:{self.password}' if self.username else '')
        return self.url

    @property
    def fingerprint(self):
        return hashlib.sha256(self.identity.encode()).hexdigest()[:16]

    @property
    def browser_compatible(self):
        return not (self.scheme.startswith('socks') and self.username)

    def config(self, *, browser=False):
        if browser and not self.browser_compatible:
            raise ValueError('Chromium does not support authenticated SOCKS proxies. Use an HTTP(S) route for browser tasks.')
        result = {'server': f'{self.scheme}://{self.authority}'}
        if self.username:
            result.update(username=self.username, password=self.password)
        return result

    def public(self):
        return dict(fingerprint=self.fingerprint, host=self.host, port=self.port,
                    protocol=self.scheme, authenticated=bool(self.username),
                    browser_compatible=self.browser_compatible)


def parse_proxy(value):
    try:
        text = value.strip()
        if not text or len(text) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ValueError()
        if '://' not in text:
            match = re.fullmatch(r'(\[[^\]]+\]|[^:]+):(\d+)(?::([^:]+):(.*))?', text)
            if not match:
                raise ValueError()
            host, port, user, password = match.groups()
            auth = f'{quote(user, safe="")}:{quote(password, safe="")}@' if user else ''
            text = f'http://{auth}{host}:{port}'
        parts = urlsplit(text)
        if parts.scheme not in {'http','https','socks4','socks5'} or parts.path not in ('','/') or parts.query or parts.fragment:
            raise ValueError()
        host = (parts.hostname or '').lower().rstrip('.')
        if ':' in host:
            host = str(ipaddress.IPv6Address(host))
        else:
            host = host.encode('idna').decode('ascii')
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) or '..' in host:
                raise ValueError()
        port = parts.port
        if not port or not 1 <= port <= 65535:
            raise ValueError()
        user, password = unquote(parts.username or ''), unquote(parts.password or '')
        if parts.username is not None and not user or any(ord(c)<32 or ord(c)==127 for c in user+password):
            raise ValueError()
        if len(user.encode())>255 or len(password.encode())>255 or parts.scheme=='socks4' and password:
            raise ValueError()
        return Proxy(parts.scheme,host,port,user,password)
    except (ValueError, UnicodeError, AttributeError):
        raise ValueError('Invalid proxy. Use host:port[:username:password] or an HTTP, HTTPS, SOCKS4 or SOCKS5 URI. SOCKS4 supports a user ID, not a password.') from None


def normalize_entries(value):
    entries = dict.fromkeys(parse_proxy(line).identity for line in value.splitlines() if line.strip())
    if len(entries) > 1000:
        raise ValueError('Add at most 1000 proxy connections')
    return '\n'.join(entries)
