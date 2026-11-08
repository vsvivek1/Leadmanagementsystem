"""SQLite storage. One short-lived connection per call keeps it thread-safe for the scheduler."""
import sqlite3
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


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params=()):
        row = self.one(sql, params)
        return row[0] if row else None

    def run(self, sql: str, params=()) -> int:
        """Execute a write; returns lastrowid."""
        with self.connect() as conn:
            return conn.execute(sql, params).lastrowid

    def get_meta(self, key: str, default=None):
        value = self.scalar("SELECT value FROM meta WHERE key = ?", (key,))
        return default if value is None else value

    def set_meta(self, key: str, value: str) -> None:
        self.run("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
