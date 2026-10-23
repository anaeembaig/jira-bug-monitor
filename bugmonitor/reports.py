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


# =========================================================================== monthly and sprint review
def _month_bounds(month: str | None) -> tuple[datetime, datetime, str]:
    now = datetime.now().astimezone()
    if month:
        y, m = (int(x) for x in month.split("-")[:2])
    else:
        y, m = now.year, now.month
    start = now.replace(year=y, month=m, day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = start.replace(year=y + (m == 12), month=1 if m == 12 else m + 1)
    end = min(nxt, now) if start <= now else nxt
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), start.strftime("%B %Y")


def _in(ts, start, end) -> bool:
    t = parse_ts(ts)
    return bool(t) and start <= t < end


def _period(conn, settings: Settings, start: datetime, end: datetime) -> dict:
    """Shared numbers for any window: bugs, SLA, delivery, quality, builds, tests, time."""
    import statistics
    from .deployments import dora
    from .quality import quality_check, sla_status
    from .testhealth import flaky_tests
    days = max((end - start).days, 1)
    bugs = [dict(r) for r in conn.execute(
        "SELECT key, summary, priority, status_category, created, resolved, assignee, reporter, labels, components, "
        "description, environment, affects_versions, issuetype, project, assignee_id, url FROM issues WHERE in_bugs=1")]
    created = [b for b in bugs if _in(b["created"], start, end)]
    resolved = [b for b in bugs if b["status_category"] == "done" and _in(b["resolved"], start, end)]
    fix_h = [(parse_ts(b["resolved"]) - parse_ts(b["created"])).total_seconds() / 3600 for b in resolved if parse_ts(b["created"])]
    by_pri: dict = {}
    for b in created:
        by_pri[b["priority"] or "None"] = by_pri.get(b["priority"] or "None", 0) + 1
    from .analytics import status_categories
    cats = status_categories(conn)
    reopened = sum(1 for e in conn.execute("SELECT ts, from_val, to_val FROM events WHERE kind='status'")
                   if cats.get(e["from_val"]) == "done" and cats.get(e["to_val"]) not in (None, "done") and _in(e["ts"], start, end))
    esc = {l.lower() for l in settings.team.escape_labels}
    import json as _json
    escaped = [b for b in created if esc & {l.lower() for l in _json.loads(b["labels"] or "[]")}]
    checked = []
    for b in created:
        row = dict(b, components=_json.loads(b["components"] or "[]"), affects_versions=_json.loads(b["affects_versions"] or "[]"))
        q = quality_check(row, settings)
        if q["applies"]:
            checked.append(not q["problems"])
    sla = None
    if settings.sla.enabled:
        resp = fix = resp_ok = fix_ok = 0
        for b in resolved:
            st = sla_status(conn, b, settings)
            if st and st["response"]:
                resp += 1; resp_ok += st["response"]["state"] == "met"
            if st and st["resolve"]:
                fix += 1; fix_ok += st["resolve"]["state"] == "met"
        sla = {"response_met_pct": round(resp_ok / resp * 100) if resp else None,
               "resolve_met_pct": round(fix_ok / fix * 100) if fix else None, "bugs": len(resolved)}
    scope = "in_team=1" if settings.team.jql else "in_mine=1"
    done = [dict(r) for r in conn.execute(f"SELECT key, summary, issuetype, assignee, story_points, resolved, url FROM issues "
                                          f"WHERE status_category='done' AND ({scope})") if _in(r["resolved"], start, end)]
    people: dict = {}
    for r in done:
        p = people.setdefault(r["assignee"] or "Unassigned", {"person": r["assignee"] or "Unassigned", "done": 0, "points": 0.0})
        p["done"] += 1
        p["points"] += r["story_points"] or 0
    prs = [dict(r) for r in conn.execute("SELECT repo, number, title, author, merged_at FROM github_prs WHERE state='MERGED'")
           if _in(r["merged_at"], start, end)]
    builds = [r["conclusion"] for r in conn.execute("SELECT conclusion, created FROM ci_runs WHERE status='completed'")
              if _in(r["created"], start, end) and r["conclusion"] in ("success", "failure", "timed_out")]
    builds += ["success" if r["result"] == "SUCCESS" else "failure" for r in conn.execute(
        "SELECT result, started FROM jenkins_builds WHERE building=0 AND is_deploy=0") if _in(r["started"], start, end)
        and r["result"] in ("SUCCESS", "FAILURE", "UNSTABLE")]
    flaky = flaky_tests(conn, settings, days=max(days, 1) + max((datetime.now(timezone.utc) - end).days, 0))
    me_id = get_state(conn, "jira_me_id") or ""
    hours_me = sum(r["seconds"] or 0 for r in conn.execute("SELECT started, seconds FROM worklogs WHERE author_id=?", (me_id,))
                   if _in(r["started"], start, end)) / 3600
    hours_all = sum(r["seconds"] or 0 for r in conn.execute("SELECT started, seconds FROM worklogs") if _in(r["started"], start, end)) / 3600
    oldest = conn.execute("SELECT MIN(created) FROM ci_runs").fetchone()[0]
    oldest_jk = conn.execute("SELECT MIN(started) FROM jenkins_builds").fetchone()[0]
    first_build = min([t for t in (parse_ts(oldest), parse_ts(oldest_jk)) if t], default=None)
    partial = bool(first_build and first_build > start + timedelta(days=1))
    return {
        "builds_partial_since": first_build.isoformat() if partial else None,
        "bugs": {"created": len(created), "resolved": len(resolved), "net": len(created) - len(resolved),
                 "open_now": sum(1 for b in bugs if b["status_category"] != "done"),
                 "median_fix_hours": round(statistics.median(fix_h), 1) if fix_h else None,
                 "reopened": reopened, "escaped": len(escaped),
                 "by_priority": sorted(({"priority": k, "n": v} for k, v in by_pri.items()),
                                       key=lambda x: -settings.priority_rank(x["priority"]))},
        "quality": {"checked": len(checked), "complete_pct": round(sum(checked) / len(checked) * 100) if checked else None},
        "sla": sla,
        "delivery": {"finished": len(done), "points": round(sum(r["story_points"] or 0 for r in done), 1),
                     "prs_merged": len(prs), "dora": dora(conn, settings, start=start, end=end)},
        "builds": {"runs": len(builds), "success_pct": round(builds.count("success") / len(builds) * 100) if builds else None,
                   "flaky_tests": len(flaky["flaky"]), "top_flaky": [t["test"] for t in flaky["flaky"][:5]]},
        "time": {"mine_hours": round(hours_me, 1), "all_hours": round(hours_all, 1)},
        "people": sorted(people.values(), key=lambda p: -p["done"]),
        "done": done,
    }


