"""Daily time log: Jira worklogs, time tracked from Claude, and ticket activity, by day.

Sources, per day and ticket:
  logged   Jira worklogs (the official record)
  tracked  `log_work` start/stop entries from any Claude session
  activity status changes and comments you made in Jira, PRs you opened, merged, or reviewed
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from .analytics import parse_ts
from .config import Settings
from .db import get_state, now_iso, set_state

log = logging.getLogger("bugmonitor.timelog")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _local_day(ts: str | None) -> str | None:
    t = parse_ts(ts)
    return t.astimezone().date().isoformat() if t else None


def is_workday(settings: Settings, d: date) -> bool:
    wanted = {w.strip().lower()[:3] for w in settings.timelog.workdays}
    return DAY_NAMES[d.weekday()] in wanted


# --------------------------------------------------------------------------- sync
def _adf_plain(body) -> str:
    from .sync import _adf_text
    return _adf_text(body, []).strip() if isinstance(body, dict) else (body or "")


def sync_worklogs(conn, settings: Settings, client, me_id: str = "") -> dict:
    """Your worklogs on any ticket, plus teammates' worklogs on team tickets."""
    t = settings.timelog
    if not t.enabled or not hasattr(client, "worklogs"):
        return {"skipped": True}
    started = datetime.now(timezone.utc)
    since = started - timedelta(days=t.days)
    since_ms = int(since.timestamp() * 1000)
    last = get_state(conn, "worklogs_last_sync")
    minutes = int((started - parse_ts(last)).total_seconds() // 60) + 10 if last else None
    recent = f" AND updated >= -{minutes}m" if minutes else ""
    queries = [f"worklogAuthor = currentUser() AND worklogDate >= -{t.days}d{recent}"]
    if t.team and settings.team.jql:
        queries.append(f"({settings.team.jql}) AND worklogDate >= -{t.days}d{recent}")
    keys: dict[str, dict] = {}
    for q in queries:
        for raw in client.search(q):
            keys[raw["key"]] = raw
    # Deleting the last worklog on a ticket takes it out of the searches above, so once an hour
    # re-check every ticket we hold worklogs for in the window.
    hour = started.strftime("%Y-%m-%dT%H")
    if get_state(conn, "worklogs_recheck_hour") != hour:
        for r in conn.execute("SELECT DISTINCT key FROM worklogs WHERE started >= ?", (since.isoformat(),)):
            keys.setdefault(r["key"], None)
        set_state(conn, "worklogs_recheck_hour", hour)
    stored = 0
    for key, raw in keys.items():
        try:
            wls = client.worklogs(key, since_ms)
        except Exception as e:
            log.warning("Worklogs for %s failed: %s", key, e)
            continue
        f = (raw or {}).get("fields", {})
        status = f.get("status") or {}
        if raw is not None:
            conn.execute(
                "INSERT OR REPLACE INTO ticket_refs(key, summary, status, status_category, issuetype, project, priority, url) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (key, f.get("summary"), status.get("name"), (status.get("statusCategory") or {}).get("key"),
                 (f.get("issuetype") or {}).get("name"), (f.get("project") or {}).get("key"),
                 (f.get("priority") or {}).get("name"), client.browse_url(key)))
        ids = set()
        for w in wls:
            a = w.get("author") or {}
            ids.add(str(w["id"]))
            conn.execute(
                "INSERT OR REPLACE INTO worklogs(id, key, author, author_id, started, seconds, comment, updated) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (str(w["id"]), key, a.get("displayName"), a.get("accountId"), w.get("started"),
                 int(w.get("timeSpentSeconds") or 0), _adf_plain(w.get("comment"))[:500], w.get("updated")))
            stored += 1
        # Worklogs deleted in Jira disappear here too (within the window).
        for r in conn.execute("SELECT id, started FROM worklogs WHERE key=?", (key,)).fetchall():
            if r["id"] not in ids and (parse_ts(r["started"]) or started) >= since:
                conn.execute("DELETE FROM worklogs WHERE id=?", (r["id"],))
        conn.commit()
    old = (since - timedelta(days=1)).isoformat()
    conn.execute("DELETE FROM worklogs WHERE started < ?", (old,))
    set_state(conn, "worklogs_last_sync", started.isoformat())
    conn.commit()
    return {"tickets": len(keys), "worklogs": stored}


