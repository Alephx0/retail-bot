"""Compare isolated CVF preparation against a source snapshot; no provider calls."""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from patchright.async_api import async_playwright
from retail import signup_challenges as patched
from retail.account_creation import validate_target
from test_signup_cvf_captcha import setup,SOLVER


async def run(args):
    spec=importlib.util.spec_from_file_location('retail._baseline_cvf',args.baseline_root/'retail/signup_challenges.py')
    baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
    samples={'baseline':[],'patched':[]}
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True);context,page=await setup(browser)
        for index in range(60):
            variants=[('baseline',baseline),('patched',patched)]
            if index%2:variants.reverse()
            for name,module in variants:
                started=time.perf_counter()
                prepared=await module.prepare_token(page,'arkose',{'region':'US'},SOLVER,validate_target)
                elapsed=(time.perf_counter()-started)*1000
                if index>=10:samples[name].append(elapsed)
                for key in ('field','form','challenge_frame'):await prepared[key].dispose()
        await browser.close()
    report={'samples_per_variant':50,'scope':'local CVF preparation; excludes solving and live network time'}
    for name,values in samples.items():report[name]={'median_ms':statistics.median(values),'p95_ms':sorted(values)[47]}
    report['p95_delta_ms']=report['patched']['p95_ms']-report['baseline']['p95_ms']
    report['gate']='pass' if report['p95_delta_ms']<=10 else 'fail'
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))
    if report['gate']!='pass':raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--baseline-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('artifacts/solver-retry/performance.json'))
    asyncio.run(run(parser.parse_args()))
