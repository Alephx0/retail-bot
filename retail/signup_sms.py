"""Account-bound SMSPool signup orders. Secrets and order state stay in the vault."""
import asyncio
import re
import time
from decimal import Decimal, InvalidOperation

import httpx


class SMSError(ValueError):
    pass


class SMSPool:
    def __init__(self, provider, *, transport=None):
        self.provider = dict(provider)
        self.transport = transport

    async def request(self, path, data=None, *, public=False):
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, transport=self.transport) as client:
                if public:
                    response = await client.get('https://api.smspool.net/' + path)
                else:
                    response = await client.post('https://api.smspool.net/' + path,
                        data={'key': self.provider['api_key'], **(data or {})})
                response.raise_for_status()
                result = response.json()
        except (httpx.HTTPError, ValueError, KeyError):
            raise SMSError('SMSPool request failed or timed out. Check the connection; a number purchase will not be retried automatically.') from None
        if isinstance(result, dict) and result.get('success') in (0, '0', False):
            raise SMSError('SMSPool declined the request. Check balance, availability and the price cap in Tools > Verification.')
        return result

    async def balance(self):
        result = await self.request('request/balance')
        try:
            balance = Decimal(str(result['balance']))
            if not balance.is_finite() or balance < 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise SMSError('SMSPool returned an invalid balance.') from None
        return {'ok': True, 'balance': str(balance), 'message': 'SMSPool connection verified. No number purchased.'}

    async def service(self):
        entries = await self.request('service/retrieve_all', public=True)
        matches = [str(item['ID']) for item in entries if str(item.get('name', '')).strip().lower() in ('amazon', 'amazon / amazon web services')]
        if len(matches) != 1:
            raise SMSError('SMSPool did not identify one Amazon service. No number purchased.')
        return matches[0]


class SignupSMS:
    def __init__(self, store, account, provider, *, client=None):
        self.store, self.account, self.provider = store, dict(account), dict(provider)
        self.client = client or SMSPool(provider)
        self.key = 'signup-sms-' + account['id']
        self.lock = asyncio.Lock()

    def record(self):
        return self.store.get('signup_sms', self.key) or {}

    def save(self, **extra):
        return self.store.put('signup_sms', {**self.record(), 'account_id': self.account['id'],
                              'provider_id': self.provider['id'], **extra}, self.key)

    async def number(self):
        async with self.lock:
            old = self.record()
            if old:
                if old.get('provider_id') != self.provider['id']:
                    raise SMSError('This signup already belongs to another SMS connection.')
                if old.get('status') == 'reserved' and old.get('expires_at', 0) > time.time():
                    return old
                raise SMSError('This signup already has an SMS order or purchase intent. Review it in SMSPool; another number will not be purchased automatically.')
            service = await self.client.service()
            country = 'GB' if self.account['region'] == 'UK' else self.account['region']
            cap = Decimal(str(self.provider['max_price']))
            # Persist before sending: a lost response can still mean a paid order.
            self.save(status='purchasing', country=country, max_price=str(cap), requested_at=time.time())
            try:
                result = await self.client.request('purchase/sms', {'country': country, 'service': service,
                    'max_price': str(cap), 'pricing_option': '0', 'quantity': '1', 'activation_type': 'SMS'})
                order_id = str(result.get('order_id', ''))
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', order_id):
                    raise SMSError('SMSPool returned no usable order reference. Purchase outcome requires review.')
                self.save(order_id=order_id, status='review')
                cc = str(result.get('cc', '')).lstrip('+')
                full = str(result.get('number', '')).lstrip('+')
                national = str(result.get('phonenumber', ''))
                if cc != ('44' if country=='GB' else '1'):
                    raise SMSError('SMSPool returned a number outside the account region. Review the order.')
                if not full.startswith(cc) or not cc or not full.isdigit() or not 7 <= len(full) <= 15:
                    raise SMSError('SMSPool returned an invalid phone number. Review the existing order.')
                if not national.isdigit() or full != cc + national:
                    national = full[len(cc):]
                cost=Decimal(str(result.get('cost', 'Infinity')))
                if not cost.is_finite() or cost < 0 or cost > cap:
                    raise SMSError('SMSPool reported a price above the cap. The number was not submitted; review the order.')
                lifetime = min(1800, max(0, int(result.get('expires_in', 0))))
                if not lifetime:
                    raise SMSError('SMSPool returned an expired number. Review the order.')
                return self.save(status='reserved', number='+' + full, national=national, calling_code=cc,
                                 expires_at=time.time() + lifetime)
            except asyncio.CancelledError:
                self.save(status='uncertain')
                raise
            except Exception as exc:
                self.save(status='review')
                if isinstance(exc, SMSError):
                    raise
                raise SMSError('SMSPool purchase outcome requires review. No automatic retry.') from None

    async def code(self):
        order = self.record()
        if (order.get('status') != 'reserved' or order.get('provider_id') != self.provider['id']
                or not order.get('order_id') or order.get('code_submitted')):
            raise SMSError('No unused SMS order belongs to this signup.')
        deadline = min(order['expires_at'], time.time() + self.provider.get('timeout_seconds', 180))
        while time.time() < deadline:
            result = await self.client.request('sms/check', {'orderid': order['order_id']})
            status = int(result.get('status', 0))
            if status == 3:
                code = str(result.get('sms', '')).strip()
                if not re.fullmatch(r'\d{4,8}', code):
                    raise SMSError('SMSPool returned an unsupported code. Review the order.')
                return code
            if status not in (1, 7, 8):
                self.save(status='ended')
                raise SMSError('The SMS order expired, ended or was refunded. No replacement was purchased.')
            await asyncio.sleep(min(3, max(0, deadline - time.time())))
        raise SMSError('SMSPool did not receive a code before the wait ended. No replacement was purchased.')

    def submitted(self):
        self.save(code_submitted=True, status='submitted')

