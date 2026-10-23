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
        "jql": get_state(conn, "jql_scope_pending") or get_state(conn, "jql_scope"),
        "in_progress": get_state(conn, "sync_progress") or None,
        "projects": [r[0] for r in conn.execute(
            "SELECT project FROM issues WHERE in_bugs=1 GROUP BY project ORDER BY COUNT(*) DESC")],
    }


def summary(conn) -> dict:
    q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a).fetchall()]
    new_24h = len(list_new(conn, hours=24, limit=10_000))
    return {
        "open": conn.execute("SELECT COUNT(*) FROM issues WHERE in_bugs=1 AND status_category!='done'").fetchone()[0],
        "new_last_24h": new_24h,
        "unassigned_open": conn.execute(
            "SELECT COUNT(*) FROM issues WHERE in_bugs=1 AND status_category!='done' AND assignee IS NULL").fetchone()[0],
        "resolved_last_7d": sum(
            1 for r in conn.execute("SELECT resolved FROM issues WHERE in_bugs=1 AND status_category='done'")
            if (parse_ts(r["resolved"]) or datetime.min.replace(tzinfo=timezone.utc))
            >= datetime.now(timezone.utc) - timedelta(days=7)),
        "unread_alerts": conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE acknowledged=0").fetchone()[0],
        "open_by_priority": q("SELECT COALESCE(priority,'None') AS priority, COUNT(*) AS n FROM issues "
                              "WHERE in_bugs=1 AND status_category!='done' GROUP BY 1 ORDER BY n DESC"),
        "open_by_status": q("SELECT status, COUNT(*) AS n FROM issues WHERE in_bugs=1 AND status_category!='done' "
                            "GROUP BY 1 ORDER BY n DESC"),
        "open_by_project": q("SELECT project, COUNT(*) AS n FROM issues WHERE in_bugs=1 AND status_category!='done' "
                             "GROUP BY 1 ORDER BY n DESC"),
        "sync": health(conn),
    }


def list_open(conn, project=None, assignee=None, priority=None, status=None,
              unassigned_only=False, limit=50) -> list[dict]:
    sql = f"SELECT {OPEN_FIELDS} FROM issues WHERE in_bugs=1 AND status_category != 'done'"
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
    rows = conn.execute(f"SELECT {OPEN_FIELDS} FROM issues WHERE in_bugs=1 ORDER BY created DESC").fetchall()
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
    issue["prs"] = pr_links(conn).get(key, [])
    issue["branches"] = [row_to_dict(r) for r in conn.execute(
        "SELECT repo, name, url, checks, pr_number, pr_state, committed FROM github_branches "
        "WHERE jira_keys LIKE ?", (f'%"{key}"%',))]
    from .deployments import for_ticket
    issue["deployments"] = for_ticket(conn, key)
    issue["comments"] = [dict(r) for r in conn.execute(
        "SELECT author, created, body FROM jira_comments WHERE key=? ORDER BY created DESC LIMIT 20", (key,))]
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


# =========================================================================== 60-day analytics
def _local(ts):
    t = parse_ts(ts)
    return t.astimezone() if t else None


def list_issues(conn, scope: str = "all", limit: int = 2000, settings=None) -> list[dict]:
    """Every tracked bug (open, resolved, or all), most urgent first, with linked PRs (and SLA state when on)."""
    where = {"open": "status_category != 'done'", "resolved": "status_category = 'done'"}.get(scope, "1=1")
    rows = [row_to_dict(r) for r in conn.execute(
        f"SELECT {OPEN_FIELDS}, resolved, reporter FROM issues WHERE in_bugs=1 AND {where} "
        "ORDER BY status_category = 'done', urgency DESC, created DESC LIMIT ?", (int(limit),))]
    links = pr_links(conn)
    sla_on = bool(settings and settings.sla.enabled)
    if sla_on:
        from .quality import sla_status
    for r in rows:
        r["prs"] = links.get(r["key"], [])
        if sla_on and r["status_category"] != "done":
            st = sla_status(conn, r, settings)
            if st:
                left = [x["left_hours"] for x in (st["response"], st["resolve"])
                        if x and x["left_hours"] is not None and x["state"] != "met"]
                r["sla"] = {"state": st["state"], "left_hours": min(left) if left else None,
                            "stage": "response" if st["response"] and st["response"]["state"] in ("warn", "breached")
                            else "fix"}
    return rows


