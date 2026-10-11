"""Encrypted, bounded failure evidence. No production code is rewritten."""
import base64

from .store import now


class Diagnostics:
    def __init__(self,store): self.store=store

    async def capture(self, task, page, action, error):
        from .browser_mcp import BrowserTools, AMAZON_ACTIONS
        from .models import DOMAINS
        from .recovery_evidence import public_url
        import asyncio
        expected = getattr(page, '_retail_expected_action', action)
        record = {'task_id': task['id'], 'action': expected, 'stage': action,
                  'error': type(error).__name__, 'at': now(), 'status': 'needs_review',
                  'url': public_url(getattr(page, 'url', '')), 'dom': [], 'schema_version': 1}
        # No screenshot/full AX tree on the recovery path. Explicit user traces
        # remain encrypted and never form part of an AI/source-repair export.
        try:
            if expected in AMAZON_ACTIONS:
                async with asyncio.timeout(2):
                    browser = BrowserTools(page, expected, set(DOMAINS.values()), AMAZON_ACTIONS)
                    record['dom'] = (await browser.observe_controls())['controls']
        except Exception:
            record['capture_note'] = 'Bounded control evidence unavailable'
        return self.store.put_bounded('diagnostics', record)

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
