"""Bug quality: report completeness, similar past tickets, regression suspects, SLA timers, and hotspots."""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

from .analytics import parse_ts, status_durations
from .config import Settings
from .db import get_state, row_to_dict, set_state
from .triage import words

STEPS_RE = re.compile(r"steps?\s+to\s+reproduce|repro(duce|duction)?\s*(steps)?\s*:|^\s*(\d+[.)]|[-*•])\s+\S", re.I | re.M)
EXPECTED_RE = re.compile(r"\bexpected\b|\bactual\b|\bshould\s+(be|have|show|display|return)\b", re.I)
ENV_RE = re.compile(r"\b(browser|chrome|safari|firefox|edge|ios|android|iphone|ipad|windows|macos|mobile app|"
                    r"version|build|release|env(ironment)?|staging|production|prod|qa|uat|sandbox|v\d+(\.\d+)+)\b", re.I)


# =========================================================================== report quality
def quality_check(t: dict, settings: Settings) -> dict:
    """What a bug report is missing, with a ready-to-send note for the reporter."""
    q = settings.quality
    desc = t.get("description") or ""
    n_words = len(re.findall(r"\w+", desc))
    problems = []
    if (t.get("issuetype") or "").lower() not in ("bug", ""):
        return {"score": None, "problems": [], "note": "", "applies": False}
    if n_words < 3:
        problems.append(("description", "No description"))
    elif n_words < q.min_description_words:
        problems.append(("description", f"Description is short ({n_words} words)"))
    if q.require_steps and n_words >= 3 and not STEPS_RE.search(desc):
        problems.append(("steps", "No steps to reproduce"))
    if q.require_expected and n_words >= 3 and not EXPECTED_RE.search(desc):
        problems.append(("expected", "No expected vs. actual result"))
    if q.require_environment and not (t.get("environment") or ENV_RE.search(desc)):
        problems.append(("environment", "No environment, browser, or version"))
    if q.require_version and not t.get("affects_versions"):
        problems.append(("version", "No affected version"))
    if not t.get("priority"):
        problems.append(("priority", "No priority"))
    if not t.get("components"):
        problems.append(("component", "No component"))
    asks = {"description": "a short description of what goes wrong",
            "steps": "the steps to reproduce it", "expected": "what you expected to happen and what happened instead",
            "environment": "where you saw it (browser or app version, and environment)",
            "version": "the version where it happens"}
    wanted = [asks[c] for c, _ in problems if c in asks]
    first = (t.get("reporter") or "").split(" ")[0]
    note = ""
    if wanted:
        note = (f"Hi {first}, thanks for reporting this. " if first else "Thanks for reporting this. ") + \
               "To help us fix it faster, could you add:\n" + "\n".join(f"- {w}" for w in wanted)
    return {"applies": True, "score": max(0, 100 - 20 * len(problems)),
            "problems": [{"code": c, "text": x} for c, x in problems], "note": note}


def quality_report(conn, settings: Settings) -> dict:
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT key, summary, issuetype, priority, status, reporter, assignee, components, description, environment, "
        "affects_versions, url, created FROM issues WHERE in_bugs=1 AND status_category != 'done'")]
    items, by_problem, by_reporter = [], Counter(), {}
    for r in rows:
        c = quality_check(r, settings)
        if not c["applies"]:
            continue
        rep = by_reporter.setdefault(r["reporter"] or "Unknown", {"reporter": r["reporter"] or "Unknown", "bugs": 0, "incomplete": 0})
        rep["bugs"] += 1
        if c["problems"]:
            rep["incomplete"] += 1
            by_problem.update(p["text"].split(" (")[0] for p in c["problems"])
            items.append({k: r[k] for k in ("key", "summary", "priority", "status", "reporter", "assignee", "url", "created")}
                         | {"score": c["score"], "problems": c["problems"], "note": c["note"]})
    items.sort(key=lambda i: (i["score"], -settings.priority_rank(i["priority"])))
    checked = sum(r["bugs"] for r in by_reporter.values())
    return {"checked": checked, "incomplete": len(items),
            "complete_pct": round((checked - len(items)) / checked * 100) if checked else None,
            "by_problem": [{"label": k, "n": v} for k, v in by_problem.most_common()],
            "by_reporter": sorted(by_reporter.values(), key=lambda r: -r["incomplete"])[:10],
            "items": items[:100]}


