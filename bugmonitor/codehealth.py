"""Code health: CI on default branches, reviewer suggestions, and security alerts (GitHub)."""
from __future__ import annotations

import json
import logging
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

from .alerts import Alerter
from .analytics import parse_ts
from .config import Settings
from .db import get_state, now_iso, row_to_dict, set_state

log = logging.getLogger("bugmonitor.codehealth")
SEVERITY = ["low", "medium", "high", "critical"]


def _due(conn, key: str, minutes: float) -> bool:
    last = parse_ts(get_state(conn, key))
    return not last or (datetime.now(timezone.utc) - last).total_seconds() >= minutes * 60


def _repos(conn, settings: Settings) -> list[str]:
    from .deployments import _repos
    return _repos(conn, settings)


# =========================================================================== CI
def sync_ci(conn, settings: Settings, client, alerter: Alerter | None = None, force: bool = False) -> dict:
    c = settings.code
    if not (settings.github.enabled and c.ci) or not hasattr(client, "ci_runs"):
        return {"skipped": True}
    if not force and not _due(conn, "ci_last_sync", 10):
        return {"skipped": "recent"}
    alerter = alerter or Alerter(settings, conn)
    first = get_state(conn, "ci_first_done") is None
    since = (datetime.now(timezone.utc) - timedelta(days=c.ci_days)).date().isoformat()
    me = (get_state(conn, "github_login") or "").lower()
    stats = {"repos": 0, "runs": 0, "alerts": 0}
    for repo in _repos(conn, settings):
        default = get_state(conn, f"default_branch:{repo}")
        if default is None and hasattr(client, "default_branch"):
            try:
                default = client.default_branch(repo) or ""
            except Exception:
                default = ""
            set_state(conn, f"default_branch:{repo}", default)
        branches = [b for b in dict.fromkeys(([default] if default else []) + list(c.ci_branches)) if b]
        stats["repos"] += 1
        for branch in branches:
            try:
                runs = client.ci_runs(repo, branch, since)
            except Exception as e:
                log.warning("CI runs for %s %s failed: %s", repo, branch, e)
                continue
            for r in runs:
                head = r.get("head_commit") or {}
                start, end = parse_ts(r.get("run_started_at") or r.get("created_at")), parse_ts(r.get("updated_at"))
                conn.execute(
                    "INSERT OR REPLACE INTO ci_runs(id, repo, branch, workflow, sha, status, conclusion, created, updated, "
                    "attempt, actor, commit_author, message, url, duration_s) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], repo, r.get("head_branch") or branch, r.get("name"), r.get("head_sha"), r.get("status"),
                     r.get("conclusion"), r.get("created_at"), r.get("updated_at"), r.get("run_attempt") or 1,
                     (r.get("actor") or {}).get("login"), ((head.get("author") or {}).get("name")),
                     (head.get("message") or "").split("\n")[0][:200], r.get("html_url"),
                     (end - start).total_seconds() if start and end and r.get("status") == "completed" else None))
                stats["runs"] += 1
        if default and hasattr(client, "branch_checks"):
            try:
                set_state(conn, f"branch_checks:{repo}", json.dumps(dict(client.branch_checks(repo, default), branch=default,
                                                                         at=now_iso())))
            except Exception as e:
                log.debug("Checks on %s %s: %s", repo, default, e)
    conn.commit()
    # A default branch that just went red.
    for (repo, wf, branch) in conn.execute("SELECT DISTINCT repo, workflow, branch FROM ci_runs").fetchall():
        done = conn.execute("SELECT * FROM ci_runs WHERE repo=? AND workflow=? AND branch=? AND status='completed' "
                            "AND conclusion IN ('success','failure','timed_out') ORDER BY created DESC LIMIT 2",
                            (repo, wf, branch)).fetchall()
        if len(done) < 2 or done[0]["conclusion"] == "success" or done[1]["conclusion"] != "success":
            continue
        key = f"ci_alerted:{done[0]['id']}"
        if get_state(conn, key):
            continue
        set_state(conn, key, now_iso())
        if first or not c.alert_ci_failures:
            continue
        r = done[0]
        broke_it = (r["actor"] or "").lower() == me
        alerter.raise_alert({"key": f"{repo.split('/')[-1]}:{branch}", "summary": f"{wf} failed on {branch}",
                             "url": r["url"], "status": "failure", "assignee": r["actor"], "priority": None,
                             "project": ""},
                            "ci_failed", f"{(r['sha'] or '')[:7]} {r['message'] or ''} by {r['commit_author'] or r['actor']}",
                            external=True, personal=broke_it, team=True)
        stats["alerts"] += 1
    # Kept for 40 days so monthly reports cover a whole month; views show the last ci_days.
    keep = (datetime.now(timezone.utc) - timedelta(days=max(c.ci_days + 1, 40))).isoformat()
    conn.execute("DELETE FROM ci_runs WHERE created < ?", (keep,))
    set_state(conn, "ci_first_done", get_state(conn, "ci_first_done") or now_iso())
    set_state(conn, "ci_last_sync", now_iso())
    conn.commit()
    return stats


