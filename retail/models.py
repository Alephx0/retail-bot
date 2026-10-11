import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator
from .retailers import retailer_id

DOMAINS = {"US": "www.amazon.com", "UK": "www.amazon.co.uk", "CA": "www.amazon.ca"}


def inputs(text: str) -> list[dict]:
    result = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) > 3:
            raise ValueError("Use ASIN;max price;offer ID, one product per line")
        asin = parts[0].upper()
        if parts[0].lower().startswith(('https://', 'http://')):
            parsed = urlparse(parts[0])
            if parsed.username or parsed.password or parsed.hostname not in set(DOMAINS.values()) | {d.removeprefix('www.') for d in DOMAINS.values()}:
                raise ValueError("Product URLs must use Amazon US, UK, or Canada")
            match = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:/|$)", parsed.path, re.I)
            asin = match[1].upper() if match else ""
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            raise ValueError(f"Invalid ASIN: {parts[0]}")
        cap, offer = None, ""
        if len(parts) > 1 and parts[1]:
            try:
                cap = Decimal(parts[1])
                if not cap.is_finite() or cap < 0:
                    raise ValueError("Price must be finite and non-negative")
                cap = float(cap)
            except InvalidOperation:
                if len(parts) == 3:
                    raise ValueError("The middle field must be a price")
                offer = parts[1]
        if len(parts) == 3:
            offer = parts[2]
        result.append({"asin": asin, "max_price": cap, "offer_id": offer})
    if not result:
        raise ValueError("Add at least one ASIN")
    if len(result) > 20:
        raise ValueError("Maximum 20 products per group")
    return result


