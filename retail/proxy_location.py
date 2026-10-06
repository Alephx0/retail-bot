"""Resolve a selected proxy's exit location through Chromium's own proxy route."""

import ipaddress
import json
import math
from importlib.resources import files
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import now

LOOKUP_URL = 'https://ipwho.is/?fields=success,ip,country_code,region,city,latitude,longitude,timezone.id'
US_TIMEZONES = {line.split('\t')[2] for line in files('tzdata.zoneinfo').joinpath('zone.tab').read_text().splitlines()
                if line.startswith('US\t')}
US_TIMEZONES.update(('America/Indianapolis', 'America/Louisville', 'America/Shiprock', 'America/Atka'))


class ProxyLocationError(ValueError):
    pass


def validate_location(data):
    try:
        if not isinstance(data, dict) or data.get('success') is not True:
            raise ValueError()
        address = ipaddress.ip_address(data['ip'])
        if not address.is_global or data['country_code'] != 'US':
            raise ValueError()
        timezone = data['timezone']['id']
        if not isinstance(timezone, str) or (timezone not in US_TIMEZONES and not timezone.startswith('US/')):
            raise ValueError()
        ZoneInfo(timezone)  # Keep the IANA identifier so Chromium handles DST.
        latitude, longitude = data['latitude'], data['longitude']
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for value in (latitude, longitude)):
            raise ValueError()
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError()
        return {'ip': str(address), 'country': 'US', 'timezone': timezone,
                'latitude': latitude, 'longitude': longitude,
                'region': str(data.get('region', ''))[:100], 'city': str(data.get('city', ''))[:100],
                'checked_at': now()}
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        raise ProxyLocationError('The proxy must report a public US exit IP and a valid US location/timezone.') from None


async def lookup_location(context):
    page = None
    try:
        page = await context.new_page()
        response = await page.goto(LOOKUP_URL, wait_until='domcontentloaded', timeout=15000)
        if not response or response.status != 200:
            raise ProxyLocationError('Proxy location lookup failed. Check the proxy and ipwho.is availability, then retry.')
        body = await response.body()
        if len(body) > 65536:
            raise ProxyLocationError('Proxy location lookup returned an invalid response.')
        return validate_location(json.loads(body))
    except ProxyLocationError:
        raise
    except Exception:
        raise ProxyLocationError('Could not verify the US proxy location. Check the proxy connection and retry.') from None
    finally:
        if page:
            await page.close()


async def bootstrap_location(browser, proxy):
    context = None
    try:
        # No account cookies or storage are copied into this lookup context.
        context = await browser.new_context(proxy=proxy, locale='en-US')
        return await lookup_location(context)
    except ProxyLocationError:
        raise
    except Exception:
        raise ProxyLocationError('Could not open the proxy location check. Check the proxy connection and retry.') from None
    finally:
        if context:
            await context.close()


def ensure_same_route(before, after):
    if any(before[key] != after[key] for key in ('ip', 'country', 'timezone')):
        raise ProxyLocationError('The proxy exit changed during browser setup. Use a sticky US proxy session; this browser was closed.')


def context_options(location):
    return {'locale': 'en-US', 'timezone_id': location['timezone'],
            'geolocation': {'latitude': location['latitude'], 'longitude': location['longitude'],
                            'accuracy': 50000}}


def summary(location):
    return {key: location[key] for key in ('country', 'region', 'city', 'timezone', 'checked_at')}
