"""Real configured AI + Chrome DevTools MCP, intercepted storefront, offline source repair.

No retailer, account, payment, proxy or mail requests. The only remote request is the
configured AI API, receiving approved control labels and the permitted source component.
"""
from contextlib import ExitStack
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from patchright.async_api import async_playwright
from retail.browser_agent import BrowserAgent
from retail.interactions import resolve,InteractionError
from retail.source_repair import snapshot,propose,validate
from retail.store import Store
from retail.workflow_recovery import RecoveryRequest,inspect_recovery


async def demo(data_dir,checkout):
    configured=Store(data_dir)
    settings=configured.get('settings','settings') or {}
    connection=configured.get('ai_connections',settings.get('ai_connection_id',''))
    configured.db.close()
    if not connection:raise ValueError('A configured model connection is required for the actual AI demonstration')
    # Replay the pre-repair component kept in the validated package when rerunning.
    deterministic=resolve
    package=checkout/'artifacts/source-repair/package.json'
    if package.is_file():
        import types
        module=types.ModuleType('retail.acceptance_before');module.__package__='retail'
        saved=json.loads(package.read_text(encoding='utf-8'))
        exec(compile(saved['before']['retail/interactions.py'],'acceptance_before','exec'),module.__dict__)
        deterministic=module.resolve
    report={'fixture':'Intercepted storefront.test; no retailer or purchase traffic','model':connection['model']}
    with tempfile.TemporaryDirectory(prefix='retail-recovery-demo-') as folder, ExitStack() as cleanup:
        store=Store(Path(folder));cleanup.callback(store.db.close);store.put('settings',{'ai_connection_id':connection['id']},'settings');store.put('ai_connections',connection)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True,args=[f'--remote-debugging-port={port}'])
            try:
                page=await browser.new_page();context=page.context
                context._retail_recovery_endpoint=f'http://127.0.0.1:{port}';context._retail_task_id='local-acceptance'
                markup='''<h1>Fixture product</h1><button onclick="document.body.dataset.cart='verified'">Add this product to cart</button>'''
                await page.route('**/*',lambda r:r.fulfill(body=markup,content_type='text/html'))
                await page.goto('https://storefront.test/product')
                started=time.perf_counter()
                try:await deterministic(page,'ADD_TO_CART')
                except InteractionError:report['deterministic_failed_before']=True
                else:raise AssertionError('The demonstration must reproduce a deterministic failure')
                report['detection_ms']=round((time.perf_counter()-started)*1000,2)
                agent=BrowserAgent(store)
                request=RecoveryRequest('ADD_TO_CART',frozenset({'storefront.test'}),{'ADD_TO_CART':r'^add this product to cart$'})
                async def verified(page):
                    await page.wait_for_function("document.body.dataset.cart==='verified'",timeout=3000)
                    return True
                started=time.perf_counter()
                await inspect_recovery(agent,page,request,execute=True,verify=verified)
                report.update(recovery_ms=round((time.perf_counter()-started)*1000,2),
                    real_model_recovery=True,transport='chrome-devtools-mcp',same_context=page.context is context,
                    recovery_model_steps=store.all('agent_runs')[0]['steps'])
                incident=store.all('repair_queue')[0]
                print(json.dumps({'stage':'recovered','recovery_ms':report['recovery_ms'],'model_steps':report['recovery_model_steps']}),flush=True)
                existing=checkout/'artifacts/source-repair/report.json'
                if existing.is_file():
                    validation=json.loads(existing.read_text(encoding='utf-8'))
                    if validation['status']!='ready_for_review':raise ValueError('Existing repair did not pass validation')
                else:
                    baseline=await asyncio.to_thread(snapshot,ROOT,checkout)
                    test=await propose(connection,checkout,incident)
                    print(json.dumps({'stage':'model_authored_repair','test':test}),flush=True)
                    validation=await asyncio.to_thread(validate,checkout,baseline,test)
                report.update(repair_validation=validation,repair_checkout=str(checkout))
                # Reload only the offline repaired component into this fixture process.
                spec=importlib.util.spec_from_file_location('retail.acceptance_repaired',checkout/'retail/interactions.py')
                repaired=importlib.util.module_from_spec(spec);spec.loader.exec_module(repaired)
                await page.reload();before_calls=len(store.all('agent_runs'));started=time.perf_counter()
                control=await repaired.resolve(page,'ADD_TO_CART');await control.click();await verified(page)
                report.update(deterministic_after_ms=round((time.perf_counter()-started)*1000,2),
                              deterministic_passed_after=True,repeat_ai_calls=len(store.all('agent_runs'))-before_calls)
            finally:await browser.close()
        store.db.close()
    output=ROOT/'artifacts/overhaul/self-healing-demo.json';output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'stage':'complete','report':str(output),'repeat_ai_calls':report['repeat_ai_calls']}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data-dir',required=True,type=Path);p.add_argument('--checkout',required=True,type=Path)
    args=p.parse_args();asyncio.run(demo(args.data_dir,args.checkout))
