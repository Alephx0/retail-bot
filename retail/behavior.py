"""Bounded, cancellable interaction pacing using public Patchright APIs.

Engineering defaults, not a trained biometric model or an evasion guarantee.
No input values or event traces are recorded by the runtime controller.
"""
import asyncio
import copy
import math
import random
import time

from .interactions import InteractionError
from ._vendor.humantyping import keystroke_intervals


# Duration multipliers, not behavioral identities. Balanced preserves existing
# input timing; actionability, network timeouts and retry policy are independent.
TIMING_PRESETS = {'slow': 1.5, 'balanced': 1.0, 'fast': .7, 'aggressive': .5}
POINTER_FRAME_SECONDS = 1 / 120


def calibration_for(settings, *, browsing=False):
    scope = settings.get('input_calibration_scope', 'off')
    return copy.deepcopy(settings.get('input_calibration')) if browsing and scope=='browsing' else None


def _sample_timing(rng, sample):
    return rng.triangular(sample['low'], sample['high'], sample['median'])


def _pointer_progress(t, profile=None):
    if t<=0:
        return 0.
    if t>=1:
        return 1.
    standard=t**3*(10-15*t+6*t*t)
    if not profile:
        return standard
    # Time-warp a smooth curve, then blend with the original. Validated powers
    # keep velocity and acceleration continuous at both ends. No trace replay.
    a=t**profile['start_power']
    b=(1-t)**profile['end_power']
    u=a/(a+b)
    return (standard+u**3*(10-15*u+6*u*u))/2


