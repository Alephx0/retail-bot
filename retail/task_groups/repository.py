"""Encrypted execution ledger; reservations and intent transitions are atomic."""
import json
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import Plan, windows

ACTIVE_RUNS = {'scheduled', 'preparing', 'watching', 'paused', 'stopping'}
HELD = {'reserved', 'preparing', 'carting', 'reviewing', 'waiting_user', 'submitting', 'reconciliation_required', 'confirmed'}
MUTATED = {'carting', 'reviewing', 'waiting_user', 'submitting', 'reconciliation_required'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


class Conflict(ValueError):
    pass


class Repository:
    def __init__(self, store):
        self.store = store
        self.db = sqlite3.connect(store.folder/'retail.sqlite3', timeout=5, check_same_thread=False)
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS tg_records (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
            payload BLOB NOT NULL);
          CREATE INDEX IF NOT EXISTS tg_owner ON tg_records(kind,owner);
          CREATE TABLE IF NOT EXISTS tg_claims (account_id TEXT PRIMARY KEY, attempt_id TEXT UNIQUE NOT NULL);
          CREATE TABLE IF NOT EXISTS tg_commands (command_key TEXT PRIMARY KEY, run_id TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS tg_orders (order_key TEXT PRIMARY KEY, attempt_id TEXT UNIQUE NOT NULL);
          CREATE TABLE IF NOT EXISTS tg_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, payload BLOB NOT NULL);
          CREATE INDEX IF NOT EXISTS tg_events_run ON tg_events(run_id,seq);
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _put(self, kind, value, owner=''):
        self.db.execute('INSERT INTO tg_records VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,owner=excluded.owner',
                        (value['id'], kind, owner, self.store.cipher.encrypt(json.dumps(value).encode())))
        return value

    def get(self, kind, id):
        row = self.db.execute('SELECT payload FROM tg_records WHERE kind=? AND id=?', (kind, id)).fetchone()
        return json.loads(self.store.cipher.decrypt(row[0])) if row else None

    def require(self, kind, id):
        value = self.get(kind, id)
        if not value:
            raise ValueError(f'{kind.title()} not found')
        return value

    def all(self, kind, owner=None):
        sql, args = 'SELECT payload FROM tg_records WHERE kind=?', [kind]
        if owner is not None:
            sql += ' AND owner=?'; args.append(owner)
        return [json.loads(self.store.cipher.decrypt(r[0])) for r in self.db.execute(sql+' ORDER BY rowid', args)]

    def event(self, run_id, kind, message, attempt_id='', **details):
        data = {'at':stamp(), 'kind':kind, 'message':message, 'attempt_id':attempt_id, **details}
        self.db.execute('INSERT INTO tg_events(run_id,payload) VALUES (?,?)',
                        (run_id, self.store.cipher.encrypt(json.dumps(data).encode())))

    def events(self, run_id, after=0, limit=150):
        return [dict(json.loads(self.store.cipher.decrypt(p)), seq=s) for s,p in self.db.execute(
            'SELECT seq,payload FROM tg_events WHERE run_id=? AND seq>? ORDER BY seq DESC LIMIT ?',
            (run_id, after, limit))]

    def group_events(self, group_id, limit=150):
        return [dict(json.loads(self.store.cipher.decrypt(p)),seq=s,run_id=r) for s,r,p in self.db.execute(
            'SELECT e.seq,e.run_id,e.payload FROM tg_events e JOIN tg_records r ON r.id=e.run_id WHERE r.kind=? AND r.owner=? ORDER BY e.seq DESC LIMIT ?',
            ('run',group_id,limit))]

    def group_attempts(self, group_id):
        return [json.loads(self.store.cipher.decrypt(row[0])) for row in self.db.execute(
            'SELECT a.payload FROM tg_records a JOIN tg_records r ON a.owner=r.id WHERE a.kind=? AND r.kind=? AND r.owner=? ORDER BY a.rowid',
            ('attempt','run',group_id))]

    def save_plan(self, data, group_id=None, expected_revision=None, legacy_id=''):
        plan = Plan.model_validate(data).model_dump(mode='json')
        with self.transaction():
            group = self.require('group', group_id) if group_id else {'id':uuid.uuid4().hex, 'created_at':stamp(), 'armed':False}
            if group_id and group['revision_id'] != expected_revision:
                raise Conflict('This group changed. Refresh before saving.')
            if group.get('active_run_id'):
                run = self.get('run', group['active_run_id'])
                if run and run['state'] in ACTIVE_RUNS:
                    raise Conflict('Stop this run before editing its plan')
            revision = {'id':uuid.uuid4().hex, 'plan':plan, 'at':stamp()}
            self._put('revision', revision, group['id'])
            group.update(name=plan['name'], revision_id=revision['id'], updated_at=stamp(), armed=False)
            if legacy_id:
                group['legacy_id'] = legacy_id
            return self._put('group', group)

    def start(self, group_id, key, current=None, occurrence=None):
        current = current or datetime.now(timezone.utc)
        with self.transaction():
            command = self.db.execute('SELECT run_id FROM tg_commands WHERE command_key=?', (group_id+':'+key,)).fetchone()
            if command:
                return self.require('run', command[0])
            group = self.require('group', group_id)
            existing = self.get('run', group.get('active_run_id', ''))
            if existing and existing['state'] in ACTIVE_RUNS:
                return existing
            plan = self.require('revision', group['revision_id'])['plan']
            scheduled = plan['schedule']['kind'] != 'manual'
            if scheduled and occurrence is None:
                occurrence = next((w for w in windows(plan['schedule'],current) if w['end']>current.isoformat()),None)
                if not occurrence:
                    raise ValueError('No upcoming valid window. Check the date and timezone (nonexistent DST times are skipped).')
            if occurrence and any(r.get('window',{}).get('key')==occurrence['key'] and r['revision_id']==group['revision_id'] for r in self.all('run',group_id)):
                raise Conflict('This scheduled occurrence has already run')
            quota = group_id+('/simulation' if plan['simulation'] else '/live')
            if plan['schedule']['quota_scope']=='window' and occurrence:
                quota += '/'+occurrence['key']
            run = {'id':uuid.uuid4().hex, 'group_id':group_id, 'revision_id':group['revision_id'], 'plan':plan,
                   'state':'scheduled' if scheduled else 'watching', 'generation':0, 'quota_key':quota,
                   'window':occurrence or {}, 'created_at':stamp(), 'message':'Waiting for preparation time' if scheduled else 'Watching products'}
            if self.progress(run)['confirmed_units'] >= Plan.model_validate(plan).desired_units and plan['action'] not in ('notify','quote'):
                raise Conflict('The group purchase goal is already fulfilled. Duplicate the group for an intentional new goal.')
            group.update(active_run_id=run['id'], armed=scheduled)
            self._put('group', group); self._put('run',run,group_id)
            self.db.execute('INSERT INTO tg_commands VALUES (?,?)',(group_id+':'+key,run['id']))
            self.event(run['id'],'started','Run created from the saved plan')
            return run

    def attempts(self, run_id):
        return self.all('attempt',run_id)

    def quota_attempts(self, run):
        runs = self.all('run',run['group_id'])
        ids = {r['id'] for r in runs if r['quota_key']==run['quota_key']}
        return [a for run_id in ids for a in self.all('attempt',run_id)]

    def progress(self, run):
        attempts = self.quota_attempts(run)
        confirmed = [a for a in attempts if a['state']=='confirmed']
        reserved = [a for a in attempts if a['state'] in HELD and a['state']!='confirmed']
        return {'confirmed_units':sum(a['units'] for a in confirmed), 'reserved_units':sum(a['units'] for a in reserved),
                'spent_cents':sum(a['money_cents'] for a in confirmed), 'reserved_cents':sum(a['money_cents'] for a in reserved)}

    def claimed(self, account_id):
        row=self.db.execute('SELECT attempt_id FROM tg_claims WHERE account_id=?',(account_id,)).fetchone()
        return row[0] if row else None

    def reserve(self, run_id, account_id, product_id, expected_generation, observation):
        with self.transaction():
            run=self.require('run',run_id); plan=run['plan']
            if run['state']!='watching' or run['generation']!=expected_generation:
                raise Conflict('Run is paused or stopped')
            if run['window'] and stamp()>=run['window']['end']:
                raise Conflict('Execution window has ended')
            roster=plan['account_ids'] or (['simulation'] if plan['simulation'] else [])
            if account_id not in roster:
                raise Conflict('Account is not in this run')
            target=next((p for p in plan['products'] if p['product_id']==product_id),None)
            if not target:
                raise ValueError('Product is not in this run')
            claim_key=('simulation:' if plan['simulation'] else '')+account_id
            if self.claimed(claim_key):
                raise Conflict('Account is busy')
            active=[a for a in self.quota_attempts(run) if a['state'] in HELD]
            if plan['action'] in ('notify','quote') and any(a['product_id']==product_id and a['account_id']==account_id and a['state']=='quoted' for a in self.attempts(run_id)):
                raise Conflict('This checkout total was already collected')
            remaining=Plan.model_validate(plan).desired_units-sum(a['units'] for a in active)
            account_left=plan['per_account_units']-sum(a['units'] for a in active if a['account_id']==account_id)
            if plan['selection']=='each':
                remaining=min(remaining,target['desired_units']-sum(a['units'] for a in active if a['product_id']==product_id))
            units=min(plan['units_per_order'],remaining,account_left)
            if units<=0:
                raise Conflict('Unit goal or account cap is reserved or fulfilled')
            if sum(a['money_cents'] for a in active)+plan['max_order_cents']>plan['max_spend_cents']:
                raise Conflict('Waiting for group budget: insufficient unreserved allowance')
            attempt={'id':uuid.uuid4().hex,'run_id':run_id,'group_id':run['group_id'],'account_id':account_id,
                     'product_id':product_id,'units':units,'money_cents':plan['max_order_cents'],'state':'reserved',
                     'generation':run['generation'],'created_at':stamp(),'updated_at':stamp(),'observation':observation,
                     'simulation':plan['simulation'],'region':plan['region'],'claim_key':claim_key,'message':'Quantity and order allowance reserved'}
            account=self.store.get('accounts',account_id)
            if account:
                from ..models import account_fingerprint_settings
                effective=account_fingerprint_settings(self.store.get('settings','settings') or {},account)
                attempt['browser_profile']={k:v for k,v in effective.items() if k.startswith(('browser_','fingerprint_'))}
                attempt['browser_profile']['generated_values']=account.get('fingerprint_values') or {}
            self._put('attempt',attempt,run_id)
            self.db.execute('INSERT INTO tg_claims VALUES (?,?)',(claim_key,attempt['id']))
            self.event(run_id,'reserved',attempt['message'],attempt['id'])
            return attempt

    def gate(self, attempt_id):
        a=self.require('attempt',attempt_id); run=self.require('run',a['run_id'])
        if a['state'] not in HELD or a['state'] in ('confirmed','reconciliation_required','submitting'):
            raise Conflict('Attempt cannot perform another mutation')
        if run['state']!='watching' or run['generation']!=a['generation']:
            raise Conflict('Run is paused or stopped')
        if run['window'] and stamp()>=run['window']['end']:
            raise Conflict('Execution window has ended')
        if self.claimed(a['claim_key'])!=a['id']:
            raise Conflict('Account ownership was lost')
        return a

    def stage(self, attempt_id, state, message, **metadata):
        with self.transaction():
            a=self.require('attempt',attempt_id)
            if a['state'] in ('confirmed','cancelled','rejected','failed','quoted'):
                raise Conflict('Attempt is already finished')
            predecessors={'preparing':{'reserved'},'carting':{'preparing'},
                          'reviewing':{'preparing','carting','reviewing'},'waiting_user':{'preparing','reviewing'}}
            if state not in predecessors or a['state'] not in predecessors[state]:
                raise Conflict('Invalid attempt transition')
            if state in ('carting','reviewing'):
                self.gate(attempt_id)
            a.update(state=state,message=message,updated_at=stamp(),**metadata)
            self._put('attempt',a,a['run_id']); self.event(a['run_id'],state,message,a['id'])
            return a

    def intent(self, attempt_id, snapshot):
        from .domain import cents
        with self.transaction():
            a=self.gate(attempt_id)
            if a.get('intent'):
                raise Conflict('This attempt already has a submission intent')
            if not a['simulation'] and a['state']!='reviewing':
                raise Conflict('Live submission requires a verified checkout review')
            run=self.require('run',a['run_id']); plan=run['plan']; total=cents(snapshot['total'])
            currency={'US':'USD','UK':'GBP','CA':'CAD'}[plan['region']]
            if snapshot.get('currency')!=currency or snapshot.get('quantity')!=a['units'] or snapshot.get('asin')!=a['product_id']:
                raise Conflict('Final checkout identity, quantity or currency did not match')
            if total>a['money_cents']:
                raise Conflict('Final order total exceeds the reserved allowance')
            a.update(state='submitting',money_cents=total,intent={'id':uuid.uuid4().hex,'at':stamp(),'snapshot':snapshot},updated_at=stamp(),message='Submission intent saved; automatic retries disabled')
            self._put('attempt',a,a['run_id']); self.event(a['run_id'],'submitting',a['message'],a['id'])
            return a

    def finish(self, attempt_id, state, message, order_id='', total_cents=None, evidence='', actual_units=None):
        if state not in ('confirmed','quoted','cancelled','rejected','failed','reconciliation_required'):
            raise ValueError('Invalid attempt outcome')
        with self.transaction():
            a=self.require('attempt',attempt_id)
            if a['state'] in ('confirmed','quoted','cancelled','rejected','failed'):
                if a['state']==state:
                    return a
                raise Conflict('Attempt has already been finalized')
            if state in ('cancelled','rejected','failed') and a['state'] in MUTATED and not evidence:
                state='reconciliation_required'; message='Browser state needs reconciliation before releasing this account and budget'
            if state=='confirmed':
                if not a['simulation'] and not a.get('intent') and not evidence:
                    raise Conflict('Live confirmation requires a submission intent or recorded reconciliation')
                if not order_id or type(total_cents) is not int or total_cents<0:
                    raise ValueError('Confirmed orders need an order ID and verified total')
                if total_cents>a['money_cents'] and not evidence:
                    raise Conflict('Verified total exceeds reservation; reconcile explicitly')
                plan=self.require('run',a['run_id'])['plan']
                order_key=hashlib.sha256('/'.join([str(a['simulation']),plan['retailer'],plan['region'],a['account_id'],order_id]).encode()).hexdigest()
                try:
                    self.db.execute('INSERT INTO tg_orders VALUES (?,?)',(order_key,a['id']))
                except sqlite3.IntegrityError:
                    raise Conflict('This order is already recorded; reconcile the duplicate observation') from None
                a.update(order_id=order_id,money_cents=total_cents)
                if actual_units is not None:
                    if not evidence or type(actual_units) is not int or not 1<=actual_units<=1000:
                        raise ValueError('Reconciled quantity must be a verified positive integer')
                    a['units']=actual_units
            a.update(state=state,message=message,updated_at=stamp())
            if evidence:
                a['reconciliation']={'at':stamp(),'evidence':evidence}
            if state!='reconciliation_required':
                self.db.execute('DELETE FROM tg_claims WHERE attempt_id=?',(a['id'],))
            self._put('attempt',a,a['run_id']); self.event(a['run_id'],state,message,a['id'])
            run=self.require('run',a['run_id']); p=self.progress(run)
            group=self.require('group',run['group_id'])
            current=self.get('run',group.get('active_run_id',''))
            affected=[run]
            if current and current['id']!=run['id'] and current['quota_key']==run['quota_key'] and current['state'] in ACTIVE_RUNS:
                affected.append(current)
            for affected_run in affected:
                if p['confirmed_units']>=Plan.model_validate(affected_run['plan']).desired_units and not p['reserved_units']:
                    affected_run.update(state='completed',message='Purchase goal fulfilled',generation=affected_run['generation']+1)
                    self._put('run',affected_run,affected_run['group_id']); self.event(affected_run['id'],'completed',affected_run['message'])
            return a

    def command(self, run_id, action):
        with self.transaction():
            run=self.require('run',run_id)
            if action=='pause':
                if run['state'] not in ('watching','preparing','scheduled'):
                    return run
                run['resume_state']=run['state']; run['state']='paused'
            elif action=='resume':
                if run['state']!='paused':
                    raise Conflict('Only a paused run can resume')
                if any(a['state']=='reconciliation_required' for a in self.attempts(run_id)):
                    raise Conflict('Resolve uncertain attempts before resuming')
                if run['window'] and stamp()>=run['window']['end']:
                    raise Conflict('The execution window has ended')
                run['state']=run.get('resume_state','watching')
            elif action=='stop':
                group=self.require('group',run['group_id']); group['armed']=False; self._put('group',group)
                if run['state'] in ('completed','stopped'):
                    return run
                run['state']='stopping'
            else:
                raise ValueError('Unknown run command')
            run['generation']+=1; run['message']={'pause':'Paused: no new mutations','resume':'Run resumed','stop':'Stopping: reconciling in-flight work'}[action]
            if action=='resume':
                for a in self.attempts(run_id):
                    if a['state'] in HELD and a['state'] not in ('confirmed','submitting','reconciliation_required'):
                        a['generation']=run['generation']; self._put('attempt',a,run_id)
            self._put('run',run,run['group_id']); self.event(run_id,action,run['message'])
            return run

    def run_state(self, run_id, state, message):
        with self.transaction():
            run=self.require('run',run_id)
            if run['state'] in ('completed','stopped'):
                return run
            run.update(state=state,message=message)
            self._put('run',run,run['group_id']); self.event(run_id,state,message)
            return run

    def recover(self):
        for run in self.all('run'):
            if run['state'] in ACTIVE_RUNS:
                self.command(run['id'],'pause')
                self.run_state(run['id'],'paused','Application restarted; inspect pending outcomes, then explicitly resume')
            for a in self.attempts(run['id']):
                if a['state'] in HELD and a['state']!='confirmed':
                    self.finish(a['id'],'reconciliation_required' if a['state'] in MUTATED else 'cancelled',
                                'Recovered after restart; no automatic submission replay')

    def observe(self, run_id, observation):
        with self.transaction():
            key=run_id+'/'+observation['account_id']+'/'+observation['product_id']
            old=self.get('observation',key)
            self._put('observation',dict(observation,id=key),run_id)
            if not old or old.get('reason')!=observation['reason']:
                self.event(run_id,'observation',observation['message'],product_id=observation['product_id'])

    def close(self):
        self.db.close()
