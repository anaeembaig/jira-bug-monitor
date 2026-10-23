"""Daily workflow: standup notes, start-work briefs, watch list, snooze, quick actions, and search."""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from . import service

from .analytics import parse_ts
from .config import Settings
from .db import get_state, now_iso


def _short(repo: str, number) -> str:
    return f"{repo.split('/')[-1]}#{number}"


def _prev_workday(settings: Settings, d: date) -> date:
    from .timelog import is_workday
    p = d - timedelta(days=1)
    for _ in range(7):
        if is_workday(settings, p):
            return p
        p -= timedelta(days=1)
    return d - timedelta(days=1)


# =========================================================================== standup
def standup(conn, settings: Settings, today: date | None = None) -> dict:
    """Your standup: what you did since the last workday, what's next, and what's blocking you."""
    from .timelog import timesheet
    today = today or datetime.now().astimezone().date()
    prev = _prev_workday(settings, today)
    sheet = timesheet(conn, settings, days=(datetime.now().astimezone().date() - prev).days + 1)
    done_lines, seen = [], set()
    for day in sheet["days"]:
        if not (prev.isoformat() <= day["date"] < today.isoformat()):
            continue
        for t in day["tickets"]:
            bits = []
            if t["logged_hours"]:
                bits.append(f"{t['logged_hours']:g}h logged")
            elif t["tracked_hours"]:
                bits.append(f"{t['tracked_hours']:.1f}h tracked")
            bits += [a for a in t["activity"] if a not in bits][:4]
            bits += [c for c in t.get("comments") or []][:1]
            if t["key"] in seen:
                continue
            seen.add(t["key"])
            done_lines.append({"key": t["key"], "summary": t["summary"], "url": t["url"],
                               "status": t["status"], "detail": "; ".join(bits)})
    start_utc = datetime.combine(prev, datetime.min.time()).astimezone().astimezone(timezone.utc)
    end_utc = datetime.combine(today, datetime.min.time()).astimezone().astimezone(timezone.utc)
    notes = [r for r in conn.execute("SELECT key, note, ts FROM work_log WHERE action IN ('note','done') AND note IS NOT NULL")
             if start_utc <= (parse_ts(r["ts"]) or start_utc - timedelta(days=1)) < end_utc]
    for r in notes:
        line = next((x for x in done_lines if x["key"] == r["key"]), None)
        if line and r["note"] not in line["detail"]:
            line["detail"] = f"{line['detail']}; {r['note']}".strip("; ")

    mine = service.my_tickets(conn)
    open_t = [t for g in mine["groups"] if g["category"] != "done" for t in g["tickets"]]
    in_progress = sorted((t for t in open_t if t["status_category"] == "indeterminate"
                          and "block" not in (t["status"] or "").lower()),
                         key=lambda t: -(t["urgency"] or 0))
    next_up = sorted((t for t in open_t if t["status_category"] == "new"), key=lambda t: -(t["urgency"] or 0))[:3]
    today_lines = [{"key": t["key"], "summary": t["summary"], "url": t["url"], "detail": t["status"]}
                   for t in in_progress + next_up]
    prs = service.list_prs(conn) if settings.github.enabled else []
    reviews = [p for p in prs if "review_requested" in p["relations"] and p["state"] == "OPEN"]
    for p in reviews[:5]:
        today_lines.append({"key": _short(p["repo"], p["number"]), "summary": p["title"], "url": p["url"],
                            "detail": f"review for {p['author']}"})

    blockers = []
    latest = {r["key"]: r["action"] for r in conn.execute(
        "SELECT w.key, w.action FROM work_log w JOIN (SELECT key, MAX(id) mid FROM work_log GROUP BY key) m "
        "ON w.id = m.mid")}
    for t in open_t:
        if "block" in (t["status"] or "").lower() or latest.get(t["key"]) == "blocked":
            blockers.append({"key": t["key"], "summary": t["summary"], "url": t["url"], "detail": t["status"]})
    for p in prs:
        if "authored" in p["relations"] and p["state"] == "OPEN" and p["status"] in (
                "Changes requested", "Merge conflicts", "Checks failing"):
            blockers.append({"key": _short(p["repo"], p["number"]), "summary": p["title"], "url": p["url"],
                             "detail": p["status"]})
    from .team import stuck
    for s in stuck(conn, settings, only_mine=True)["items"][:5]:
        if not any(b["key"] == s["key"] for b in blockers):
            blockers.append({"key": s["key"], "summary": s.get("title", ""), "url": s.get("url"),
                             "detail": f"{s['status']} for {s['days']} days"})
    out = {"date": today.isoformat(), "since": prev.isoformat(), "person": get_state(conn, "jira_me") or "",
           "yesterday": done_lines, "today": today_lines, "blockers": blockers}
    out["text"] = standup_text(out)
    return out