def ci_overview(conn, settings: Settings) -> dict:
    window = (datetime.now(timezone.utc) - timedelta(days=settings.code.ci_days)).isoformat()
    runs = [row_to_dict(r) for r in conn.execute("SELECT * FROM ci_runs WHERE created >= ? ORDER BY created", (window,))]
    groups: dict = {}
    for r in runs:
        groups.setdefault((r["repo"], r["workflow"], r["branch"]), []).append(r)
    pipelines = []
    for (repo, wf, branch), rs in groups.items():
        done = [r for r in rs if r["status"] == "completed" and r["conclusion"] in ("success", "failure", "timed_out", "cancelled")]
        judged = [r for r in done if r["conclusion"] != "cancelled"]
        ok = [r for r in judged if r["conclusion"] == "success"]
        durations = [r["duration_s"] / 60 for r in ok if r["duration_s"]]
        latest = rs[-1]
        last_done = done[-1] if done else None
        streak = 0
        for r in reversed(judged):
            if r["conclusion"] == "success":
                break
            streak += 1
        daily: dict = {}
        for r in ok:
            if r["duration_s"]:
                daily.setdefault((r["created"] or "")[:10], []).append(r["duration_s"] / 60)
        pipelines.append({
            "repo": repo, "workflow": wf, "branch": branch, "runs": len(rs),
            "state": latest["status"] if latest["status"] != "completed" else latest["conclusion"],
            "failing": bool(last_done and last_done["conclusion"] in ("failure", "timed_out")),
            "failing_since": next((r["created"] for r in reversed(judged) if r["conclusion"] == "success"), None) if streak else None,
            "failed_in_a_row": streak,
            "success_rate": round(len(ok) / len(judged) * 100) if judged else None,
            "median_minutes": round(statistics.median(durations), 1) if durations else None,
            "duration_trend": [{"date": d, "minutes": round(statistics.median(v), 1)} for d, v in sorted(daily.items())],
            "url": latest["url"],
        })
    pipelines.sort(key=lambda p: (not p["failing"], p["success_rate"] if p["success_rate"] is not None else 101))
    flaky = [{"repo": r["repo"], "workflow": r["workflow"], "branch": r["branch"], "sha": (r["sha"] or "")[:7],
              "attempt": r["attempt"], "url": r["url"], "created": r["created"]}
             for r in runs if (r["attempt"] or 1) > 1 and r["conclusion"] == "success"]
    by_wf: dict = {}
    for f in flaky:
        by_wf[(f["repo"], f["workflow"])] = by_wf.get((f["repo"], f["workflow"]), 0) + 1
    failures = [{"repo": r["repo"], "workflow": r["workflow"], "branch": r["branch"], "sha": (r["sha"] or "")[:7],
                 "message": r["message"], "who": r["commit_author"] or r["actor"], "url": r["url"], "created": r["created"]}
                for r in reversed(runs) if r["conclusion"] in ("failure", "timed_out")][:20]
    judged = [r for r in runs if r["status"] == "completed" and r["conclusion"] in ("success", "failure", "timed_out")]
    for p in pipelines:
        p["source"] = "github"
    # Jenkins jobs on default branches, in the same shape.
    jwin = (datetime.now(timezone.utc) - timedelta(days=settings.jenkins.days)).isoformat()
    jk = [row_to_dict(r) for r in conn.execute("SELECT * FROM jenkins_builds WHERE is_deploy=0 AND started >= ? "
                                               "ORDER BY started", (jwin,))]
    jgroups: dict = {}
    for r in jk:
        if r["branch"] is None or r["branch"] in settings.code.ci_branches:
            jgroups.setdefault(r["job"], []).append(r)
    for job, rs in jgroups.items():
        done = [r for r in rs if not r["building"] and r["result"] in ("SUCCESS", "FAILURE", "UNSTABLE")]
        ok = [r for r in done if r["result"] == "SUCCESS"]
        mins = [r["duration_s"] / 60 for r in ok if r["duration_s"]]
        streak = 0
        for r in reversed(done):
            if r["result"] == "SUCCESS":
                break
            streak += 1
        latest = rs[-1]
        state = "in_progress" if latest["building"] else {"SUCCESS": "success", "FAILURE": "failure", "UNSTABLE": "unstable",
                                                          "ABORTED": "cancelled"}.get(latest["result"] or "", "unknown")
        pipelines.append({"repo": latest["repo"] or "Jenkins", "workflow": job, "branch": latest["branch"] or "—",
                          "runs": len(rs), "state": state, "source": "jenkins",
                          "failing": bool(done and done[-1]["result"] != "SUCCESS"), "failed_in_a_row": streak,
                          "failing_since": None, "success_rate": round(len(ok) / len(done) * 100) if done else None,
                          "median_minutes": round(statistics.median(mins), 1) if mins else None, "duration_trend": [],
                          "url": latest["url"]})
        for r in done:
            judged.append({"conclusion": "success" if r["result"] == "SUCCESS" else "failure"})
        failures += [{"repo": r["repo"] or "Jenkins", "workflow": job, "branch": r["branch"] or "—", "sha": (r["sha"] or "")[:7],
                      "message": (r.get("changes") or [{}])[-1].get("msg") if r.get("changes") else f"#{r['number']}",
                      "who": ", ".join(sorted({c.get("author") or "" for c in r.get("changes") or []})) or r["triggered_by"],
                      "url": r["url"], "created": r["started"]} for r in done if r["result"] != "SUCCESS"]
    pipelines.sort(key=lambda p: (not p["failing"], p["success_rate"] if p["success_rate"] is not None else 101))
    failures = sorted(failures, key=lambda f: f["created"] or "", reverse=True)[:20]
    checks = []
    for (k, v) in conn.execute("SELECT k, v FROM sync_state WHERE k LIKE 'branch_checks:%'"):
        try:
            c = json.loads(v)
        except ValueError:
            continue
        checks.append(dict(c, repo=k.split(":", 1)[1]))
    return {"enabled": (settings.github.enabled and settings.code.ci) or settings.jenkins.enabled,
            "jenkins": settings.jenkins.enabled, "last_sync": get_state(conn, "ci_last_sync"),
            "branch_checks": checks, "jenkins_last_sync": get_state(conn, "jenkins_last_sync"),
            "jenkins_error": get_state(conn, "jenkins_last_error") or None,
            "runs": sum(p["runs"] for p in pipelines), "success_rate": round(sum(r["conclusion"] == "success" for r in judged) / len(judged) * 100)
            if judged else None,
            "failing_now": sum(1 for p in pipelines if p["failing"]), "pipelines": pipelines,
            "flaky": flaky[-20:][::-1],
            "flaky_by_workflow": [{"repo": k[0], "workflow": k[1], "retries": v} for k, v in sorted(by_wf.items(), key=lambda kv: -kv[1])],
            "recent_failures": failures}


