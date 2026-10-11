"""Allowlisted recovery evidence. Page prose, URLs and exception text are not exports."""
import hashlib
import re
from urllib.parse import urlsplit

VERSION = 1
PROTECTED_DIALOG = re.compile(r'captcha|robot check|verify|verification|sign in|log in|password|payment|credit card|shipping|delivery|address|location|country|region|store selection|cookies?|consent|terms|privacy|accept|agree', re.I)


def page_key(url):
    parsed = urlsplit(url)
    path = parsed.path.lower()
    route = ('product' if '/dp/' in path or '/gp/product/' in path else
             'review' if path.endswith('/spc') else 'checkout' if 'checkout' in path else
             'cart' if '/cart' in path else 'other')
    return parsed.hostname or '', route


def public_url(url):
    host, route = page_key(url)
    # Never retain arbitrary paths, query parameters or embedded credentials.
    return f'https://{host}/{route}' if re.fullmatch(r'[a-z0-9.-]+', host) else ''


def approved(recipe):
    return (recipe.get('status') == 'approved' and recipe.get('schema_version') == VERSION
            and bool(recipe.get('review_commit')) and bool(recipe.get('test_evidence')))


def incident_key(host, route, action, category):
    return hashlib.sha256(f'{VERSION}|{host}|{route}|{action}|{category}'.encode()).hexdigest()[:24]
