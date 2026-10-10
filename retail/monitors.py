"""Shared, read-only product watchers with independent browser identities."""
import asyncio
import hashlib
import time
import uuid
from collections import deque

from .amazon import Attention, BackoffRequired
from .models import inputs, stock_observation
from .store import now


class MonitorUnavailable(ValueError):
    pass


def monitor_items(store, group, task=None):
    source = store.get('input_lists', group.get('input_list_id', '')) if group.get('input_list_id') else group
    if not source:
        raise ValueError('The monitor input list no longer exists')
    items = inputs(source['products']) if group.get('retailer', 'amazon') == 'amazon' else [
        {'asin': line.strip(), 'max_price': None, 'offer_id': ''}
        for line in source['products'].splitlines() if line.strip()]
    for item in items:
        item['offer_id'] = item['offer_id'] or group.get('offer_id', '')
    selected = (task or {}).get('monitor_asin', '')
    if selected:
        items = [item for item in items if item['asin'] == selected]
        if not items:
            raise ValueError('The assigned monitor is no longer in this group; select a current product')
    return items


class ProductMonitor:
    def __init__(self, registry, key, group, item, region, simulation):
        self.registry, self.key = registry, key
        self.group, self.item = group, item
        self.region, self.simulation = region, simulation
        self.id = 'monitor-' + uuid.uuid4().hex
        self.identity = {
            'id': self.id, 'region': region, 'fingerprint_seed': uuid.uuid4().hex,
            'fingerprint_overrides': {'fingerprint_backend': 'fingerprint-suite',
                                      'browser_incognito': True,
                                      'browser_extension_ids': []},
            'proxy': '', 'proxy_list_id': '',
        }
        self.subscribers = set()
        self.log = deque(maxlen=100)
        self.changed = asyncio.Event()
        self.product = None
        self.observed_at = 0
        self.sequence = 0
        self.error = None
        self.status, self.message = '', ''
        self.job = None
        self.update('starting', 'Starting independent stock monitor')

    def update(self, status, message):
        self.status, self.message = status, message
        self.log.append({'at': now(), 'status': status, 'message': message})
        previous, self.changed = self.changed, asyncio.Event()
        previous.set()

    def public(self):
        return {'id': self.id, 'group_id': self.group['id'], 'asin': self.item['asin'],
                'region': self.region, 'simulation': self.simulation,
                'connection': 'Proxy group' if self.group.get('monitor_proxy_id') else 'Direct / device network',
                'status': self.status, 'message': self.message, 'log': list(self.log),
                'task_ids': sorted(self.subscribers)}

    async def run(self):
        engine = self.registry.engine
        context = None
        errors = backoffs = 0
        try:
            if not self.simulation:
                adapter = engine.adapter_for(self.group['retailer'])
                # Empty string explicitly disables proxy fallback. No account cookies
                # or credentials enter this anonymous monitor context.
                proxy = engine.proxy(self.group.get('monitor_proxy_id', ''), self.id) or ''
                if self.group.get('monitor_proxy_id') and not proxy:
                    raise ValueError('The selected monitor proxy group has no usable route')
                context = await adapter.context(self.identity, proxy, '')
                page = await context.new_page()
                if not (engine.store.get('settings', 'settings') or {}).get('show_browser_window', False):
                    if hasattr(adapter, 'hide'):
                        await adapter.hide(page)
            while True:
                latest = engine.store.get('groups', self.group['id']) or self.group
                delay = latest.get('delay_ms', 4500) / 1000
                try:
                    self.update('checking', 'Checking stock')
                    if self.simulation:
                        await asyncio.sleep(0.05)
                        price = 0 if self.group['mode'] == 'deals' and self.group['only_freebies'] else min(
                            29.99, self.item['max_price'] if self.item['max_price'] is not None else
                            self.group['max_price'] if self.group['max_price'] is not None else 29.99)
                        product = {**self.item, 'title': 'Simulation product ' + self.item['asin'],
                                   'price': price, 'original_price': 100, 'available': True,
                                   'seller': 'Amazon (simulation)', 'amazon_seller': True, 'condition': 'new'}
                    else:
                        async with self.registry.scan_slots, self.registry.group_slots[self.group['id']]:
                            product = await adapter.inspect(page, self.item, self.region)
                    self.product = product
                    self.observed_at = time.monotonic()
                    self.sequence += 1
                    errors = backoffs = 0
                    engine.store.put('feed', dict(product, at=now(), simulation=self.simulation,
                                                 group_id=self.group['id'], retailer=self.group['retailer']), self.id)
                    stock, message = stock_observation(product)
                    self.update({'available': 'in_stock', 'unavailable': 'out_of_stock', 'unknown': 'stock_unknown'}[stock],
                                message + f'; next check in {delay:g}s')
                except BackoffRequired as exc:
                    self.product = None
                    backoffs += 1
                    if backoffs >= 3:
                        raise Attention('Repeated retailer backoffs; stop and restart after reviewing the connection') from exc
                    delay = max(exc.retry_after_seconds or 0, min(900, 30 * 2 ** (backoffs - 1)))
                    self.update('backing_off', f'Retailer requested a cooldown; next check in {delay:g}s')
                except Attention:
                    raise
                except Exception:
                    self.product = None
                    errors += 1
                    if errors >= self.group['max_errors']:
                        raise ValueError('Monitor stopped after repeated browser/network failures')
                    delay = min(60, delay * 2 ** (errors - 1))
                    self.update('retrying', f'Browser/network error; retry {errors}/{self.group["max_errors"]} in {delay:g}s')
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, (Attention, ValueError)) else 'Monitor browser could not start; check browser settings'
            self.update('attention' if isinstance(exc, Attention) else 'error', self.error)
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            engine.store.delete('browser_profiles', 'browser-profile-' + self.id)
            binding = hashlib.sha256((self.group.get('monitor_proxy_id', '') + '/' + self.id).encode()).hexdigest()[:24]
            engine.store.delete('network_routes', 'route-' + binding)


