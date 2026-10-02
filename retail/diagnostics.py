"""Encrypted, bounded failure evidence. No production code is rewritten."""
import base64
from urllib.parse import urlsplit, urlunsplit

from .store import now


class Diagnostics:
    def __init__(self,store): self.store=store

    async def capture(self,task,page,action,error):
        record={'task_id':task['id'],'action':getattr(page,'_retail_expected_action',action),'stage':action,'previous_locator':getattr(page,'_retail_previous_locator',''),'error':str(error)[:500], 'at':now(),'status':'needs_review'}
        try:
            u=urlsplit(page.url);record['url']=urlunsplit((u.scheme,u.netloc,u.path,'',''))
            if '/checkout/' in u.path:
                from .browser_mcp import PriceTools
                try:
                    pricing = PriceTools(page, {u.hostname})
                    record['price_candidates'] = (await pricing.observe_price_rows())['price_rows']
                except Exception:
                    record['price_candidates'] = []
            # Capture semantic controls only, excluding input values, scripts,
            # cookies, hidden fields, and arbitrary page/customer text.
            record['dom']=await page.locator('button,input[type=submit],input[type=button],select,a[href],[role=button],[role=link]').evaluate_all("els=>els.filter(e=>e.getClientRects().length).slice(0,500).map(e=>({tag:e.tagName,role:e.getAttribute('role'),id:e.id,name:e.getAttribute('name'),label:(e.getAttribute('aria-label')||(e.matches('input')?e.value:e.innerText)||'').slice(0,120)}))")
            record['accessibility']=await page.locator('body').aria_snapshot(timeout=3000)
            record['accessibility']=record['accessibility'][:20000]
            # Mask customer content, retain control placement. Stored encrypted.
            mask=page.locator('input,textarea,[contenteditable],p,span,a,td')
            record['screenshot']=base64.b64encode(await page.screenshot(mask=[mask],timeout=3000)).decode()
        except Exception:
            record['capture_note']='Some evidence could not be captured before the browser closed'
        return self.store.put('diagnostics',record)

    async def finish_trace(self, task_id, context):
        import tempfile
        from pathlib import Path
        if not getattr(context,'_retail_tracing',False): return
        with tempfile.TemporaryDirectory(prefix='retail-trace-') as directory:
            path=Path(directory)/'trace.zip'
            try:
                await context.tracing.stop(path=str(path))
                if path.stat().st_size<=25_000_000:
                    self.store.put('diagnostics',{'task_id':task_id,'action':'BROWSER_TRACE','at':now(),'status':'captured','trace':base64.b64encode(path.read_bytes()).decode()})
            except Exception:
                self.store.event(task_id,'diagnostic_error','Browser trace could not be captured')
