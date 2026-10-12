"""Local browser fixture: state-read overhead and unavailable-MCP reproduction."""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from patchright.async_api import async_playwright
from retail import account_creation, signup_recovery
from retail.amazon import Amazon
from retail.store import Store


def load(root,name):
    spec=importlib.util.spec_from_file_location('retail._baseline_'+name,root/'retail'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


async def run(args):
    baseline=load(args.baseline_root,'account_creation')
    old_recovery=load(args.baseline_root,'signup_recovery')
    samples={'baseline':[],'patched':[]}
    with tempfile.TemporaryDirectory(prefix='signup-error-benchmark-') as temporary:
        store=Store(Path(temporary));adapter=Amazon(store)
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True);page=await browser.new_page()
            body='<form><input id=ap_customer_name name=customerName><input id=ap_password type=password></form>'
            await page.route('**/*',lambda r:r.fulfill(body=body,content_type='text/html'))
            await page.goto('https://www.amazon.com/ap/register')
            for index in range(args.samples+10):
                variants=[('baseline',baseline.signup_state),('patched',account_creation.signup_state)]
                if index%2:variants.reverse()
                for name,read in variants:
                    started=time.perf_counter();await read(page)
                    if index>=10:samples[name].append((time.perf_counter()-started)*1000)
            await page.set_content('<div id=auth-error-message-box>Internal error please try again.</div>'+body)
            def unavailable(*_):raise RuntimeError('MCP unavailable in reproduction fixture')
            old_recovery.ScopedMCP=unavailable
            account={'id':'fixture','region':'US'}
            try:
                await old_recovery.recover_signup(adapter,account,page,lambda *a,**kw:None)
                reproduction='unexpected_success'
            except RuntimeError:reproduction='known_page_failed_at_mcp_connection'
            result=await signup_recovery.recover_signup(adapter,account,page,lambda *a,**kw:None)
            await browser.close()
        store.db.close()
    report={'samples_per_variant':args.samples,'scope':'local registration state reads; excludes network/provider time',
            'baseline_reproduction':reproduction,'patched_checkpoint':result}
    for name,values in samples.items():
        report[name]={'median_ms':statistics.median(values),'p95_ms':sorted(values)[int(.95*(len(values)-1))]}
    report['p95_delta_ms']=report['patched']['p95_ms']-report['baseline']['p95_ms']
    report['gate']='pass' if report['p95_delta_ms']<=10 else 'fail'
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))
    if report['gate']!='pass':raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--baseline-root',type=Path,required=True)
    parser.add_argument('--samples',type=int,default=50)
    parser.add_argument('--output',type=Path,default=Path('artifacts/signup-registration-error/performance.json'))
    asyncio.run(run(parser.parse_args()))
