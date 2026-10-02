"""Canonical resources with many-to-many organizational membership."""
import hashlib
import random
import re

from .store import now

KINDS = {"accounts", "profiles", "proxies", "input_lists"}


def matching_email(value):
    """Compare email spelling, without collapsing aliases or guessing identities."""
    value = (value or '').strip().lower()
    return value if re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value) else ''


class Resources:
    def __init__(self, store):
        self.store = store
        store.db.executescript('''
            CREATE TABLE IF NOT EXISTS resource_memberships (
                folder_id TEXT NOT NULL, resource_id TEXT NOT NULL,
                PRIMARY KEY(folder_id, resource_id));
            CREATE TABLE IF NOT EXISTS account_profiles (
                account_id TEXT NOT NULL, profile_id TEXT NOT NULL,
                PRIMARY KEY(account_id, profile_id));
        ''')

    def migrate(self):
        if self.store.get('migrations', 'resource-folders-v1'):
            return
        for kind in KINDS:
            for item in self.store.all(kind):
                label = item.get('group') or 'Personal'
                folder_id = 'folder-' + hashlib.sha256(f'{kind}/{label}'.encode()).hexdigest()[:24]
                if not self.store.get('folders', folder_id):
                    self.store.put('folders', {'name':label,'resource_kind':kind}, folder_id)
                self.add(folder_id, [item['id']])
        self.store.put('migrations', {'at':now()}, 'resource-folders-v1')

    def members(self, folder_id):
        return [r[0] for r in self.store.db.execute('SELECT resource_id FROM resource_memberships WHERE folder_id=? ORDER BY rowid', (folder_id,))]

    def add(self, folder_id, ids):
        folder = self.store.get('folders', folder_id)
        if not folder:
            raise ValueError('Folder not found')
        if not isinstance(ids, list) or len(ids)>1000 or any(not isinstance(i,str) or not self.store.get(folder['resource_kind'],i) for i in ids):
            raise ValueError('Every selected item must belong to this resource type')
        with self.store.db:
            self.store.db.executemany('INSERT OR IGNORE INTO resource_memberships VALUES (?,?)', [(folder_id,i) for i in ids])

    def remove(self, folder_id, resource_id):
        with self.store.db:
            self.store.db.execute('DELETE FROM resource_memberships WHERE folder_id=? AND resource_id=?',(folder_id,resource_id))

    def links(self):
        return [{'account_id':a,'profile_id':p} for a,p in self.store.db.execute('SELECT account_id,profile_id FROM account_profiles')]

    def link(self, account_id, profile_id, enabled=True):
        if not self.store.get('accounts', account_id) or not self.store.get('profiles', profile_id):
            raise ValueError('Account or profile not found')
        with self.store.db:
            if enabled:
                self.store.db.execute('INSERT OR IGNORE INTO account_profiles VALUES (?,?)',(account_id,profile_id))
            else:
                self.store.db.execute('DELETE FROM account_profiles WHERE account_id=? AND profile_id=?',(account_id,profile_id))

    def cleanup(self, kind, id):
        with self.store.db:
            self.store.db.execute('DELETE FROM resource_memberships WHERE resource_id=? OR folder_id=?',(id,id))
            if kind=='accounts': self.store.db.execute('DELETE FROM account_profiles WHERE account_id=?',(id,))
            if kind=='profiles': self.store.db.execute('DELETE FROM account_profiles WHERE profile_id=?',(id,))

    def selection(self, data, kind, prefix):
        folder_id = data.get(prefix+'_group_id')
        if folder_id:
            folder = self.store.get('folders', folder_id)
            if not folder or folder['resource_kind'] != kind:
                raise ValueError('Choose a valid '+prefix+' group')
            ids = self.members(folder_id)
        else:
            ids = [data[prefix+'_id']] if data.get(prefix+'_id') else []
        if any(not self.store.get(kind,i) for i in ids):
            raise ValueError('Selected '+prefix+' no longer exists')
        return ids

    def assignments(self, data):
        group = self.store.get('groups', data.get('group_id',''))
        if not group: raise ValueError('Select a task group')
        count = data.get('count',1)
        if type(count) is not int or not 1 <= count <= 100: raise ValueError('Task quantity must be 1–100')
        profiles = self.selection(data,'profiles','profile')
        accounts = [] if data.get('match_profiles') else self.selection(data,'accounts','account')
        accounts = [i for i in accounts if self.store.get('accounts',i).get('retailer','amazon')==group.get('retailer','amazon')]
        if not profiles and data.get('profile_group_id'): raise ValueError('Profile group is empty')
        if not accounts and data.get('account_group_id') and not data.get('match_profiles'): raise ValueError('Account group has no accounts for this retailer')
        distribution = data.get('distribution','sequential')
        if distribution not in ('sequential','random','one_to_one'): raise ValueError('Unknown distribution')
        if distribution=='one_to_one' and ((profiles and len(profiles)!=count) or (accounts and not data.get('match_profiles') and len(accounts)!=count)):
            raise ValueError('One-to-one requires the task quantity to equal each selected group size')
        randomizer = random.Random(data.get('seed',0))
        if distribution=='random': randomizer.shuffle(profiles); randomizer.shuffle(accounts)
        matches = {}
        if data.get('match_profiles'):
            retailer_accounts = {a['id']: a for a in self.store.all('accounts')
                                 if a.get('retailer', 'amazon') == group.get('retailer', 'amazon')}
            links = self.links()
            for profile_id in profiles:
                profile = self.store.get('profiles', profile_id)
                candidates = [link['account_id'] for link in links
                              if link['profile_id'] == profile_id and link['account_id'] in retailer_accounts]
                source = 'manual_link'
                if not candidates:
                    source = 'email'
                    email = matching_email(profile.get('email'))
                    candidates = [id for id, account in retailer_accounts.items()
                                  if email and matching_email(account.get('email')) == email]
                reason = 'Saved relationship' if source == 'manual_link' else 'Matching email'
                error = ''
                if not candidates:
                    error = 'No matching account. Add the same email to the profile and retailer account, save a relationship, or turn off matching to choose manually.'
                elif len(candidates) > 1:
                    error = (f'{len(candidates)} accounts match this profile for the selected retailer '
                             f'by {reason.lower()}. Keep one saved relationship or turn off matching to choose manually.')
                matches[profile_id] = {'account_id': candidates[0] if len(candidates) == 1 else '',
                                       'match_source': source if not error else 'unresolved',
                                       'match_reason': reason if not error else 'Needs a match', 'error': error}
        rows, errors = [], []
        for i in range(count):
            profile = profiles[i%len(profiles)] if profiles else ''
            account = accounts[i%len(accounts)] if accounts else ''
            source, reason = 'selection', 'Manual selection' if account else 'No account selected'
            if data.get('match_profiles'):
                match = matches.get(profile, {'account_id': '', 'match_source': 'unresolved',
                                             'match_reason': 'Choose a profile',
                                             'error': 'Select a profile or turn off matching to choose accounts manually.'})
                if match['error']: errors.append(f"Task {i+1}: {match['error']}")
                account, source, reason = match['account_id'], match['match_source'], match['match_reason']
            if not account and not data.get('simulation',True) and not data.get('match_profiles'): errors.append(f'Task {i+1}: select an account for live execution')
            rows.append({'account_id':account,'profile_id':profile,'index':i+1,
                         'match_source':source,'match_reason':reason})
        return {'rows':rows,'errors':errors}
