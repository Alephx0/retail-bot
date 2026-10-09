"""Immutable plans, integer money and explicit local-time execution windows."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


def cents(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError('Price must be finite and nonnegative')
        return int((amount * 100).quantize(Decimal('1')))
    except (InvalidOperation, TypeError):
        raise ValueError('Invalid price') from None


class ProductTarget(Model):
    product_id: str = Field(pattern=r'^[A-Z0-9]{10}$')
    label: str = Field(default='', max_length=160)
    offer_id: str = Field(default='', max_length=2000)
    max_unit_cents: int = Field(ge=0, le=100_000_000)
    desired_units: int = Field(default=1, ge=1, le=1000)


class Schedule(Model):
    kind: Literal['manual', 'once', 'weekly'] = 'manual'
    timezone: str = 'America/New_York'
    start: str = Field(default='10:00', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    end: str = Field(default='10:30', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    date: str = ''
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    prepare_minutes: int = Field(default=5, ge=0, le=120)
    quota_scope: Literal['group', 'window'] = 'group'

    @field_validator('timezone')
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError('Choose an installed IANA timezone') from None
        return value

    @model_validator(mode='after')
    def valid(self):
        if self.kind == 'once':
            try:
                datetime.strptime(self.date, '%Y-%m-%d')
            except ValueError:
                raise ValueError('A one-time window needs a valid date') from None
        if self.kind == 'weekly' and (not self.weekdays or any(d not in range(7) for d in self.weekdays)):
            raise ValueError('Choose at least one valid weekday')
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError('Weekdays must be unique')
        if self.kind == 'manual' and self.quota_scope == 'window':
            raise ValueError('Per-window goals require a schedule')
        return self


class AccountOverrides(Model):
    """Only group-local purchasing options; None means inherit, including false/zero."""
    action: Literal['notify', 'review', 'automatic', 'quote'] | None = None
    units_per_order: int | None = Field(default=None, ge=1, le=30)
    per_account_units: int | None = Field(default=None, ge=1, le=1000)
    per_account_orders: int | None = Field(default=None, ge=1, le=1000)
    per_account_spend_cents: int | None = Field(default=None, ge=0, le=1_000_000_000)
    max_order_cents: int | None = Field(default=None, ge=0, le=100_000_000)
    max_unit_cents: int | None = Field(default=None, ge=0, le=100_000_000)
    product_ids: list[str] | None = Field(default=None, min_length=1, max_length=20)
    allow_third_party: bool | None = None
    allow_used: bool | None = None
    max_read_errors: int | None = Field(default=None, ge=1, le=10)
    retry_delay_seconds: int | None = Field(default=None, ge=5, le=600)
    read_timeout_seconds: int | None = Field(default=None, ge=5, le=300)
    checkout_timeout_seconds: int | None = Field(default=None, ge=30, le=3600)


class AccountSettings(Model):
    enabled: bool = True
    overrides: AccountOverrides = Field(default_factory=AccountOverrides)


def effective_plan(plan, account_id):
    """Resolve once per operation from an immutable revision; never mutate the source."""
    assignment = plan.get('account_settings', {}).get(account_id, {})
    overrides = {k: v for k, v in assignment.get('overrides', {}).items() if v is not None}
    result = {**plan, **overrides}
    result['products'] = [dict(p) for p in plan['products']
                          if not overrides.get('product_ids') or p['product_id'] in overrides['product_ids']]
    if 'max_unit_cents' in overrides:
        for product in result['products']:
            product['max_unit_cents'] = overrides['max_unit_cents']
    return result


def enabled_accounts(plan):
    if not plan['account_ids'] and plan['simulation']:
        return ['simulation']
    return [id for id in plan['account_ids'] if plan.get('account_settings', {}).get(id, {}).get('enabled', True)]


def goal_met(plan, progress):
    if plan.get('goal_mode', 'units') == 'first_success':
        return progress.get('confirmed_orders', 0) >= 1
    if plan.get('goal_mode') == 'multiple_success':
        return progress.get('confirmed_orders', 0) >= plan.get('target_orders', 1)
    return progress['confirmed_units'] >= Plan.model_validate(plan).desired_units


INHERITABLE = {'action', 'units_per_order', 'per_account_units', 'per_account_orders',
               'allow_third_party', 'allow_used', 'max_read_errors', 'retry_delay_seconds',
               'read_timeout_seconds', 'checkout_timeout_seconds', 'monitor_interval_ms'}


def purchasing_defaults(store):
    values = {key: Plan.model_fields[key].default for key in INHERITABLE}
    values.update((store.get('task_group_defaults', 'defaults') or {}).get('values', {}))
    values['monitor_interval_ms'] = (store.get('settings', 'settings') or {}).get('default_monitor_delay',4500)
    return values


def resolve_defaults(plan, store):
    inherited = plan.get('inherited_fields', [])
    if not isinstance(inherited, list) or any(not isinstance(key, str) or key not in INHERITABLE for key in inherited):
        raise ValueError('Choose supported inherited defaults')
    defaults = purchasing_defaults(store)
    return {**plan, **{key:defaults[key] for key in inherited}}


class Plan(Model):
    inherited_fields: list[str] = Field(default_factory=list, max_length=11)
    name: str = Field(min_length=1, max_length=100)
    retailer: Literal['amazon'] = 'amazon'
    region: Literal['US', 'UK', 'CA'] = 'US'
    products: list[ProductTarget] = Field(min_length=1, max_length=20)
    account_ids: list[str] = Field(default_factory=list, max_length=100)
    account_settings: dict[str, AccountSettings] = Field(default_factory=dict)
    goal_mode: Literal['units', 'first_success', 'multiple_success'] = 'units'
    target_orders: int = Field(default=1, ge=1, le=1000)
    per_account_orders: int = Field(default=1000, ge=1, le=1000)
    per_account_spend_cents: int = Field(default=1_000_000_000, ge=0, le=1_000_000_000)
    retry_delay_seconds: int = Field(default=5, ge=5, le=600)
    read_timeout_seconds: int = Field(default=45, ge=5, le=300)
    checkout_timeout_seconds: int = Field(default=300, ge=30, le=3600)
    action: Literal['notify', 'review', 'automatic', 'quote'] = 'review'
    simulation: bool = True
    selection: Literal['any', 'each'] = 'any'
    target_units: int = Field(default=1, ge=1, le=1000)
    units_per_order: int = Field(default=1, ge=1, le=30)
    per_account_units: int = Field(default=1, ge=1, le=1000)
    max_order_cents: int = Field(default=10000, ge=0, le=100_000_000)
    max_spend_cents: int = Field(default=10000, ge=0, le=1_000_000_000)
    min_unit_cents: int = Field(default=0, ge=0, le=100_000_000)
    min_discount_percent: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    min_savings_cents: int = Field(default=0, ge=0, le=100_000_000)
    only_freebies: bool = False
    allow_third_party: bool = False
    allow_used: bool = False
    monitor_interval_ms: int = Field(default=4500, ge=3500, le=3600000)
    monitor_concurrency: int = Field(default=2, ge=1, le=5)
    max_parallel_checkouts: int = Field(default=2, ge=1, le=10)
    max_read_errors: int = Field(default=3, ge=1, le=10)
    schedule: Schedule = Field(default_factory=Schedule)

    @model_validator(mode='after')
    def valid(self):
        if set(self.inherited_fields)-INHERITABLE or len(set(self.inherited_fields))!=len(self.inherited_fields):
            raise ValueError('Choose supported inherited defaults')
        if set(self.account_settings) - set(self.account_ids):
            raise ValueError('Account settings must reference assigned accounts')
        for settings in self.account_settings.values():
            overrides = settings.overrides
            if overrides.product_ids and (len(set(overrides.product_ids)) != len(overrides.product_ids) or set(overrides.product_ids) - {p.product_id for p in self.products}):
                raise ValueError('Account products must be unique targets in this group')
            if overrides.max_order_cents is not None and overrides.max_order_cents > self.max_spend_cents:
                raise ValueError('Account order allowance cannot exceed the group spending limit')
        if self.goal_mode != 'units' and self.selection != 'any':
            raise ValueError('Order-count goals require Any matching product; use unit targets for Each product')
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError('Select each account only once')
        if len({p.product_id for p in self.products}) != len(self.products):
            raise ValueError('Duplicate products must be combined into one target')
        if self.max_order_cents > self.max_spend_cents:
            raise ValueError('Group spending limit must cover the maximum order total')
        if not self.simulation and not self.account_ids:
            raise ValueError('Live execution needs at least one account')
        if not self.simulation and self.region != 'US':
            raise ValueError('Live task groups currently support Amazon US; other regions are available in simulation')
        return self

    @property
    def desired_units(self):
        return self.target_units if self.selection == 'any' else sum(p.desired_units for p in self.products)


def windows(schedule, current, days_ahead=8):
    """UTC windows; skip nonexistent wall times, choose fold zero exactly once."""
    s = Schedule.model_validate(schedule)
    if s.kind == 'manual':
        return []
    zone = ZoneInfo(s.timezone)
    local = current.astimezone(zone)
    dates = ([datetime.strptime(s.date, '%Y-%m-%d').date()] if s.kind == 'once'
             else [local.date() + timedelta(days=i) for i in range(-1, days_ahead)])
    result = []
    for day in dates:
        if s.kind == 'weekly' and day.weekday() not in s.weekdays:
            continue
        start_wall = datetime.fromisoformat(f'{day.isoformat()}T{s.start}')
        end_wall = datetime.fromisoformat(f'{day.isoformat()}T{s.end}')
        if end_wall <= start_wall:
            end_wall += timedelta(days=1)
        start, end = (wall.replace(tzinfo=zone, fold=0).astimezone(timezone.utc) for wall in (start_wall, end_wall))
        if start.astimezone(zone).replace(tzinfo=None) != start_wall or end.astimezone(zone).replace(tzinfo=None) != end_wall:
            continue
        result.append({'key': start.isoformat(), 'start': start.isoformat(), 'end': end.isoformat(),
                       'prepare': (start-timedelta(minutes=s.prepare_minutes)).isoformat()})
    return result


def qualifying(product, target, plan):
    if not product.get('available'):
        return False, 'out_of_stock', 'Waiting for stock'
    if product.get('price') is None:
        return False, 'unknown_price', 'Price is not verified'
    try:
        price = cents(product['price'])
    except ValueError:
        return False, 'unknown_price', 'Price is not verified'
    if price > target['max_unit_cents']:
        return False, 'above_price_cap', 'Above the unit-price cap'
    if price < plan.get('min_unit_cents',0):
        return False, 'below_price_floor', 'Below the minimum unit price'
    if plan.get('only_freebies') and price!=0:
        return False, 'not_free', 'Waiting for a free item'
    if plan.get('min_discount_percent') or plan.get('min_savings_cents'):
        try:
            original=cents(product.get('original_price'))
        except ValueError:
            original=0
        if original<=0:
            return False, 'unknown_reference_price', 'Discount cannot be verified without a reference price'
        saving=original-price
        if saving<plan.get('min_savings_cents',0) or saving*100<original*Decimal(str(plan.get('min_discount_percent',0))):
            return False, 'discount_too_small', 'Discount or savings are below the group minimum'
    if target.get('offer_id') and product.get('offer_id') != target['offer_id']:
        return False, 'offer_mismatch', 'Waiting for the selected offer'
    if not plan['allow_third_party'] and not product.get('amazon_seller'):
        return False, 'seller_rejected', 'Seller does not meet the group rules'
    if not plan['allow_used'] and product.get('condition') != 'new':
        return False, 'condition_rejected', 'Condition does not meet the group rules'
    return True, 'eligible', 'Matches the group rules'
