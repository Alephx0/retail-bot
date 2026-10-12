"""Local pointer cadence comparison; no retailer traffic or purchases."""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patchright.async_api import async_playwright
from retail.behavior import PacedInput, TIMING_PRESETS
from retail.visible_mouse import install


async def run(label):
    rows = []
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        context = await browser.new_context(viewport={'width':1000, 'height':700})
        page = await context.new_page()
        await install(context)
        await page.goto('data:text/html,<body style="background:%23172033;color:white"><h1>Pointer cadence fixture</h1>')
        await page.evaluate("window.samples=[];document.addEventListener('mousemove',e=>samples.push({t:performance.now(),x:e.clientX,y:e.clientY}))", isolated_context=False)
        for preset in TIMING_PRESETS:
            context._retail_input_timing_preset = preset
            for seed in range(3):
                await page.mouse.move(60, 60)
                await page.evaluate('samples=[]', isolated_context=False)
                controller = PacedInput(page, seed=seed)
                controller.position = (60, 60)
                started = time.perf_counter()
                await controller.move(900, 550)
                elapsed = (time.perf_counter() - started) * 1000
                samples = await page.evaluate('samples', isolated_context=False)
                gaps = [b['t']-a['t'] for a,b in zip(samples,samples[1:])]
                rows.append({'preset':preset, 'seed':seed, 'elapsed_ms':round(elapsed,2),
                             'events':len(samples), 'median_gap_ms':round(statistics.median(gaps),2),
                             'p95_gap_ms':round(sorted(gaps)[int((len(gaps)-1)*.95)],2),
                             'samples':samples})
        await browser.close()
    output = Path('artifacts/pointer-smoothing')
    output.mkdir(parents=True, exist_ok=True)
    (output/f'{label}.json').write_text(json.dumps(rows, indent=2))
    print(json.dumps([{k:v for k,v in row.items() if k != 'samples'} for row in rows], indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', choices=['before','after'], required=True)
    asyncio.run(run(parser.parse_args().label))