# --------------------------------------------------------------------------- views
def _ticket(conn, key: str) -> dict:
    r = conn.execute("SELECT key, summary, status, status_category, issuetype, project, priority, url "
                     "FROM issues WHERE key=?", (key,)).fetchone() or \
        conn.execute("SELECT * FROM ticket_refs WHERE key=?", (key,)).fetchone()
    return dict(r) if r else {"key": key, "summary": "", "status": None, "url": None}


def _tracked_minutes(conn, since: str) -> dict:
    """(day, key) -> minutes from Claude `log_work` entries, start/stop pairs split by day."""
    from .service import _work_rows
    out: dict = {}
    open_start: dict = {}
    now = datetime.now(timezone.utc)
    for r in _work_rows(conn, since=since):
        k, t = r["key"], parse_ts(r["ts"])
        if r["minutes"]:
            day = _local_day(r["ts"])
            out[(day, k)] = out.get((day, k), 0) + r["minutes"]
        elif r["action"] == "start":
            open_start[k] = t
        elif r["action"] in ("stop", "done", "blocked") and k in open_start:
            _add_span(out, k, open_start.pop(k), t)
    for k, t in open_start.items():
        _add_span(out, k, t, now)
    return out


def _add_span(out: dict, key: str, a: datetime, b: datetime) -> None:
    a, b = a.astimezone(), b.astimezone()
    while a < b:
        end = min(b, datetime.combine(a.date() + timedelta(days=1), datetime.min.time(), tzinfo=a.tzinfo))
        day = a.date().isoformat()
        out[(day, key)] = out.get((day, key), 0) + (end - a).total_seconds() / 60
        a = end


def _activity(conn, settings: Settings, since: str, me_name: str, me_id: str, gh_login: str) -> dict:
    """(day, key) -> list of things you did on the ticket that day."""
    out: dict = {}

    def add(day, key, text):
        if day and key:
            out.setdefault((day, key), []).append(text)

    for e in conn.execute("SELECT key, ts, kind, from_val, to_val FROM events WHERE author=? AND ts >= ? "
                          "AND kind IN ('status','assignee','priority')", (me_name, since)):
        add(_local_day(e["ts"]), e["key"], f"{e['kind']}: {e['from_val'] or '—'} → {e['to_val'] or '—'}")
    for c in conn.execute("SELECT key, created FROM jira_comments WHERE author_id=? AND created >= ?", (me_id, since)):
        add(_local_day(c["created"]), c["key"], "commented")
    if gh_login:
        for p in conn.execute("SELECT repo, number, author, created, merged_at, jira_keys, details FROM github_prs"):
            keys = json.loads(p["jira_keys"] or "[]")
            ref = f"{p['repo'].split('/')[-1]}#{p['number']}"
            for k in keys:
                if (p["author"] or "").lower() == gh_login.lower():
                    if (p["created"] or "") >= since:
                        add(_local_day(p["created"]), k, f"opened {ref}")
                    if p["merged_at"] and p["merged_at"] >= since:
                        add(_local_day(p["merged_at"]), k, f"merged {ref}")
                for c in (json.loads(p["details"] or "{}").get("conversation") or []):
                    if (c.get("author") or "").lower() == gh_login.lower() and (c.get("at") or "") >= since:
                        add(_local_day(c["at"]), k, f"{'reviewed' if c['kind'] == 'review' else 'commented on'} {ref}")
    return out