def monthly(conn, settings: Settings, month: str | None = None) -> dict:
    from .quality import hotspots
    start, end, label = _month_bounds(month)
    rep = _period(conn, settings, start, end)
    hot = hotspots(conn, settings, max((datetime.now(timezone.utc) - start).days, 1))
    rep.update({"kind": "monthly", "title": f"Monthly report: {label}", "start": start.isoformat(), "end": end.isoformat(),
                "month": start.astimezone().strftime("%Y-%m"),
                "hotspots": {"files": [f["path"] for f in hot["files"][:5]],
                             "components": [f"{c['component']} ({c['bugs']})" for c in hot["components"][:5]]}})
    rep["text"] = report_text(rep)
    return rep


def team_sprint_ids(conn) -> set:
    """Sprints that hold your or your team's tickets (bugs from other boards bring their sprints along too)."""
    import json as _json
    ids: set = set()
    for (v,) in conn.execute("SELECT sprint_ids FROM issues WHERE in_team=1 OR in_mine=1"):
        ids |= set(_json.loads(v or "[]"))
    return ids


def sprints_list(conn) -> list[dict]:
    mine = team_sprint_ids(conn)
    return [dict(r) for r in conn.execute(
        "SELECT id, name, state, start, end, complete FROM sprints WHERE state IN ('active','closed') "
        "ORDER BY COALESCE(complete, end, start) DESC") if r["id"] in mine][:20]


