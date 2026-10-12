import asyncio
from pathlib import Path
import random

import pytest
from patchright.async_api import async_playwright

from retail import behavior
from retail._vendor.humantyping import keystroke_intervals
from retail._vendor.humantyping.timing import MAX_INTERVAL, MIN_INTERVAL


def test_timing_is_bounded_repeatable_and_independent():
    text = 'The quick brown fox! ' * 6
    global_state = random.getstate()
    first = list(keystroke_intervals(text, random.Random(40)))
    assert len(first) == len(text)
    assert all(MIN_INTERVAL <= value <= MAX_INTERVAL for value in first)
    assert len(set(first)) > 10
    list(keystroke_intervals('another account', random.Random(5)))
    assert first == list(keystroke_intervals(text, random.Random(40)))
    assert first != list(keystroke_intervals(text, random.Random(41)))
    assert random.getstate() == global_state
    assert list(keystroke_intervals('', random.Random(40))) == []


def test_context_changes_timing_without_random_noise():
    class MeanRandom:
        def gauss(self, mean, deviation):
            return mean

    def intervals(text):
        return list(keystroke_intervals(text, MeanRandom()))

    assert intervals('the')[0] < intervals('trunk')[0]  # Common word.
    assert intervals('zqx')[0] > intervals('trunk')[0]  # Complex word.
    assert intervals('he')[1] < intervals('hx')[1]  # Common bigram.
    assert intervals('df')[1] < intervals('dp')[1]  # Nearby vs distant keys.
    assert intervals('T')[0] > intervals('t')[0]
    assert intervals('a a')[1] > intervals('aaa')[1]  # Word pause.
    assert intervals('k' * 128)[-1] > intervals('k' * 128)[0]  # Fatigue.


@pytest.mark.parametrize('wpm', [0, -1, float('nan'), float('inf')])
def test_invalid_timing_speed(wpm):
    with pytest.raises(ValueError, match='positive finite'):
        list(keystroke_intervals('fixture', random.Random(1), target_wpm=wpm))


@pytest.mark.parametrize('width', [1000, 390])
def test_typing_cancellation_context_isolation_and_exact_values(width):
    async def scenario():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(channel='chrome')
            contexts = [await browser.new_context(viewport={'width': width, 'height': 700})
                        for _ in range(2)]
            try:
                pages = []
                for context in contexts:
                    context._retail_paced_input = True
                    page = await context.new_page()
                    await page.set_content('''<!doctype html><html><meta name="viewport" content="width=device-width">
                        <style>body{font:18px sans-serif;margin:24px}label{display:block;margin:16px 0}
                        input{display:block;box-sizing:border-box;width:100%;max-width:420px;padding:10px}</style>
                        <h1>Paced input fixture</h1><form onsubmit="window.submits++;return false">
                        <label>Text<input id="text" value="Replace this"></label>
                        <label>Limited input<input id="limited" maxlength="2"></label>
                        <label>Native fallback<input id="native"></label></form>
                        <script>window.submits=0;window.keys=[];
                        document.addEventListener('keydown',e=>keys.push(e.key));</script></html>''')
                    page._retail_paced_controller = behavior.PacedInput(page, seed=40)
                    pages.append(page)
                first, second = pages
                field = first.locator('#text')
                interrupted = asyncio.create_task(behavior.fill(first, field, 'x' * 100))
                independent = asyncio.create_task(behavior.fill(second, second.locator('#text'), 'the Fixture+42!'))
                try:
                    await first.wait_for_function("document.querySelector('#text').value.startsWith('x')", timeout=5000)
                    interrupted.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await interrupted
                    # Allow an already-dispatched key's release to complete.
                    await asyncio.sleep(.2)
                    partial = await field.input_value()
                    await asyncio.sleep(.3)
                    assert await field.input_value() == partial
                    assert 0 < len(partial) < 100
                    assert not behavior.controller(first).lock.locked()
                    await independent
                finally:
                    for task in (interrupted, independent):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(interrupted, independent, return_exceptions=True)
                assert await second.locator('#text').input_value() == 'the Fixture+42!'
                await behavior.fill(first, field, '')
                assert await field.input_value() == ''
                await behavior.fill(first, field, 'the Fixture+42!')
                assert await field.input_value() == 'the Fixture+42!'
                # A field that transforms/truncates input fails without retyping.
                with pytest.raises(behavior.InteractionError, match='requested value'):
                    await behavior.fill(first, first.locator('#limited'), '123')
                assert await first.locator('#limited').input_value() == '12'
                keys = await first.evaluate('keys', isolated_context=False)
                assert keys[-3:] == ['1', '2', '3']
                assert not {'Enter', 'Backspace', 'ArrowLeft', 'ArrowRight'} & set(keys)
                for value in ('x' * 129, 'line\tbreak', 'Café 東京'):
                    await behavior.fill(first, first.locator('#native'), value)
                    assert await first.locator('#native').input_value() == value
                assert await first.evaluate('keys', isolated_context=False) == keys
                assert await first.evaluate('submits', isolated_context=False) == 0
                assert await second.evaluate('submits', isolated_context=False) == 0
                output = Path('artifacts/humantyping')
                output.mkdir(parents=True, exist_ok=True)
                await first.screenshot(path=str(output / f'fixture-{width}.png'))
            finally:
                await browser.close()
    asyncio.run(asyncio.wait_for(scenario(), 30))
