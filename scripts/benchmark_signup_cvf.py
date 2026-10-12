"""Controlled CVF preparation timing; no provider calls or retailer requests."""
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from patchright.async_api import async_playwright
from retail import signup_challenges as challenges
from retail.account_creation import validate_target
from test_signup_cvf_captcha import setup,SOLVER


async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        context,page=await setup(browser)
        samples=[]
        for index in range(25):
            start=time.perf_counter()
            prepared=await challenges.prepare_token(page,'arkose',{'region':'US'},SOLVER,validate_target)
            if index>=5:samples.append((time.perf_counter()-start)*1000)
            for name in ('field','form','challenge_frame'):await prepared[name].dispose()
        ordered=sorted(samples)
        result={'samples':20,'warmup':5,'median_ms':statistics.median(samples),
                'p95_ms':ordered[18],'max_ms':max(samples),'samples_ms':samples,
                'scope':'Controlled CVF preparation including browser protocol overhead; no solver, submission or live retailer requests.'}
        output=ROOT/'artifacts'/'cvf-review';output.mkdir(parents=True,exist_ok=True)
        (output/'timings.json').write_text(json.dumps(result,indent=2))
        print(json.dumps({key:value for key,value in result.items() if key!='samples_ms'}))
        await context.close();await browser.close()


if __name__=='__main__':asyncio.run(main())
