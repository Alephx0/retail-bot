import ctypes
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet


def now():
    return datetime.now(timezone.utc).isoformat()


def protect(raw: bytes, decrypt=False) -> bytes:
    """Bind the encryption key to this Windows user's DPAPI credentials."""
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise OSError("Windows could not unlock the local vault")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


class Store:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.folder=folder
        keyfile = folder / "vault.key"
        if not keyfile.exists():
            key = Fernet.generate_key()
            keyfile.write_bytes(protect(key) if os.name == "nt" else key)
            if os.name != "nt":
                keyfile.chmod(0o600)
        raw = keyfile.read_bytes()
        self.cipher = Fernet(protect(raw, True) if os.name == "nt" else raw)
        self.db = sqlite3.connect(folder / "retail.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT, payload BLOB)")
        self.db.execute("CREATE INDEX IF NOT EXISTS records_kind ON records(kind)")
        self.db.execute('CREATE TABLE IF NOT EXISTS diagnostic_index (id TEXT PRIMARY KEY, payload BLOB)')
        self.db.commit()

    def put(self, kind, data, id=None):
        value = dict(data, id=id or data.get("id") or uuid.uuid4().hex)
        encoded = self.cipher.encrypt(json.dumps(value).encode())
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?)", (value["id"], kind, encoded))
            if kind == 'diagnostics':
                self.db.execute('INSERT OR REPLACE INTO diagnostic_index VALUES (?, ?)',
                                (value['id'], self.diagnostic_summary(value)))
        return value

    def diagnostic_summary(self, value):
        summary = {k: value[k] for k in ('id', 'task_id', 'action', 'stage', 'error', 'at', 'status', 'url', 'schema_version', 'capture_note') if k in value}
        return self.cipher.encrypt(json.dumps(summary).encode())

    def diagnostic_summaries(self):
        rows = self.db.execute("SELECT i.payload FROM records r JOIN diagnostic_index i ON i.id=r.id WHERE r.kind='diagnostics' ORDER BY r.rowid DESC LIMIT 100").fetchall()
        return [json.loads(self.cipher.decrypt(row[0])) for row in reversed(rows)]

    def diagnostics_need_index(self):
        return self.db.execute("SELECT 1 FROM records r LEFT JOIN diagnostic_index i ON i.id=r.id WHERE r.kind='diagnostics' AND i.id IS NULL LIMIT 1").fetchone() is not None

    def index_legacy_diagnostics(self, limit=4):
        """Resumable metadata backfill on a dedicated connection, off the engine loop."""
        db = sqlite3.connect(self.folder / 'retail.sqlite3', timeout=30)
        try:
            rows = db.execute("SELECT r.id FROM records r LEFT JOIN diagnostic_index i ON i.id=r.id WHERE r.kind='diagnostics' AND i.id IS NULL ORDER BY r.rowid DESC LIMIT ?", (limit,)).fetchall()
            for (identifier,) in rows:
                row = db.execute("SELECT payload FROM records WHERE kind='diagnostics' AND id=?", (identifier,)).fetchone()
                if not row:
                    continue
                summary = self.diagnostic_summary(json.loads(self.cipher.decrypt(row[0])))
                # A concurrent replacement writes its own index atomically.
                with db:
                    db.execute("INSERT OR IGNORE INTO diagnostic_index SELECT ?, ? WHERE EXISTS (SELECT 1 FROM records WHERE kind='diagnostics' AND id=? AND payload=?)", (identifier, summary, identifier, row[0]))
            return bool(rows)
        finally:
            db.close()

    def put_many(self, kind, records):
        """Encrypt first, then insert the entire validated batch in one transaction."""
        values = [dict(record, id=uuid.uuid4().hex) for record in records]
        rows = [(value["id"], kind, self.cipher.encrypt(json.dumps(value).encode())) for value in values]
        with self.db:
            self.db.executemany("INSERT INTO records VALUES (?, ?, ?)", rows)
            if kind == 'diagnostics':
                self.db.executemany('INSERT OR REPLACE INTO diagnostic_index VALUES (?, ?)',
                                    [(value['id'], self.diagnostic_summary(value)) for value in values])
        return values

    def get(self, kind, id):
        row = self.db.execute("SELECT payload FROM records WHERE kind=? AND id=?", (kind, id)).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else None

    def all(self, kind):
        return [json.loads(self.cipher.decrypt(row[0])) for row in self.db.execute("SELECT payload FROM records WHERE kind=? ORDER BY rowid", (kind,))]

    def delete(self, kind, id):
        with self.db:
            self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, id))
            if kind == 'diagnostics':
                self.db.execute('DELETE FROM diagnostic_index WHERE id=?', (id,))

    def put_bounded(self, kind, record, id=None, *, limit=200):
        """Bound exceptional evidence on disk without decrypting historical rows."""
        value = self.put(kind, record, id)
        with self.db:
            self.db.execute('DELETE FROM records WHERE kind=? AND rowid NOT IN '
                            '(SELECT rowid FROM records WHERE kind=? ORDER BY rowid DESC LIMIT ?)',
                            (kind, kind, limit))
            if kind == 'diagnostics':
                self.db.execute('DELETE FROM diagnostic_index WHERE id NOT IN (SELECT id FROM records WHERE kind=?)', (kind,))
        return value

    def event(self, task_id, status, message):
        self.put("events", {"task_id": task_id, "status": status, "message": message, "at": now()})
        with self.db:
            self.db.execute("DELETE FROM records WHERE kind='events' AND rowid NOT IN (SELECT rowid FROM records WHERE kind='events' ORDER BY rowid DESC LIMIT 1000)")
