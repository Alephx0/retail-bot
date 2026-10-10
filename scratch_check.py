from retail.fingerprint import build_scripts
from retail.account_consistency import AccountBrowserProfiles
from retail.store import Store
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
store = Store(tmp)
profiles = AccountBrowserProfiles(store)
a = store.put('accounts', {'name': 'A', 'region': 'US'})
b = store.put('accounts', {'name': 'B', 'region': 'US'})
pa = profiles.get(a)
pb = profiles.get(b)

print('A id:', a['id'])
print('B id:', b['id'])
print('A seed hex:', pa['seed'])
print('B seed hex:', pb['seed'])
print('A seed int:', int(pa['seed'], 16) & 0xFFFFFFFF)
print('B seed int:', int(pb['seed'], 16) & 0xFFFFFFFF)

# build_scripts() now returns an empty list when no surface is enabled.
# Enable one explicit surface so the two seeds can be compared.
flags = {'perturb_canvas': True, 'spoof_audio': True}
sa = build_scripts(int(pa['seed'], 16), **flags)[0]
sb = build_scripts(int(pb['seed'], 16), **flags)[0]
print('A script starts:', sa[:120])
print('B script starts:', sb[:120])
print('A and B scripts equal:', sa == sb)
store.db.close()