# =========================================================================== reviewer suggestions
def _files(p: dict) -> set[str]:
    return {f["path"] if isinstance(f, dict) else f for f in ((p.get("details") or {}).get("files") or [])}


def reviewer_suggestions(conn, settings: Settings, repo: str, number: int, limit: int = 3) -> dict:
    """Who knows this code and has room to review: past authors and reviewers of the same files, minus current load."""
    pr = row_to_dict(conn.execute("SELECT * FROM github_prs WHERE repo=? AND number=?", (repo, int(number))).fetchone())
    if not pr:
        return {"error": f"{repo}#{number} isn't in your synced PRs."}
    files = _files(pr)
    author = (pr["author"] or "").lower()
    team = {(m.get("github") or "").lower(): m.get("name") for m in settings.team.members if m.get("github")}
    since = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    expertise: dict = {}
    reasons: dict = {}

    def credit(login, pts, why):
        login = (login or "").lower()
        if not login or login == author or login.endswith("[bot]") or (team and login not in team):
            return
        expertise[login] = expertise.get(login, 0) + pts
        reasons.setdefault(login, Counter())[why] += 1

    for p in conn.execute("SELECT * FROM github_prs WHERE repo=? AND id != ? AND updated >= ?", (repo, pr["id"], since)):
        p = row_to_dict(p)
        overlap = files & _files(p)
        if not overlap:
            continue
        credit(p["author"], len(overlap), "changed these files")
        for r in (p.get("details") or {}).get("reviewers") or []:
            if r.get("state") and not r.get("pending"):
                credit(r.get("login"), 1.5 * len(overlap), "reviewed changes here")
    load: dict = {}
    for p in conn.execute("SELECT pending_reviewers FROM github_prs WHERE state='OPEN'"):
        for who in json.loads(p["pending_reviewers"] or "[]"):
            load[who.lower()] = load.get(who.lower(), 0) + 1
    if not expertise and team:      # no history on these files: least-loaded teammates
        for login in team:
            if login != author:
                expertise[login] = 0.1
                reasons.setdefault(login, Counter())["teammate"] += 1
    already = {w.lower() for w in pr.get("pending_reviewers") or []} | {
        (r.get("login") or "").lower() for r in (pr.get("details") or {}).get("reviewers") or []}
    out = []
    for login, pts in expertise.items():
        score = pts / (1 + 0.4 * load.get(login, 0))
        why = [f"{why} ({n} PR{'s' * (n != 1)})" if why != "teammate" else "teammate" for why, n in reasons[login].most_common()]
        if load.get(login):
            why.append(f"{load[login]} review{'s' * (load[login] != 1)} waiting")
        out.append({"login": login, "name": team.get(login), "score": round(score, 2), "reasons": why,
                    "already_requested": login in already})
    out.sort(key=lambda x: (x["already_requested"], -x["score"]))
    return {"repo": repo, "number": int(number), "title": pr["title"], "url": pr["url"], "files": len(files),
            "suggestions": out[:limit]}


