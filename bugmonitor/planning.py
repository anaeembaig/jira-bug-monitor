"""Planning: sprint and release forecasts, team capacity, and release readiness."""
from __future__ import annotations

import random
import statistics
from datetime import date, datetime, timedelta, timezone

from .analytics import parse_ts, status_durations
from .config import Settings
from .db import row_to_dict
from .timelog import is_workday


def _today() -> date:
    return datetime.now().astimezone().date()


def _workdays(settings: Settings, start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if is_workday(settings, d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _local_date(ts: str | None) -> date | None:
    t = parse_ts(ts)
    return t.astimezone().date() if t else None


# =========================================================================== forecasts
def daily_throughput(conn, settings: Settings, workdays: int = 30) -> dict:
    """Tickets (and points) finished on each of the last N workdays, for the team (or just you)."""
    scope = "in_team=1" if settings.team.jql else "in_mine=1"
    end = _today() - timedelta(days=1)
    days = []
    d = end
    while len(days) < workdays and (end - d).days < workdays * 3:
        if is_workday(settings, d):
            days.append(d)
        d -= timedelta(days=1)
    tickets = {x: 0 for x in days}
    points = {x: 0.0 for x in days}
    for r in conn.execute(f"SELECT resolved, story_points FROM issues WHERE status_category='done' AND ({scope})"):
        rd = _local_date(r["resolved"])
        if rd in tickets:
            tickets[rd] += 1
            points[rd] += r["story_points"] or 0
    return {"days": [x.isoformat() for x in sorted(days)], "tickets": [tickets[x] for x in sorted(days)],
            "points": [round(points[x], 1) for x in sorted(days)]}


def _simulate(samples: list[float], remaining: float, days_left: int, sims: int, rng: random.Random) -> dict:
    if remaining <= 0:
        return {"probability": 100, "p50_days": 0, "p85_days": 0}
    if not samples or not any(samples):
        return {"probability": None, "p50_days": None, "p85_days": None}
    needed = []
    for _ in range(sims):
        total, n = 0.0, 0
        while total < remaining and n < 260:
            total += rng.choice(samples)
            n += 1
        needed.append(n)
    hits = sum(1 for n in needed if n <= days_left)
    needed.sort()
    return {"probability": round(hits / sims * 100), "p50_days": needed[int(sims * 0.5)],
            "p85_days": needed[min(int(sims * 0.85), sims - 1)]}


def _add_workdays(settings: Settings, start: date, n: int) -> date:
    d, left = start, n
    if not settings.timelog.workdays:
        return start + timedelta(days=n)
    while left > 0 and (d - start).days < n * 7 + 14:
        d += timedelta(days=1)
        if is_workday(settings, d):
            left -= 1
    return d


def _verdict(p: int | None) -> str:
    if p is None:
        return "Not enough history"
    return "On track" if p >= 80 else "At risk" if p >= 50 else "Unlikely"


def forecast(conn, settings: Settings, sims: int = 2000, seed: int = 7) -> dict:
    """Monte Carlo: replay recent daily throughput to estimate when open work finishes."""
    from .team import releases, sprint
    rng = random.Random(seed)
    tp = daily_throughput(conn, settings)
    today = _today()
    history_ok = sum(1 for x in tp["tickets"] if x) >= 3
    out_sprints = []
    for sp in sprint(conn, settings)["sprints"]:
        end = _local_date(sp["end"])
        unit = sp["unit"]
        remaining = sp["total"] - sp["by_category"]["done"]
        if unit == "points" and not any(tp["points"]):
            # No finished work has points yet: forecast in tickets instead.
            unit, remaining = "tickets", sum(1 for t in sp["tickets"] if t["status_category"] != "done")
        left = len(_workdays(settings, today, end)) if end else 0
        sim = _simulate(tp[unit] if history_ok else [], remaining, left, sims, rng)
        out_sprints.append({"name": sp["name"], "end": sp["end"], "workdays_left": left, "unit": unit,
                            "remaining": round(remaining, 1), **sim, "verdict": _verdict(sim["probability"]),
                            "likely_finish": _add_workdays(settings, today, sim["p50_days"]).isoformat()
                            if sim["p50_days"] is not None else None,
                            "safe_finish": _add_workdays(settings, today, sim["p85_days"]).isoformat()
                            if sim["p85_days"] is not None else None})
    out_releases = []
    for rel in releases(conn, settings)["releases"]:
        remaining = rel["total"] - rel["done"]
        rd = date.fromisoformat(rel["release_date"]) if rel["release_date"] else None
        left = len(_workdays(settings, today, rd)) if rd else 260
        sim = _simulate(tp["tickets"] if history_ok else [], remaining, left, sims, rng)
        out_releases.append({"project": rel["project"], "name": rel["name"], "release_date": rel["release_date"],
                             "workdays_left": left if rd else None, "remaining": remaining, **sim,
                             "probability": sim["probability"] if rd else None,
                             "verdict": _verdict(sim["probability"]) if rd else "No release date",
                             "likely_finish": _add_workdays(settings, today, sim["p50_days"]).isoformat()
                             if sim["p50_days"] is not None else None,
                             "safe_finish": _add_workdays(settings, today, sim["p85_days"]).isoformat()
                             if sim["p85_days"] is not None else None})
    return {"throughput": tp, "history_ok": history_ok,
            "avg_per_day": round(statistics.mean(tp["tickets"]), 2) if tp["tickets"] else 0,
            "sprints": out_sprints, "releases": out_releases,
            "method": "Replays the last 30 workdays of finished tickets 2,000 times."}


# =========================================================================== capacity
def _time_off(settings: Settings, name: str, days: list[date]) -> dict:
    off: dict = {}
    for t in settings.team.time_off or []:
        if (t.get("name") or "").lower() != name.lower():
            continue
        try:
            a, b = date.fromisoformat(str(t.get("start"))), date.fromisoformat(str(t.get("end") or t.get("start")))
        except ValueError:
            continue
        hours = t.get("hours")
        for d in days:
            if a <= d <= b:
                off[d] = float(hours) if hours is not None else None   # None = whole day
    return off


def _learned_rates(conn) -> tuple[float, dict]:
    """Hours per story point, and typical hands-on hours per issue type, from finished tickets."""
    per_point, by_type = [], {}
    for r in conn.execute("SELECT key, issuetype, story_points FROM issues WHERE status_category='done' "
                          "AND (in_team=1 OR in_mine=1)"):
        active = status_durations(conn, r["key"])["active_sec"] / 3600
        if not active:
            continue
        by_type.setdefault(r["issuetype"] or "Other", []).append(active)
        if r["story_points"]:
            per_point.append(active / r["story_points"])
    return (statistics.median(per_point) if len(per_point) >= 3 else 0,
            {k: statistics.median(v) for k, v in by_type.items() if len(v) >= 3})


def capacity(conn, settings: Settings) -> dict:
    """Available focus hours (minus time off) against the remaining work assigned to each person."""
    from .team import sprint
    today = _today()
    sprints = sprint(conn, settings)["sprints"]
    ends = [_local_date(s["end"]) for s in sprints if s["end"]]
    horizon_end = min(ends) if ends else _add_workdays(settings, today, 9)
    days = _workdays(settings, today, horizon_end)
    sprint_ids = {s["id"] for s in sprints}
    learned_pp, by_type = _learned_rates(conn)
    hpp = settings.team.hours_per_point or learned_pp or 4
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT key, summary, status, status_category, issuetype, assignee, story_points, sprint_ids, url, "
        "original_estimate_sec, time_spent_sec FROM issues WHERE (in_team=1 OR in_mine=1) AND status_category != 'done'")]
    if sprint_ids:
        rows = [r for r in rows if set(r["sprint_ids"] or []) & sprint_ids]
    names = [m["name"] for m in settings.team.members if m.get("name")] or sorted({r["assignee"] for r in rows if r["assignee"]})
    focus = settings.team.focus_hours_per_day
    people = []
    for name in names:
        off = _time_off(settings, name, days)
        available = sum(focus if d not in off else max(focus - (off[d] or focus), 0) for d in days)
        tickets = []
        for r in (r for r in rows if r["assignee"] == name):
            if r["original_estimate_sec"]:
                hours = max(r["original_estimate_sec"] - (r["time_spent_sec"] or 0), 0) / 3600
                basis = "Jira estimate"
            elif r["story_points"]:
                hours, basis = r["story_points"] * hpp, f"{r['story_points']:g} points"
            else:
                hours, basis = by_type.get(r["issuetype"] or "Other", 6), "typical for its type"
            if basis != "Jira estimate" and r["status_category"] == "indeterminate":
                spent = status_durations(conn, r["key"])["active_sec"] / 3600
                hours = max(hours - spent, hours * 0.25)
            tickets.append({"key": r["key"], "summary": r["summary"], "status": r["status"], "url": r["url"],
                            "hours": round(hours, 1), "basis": basis})
        assigned = sum(t["hours"] for t in tickets)
        load = round(assigned / available * 100) if available else None
        people.append({"person": name, "available_hours": round(available, 1), "assigned_hours": round(assigned, 1),
                       "load_pct": load, "days_off": [d.isoformat() for d in sorted(off)],
                       "state": "Away" if not available else "Over" if load > 110 else "Full" if load >= 85 else "Room",
                       "tickets": sorted(tickets, key=lambda t: -t["hours"])})
    unassigned = [r for r in rows if not r["assignee"]]
    return {"start": today.isoformat(), "end": horizon_end.isoformat(), "workdays": len(days),
            "basis": "active sprint" if sprints else "next 10 workdays", "focus_hours_per_day": focus,
            "hours_per_point": round(hpp, 1), "people": people, "unassigned": len(unassigned)}