# =========================================================================== similar tickets
def _text_words(r) -> tuple[set, set]:
    desc = " ".join(re.findall(r"\w+", (r["description"] or "")[:1500]))
    return words(r["summary"]), words(desc)


def similar_tickets(conn, key: str, limit: int = 5, min_score: float = 0.12) -> list[dict]:
    """Finished tickets that read most like this one, with how long they took and the PRs that fixed them."""
    me = conn.execute("SELECT key, summary, description, components, project, issuetype FROM issues WHERE key=?",
                      (key.upper(),)).fetchone()
    if not me:
        return []
    sw, dw = _text_words(me)
    comps = set(json.loads(me["components"] or "[]"))
    scored = []
    for r in conn.execute("SELECT key, summary, description, components, project, issuetype, assignee, resolved, url "
                          "FROM issues WHERE status_category='done' AND key != ?", (me["key"],)):
        s2, d2 = _text_words(r)
        sim = (len(sw & s2) / len(sw | s2) if sw and s2 else 0) * 0.7 + \
              (len(dw & d2) / len(dw | d2) if dw and d2 else 0) * 0.3
        if comps & set(json.loads(r["components"] or "[]")):
            sim += 0.08
        if r["issuetype"] == me["issuetype"]:
            sim += 0.02
        if sim >= min_score:
            scored.append((sim, r))
    scored.sort(key=lambda x: -x[0])
    from .service import pr_links
    links = pr_links(conn)
    out = []
    for sim, r in scored[:limit]:
        d = status_durations(conn, r["key"])
        out.append({"key": r["key"], "summary": r["summary"], "url": r["url"], "assignee": r["assignee"],
                    "resolved": r["resolved"], "similarity": round(min(sim, 1), 2),
                    "active_hours": round(d["active_sec"] / 3600, 1) if d["active_sec"] else None,
                    "prs": [{"repo": p["repo"], "number": p["number"], "url": p["url"]} for p in links.get(r["key"], [])]})
    return out


def similar_estimate(conn, key: str) -> dict | None:
    sims = [s for s in similar_tickets(conn, key, limit=8) if s["active_hours"]]
    if len(sims) < 3:
        return None
    hours = [s["active_hours"] for s in sims]
    return {"tickets": [s["key"] for s in sims], "median_hours": round(statistics.median(hours), 1),
            "range_hours": [min(hours), max(hours)]}


# =========================================================================== regression suspects
def _path_words(paths: list[str]) -> set[str]:
    return {w for p in paths for w in re.split(r"[^a-z0-9]+", p.lower()) if len(w) > 2}


