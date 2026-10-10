"""Retailer contract and independently testable monitor/cart/checkout services."""
import asyncio
from dataclasses import dataclass
from typing import Protocol

from .store import now
from .models import stock_observation


class RetailerAdapter(Protocol):
    async def ensure_session(self, context, account, page): ...
    async def inspect(self, page, item, region): ...
    async def get_cart(self, page): ...
    async def free_shipping(self, page): ...
    async def payment_verification(self, page): ...
    async def cart(self, page, quantity, asin): ...
    async def buy_now(self, page, quantity, asin): ...
    async def prepare_checkout(self, page, asin, quantity): ...
    async def checkout_snapshot(self, page, asin, quantity, max_total, **limits): ...
    async def submit_order(self, page): ...
    async def confirmation(self, page): ...


@dataclass(frozen=True)
class MonitorEvent:
    retailer: str
    product_id: str
    offer_id: str
    seller: str
    price: float | None
    availability: str
    timestamp: str
    observation: dict


class MonitorService:
    def __init__(self, adapter: RetailerAdapter): self.adapter=adapter

    async def scan(self, pages, items, region, retailer='amazon', concurrency=3):
        semaphore=asyncio.Semaphore(concurrency)
        async def inspect(page,item):
            async with semaphore:
                product=await self.adapter.inspect(page,item,region)
                return MonitorEvent(retailer,item['asin'],product.get('offer_id',''),product.get('seller',''),product.get('price'),stock_observation(product)[0],now(),product)
        results=await asyncio.gather(*(inspect(p,i) for p,i in zip(pages,items)),return_exceptions=True)
        return results


class CartService:
    def __init__(self, adapter: RetailerAdapter): self.adapter=adapter
    async def add(self,page,item,quantity):
        return await self.adapter.cart(page,quantity,item['asin'])


class CheckoutService:
    def __init__(self, adapter: RetailerAdapter): self.adapter=adapter
    async def review(self,page,item,quantity,group,task):
        if not task.get('use_buy_now'):
            await self.adapter.prepare_checkout(page,item['asin'],quantity)
        if task['force_free_shipping']: await self.adapter.free_shipping(page)
        caps=[x for x in (item['max_price'],group['max_price']) if x is not None]
        return await self.adapter.checkout_snapshot(page,item['asin'],quantity,group['max_total'],max_unit_price=min(caps) if caps else None,allow_third_party=group['allow_third_party'],allow_used=group['allow_used'])
    async def submit(self,page): await self.adapter.submit_order(page)
    async def verify(self,page): return await self.adapter.confirmation(page)