def overview(conn, settings=None, days: int = 60) -> dict:
    now = datetime.now(timezone.utc).astimezone()
    start = (now - timedelta(days=days - 1)).date()
    issues = [row_to_dict(r) for r in conn.execute(
        "SELECT key, status, status_category, priority, assignee, project, created, resolved FROM issues WHERE in_bugs=1")]

    # weekly created vs resolved (weeks start Monday)
    def week(d):
        return d - timedelta(days=d.weekday())
    first_week = week(start)
    weeks = []
    w = first_week
    while w <= now.date():
        weeks.append(w); w += timedelta(days=7)
    created_w = {w: 0 for w in weeks}; resolved_w = {w: 0 for w in weeks}
    daily_delta: dict = {}
    for i in issues:
        c = _local(i["created"])
        if c and c.date() >= first_week:
            created_w[week(c.date())] = created_w.get(week(c.date()), 0) + 1
        if c:
            daily_delta[c.date()] = daily_delta.get(c.date(), 0) + 1
        r = _local(i["resolved"]) if i["status_category"] == "done" else None
        if r and r.date() >= first_week:
            resolved_w[week(r.date())] = resolved_w.get(week(r.date()), 0) + 1
        if r:
            daily_delta[r.date()] = daily_delta.get(r.date(), 0) - 1

    backlog, running, d = [], sum(v for k, v in daily_delta.items() if k < start), start
    while d <= now.date():
        running += daily_delta.get(d, 0)
        backlog.append({"date": d.isoformat(), "open": running})
        d += timedelta(days=1)

    open_i = [i for i in issues if i["status_category"] != "done"]

    def count(rows, key, order=None, top=None):
        c: dict = {}
        for r in rows:
            k = r.get(key) or ("Unassigned" if key == "assignee" else "None")
            c[k] = c.get(k, 0) + 1
        items = sorted(c.items(), key=lambda kv: (order.index(kv[0]) if order and kv[0] in order
                                                    else 99, -kv[1]))
        if top and len(items) > top:
            rest = sum(v for _, v in items[top - 1:])
            items = items[:top - 1] + [("Other", rest)]
        return [{"label": k, "n": v} for k, v in items]

    buckets = [("0–2 days", 0, 2), ("3–7 days", 3, 7), ("8–14 days", 8, 14),
               ("15–30 days", 15, 30), ("31–60 days", 31, 60), ("60+ days", 61, 10**6)]
    aging = []
    for label, lo, hi in buckets:
        n = sum(1 for i in open_i if (c := _local(i["created"])) and lo <= (now - c).days <= hi)
        if n or hi <= 60:
            aging.append({"label": label, "n": n})

    res_hours = [(_local(i["resolved"]) - _local(i["created"])).total_seconds() / 3600
                 for i in issues if i["status_category"] == "done" and i["resolved"] and i["created"]]
    res_hours.sort()
    median_h = res_hours[len(res_hours) // 2] if res_hours else None

    seven = now - timedelta(days=7)
    return {
        "days": days,
        "kpis": {
            "open": len(open_i),
            "created_7d": sum(1 for i in issues if (c := _local(i["created"])) and c >= seven),
            "resolved_7d": sum(1 for i in issues if i["status_category"] == "done"
                               and (r := _local(i["resolved"])) and r >= seven),
            "unassigned": sum(1 for i in open_i if not i["assignee"]),
            "high_open": sum(1 for i in open_i if settings and settings.is_high(i["priority"])),
            "high_names": [p for p in (settings.high_priorities if settings else [])
                           if any((i["priority"] or "").lower() == p.lower() for i in issues)],
            "median_resolve_h": round(median_h, 1) if median_h is not None else None,
            "total": len(issues),
        },
        "weekly": [{"week": w.isoformat(), "created": created_w.get(w, 0),
                    "resolved": resolved_w.get(w, 0)} for w in weeks],
        "backlog": backlog,
        "by_status": count(open_i, "status"),
        "by_priority": sorted(count(open_i, "priority"),
                              key=lambda d: -(settings.priority_rank(d["label"]) if settings else 0)),
        "by_assignee": count(open_i, "assignee", top=8),
        "by_project": count(issues, "project"),
        "aging": aging,
    }


# =========================================================================== GitHub
def pr_links(conn) -> dict[str, list[dict]]:
    links: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT repo, number, title, url, status, state, jira_keys, approvals, "
                          "pending_reviewers, comments_count, unresolved_threads FROM github_prs"):
        for k in json.loads(r["jira_keys"] or "[]"):
            links.setdefault(k, []).append({
                "repo": r["repo"], "number": r["number"], "url": r["url"], "status": r["status"],
                "state": r["state"], "approvals": r["approvals"] or 0,
                "pending": json.loads(r["pending_reviewers"] or "[]"),
                "comments": r["comments_count"] or 0, "unresolved": r["unresolved_threads"] or 0})
    return links