def sprint_review(conn, settings: Settings, sprint_id: int | None = None) -> dict:
    from .team import _added_at
    sp = None
    if sprint_id:
        sp = conn.execute("SELECT * FROM sprints WHERE id=?", (int(sprint_id),)).fetchone()
    if not sp:
        mine = team_sprint_ids(conn)
        rows = [r for r in conn.execute("SELECT * FROM sprints WHERE state IN ('closed','active') "
                                        "ORDER BY state='closed' DESC, COALESCE(complete, start) DESC") if r["id"] in mine]
        sp = rows[0] if rows else None
    if not sp:
        return {"error": "No sprints found yet. Sprints come from the Sprint field on your team's tickets."}
    sp = dict(sp)
    start = parse_ts(sp["start"]) or datetime.now(timezone.utc) - timedelta(days=14)
    end = parse_ts(sp["complete"]) or min(parse_ts(sp["end"]) or datetime.now(timezone.utc), datetime.now(timezone.utc))
    import json as _json
    items = [dict(r) for r in conn.execute(
        "SELECT key, summary, issuetype, status, status_category, assignee, story_points, created, resolved, sprint_ids, url "
        "FROM issues WHERE in_team=1 OR in_mine=1")]
    items = [r for r in items if sp["id"] in _json.loads(r["sprint_ids"] or "[]")]
    use_points = any(r["story_points"] for r in items)
    w = (lambda r: r["story_points"] or 0) if use_points else (lambda r: 1)
    late = {r["key"] for r in items if (_added_at(conn, r, sp) or start) > start + timedelta(hours=12)}
    done = [r for r in items if r["status_category"] == "done" and (parse_ts(r["resolved"]) or end) <= end + timedelta(hours=12)]
    not_done = [r for r in items if r not in done]
    committed = [r for r in items if r["key"] not in late]
    rep = _period(conn, settings, start, end)
    people: dict = {}
    for r in done:
        p = people.setdefault(r["assignee"] or "Unassigned", {"person": r["assignee"] or "Unassigned", "done": 0, "points": 0.0})
        p["done"] += 1
        p["points"] += r["story_points"] or 0
    unit = "points" if use_points else "tickets"
    rep.update({
        "kind": "sprint", "title": f"Sprint review: {sp['name']}", "sprint": sp, "unit": unit,
        "start": start.isoformat(), "end": end.isoformat(),
        "committed": round(sum(w(r) for r in committed), 1), "added": round(sum(w(r) for r in items if r["key"] in late), 1),
        "completed": round(sum(w(r) for r in done), 1),
        "completed_committed": round(sum(w(r) for r in done if r["key"] not in late), 1),
        "completion_pct": round(sum(w(r) for r in done) / sum(w(r) for r in items) * 100) if items and sum(w(r) for r in items) else None,
        "carried_over": [{k: r[k] for k in ("key", "summary", "status", "assignee", "url")} for r in not_done],
        "people": sorted(people.values(), key=lambda p: -p["done"]),
        "done": [{k: r[k] for k in ("key", "summary", "issuetype", "assignee", "url")} for r in done],
    })
    rep["text"] = report_text(rep)
    return rep


