"""Work that runs after every sync: held messages, escalations, stuck reminders, digests."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import service, team
from .alerts import Alerter, in_quiet_hours
from .analytics import parse_ts
from .config import Settings
from .db import get_state, row_to_dict, set_state

log = logging.getLogger("bugmonitor.jobs")
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def run_all(conn, settings: Settings, alerter: Alerter | None = None, now: datetime | None = None) -> dict:
    alerter = alerter or Alerter(settings, conn)
    out = {}
    for name, fn in (("outbox", lambda: alerter.flush_outbox()),
                     ("escalations", lambda: escalations(conn, settings, alerter)),
                     ("stuck", lambda: stuck_reminders(conn, settings, alerter)),
                     ("digests", lambda: digests(conn, settings, alerter, now)),
                     ("timelog", lambda: _timelog_reminder(conn, settings, alerter, now)),
                     ("housekeeping", lambda: housekeeping(conn, settings))):
        try:
            out[name] = fn()
        except Exception as e:  # one failing job never blocks the others
            log.warning("Job %s failed: %s", name, e)
            out[name] = f"error: {e}"
    return out


def _timelog_reminder(conn, settings, alerter, now):
    from .timelog import reminder
    return reminder(conn, settings, alerter, now)


def _once(conn, key: str) -> bool:
    """True the first time a key is seen; used to send each reminder only once."""
    if get_state(conn, key):
        return False
    set_state(conn, key, datetime.now(timezone.utc).isoformat())
    conn.commit()
    return True


# --------------------------------------------------------------------------- escalations
def escalations(conn, settings: Settings, alerter: Alerter) -> int:
    if not settings.escalations:
        return 0
    now = datetime.now(timezone.utc)
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM issues WHERE (in_bugs=1 OR in_team=1) AND status_category != 'done'")]
    fired = 0
    for rule in settings.escalations:
        for r in rows:
            if rule.projects and r["project"] not in [p.upper() for p in rule.projects]:
                continue
            if not settings.known_priority(r["priority"]) or not settings.at_least(r["priority"], rule.min_priority):
                continue
            age_h = (now - (parse_ts(r["created"]) or now)).total_seconds() / 3600
            reasons = []
            if rule.unassigned_hours and not r["assignee"] and age_h >= rule.unassigned_hours:
                reasons.append(("unassigned", f"unassigned for {age_h:.0f} hours"))
            if rule.open_hours and age_h >= rule.open_hours:
                reasons.append(("open", f"open for {age_h:.0f} hours"))
            for code, text in reasons:
                if _once(conn, f"esc:{rule.name}:{r['key']}:{code}"):
                    alerter.raise_alert(r, "escalation", f"{rule.name}: {text}", external=True,
                                        personal=not rule.route, route=rule.route or None, team=bool(rule.route))
                    fired += 1
    return fired


# --------------------------------------------------------------------------- stuck reminders
def stuck_reminders(conn, settings: Settings, alerter: Alerter) -> int:
    me = get_state(conn, "jira_me") or ""
    gh_me = (get_state(conn, "github_login") or "").lower()
    items = team.stuck(conn, settings)["items"] if settings.team.jql else []
    mine_keys = {i["key"] for i in team.stuck(conn, settings, only_mine=True)["items"]}
    if not settings.team.jql:
        items = team.stuck(conn, settings, only_mine=True)["items"]
    sent = 0
    for i in items:
        is_mine = i["key"] in mine_keys or (i["type"] == "pr" and i["owner"].lower() == gh_me) or i["owner"] == me
        if not _once(conn, f"stuck:{i['key']}:{i['status']}"):
            continue
        issue = {"key": i["key"], "summary": i["title"], "url": i["url"], "priority": i.get("priority"),
                 "status": i["status"], "assignee": i["owner"] if i["owner"] != team.UNASSIGNED else None,
                 "project": i["key"].split("-")[0] if i["type"] == "ticket" else ""}
        detail = f"{i['status']} for {i['days']} days (limit {i['limit_days']})"
        if i.get("waiting_on"):
            detail += f", waiting on {', '.join(i['waiting_on'])}"
        alerter.raise_alert(issue, "stuck", detail, external=True,
                            personal=is_mine and settings.alerts.stuck_reminders, team=i["type"] == "ticket")
        sent += 1
    return sent


# --------------------------------------------------------------------------- digests
def _due(settings: Settings, now: datetime) -> bool:
    t = settings.notify.digest_time
    if not t:
        return False
    h, m = (t + ":0").split(":")[:2]
    return (now.hour, now.minute) >= (int(h), int(m))


def digests(conn, settings: Settings, alerter: Alerter, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()       # local time
    today = now.date().isoformat()
    sent: list[str] = []
    if not _due(settings, now) or in_quiet_hours(settings.notify, now):
        return sent
    if settings.notify.digest_personal and settings.alerts.teams_webhook and get_state(conn, "digest_personal") != today:
        if alerter.post_card(settings.alerts.teams_webhook, personal_digest(conn, settings)):
            set_state(conn, "digest_personal", today)
            sent.append("personal")
    targets = [r for r in settings.routes if r.digest and r.webhook]
    # State per destination, so one failing channel doesn't make the others post twice.
    due_team = [r for r in targets if get_state(conn, f"digest_team:{r.name}") != today]
    if settings.notify.digest_team and settings.team.jql and due_team:
        card = team_digest(conn, settings)
        for r in due_team:
            if alerter.post_card(r.webhook, card):
                set_state(conn, f"digest_team:{r.name}", today)
                sent.append(f"team:{r.name}")
    day = settings.notify.weekly_report_day.lower()
    if day and DAYS[now.weekday()] == day:
        dests = [(f"route:{r.name}", r.webhook) for r in targets] or (
            [("private", settings.alerts.teams_webhook)] if settings.alerts.teams_webhook else [])
        dests = [(k, w) for k, w in dests if get_state(conn, f"weekly_report:{k}") != today]
        if dests:
            from .reports import weekly_card
            card = weekly_card(conn, settings)
            for k, w in dests:
                if alerter.post_card(w, card):
                    set_state(conn, f"weekly_report:{k}", today)
                    sent.append(f"weekly:{k}")
    conn.commit()
    return sent


def _section(title: str, lines: list[str]) -> list[dict]:
    out = [{"type": "TextBlock", "text": title, "weight": "Bolder", "spacing": "Medium", "wrap": True}]
    out += [{"type": "TextBlock", "text": f"- {l}", "wrap": True, "spacing": "None"} for l in lines] or []
    return out


def _card(heading: str, sections: list[dict], settings: Settings) -> dict:
    d = settings.dashboard
    return {"type": "AdaptiveCard", "version": "1.4",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": [{"type": "TextBlock", "text": heading, "weight": "Bolder", "size": "Medium", "wrap": True}] + sections,
            "actions": [{"type": "Action.OpenUrl", "title": "Open dashboard", "url": f"http://{d.host}:{d.port}"}]}


def _link(key: str, text: str, url: str | None) -> str:
    return f"[{key}]({url}) {text}" if url else f"{key} {text}"


def personal_digest(conn, settings: Settings) -> dict:
    m = service.my_tickets(conn)
    sections = []
    open_groups = [g for g in m["groups"] if g["category"] != "done"]
    if open_groups:
        sections += _section("Your tickets", [f"{g['status']}: {g['count']}" for g in open_groups])
    st = team.stuck(conn, settings, only_mine=True)["items"]
    if st:
        sections += _section("Stuck", [_link(i["key"], f"{i['status']} for {i['days']} days", i["url"]) for i in st[:5]])
    if settings.github.enabled:
        prs = service.list_prs(conn)
        review = [p for p in prs if "review_requested" in p["relations"] and p["state"] == "OPEN"]
        mine = [p for p in prs if "authored" in p["relations"] and p["state"] == "OPEN"]
        if review:
            sections += _section("Waiting on your review",
                                 [_link(f"{p['repo'].split('/')[-1]}#{p['number']}", p["title"], p["url"]) for p in review[:5]])
        attention = [p for p in mine if p["status"] in ("Checks failing", "Changes requested", "Merge conflicts")
                     or (p.get("unresolved_threads") or 0) > 0]
        if attention:
            sections += _section("Your PRs needing attention", [
                _link(f"{p['repo'].split('/')[-1]}#{p['number']}",
                      f"{p['status']}{f', {p['unresolved_threads']} unresolved' if p.get('unresolved_threads') else ''}", p["url"])
                for p in attention[:5]])
    new = service.list_new(conn, 24)
    sections += _section(f"New bugs in the last 24 hours: {len(new)}",
                         [_link(b["key"], f"{b['summary']} ({b['priority'] or 'no priority'})", b["url"]) for b in new[:5]])
    if settings.timelog.enabled and settings.timelog.recap_in_digest:
        from .timelog import recap_lines
        lines = recap_lines(conn, settings)
        if lines:
            sections += _section("Time log", lines)
    return _card(f"Good morning. Here's your day ({datetime.now().strftime('%a %b %d')})", sections, settings)


def team_digest(conn, settings: Settings) -> dict:
    b = team.board(conn, settings)
    sections = _section("Work in progress", [
        f"{r['person']}: {r['wip']} in progress, {r['open']} open" + (f", {r['stuck']} stuck" if r["stuck"] else "")
        for r in b["rows"] if r["open"]])
    for sp in team.sprint(conn, settings)["sprints"]:
        sections += _section(f"{sp['name']}: {sp['done_pct']}% done, {sp['days_left']} days left",
                             [f"{sp['by_category']['indeterminate']} {sp['unit']} in progress, "
                              f"{sp['by_category']['new']} to do, {len(sp['added_after_start'])} added after start"])
    q = team.reviews(conn, settings)["queue"]
    if q:
        sections += _section(f"Waiting on review: {len(q)}", [
            _link(f"{x['repo'].split('/')[-1]}#{x['number']}", f"{x['title']} ({x['waiting_hours']:.0f}h)", x["url"])
            for x in q[:5]])
    st = team.stuck(conn, settings)["items"]
    if st:
        sections += _section(f"Stuck: {len(st)}", [_link(i["key"], f"{i['owner']}, {i['status']} {i['days']} days", i["url"])
                                                   for i in st[:6]])
    soon = [r for r in team.releases(conn, settings)["releases"] if r["days_left"] is not None and r["days_left"] <= 7]
    if soon:
        sections += _section("Releases this week", [
            f"{r['project']} {r['name']}: {r['progress']}% done, {len(r['blockers'])} blocking bugs" for r in soon])
    new = service.list_new(conn, 24)
    sections += _section(f"New bugs in the last 24 hours: {len(new)}",
                         [_link(x["key"], f"{x['summary']} ({x['priority'] or 'no priority'})", x["url"]) for x in new[:5]])
    return _card(f"{settings.team.name or 'Team'} daily summary ({datetime.now().strftime('%a %b %d')})", sections, settings)


# --------------------------------------------------------------------------- housekeeping
def housekeeping(conn, settings: Settings, force: bool = False) -> dict | None:
    """Once a day: drop old alerts, audit entries, and work logs; tighten file permissions."""
    import os
    from datetime import timedelta
    today = datetime.now().date().isoformat()
    if not force and get_state(conn, "housekeeping") == today:
        return None
    st = settings.storage
    now = datetime.now(timezone.utc)
    cut = lambda d: (now - timedelta(days=d)).isoformat()
    out = {
        "alerts": conn.execute("DELETE FROM alerts WHERE ts < ? AND acknowledged=1", (cut(st.keep_alerts_days),)).rowcount,
        "audit": conn.execute("DELETE FROM audit WHERE ts < ?", (cut(st.keep_audit_days),)).rowcount,
        "work_log": conn.execute("DELETE FROM work_log WHERE ts < ?", (cut(st.keep_work_log_days),)).rowcount,
        "outbox": conn.execute("DELETE FROM outbox WHERE ts < ?", (cut(7),)).rowcount,
        "orphan_events": conn.execute("DELETE FROM events WHERE key NOT IN (SELECT key FROM issues)").rowcount,
        "orphan_comments": conn.execute("DELETE FROM jira_comments WHERE key NOT IN (SELECT key FROM issues)").rowcount,
    }
    set_state(conn, "housekeeping", today)
    conn.commit()
    if os.name == "posix":   # settings and data are private to you
        for p in [settings.config_file, settings.db_path, settings.db_path.with_name(settings.db_path.name + "-wal")]:
            try:
                if p and p.exists():
                    os.chmod(p, 0o600)
            except OSError:
                pass
        try:
            os.chmod(settings.db_path.parent, 0o700)
        except OSError:
            pass
    return out
