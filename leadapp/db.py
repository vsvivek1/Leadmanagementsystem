"""Storage: SQLite locally, Postgres (e.g. Supabase) when DATABASE_URL is set.

SQL is written with '?' placeholders; they are translated for Postgres.
"""
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

LEAD_STATUSES = ["new", "contacted", "replied", "interested", "not_interested", "unsubscribed"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    name TEXT DEFAULT '',
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    school TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    city TEXT DEFAULT '',
    fields TEXT DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT DEFAULT '',
    source TEXT DEFAULT '',
    emails_sent INTEGER NOT NULL DEFAULT 0,
    last_emailed_at TEXT,
    last_reply_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    template_id INTEGER NOT NULL REFERENCES templates(id),
    statuses TEXT NOT NULL DEFAULT 'new,contacted',
    interval_days INTEGER NOT NULL DEFAULT 7,
    max_per_lead INTEGER NOT NULL DEFAULT 3,
    daily_limit INTEGER NOT NULL DEFAULT 50,
    active INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    template_id INTEGER,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    message_id TEXT,
    status TEXT NOT NULL,
    error TEXT DEFAULT '',
    sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emails_lead ON emails(lead_id);
CREATE INDEX IF NOT EXISTS idx_emails_msgid ON emails(message_id);
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    email_id INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    message_id TEXT UNIQUE,
    from_addr TEXT NOT NULL,
    subject TEXT DEFAULT '',
    body TEXT DEFAULT '',
    received_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_replies_lead ON replies(lead_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id BIGSERIAL PRIMARY KEY,
    name TEXT DEFAULT '',
    email TEXT NOT NULL UNIQUE,
    school TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    city TEXT DEFAULT '',
    fields TEXT DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT DEFAULT '',
    source TEXT DEFAULT '',
    emails_sent INTEGER NOT NULL DEFAULT 0,
    last_emailed_at TEXT,
    last_reply_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS templates (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    template_id BIGINT NOT NULL REFERENCES templates(id),
    statuses TEXT NOT NULL DEFAULT 'new,contacted',
    interval_days INTEGER NOT NULL DEFAULT 7,
    max_per_lead INTEGER NOT NULL DEFAULT 3,
    daily_limit INTEGER NOT NULL DEFAULT 50,
    active INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emails (
    id BIGSERIAL PRIMARY KEY,
    lead_id BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    campaign_id BIGINT REFERENCES campaigns(id) ON DELETE SET NULL,
    template_id INTEGER,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    message_id TEXT,
    status TEXT NOT NULL,
    error TEXT DEFAULT '',
    sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emails_lead ON emails(lead_id);
CREATE INDEX IF NOT EXISTS idx_emails_msgid ON emails(message_id);
CREATE TABLE IF NOT EXISTS replies (
    id BIGSERIAL PRIMARY KEY,
    lead_id BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    email_id BIGINT REFERENCES emails(id) ON DELETE SET NULL,
    message_id TEXT UNIQUE,
    from_addr TEXT NOT NULL,
    subject TEXT DEFAULT '',
    body TEXT DEFAULT '',
    received_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_replies_lead ON replies(lead_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.I)


class _Conn:
    """Thin wrapper so callers can use '?' placeholders on either database."""

    def __init__(self, raw, pg: bool):
        self.raw, self.pg = raw, pg

    def execute(self, sql: str, params=()):
        if self.pg:
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, tuple(params))


class Database:
    def __init__(self, path: str = "", url: str = ""):
        self.path, self.url = path, url
        self.pg = bool(url)
        self._local = threading.local()
        if not self.pg and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            if self.pg:
                if not conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'leadapp'").fetchone():
                    conn.execute("CREATE SCHEMA leadapp")
                for stmt in PG_SCHEMA.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
            else:
                conn.raw.executescript(SCHEMA)

    def _pg_conn(self):
        import psycopg
        from psycopg.rows import dict_row
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            # prepare_threshold=None keeps it compatible with Supabase's transaction pooler.
            conn = psycopg.connect(self.url, row_factory=dict_row, prepare_threshold=None,
                                   options="-c search_path=leadapp,public", connect_timeout=15)
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self):
        if self.pg:
            conn = self._pg_conn()
            try:
                yield _Conn(conn, True)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    conn.close()
                raise
            return
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield _Conn(conn, False)
            conn.commit()
        finally:
            conn.close()

    def _retry(self, fn):
        """Run fn once more on a fresh connection if a pooled Postgres connection went stale."""
        try:
            return fn()
        except Exception as exc:
            if not self.pg or type(exc).__name__ not in ("OperationalError", "InterfaceError"):
                raise
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
            return fn()

    def all(self, sql: str, params=()) -> list:
        def go():
            with self.connect() as conn:
                return conn.execute(sql, params).fetchall()
        return self._retry(go)

    def one(self, sql: str, params=()):
        def go():
            with self.connect() as conn:
                return conn.execute(sql, params).fetchone()
        return self._retry(go)

    def scalar(self, sql: str, params=()):
        row = self.one(sql, params)
        if row is None:
            return None
        return next(iter(row.values())) if isinstance(row, dict) else row[0]

    def run(self, sql: str, params=()):
        """Execute a write; for INSERTs returns the new row id."""
        match = _INSERT_RE.match(sql)
        returning = self.pg and match and match.group(1).lower() != "meta"

        def go():
            with self.connect() as conn:
                if returning:
                    row = conn.execute(sql + " RETURNING id", params).fetchone()
                    return row["id"]
                cur = conn.execute(sql, params)
                return None if self.pg else cur.lastrowid
        return self._retry(go)

    def get_meta(self, key: str, default=None):
        value = self.scalar("SELECT value FROM meta WHERE key = ?", (key,))
        return default if value is None else value

    def set_meta(self, key: str, value: str) -> None:
        self.run("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
