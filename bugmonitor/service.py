"""Read/write operations shared by the MCP server, dashboard, and CLI."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .analytics import estimate_fix, parse_ts, status_durations
from .db import get_state, now_iso, row_to_dict

WORK_ACTIONS = ("start", "stop", "note", "blocked", "done")
OPEN_FIELDS = ("key, summary, status, status_category, priority, assignee, project, created, "
               "updated, urgency, reopen_count, url, components, labels")


def _key(k: str) -> str:
    return k.strip().upper()


def health(conn) -> dict:
    return {
        "last_sync_started": get_state(conn, "last_sync_started"),
        "last_sync_finished": get_state(conn, "last_sync_finished"),
        "last_sync_stats": json.loads(get_state(conn, "last_sync_stats", "null") or "null"),
        "last_sync_error": get_state(conn, "last_sync_error") or None,
    }


def summary(conn) -> dict:
    q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a).fetchall()]
    new_24h = len(list_new(conn, hours=24, limit=10_000))
    return {
        "open": conn.execute("SELECT COUNT(*) FROM issues WHERE status_category!='done'").fetchone()[0],
        "new_last_24h": new_24h,
        "unassigned_open": conn.execute(
            "SELECT COUNT(*) FROM issues WHERE status_category!='done' AND assignee IS NULL").fetchone()[0],
        "resolved_last_7d": sum(
            1 for r in conn.execute("SELECT resolved FROM issues WHERE status_category='done'")
            if (parse_ts(r["resolved"]) or datetime.min.replace(tzinfo=timezone.utc))
            >= datetime.now(timezone.utc) - timedelta(days=7)),
        "unread_alerts": conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE acknowledged=0").fetchone()[0],
        "open_by_priority": q("SELECT COALESCE(priority,'None') AS priority, COUNT(*) AS n FROM issues "
                              "WHERE status_category!='done' GROUP BY 1 ORDER BY n DESC"),
        "open_by_status": q("SELECT status, COUNT(*) AS n FROM issues WHERE status_category!='done' "
                            "GROUP BY 1 ORDER BY n DESC"),
        "open_by_project": q("SELECT project, COUNT(*) AS n FROM issues WHERE status_category!='done' "
                             "GROUP BY 1 ORDER BY n DESC"),
        "sync": health(conn),
    }


def list_open(conn, project=None, assignee=None, priority=None, status=None,
              unassigned_only=False, limit=50) -> list[dict]:
    sql = f"SELECT {OPEN_FIELDS} FROM issues WHERE status_category != 'done'"
    args: list = []
    if project:
        sql += " AND project = ?"; args.append(project.upper())
    if assignee:
        sql += " AND assignee LIKE ?"; args.append(f"%{assignee}%")
    if priority:
        sql += " AND priority = ?"; args.append(priority)
    if status:
        sql += " AND status = ?"; args.append(status)
    if unassigned_only:
        sql += " AND assignee IS NULL"
    sql += " ORDER BY urgency DESC, created ASC LIMIT ?"; args.append(int(limit))
    return [row_to_dict(r) for r in conn.execute(sql, args).fetchall()]


def list_new(conn, hours: float = 24, limit: int = 100) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = conn.execute(f"SELECT {OPEN_FIELDS} FROM issues ORDER BY created DESC").fetchall()
    out = [row_to_dict(r) for r in rows if (parse_ts(r["created"]) or cutoff) > cutoff]
    return out[:limit]


def timeline(conn, key: str) -> list[dict]:
    key = _key(key)
    ev = [dict(r, source=r["source"]) for r in conn.execute(
        "SELECT ts, kind, from_val, to_val, author, source FROM events WHERE key=?", (key,))]
    work = [{"ts": r["ts"], "kind": f"work:{r['action']}", "from_val": None,
             "to_val": r["note"], "author": r["actor"], "source": f"local:{r['session'] or ''}",
             "minutes": r["minutes"]}
            for r in conn.execute("SELECT * FROM work_log WHERE key=?", (key,))]
    items = ev + work
    items.sort(key=lambda e: parse_ts(e["ts"]) or datetime.min.replace(tzinfo=timezone.utc))
    return items


def get_ticket(conn, key: str) -> dict:
    key = _key(key)
    issue = row_to_dict(conn.execute("SELECT * FROM issues WHERE key=?", (key,)).fetchone())
    if not issue:
        return {"error": f"{key} is not in the local store. It may not be a bug, or sync hasn't run."}
    d = status_durations(conn, key)
    issue["hours_by_status"] = {s: round(v / 3600, 1) for s, v in d["by_status"].items()}
    issue["active_hours"] = round(d["active_sec"] / 3600, 1)
    issue["local_work_minutes"] = work_minutes(conn, key)
    issue["timeline"] = timeline(conn, key)
    issue["estimate"] = estimate_fix(conn, key)
    issue["alerts"] = [dict(r) for r in conn.execute(
        "SELECT id, ts, kind, message, acknowledged FROM alerts WHERE key=? ORDER BY ts DESC", (key,))]
    return issue


def log_work(conn, key: str, action: str, note: str = "", minutes: float | None = None,
             actor: str = "", session: str = "") -> dict:
    key, action = _key(key), action.lower().strip()
    if action not in WORK_ACTIONS:
        return {"error": f"action must be one of {', '.join(WORK_ACTIONS)}"}
    ts = now_iso()
    conn.execute(
        "INSERT INTO work_log(key, ts, action, note, minutes, actor, session) VALUES(?,?,?,?,?,?,?)",
        (key, ts, action, note or None, minutes, actor or None, session or None),
    )
    conn.commit()
    known = conn.execute("SELECT 1 FROM issues WHERE key=?", (key,)).fetchone() is not None
    return {"ok": True, "key": key, "action": action, "ts": ts,
            "total_minutes": work_minutes(conn, key),
            "note": None if known else "Ticket not in local store yet; record kept anyway."}


def _work_rows(conn, key: str | None = None, since: str | None = None):
    sql, args = "SELECT * FROM work_log WHERE 1=1", []
    if key:
        sql += " AND key=?"; args.append(_key(key))
    if since:
        sql += " AND ts >= ?"; args.append(since)
    return conn.execute(sql + " ORDER BY key, ts", args).fetchall()


def _sum_minutes(rows) -> dict[str, float]:
    totals: dict[str, float] = {}
    open_start: dict[str, datetime] = {}
    for r in rows:
        k, t = r["key"], parse_ts(r["ts"])
        totals.setdefault(k, 0.0)
        if r["minutes"]:
            totals[k] += r["minutes"]
        elif r["action"] == "start":
            open_start[k] = t
        elif r["action"] in ("stop", "done", "blocked") and k in open_start:
            totals[k] += (t - open_start.pop(k)).total_seconds() / 60
    now = datetime.now(timezone.utc)
    for k, t in open_start.items():  # still running
        totals[k] += (now - t).total_seconds() / 60
    return {k: round(v, 1) for k, v in totals.items()}


def work_minutes(conn, key: str) -> float:
    return _sum_minutes(_work_rows(conn, key)).get(_key(key), 0.0)


def active_work(conn) -> list[dict]:
    """Tickets whose latest local work entry is 'start' (someone is on it now)."""
    rows = conn.execute(
        "SELECT w.* FROM work_log w JOIN (SELECT key, MAX(id) AS mid FROM work_log "
        "WHERE action IN ('start','stop','done','blocked') GROUP BY key) m ON w.id = m.mid "
        "WHERE w.action='start' ORDER BY w.ts"
    ).fetchall()
    out = []
    for r in rows:
        issue = conn.execute("SELECT summary, status, priority FROM issues WHERE key=?",
                             (r["key"],)).fetchone()
        out.append({"key": r["key"], "since": r["ts"], "actor": r["actor"], "session": r["session"],
                    "note": r["note"], **(dict(issue) if issue else {})})
    return out


def work_report(conn, days: float = 7) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    totals = _sum_minutes(_work_rows(conn, since=since))
    out = []
    for k, mins in sorted(totals.items(), key=lambda kv: -kv[1]):
        issue = conn.execute("SELECT summary, status, priority FROM issues WHERE key=?", (k,)).fetchone()
        entries = conn.execute("SELECT COUNT(*) FROM work_log WHERE key=? AND ts>=?", (k, since)).fetchone()[0]
        out.append({"key": k, "minutes": mins, "hours": round(mins / 60, 2), "entries": entries,
                    **(dict(issue) if issue else {})})
    return out


def list_alerts(conn, unacknowledged_only=True, limit=50) -> list[dict]:
    sql = "SELECT * FROM alerts"
    if unacknowledged_only:
        sql += " WHERE acknowledged=0"
    sql += " ORDER BY id DESC LIMIT ?"
    return [row_to_dict(r) for r in conn.execute(sql, (int(limit),)).fetchall()]


def ack_alerts(conn, alert_id: int | None = None) -> dict:
    if alert_id is None:
        n = conn.execute("UPDATE alerts SET acknowledged=1 WHERE acknowledged=0").rowcount
    else:
        n = conn.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (int(alert_id),)).rowcount
    conn.commit()
    return {"acknowledged": n}