def _line(x: dict) -> str:
    detail = f" ({x['detail']})" if x.get("detail") else ""
    return f"{x['key']} {x.get('summary') or ''}{detail}".strip()


def standup_text(s: dict) -> str:
    label = "Yesterday" if (date.fromisoformat(s["date"]) - date.fromisoformat(s["since"])).days == 1 else \
        f"Since {date.fromisoformat(s['since']).strftime('%A')}"
    parts = [label + ":"] + [f"- {_line(x)}" for x in s["yesterday"]] + (["- Nothing logged"] if not s["yesterday"] else [])
    parts += ["", "Today:"] + [f"- {_line(x)}" for x in s["today"]] + (["- Nothing planned yet"] if not s["today"] else [])
    parts += ["", "Blockers:"] + ([f"- {_line(x)}" for x in s["blockers"]] or ["- None"])
    return "\n".join(parts)


def team_standup(conn, settings: Settings, today: date | None = None) -> dict:
    """Per teammate: what changed since the last workday, what's in progress, and what's stuck."""
    today = today or datetime.now().astimezone().date()
    prev = _prev_workday(settings, today)
    since = datetime.combine(prev, datetime.min.time()).astimezone().astimezone(timezone.utc)
    until = datetime.combine(today, datetime.min.time()).astimezone().astimezone(timezone.utc)
    inside = lambda ts: bool(ts) and since <= (parse_ts(ts) or since - timedelta(days=1)) < until
    from .team import stuck
    stuck_items = stuck(conn, settings)["items"]
    people = []
    for m in settings.team.members:
        name, login = m.get("name") or "", (m.get("github") or "").lower()
        if not name:
            continue
        did: dict = {}
        for e in conn.execute("SELECT key, ts, to_val FROM events WHERE author=? AND kind='status'", (name,)):
            if inside(e["ts"]):
                did.setdefault(e["key"], []).append(f"moved to {e['to_val']}")
        hours: dict = {}
        for w in conn.execute("SELECT key, started, seconds FROM worklogs WHERE author=?", (name,)):
            if inside(w["started"]):
                hours[w["key"]] = hours.get(w["key"], 0) + (w["seconds"] or 0) / 3600
        for k, h in hours.items():
            did.setdefault(k, []).append(f"{round(h, 2):g}h logged")
        if login:
            for p in conn.execute("SELECT repo, number, created, merged_at FROM github_prs WHERE lower(author)=?", (login,)):
                if inside(p["merged_at"]):
                    did.setdefault(_short(p["repo"], p["number"]), []).append("merged")
                elif inside(p["created"]):
                    did.setdefault(_short(p["repo"], p["number"]), []).append("opened")
        wip = [dict(r) for r in conn.execute(
            "SELECT key, summary, status, url FROM issues WHERE in_team=1 AND assignee=? AND status_category='indeterminate' "
            "ORDER BY urgency DESC", (name,))]
        people.append({"person": name, "yesterday": [{"key": k, "detail": ", ".join(v)} for k, v in did.items()],
                       "in_progress": wip,
                       "stuck": [{"key": s["key"], "detail": f"{s['status']} {s['days']} days"}
                                 for s in stuck_items if s.get("owner") == name]})
    return {"date": today.isoformat(), "since": prev.isoformat(), "people": people}