def regression_suspects(conn, settings: Settings, key: str, days: int = 14) -> list[dict]:
    """PRs merged or deployed shortly before this bug was reported that touch the same area."""
    from .deployments import is_production
    bug = row_to_dict(conn.execute("SELECT * FROM issues WHERE key=?", (key.upper(),)).fetchone())
    if not bug or not parse_ts(bug["created"]):
        return []
    t = parse_ts(bug["created"]).astimezone(timezone.utc)
    start = t - timedelta(days=days)
    bw = words(bug["summary"]) | words((bug.get("description") or "")[:800])
    comps = {c.lower() for c in bug.get("components") or []}
    shipped: dict = {}      # (repo, number) -> deploy info
    for d in conn.execute("SELECT * FROM deployments WHERE kind='deployment' AND state IN ('SUCCESS','ACTIVE','INACTIVE') "
                          "AND rollback=0"):
        d = row_to_dict(d)
        when = parse_ts(d["created"])
        if not when or not start <= when <= t:
            continue
        for n in d.get("prs") or []:
            prev = shipped.get((d["repo"], n))
            if not prev or is_production(settings, d["environment"]):
                shipped[(d["repo"], n)] = {"environment": d["environment"], "at": d["created"],
                                           "production": is_production(settings, d["environment"])}
    out = []
    for p in conn.execute("SELECT * FROM github_prs WHERE state='MERGED'"):
        p = row_to_dict(p)
        merged = parse_ts(p["merged_at"])
        dep = shipped.get((p["repo"], p["number"]))
        if not dep and not (merged and start <= merged <= t):
            continue
        if bug["key"] in (p.get("jira_keys") or []):
            continue
        files = [f["path"] if isinstance(f, dict) else f for f in ((p.get("details") or {}).get("files") or [])]
        ticket_text = " ".join(r["summary"] for r in conn.execute(
            f"SELECT summary FROM issues WHERE key IN ({','.join('?' * len(p.get('jira_keys') or []))})",
            p.get("jira_keys") or []).fetchall()) if p.get("jira_keys") else ""
        pw = words(p["title"]) | words(ticket_text) | _path_words(files)
        overlap = bw & pw
        reasons, score = [], 0.0
        if overlap:
            score += min(len(overlap) / max(len(bw), 1) * 1.5, 0.8)
            reasons.append("mentions " + ", ".join(sorted(overlap)[:4]))
        hit = sorted(c for c in comps if c in (p["title"] or "").lower() or any(c.replace(" ", "") in f.lower().replace("_", "")
                                                                          for f in files))
        if hit:
            score += 0.3
            reasons.append("touches " + ", ".join(hit))
        if bug["project"] and any(k.startswith(bug["project"] + "-") for k in p.get("jira_keys") or []):
            score += 0.05
        when = parse_ts(dep["at"]) if dep else merged
        if when:
            score += max(0, 0.2 * (1 - (t - when).total_seconds() / (days * 86400)))
            hours = (t - when).total_seconds() / 3600
            reasons.append(f"{'deployed to ' + dep['environment'] if dep else 'merged'} "
                           f"{hours / 24:.1f} days before the report" if hours >= 24 else
                           f"{'deployed to ' + dep['environment'] if dep else 'merged'} {hours:.0f}h before the report")
        if dep and dep["production"]:
            score += 0.1
        if score < 0.25 or not (overlap or hit):
            continue
        out.append({"repo": p["repo"], "number": p["number"], "title": p["title"], "url": p["url"], "author": p["author"],
                    "merged_at": p["merged_at"], "tickets": p.get("jira_keys") or [], "score": round(score, 2),
                    "reasons": reasons, "files": files[:8]})
    return sorted(out, key=lambda x: -x["score"])[:8]


# =========================================================================== SLA timers
def _target(table: dict, priority: str | None) -> float | None:
    n = (priority or "").lower()
    return next((float(v) for k, v in (table or {}).items() if k.lower() == n), None)


def first_response(conn, t: dict) -> datetime | None:
    """First sign someone picked it up: assigned, moved out of a to-do status, or a comment from someone else."""
    from .analytics import status_categories
    cats = status_categories(conn)
    times = []
    for e in conn.execute("SELECT ts, kind, from_val, to_val, author FROM events WHERE key=? AND kind IN ('assignee','status')",
                          (t["key"],)):
        if e["kind"] == "assignee" and e["to_val"]:
            times.append(parse_ts(e["ts"]))
        elif e["kind"] == "status" and cats.get(e["from_val"]) == "new":
            times.append(parse_ts(e["ts"]))
    r = conn.execute("SELECT MIN(created) FROM jira_comments WHERE key=? AND author != ?",
                     (t["key"], t.get("reporter") or "")).fetchone()[0]
    if r:
        times.append(parse_ts(r))
    times = [x for x in times if x]
    return min(times) if times else None