def my_review_suggestions(conn, settings: Settings) -> list[dict]:
    """For your open PRs that nobody has been asked to review yet."""
    out = []
    for p in conn.execute("SELECT * FROM github_prs WHERE state='OPEN' AND is_draft=0 AND relations LIKE '%authored%'"):
        p = row_to_dict(p)
        reviewed = [r for r in (p.get("details") or {}).get("reviewers") or [] if r.get("state")]
        if p.get("pending_reviewers") or reviewed:
            continue
        s = reviewer_suggestions(conn, settings, p["repo"], p["number"])
        if s.get("suggestions"):
            out.append(s)
    return out


# =========================================================================== security alerts
def _sev(x: str | None) -> str:
    x = (x or "").lower()
    return {"error": "high", "warning": "medium", "note": "low", "moderate": "medium"}.get(x, x if x in SEVERITY else "low")


def sync_security(conn, settings: Settings, client, alerter: Alerter | None = None, force: bool = False) -> dict:
    c = settings.code
    if not (settings.github.enabled and c.security) or not hasattr(client, "security_alerts"):
        return {"skipped": True}
    if not force and not _due(conn, "security_last_sync", 60):
        return {"skipped": "recent"}
    alerter = alerter or Alerter(settings, conn)
    first = get_state(conn, "security_first_done") is None
    floor = SEVERITY.index(_sev(c.security_min_severity))
    stats = {"repos": 0, "open": 0, "alerts": 0}
    for repo in _repos(conn, settings):
        try:
            data = client.security_alerts(repo)
        except Exception as e:
            log.warning("Security alerts for %s failed: %s", repo, e)
            continue
        stats["repos"] += 1
        found = []
        for a in data.get("dependabot") or []:
            adv, vuln = a.get("security_advisory") or {}, a.get("security_vulnerability") or {}
            pkg = ((vuln.get("package") or (a.get("dependency") or {}).get("package") or {}).get("name"))
            found.append({"id": f"{repo}:dependabot:{a['number']}", "repo": repo, "kind": "dependabot", "number": a["number"],
                          "severity": _sev(vuln.get("severity") or adv.get("severity")), "state": a.get("state"),
                          "package": pkg, "summary": adv.get("summary"), "cve": adv.get("cve_id"), "ghsa": adv.get("ghsa_id"),
                          "url": a.get("html_url"), "created": a.get("created_at"), "updated": a.get("updated_at")})
        for a in data.get("code_scanning") or []:
            rule = a.get("rule") or {}
            loc = ((a.get("most_recent_instance") or {}).get("location") or {}).get("path")
            found.append({"id": f"{repo}:code_scanning:{a['number']}", "repo": repo, "kind": "code_scanning",
                          "number": a["number"], "severity": _sev(rule.get("security_severity_level") or rule.get("severity")),
                          "state": a.get("state"), "package": loc, "summary": rule.get("description") or rule.get("id"),
                          "cve": None, "ghsa": None, "url": a.get("html_url"), "created": a.get("created_at"),
                          "updated": a.get("updated_at")})
        ids = [f["id"] for f in found]
        conn.execute(f"DELETE FROM security_alerts WHERE repo=? AND id NOT IN ({','.join('?' * len(ids))})", [repo] + ids)
        for f in found:
            refs = [r[0] for r in conn.execute(
                "SELECT key FROM issues WHERE (? IS NOT NULL AND (summary LIKE ? OR description LIKE ? OR labels LIKE ?)) "
                "OR (? IS NOT NULL AND (summary LIKE ? OR description LIKE ?))",
                (f["ghsa"], f"%{f['ghsa']}%", f"%{f['ghsa']}%", f"%{f['ghsa']}%",
                 f["cve"], f"%{f['cve']}%", f"%{f['cve']}%"))]
            exists = conn.execute("SELECT 1 FROM security_alerts WHERE id=?", (f["id"],)).fetchone()
            conn.execute("INSERT OR REPLACE INTO security_alerts(id, repo, kind, number, severity, state, package, summary, cve, "
                         "ghsa, url, created, updated, jira_keys) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f["id"], repo, f["kind"], f["number"], f["severity"], f["state"], f["package"], f["summary"],
                          f["cve"], f["ghsa"], f["url"], f["created"], f["updated"], json.dumps(refs)))
            stats["open"] += 1
            if not exists and not first and c.alert_security and SEVERITY.index(f["severity"]) >= floor:
                what = "Dependency" if f["kind"] == "dependabot" else "Code scanning"
                alerter.raise_alert({"key": f"{repo.split('/')[-1]}:{f['package'] or f['number']}",
                                     "summary": f["summary"] or "", "url": f["url"], "priority": None,
                                     "status": f["severity"].title(), "assignee": None, "project": ""},
                                    "security", f"{what}, {f['severity']} severity" + (f", {f['cve']}" if f["cve"] else ""),
                                    external=True, team=True)
                stats["alerts"] += 1
    set_state(conn, "security_first_done", get_state(conn, "security_first_done") or now_iso())
    set_state(conn, "security_last_sync", now_iso())
    conn.commit()
    return stats