def standup_card(conn, settings: Settings, team: bool = False) -> dict:
    from .jobs import _card, _link, _section
    if team:
        t = team_standup(conn, settings)
        sections = []
        for p in t["people"]:
            lines = [f"Done: {x['key']} {x['detail']}" for x in p["yesterday"][:4]]
            lines += [_link(x["key"], f"in {x['status']}", x["url"]) for x in p["in_progress"][:3]]
            lines += [f"Stuck: {x['key']} ({x['detail']})" for x in p["stuck"][:2]]
            sections += _section(p["person"], lines or ["No updates"])
        return _card(f"{settings.team.name or 'Team'} standup ({datetime.now().strftime('%a %b %d')})", sections, settings)
    s = standup(conn, settings)
    fmt = lambda xs: [_link(x["key"], f"{x.get('summary') or ''}" + (f" ({x['detail']})" if x.get("detail") else ""),
                            x.get("url")) for x in xs]
    sections = (_section("Since last workday", fmt(s["yesterday"]) or ["Nothing logged"])
                + _section("Today", fmt(s["today"]) or ["Nothing planned yet"])
                + _section("Blockers", fmt(s["blockers"]) or ["None"]))
    return _card(f"Your standup ({datetime.now().strftime('%a %b %d')})", sections, settings)


# =========================================================================== start work
def _slug(text: str, n: int = 40) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > n:
            break
        out = f"{out}-{w}" if out else w
    return out


def branch_name(conn, key: str, summary: str) -> str:
    """Follow the naming your existing branches use, e.g. feature/HUM-12-short-title."""
    prefixes, lower = Counter(), 0
    for (name,) in conn.execute("SELECT name FROM github_branches"):
        m = re.search(r"([A-Za-z][A-Za-z0-9]+-\d+)", name)
        if m:
            prefixes[name[:m.start()]] += 1
            lower += m.group(1).islower()
    total = sum(prefixes.values())
    prefix = prefixes.most_common(1)[0][0] if prefixes else ""
    k = key.lower() if total and lower > total / 2 else key.upper()
    slug = _slug(summary)
    return f"{prefix}{k}-{slug}" if slug else f"{prefix}{k}"


def start_work(conn, settings: Settings, key: str, actor: str = "", session: str = "", start_timer: bool = False) -> dict:
    """Everything needed to start on a ticket in one place."""
    from .quality import quality_check, regression_suspects, similar_tickets, sla_status
    t = service.get_ticket(conn, key)
    if t.get("error"):
        return {"error": t["error"] + " Add it to your watch list (watch_ticket) and sync to load it."}
    # Reuse your existing branch for this ticket; otherwise suggest one.
    branch = t["branches"][0]["name"] if t["branches"] else branch_name(conn, t["key"], t["summary"])
    base = "main"
    row = conn.execute("SELECT base FROM github_prs WHERE jira_keys LIKE ? AND base IS NOT NULL LIMIT 1",
                       (f'%"{t["project"]}-%',)).fetchone()
    if row:
        base = row["base"]
    brief = {
        "key": t["key"], "summary": t["summary"], "type": t.get("issuetype"), "priority": t["priority"],
        "status": t["status"], "assignee": t["assignee"], "reporter": t["reporter"], "url": t["url"],
        "description": (t.get("description") or "")[:1500], "environment": t.get("environment"),
        "components": t.get("components"), "fix_versions": t.get("fix_versions"),
        "quality": quality_check(t, settings),
        "recent_comments": t["comments"][:5],
        "prs": t["prs"], "branches": t["branches"], "deployments": t["deployments"][:5],
        "estimate": t["estimate"].get("summary"),
        "similar_resolved": similar_tickets(conn, t["key"], limit=5),
        "sla": sla_status(conn, t, settings) if settings.sla.enabled else None,
        "branch": branch,
        "git": ["git fetch origin", f"git switch -c {branch} origin/{base}"] if not t["branches"] else
               ["git fetch origin", f"git switch {branch}"],
    }
    if (t.get("issuetype") or "").lower() == "bug":
        brief["regression_suspects"] = regression_suspects(conn, settings, t["key"])[:5]
    if start_timer:
        brief["timer"] = service.log_work(conn, t["key"], "start", "started with start_work", actor=actor, session=session)
    return brief


