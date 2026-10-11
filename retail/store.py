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
        self.db.commit()

    def put(self, kind, data, id=None):
        value = dict(data, id=id or data.get("id") or uuid.uuid4().hex)
        encoded = self.cipher.encrypt(json.dumps(value).encode())
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?)", (value["id"], kind, encoded))
        return value

    def put_many(self, kind, records):
        """Encrypt first, then insert the entire validated batch in one transaction."""
        values = [dict(record, id=uuid.uuid4().hex) for record in records]
        rows = [(value["id"], kind, self.cipher.encrypt(json.dumps(value).encode())) for value in values]
        with self.db:
            self.db.executemany("INSERT INTO records VALUES (?, ?, ?)", rows)
        return values

    def get(self, kind, id):
        row = self.db.execute("SELECT payload FROM records WHERE kind=? AND id=?", (kind, id)).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else None

    def all(self, kind):
        return [json.loads(self.cipher.decrypt(row[0])) for row in self.db.execute("SELECT payload FROM records WHERE kind=? ORDER BY rowid", (kind,))]

    def delete(self, kind, id):
        with self.db:
            self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, id))

    def put_bounded(self, kind, record, id=None, *, limit=200):
        """Bound exceptional evidence on disk without decrypting historical rows."""
        value = self.put(kind, record, id)
        with self.db:
            self.db.execute('DELETE FROM records WHERE kind=? AND rowid NOT IN '
                            '(SELECT rowid FROM records WHERE kind=? ORDER BY rowid DESC LIMIT ?)',
                            (kind, kind, limit))
        return value

    def event(self, task_id, status, message):
        self.put("events", {"task_id": task_id, "status": status, "message": message, "at": now()})
        with self.db:
            self.db.execute("DELETE FROM records WHERE kind='events' AND rowid NOT IN (SELECT rowid FROM records WHERE kind='events' ORDER BY rowid DESC LIMIT 1000)")