# =========================================================================== release readiness
def readiness(conn, settings: Settings) -> dict:
    """A go/no-go checklist for each unreleased version."""
    from .deployments import environments_for_tickets
    from .service import pr_links
    now = datetime.now(timezone.utc)
    links = pr_links(conn)
    envs = environments_for_tickets(conn)
    has_deploys = conn.execute("SELECT 1 FROM deployments WHERE kind='deployment' LIMIT 1").fetchone() is not None
    qa = {s.lower() for s in settings.team.qa_statuses}
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT key, summary, status, status_category, issuetype, priority, assignee, url, project, fix_versions "
        "FROM issues WHERE in_team=1 OR in_bugs=1 OR in_mine=1")]
    out = []
    for v in conn.execute("SELECT * FROM versions WHERE archived=0 AND released=0 ORDER BY release_date IS NULL, release_date"):
        items = [r for r in rows if r["project"] == v["project"] and v["name"] in (r["fix_versions"] or [])]
        if not items:
            continue
        mini = lambda r: {k: r[k] for k in ("key", "summary", "status", "assignee", "url")}
        open_items = [r for r in items if r["status_category"] != "done"]
        blockers = [r for r in open_items if (r["issuetype"] or "").lower() == "bug" and settings.is_high(r["priority"])]
        open_prs = [dict(p, key=r["key"]) for r in items for p in links.get(r["key"], []) if p["state"] == "OPEN"]
        no_code = [r for r in items if r["status_category"] == "done" and not links.get(r["key"])
                   and (r["issuetype"] or "").lower() in ("bug", "story", "task")]
        in_qa = [r for r in items if (r["status"] or "").lower() in qa]
        ready_for_test = [r for r in items if r["status_category"] == "done" or (r["status"] or "").lower() in qa]
        not_deployed = [r for r in ready_for_test if not envs.get(r["key"])]
        rel = parse_ts(v["release_date"] + "T23:59:59+00:00") if v["release_date"] else None
        checks = [
            {"name": "All tickets done", "ok": not open_items, "items": [mini(r) for r in open_items],
             "detail": f"{len(items) - len(open_items)} of {len(items)} done"},
            {"name": "No open high-priority bugs", "ok": not blockers, "items": [mini(r) for r in blockers]},
            {"name": "All PRs merged", "ok": not open_prs,
             "items": [{"key": f"{p['repo'].split('/')[-1]}#{p['number']}", "summary": p["key"], "status": p["status"],
                        "url": p["url"]} for p in open_prs]},
            {"name": "QA finished", "ok": not in_qa, "items": [mini(r) for r in in_qa]},
            {"name": "Deployed to a test environment", "ok": not not_deployed if has_deploys else None,
             "items": [mini(r) for r in not_deployed] if has_deploys else [],
             "detail": None if has_deploys else "No deployment data (turn on GitHub deployments)"},
            {"name": "Code linked to every ticket", "ok": not no_code, "warning_only": True,
             "items": [mini(r) for r in no_code]},
            {"name": "Release date ahead", "ok": not (rel and rel < now) if rel else None,
             "detail": v["release_date"] or "No release date"},
        ]
        required = [c for c in checks if c["ok"] is not None and not c.get("warning_only")]
        passed = sum(1 for c in required if c["ok"])
        out.append({"project": v["project"], "name": v["name"], "release_date": v["release_date"],
                    "total": len(items), "ready": passed == len(required), "passed": passed, "required": len(required),
                    "checks": checks})
    return {"releases": out}