# =========================================================================== watch list and snooze
def watch(conn, key: str, note: str = "") -> dict:
    key = key.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]+-\d+", key):
        return {"error": f"{key} isn't a Jira key like HUM-123."}
    conn.execute("INSERT OR REPLACE INTO watches(key, added, note) VALUES(?,?,?)", (key, now_iso(), note or None))
    conn.execute("UPDATE issues SET in_watch=1 WHERE key=?", (key,))
    conn.commit()
    return {"ok": True, "key": key, "note": "You'll get alerts for status, assignee, priority, and comments."}


def unwatch(conn, key: str) -> dict:
    key = key.strip().upper()
    n = conn.execute("DELETE FROM watches WHERE key=?", (key,)).rowcount
    conn.execute("UPDATE issues SET in_watch=0 WHERE key=?", (key,))
    conn.commit()
    return {"ok": bool(n), "key": key}


def watches(conn) -> list[dict]:
    out = []
    for w in conn.execute("SELECT * FROM watches ORDER BY added DESC"):
        r = conn.execute("SELECT key, summary, status, status_category, priority, assignee, updated, url "
                         "FROM issues WHERE key=?", (w["key"],)).fetchone()
        err = get_state(conn, f"watch_error:{w['key']}")
        out.append(dict(dict(r) if r else {"key": w["key"], "summary": (
                            f"Can't load it: not found, or you don't have access ({err[:80]})" if err else "Loads on the next sync")},
                        added=w["added"], note=w["note"], error=err or None))
    return out


def snooze(conn, key: str, hours: float = 24, reason: str = "") -> dict:
    key = key.strip().upper()
    hours = max(0.25, min(float(hours), 24 * 30))
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat()
    conn.execute("INSERT OR REPLACE INTO snoozes(key, until, reason) VALUES(?,?,?)", (key, until, reason or None))
    conn.commit()
    return {"ok": True, "key": key, "until": until,
            "note": "Alerts for this ticket stay on the dashboard only until then. Team channels aren't affected."}


def unsnooze(conn, key: str) -> dict:
    n = conn.execute("DELETE FROM snoozes WHERE key=?", (key.strip().upper(),)).rowcount
    conn.commit()
    return {"ok": bool(n)}


def snoozes(conn) -> list[dict]:
    conn.execute("DELETE FROM snoozes WHERE until <= ?", (now_iso(),))
    conn.commit()
    return [dict(r) for r in conn.execute("SELECT * FROM snoozes ORDER BY until")]


# =========================================================================== quick actions (opt-in)
def _audit(conn, action: str, detail: str) -> None:
    conn.execute("INSERT INTO audit(ts, actor, action, detail) VALUES(?,?,?,?)", (now_iso(), "dashboard/claude", action, detail))
    conn.commit()


def ticket_action(conn, settings: Settings, client, key: str, action: str, value: str = "") -> dict:
    """action: transitions | transition (value = transition id or name) | assign_me | unassign | comment (value = text)."""
    key = key.strip().upper()
    if action == "transitions":
        return {"key": key, "transitions": [{"id": t["id"], "name": t["name"],
                                             "to": (t.get("to") or {}).get("name")} for t in client.transitions(key)],
                "allowed": settings.jira.allow_actions}
    if not settings.jira.allow_actions:
        return {"error": "Ticket actions are off. Set allow_actions = true under [jira] in config.toml."}
    if action == "transition":
        options = client.transitions(key)
        t = next((t for t in options if str(t["id"]) == str(value) or t["name"].lower() == value.lower()
                  or ((t.get("to") or {}).get("name") or "").lower() == value.lower()), None)
        if not t:
            return {"error": f"{key} can't move to '{value}'. Options: " + ", ".join(t["name"] for t in options)}
        client.transition(key, t["id"])
        _audit(conn, "jira_transition", f"{key} → {(t.get('to') or {}).get('name') or t['name']}")
        return {"ok": True, "key": key, "status": (t.get("to") or {}).get("name") or t["name"]}
    if action in ("assign_me", "unassign"):
        me = get_state(conn, "jira_me_id") if action == "assign_me" else None
        if action == "assign_me" and not me:
            return {"error": "Your Jira account isn't known yet. Run a sync first."}
        client.assign(key, me)
        _audit(conn, "jira_assign", f"{key} → {'you' if me else 'unassigned'}")
        return {"ok": True, "key": key}
    if action == "comment":
        text = (value or "").strip()
        if not text or len(text) > 5000:
            return {"error": "A comment needs 1 to 5,000 characters."}
        client.add_comment(key, text)
        _audit(conn, "jira_comment", f"{key}: {text[:80]}")
        return {"ok": True, "key": key}
    return {"error": f"Unknown action {action}."}


