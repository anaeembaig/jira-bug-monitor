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

CREATE TABLE IF NOT EXISTS github_prs (
    id TEXT PRIMARY KEY, repo TEXT, number INTEGER, title TEXT, url TEXT,
    state TEXT,                -- OPEN | MERGED | CLOSED
    is_draft INTEGER, review_decision TEXT, checks TEXT, mergeable TEXT,
    head TEXT, base TEXT, author TEXT,
    relations TEXT,            -- JSON: authored | review_requested | reviewed | assigned | involved
    jira_keys TEXT,            -- JSON list of linked Jira keys
    additions INTEGER, deletions INTEGER,
    created TEXT, updated TEXT, merged_at TEXT, closed_at TEXT,
    status TEXT,               -- human label, e.g. "Checks failing"
    last_synced TEXT
);
CREATE TABLE IF NOT EXISTS sprints (
    id INTEGER PRIMARY KEY, board_id INTEGER, name TEXT, state TEXT,
    start TEXT, end TEXT, complete TEXT, goal TEXT
);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY, project TEXT, name TEXT, released INTEGER, archived INTEGER,
    release_date TEXT, description TEXT
);
CREATE TABLE IF NOT EXISTS jira_comments (
    id TEXT PRIMARY KEY, key TEXT, author TEXT, author_id TEXT, created TEXT,
    body TEXT, mentions TEXT        -- JSON list of mentioned account IDs
);
CREATE INDEX IF NOT EXISTS ix_comments_key ON jira_comments(key, created);
CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY, repo TEXT, environment TEXT, sha TEXT, state TEXT,
    created TEXT, updated TEXT, url TEXT
);
CREATE INDEX IF NOT EXISTS ix_deploy_sha ON deployments(sha);
CREATE TABLE IF NOT EXISTS worklogs (
    id TEXT PRIMARY KEY, key TEXT, author TEXT, author_id TEXT, started TEXT, seconds INTEGER,
    comment TEXT, updated TEXT
);
CREATE INDEX IF NOT EXISTS ix_worklogs_started ON worklogs(started);
CREATE TABLE IF NOT EXISTS ticket_refs (    -- minimal info for tickets outside other scopes
    key TEXT PRIMARY KEY, summary TEXT, status TEXT, status_category TEXT, issuetype TEXT,
    project TEXT, priority TEXT, url TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS outbox (     -- Teams/Slack/email messages held for quiet hours
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, target TEXT, title TEXT, body TEXT, url TEXT
);
CREATE TABLE IF NOT EXISTS github_branches (
    repo TEXT, name TEXT, sha TEXT, committed TEXT, message TEXT, checks TEXT,
    pr_number INTEGER, pr_state TEXT, pr_url TEXT, jira_keys TEXT, url TEXT,
    PRIMARY KEY (repo, name)
);
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
    _migrate(conn)
    return conn


# Columns added after the first release; ALTER them into older databases.
MIGRATIONS = {
    "issues": [("issuetype", "TEXT"), ("assignee_id", "TEXT"),
               ("in_bugs", "INTEGER DEFAULT 1"),   # matches the bug JQL
               ("in_mine", "INTEGER DEFAULT 0"),   # matches the my-tickets JQL
               ("in_team", "INTEGER DEFAULT 0"),   # matches the team JQL
               ("story_points", "REAL"), ("sprint_ids", "TEXT"), ("fix_versions", "TEXT")],
    "github_prs": [("merge_state", "TEXT"), ("labels", "TEXT"), ("approvals", "INTEGER"),
                   ("pending_reviewers", "TEXT"), ("comments_count", "INTEGER"),
                   ("unresolved_threads", "INTEGER"), ("details", "TEXT")],
}


def _migrate(conn) -> None:
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


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
    for k in ("components", "labels", "channels", "relations", "jira_keys", "pending_reviewers",
              "details", "sprint_ids", "fix_versions", "mentions"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d
