import json
import time

from retail.store import Store


def test_legacy_index_is_resumable_and_preserves_encrypted_evidence(tmp_path):
    store = Store(tmp_path)
    originals = []
    for index in range(3):
        record = {'id': f'old-{index}', 'action': 'TRACE', 'at': '2026-10-11', 'trace': 'private'*200000, 'dom': ['private']}
        payload = store.cipher.encrypt(json.dumps(record).encode())
        store.db.execute('INSERT INTO records VALUES (?,?,?)', (record['id'], 'diagnostics', payload))
        originals.append((record['id'], payload))
    store.db.commit()
    assert store.diagnostics_need_index()
    assert store.index_legacy_diagnostics(limit=1)
    assert len(store.diagnostic_summaries()) == 1
    store.db.close()
    recovered = Store(tmp_path)
    while recovered.index_legacy_diagnostics(limit=1):
        pass
    assert not recovered.diagnostics_need_index()
    assert len(recovered.diagnostic_summaries()) == 3
    assert recovered.db.execute("SELECT id,payload FROM records WHERE kind='diagnostics' ORDER BY id").fetchall() == originals
    assert all('trace' not in r and 'dom' not in r for r in recovered.diagnostic_summaries())
    recovered.db.close()


def test_index_stays_current_on_replace_bulk_prune_and_delete(tmp_path):
    store = Store(tmp_path)
    first = store.put('diagnostics', {'action': 'old', 'trace': 'secret'})
    store.put('diagnostics', {**first, 'action': 'new'})
    assert store.diagnostic_summaries()[0]['action'] == 'new'
    store.put_many('diagnostics', [{'action': 'bulk', 'screenshot': 'secret'}])
    assert len(store.diagnostic_summaries()) == 2
    last = store.put_bounded('diagnostics', {'action': 'last'}, limit=1)
    assert store.diagnostic_summaries() == [last]
    store.delete('diagnostics', last['id'])
    assert store.diagnostic_summaries() == []
    store.db.close()


def test_dashboard_summary_does_not_decrypt_trace_payloads(tmp_path):
    store = Store(tmp_path)
    store.put('diagnostics', {'action': 'TRACE', 'trace': 'x'*5_000_000})
    original = store.cipher.decrypt
    def small_only(payload):
        assert len(payload) < 1024, 'Dashboard attempted to decrypt large evidence'
        return original(payload)
    store.cipher.decrypt = small_only
    start = time.perf_counter()
    for _ in range(20):
        assert store.diagnostic_summaries()[0]['action'] == 'TRACE'
    assert time.perf_counter()-start < .5
    store.db.close()
