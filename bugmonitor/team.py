"""Team views: board, sprint, releases, review queue, QA handoff, and stuck work."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .analytics import parse_ts
from .config import Settings
from .db import row_to_dict
from .service import _status_sort, _time_in_current, pr_links

UNASSIGNED = "Unassigned"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rows(conn, where: str = "in_team=1", args=()) -> list[dict]:
    return [row_to_dict(r) for r in conn.execute(
        "SELECT key, summary, status, status_category, priority, issuetype, project, assignee, "
        f"assignee_id, created, updated, resolved, url, urgency, story_points, sprint_ids, fix_versions "
        f"FROM issues WHERE {where}", args)]


def _mini(r: dict, now: datetime, conn, settings: Settings | None = None) -> dict:
    hours = None if r["status_category"] == "done" else round(_time_in_current(conn, r, now) / 3600, 1)
    out = {k: r.get(k) for k in ("key", "summary", "status", "status_category", "priority", "issuetype",
                                 "assignee", "url", "story_points", "project", "resolved", "urgency")}
    out["in_status_hours"] = hours
    if settings and hours is not None:
        limit = stuck_limit_days(settings, r["status"], r["status_category"])
        out["stuck"] = limit is not None and hours / 24 > limit
    return out


def stuck_limit_days(settings: Settings, status: str, category: str | None) -> float | None:
    if category != "indeterminate":
        return None
    sd = {k.lower(): v for k, v in (settings.team.stuck_days or {}).items()}
    return sd.get((status or "").lower(), sd.get("*"))


# --------------------------------------------------------------------------- board
def board(conn, settings: Settings) -> dict:
    now = _now()
    week_ago = now - timedelta(days=7)
    rows = [r for r in _rows(conn) if r["status_category"] != "done"
            or (parse_ts(r["resolved"]) or parse_ts(r["updated"]) or now) >= week_ago]
    cats = {r["status"]: r["status_category"] for r in rows}
    statuses = sorted(cats, key=lambda s: _status_sort(s, cats[s]))
    order = [m.get("name") for m in settings.team.members if m.get("name")]
    # Configured teammates first (even with nothing assigned), then others, then Unassigned.
    people = sorted({r["assignee"] or UNASSIGNED for r in rows} | set(order),
                    key=lambda p: (p == UNASSIGNED, order.index(p) if p in order else len(order), p.lower()))
    out_rows = []
    for p in people:
        mine = [_mini(r, now, conn, settings) for r in rows if (r["assignee"] or UNASSIGNED) == p]
        by_status = {s: sorted((t for t in mine if t["status"] == s), key=lambda t: -(t["urgency"] or 0))
                     for s in statuses}
        out_rows.append({
            "person": p,
            "wip": sum(1 for t in mine if t["status_category"] == "indeterminate"),
            "open": sum(1 for t in mine if t["status_category"] != "done"),
            "done_7d": sum(1 for t in mine if t["status_category"] == "done"),
            "points_open": round(sum(t["story_points"] or 0 for t in mine if t["status_category"] != "done"), 1),
            "stuck": sum(1 for t in mine if t.get("stuck")),
            "tickets": by_status,
        })
    return {"statuses": statuses, "categories": cats, "rows": out_rows,
            "totals": {s: sum(len(r["tickets"][s]) for r in out_rows) for s in statuses},
            "team": settings.team.name}


# --------------------------------------------------------------------------- sprint
def _added_at(conn, r: dict, sprint: dict) -> datetime | None:
    start = parse_ts(sprint["start"])
    when = max([t for t in (start, parse_ts(r["created"])) if t] or [None]) if start else parse_ts(r["created"])
    ev = conn.execute("SELECT ts, to_val FROM events WHERE key=? AND kind='sprint' ORDER BY ts", (r["key"],)).fetchall()
    for e in ev:
        if sprint["name"] and sprint["name"] in (e["to_val"] or ""):
            t = parse_ts(e["ts"])
            if t and (not start or t > start):
                when = max(when, t) if when else t
    return when


def sprint(conn, settings: Settings) -> dict:
    team_rows = _rows(conn, "in_team=1 OR in_mine=1")
    ids = {i for r in team_rows for i in (r["sprint_ids"] or [])}
    sprints = [dict(s) for s in conn.execute("SELECT * FROM sprints WHERE state='active' ORDER BY start")
               if s["id"] in ids]
    now = _now()
    out = []
    closed = {s["id"] for s in conn.execute("SELECT id FROM sprints WHERE state='closed'")}
    for sp in sprints:
        items = [r for r in team_rows if sp["id"] in (r["sprint_ids"] or [])]
        start, end = parse_ts(sp["start"]), parse_ts(sp["end"])
        use_points = any(r["story_points"] for r in items)
        weight = (lambda r: r["story_points"] or 0) if use_points else (lambda r: 1)
        added = {r["key"]: _added_at(conn, r, sp) for r in items}
        late = [r for r in items if start and added[r["key"]] and added[r["key"]] > start + timedelta(hours=12)]
        carried = [r for r in items if set(r["sprint_ids"] or []) & (closed - {sp["id"]})]
        burn = []
        if start:
            last_day = min(now, end or now).date()
            d = start.date()
            while d <= last_day:
                day_end = datetime.combine(d, datetime.max.time(), tzinfo=timezone.utc)
                scope = sum(weight(r) for r in items if (added[r["key"]] or start) <= day_end)
                done = sum(weight(r) for r in items if r["status_category"] == "done"
                           and (parse_ts(r["resolved"]) or parse_ts(r["updated"]) or now) <= day_end)
                burn.append({"date": d.isoformat(), "scope": round(scope, 1), "done": round(done, 1)})
                d += timedelta(days=1)
        by_cat = {c: sum(weight(r) for r in items if r["status_category"] == c) for c in ("new", "indeterminate", "done")}
        total = sum(by_cat.values())
        out.append({
            "id": sp["id"], "name": sp["name"], "goal": sp["goal"], "start": sp["start"], "end": sp["end"],
            "days_left": round(((end - now).total_seconds() / 86400), 1) if end else None,
            "unit": "points" if use_points else "tickets",
            "total": round(total, 1), "by_category": {k: round(v, 1) for k, v in by_cat.items()},
            "done_pct": round(by_cat["done"] / total * 100) if total else 0,
            "added_after_start": [_mini(r, now, conn) for r in late],
            "carried_over": [_mini(r, now, conn) for r in carried],
            "burnup": burn,
            "tickets": sorted((_mini(r, now, conn, settings) for r in items),
                              key=lambda t: (_status_sort(t["status"], t["status_category"]), t["key"])),
        })
    return {"sprints": out}


# --------------------------------------------------------------------------- releases
def releases(conn, settings: Settings) -> dict:
    now = _now()
    rows = _rows(conn, "in_team=1 OR in_bugs=1 OR in_mine=1")
    links = pr_links(conn)
    out = []
    for v in conn.execute("SELECT * FROM versions WHERE archived=0 AND released=0 ORDER BY release_date IS NULL, release_date"):
        items = [r for r in rows if r["project"] == v["project"] and v["name"] in (r["fix_versions"] or [])]
        if not items:
            continue
        rel = parse_ts(v["release_date"] + "T23:59:59+00:00") if v["release_date"] else None
        open_items = [r for r in items if r["status_category"] != "done"]
        blockers = [r for r in open_items if (r["issuetype"] or "").lower() == "bug" and settings.is_high(r["priority"])]
        open_prs = [p for r in items for p in links.get(r["key"], []) if p["state"] == "OPEN"]
        out.append({
            "id": v["id"], "project": v["project"], "name": v["name"], "release_date": v["release_date"],
            "days_left": round((rel - now).total_seconds() / 86400, 1) if rel else None,
            "overdue": bool(rel and rel < now),
            "total": len(items), "done": len(items) - len(open_items),
            "by_category": {c: sum(1 for r in items if r["status_category"] == c) for c in ("new", "indeterminate", "done")},
            "progress": round((len(items) - len(open_items)) / len(items) * 100),
            "blockers": [_mini(r, now, conn) for r in blockers],
            "open_prs": open_prs,
            "open": [_mini(r, now, conn) for r in sorted(open_items, key=lambda r: -(r["urgency"] or 0))],
        })
    return {"releases": out}


# --------------------------------------------------------------------------- review queue
def reviews(conn, settings: Settings) -> dict:
    now = _now()
    prs = [row_to_dict(r) for r in conn.execute("SELECT * FROM github_prs WHERE state='OPEN' AND is_draft=0")]
    queue, load = [], {}
    for p in prs:
        rel = p["relations"] or []
        if not ({"team", "authored"} & set(rel)):
            continue
        det = p.get("details") or {}
        for who in p.get("pending_reviewers") or []:
            load[who] = load.get(who, 0) + 1
        if p["review_decision"] == "APPROVED" or (p["review_decision"] is None and (p["approvals"] or 0) > 0):
            continue
        waiting = (now - (parse_ts(p["created"]) or now)).total_seconds() / 3600
        queue.append({
            "repo": p["repo"], "number": p["number"], "title": p["title"], "url": p["url"], "author": p["author"],
            "status": p["status"], "pending": p.get("pending_reviewers") or [],
            "reviewers": det.get("reviewers", []), "comments": p["comments_count"] or 0,
            "unresolved": p["unresolved_threads"] or 0, "waiting_hours": round(waiting, 1),
            "overdue": waiting > settings.team.pr_review_hours, "jira_keys": p["jira_keys"] or [],
            "additions": p["additions"], "deletions": p["deletions"],
        })
    queue.sort(key=lambda q: -q["waiting_hours"])
    return {"queue": queue, "load": sorted(({"reviewer": k, "pending": v} for k, v in load.items()),
                                           key=lambda x: -x["pending"]),
            "threshold_hours": settings.team.pr_review_hours}


# --------------------------------------------------------------------------- QA handoff
def qa(conn, settings: Settings) -> dict:
    now = _now()
    wanted = {s.lower() for s in settings.team.qa_statuses}
    rows = [r for r in _rows(conn, "in_team=1 OR in_mine=1") if (r["status"] or "").lower() in wanted]
    links = pr_links(conn)
    deployed = _deployed_envs(conn)
    out = []
    for r in rows:
        prs = links.get(r["key"], [])
        merged = [p for p in prs if p["state"] == "MERGED"]
        envs = sorted({e for p in merged for e in deployed.get((p["repo"], p["number"]), [])})
        ready = bool(merged) and not any(p["state"] == "OPEN" for p in prs)
        out.append(dict(_mini(r, now, conn, settings), prs=prs, merged=bool(merged), environments=envs,
                        testable="Deployed" if envs else "Merged, not deployed yet" if ready
                        else "PR still open" if prs else "No PR linked"))
    out.sort(key=lambda t: -(t["in_status_hours"] or 0))
    return {"tickets": out, "statuses": settings.team.qa_statuses}


def _deployed_envs(conn) -> dict:
    """(repo, pr number) -> environments its merge commit reached. Filled by the GitHub sync."""
    out: dict = {}
    for r in conn.execute("SELECT repo, number, details FROM github_prs WHERE state='MERGED'"):
        det = json.loads(r["details"] or "{}")
        envs = det.get("environments") or []
        if envs:
            out[(r["repo"], r["number"])] = envs
    return out


# --------------------------------------------------------------------------- stuck work
def stuck(conn, settings: Settings, only_mine: bool = False) -> dict:
    now = _now()
    where = "in_mine=1" if only_mine else "in_team=1 OR in_mine=1"
    items = []
    for r in _rows(conn, where):
        limit = stuck_limit_days(settings, r["status"], r["status_category"])
        if limit is None:
            continue
        days = _time_in_current(conn, r, now) / 86400
        if days > limit:
            items.append({"type": "ticket", "key": r["key"], "title": r["summary"], "owner": r["assignee"] or UNASSIGNED,
                          "status": r["status"], "days": round(days, 1), "limit_days": limit, "url": r["url"],
                          "priority": r["priority"]})
    if not only_mine:
        for q in reviews(conn, settings)["queue"]:
            if q["overdue"]:
                items.append({"type": "pr", "key": f"{q['repo'].split('/')[-1]}#{q['number']}", "title": q["title"],
                              "owner": q["author"], "status": "Waiting for review", "days": round(q["waiting_hours"] / 24, 1),
                              "limit_days": round(settings.team.pr_review_hours / 24, 2), "url": q["url"],
                              "waiting_on": q["pending"]})
    items.sort(key=lambda i: -(i["days"] / (i["limit_days"] or 1)))
    return {"items": items}