def timesheet(conn, settings: Settings, days: int | None = None, person: str | None = None) -> dict:
    """Your (or one teammate's) day-by-day time: logged in Jira, tracked from Claude, and activity."""
    days = days or settings.timelog.days
    today = datetime.now().astimezone().date()
    first = today - timedelta(days=days - 1)
    since = datetime.combine(first, datetime.min.time()).astimezone().astimezone(timezone.utc).isoformat()
    me_name, me_id = get_state(conn, "jira_me") or "", get_state(conn, "jira_me_id") or ""
    who = person or me_name
    mine = who == me_name
    logged: dict = {}
    comments: dict = {}
    q = "SELECT key, started, seconds, comment FROM worklogs WHERE started >= ? AND "
    args = [since]
    if mine and me_id:
        q += "author_id=?"; args.append(me_id)
    else:
        q += "author=?"; args.append(who)
    for w in conn.execute(q, args):
        k = (_local_day(w["started"]), w["key"])
        logged[k] = logged.get(k, 0) + w["seconds"] / 3600
        if w["comment"]:
            comments.setdefault(k, []).append(w["comment"])
    tracked = {k: v / 60 for k, v in _tracked_minutes(conn, since).items()} if mine else {}
    activity = _activity(conn, settings, since, me_name, me_id, get_state(conn, "github_login") or "") if mine else {}

    target = settings.timelog.daily_target_hours
    out_days = []
    d = today
    while d >= first:
        ds = d.isoformat()
        keys = {k for (day, k) in list(logged) + list(tracked) + list(activity) if day == ds}
        rows = []
        for k in keys:
            rows.append(dict(_ticket(conn, k), logged_hours=round(logged.get((ds, k), 0), 2),
                             tracked_hours=round(tracked.get((ds, k), 0), 2),
                             activity=activity.get((ds, k), []), comments=comments.get((ds, k), [])))
        rows.sort(key=lambda r: (-r["logged_hours"], -r["tracked_hours"], r["key"]))
        total = round(sum(r["logged_hours"] for r in rows), 2)
        workday = is_workday(settings, d)
        out_days.append({
            "date": ds, "weekday": d.strftime("%a"), "workday": workday,
            "logged_hours": total, "tracked_hours": round(sum(r["tracked_hours"] for r in rows), 2),
            "target_hours": target if workday else 0,
            "short_hours": round(max(target - total, 0), 2) if workday and d <= today else 0,
            "unlogged_tickets": [r["key"] for r in rows if not r["logged_hours"] and (r["activity"] or r["tracked_hours"])],
            "tickets": rows,
        })
        d -= timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    week = [x for x in out_days if x["date"] >= week_start.isoformat()]
    return {"person": who, "days": out_days, "target_hours": target,
            "week": {"logged": round(sum(x["logged_hours"] for x in week), 2),
                     "target": round(sum(x["target_hours"] for x in week if x["date"] <= today.isoformat()), 2)},
            "can_write": settings.timelog.allow_jira_write}


def team_time(conn, settings: Settings, days: int = 10) -> dict:
    """Hours logged in Jira per teammate per day (worklogs on team tickets and your own)."""
    today = datetime.now().astimezone().date()
    dates = [(today - timedelta(days=i)) for i in range(days - 1, -1, -1)]
    since = datetime.combine(dates[0], datetime.min.time()).astimezone().astimezone(timezone.utc).isoformat()
    grid: dict = {}
    tickets: dict = {}
    for w in conn.execute("SELECT author, key, started, seconds FROM worklogs WHERE started >= ?", (since,)):
        day = _local_day(w["started"])
        grid.setdefault(w["author"], {}).setdefault(day, 0)
        grid[w["author"]][day] += w["seconds"] / 3600
        tickets.setdefault(w["author"], set()).add(w["key"])
    names = [m.get("name") for m in settings.team.members if m.get("name")]
    people = names + sorted(p for p in grid if p not in names)
    rows = [{"person": p, "days": {d.isoformat(): round(grid.get(p, {}).get(d.isoformat(), 0), 2) for d in dates},
             "total": round(sum(grid.get(p, {}).values()), 2), "tickets": len(tickets.get(p, ()))} for p in people]
    return {"dates": [{"date": d.isoformat(), "weekday": d.strftime("%a"), "workday": is_workday(settings, d)}
                      for d in dates], "rows": rows, "target_hours": settings.timelog.daily_target_hours}


