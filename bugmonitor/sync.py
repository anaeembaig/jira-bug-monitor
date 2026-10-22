"""Pull bugs from Jira into the local store, record history, and raise alerts."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from .alerts import Alerter
from .analytics import parse_ts, status_categories, urgency_score
from .config import Settings
from .db import get_state, now_iso, row_to_dict, set_state
from .jira_client import JiraClient
from .locks import FileLock

log = logging.getLogger("bugmonitor.sync")
TRACKED_FIELDS = {"status": "status", "assignee": "assignee", "priority": "priority",
                  "resolution": "resolution"}


def _name(obj, attr="name"):
    return obj.get(attr) if isinstance(obj, dict) else None


def map_issue(raw: dict, client: JiraClient) -> dict:
    f = raw.get("fields", {})
    status = f.get("status") or {}
    return {
        "key": raw["key"],
        "id": raw.get("id"),
        "project": _name(f.get("project"), "key"),
        "issuetype": _name(f.get("issuetype")),
        "summary": f.get("summary") or "",
        "status": status.get("name"),
        "status_category": (status.get("statusCategory") or {}).get("key"),
        "priority": _name(f.get("priority")),
        "assignee": _name(f.get("assignee"), "displayName"),
        "assignee_id": _name(f.get("assignee"), "accountId"),
        "reporter": _name(f.get("reporter"), "displayName"),
        "created": f.get("created"),
        "updated": f.get("updated"),
        "resolved": f.get("resolutiondate"),
        "components": [c.get("name") for c in f.get("components") or []],
        "labels": f.get("labels") or [],
        "original_estimate_sec": f.get("timeoriginalestimate"),
        "time_spent_sec": f.get("timespent"),
        "url": client.browse_url(raw["key"]),
    }


UPSERT_COLS = ["key", "id", "project", "issuetype", "summary", "status", "status_category",
               "priority", "assignee", "assignee_id", "reporter", "created", "updated", "resolved",
               "components", "labels", "original_estimate_sec", "time_spent_sec", "url", "urgency",
               "reopen_count", "first_seen", "last_synced", "in_bugs", "in_mine"]


def _upsert(conn, d: dict, first_seen: str | None, scope: str) -> None:
    """Insert or update a ticket. `scope` (bugs | mine) is switched on; the other is kept."""
    vals = dict(d, components=json.dumps(d["components"]), labels=json.dumps(d["labels"]),
                first_seen=first_seen, last_synced=now_iso(),
                in_bugs=1 if scope == "bugs" else 0, in_mine=1 if scope == "mine" else 0)
    keep = {"key", "first_seen", "in_bugs", "in_mine"}
    conn.execute(
        f"INSERT INTO issues({','.join(UPSERT_COLS)}) VALUES({','.join('?' * len(UPSERT_COLS))}) "
        f"ON CONFLICT(key) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in UPSERT_COLS if c not in keep)
        + f", in_{scope}=1",
        [vals.get(c) for c in UPSERT_COLS],
    )


def _ingest_changelog(conn, key: str, histories: list[dict]) -> None:
    for h in histories:
        author = _name(h.get("author"), "displayName")
        for item in h.get("items", []):
            kind = TRACKED_FIELDS.get(item.get("field"))
            if not kind:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO events(key, ts, kind, from_val, to_val, author, source, ext_id)"
                " VALUES(?,?,?,?,?,?, 'jira', ?)",
                (key, h.get("created"), kind, item.get("fromString"), item.get("toString"),
                 author, f"{h.get('id')}"),
            )


def _diff_events(conn, old: dict, new: dict) -> None:
    for kind in ("status", "assignee", "priority"):
        if old.get(kind) != new.get(kind):
            conn.execute(
                "INSERT OR IGNORE INTO events(key, ts, kind, from_val, to_val, source, ext_id)"
                " VALUES(?,?,?,?,?, 'jira', ?)",
                (new["key"], new["updated"], kind, old.get(kind), new.get(kind),
                 f"diff-{new['updated']}"),
            )


def _reopen_count(conn, key: str, cats: dict) -> int:
    rows = conn.execute(
        "SELECT from_val, to_val FROM events WHERE key=? AND kind='status'", (key,)
    ).fetchall()
    return sum(1 for r in rows if cats.get(r["from_val"]) == "done"
               and cats.get(r["to_val"]) not in (None, "done"))


def _forget(conn, keys: list[str]) -> int:
    """Drop tickets from the dashboard. Local work logs are kept for your records."""
    for k in keys:
        for table in ("issues", "events", "alerts"):
            conn.execute(f"DELETE FROM {table} WHERE key=?", (k,))
    return len(keys)


def sync_once(conn, settings: Settings, client: JiraClient, alerter: Alerter | None = None) -> dict:
    """Run one sync. Only one process may sync at a time; others skip instead of waiting."""
    lock = FileLock(settings.db_path.parent / "sync.lock")
    if not lock.acquire():
        return {"skipped": True,
                "reason": f"Another sync is already running (pid {lock.holder_pid()})."}
    try:
        return _sync_locked(conn, settings, client, alerter)
    finally:
        lock.release()


class _Ctx:
    """Shared state for one sync run."""

    def __init__(self, conn, settings, client, alerter, started):
        self.conn, self.s, self.client, self.alerter, self.started = conn, settings, client, alerter, started
        self.cats = status_categories(conn)
        self.me_id, self.me_name = _whoami(conn, client)
        self.stats = {"seen": 0, "new": 0, "changed": 0, "alerts": 0, "removed": 0,
                      "first_sync": False, "mine": 0}

    def is_me(self, d: dict | None) -> bool:
        if not d:
            return False
        if self.me_id and d.get("assignee_id"):
            return d["assignee_id"] == self.me_id
        return bool(self.me_name) and d.get("assignee") == self.me_name

    def alert(self, d, kind, detail="", external=None):
        self.alerter.raise_alert(d, kind, detail, external=external)
        self.stats["alerts"] += 1


def _whoami(conn, client) -> tuple[str, str]:
    me_id, me_name = get_state(conn, "jira_me_id"), get_state(conn, "jira_me")
    if (me_id is None or me_name is None) and hasattr(client, "myself"):
        try:
            me = client.myself()
            me_id, me_name = me.get("accountId") or "", me.get("displayName") or ""
            set_state(conn, "jira_me_id", me_id)
            set_state(conn, "jira_me", me_name)
        except Exception:
            pass
    return me_id or "", me_name or ""


def _process(ctx: _Ctx, raw: dict, scope: str, quiet: bool) -> None:
    """Store one ticket from either query and raise any alerts. `quiet` = initial load."""
    conn, s, a = ctx.conn, ctx.s, ctx.s.alerts
    d = map_issue(raw, ctx.client)
    if d["status"] and d["status_category"]:
        ctx.cats[d["status"]] = d["status_category"]
    old = row_to_dict(conn.execute("SELECT * FROM issues WHERE key=?", (d["key"],)).fetchone())
    if old and old["updated"] == d["updated"]:
        # Unchanged, but make sure it's flagged for this scope.
        conn.execute(f"UPDATE issues SET in_{scope}=1 WHERE key=?", (d["key"],))
        return

    # Network first, writes second: never hold the database lock while waiting on Jira.
    histories = list(ctx.client.changelog(d["key"])) if s.jira.fetch_changelog else []
    if old is None:
        ctx.stats["new"] += 1
        conn.execute(
            "INSERT OR IGNORE INTO events(key, ts, kind, to_val, author, source, ext_id)"
            " VALUES(?,?, 'created', ?, ?, 'jira', 'created')",
            (d["key"], d["created"], d["status"], d["reporter"]))
    else:
        ctx.stats["changed"] += 1
    if s.jira.fetch_changelog:
        _ingest_changelog(conn, d["key"], histories)
    elif old:
        _diff_events(conn, old, d)
    d["reopen_count"] = _reopen_count(conn, d["key"], ctx.cats)
    d["urgency"] = urgency_score(d, s)
    _upsert(conn, d, first_seen=now_iso(), scope=scope)
    conn.commit()  # release the write lock after each ticket

    if quiet:
        return
    open_now = d["status_category"] != "done"
    if old is None:
        if scope == "bugs" and a.new_bug and open_now:
            ctx.alert(d, "new_bug", "assigned to you" if ctx.is_me(d) else "")
        elif scope == "mine" and a.assigned_to_me and open_now:
            ctx.alert(d, "assigned_to_me", d.get("issuetype") or "", external=True)
        return
    if old.get("in_bugs") and a.priority_raised and \
            s.priority_rank(d["priority"]) > s.priority_rank(old["priority"]):
        ctx.alert(d, "priority_raised", f"{old['priority']} → {d['priority']}")
    if old.get("in_bugs") and a.reopened and old["status_category"] == "done" and open_now:
        ctx.alert(d, "reopened", f"now {d['status']}")
    if a.assigned_to_me and ctx.is_me(d) and not ctx.is_me(old) and open_now:
        ctx.alert(d, "assigned_to_me", f"previously {old['assignee'] or 'unassigned'}", external=True)
    elif a.my_status_changes and ctx.is_me(d) and old["status"] != d["status"]:
        ctx.alert(d, "my_status", f"{old['status']} → {d['status']}", external=True)


def _sync_locked(conn, settings: Settings, client: JiraClient, alerter: Alerter | None) -> dict:
    started = datetime.now(timezone.utc)
    ctx = _Ctx(conn, settings, client, alerter or Alerter(settings, conn), started)
    stats = ctx.stats
    last = get_state(conn, "last_sync_started")
    base = settings.jira.jql
    # Rows stored under a different (or unknown) JQL get cleaned up after a full re-sync.
    prev_scope = get_state(conn, "jql_scope")
    has_rows = conn.execute("SELECT 1 FROM issues WHERE in_bugs=1 LIMIT 1").fetchone() is not None
    scope_changed = has_rows and prev_scope != base
    first_sync = last is None or scope_changed
    stats["first_sync"] = first_sync
    set_state(conn, "jql_scope_pending", base)
    set_state(conn, "sync_progress", "0")
    conn.commit()

    if first_sync:
        jql = (f"({base}) AND (statusCategory != Done OR updated >= -{settings.jira.backfill_days}d)"
               " ORDER BY updated ASC")
    else:
        minutes = int((started - parse_ts(last)).total_seconds() // 60) + 5  # overlap for safety
        jql = f"({base}) AND updated >= -{minutes}m ORDER BY updated ASC"

    cutoff = (started - timedelta(days=settings.jira.retention_days)
              if settings.jira.retention_days else None)
    seen: set[str] = set()
    for raw in client.search(jql):
        created = parse_ts((raw.get("fields") or {}).get("created"))
        if cutoff and (created or started) < cutoff:
            continue  # outside the retention window: never store or alert
        stats["seen"] += 1
        seen.add(raw["key"])
        _process(ctx, raw, "bugs", quiet=first_sync and not settings.alerts.alert_on_first_sync)
        if stats["seen"] % 25 == 0:
            set_state(conn, "sync_progress", str(stats["seen"]))
            conn.commit()
            log.info("Synced %s tickets so far...", stats["seen"])

    # ---- my tickets: small set, so re-read in full every time
    my_jql = settings.jira.my_jql.strip()
    if my_jql:
        mine_first = get_state(conn, "my_jql_scope") != my_jql
        mine_seen: set[str] = set()
        for raw in client.search(f"({my_jql}) ORDER BY updated DESC"):
            mine_seen.add(raw["key"])
            _process(ctx, raw, "mine", quiet=mine_first)
        for r in conn.execute("SELECT key FROM issues WHERE in_mine=1").fetchall():
            if r["key"] not in mine_seen:
                conn.execute("UPDATE issues SET in_mine=0 WHERE key=?", (r["key"],))
        stats["mine"] = len(mine_seen)
        set_state(conn, "my_jql_scope", my_jql)
    else:
        conn.execute("UPDATE issues SET in_mine=0")

    # ---- keep the store in scope (your own tickets are kept while they're yours)
    if scope_changed:
        for r in conn.execute("SELECT key FROM issues WHERE in_bugs=1").fetchall():
            if r["key"] not in seen:
                conn.execute("UPDATE issues SET in_bugs=0 WHERE key=?", (r["key"],))
    if cutoff:
        for r in conn.execute("SELECT key, created FROM issues WHERE in_bugs=1").fetchall():
            if (parse_ts(r["created"]) or started) < cutoff:
                conn.execute("UPDATE issues SET in_bugs=0 WHERE key=?", (r["key"],))
    orphans = [r["key"] for r in conn.execute("SELECT key FROM issues WHERE in_bugs=0 AND in_mine=0")]
    stats["removed"] += _forget(conn, orphans)

    # Age changes urgency even when Jira doesn't, so re-score every open ticket.
    for row in conn.execute("SELECT * FROM issues WHERE status_category != 'done'").fetchall():
        d = row_to_dict(row)
        conn.execute("UPDATE issues SET urgency=? WHERE key=?", (urgency_score(d, settings), d["key"]))

    set_state(conn, "status_categories", json.dumps(ctx.cats))
    set_state(conn, "last_sync_started", started.isoformat())
    set_state(conn, "last_sync_finished", now_iso())
    set_state(conn, "last_sync_stats", json.dumps(stats))
    set_state(conn, "jql_scope", base)
    set_state(conn, "sync_progress", "")
    set_state(conn, "last_sync_error", "")
    conn.commit()
    log.info("Sync done: %s", stats)
    return stats


def sync_github_safe(conn, settings: Settings, alerter: Alerter | None = None) -> dict | None:
    """Run the GitHub sync if enabled; record errors instead of raising."""
    if not settings.github.enabled:
        return None
    from .github_client import GitHubClient, resolve_token
    try:
        from .github_sync import sync_github
        client = GitHubClient(resolve_token(settings.github.token))
        return sync_github(conn, settings, client, alerter)
    except Exception as e:
        conn.rollback()
        set_state(conn, "github_last_error", f"{now_iso()} {e}")
        conn.commit()
        log.warning("GitHub sync failed: %s", e)
        return {"error": str(e)}


def run_forever(conn, settings: Settings, client: JiraClient, stop_event=None) -> None:
    alerter = Alerter(settings, conn)
    interval = max(settings.jira.poll_minutes, 1) * 60
    while not (stop_event and stop_event.is_set()):
        try:
            sync_once(conn, settings, client, alerter)
        except Exception as e:
            conn.rollback()
            set_state(conn, "last_sync_error", f"{now_iso()} {e}")
            conn.commit()
            log.exception("Sync failed")
        sync_github_safe(conn, settings, alerter)
        if stop_event:
            stop_event.wait(interval)
        else:
            time.sleep(interval)
