"""Normalize saved proxy pools and select a consistent, tested route."""
import hashlib
from .services import proxy_fingerprint


class ProxyPool:
    def __init__(self,store): self.store=store

    def sync(self):
        records={}
        for pool in self.store.all('proxies'):
            health=self.store.get('proxy_health','health-'+pool['id']) or {}
            tested={r['fingerprint']:r for r in health.get('results',[])}
            for line in pool['entries'].splitlines():
                fingerprint=proxy_fingerprint(line)
                item=records.setdefault(fingerprint,{'host':line.split(':')[0],'port':int(line.split(':')[1]),'protocol':'http','connection':line,'label':line.split(':')[0], 'pool_ids':[]})
                item['pool_ids'].append(pool['id'])
                result=tested.get(fingerprint,{})
                item.update(status=result.get('status','untested'),latency_ms=result.get('latency_ms'),last_tested=health.get('at'))
        for fingerprint,item in records.items(): self.store.put('proxy_endpoints',item,'endpoint-'+fingerprint)
        valid={'endpoint-'+f for f in records}
        for old in self.store.all('proxy_endpoints'):
            if old['id'] not in valid:self.store.delete('proxy_endpoints',old['id'])

    def choose(self,pool_id,owner):
        pool=self.store.get('proxies',pool_id)
        if not pool:return ''
        lines=[x.strip() for x in pool['entries'].splitlines() if x.strip()]
        binding_id='route-'+hashlib.sha256((pool_id+'/'+owner).encode()).hexdigest()[:24]
        binding=self.store.get('network_routes',binding_id)
        if binding:
            for line in lines:
                if proxy_fingerprint(line)==binding['fingerprint']:return line
        health=self.store.get('proxy_health','health-'+pool_id)
        if health and health.get('status')=='completed':
            healthy={x['fingerprint'] for x in health.get('results',[]) if x.get('status') in ('reachable','healthy')}
            lines=[line for line in lines if proxy_fingerprint(line) in healthy]
            if not lines:raise ValueError('No reachable proxies in this pool; run the connectivity check or update the pool')
        if not lines:return ''
        line=lines[int(hashlib.sha256(owner.encode()).hexdigest()[:8],16)%len(lines)]
        self.store.put('network_routes',{'pool_id':pool_id,'owner':owner,'fingerprint':proxy_fingerprint(line)},binding_id)
        return line