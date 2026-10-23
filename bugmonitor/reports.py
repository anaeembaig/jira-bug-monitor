"""Weekly report: what got done, shipped, and logged, ready to paste or post to Teams."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .analytics import parse_ts
from .config import Settings
from .db import get_state


def weekly(conn, settings: Settings, person: str | None = None, days: int = 7) -> dict:
    """person=None: you. person="team": everyone on team tickets. Otherwise a teammate's name."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    me = get_state(conn, "jira_me") or ""
    who = me if person in (None, "", "me") else person
    team_mode = who == "team"
    rows = [dict(r) for r in conn.execute(
        "SELECT key, summary, issuetype, assignee, status_category, resolved, url, story_points FROM issues "
        "WHERE status_category='done' AND (in_team=1 OR in_mine=1 OR in_bugs=1)")]
    resolved = [r for r in rows if (parse_ts(r["resolved"]) or since) >= since and (team_mode or r["assignee"] == who)]
    prs = [dict(r) for r in conn.execute("SELECT repo, number, title, author, url, merged_at FROM github_prs "
                                         "WHERE state='MERGED' AND merged_at >= ?", (since.isoformat(),))]
    gh_me = (get_state(conn, "github_login") or "").lower()
    if not team_mode:
        member = next((m for m in settings.team.members if m.get("name") == who), None)
        login = (member or {}).get("github", "").lower() or (gh_me if who == me else "")
        prs = [p for p in prs if (p["author"] or "").lower() == login]
    deploys = [dict(r) for r in conn.execute(
        "SELECT environment, repo, state, created, creator, tickets FROM deployments WHERE kind='deployment' "
        "AND created >= ? AND state IN ('SUCCESS','ACTIVE','INACTIVE')", (since.isoformat(),))]
    hours = 0.0
    q = "SELECT SUM(seconds) FROM worklogs WHERE started >= ?"
    args: list = [since.isoformat()]
    if not team_mode:
        q += " AND author=?"
        args.append(who)
    hours = round((conn.execute(q, args).fetchone()[0] or 0) / 3600, 1)
    by_type: dict = {}
    for r in resolved:
        by_type[r["issuetype"] or "Other"] = by_type.get(r["issuetype"] or "Other", 0) + 1
    from .insights import cycle_time
    ct = cycle_time(conn, settings, days)
    title = f"{'Team' if team_mode else who} weekly report, {(since.astimezone()).strftime('%b %d')} to {datetime.now().strftime('%b %d')}"
    return {"title": title, "resolved": resolved, "by_type": by_type, "merged_prs": prs, "deploys": deploys,
            "hours_logged": hours, "points": round(sum(r["story_points"] or 0 for r in resolved), 1),
            "cycle_median_hours": ct["cycle_median_hours"]}


def as_text(rep: dict) -> str:
    lines = [rep["title"], ""]
    def plural(word: str, n: int) -> str:
        w = word.lower()
        if n == 1:
            return w
        return w[:-1] + "ies" if w.endswith("y") and w[-2:-1] not in "aeiou" else w + "s"
    types = ", ".join(f"{n} {plural(t, n)}" for t, n in sorted(rep["by_type"].items()))
    lines.append(f"Resolved {len(rep['resolved'])} tickets ({types or 'none'})"
                 + (f", {rep['points']:g} points" if rep["points"] else ""))
    lines += [f"- {r['key']} {r['summary']}" for r in rep["resolved"][:25]]
    lines += ["", f"Merged {len(rep['merged_prs'])} pull requests"]
    lines += [f"- {p['repo'].split('/')[-1]}#{p['number']} {p['title']}" for p in rep["merged_prs"][:20]]
    envs: dict = {}
    for d in rep["deploys"]:
        envs[d["environment"]] = envs.get(d["environment"], 0) + 1
    if envs:
        lines += ["", "Deploys: " + ", ".join(f"{k} {v}" for k, v in sorted(envs.items()))]
    lines += ["", f"Time logged in Jira: {rep['hours_logged']:g}h"]
    if rep["cycle_median_hours"] is not None:
        lines.append(f"Median hands-on time per ticket: {rep['cycle_median_hours']:g}h")
    return "\n".join(lines)


def weekly_card(conn, settings: Settings) -> dict:
    rep = weekly(conn, settings, "team" if settings.team.jql else None)
    text = as_text(rep).split("\n")
    d = settings.dashboard
    body = [{"type": "TextBlock", "text": text[0], "weight": "Bolder", "size": "Medium", "wrap": True}]
    body += [{"type": "TextBlock", "text": t or " ", "wrap": True, "spacing": "None"} for t in text[2:60]]
    return {"type": "AdaptiveCard", "version": "1.4", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": body, "actions": [{"type": "Action.OpenUrl", "title": "Open dashboard", "url": f"http://{d.host}:{d.port}"}]}
