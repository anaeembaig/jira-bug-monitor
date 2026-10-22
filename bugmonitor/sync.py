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
        "summary": f.get("summary") or "",
        "status": status.get("name"),
        "status_category": (status.get("statusCategory") or {}).get("key"),
        "priority": _name(f.get("priority")),
        "assignee": _name(f.get("assignee"), "displayName"),
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


def _upsert(conn, d: dict, first_seen: str | None) -> None:
    cols = ["key", "id", "project", "summary", "status", "status_category", "priority",
            "assignee", "reporter", "created", "updated", "resolved", "components", "labels",
            "original_estimate_sec", "time_spent_sec", "url", "urgency", "reopen_count",
            "first_seen", "last_synced"]
    vals = dict(d, components=json.dumps(d["components"]), labels=json.dumps(d["labels"]),
                first_seen=first_seen, last_synced=now_iso())
    conn.execute(
        f"INSERT INTO issues({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
        f"ON CONFLICT(key) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("key", "first_seen")),
        [vals.get(c) for c in cols],
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


def _sync_locked(conn, settings: Settings, client: JiraClient, alerter: Alerter | None) -> dict:
    alerter = alerter or Alerter(settings, conn)
    started = datetime.now(timezone.utc)
    last = get_state(conn, "last_sync_started")
    base = settings.jira.jql
    # A changed JQL (new projects, new date range) triggers a full re-sync that also
    # removes tickets no longer in scope. No alerts fire for that re-sync.
    prev_scope = get_state(conn, "jql_scope")
    has_rows = conn.execute("SELECT 1 FROM issues LIMIT 1").fetchone() is not None
    # Rows stored under a different (or unknown) JQL get cleaned up after a full re-sync.
    scope_changed = has_rows and prev_scope != base
    first_sync = last is None or scope_changed
    set_state(conn, "jql_scope_pending", base)
    set_state(conn, "sync_progress", "0")
    conn.commit()

    if first_sync:
        jql = (f"({base}) AND (statusCategory != Done OR updated >= -{settings.jira.backfill_days}d)"
               " ORDER BY updated ASC")
    else:
        minutes = int((started - parse_ts(last)).total_seconds() // 60) + 5  # overlap for safety
        jql = f"({base}) AND updated >= -{minutes}m ORDER BY updated ASC"

    me = get_state(conn, "jira_me")
    if me is None and hasattr(client, "myself"):
        try:
            me = client.myself().get("displayName") or ""
            set_state(conn, "jira_me", me)
        except Exception:
            me = ""

    cats = status_categories(conn)
    stats = {"seen": 0, "new": 0, "changed": 0, "alerts": 0, "first_sync": first_sync,
             "removed": 0}
    seen: set[str] = set()
    cutoff = (started - timedelta(days=settings.jira.retention_days)
              if settings.jira.retention_days else None)
    for raw in client.search(jql):
        d = map_issue(raw, client)
        if cutoff and (parse_ts(d["created"]) or started) < cutoff:
            continue  # outside the retention window: never store or alert
        stats["seen"] += 1
        seen.add(d["key"])
        if d["status"] and d["status_category"]:
            cats[d["status"]] = d["status_category"]
        old = row_to_dict(conn.execute("SELECT * FROM issues WHERE key=?", (d["key"],)).fetchone())

        if old and old["updated"] == d["updated"]:
            continue  # nothing changed since last time

        # Network first, writes second: never hold the database lock while waiting on Jira.
        histories = list(client.changelog(d["key"])) if settings.jira.fetch_changelog else []

        if old is None:
            stats["new"] += 1
            conn.execute(
                "INSERT OR IGNORE INTO events(key, ts, kind, to_val, author, source, ext_id)"
                " VALUES(?,?, 'created', ?, ?, 'jira', 'created')",
                (d["key"], d["created"], d["status"], d["reporter"]),
            )
        else:
            stats["changed"] += 1

        if settings.jira.fetch_changelog:
            _ingest_changelog(conn, d["key"], histories)
        elif old:
            _diff_events(conn, old, d)

        d["reopen_count"] = _reopen_count(conn, d["key"], cats)
        d["urgency"] = urgency_score(d, settings)
        _upsert(conn, d, first_seen=now_iso())
        conn.commit()  # release the write lock after each ticket
        if stats["seen"] % 25 == 0:
            set_state(conn, "sync_progress", str(stats["seen"]))
            conn.commit()
            log.info("Synced %s tickets so far...", stats["seen"])

        # ---- alert rules
        a = settings.alerts
        if old is None:
            if (a.new_bug and d["status_category"] != "done"
                    and (not first_sync or a.alert_on_first_sync)):
                alerter.raise_alert(d, "new_bug"); stats["alerts"] += 1
        else:
            if (a.priority_raised and
                    settings.priority_rank(d["priority"]) > settings.priority_rank(old["priority"])):
                alerter.raise_alert(d, "priority_raised",
                                    f"{old['priority']} → {d['priority']}"); stats["alerts"] += 1
            if (a.reopened and old["status_category"] == "done"
                    and d["status_category"] != "done"):
                alerter.raise_alert(d, "reopened", f"now {d['status']}"); stats["alerts"] += 1
            if (a.assigned_to_me and me and d["assignee"] == me and old["assignee"] != me
                    and d["status_category"] != "done" and not first_sync):
                # Always worth a ping, whatever the priority.
                alerter.raise_alert(d, "assigned_to_me", f"previously {old['assignee'] or 'unassigned'}",
                                    external=True); stats["alerts"] += 1

    # ---- keep the store in scope
    if scope_changed:
        stale = [r["key"] for r in conn.execute("SELECT key FROM issues") if r["key"] not in seen]
        stats["removed"] += _forget(conn, stale)
    if cutoff:
        old_keys = [r["key"] for r in conn.execute("SELECT key, created FROM issues")
                    if (parse_ts(r["created"]) or started) < cutoff]
        stats["removed"] += _forget(conn, old_keys)

    # Age changes urgency even when Jira doesn't, so re-score every open bug.
    for row in conn.execute("SELECT * FROM issues WHERE status_category != 'done'").fetchall():
        d = row_to_dict(row)
        conn.execute("UPDATE issues SET urgency=? WHERE key=?", (urgency_score(d, settings), d["key"]))

    set_state(conn, "status_categories", json.dumps(cats))
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