def _typing_timings(value, rng, preset, calibration=None):
    """HumanTyping intervals include the key hold, scaled by the saved profile."""
    scale = TIMING_PRESETS.get(preset, 1.0)
    for char, interval in zip(value, keystroke_intervals(value, rng)):
        hold_ms = min(rng.triangular(35, 125, 65), interval * 1000 - 25)
        gap_ms = interval * 1000 - hold_ms
        if calibration:
            # A short demonstration contributes half the timing, preserving
            # HumanTyping's word/key-distance variation and bounded defaults.
            hold_ms = (hold_ms + _sample_timing(rng, calibration['key_hold_ms'])) / 2
            gap_ms = (gap_ms + _sample_timing(rng, calibration['key_gap_ms'])) / 2
        yield char, hold_ms * scale, gap_ms / 1000 * scale


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
        self.preset = getattr(page.context, '_retail_input_timing_preset', 'balanced')
        self.duration_scale = TIMING_PRESETS.get(self.preset, 1.0)
        self.calibration = copy.deepcopy(getattr(page.context, '_retail_input_calibration', None))
        self.browsing = getattr(page.context, '_retail_browsing_input', False) is True
        self.rng = random.Random(seed)
        self.position = (0.0, 0.0)
        self.lock = asyncio.Lock()

    def click_delay(self):
        milliseconds = self.rng.triangular(35, 130, 65)
        if self.calibration:
            milliseconds = (milliseconds + _sample_timing(self.rng, self.calibration['click_hold_ms'])) / 2
        return milliseconds * self.duration_scale

    async def move(self, x, y):
        start_x, start_y = self.position
        dx, dy = x - start_x, y - start_y
        distance = math.hypot(dx, dy)
        if distance < 1:
            return
        duration = min(1.1, .18 + distance / self.rng.uniform(1000, 1700))
        bend = self.rng.choice((-1, 1)) * min(80, distance * self.rng.uniform(.12, .22))
        profile = self.calibration.get('pointer') if self.browsing and self.calibration else None
        if profile:
            learned = min(1.8, (profile['base_ms']+profile['ms_per_pixel']*distance)/1000)
            duration = (duration+learned)/2
            learned_bend = min(75, distance*profile['bend_fraction']*self.rng.uniform(.7, 1.3))
            bend = (bend+math.copysign(learned_bend, bend))/2
        duration *= self.duration_scale
        viewport = self.page.viewport_size or await self.page.evaluate('() => ({width:innerWidth,height:innerHeight})')
        started = time.monotonic()
        next_frame = started + POINTER_FRAME_SECONDS
        while True:
            # Include browser round-trip time in the cadence. Sample current
            # progress after a stall instead of replaying missed coordinates.
            await asyncio.sleep(max(0, min(next_frame, started + duration) - time.monotonic()))
            now = time.monotonic()
            t = min(1.0, (now - started) / duration)
            progress = _pointer_progress(t, profile)
            # The bend shares the easing so both axes start and stop smoothly.
            offset = math.sin(math.pi * progress) * bend if t < 1 else 0
            px = min(viewport['width'] - 1, max(0, start_x + dx*progress - dy/distance*offset))
            py = min(viewport['height'] - 1, max(0, start_y + dy*progress + dx/distance*offset))
            await self.page.mouse.move(px, py)
            self.position = (px, py)
            if t >= 1:
                break
            next_frame = now + POINTER_FRAME_SECONDS

    async def _prepare(self, target, position=None):
        if hasattr(target, 'wait_for_element_state'):
            await target.wait_for_element_state('visible', timeout=3000)
        else:
            await target.wait_for(state='visible', timeout=3000)
        viewport = self.page.viewport_size or await self.page.evaluate('() => ({width:innerWidth,height:innerHeight})')
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
                await asyncio.sleep(self.rng.uniform(.04, .09) * self.duration_scale)
            after = await target.bounding_box()
            if after and abs(after['y'] - box['y']) < 1:
                break  # Nested containers/scroll blockers need native scrolling.
        await target.scroll_into_view_if_needed(timeout=3000)
        box = await target.bounding_box()
        if not box:
            raise InteractionError('Interaction target disappeared before input')
        # Native click positions are relative to the padding box, whereas
        # bounding_box includes borders. Keep movement and click on one point.
        borders = await target.evaluate('''e => {
            const s=getComputedStyle(e);
            return {left:parseFloat(s.borderLeftWidth)||0, top:parseFloat(s.borderTopWidth)||0,
                    right:parseFloat(s.borderRightWidth)||0, bottom:parseFloat(s.borderBottomWidth)||0};
        }''')
        origin_x, origin_y = box['x'] + borders['left'], box['y'] + borders['top']
        if position is None:
            position = {}
            for axis, origin, size, end_border, start_border, extent in (
                ('x', origin_x, box['width'], borders['right'], borders['left'], viewport['width']),
                ('y', origin_y, box['height'], borders['bottom'], borders['top'], viewport['height']),
            ):
                low, high = max(0, -origin), min(size-start_border-end_border, extent-origin)
                if high <= low:
                    raise InteractionError('Interaction target has no visible interior')
                # Center-weighted variation, away from edges and rounded corners.
                # Scale the inset down for tiny controls instead of missing them.
                inset = (high-low)*.2
                position[axis] = self.rng.triangular(low+inset, high-inset, (low+high)/2)
        x, y = origin_x + position['x'], origin_y + position['y']
        await self.move(x, y)
        await asyncio.sleep(self.rng.uniform(.06, .18) * self.duration_scale)
        return position

    async def prepare(self, target):
        async with self.lock, asyncio.timeout(18 if self.preset == 'slow' else 12):
            return await self._prepare(target)

    async def scroll(self, delta):
        """Paced wheel input with a smooth cadence, cancellable between events."""
        async with self.lock, asyncio.timeout(5):
            viewport = self.page.viewport_size or await self.page.evaluate('() => ({width:innerWidth,height:innerHeight})')
            x, y = self.position
            if not self.browsing or not (.15*viewport['width'] < x < .85*viewport['width'] and .15*viewport['height'] < y < .85*viewport['height']):
                await self.move(viewport['width'] * self.rng.uniform(.4, .65), viewport['height'] * self.rng.uniform(.4, .65))
            duration = (self.rng.uniform(.25, .65) if self.browsing else .45) * self.duration_scale
            # One lock owns a bounded pointer/wheel pair. Wheel round trips must
            # not reduce the calibrated pointer's frame rate.
            origin_x, origin_y = self.position
            if self.browsing:
                end_x = min(.85*viewport['width'], max(.15*viewport['width'], origin_x +
                    self.rng.choice((-1, 1))*viewport['width']*self.rng.uniform(.035, .12)))
                end_y = min(.85*viewport['height'], max(.15*viewport['height'], origin_y +
                    self.rng.choice((-1, 1))*viewport['height']*self.rng.uniform(.025, .08)))
                actions = [asyncio.create_task(self.move(end_x, end_y)),
                           asyncio.create_task(self._wheel(delta, duration))]
                try:
                    await asyncio.gather(*actions)
                finally:
                    # Stop, timeout, or failure joins both before releasing the
                    # controller. No mouse input survives into the next action.
                    for action in actions:
                        if not action.done():
                            action.cancel()
                    await asyncio.gather(*actions, return_exceptions=True)
            else:
                await self._wheel(delta, duration)

    async def _wheel(self, delta, duration):
        start, sent = time.monotonic(), 0.0
        while True:
            t = min(1., (time.monotonic()-start)/duration)
            target = delta * (t*t*(3-2*t))
            await self.page.mouse.wheel(0, target-sent)
            sent = target
            if t >= 1:
                break
            await asyncio.sleep(1/60)

    async def click(self, target, **kwargs):
        async with self.lock, asyncio.timeout(23 if self.preset == 'slow' else 15):
            position = await self._prepare(target, kwargs.pop('position', None))
            # Locator/ElementHandle checks still decide where and whether to click.
            # A failure is never retried by this layer.
            kwargs.setdefault('delay', self.click_delay())
            await target.click(position=position, **kwargs)

    async def fill(self, target, value):
        async with self.lock, asyncio.timeout(65 if self.preset == 'slow' else 45):
            # Complex Unicode, special input types and long values retain native
            # fill semantics rather than synthesizing incorrect keyboard events.
            input_type = (await target.get_attribute('type') or 'text').lower()
            if len(value) > 128 or not value.isascii() or any(ord(c) < 32 for c in value) or input_type not in ('text', 'password', 'email', 'search', 'tel', 'url'):
                await target.fill(value)
                return
            position = await self._prepare(target)
            # press() focuses without checking whether a pointer can reach the
            # field. Click it first so overlays/disabled controls stop input.
            if not await target.is_editable():
                raise InteractionError('Paced input target is not editable')
            await target.click(position=position, delay=self.click_delay(), timeout=3000)
            if await target.input_value():
                await target.fill('')
            # One visibly corrected slip in ordinary prose/identity fields only.
            # Never perturb passwords, OTPs, payment details or short codes.
            metadata=await target.evaluate("e=>({autocomplete:e.autocomplete||'',name:e.name||'',id:e.id||'',label:e.getAttribute('aria-label')||''})")
            sensitive=any(word in ' '.join(metadata.values()).lower() for word in
                          ('password','otp','one-time','code','card','cc-','cvv','cvc','payment'))
            eligible=[i for i,c in enumerate(value) if c.isalpha() and c.isascii() and 0<i<len(value)-1]
            slip=self.rng.choice(eligible) if input_type in ('text','email','search') and len(value)>=5 and eligible and not sensitive else -1
            for index,(char, hold_ms, pause) in enumerate(_typing_timings(value, self.rng, self.preset, self.calibration)):
                if index==slip:
                    wrong=self.rng.choice([c for c in 'abcdefghijklmnopqrstuvwxyz' if c!=char.lower()])
                    await target.press(wrong,delay=hold_ms,timeout=3000)
                    await asyncio.sleep(self.rng.uniform(.12,.25)*self.duration_scale)
                    await target.press('Backspace',delay=hold_ms,timeout=3000)
                await asyncio.sleep(pause)
                # A locator press focuses the intended field each time. Its
                # chord also releases modifiers after shifted characters.
                await target.press(_key_chord(char),
                                   delay=hold_ms, timeout=3000)
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
        return await paced.prepare(target)