# --------------------------------------------------------------------------- writing to Jira (opt-in)
def log_to_jira(conn, settings: Settings, client, key: str, minutes: float, day: str | None = None,
                comment: str = "") -> dict:
    if not settings.timelog.allow_jira_write:
        return {"error": "Logging time to Jira is off. Set allow_jira_write = true under [timelog]."}
    if not (0 < minutes <= 24 * 60):
        return {"error": "Minutes must be between 1 and 1440."}
    d = date.fromisoformat(day) if day else datetime.now().date()
    start = datetime.combine(d, datetime.min.time()).replace(hour=9).astimezone()
    started = start.strftime("%Y-%m-%dT%H:%M:%S.000%z")
    w = client.add_worklog(key.upper(), int(minutes * 60), started, comment)
    a = w.get("author") or {}
    conn.execute("INSERT OR REPLACE INTO worklogs(id, key, author, author_id, started, seconds, comment, updated) "
                 "VALUES(?,?,?,?,?,?,?,?)", (str(w.get("id")), key.upper(), a.get("displayName"), a.get("accountId"),
                                            w.get("started") or started, int(minutes * 60), comment, now_iso()))
    conn.execute("INSERT INTO audit(ts, actor, action, detail) VALUES(?,?,?,?)",
                 (now_iso(), "dashboard/claude", "jira_worklog", f"{key.upper()} {minutes} min on {d}"))
    conn.commit()
    return {"ok": True, "key": key.upper(), "minutes": minutes, "day": d.isoformat(), "id": w.get("id")}


# --------------------------------------------------------------------------- reminders
def reminder(conn, settings: Settings, alerter, now: datetime | None = None) -> str | None:
    """End-of-day nudge when today's Jira time is under target. Once per day, workdays only."""
    t = settings.timelog
    now = now or datetime.now()
    if not (t.enabled and t.reminder_time and is_workday(settings, now.date())):
        return None
    h, m = (t.reminder_time + ":0").split(":")[:2]
    if (now.hour, now.minute) < (int(h), int(m)) or get_state(conn, "timelog_reminder") == now.date().isoformat():
        return None
    ts = timesheet(conn, settings, days=7)
    today = ts["days"][0]
    set_state(conn, "timelog_reminder", now.date().isoformat())
    conn.commit()
    if today["logged_hours"] >= today["target_hours"]:
        return None
    lines = []
    for r in today["tickets"]:
        bits = []
        if r["logged_hours"]:
            bits.append(f"{r['logged_hours']:g}h logged")
        if r["tracked_hours"]:
            bits.append(f"{r['tracked_hours']:.1f}h tracked in Claude")
        if r["activity"]:
            bits.append(", ".join(r["activity"][:2]))
        lines.append((f"{r['key']} {r['summary'] or ''}".strip(), "; ".join(bits), r.get("url")))
    short_days = [d for d in ts["days"][1:] if d["workday"] and d["short_hours"] > 0
                  and d["date"] >= (now.date() - timedelta(days=now.weekday())).isoformat()]
    detail = f"{today['logged_hours']:g}h of {today['target_hours']:g}h logged today"
    if short_days:
        detail += "; also short on " + ", ".join(f"{d['weekday']} ({d['logged_hours']:g}h)" for d in short_days)
    from .alerts import summary_card
    card = summary_card(f"Time log: {detail}", lines or [("No ticket activity found today", "", None)],
                        settings.dashboard, "Warning")
    if settings.alerts.teams_webhook:
        alerter.post_card(settings.alerts.teams_webhook, card)
    alerter.raise_alert({"key": "Time log", "summary": detail, "url": None}, "timelog", "", external=False, team=False)
    return detail


def recap_lines(conn, settings: Settings) -> list[str]:
    """For the morning digest: yesterday's hours and any short days this week."""
    ts = timesheet(conn, settings, days=8)
    prev = next((d for d in ts["days"][1:] if d["workday"]), None)
    if not prev:
        return []
    lines = [f"{prev['weekday']}: {prev['logged_hours']:g}h logged across {len(prev['tickets'])} tickets"]
    if prev["unlogged_tickets"]:
        lines.append("Worked on but not logged: " + ", ".join(prev["unlogged_tickets"][:6]))
    lines.append(f"This week: {ts['week']['logged']:g}h of {ts['week']['target']:g}h")
    return lines