def report_text(rep: dict) -> str:
    b, d, q, bl, t = rep["bugs"], rep["delivery"], rep["quality"], rep["builds"], rep["time"]
    pct = lambda v: "—" if v is None else f"{v}%"
    lines = [rep["title"], ""]
    if rep["kind"] == "sprint":
        u = rep["unit"]
        lines += [f"Completed {rep['completed']:g} {u} of {rep['committed'] + rep['added']:g} ({pct(rep['completion_pct'])})",
                  f"- Committed at start: {rep['committed']:g} {u}, finished {rep['completed_committed']:g}",
                  f"- Added after start: {rep['added']:g} {u}",
                  f"- {'Not done yet' if rep['sprint'].get('state') == 'active' else 'Carried over'}: "
                  f"{len(rep['carried_over'])} ticket{'s' * (len(rep['carried_over']) != 1)}", ""]
    lines += ["Bugs",
              f"- {b['created']} reported, {b['resolved']} fixed (net {b['net']:+d}), {b['open_now']} open now",
              f"- Median time to fix: {'—' if b['median_fix_hours'] is None else str(b['median_fix_hours']) + 'h'}",
              f"- Reopened: {b['reopened']}, escaped to customers or production: {b['escaped']}",
              f"- Complete bug reports: {pct(q['complete_pct'])} of {q['checked']}"]
    if rep.get("sla"):
        lines.append(f"- SLA: first response on time {pct(rep['sla']['response_met_pct'])}, fixed on time "
                     f"{pct(rep['sla']['resolve_met_pct'])}")
    dr = d["dora"]
    lines += ["", "Delivery",
              f"- Finished {d['finished']} tickets" + (f", {d['points']:g} points" if d["points"] else "")
              + f", merged {d['prs_merged']} PRs",
              f"- Production deploys: {dr['deploys']}, change failure rate {pct(dr['change_failure_rate'])}"
              + (f", lead time {dr['lead_time_hours']}h" if dr["lead_time_hours"] is not None else "")
              + (f", time to restore {dr['time_to_restore_hours']}h" if dr["time_to_restore_hours"] is not None else ""),
              "", "Builds and tests" + (f" (since {parse_ts(rep['builds_partial_since']).astimezone().strftime('%b %d')}; "
                                        "build history is kept for 40 days)" if rep.get("builds_partial_since") else ""),
              f"- {bl['runs']} default-branch builds, {pct(bl['success_pct'])} passed",
              f"- Flaky tests: {bl['flaky_tests']}" + (f" (top: {', '.join(bl['top_flaky'][:3])})" if bl["top_flaky"] else "")]
    if rep.get("hotspots") and (rep["hotspots"]["files"] or rep["hotspots"]["components"]):
        lines += ["", "Where bugs come from"]
        if rep["hotspots"]["components"]:
            lines.append("- Components: " + ", ".join(rep["hotspots"]["components"]))
        if rep["hotspots"]["files"]:
            lines.append("- Files: " + ", ".join(rep["hotspots"]["files"]))
    if rep["people"]:
        lines += ["", "By person"] + [f"- {p['person']}: {p['done']} done" + (f", {p['points']:g} points" if p["points"] else "")
                                     for p in rep["people"][:12]]
    lines += ["", f"Time logged in Jira: {t['all_hours']:g}h (you: {t['mine_hours']:g}h)"]
    if rep["kind"] == "sprint" and rep["carried_over"]:
        lines += ["", "Not done yet" if rep["sprint"].get("state") == "active" else "Carried over"] + [f"- {r['key']} {r['summary']} ({r['status']})" for r in rep["carried_over"][:15]]
    if rep.get("done"):
        lines += ["", "Finished"] + [f"- {r['key']} {r['summary']}" for r in rep["done"][:20]]
        if len(rep["done"]) > 20:
            lines.append(f"- and {len(rep['done']) - 20} more")
    return "\n".join(lines)


def report_card(rep: dict, settings: Settings) -> dict:
    text = rep["text"].split("\n")
    d = settings.dashboard
    body = [{"type": "TextBlock", "text": text[0], "weight": "Bolder", "size": "Medium", "wrap": True}]
    for t in text[2:80]:
        bold = t and not t.startswith("-")
        body.append({"type": "TextBlock", "text": t or " ", "wrap": True, "spacing": "Medium" if bold else "None",
                     "weight": "Bolder" if bold else "Default"})
    return {"type": "AdaptiveCard", "version": "1.4", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": body, "actions": [{"type": "Action.OpenUrl", "title": "Open dashboard", "url": f"http://{d.host}:{d.port}"}]}
