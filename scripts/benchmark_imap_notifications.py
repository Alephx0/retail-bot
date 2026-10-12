"""Local IMAP wire benchmark; synthetic email and deliberate fixture latency."""
import asyncio
import imaplib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from pytest import MonkeyPatch
from retail import identity
from test_imap_notifications import setup, mail


async def sample(root, mode):
    patch = MonkeyPatch()
    server, store, service, account = await setup(root, patch)
    server.command_latency = .02  # Controlled 20 ms per command, not production pacing.
    for _ in range(39):server.add(mail(recipient='other@example.test'))
    since = time.time()-1
    def connect(m):
        client = imaplib.IMAP4(m['host'], m['port'], timeout=3)
        client.login(m['username'], m['password'])
        client.select('INBOX', readonly=True)
        return client
    patch.setattr(identity, 'imap_connect', connect)
    async def legacy():
        while True:
            try:
                return await service.code(account, since=since)
            except ValueError:
                await asyncio.sleep(3)  # Former signup polling delay, benchmark baseline only.
    job = asyncio.create_task(legacy() if mode == 'before' else service.wait_code(account, since=since, timeout=10))
    try:
        await asyncio.wait_for((server.searched if mode == 'before' else server.idling).wait(), 5)
        start = time.perf_counter()
        server.add(mail())
        assert (await asyncio.wait_for(job, 8))['code'] == '123456'
        return {'arrival_to_code_ms': (time.perf_counter()-start)*1000,
                'connections':server.logins, 'fetch_commands':len(server.fetches)}
    finally:
        job.cancel();await asyncio.gather(job, return_exceptions=True)
        await server.close();store.db.close();patch.undo()


async def main():
    result = {'scope':'Local wire fixture, 39 unrelated messages, 20 ms per command. Delivery after the first empty candidate search for the former polling path, after IDLE starts for the reactive path. No live network, provider delivery or browser typing time.'}
    with tempfile.TemporaryDirectory() as folder:
        for mode in ('before','after'):
            rows = [await sample(Path(folder)/f'{mode}-{i}',mode) for i in range(5)]
            result[mode] = {'samples':5,'median_ms':round(statistics.median(row['arrival_to_code_ms'] for row in rows),1),
                            'max_ms':round(max(row['arrival_to_code_ms'] for row in rows),1),'runs':rows}
    output=ROOT/'artifacts'/'imap-review';output.mkdir(parents=True,exist_ok=True)
    (output/'notification-timings.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))


if __name__=='__main__':asyncio.run(main())