# --------------------------------------------------------------------------- export
def export_csv(conn, settings: Settings, days: int = 14, person: str | None = None, team: bool = False) -> str:
    """Worklogs as CSV (opens in Excel): one row per person, day, and ticket."""
    import csv
    import io
    today = datetime.now().astimezone().date()
    first = today - timedelta(days=max(int(days), 1) - 1)
    since = datetime.combine(first, datetime.min.time()).astimezone().astimezone(timezone.utc).isoformat()
    me_name, me_id = get_state(conn, "jira_me") or "", get_state(conn, "jira_me_id") or ""
    q = "SELECT key, author, author_id, started, seconds, comment FROM worklogs WHERE started >= ?"
    args: list = [since]
    if not team:
        if person and person != me_name:
            q += " AND author=?"; args.append(person)
        elif me_id:
            q += " AND author_id=?"; args.append(me_id)
        else:
            q += " AND author=?"; args.append(me_name)
    grid: dict = {}
    for w in conn.execute(q, args):
        k = (_local_day(w["started"]), w["author"] or "", w["key"])
        g = grid.setdefault(k, {"seconds": 0, "comments": []})
        g["seconds"] += w["seconds"] or 0
        if w["comment"]:
            g["comments"].append(w["comment"])
    def cell(v):            # keep Excel from running text as a formula
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(["Date", "Person", "Ticket", "Summary", "Project", "Type", "Hours", "Comments"])
    total = 0.0
    for (day, who, key) in sorted(grid):
        t = _ticket(conn, key)
        hours = round(grid[(day, who, key)]["seconds"] / 3600, 2)
        total += hours
        out.writerow([day, cell(who), cell(key), cell(t.get("summary")), cell(t.get("project") or key.split("-")[0]),
                      cell(t.get("issuetype")), f"{hours:g}", cell(" | ".join(grid[(day, who, key)]["comments"])[:500])])
    out.writerow(["Total", "", "", "", "", "", f"{round(total, 2):g}", ""])
    return buf.getvalue()


# --------------------------------------------------------------------------- suggested worklogs
def _quarter(h: float) -> float:
    return max(0.25, round(h * 4) / 4)


def suggestions(conn, settings: Settings, days: int = 7) -> list[dict]:
    """Worklogs you probably forgot: Claude-tracked time not yet logged, and tickets you worked on
    (status changes, PRs, reviews, comments) on days that are short of your target."""
    sheet = timesheet(conn, settings, days)
    today = datetime.now().astimezone().date().isoformat()
    out = []
    for day in sheet["days"]:
        if not day["workday"] and not day["tickets"]:
            continue
        shortfall = max(day["target_hours"] - day["logged_hours"], 0)
        candidates = []
        for t in day["tickets"]:
            if get_state(conn, f"wl_dismiss:{day['date']}:{t['key']}"):
                continue
            gap = t["tracked_hours"] - t["logged_hours"]
            if gap >= 0.25:
                out.append({"date": day["date"], "key": t["key"], "summary": t["summary"], "url": t["url"],
                            "hours": _quarter(gap), "reason": f"{t['tracked_hours']:.1f}h tracked in Claude",
                            "comment": "; ".join(t["activity"])[:200]})
                shortfall = max(shortfall - gap, 0)
            elif not t["logged_hours"] and t["activity"]:
                candidates.append(t)
        if candidates and shortfall >= 0.25 and day["date"] <= today:
            weight = sum(len(t["activity"]) for t in candidates)
            for t in candidates:
                h = min(shortfall * len(t["activity"]) / weight, 4)
                out.append({"date": day["date"], "key": t["key"], "summary": t["summary"], "url": t["url"],
                            "hours": _quarter(h), "reason": "you worked on it: " + ", ".join(t["activity"][:3]),
                            "comment": "; ".join(t["activity"])[:200]})
    return out


def dismiss_suggestion(conn, day: str, key: str) -> dict:
    from .db import set_state
    set_state(conn, f"wl_dismiss:{day}:{key.upper()}", now_iso())
    conn.commit()
    return {"ok": True}