def _pr_summary(r: dict) -> dict:
    """List view: keep reviewers and checks, drop the full conversation."""
    det = r.pop("details", None) or {}
    r["reviewers"] = det.get("reviewers", [])
    r["checks_list"] = det.get("checks", [])
    last = (det.get("conversation") or [None])[0]
    r["last_comment"] = {k: last.get(k) for k in ("author", "at", "kind")} if last else None
    return r


def list_prs(conn, relation: str | None = None, state: str | None = None) -> list[dict]:
    rows = [_pr_summary(row_to_dict(r)) for r in conn.execute(
        "SELECT * FROM github_prs ORDER BY state='OPEN' DESC, updated DESC")]
    if relation:
        rows = [r for r in rows if relation in r["relations"]]
    if state:
        rows = [r for r in rows if r["state"] == state.upper()]
    return rows


def get_pr(conn, repo: str, number: int) -> dict:
    """Everything about one PR: reviewers, conversation, threads, checks, linked Jira tickets."""
    if "/" not in repo:  # allow "web-schedule" for "tcp-software/web-schedule"
        row = conn.execute("SELECT repo FROM github_prs WHERE repo LIKE ? AND number=?",
                           (f"%/{repo}", int(number))).fetchone()
        repo = row["repo"] if row else repo
    r = row_to_dict(conn.execute("SELECT * FROM github_prs WHERE repo=? AND number=?",
                                 (repo, int(number))).fetchone())
    if not r:
        return {"error": f"{repo}#{number} isn't in your synced PRs (last 60 days)."}
    r["tickets"] = []
    for k in r.get("jira_keys") or []:
        t = conn.execute("SELECT key, summary, status, status_category, priority, assignee, url "
                         "FROM issues WHERE key=?", (k,)).fetchone()
        r["tickets"].append(dict(t) if t else {"key": k})
    return r


def list_branches(conn) -> list[dict]:
    return [row_to_dict(r) for r in conn.execute("SELECT * FROM github_branches ORDER BY committed DESC")]


