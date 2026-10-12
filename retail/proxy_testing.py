"""Bounded proxy test jobs and rolling observations, independent of account state."""
import asyncio
from datetime import datetime, timezone, timedelta
import statistics
import time

from .proxy_probe import NEUTRAL, TRANSIENT, destination, probe
from .proxy_protocol import parse_proxy
from .store import now

FRESH_SECONDS = 900
HISTORY_LIMIT = 20


def summarize(history):
    successes = [x for x in history if x['status']=='healthy']
    last = history[-1]
    latencies = [x['latency_ms'] for x in successes]
    recent = history[-3:]
    if last['status']=='authentication_failed': status='authentication_failed'
    elif all(x['status']!='healthy' for x in recent) and len(recent)>=3: status='unreachable'
    elif len(successes)==len(history): status='healthy'
    else: status='degraded'
    return dict(status=status, success_rate=round(100*len(successes)/len(history),1),
                samples=len(history), timeout_rate=round(100*sum(x.get('error')=='timeout' for x in history)/len(history),1),
                jitter_ms=round(statistics.pstdev(latencies),2) if latencies else None,
                latency_ms=round(statistics.median(latencies),2) if latencies else None,
                last_success=successes[-1]['observed_at'] if successes else None,
                last_tested=last['observed_at'],fresh_until=(datetime.fromisoformat(last['observed_at'])+timedelta(seconds=FRESH_SECONDS)).isoformat())