def security_overview(conn, settings: Settings) -> dict:
    now = datetime.now(timezone.utc)
    rows = [row_to_dict(r) for r in conn.execute("SELECT * FROM security_alerts")]
    for r in rows:
        r["jira_keys"] = json.loads(r["jira_keys"] or "[]") if isinstance(r["jira_keys"], str) else r["jira_keys"] or []
        c = parse_ts(r["created"])
        r["age_days"] = round((now - c).total_seconds() / 86400) if c else None
    rows.sort(key=lambda r: (-SEVERITY.index(r["severity"] if r["severity"] in SEVERITY else "low"), -(r["age_days"] or 0)))
    by_sev = {s: sum(1 for r in rows if r["severity"] == s) for s in reversed(SEVERITY)}
    by_repo: dict = {}
    for r in rows:
        by_repo[r["repo"]] = by_repo.get(r["repo"], 0) + 1
    return {"enabled": settings.github.enabled and settings.code.security, "last_sync": get_state(conn, "security_last_sync"),
            "open": len(rows), "by_severity": by_sev,
            "by_repo": [{"repo": k, "n": v} for k, v in sorted(by_repo.items(), key=lambda kv: -kv[1])],
            "untracked": sum(1 for r in rows if not r["jira_keys"] and r["severity"] in ("high", "critical")),
            "items": rows[:200]}
