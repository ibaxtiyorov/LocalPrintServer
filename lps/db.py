"""SQLite persistence (users, devices, jobs, uploads, audit log, settings).

One connection per thread; WAL mode so the web threads, the print worker and
the spooler monitor can read/write concurrently.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    password_changed_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    custom_name TEXT NOT NULL DEFAULT '',
    last_known_name TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_ip TEXT,
    ip_history_json TEXT NOT NULL DEFAULT '[]',
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS ix_devices_last_seen ON devices(last_seen_at);

CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    owner_key TEXT NOT NULL,
    device_id TEXT,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_uploads_owner ON uploads(owner_key);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    user_id INTEGER,
    username TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    device_id TEXT,
    client_ip TEXT,
    filename TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    stored_name TEXT,
    file_size INTEGER,
    page_count INTEGER,
    pages_selected TEXT,
    sheet_count INTEGER,
    options_json TEXT NOT NULL,
    paper TEXT, media TEXT, color TEXT, quality TEXT, orientation TEXT,
    scaling TEXT, margins_mm REAL, copies INTEGER, collated INTEGER,
    reverse_order INTEGER, nup INTEGER, repeat_on_sheet INTEGER NOT NULL DEFAULT 0,
    borderless INTEGER,
    mirror INTEGER, rotate180 INTEGER,
    status TEXT NOT NULL,
    status_detail TEXT,
    error_code TEXT,
    error_message TEXT,
    spooler_job_id INTEGER,
    spooler_doc_name TEXT,
    spooler_seen_printing INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    queued_at TEXT,
    render_started_at TEXT,
    submitted_at TEXT,
    printing_started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS ix_jobs_owner ON jobs(owner_key);
CREATE INDEX IF NOT EXISTS ix_jobs_created ON jobs(created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    actor_ip TEXT,
    action TEXT NOT NULL,
    target TEXT,
    result TEXT NOT NULL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_key TEXT NOT NULL,          -- "user:<id>", "name:<username>", "guest:<id>" or "*" (shared)
    name TEXT NOT NULL,
    options_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_presets_owner ON presets(owner_key);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self.write_lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    # Columns added after v1.0 (schema 2 & 3). Added in place to existing databases.
    _MIGRATIONS = {
        "jobs": [
            ("parts_json", "TEXT"),                              # [{filename, doc_type, stored_name, pages...}]
            ("device_id", "TEXT"),                               # persistent client device identifier
            ("repeat_on_sheet", "INTEGER NOT NULL DEFAULT 0"),   # N-up repeat page on sheet
        ],
        "uploads": [
            ("conversion_ms", "INTEGER"),                        # Word/Excel -> PDF conversion time
            ("device_id", "TEXT"),                               # persistent client device identifier
        ],
        "devices": [
            ("custom_name", "TEXT NOT NULL DEFAULT ''"),
            ("last_known_name", "TEXT NOT NULL DEFAULT ''"),
            ("first_seen_at", "TEXT"),
            ("last_seen_at", "TEXT"),
            ("last_ip", "TEXT"),
            ("ip_history_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("user_agent", "TEXT"),
        ],
    }

    def _init_schema(self):
        with self.write_lock:
            self.conn.executescript(SCHEMA)
            for table, cols in self._MIGRATIONS.items():
                have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for name, decl in cols:
                    if name not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            self.conn.execute("CREATE INDEX IF NOT EXISTS ix_uploads_device ON uploads(device_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_device ON jobs(device_id)")
            self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                              (str(SCHEMA_VERSION),))
            self.conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                              (str(SCHEMA_VERSION),))

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.write_lock:
            return self.conn.execute(sql, params)

    def transaction(self):
        return _Tx(self)

    # ------------------------------------------------------------ device store
    def get_device(self, device_id: str) -> dict | None:
        if not device_id:
            return None
        r = self.one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
        return self._device_dict(r) if r else None

    def list_devices(self) -> list[dict]:
        rows = self.query("SELECT * FROM devices ORDER BY last_seen_at DESC")
        return [self._device_dict(r) for r in rows]

    @staticmethod
    def _device_dict(r: sqlite3.Row) -> dict:
        d = dict(r)
        try:
            d["ip_history"] = json.loads(d.get("ip_history_json") or "[]")
            if not isinstance(d["ip_history"], list):
                d["ip_history"] = []
        except (ValueError, TypeError):
            d["ip_history"] = []
        return d

    def touch_device(self, device_id: str, ip: str | None = None, user_agent: str | None = None,
                     custom_name: str | None = None, last_known_name: str | None = None) -> dict:
        now = utcnow()
        ua = (user_agent or "").strip()[:255] or None
        with self.transaction() as c:
            row = c.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
            if row is None:
                hist = [ip] if ip and ip != "-" else []
                cname = (custom_name or "").strip()[:40]
                lname = (last_known_name or cname or "").strip()[:80]
                c.execute(
                    "INSERT INTO devices(device_id, custom_name, last_known_name, first_seen_at, last_seen_at,"
                    " last_ip, ip_history_json, user_agent) VALUES (?,?,?,?,?,?,?,?)",
                    (device_id, cname, lname, now, now, ip, json.dumps(hist), ua)
                )
            else:
                try:
                    hist = json.loads(row["ip_history_json"] or "[]")
                    if not isinstance(hist, list):
                        hist = []
                except (ValueError, TypeError):
                    hist = []
                if ip and ip != "-":
                    if ip in hist:
                        hist.remove(ip)
                    hist.insert(0, ip)
                    hist = hist[:50]
                existing_lname = row["last_known_name"] if "last_known_name" in row.keys() else ""
                lname = (last_known_name or "").strip()[:80] if last_known_name is not None else existing_lname
                c.execute(
                    "UPDATE devices SET last_known_name = ?, last_seen_at = ?, last_ip = COALESCE(?, last_ip),"
                    " ip_history_json = ?, user_agent = COALESCE(?, user_agent) WHERE device_id = ?",
                    (lname, now, ip, json.dumps(hist), ua, device_id)
                )
        return self.get_device(device_id)

    def set_device_custom_name(self, device_id: str, custom_name: str) -> dict | None:
        cname = (custom_name or "").strip()[:40]
        self.execute("UPDATE devices SET custom_name = ? WHERE device_id = ?", (cname, device_id))
        return self.get_device(device_id)

    def delete_device(self, device_id: str) -> bool:
        return self.execute("DELETE FROM devices WHERE device_id = ?", (device_id,)).rowcount > 0


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db.write_lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db.conn.execute("COMMIT")
            else:
                self.db.conn.execute("ROLLBACK")
        finally:
            self.db.write_lock.release()
        return False