class ProxyTesting:
    def __init__(self, store):
        self.store=store
        self.jobs={}
        self._slots=None
        self._cooldowns={}
        self._next_maintenance=0
        self.background_group=None

    def busy(self, record_id):
        return any(key.endswith(':'+record_id) for key in self.jobs)

    async def ready_route(self, pool_id, owner, *, progress=lambda message: None):
        """Refresh expired healthy evidence once before a new signup session.

        Bound routes and fresh selections retain the synchronous fast path.
        Join shared work without cancelling another account's connectivity check.
        """
        from .proxy_pool import ProxyPool, ProxySelectionError
        pool=ProxyPool(self.store)
        try:
            return pool.choose(pool_id,owner)
        except ProxySelectionError as exc:
            if exc.code!='proxy_stale':raise
        progress('Rechecking the assigned proxy before opening signup.')
        jobs=[job for key,job in self.jobs.items() if key.endswith(':'+pool_id)]
        if jobs:
            if self.background_group==pool_id:self.background_group=None
            await asyncio.shield(asyncio.gather(*jobs,return_exceptions=True))
        try:
            return pool.choose(pool_id,owner)
        except ProxySelectionError as exc:
            if exc.code!='proxy_stale':raise
            fingerprints=list(exc.stale[:5])
        record=self.store.get('proxies',pool_id)
        if not record:return pool.choose(pool_id,owner)
        if len(self.jobs)>=3:
            raise ProxySelectionError('proxy_check_busy', 'Proxy checks are busy. Wait for a check to finish, then start signup again.')
        await self.start(record,'amazon',fingerprints=fingerprints,force=False)
        await asyncio.shield(self.jobs['connectivity:'+pool_id])
        try:
            return pool.choose(pool_id,owner)
        except ProxySelectionError:
            raise ProxySelectionError('proxy_check_failed', 'The assigned proxy did not pass the startup connectivity check. Check its credentials or connection in Proxies, then start signup again.') from None

    async def start(self, record, retailer, mode='connectivity', *, fingerprints=None,
                    target=None, repeats=1, throughput=False, force=True):
        if mode not in {'connectivity','browser'}:
            raise ValueError('Proxy check mode must be connectivity or browser')
        if self.busy(record['id']): raise ValueError('This group already has a check running')
        if len(self.jobs)>=3: raise ValueError('Three proxy groups are already testing; wait or cancel one')
        if type(repeats) is not int or not 1<=repeats<=3 or type(throughput) is not bool:
            raise ValueError('Use 1–3 samples and a boolean throughput option')
        target=target or NEUTRAL
        destination(target)
        if fingerprints is not None:
            if not isinstance(fingerprints,list) or len(fingerprints)>1000 or any(not isinstance(f,str) for f in fingerprints):
                raise ValueError('Invalid connection selection')
            selected=set(fingerprints)
            lines=[s for s in record['entries'].splitlines() if parse_proxy(s).fingerprint in selected]
            if len(lines)!=len(selected) or not lines:raise ValueError('Selected connections changed; refresh the group')
            record={**record,'entries':'\n'.join(lines)}
        key=mode+':'+record['id']
        if not self.jobs:
            settings=self.store.get('settings','settings') or {}
            self._slots=asyncio.Semaphore(settings.get('proxy_concurrency',5))
        self.jobs[key]=asyncio.create_task(self.run(record,retailer,mode,key,target,repeats,throughput,force))

    def _persist(self, record, retailer, mode, results, status):
        prefix='health-' if mode=='connectivity' else 'browser-health-'
        self.store.put('proxy_health',dict(list_id=record['id'],retailer=retailer,kind=mode,
                       status=status,results=results,at=now(),total=len(record['entries'].splitlines())),prefix+record['id'])

    async def run(self, record, retailer, mode, key, target, repeats, throughput, force):
        try:
            if mode=='browser':await self._run_browser(record,retailer)
            else:await self._run_connectivity(record,retailer,target,repeats,throughput,force)
        except asyncio.CancelledError:
            old=self.store.get('proxy_health',('health-' if mode=='connectivity' else 'browser-health-')+record['id']) or {}
            self._persist(record,retailer,mode,old.get('results',[]),'cancelled')
            raise
        except Exception:
            old=self.store.get('proxy_health',('health-' if mode=='connectivity' else 'browser-health-')+record['id']) or {}
            self._persist(record,retailer,mode,old.get('results',[]),'failed')
        finally:
            self.jobs.pop(key,None)
            if self.background_group==record['id']:self.background_group=None

    async def _run_connectivity(self, record, retailer, target, repeats, throughput, force):
        settings=self.store.get('settings','settings') or {}
        results=[]; lines=iter(enumerate(record['entries'].splitlines(),1)); updated=0
        self._persist(record,retailer,'connectivity',results,'testing')
        destination_key=destination(target).netloc
        async def worker():
            nonlocal updated
            for index,line in lines:
                parsed=parse_proxy(line); endpoint_id='endpoint-'+parsed.fingerprint
                old=self.store.get('proxy_endpoints',endpoint_id) or {}
                if not force and old.get('fresh_until','')>now() and old.get('target')==target:
                    results.append({**old.get('measurement',{}),'index':index,'cached':True})
                    continue
                history=old.get('history',[])
                observation=None
                for _ in range(repeats):
                    async with self._slots:
                        if self._cooldowns.get(destination_key,0)>time.time():break
                        observation=await probe(line,target,timeout=settings.get('proxy_timeout_seconds',15),throughput=throughput)
                        # At most one extra attempt, only for transient transport errors.
                        if observation.get('error') in TRANSIENT:
                            history.append(observation)
                            observation=await probe(line,target,timeout=settings.get('proxy_timeout_seconds',15),throughput=throughput)
                        delay=observation.get('retry_after_seconds')
                        if observation.get('http_status') in (429,503):
                            self._cooldowns[destination_key]=time.time()+(delay if delay is not None else 60)
                    history.append(observation);history=history[-HISTORY_LIMIT:]
                    if observation.get('http_status') in (403,429,503) or observation.get('status')=='authentication_failed':break
                if observation is None:
                    results.append({**parsed.public(),'index':index,'status':'deferred','error':'destination_cooldown'})
                else:
                    summary=summarize(history)
                    self.store.put('proxy_endpoints',{**old,**parsed.public(),**summary,
                        'connection':line,'pool_ids':old.get('pool_ids',[record['id']]),'history':history,
                        'measurement':observation,'target':target},endpoint_id)
                    results.append({**observation,**summary,'index':index})
                # Endpoint results are durable immediately; aggregate writes are throttled.
                if time.monotonic()-updated>=.25:
                    self._persist(record,retailer,'connectivity',results,'testing');updated=time.monotonic()
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(min(len(record['entries'].splitlines()),settings.get('proxy_concurrency',5))):group.create_task(worker())
        finally:
            self._persist(record,retailer,'connectivity',results,'testing')
        self._persist(record,retailer,'connectivity',results,'completed')

    async def maintenance(self, *, idle):
        if not idle and self.background_group:
            await self.cancel(self.background_group)
            self.background_group=None
        if not idle or self.jobs or time.monotonic()<self._next_maintenance:
            return
        self._next_maintenance=time.monotonic()+60
        if not (self.store.get('settings','settings') or {}).get('proxy_background_checks',False):return
        endpoints={p['fingerprint']:p for p in self.store.all('proxy_endpoints')}
        for group in self.store.all('proxies'):
            stale=[parse_proxy(line).fingerprint for line in group['entries'].splitlines() if endpoints.get(parse_proxy(line).fingerprint,{}).get('fresh_until','')<=now()]
            if stale:
                self.background_group=group['id']
                await self.start(group,'amazon',fingerprints=stale[:5],force=False)
                return

    async def cancel(self, record_id):
        jobs=[job for key,job in self.jobs.items() if key.endswith(':'+record_id)]
        for job in jobs:job.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)

    async def close(self):
        jobs=list(self.jobs.values())
        for job in jobs:job.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
        self.jobs.clear()