def sla_status(conn, t: dict, settings: Settings, now: datetime | None = None) -> dict | None:
    s = settings.sla
    resp_h, fix_h = _target(s.response_hours, t.get("priority")), _target(s.resolve_hours, t.get("priority"))
    created = parse_ts(t.get("created"))
    if not created or not (resp_h or fix_h):
        return None
    now = now or datetime.now(timezone.utc)
    resolved = parse_ts(t.get("resolved")) if t.get("status_category") == "done" else None

    def stage(target, met):
        if not target:
            return None
        due = created + timedelta(hours=target)
        end = met or now
        used = (end - created).total_seconds() / 3600
        if met:
            state = "met" if met <= due else "missed"
        else:
            state = "breached" if now > due else "warn" if used >= target * s.warn_at else "ok"
        return {"target_hours": target, "due": due.isoformat(), "used_hours": round(used, 1),
                "left_hours": None if met else round((due - now).total_seconds() / 3600, 1),
                "met_at": met.isoformat() if met else None, "state": state}

    resp_met = first_response(conn, t)
    if resolved and (not resp_met or resolved < resp_met):
        resp_met = resolved
    out = {"priority": t.get("priority"), "response": stage(resp_h, resp_met), "resolve": stage(fix_h, resolved)}
    rank = {"breached": 4, "missed": 3, "warn": 2, "ok": 1, "met": 0}
    states = [x["state"] for x in (out["response"], out["resolve"]) if x]
    out["state"] = max(states, key=lambda x: rank[x]) if states else None
    return out


def _sla_rows(conn, settings: Settings, where: str) -> list[dict]:
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT key, summary, priority, status, status_category, assignee, assignee_id, reporter, project, created, "
        f"resolved, url FROM issues WHERE in_bugs=1 AND {where}")]
    projects = {p.upper() for p in settings.sla.projects}
    return [r for r in rows if not projects or (r["project"] or "").upper() in projects]


def sla_overview(conn, settings: Settings, days: int = 30) -> dict:
    now = datetime.now(timezone.utc)
    items = []
    for r in _sla_rows(conn, settings, "status_category != 'done'"):
        st = sla_status(conn, r, settings, now)
        if st:
            items.append(dict({k: r[k] for k in ("key", "summary", "priority", "status", "assignee", "url")}, sla=st))
    order = {"breached": 0, "warn": 1, "ok": 2}
    items.sort(key=lambda i: (order.get(i["sla"]["state"], 3),
                              min((x["left_hours"] for x in (i["sla"]["response"], i["sla"]["resolve"])
                                   if x and x["left_hours"] is not None), default=1e9)))
    since = now - timedelta(days=days)
    resp_n = resp_ok = fix_n = fix_ok = 0
    for r in _sla_rows(conn, settings, "status_category = 'done'"):
        if (parse_ts(r["resolved"]) or since) < since:
            continue
        st = sla_status(conn, r, settings, now)
        if not st:
            continue
        if st["response"]:
            resp_n += 1; resp_ok += st["response"]["state"] == "met"
        if st["resolve"]:
            fix_n += 1; fix_ok += st["resolve"]["state"] == "met"
    return {"enabled": settings.sla.enabled, "days": days,
            "breached": sum(1 for i in items if i["sla"]["state"] == "breached"),
            "warn": sum(1 for i in items if i["sla"]["state"] == "warn"),
            "response_met_pct": round(resp_ok / resp_n * 100) if resp_n else None,
            "resolve_met_pct": round(fix_ok / fix_n * 100) if fix_n else None,
            "targets": [{"priority": p, "response_hours": _target(settings.sla.response_hours, p),
                         "resolve_hours": _target(settings.sla.resolve_hours, p)}
                        for p in settings.priority_order if _target(settings.sla.resolve_hours, p)],
            "items": items[:200]}