class MonitorRegistry:
    def __init__(self, engine):
        self.engine = engine
        self.active = {}
        self.history = deque(maxlen=100)
        self.scan_slots = asyncio.Semaphore(10)
        self.group_slots = {}

    def subscribe(self, task, group, items, region):
        monitors = []
        self.group_slots.setdefault(group['id'], asyncio.Semaphore(group.get('monitor_concurrency', 3)))
        keys = [(group['id'], item['asin'], item.get('max_price'), item.get('offer_id'),
                 region, task['simulation'], group.get('monitor_proxy_id', '')) for item in items]
        if len(self.active) + len(set(keys) - self.active.keys()) > 50:
            raise ValueError('Monitor limit reached (50); stop unused tasks first')
        for key, item in zip(keys, items):
            monitor = self.active.get(key)
            if monitor is None:
                monitor = self.active[key] = ProductMonitor(self, key, group, item, region, task['simulation'])
                monitor.job = asyncio.create_task(monitor.run())
            monitor.subscribers.add(task['id'])
            monitors.append(monitor)
        return monitors

    async def observations(self, monitors, seen):
        while True:
            products, fresh = [], False
            for monitor in monitors:
                if monitor.error:
                    raise MonitorUnavailable(f'{monitor.item["asin"]}: {monitor.error}')
                group = self.engine.store.get('groups', monitor.group['id']) or monitor.group
                valid = monitor.product is not None and time.monotonic() - monitor.observed_at <= max(10, group['delay_ms'] / 500)
                if valid and monitor.sequence > seen.get(monitor.id, 0):
                    fresh = True
                products.append(monitor.product if valid else {'asin': monitor.item['asin'], 'price': None,
                                'available': False, 'seller': 'Unknown', 'condition': 'unknown'})
            if fresh:
                seen.update({m.id: m.sequence for m in monitors})
                return products
            waits = [asyncio.create_task(m.changed.wait()) for m in monitors]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for wait in waits:
                    wait.cancel()
                await asyncio.gather(*waits, return_exceptions=True)

    async def unsubscribe(self, task_id, monitors):
        for monitor in set(monitors):
            monitor.subscribers.discard(task_id)
            if not monitor.subscribers and self.active.get(monitor.key) is monitor:
                # Remove before awaiting teardown so a new start cannot join a dying watcher.
                self.active.pop(monitor.key)
                monitor.job.cancel()
                await asyncio.gather(monitor.job, return_exceptions=True)
                if not monitor.error:
                    monitor.update('stopped', 'Stopped; no tasks are using this monitor')
                self.history.append(monitor.public())
        for group_id in list(self.group_slots):
            if not any(m.group['id'] == group_id for m in self.active.values()):
                self.group_slots.pop(group_id)

    def snapshot(self):
        return [*self.history, *(m.public() for m in self.active.values())]
