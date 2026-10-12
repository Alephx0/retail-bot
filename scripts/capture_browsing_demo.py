"""Visible, isolated browsing demonstration; local timing/geometry only."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time

from patchright.async_api import async_playwright


TYPES = {'move', 'click', 'wheel', 'scroll', 'key', 'page'}
PAGES = {'home', 'search', 'product'}
FIELDS = {'t', 'x', 'y', 'w', 'h', 'hold', 'flight', 'rx', 'ry', 'tw', 'th', 'dx', 'dy', 'mode'}


def sanitize(event):
    if not isinstance(event, dict) or event.get('type') not in TYPES or event.get('page') not in PAGES:
        return None
    clean = {'type': event['type'], 'page': event['page']}
    for key in FIELDS:
        value = event.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) < (1e14 if key=='t' else 1e7):
            clean[key] = round(value, 3)
    if event.get('target') in {'search','variant','details','reviews','product','control','page'}:
        clean['target'] = event['target']
    if event.get('category') in {'character','backspace','enter','navigation'}:
        clean['category'] = event['category']
    return clean


async def main(folder):
    folder.mkdir(parents=True, exist_ok=True)
    started = time.time()
    recording, paused, count = True, False, 0
    counts = {}
    connected_pages = 0
    stream = (folder/'events.jsonl').open('a', encoding='utf-8', buffering=1)

    def status():
        value = {'pid': os.getpid(), 'recording': recording, 'paused': paused, 'events': count, 'counts': counts, 'connected_pages': connected_pages,
                 'started_at': started, 'updated_at': time.time(), 'expires_at': started+900}
        temp = folder/'status.tmp'
        temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
        temp.replace(folder/'status.json')
        return value

    def batch(events):
        nonlocal recording, count
        if time.time()-started >= 900 or count>=150000:
            recording = False
        if recording and isinstance(events, list):
            for event in events[:1000]:
                clean = sanitize(event)
                if clean:
                    stream.write(json.dumps(clean, separators=(',', ':'))+'\n')
                    count += 1
                    counts[clean['type']] = counts.get(clean['type'], 0)+1
        return status()

    def finish():
        nonlocal recording
        recording = False
        stream.flush()
        return status()

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=False, args=['--start-maximized'])
        disconnected = asyncio.Event()
        browser.on('disconnected', lambda _: disconnected.set())
        context = await browser.new_context(no_viewport=True)
        await context.add_init_script(path=str(Path(__file__).with_name('browser_demo_capture.js')))
        page = await context.new_page()
        try:
            await page.goto('https://www.amazon.com/', wait_until='domcontentloaded', timeout=30000)
        except Exception:
            pass  # Leave the visible window available for manual navigation.
        await page.bring_to_front()
        status()
        sessions = {}
        try:
            while not disconnected.is_set():
                connected_pages = 0
                for tab in context.pages:
                    if tab.is_closed():
                        continue
                    try:
                        session = sessions.get(tab)
                        if session is None:
                            session = sessions[tab] = await context.new_cdp_session(tab)
                        async with asyncio.timeout(2):
                            result = await session.send('Runtime.evaluate', {'expression': 'window.__retailDemoDrain?.() || null', 'returnByValue': True})
                            captured = result.get('result', {}).get('value') or {}
                            connected_pages += int('events' in captured)
                            batch(captured.get('events', []))
                            for command in captured.get('commands', []):
                                if command=='finish':
                                    finish()
                                elif command in {'pause','resume'}:
                                    paused = command=='pause'
                            if time.time()-started>=900:
                                finish()
                            await session.send('Runtime.evaluate', {'expression': 'window.__retailDemoSetState?.('+json.dumps({'recording':recording,'paused':paused})+')'})
                    except Exception:
                        pass  # Navigation briefly destroys execution contexts.
                status()
                try:
                    await asyncio.wait_for(disconnected.wait(), timeout=.25)
                except TimeoutError:
                    pass
        finally:
            finish()
            stream.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(main(parser.parse_args().output))
