"""Consume one verified incident in a separate, bounded offline repair process."""
import argparse
import asyncio
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from retail.source_repair import snapshot, propose, validate
from retail.store import Store, now


async def work(data_dir,identifier):
    store=Store(Path(data_dir))
    try:
        job=store.get('repair_queue',identifier)
        if not job or job.get('status')!='pending' or job.get('attempts',0):return
        settings=store.get('settings','settings') or {}
        connection=store.get('ai_connections',settings.get('ai_connection_id',''))
        if not connection:raise ValueError('No configured repair model')
        job={**job,'status':'preparing','attempts':1,'updated_at':now()};store.put('repair_queue',job)
        # The queue identifier is a host-generated hash, never model/page input.
        import re
        if not re.fullmatch(r'repair-[a-f0-9]{24}',identifier):raise ValueError('Invalid incident identifier')
        checkout=ROOT.parent/'.retail-repairs'/identifier
        base=await asyncio.to_thread(snapshot,ROOT,checkout)
        job.update(status='generating',checkout=str(checkout));store.put('repair_queue',job)
        test=await propose(connection,checkout,job)
        job.update(status='validating');store.put('repair_queue',job)
        report=await asyncio.to_thread(validate,checkout,base,test)
        job.update(status=report['status'],report=str(checkout/'artifacts/source-repair/report.json'),updated_at=now())
        store.put('repair_queue',job)
    except Exception as exc:
        if job:store.put('repair_queue',{**job,'status':'needs_review','failure_type':type(exc).__name__,'updated_at':now()})
        raise
    finally:store.db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True);parser.add_argument('--incident',required=True)
    args=parser.parse_args()
    try:asyncio.run(work(args.data_dir,args.incident))
    except Exception:sys.exit(1)  # Provider payloads and local exception strings stay out of app logs.