def github_summary(conn) -> dict:
    prs = list_prs(conn)
    mine_open = [p for p in prs if "authored" in p["relations"] and p["state"] == "OPEN"]
    by_status: dict = {}
    for p in mine_open:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    return {
        "login": get_state(conn, "github_login"),
        "last_sync": get_state(conn, "github_last_sync"),
        "last_error": get_state(conn, "github_last_error") or None,
        "my_open": len(mine_open),
        "needs_my_review": sum(1 for p in prs if "review_requested" in p["relations"] and p["state"] == "OPEN"),
        "failing": sum(1 for p in mine_open if p["status"] == "Checks failing"),
        "ready": sum(1 for p in mine_open if p["status"] == "Ready to merge"),
        # Any completed review (approved, changes requested, or commented) counts as dev reviewed.
        "dev_reviewed": sum(1 for p in mine_open
                            if any(r.get("state") and not r.get("pending") for r in p.get("reviewers") or [])),
        "approved": sum(1 for p in mine_open if (p.get("approvals") or 0) > 0),
        "unresolved_threads": sum(p.get("unresolved_threads") or 0 for p in mine_open),
        "merged_60d": sum(1 for p in prs if "authored" in p["relations"] and p["state"] == "MERGED"),
        "branches": conn.execute("SELECT COUNT(*) FROM github_branches").fetchone()[0],
        "my_open_by_status": [{"label": k, "n": v} for k, v in sorted(by_status.items(), key=lambda kv: -kv[1])],
    }


# =========================================================================== my tickets
STATUS_ORDER = ["open", "reopened", "backlog", "to do", "selected for development", "ready",
                "in progress", "in development", "blocked", "code review", "in review", "review",
                "ready for qa", "in qa", "qa", "testing", "ready for release", "done", "closed", "resolved"]
CAT_ORDER = {"new": 0, "indeterminate": 1, "done": 2}


def _status_sort(name: str, cat: str | None) -> tuple:
    n = (name or "").lower()
    return (CAT_ORDER.get(cat or "", 1), STATUS_ORDER.index(n) if n in STATUS_ORDER else 50, n)


def my_tickets(conn) -> dict:
    """Tickets assigned to you, grouped by status, with time spent in every status."""
    from .analytics import status_durations
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT key, summary, status, status_category, priority, issuetype, project, assignee, "
        "created, updated, resolved, url, urgency, reopen_count FROM issues WHERE in_mine=1")]
    now = datetime.now(timezone.utc)
    links = pr_links(conn)
    for r in rows:
        d = status_durations(conn, r["key"])
        r["hours_by_status"] = {k: round(v / 3600, 2) for k, v in d["by_status"].items()}
        r["total_hours"] = round(sum(d["by_status"].values()) / 3600, 2)
        r["active_hours"] = round(d["active_sec"] / 3600, 2)
        r["in_status_hours"] = (None if r["status_category"] == "done"
                                else round(_time_in_current(conn, r, now) / 3600, 2))
        r["work_minutes"] = work_minutes(conn, r["key"])
        r["prs"] = links.get(r["key"], [])
    cats = {r["status"]: r["status_category"] for r in rows}
    statuses = sorted(cats, key=lambda s: _status_sort(s, cats[s]))
    groups = []
    for st in statuses:
        items = sorted((r for r in rows if r["status"] == st), key=lambda r: -(r["urgency"] or 0))
        g = {"status": st, "category": cats[st], "count": len(items), "tickets": items,
             "total_hours": round(sum(i["in_status_hours"] or 0 for i in items), 2)}
        if cats[st] == "done" and items:
            g["avg_cycle_hours"] = round(sum(i["total_hours"] for i in items) / len(items), 2)
        groups.append(g)
    return {"total": len(rows), "statuses": statuses, "groups": groups,
            "me": get_state(conn, "jira_me"), "jql": get_state(conn, "my_jql_scope")}


def _time_in_current(conn, r: dict, now: datetime) -> float:
    last = conn.execute("SELECT ts FROM events WHERE key=? AND kind='status' ORDER BY ts DESC LIMIT 1",
                        (r["key"],)).fetchone()
    since = parse_ts(last["ts"]) if last else parse_ts(r["created"])
    end = parse_ts(r["resolved"]) if r["status_category"] == "done" and r["resolved"] else now
    return max(((end or now) - (since or now)).total_seconds(), 0)
