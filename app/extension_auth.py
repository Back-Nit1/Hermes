"""Browser-extension device auth: one-time pairing codes -> revocable bearer device tokens.

Separate SQLite file (extension.sqlite3) so neither the root app token nor the
account tables are touched. Pairing codes and device tokens are random and are
stored only as SHA-256 hashes; the plaintext token is returned exactly once, by
`exchange()`. A device is bound to one user (0 in local mode) with an explicit
scope list, an expiry, and a revocation timestamp.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from .migrations import migrate_sqlite

MIGRATIONS = [
    """
CREATE TABLE pairing_codes (
  code_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, scopes TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL,
  name TEXT NOT NULL, scopes TEXT NOT NULL, created_at REAL NOT NULL, last_used_at REAL,
  expires_at REAL NOT NULL, revoked_at REAL);
CREATE INDEX idx_devices_user ON devices(user_id);
""",
]

SCOPES = frozenset(
    {"status", "campaigns:read", "capture:preview", "capture:commit", "queue:read", "notifications:read"}
)
CODE_TTL = 300
TOKEN_TTL = 30 * 86400
MAX_DEVICES_PER_USER = 10
TOKEN_PREFIX = "hxe_"  # noqa: S105 - a public marker, not a secret
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L
LAST_USED_GRANULARITY = 60

_PUBLIC = "id, name, scopes, created_at, last_used_at, expires_at, revoked_at"


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _device(row):
    d = dict(row)
    d["scopes"] = json.loads(d["scopes"])
    return d


class RateLimiter:
    """In-memory sliding window per key. ponytail: per-process; move to SQLite if running several workers."""

    MAX_KEYS = 5000

    def __init__(self, clock=time.monotonic):
        self.clock, self.hits, self.lock = clock, defaultdict(deque), threading.Lock()

    def allow(self, key, limit, window):
        """Records a hit; False (and records nothing) once `limit` hits fall inside `window` seconds."""
        now = self.clock()
        with self.lock:
            if len(self.hits) > self.MAX_KEYS:
                for k in [k for k, q in self.hits.items() if not q or now - q[-1] > window]:
                    del self.hits[k]
            q = self.hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


class ExtensionAuth:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        path = Path(path)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        migrate_sqlite(self.db, path, MIGRATIONS)
        os.chmod(path, 0o600)
        self.lock = threading.Lock()

    def close(self):
        self.db.close()

    # ------------------------------------------------------------ pairing
    def create_code(self, user_id, scopes=None):
        """Dashboard side. Returns (plaintext code, expires_at). The code works once."""
        scopes = sorted(set(scopes or SCOPES))
        bad = [s for s in scopes if s not in SCOPES]
        if bad:
            raise ValueError(f"unknown scopes: {bad}")
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
        now = self.clock()
        with self.lock:
            self.db.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
            self.db.execute("DELETE FROM pairing_codes WHERE user_id=?", (user_id,))  # one live code per user
            self.db.execute(
                "INSERT INTO pairing_codes VALUES (?,?,?,?)",
                (_digest(code), user_id, json.dumps(scopes), now + CODE_TTL),
            )
        return code, now + CODE_TTL

    def exchange(self, code, device_name):
        """Extension side. Returns (token, device) or None for an unknown/expired/used code."""
        now = self.clock()
        with self.lock:
            row = self.db.execute(
                "SELECT user_id, scopes FROM pairing_codes WHERE code_hash=? AND expires_at >= ?",
                (_digest(code), now),
            ).fetchone()
            gone = self.db.execute("DELETE FROM pairing_codes WHERE code_hash=?", (_digest(code),)).rowcount
            if not row or gone != 1:
                return None
            live = self.db.execute(
                "SELECT COUNT(*) FROM devices WHERE user_id=? AND revoked_at IS NULL AND expires_at >= ?",
                (row["user_id"], now),
            ).fetchone()[0]
            if live >= MAX_DEVICES_PER_USER:
                raise ValueError(f"at most {MAX_DEVICES_PER_USER} paired devices; revoke one first")
            token = TOKEN_PREFIX + secrets.token_urlsafe(32)
            cur = self.db.execute(
                "INSERT INTO devices (token_hash, user_id, name, scopes, created_at, expires_at) VALUES (?,?,?,?,?,?)",
                (_digest(token), row["user_id"], device_name, row["scopes"], now, now + TOKEN_TTL),
            )
            return token, self._get(cur.lastrowid)

    # ------------------------------------------------------------ authentication
    def authenticate(self, token):
        """Device dict for a live token, else None. Touches last_used_at at most once a minute."""
        if not token or not token.startswith(TOKEN_PREFIX) or len(token) > 128:
            return None
        now = self.clock()
        with self.lock:
            row = self.db.execute(
                f"SELECT {_PUBLIC}, user_id FROM devices WHERE token_hash=?",  # noqa: S608 - _PUBLIC is a constant
                (_digest(token),),
            ).fetchone()
            if not row or row["revoked_at"] is not None or row["expires_at"] < now:
                return None
            if row["last_used_at"] is None or now - row["last_used_at"] > LAST_USED_GRANULARITY:
                self.db.execute("UPDATE devices SET last_used_at=? WHERE id=?", (now, row["id"]))
            return _device(row)

    # ------------------------------------------------------------ management
    def _get(self, device_id):
        return _device(self.db.execute(f"SELECT {_PUBLIC}, user_id FROM devices WHERE id=?", (device_id,)).fetchone())  # noqa: S608

    def list_devices(self, user_id):
        with self.lock:
            rows = self.db.execute(f"SELECT {_PUBLIC} FROM devices WHERE user_id=? ORDER BY id DESC", (user_id,))  # noqa: S608
            return [_device(r) for r in rows.fetchall()]

    def revoke(self, device_id, user_id):
        """Revoke one of `user_id`'s devices. False when it is not theirs or already revoked."""
        with self.lock:
            return (
                self.db.execute(
                    "UPDATE devices SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                    (self.clock(), device_id, user_id),
                ).rowcount
                == 1
            )
