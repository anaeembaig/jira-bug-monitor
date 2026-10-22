"""SQLite storage shared by the poller, the MCP server, and the dashboard."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    key              TEXT PRIMARY KEY,
    id               TEXT,
    project          TEXT,
    summary          TEXT,
    status           TEXT,
    status_category  TEXT,          -- new | indeterminate | done
    priority         TEXT,
    assignee         TEXT,
    reporter         TEXT,
    created          TEXT,
    updated          TEXT,
    resolved         TEXT,
    components       TEXT,          -- JSON list
    labels           TEXT,          -- JSON list
    original_estimate_sec INTEGER,
    time_spent_sec   INTEGER,
    url              TEXT,
    urgency          REAL DEFAULT 0,
    reopen_count     INTEGER DEFAULT 0,
    first_seen       TEXT,
    last_synced      TEXT
);
CREATE INDEX IF NOT EXISTS ix_issues_cat ON issues(status_category);
CREATE INDEX IF NOT EXISTS ix_issues_created ON issues(created);

-- Full timeline per ticket, from Jira changelog and from Claude sessions.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,        -- created | status | assignee | priority | resolution | work
    from_val   TEXT,
    to_val     TEXT,
    author     TEXT,
    source     TEXT NOT NULL,        -- jira | local
    ext_id     TEXT,                 -- Jira changelog id, for de-duplication
    UNIQUE(key, ext_id, kind)
);
CREATE INDEX IF NOT EXISTS ix_events_key ON events(key, ts);

-- Work records written from any Claude session (CLI or desktop).
CREATE TABLE IF NOT EXISTS work_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    key      TEXT NOT NULL,
    ts       TEXT NOT NULL,
    action   TEXT NOT NULL,          -- start | stop | note | blocked | done
    note     TEXT,
    minutes  REAL,
    actor    TEXT,
    session  TEXT
);
CREATE INDEX IF NOT EXISTS ix_work_key ON work_log(key, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,      -- new_bug | priority_raised | reopened
    priority     TEXT,
    message      TEXT NOT NULL,
    channels     TEXT,               -- JSON list of channels delivered to
    acknowledged INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alerts_ack ON alerts(acknowledged, ts);

CREATE TABLE IF NOT EXISTS sync_state (k TEXT PRIMARY KEY, v TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # many readers + one writer
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def get_state(conn, k: str, default=None):
    row = conn.execute("SELECT v FROM sync_state WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def set_state(conn, k: str, v: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, v),
    )


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in ("components", "labels", "channels"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d