def canonical_inputs(text):
    """Store canonical IDs while retaining legacy per-product price/offer limits."""
    items = inputs(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return '\n'.join(item['asin'] + (';' + line.split(';', 1)[1] if ';' in line else '')
                     for item, line in zip(items, lines))


def proxy_config(value: str) -> dict | None:
    if not value.strip():
        return None
    parts = value.strip().split(":", 3)
    if len(parts) not in (2, 4) or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 65535:
        raise ValueError("Proxy format: host:port or host:port:username:password")
    if not re.fullmatch(r"[a-zA-Z0-9.\-]+", parts[0]):
        raise ValueError("Invalid proxy host")
    result = {"server": f"http://{parts[0]}:{parts[1]}"}
    if len(parts) == 4:
        result.update(username=parts[2], password=parts[3])
    return result


class FingerprintOverrides(BaseModel):
    """None inherits the current global value; False is an explicit override."""
    model_config = {'extra': 'forbid'}
    fingerprint_backend: Literal['javascript', 'native', 'fingerprint-suite'] | None = None
    fingerprint_canvas: bool | None = None
    fingerprint_webgl: bool | None = None
    fingerprint_webgpu: bool | None = None
    fingerprint_audio: bool | None = None
    fingerprint_workers: bool | None = None
    fingerprint_fonts: bool | None = None
    fingerprint_navigator: bool | None = None
    fingerprint_screen: bool | None = None
    fingerprint_proxy_location: bool | None = None
    fingerprint_timezone: Literal['America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'America/Phoenix', 'America/Anchorage', 'Pacific/Honolulu'] | None = None
    browser_incognito: bool | None = None
    browser_identity: Literal['default', 'chrome', 'msedge', 'brave', 'opera'] | None = None
    browser_extension_ids: list[str] | None = Field(default=None, max_length=20)


class FingerprintValues(BaseModel):
    model_config = {'extra': 'forbid'}
    cpu: Literal['auto', 'native', '2', '4', '8', '12', '16'] = 'auto'
    memory: Literal['auto', 'native', '8', '16', '32'] = 'auto'
    screen: Literal['auto', '1366x768@1', '1440x900@1', '1536x864@1.25', '1600x900@1', '1920x1080@1', '2048x1152@1.25', '2560x1440@1'] = 'auto'
    gpu: Literal['auto', 'native', 'family-0', 'family-1', 'family-2', 'family-3'] = 'auto'
    fonts: Literal['auto', 'native', 'core', 'office'] = 'auto'
    canvas_noise: Literal['subtle', 'standard'] = 'standard'
    webgl_noise: Literal['off', 'subtle', 'standard'] = 'standard'
    webgpu_limits: Literal['native', 'compatible'] = 'compatible'


class BrowserExtension(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=2000)
    catalog_id: str = ''
    random_eligible: bool = True


class FingerprintTestSite(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)

    @field_validator('name', 'url', mode='before')
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('url')
    @classmethod
    def website_url(cls, value):
        parsed = urlparse(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(char.isspace() or ord(char) < 32 for char in value)
                or '\\' in value):
            raise ValueError('Enter an HTTP or HTTPS website URL without credentials')
        try:
            parsed.port
        except ValueError:
            raise ValueError('Enter a valid website port')
        return value


class Account(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(default="", max_length=200)
    region: Literal["US", "UK", "CA"] = "US"
    group: str = "Personal"
    prime: bool = False
    notes: str = Field(default="", max_length=5000)
    proxy: str = ""
    proxy_list_id: str = ""
    cvv: str = Field(default="", pattern=r"^(?:\d{3,4})?$")
    retailer: str = "amazon"
    password: str = Field(default="", max_length=500)
    totp_secret: str = ""
    mailbox_id: str = ""
    solver_id: str = ""
    auto_otp: bool = True
    account_type: Literal["personal", "business"] = "personal"
    purchase_cooldown_days: Literal[0, 2, 3, 4, 5, 6, 7] = 0
    fingerprint_overrides: FingerprintOverrides = Field(default_factory=FingerprintOverrides)
    fingerprint_values: FingerprintValues = Field(default_factory=FingerprintValues)
    fingerprint_seed: str = Field(default='', pattern=r'^(?:[0-9a-f]{32}|[0-9a-f]{64})?$')

    _retailer = field_validator("retailer")(retailer_id)

    @field_validator("totp_secret")
    @classmethod
    def valid_secret(cls, value):
        import base64
        value = value.replace(" ", "").upper().rstrip("=")
        if value:
            try:
                if len(base64.b32decode(value + "=" * (-len(value) % 8))) < 10:
                    raise ValueError()
            except Exception:
                raise ValueError("Enter a valid base32 authenticator secret (at least 16 characters)")
        return value

    @field_validator("proxy")
    @classmethod
    def valid_proxy(cls, value):
        proxy_config(value)
        return value


class ProxyList(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    entries: str = Field(max_length=50000)

    @field_validator("entries")
    @classmethod
    def valid_entries(cls, value):
        lines = list(dict.fromkeys(line.strip() for line in value.splitlines() if line.strip()))
        if not 1 <= len(lines) <= 1000:
            raise ValueError("Add between 1 and 1000 proxy connections")
        for line in lines:
            proxy_config(line)
        return "\n".join(lines)


class ScheduleSlot(BaseModel):
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    stop: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class GroupSchedule(BaseModel):
    auto_start: bool = False
    days: list[int] = Field(default_factory=list, max_length=7)
    slots: list[ScheduleSlot] = Field(default_factory=list, max_length=12)
    configured_at: datetime | None = None

    @field_validator("days")
    @classmethod
    def valid_days(cls, value):
        if any(day not in range(7) for day in value):
            raise ValueError("Weekdays must be 0 (Monday) through 6 (Sunday)")
        return sorted(set(value))


class Group(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    mode: Literal["restock", "deals"] = "restock"
    products: str = Field(default="", max_length=10000)
    delay_ms: int = Field(default=4500, ge=3500, le=3600000)
    offer_id: str = ""
    notify_offer: bool = False
    skip_monitoring: bool = False
    quantity: int = Field(default=1, ge=1, le=30)
    highlight: str = Field(default="#64d9ad", pattern=r"^#[0-9a-fA-F]{6}$")
    schedule: GroupSchedule = Field(default_factory=GroupSchedule)
    min_price: float = Field(default=0, ge=0, allow_inf_nan=False)
    max_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    allow_third_party: bool = False
    allow_used: bool = False
    only_freebies: bool = True
    min_discount: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    min_savings: float = Field(default=0, ge=0, allow_inf_nan=False)
    max_total: float = Field(default=100, ge=0, allow_inf_nan=False)
    loop: bool = False
    max_checkouts: int = Field(default=1, ge=1, le=100)
    max_errors: int = Field(default=5, ge=1, le=30)
    retailer: str = "amazon"
    monitor_proxy_id: str = ""
    input_list_id: str = ""
    monitor_concurrency: int = Field(default=3, ge=1, le=10)
    retry_delay_ms: int = Field(default=3500, ge=1000, le=3600000)

    _retailer = field_validator("retailer")(retailer_id)

    @model_validator(mode="after")
    def prices(self):
        if not self.input_list_id and self.products.strip():
            if self.retailer == "amazon":
                self.products = canonical_inputs(self.products)
            elif not self.products.strip():
                raise ValueError("Add at least one product input")
        if self.max_price is not None and self.max_price < self.min_price:
            raise ValueError("Maximum price must be at least minimum price")
        return self


class Task(BaseModel):
    group_id: str
    monitor_asin: str = Field(default='', pattern=r'^(?:[A-Z0-9]{10})?$')
    account_id: str = ""
    proxy_id: str = ""
    simulation: bool = True
    quantity: int = Field(default=1, ge=1, le=30)
    max_total: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    scheduled_at: datetime | None = None
    profile_id: str = ""
    checkout_mode: Literal["review", "automatic", "monitor", "quote"] = "review"
    use_buy_now: bool = False
    solver_id: str = ""

    use_account_proxy: bool = False
    force_free_shipping: bool = False
    auto_open_3ds: bool = False
    retry_delay_ms: int = Field(default=3500, ge=1000, le=3600000)

    @field_validator("scheduled_at")
    @classmethod
    def timezone_required(cls, value):
        if value and value.tzinfo is None:
            raise ValueError("Schedule must include a timezone")
        return value


class Settings(BaseModel):
    webhook: str = ""
    notifications: bool = False
    checkout_sound: bool = True
    attention_sound: bool = True
    sound_volume: float = Field(default=0.4, ge=0, le=1)
    sound_style: Literal["chime", "bell", "pulse"] = "chime"
    max_running_tasks: int = Field(default=10, ge=1, le=50)
    browser_channel: Literal["chromium", "chrome", "msedge"] = "chromium"
    show_browser_window: bool = True
    session_verification_mode: Literal['http', 'headless', 'browser'] = 'http'
    browser_timeout_ms: int = Field(default=30000, ge=5000, le=120000)
    interaction_pacing: Literal['off', 'paced'] = 'off'
    proxy_timeout_seconds: int = Field(default=15, ge=3, le=60)
    proxy_concurrency: int = Field(default=5, ge=1, le=20)
    default_monitor_delay: int = Field(default=4500, ge=3500, le=3600000)
    webhook_checkouts: bool = True
    webhook_attention: bool = True
    trace_enabled: bool = False
    diagnosis_endpoint: str = ""
    cdp_endpoint: str = "http://127.0.0.1:9222"
    cdp_attach: bool = False
    agent_mode: Literal['off', 'recovery', 'agent'] = 'off'
    ai_connection_id: str = ''
    agent_max_steps: int = Field(default=4, ge=1, le=8)
    agent_timeout_seconds: int = Field(default=60, ge=10, le=180)

    # Fingerprint transformations are explicit opt-ins, independent of launch
    # mode. Both backends share GPU identity policy across APIs and initialize
    # workers automatically for graphics profiles. JS profiles require an
    # app-owned browser so worker startup can be initialized before page code.
    fingerprint_canvas: bool = False
    fingerprint_webgl: bool = False
    fingerprint_webgpu: bool = False
    fingerprint_audio: bool = False
    fingerprint_workers: bool = False
    fingerprint_fonts: bool = False
    fingerprint_navigator: bool = False
    fingerprint_screen: bool = False
    fingerprint_proxy_location: bool = True
    fingerprint_timezone: Literal['America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'America/Phoenix', 'America/Anchorage', 'Pacific/Honolulu'] = 'America/New_York'
    fingerprint_backend: Literal['javascript', 'native', 'fingerprint-suite'] = 'javascript'
    native_browser_executable: str = ''
    browser_incognito: bool = True
    browser_identity: Literal['default', 'chrome', 'msedge', 'brave', 'opera'] = 'default'
    browser_extension_ids: list[str] = Field(default_factory=list, max_length=20)
    random_account_extensions: bool = True
    brave_executable: str = ''
    opera_executable: str = ''
    fingerprint_test_sites: list[FingerprintTestSite] = Field(default_factory=lambda: [
        FingerprintTestSite(name='CreepJS', url='https://abrahamjuliot.github.io/creepjs/'),
        FingerprintTestSite(name='Google', url='https://www.google.com/'),
    ], max_length=20)

    @model_validator(mode='after')
    def profile_browser_connection(self):
        if self.cdp_attach and self.session_verification_mode == 'headless':
            raise ValueError('Headless session verification requires an app-managed browser')
        if self.cdp_attach and (not self.browser_incognito or self.browser_extension_ids):
            raise ValueError('Normal profiles and managed extensions require an app-managed browser')
        if self.fingerprint_backend == 'native' and self.browser_identity not in ('default', 'chrome'):
            raise ValueError('Choose JavaScript compatibility mode for genuine Edge, Brave or Opera identities')
        if self.fingerprint_backend == 'fingerprint-suite':
            if self.cdp_attach:
                raise ValueError('Fingerprint-suite requires an app-managed browser')
            return self  # Custom surface flags are inactive for this complete profile.
        if self.fingerprint_backend == 'native' and self.cdp_attach:
            raise ValueError('Native profiles require an app-managed browser; disable external CDP attachment')
        if self.cdp_attach and any((self.fingerprint_canvas, self.fingerprint_webgl, self.fingerprint_webgpu,
                                   self.fingerprint_fonts, self.fingerprint_navigator, self.fingerprint_screen)):
            raise ValueError('Complete JavaScript graphics profiles require an app-managed browser; disable external CDP attachment')
        return self

    @field_validator("diagnosis_endpoint", "cdp_endpoint")
    @classmethod
    def local_browser_service(cls, value):
        if value:
            url=urlparse(value)
            if url.scheme not in ("http","https") or url.hostname not in ("127.0.0.1","localhost","::1") or url.username or url.password:
                raise ValueError("Use a local HTTP service endpoint")
        return value

    @field_validator("webhook")
    @classmethod
    def valid_webhook(cls, value):
        if value and not re.fullmatch(r"https://(?:discord.com|discordapp.com)/api/webhooks/\d+/[A-Za-z0-9_\-]+", value):
            raise ValueError("Enter a Discord webhook URL")
        return value


def account_fingerprint_settings(settings, account):
    overrides = FingerprintOverrides.model_validate(account.get('fingerprint_overrides') or {})
    return Settings.model_validate({**settings, **overrides.model_dump(exclude_none=True)}).model_dump()


class Address(BaseModel):
    name: str = ""
    line1: str = ""
    line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = Field(default="US", pattern=r"^[A-Z]{2}$")


class Profile(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    group: str = "Personal"
    email: str = ""
    phone: str = ""
    shipping: Address = Field(default_factory=Address)
    billing: Address = Field(default_factory=Address)
    billing_same: bool = True
    card_name: str = ""
    card_number: str = ""
    expiry_month: int | None = Field(default=None, ge=1, le=12)
    expiry_year: int | None = Field(default=None, ge=2026, le=2100)

    @field_validator("card_number")
    @classmethod
    def card(cls, value):
        value = re.sub(r"[ -]", "", value)
        if value:
            if not re.fullmatch(r"\d{12,19}", value):
                raise ValueError("Invalid card number")
            digits = list(map(int, value[::-1]))
            if sum(n if i % 2 == 0 else n * 2 - (9 if n > 4 else 0) for i, n in enumerate(digits)) % 10:
                raise ValueError("Card number checksum failed")
        return value


class Mailbox(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(pattern=r"^[a-zA-Z0-9.\-]+$", max_length=253)
    port: int = Field(default=993, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=1000)
    folder: str = Field(default="INBOX", pattern=r"^[a-zA-Z0-9 /_.\-\[\]]+$")
    max_age_seconds: int = Field(default=300, ge=30, le=900)


class Solver(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Literal["manual", "capmonster", "2captcha", "anticaptcha", "capsolver", "flaresolverr"] = "manual"
    api_key: str = Field(default="", max_length=500)
    endpoint: str = "http://127.0.0.1:8191"
    timeout_seconds: int = Field(default=120, ge=10, le=300)

    @field_validator("endpoint")
    @classmethod
    def local_endpoint(cls, value):
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("FlareSolverr must use a local HTTP endpoint")
        return value.rstrip("/")

    @model_validator(mode="after")
    def credentials(self):
        if self.provider not in ("manual", "flaresolverr") and not self.api_key:
            raise ValueError("This provider requires an API key")
        return self


class InputList(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    retailer: str = "amazon"
    products: str = Field(min_length=1, max_length=10000)
    _retailer = field_validator("retailer")(retailer_id)

    @model_validator(mode="after")
    def valid(self):
        if self.retailer == "amazon":
            self.products = canonical_inputs(self.products)
        return self


def eligible(product: dict, item: dict, group: dict, *, defer_unknown_seller=False) -> bool:
    if defer_unknown_seller and product.get('seller', 'Unknown') in ('', 'Unknown'):
        product = {**product, 'amazon_seller': True}
    price = product.get("price")
    if not product.get("available") or price is None:
        return False
    if price < group["min_price"]:
        return False
    for cap in (item["max_price"], group["max_price"]):
        if cap is not None and price > cap:
            return False
    if item["offer_id"] and product.get("offer_id") != item["offer_id"]:
        return False
    if not group["allow_third_party"] and not product.get("amazon_seller"):
        return False
    if not group["allow_used"] and product.get("condition") != "new":
        return False
    if group["mode"] == "deals":
        if group["only_freebies"] and price != 0:
            return False
        original = product.get("original_price")
        savings = max(0, original - price) if original is not None else 0
        discount = savings / original * 100 if original else 0
        if discount < group["min_discount"] or savings < group["min_savings"]:
            return False
    return True


def stock_observation(product):
    """Inventory display only; `available` remains the guarded purchase signal."""
    status = product.get('availability_status') or ('available' if product.get('available') else 'unavailable')
    if status not in ('available', 'unavailable', 'unknown'):
        status = 'unknown'
    message = product.get('availability_message')
    if not message:
        message = {'available': 'In stock', 'unavailable': 'Item is out of stock',
                   'unknown': 'Stock could not be verified; Add-to-cart control could not be verified'}[status]
        if status == 'available' and not product.get('available'):
            message += '; Add-to-cart control could not be verified'
    return status, message


def rejection_reasons(product, item, group):
    reasons = []
    if product.get('agent_error'):
        reasons.append('AI recovery: ' + product['agent_error'])
    if not product.get('available'):
        reasons.append(stock_observation(product)[1])
    price = product.get('price')
    if price is None:
        reasons.append('Product price could not be read')
    else:
        if price < group['min_price']:
            reasons.append(f"Price {price:.2f} is below minimum {group['min_price']:.2f}")
        for cap in (item.get('max_price'), group.get('max_price')):
            if cap is not None and price > cap:
                reasons.append(f'Price {price:.2f} exceeds limit {cap:.2f}')
    if item.get('offer_id') and product.get('offer_id') != item['offer_id']:
        reasons.append('The requested offer ID is not the displayed offer')
    if not group['allow_third_party'] and not product.get('amazon_seller'):
        seller = product.get('seller') or 'Unknown'
        reasons.append('Seller could not be read' if seller == 'Unknown' else f'Seller is {seller}; this group requires Amazon')
    if not group['allow_used'] and product.get('condition') != 'new':
        reasons.append('Item condition is not verified as new')
    if group['mode'] == 'deals':
        if group['only_freebies'] and price != 0:
            reasons.append('Only Freebies is enabled; the item is not free')
        original = product.get('original_price')
        savings = max(0, original - price) if original is not None and price is not None else 0
        if (savings / original * 100 if original else 0) < group['min_discount']:
            reasons.append('Minimum discount is not met')
        if savings < group['min_savings']:
            reasons.append('Minimum savings is not met')
    return list(dict.fromkeys(reasons))


class ResourceFolder(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    resource_kind: Literal["accounts", "profiles", "proxies", "input_lists"]


class AIConnection(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Literal['openai', 'compatible'] = 'openai'
    protocol: Literal['responses', 'chat'] = 'responses'
    base_url: str = 'https://api.openai.com/v1'
    model: str = Field(default='gpt-6-sol', min_length=1, max_length=150)
    api_key: str = Field(default='', max_length=2000)

    @field_validator('base_url')
    @classmethod
    def api_endpoint(cls, value):
        parsed = urlparse(value)
        local = parsed.hostname in ('localhost', '127.0.0.1', '::1')
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Use an API base URL without credentials or query parameters')
        if parsed.scheme != 'https' and not (local and parsed.scheme == 'http'):
            raise ValueError('API connections require HTTPS, except local HTTP services')
        return value.rstrip('/')

    @model_validator(mode='after')
    def provider_endpoint(self):
        if self.provider == 'openai' and (self.base_url != 'https://api.openai.com/v1' or self.protocol != 'responses'):
            raise ValueError('OpenAI uses https://api.openai.com/v1 and the Responses protocol')
        return self
