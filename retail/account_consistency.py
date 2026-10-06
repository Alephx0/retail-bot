"""Persistent account configuration and purchase policy.

Only supported browser settings are applied. Observed hardware identifiers
and retailer-issued authentication values are never synthesized here: the
AccountBrowserProfiles class stores and restores locale, viewport, screen
and device scale factor only.

Fingerprint transformations (canvas, WebGL, WebGPU, audio, worker
interception) live in ``retail.fingerprint`` and ``retail.native_fingerprint``
and are applied by the caller from explicit Settings flags. They are independent of headed/headless
launch mode. That module fabricates device identity values when enabled;
this module does not.
"""
import hashlib
from datetime import datetime, timedelta, timezone

from .store import now


class AccountBrowserProfiles:
    def __init__(self, store):
        self.store = store

    def get(self, account):
        key = 'browser-profile-' + account['id']
        profile = self.store.get('browser_profiles', key)
        locale = 'en-GB' if account['region'] == 'UK' else 'en-US'
        if not profile:
            profile = {'account_id': account['id'], 'version': 1,
                       'seed': hashlib.sha256(('retail-profile-v1/' + account['id']).encode()).hexdigest(),
                       'locale': locale, 'viewport': {'width': 1280, 'height': 720},
                       'screen': {'width': 1280, 'height': 720}, 'device_scale_factor': 1,
                       'created_at': now()}
            profile = self.store.put('browser_profiles', profile, key)
        if profile['locale'] != locale:
            profile.pop('observed', None)
            profile = self.store.put('browser_profiles', {**profile, 'locale': locale}, key)
        return profile

    def options(self, account):
        profile = self.get(account)
        return {key: profile[key] for key in ('locale', 'viewport', 'screen', 'device_scale_factor')}

    async def check(self, page, account):
        profile = self.get(account)
        corrected = False
        if page.viewport_size != profile['viewport']:
            await page.set_viewport_size(profile['viewport'])
            corrected = True
        observed = await page.evaluate('''() => {
            const gl = document.createElement('canvas').getContext('webgl');
            const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
            return {locale: navigator.language, hardwareConcurrency: navigator.hardwareConcurrency,
                    webdriver: navigator.webdriver, screen: {width: screen.width, height: screen.height},
                    deviceScaleFactor: devicePixelRatio,
                    webglVendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
                    webglRenderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null};
        }''')
        prior = profile.get('observed')
        drift = [key for key in observed if prior and observed[key] != prior.get(key)]
        # Keep the first observation as a baseline; never repeatedly redefine
        if prior is None:
            self.store.put('browser_profiles', {**profile, 'observed': observed}, profile['id'])
        report = {'account_id': account['id'], 'at': now(), 'drift': drift,
                  'viewport_corrected': corrected, 'status': 'changed' if drift else 'consistent'}
        self.store.put('browser_health', report, 'browser-health-' + account['id'])
        return report


class PurchaseCooldown:
    def __init__(self, store):
        self.store = store

    def eligible_at(self, account_id, days, current=None):
        current = current or datetime.now(timezone.utc)
        if not days:
            return current
        latest = None
        for record in self.store.all('checkouts'):
            if record.get('account_id') != account_id or record.get('simulation'):
                continue
            if record.get('status') not in ('confirmation_detected', 'payment_verification'):
                continue
            try:
                timestamp = datetime.fromisoformat(record['at'])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                raise ValueError('An account order has an invalid timestamp; review its history before purchasing') from None
            latest = max(latest, timestamp) if latest else timestamp
        return max(current, latest + timedelta(days=days)) if latest else current
