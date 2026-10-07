"""Bounded, cancellable interaction pacing using public Patchright APIs.

Engineering defaults, not a trained biometric model or an evasion guarantee.
No input values or event traces are recorded by the runtime controller.
"""
import asyncio
import math
import random

from .interactions import InteractionError


SHIFTED_US_KEYS = dict(zip('~!@#$%^&*()_+{}|:"<>?', (
    'Backquote', 'Digit1', 'Digit2', 'Digit3', 'Digit4', 'Digit5',
    'Digit6', 'Digit7', 'Digit8', 'Digit9', 'Digit0', 'Minus', 'Equal',
    'BracketLeft', 'BracketRight', 'Backslash', 'Semicolon', 'Quote',
    'Comma', 'Period', 'Slash',
)))


def _key_chord(char):
    """Use actual US key/modifier chords for shifted ASCII characters."""
    if 'A' <= char <= 'Z':
        return 'Shift+Key' + char
    if char in SHIFTED_US_KEYS:
        return 'Shift+' + SHIFTED_US_KEYS[char]
    return 'Space' if char == ' ' else char


class PacedInput:
    def __init__(self, page, *, seed=None):
        self.page = page
        self.rng = random.Random(seed)
        self.position = (0.0, 0.0)
        self.lock = asyncio.Lock()

    async def move(self, x, y):
        start_x, start_y = self.position
        dx, dy = x - start_x, y - start_y
        distance = math.hypot(dx, dy)
        if distance < 1:
            return
        duration = min(1.1, .18 + distance / self.rng.uniform(1000, 1700))
        steps = max(4, min(48, round(duration / .024)))
        bend = self.rng.uniform(-1, 1) * min(45, distance * .12)
        viewport = self.page.viewport_size or {'width': 1280, 'height': 720}
        for index in range(1, steps + 1):
            t = index / steps
            progress = t ** 3 * (10 - 15*t + 6*t*t)
            offset = math.sin(math.pi * t) * bend
            px = min(viewport['width'] - 1, max(0, start_x + dx*progress - dy/distance*offset))
            py = min(viewport['height'] - 1, max(0, start_y + dy*progress + dx/distance*offset))
            # Relative waits avoid a burst of catch-up events after scheduler lag.
            await asyncio.sleep(duration / steps * self.rng.uniform(.8, 1.2))
            await self.page.mouse.move(px, py)
            self.position = (px, py)

    async def _prepare(self, target):
        if hasattr(target, 'wait_for_element_state'):
            await target.wait_for_element_state('visible', timeout=3000)
        else:
            await target.wait_for(state='visible', timeout=3000)
        viewport = self.page.viewport_size or {'width': 1280, 'height': 720}
        for _ in range(8):
            box = await target.bounding_box()
            if not box:
                raise InteractionError('Interaction target has no visible bounds')
            cy = box['y'] + box['height'] / 2
            if 20 <= cy <= viewport['height'] - 20:
                break
            # Put wheel input over the viewport, never click bare coordinates.
            await self.move(viewport['width'] / 2, viewport['height'] / 2)
            delta = max(-480, min(480, cy - viewport['height'] / 2))
            for _ in range(3):
                await self.page.mouse.wheel(0, delta / 3)
                await asyncio.sleep(self.rng.uniform(.04, .09))
            after = await target.bounding_box()
            if after and abs(after['y'] - box['y']) < 1:
                break  # Nested containers/scroll blockers need native scrolling.
        await target.scroll_into_view_if_needed(timeout=3000)
        box = await target.bounding_box()
        if not box:
            raise InteractionError('Interaction target disappeared before input')
        x = max(0, min(viewport['width'] - 1, box['x'] + box['width']/2))
        y = max(0, min(viewport['height'] - 1, box['y'] + box['height']/2))
        await self.move(x, y)
        await asyncio.sleep(self.rng.uniform(.06, .18))

    async def prepare(self, target):
        async with self.lock, asyncio.timeout(12):
            await self._prepare(target)

    async def click(self, target, **kwargs):
        async with self.lock, asyncio.timeout(15):
            await self._prepare(target)
            # Locator/ElementHandle checks still decide where and whether to click.
            # A failure is never retried by this layer.
            await target.click(delay=self.rng.triangular(35, 130, 65), **kwargs)

    async def fill(self, target, value):
        async with self.lock, asyncio.timeout(45):
            # Complex Unicode, special input types and long values retain native
            # fill semantics rather than synthesizing incorrect keyboard events.
            input_type = (await target.get_attribute('type') or 'text').lower()
            if len(value) > 128 or not value.isascii() or any(ord(c) < 32 for c in value) or input_type not in ('text', 'password', 'email', 'search', 'tel', 'url'):
                await target.fill(value)
                return
            await self._prepare(target)
            # press() focuses without checking whether a pointer can reach the
            # field. Click it first so overlays/disabled controls stop input.
            if not await target.is_editable():
                raise InteractionError('Paced input target is not editable')
            await target.click(delay=self.rng.triangular(35, 130, 65), timeout=3000)
            if await target.input_value():
                await target.fill('')
            for char in value:
                # A locator press focuses the intended field each time. Its
                # chord also releases modifiers after shifted characters.
                await target.press(_key_chord(char),
                                   delay=self.rng.triangular(35, 125, 65), timeout=3000)
                await asyncio.sleep(self.rng.triangular(.025, .16, .07))
            if await target.input_value() != value:
                raise InteractionError('Paced input did not produce the requested value; input stopped')


def controller(page):
    if getattr(getattr(page, 'context', None), '_retail_paced_input', False) is not True:
        return None
    value = getattr(page, '_retail_paced_controller', None)
    if value is None:
        value = page._retail_paced_controller = PacedInput(page)
    return value


async def click(page, target, **kwargs):
    paced = controller(page)
    if paced and not kwargs.get('trial'):
        await paced.click(target, **kwargs)
    else:
        await target.click(**kwargs)


async def fill(page, target, value):
    paced = controller(page)
    if paced:
        await paced.fill(target, value)
    else:
        await target.fill(value)


async def prepare(page, target):
    paced = controller(page)
    if paced:
        await paced.prepare(target)