def sla_alerts(conn, settings: Settings, alerter) -> int:
    """Warn your own tickets before they breach; tell you (and the SLA channel) when any breaches."""
    if not settings.sla.enabled:
        return 0
    me_id, me = get_state(conn, "jira_me_id") or "", get_state(conn, "jira_me") or ""
    first = get_state(conn, "sla_initialized") is None
    now = datetime.now(timezone.utc)
    sent = 0
    for r in _sla_rows(conn, settings, "status_category != 'done'"):
        st = sla_status(conn, r, settings, now)
        if not st:
            continue
        mine = (me_id and r["assignee_id"] == me_id) or (me and r["assignee"] == me)
        for stage_name in ("response", "resolve"):
            x = st[stage_name]
            if not x or x["state"] not in ("warn", "breached"):
                continue
            k = f"sla:{r['key']}:{stage_name}:{x['state']}"
            if get_state(conn, k):
                continue
            set_state(conn, k, now.isoformat())
            if first:
                continue
            label = "first response" if stage_name == "response" else "fix"
            if x["state"] == "warn" and mine:
                alerter.raise_alert(r, "sla_warning", f"{label} due in {x['left_hours']:.1f}h ({x['target_hours']:g}h target)",
                                    external=True, team=False)
                sent += 1
            elif x["state"] == "breached":
                alerter.raise_alert(r, "sla_breach", f"{label} target of {x['target_hours']:g}h passed",
                                    external=True, personal=bool(mine), route=settings.sla.route or None,
                                    team=True)
                sent += 1
    set_state(conn, "sla_initialized", now.isoformat())
    conn.commit()
    return sent


# =========================================================================== hotspots
def hotspots(conn, settings: Settings, days: int = 90) -> dict:
    """Where bugs come from: files and folders changed by bug-fix PRs, and components with the most bugs."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    bug_keys = {r[0] for r in conn.execute("SELECT key FROM issues WHERE lower(issuetype)='bug'")}
    files: dict = {}
    folders: dict = {}
    fix_prs = 0
    for p in conn.execute("SELECT repo, number, jira_keys, details, merged_at FROM github_prs WHERE state='MERGED' AND merged_at >= ?",
                          (since,)):
        keys = set(json.loads(p["jira_keys"] or "[]")) & bug_keys
        if not keys:
            continue
        fix_prs += 1
        for f in (json.loads(p["details"] or "{}").get("files") or []):
            path = f["path"] if isinstance(f, dict) else f
            e = files.setdefault((p["repo"], path), {"repo": p["repo"], "path": path, "fixes": 0, "tickets": set()})
            e["fixes"] += 1
            e["tickets"] |= keys
            folder = "/".join(path.split("/")[:-1][:3]) or "(root)"
            g = folders.setdefault((p["repo"], folder), {"repo": p["repo"], "folder": folder, "fixes": 0, "tickets": set()})
            g["fixes"] += 1
            g["tickets"] |= keys
    top = lambda d: [dict(v, tickets=sorted(v["tickets"])[:8], bugs=len(v["tickets"]))
                     for v in sorted(d.values(), key=lambda v: (-len(v["tickets"]), -v["fixes"]))[:15]]
    comp: dict = {}
    for r in conn.execute("SELECT components, status_category, created FROM issues WHERE in_bugs=1 AND created >= ?", (since,)):
        for c in json.loads(r["components"] or "[]") or ["(none)"]:
            x = comp.setdefault(c, {"component": c, "bugs": 0, "open": 0})
            x["bugs"] += 1
            x["open"] += r["status_category"] != "done"
    return {"days": days, "fix_prs": fix_prs, "files": top(files), "folders": top(folders),
            "components": sorted(comp.values(), key=lambda x: -x["bugs"])[:12]}
