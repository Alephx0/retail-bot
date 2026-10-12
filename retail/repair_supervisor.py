"""Launch one offline repair at idle priority; never import model-authored runtime code."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys

from .store import now


class RepairSupervisor:
    def __init__(self,store):
        self.store=store;self.process=None;self.active_id=None;self.next_check=0
        for old in store.all('repair_queue'):
            if old.get('status') in {'preparing','generating','validating'}:
                store.put('repair_queue',{**old,'status':'needs_review','failure_type':'WorkerInterrupted','updated_at':now()})

    async def tick(self, *, idle):
        if self.process:
            if self.process.returncode is None:return
            job=self.store.get('repair_queue',self.active_id)
            if job and job['status'] in {'preparing','generating','validating','pending'}:
                self.store.put('repair_queue',{**job,'status':'needs_review','failure_type':'WorkerExited'})
            self.process=None;self.active_id=None
        import time
        if not idle or time.monotonic()<self.next_check:return
        self.next_check=time.monotonic()+30
        settings=self.store.get('settings','settings') or {}
        if not idle or not settings.get('auto_source_repair',True) or not settings.get('ai_connection_id'):return
        job=next((j for j in self.store.all('repair_queue') if j.get('status')=='pending' and not j.get('attempts')),None)
        if not job:return
        root=Path(__file__).resolve().parents[1]
        env={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA'}}
        self.process=await asyncio.create_subprocess_exec(sys.executable,str(root/'scripts/repair_worker.py'),
            '--data-dir',str(self.store.folder.resolve()),'--incident',job['id'],cwd=root,env=env,
            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0))
        self.active_id=job['id']

    async def close(self):
        # The worker is independent and touches only its isolated checkout. Give it
        # its bounded validation window; shutdown never kills a test browser mid-write.
        if self.process and self.process.returncode is not None:await self.process.wait()
