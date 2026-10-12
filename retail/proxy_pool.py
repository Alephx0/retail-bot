"""Normalize saved proxy pools and select a consistent, tested route."""
import hashlib
from .services import proxy_fingerprint
from .proxy_protocol import parse_proxy
from .store import now


class ProxySelectionError(ValueError):
    """Safe public startup reason; connection strings never enter the message."""
    def __init__(self, code, message, *, stale=()):
        super().__init__(message)
        self.code = code
        self.stale = tuple(stale)


class ProxyPool:
    def __init__(self,store): self.store=store

    def sync(self):
        records={}
        for pool in self.store.all('proxies'):
            health=self.store.get('proxy_health','health-'+pool['id']) or {}
            tested={r['fingerprint']:r for r in health.get('results',[])}
            for line in pool['entries'].splitlines():
                fingerprint=proxy_fingerprint(line)
                parsed=parse_proxy(line)
                existing=self.store.get('proxy_endpoints','endpoint-'+fingerprint) or {}
                item=records.setdefault(fingerprint,{**existing,**parsed.public(),'connection':line,'label':parsed.host,'pool_ids':[]})
                item['pool_ids'].append(pool['id'])
                if not existing.get('history'):
                    result=tested.get(fingerprint,{})
                    item.update(status=result.get('status','untested'),latency_ms=result.get('latency_ms'),last_tested=health.get('at'))
        for fingerprint,item in records.items(): self.store.put('proxy_endpoints',item,'endpoint-'+fingerprint)
        valid={'endpoint-'+f for f in records}
        for old in self.store.all('proxy_endpoints'):
            if old['id'] not in valid:self.store.delete('proxy_endpoints',old['id'])

    def choose(self,pool_id,owner):
        pool=self.store.get('proxies',pool_id)
        if not pool:
            if pool_id:raise ProxySelectionError('proxy_missing', 'The assigned proxy group no longer exists. Select a connection in the account details.')
            return ''
        lines=[x.strip() for x in pool['entries'].splitlines() if x.strip()]
        binding_id='route-'+hashlib.sha256((pool_id+'/'+owner).encode()).hexdigest()[:24]
        binding=self.store.get('network_routes',binding_id)
        if binding:
            for line in lines:
                if proxy_fingerprint(line)==binding['fingerprint'] or hashlib.sha256(line.strip().encode()).hexdigest()[:16]==binding['fingerprint']:return line
        health=self.store.get('proxy_health','health-'+pool_id)
        if health and health.get('status')=='completed':
            healthy={x['fingerprint'] for x in health.get('results',[]) if x.get('status') in ('reachable','healthy','degraded')}
            lines=[line for line in lines if proxy_fingerprint(line) in healthy]
            if not lines:raise ProxySelectionError('proxy_unreachable', 'No reachable proxies in this pool; run the connectivity check or update the pool')
        lines=[line for line in lines if parse_proxy(line).browser_compatible]
        observations={proxy_fingerprint(line):self.store.get('proxy_endpoints','endpoint-'+proxy_fingerprint(line)) or {} for line in lines}
        if any(x.get('history') for x in observations.values()):
            stale=[fingerprint for fingerprint,item in observations.items() if item.get('status')=='healthy' and item.get('fresh_until','')<=now()]
            lines=[line for line in lines if observations[proxy_fingerprint(line)].get('status')=='healthy' and observations[proxy_fingerprint(line)].get('fresh_until','')>now()]
            if not lines:raise ProxySelectionError('proxy_stale' if stale else 'proxy_unhealthy', 'No fresh healthy browser-compatible proxy. Test this group before starting a new session.', stale=stale)
            lines.sort(key=lambda line: (observations[proxy_fingerprint(line)].get('latency_ms') or float('inf')))
            lines=lines[:max(1,(len(lines)+1)//2)]
        if not lines:raise ProxySelectionError('proxy_incompatible', 'This group has no browser-compatible proxy connections')
        line=lines[int(hashlib.sha256(owner.encode()).hexdigest()[:8],16)%len(lines)]
        self.store.put('network_routes',{'pool_id':pool_id,'owner':owner,'fingerprint':proxy_fingerprint(line)},binding_id)
        return line