# =========================================================================== search
def search(conn, q: str, limit: int = 8) -> dict:
    """Search tickets, PRs, branches, deployments, worklogs, work notes, and security alerts."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"query": q, "groups": []}
    like = f"%{q}%"
    groups = []

    def add(kind, items):
        if items:
            groups.append({"type": kind, "items": items[:limit]})

    key_q = q.upper()
    rows = conn.execute(
        "SELECT key, summary, status, priority, assignee, url FROM issues WHERE key = ? OR key LIKE ? OR summary LIKE ? "
        "OR description LIKE ? OR assignee LIKE ? ORDER BY key = ? DESC, status_category = 'done', urgency DESC LIMIT ?",
        (key_q, f"{key_q}%", like, like, like, key_q, limit)).fetchall()
    add("Tickets", [{"key": r["key"], "title": f"{r['key']} {r['summary']}", "url": r["url"],
                     "subtitle": " · ".join(x for x in (r["status"], r["priority"], r["assignee"] or "Unassigned") if x)}
                    for r in rows])
    m = re.fullmatch(r"([\w.-]+)?#(\d+)", q)
    prs = conn.execute(
        "SELECT repo, number, title, url, status, author FROM github_prs WHERE title LIKE ? OR head LIKE ? OR repo LIKE ? "
        "OR (? AND number = ?) ORDER BY updated DESC LIMIT ?",
        (like, like, like, bool(m), int(m.group(2)) if m else 0, limit)).fetchall()
    add("Pull requests", [{"repo": p["repo"], "number": p["number"], "title": f"{_short(p['repo'], p['number'])} {p['title']}",
                           "url": p["url"], "subtitle": f"{p['status']} · {p['author']}"} for p in prs])
    add("Branches", [{"title": b["name"], "url": b["url"], "subtitle": b["repo"]} for b in conn.execute(
        "SELECT repo, name, url FROM github_branches WHERE name LIKE ? LIMIT ?", (like, limit))])
    add("Deployments", [{"title": f"{d['repo'].split('/')[-1]} → {d['environment']}", "url": d["log_url"] or d["url"],
                         "subtitle": f"{(d['sha'] or '')[:7]} · {d['state']} · {d['creator'] or ''} · {(d['created'] or '')[:10]}"}
                        for d in conn.execute(
        "SELECT repo, environment, sha, state, creator, created, url, log_url FROM deployments WHERE kind='deployment' AND "
        "(sha LIKE ? OR environment LIKE ? OR tickets LIKE ? OR creator LIKE ?) ORDER BY created DESC LIMIT ?",
        (f"{q}%", like, like, like, limit))])
    add("Time log", [{"key": w["key"], "title": f"{w['key']} {w['comment']}", "url": None,
                      "subtitle": f"{w['author']} · {w['seconds'] / 3600:g}h · {(w['started'] or '')[:10]}"}
                     for w in conn.execute("SELECT key, author, started, seconds, comment FROM worklogs WHERE comment LIKE ? "
                                           "ORDER BY started DESC LIMIT ?", (like, limit))])
    add("Work notes", [{"key": w["key"], "title": f"{w['key']} {w['note']}", "url": None,
                        "subtitle": f"{w['action']} · {(w['ts'] or '')[:10]}"}
                       for w in conn.execute("SELECT key, action, note, ts FROM work_log WHERE note LIKE ? ORDER BY ts DESC LIMIT ?",
                                             (like, limit))])
    add("Security", [{"title": f"{a['package'] or a['kind']}: {a['summary']}", "url": a["url"],
                      "subtitle": f"{a['severity']} · {a['repo']}"} for a in conn.execute(
        "SELECT * FROM security_alerts WHERE summary LIKE ? OR package LIKE ? OR cve LIKE ? OR ghsa LIKE ? LIMIT ?",
        (like, like, like, like, limit))])
    return {"query": q, "groups": groups}
