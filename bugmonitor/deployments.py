"""Deployments to every environment: who, when, progress, and which tickets and PRs shipped.

Sources (GitHub):
  Deployments API   every environment (staging, production, ...), with status history,
                    who deployed, logs and environment URLs. GitHub Actions jobs that use
                    `environment:` create these automatically.
  Actions runs      workflows whose name matches [deployments] workflow_keywords, for live
                    step-by-step progress of deploy pipelines.
What shipped: the commits between the previous deploy to the same environment and this one
(GitHub compare), mapped to PRs and Jira keys. A deploy of an older commit is a rollback.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from .alerts import Alerter
from .analytics import parse_ts
from .config import Settings
from .db import get_state, now_iso, row_to_dict, set_state

log = logging.getLogger("bugmonitor.deployments")
DONE_OK = {"SUCCESS", "ACTIVE"}
DONE_BAD = {"FAILURE", "ERROR"}
PR_RE = re.compile(r"(?:\(#|Merge pull request #)(\d+)")


def env_name(settings: Settings, raw: str | None) -> str:
    raw = raw or "unknown"
    for k, v in (settings.deploy.aliases or {}).items():
        if k.lower() == raw.lower():
            return v
    return raw


def is_production(settings: Settings, env: str) -> bool:
    e = (env or "").lower()
    return any(p.lower() == e or e.startswith(p.lower()) for p in settings.github.production_environments)


def _repos(conn, settings: Settings) -> list[str]:
    repos = set(settings.github.repos)
    repos |= {r[0] for r in conn.execute("SELECT DISTINCT repo FROM github_prs")}
    return sorted(repos)


def _jira_keys(text: str, prefixes: set[str]) -> list[str]:
    from .github_sync import jira_keys
    return jira_keys(text, prefixes)


# --------------------------------------------------------------------------- sync
def sync_deployments(conn, settings: Settings, client, alerter: Alerter | None = None) -> dict:
    if not (settings.github.enabled and settings.deploy.enabled):
        return {"skipped": True}
    alerter = alerter or Alerter(settings, conn)
    from .github_sync import known_prefixes
    prefixes = known_prefixes(conn, settings)
    first = get_state(conn, "deploy_first_done") is None
    since = datetime.now(timezone.utc) - timedelta(days=settings.deploy.days)
    stats = {"repos": 0, "deployments": 0, "runs": 0, "alerts": 0}
    for repo in _repos(conn, settings):
        stats["repos"] += 1
        try:
            nodes = client.deployments(repo)
        except Exception as e:
            log.warning("Deployments for %s failed: %s", repo, e)
            continue
        nodes = [n for n in nodes if (parse_ts(n.get("createdAt")) or since) >= since]
        for n in sorted(nodes, key=lambda n: n["createdAt"]):          # oldest first, so "previous" exists
            stats["deployments"] += 1
            stats["alerts"] += _upsert_deployment(conn, settings, client, alerter, repo, n, prefixes, quiet=first)
        if hasattr(client, "workflow_runs"):
            try:
                stats["runs"] += _sync_runs(conn, settings, client, repo, since)
            except Exception as e:
                log.warning("Workflow runs for %s failed: %s", repo, e)
    conn.execute("DELETE FROM deployments WHERE created < ?", ((since - timedelta(days=1)).isoformat(),))
    set_state(conn, "deploy_first_done", now_iso())
    set_state(conn, "deploy_last_sync", now_iso())
    conn.commit()
    return stats


def _statuses(n: dict) -> list[dict]:
    out = [{"state": s.get("state"), "at": s.get("createdAt"), "description": s.get("description"),
            "log_url": s.get("logUrl"), "environment_url": s.get("environmentUrl"),
            "by": (s.get("creator") or {}).get("login")} for s in ((n.get("statuses") or {}).get("nodes") or [])]
    return sorted(out, key=lambda s: s["at"] or "")


def _what_shipped(conn, client, repo: str, base: str | None, head: str, prefixes: set[str]) -> dict:
    """Commits, PRs, Jira keys, and authors between the previous deploy and this one."""
    if not base or base == head:
        return {"commits": 0, "tickets": [], "prs": [], "authors": [], "rollback": False, "identical": base == head}
    try:
        cmp_ = client.compare(repo, base, head)
        rollback = cmp_.get("status") == "behind"
        if rollback:
            # Going back in history: the commits that were taken OUT are base's lead over head.
            cmp_ = client.compare(repo, head, base)
    except Exception as e:
        log.warning("Compare %s %s...%s failed: %s", repo, base[:7], head[:7], e)
        return {"commits": None, "tickets": None, "prs": [], "authors": [], "rollback": False}
    commits = cmp_.get("commits") or []
    keys, prs, authors = [], [], []
    pr_rows = {r["number"]: r for r in conn.execute("SELECT number, jira_keys, title FROM github_prs WHERE repo=?", (repo,))}
    for c in commits:
        msg = ((c.get("commit") or {}).get("message") or "")
        for k in _jira_keys(msg.split("\n")[0] + " " + msg, prefixes):
            if k not in keys:
                keys.append(k)
        for num in PR_RE.findall(msg):
            num = int(num)
            if num not in prs:
                prs.append(num)
                for k in json.loads((pr_rows.get(num) or {"jira_keys": "[]"})["jira_keys"] or "[]"):
                    if k not in keys:
                        keys.append(k)
        who = (c.get("author") or {}).get("login") or ((c.get("commit") or {}).get("author") or {}).get("name")
        if who and who not in authors:
            authors.append(who)
    # For a rollback, tickets/prs/commits are what was REMOVED from the environment.
    return {"commits": cmp_.get("total_commits", len(commits)), "tickets": keys, "prs": prs, "authors": authors,
            "rollback": rollback}


COMPARE_RETRIES = 3


def _upsert_deployment(conn, settings, client, alerter, repo, n, prefixes, quiet) -> int:
    env = env_name(settings, n.get("environment"))
    sts = _statuses(n)
    latest = sts[-1]["state"] if sts else (n.get("state") or "PENDING")
    started = parse_ts(n.get("createdAt"))
    finished = next((parse_ts(s["at"]) for s in sts if s["state"] in DONE_OK | DONE_BAD), None)
    old = row_to_dict(conn.execute("SELECT * FROM deployments WHERE id=?", (n["id"],)).fetchone())
    if old and old.get("tickets") is not None and old["state"] == latest:
        return 0  # nothing new
    if old and old.get("tickets") is not None:
        shipped = {k: old[k] for k in ("commits", "tickets", "prs", "authors", "previous_sha")}
        shipped["rollback"] = bool(old["rollback"])
    else:
        prev = conn.execute(
            "SELECT sha FROM deployments WHERE repo=? AND environment=? AND kind='deployment' AND created < ? "
            "AND state IN ('SUCCESS','ACTIVE','INACTIVE') ORDER BY created DESC LIMIT 1",
            (repo, env, n["createdAt"])).fetchone()
        shipped = _what_shipped(conn, client, repo, prev["sha"] if prev else None, n.get("commitOid") or "", prefixes)
        shipped["previous_sha"] = prev["sha"] if prev else None
        if shipped["tickets"] is None:
            # Compare failed: leave tickets unknown (NULL) so the next sync retries, a few times.
            tries = int(get_state(conn, f"deploy_retry:{n['id']}") or 0) + 1
            set_state(conn, f"deploy_retry:{n['id']}", str(tries))
            if tries >= COMPARE_RETRIES:
                shipped["tickets"] = []
    last_log = next((s["log_url"] for s in reversed(sts) if s["log_url"]), None)
    last_env = next((s["environment_url"] for s in reversed(sts) if s["environment_url"]), None)
    conn.execute(
        "INSERT OR REPLACE INTO deployments(id, repo, environment, sha, state, created, updated, url, kind, creator, ref, "
        "description, statuses, log_url, environment_url, commits, tickets, prs, authors, previous_sha, rollback, "
        "duration_s) VALUES(?,?,?,?,?,?,?,?, 'deployment', ?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (n["id"], repo, env, n.get("commitOid"), latest, n.get("createdAt"), n.get("updatedAt"),
         f"https://github.com/{repo}/deployments/{n.get('environment')}", (n.get("creator") or {}).get("login"),
         (n.get("ref") or {}).get("name"), n.get("description"), json.dumps(sts), last_log, last_env,
         shipped["commits"], None if shipped["tickets"] is None else json.dumps(shipped["tickets"]),
         json.dumps(shipped["prs"]), json.dumps(shipped["authors"]),
         shipped["previous_sha"], int(bool(shipped["rollback"])),
         (finished - started).total_seconds() if finished and started else None))
    if (latest in DONE_OK and shipped["tickets"] is not None and not shipped["rollback"]
            and is_production(settings, env)):
        draft_release_notes(conn, settings, n["id"])
    conn.commit()
    if quiet or shipped["tickets"] is None:
        return 0          # alert once we know what's in it
    newly_known = bool(old) and old.get("tickets") is None
    if old and old["state"] == latest and not newly_known:
        return 0
    return _alert(conn, settings, alerter, repo, env, n, latest, shipped, last_log or last_env)


def draft_release_notes(conn, settings: Settings, deployment_id: str) -> bool:
    """Release notes for a successful production deploy, drafted once and kept for the dashboard."""
    if conn.execute("SELECT 1 FROM release_notes WHERE deployment_id=?", (deployment_id,)).fetchone():
        return False
    from .triage import release_notes
    rn = release_notes(conn, settings, deployment_id=deployment_id)
    d = conn.execute("SELECT repo, environment FROM deployments WHERE id=?", (deployment_id,)).fetchone()
    if not d:
        return False
    conn.execute("INSERT OR IGNORE INTO release_notes(deployment_id, created, repo, environment, title, text, tickets, not_done) "
                 "VALUES(?,?,?,?,?,?,?,?)", (deployment_id, now_iso(), d["repo"], d["environment"], rn["title"],
                                              rn["text"] if rn["tickets"] else rn["text"] + "\n\nNo Jira tickets found in this deploy.",
                                              rn["tickets"], json.dumps(rn["not_done"])))
    return True


def release_notes_list(conn, limit: int = 20) -> list[dict]:
    return [row_to_dict(r) for r in conn.execute(
        "SELECT r.*, d.creator, d.sha, d.url AS deploy_url FROM release_notes r LEFT JOIN deployments d "
        "ON d.id = r.deployment_id ORDER BY r.created DESC LIMIT ?", (limit,))]


def _alert(conn, settings, alerter, repo, env, n, state, shipped, url) -> int:
    me = (get_state(conn, "jira_me") or "").lower()
    mine = [k for k in shipped["tickets"] if (r := conn.execute(
        "SELECT assignee FROM issues WHERE key=?", (k,)).fetchone()) and (r["assignee"] or "").lower() == me]
    who = (n.get("creator") or {}).get("login") or "someone"
    short = f"{repo.split('/')[-1]}@{(n.get('commitOid') or '')[:7]}"
    projects = sorted({k.split("-")[0] for k in shipped["tickets"]})
    base = {"key": f"{repo.split('/')[-1]} → {env}", "summary": "", "url": url, "projects": projects,
            "project": projects[0] if projects else "", "assignee": None, "status": state, "priority": None}
    prod = is_production(settings, env)
    sent = 0
    d = settings.deploy
    if shipped["rollback"] and state in DONE_OK and d.alert_rollbacks:
        alerter.raise_alert(dict(base, summary=f"Rolled back to {short} by {who}"), "rollback",
                            f"{env}: {len(mine)} of your tickets affected" if mine else env,
                            external=True, personal=bool(mine) or prod, team=True)
        sent += 1
    elif state in DONE_BAD and d.alert_failures and (mine or prod):
        alerter.raise_alert(dict(base, summary=f"Deploy of {short} by {who} failed"), "deploy_failed",
                            f"{env}" + (f", includes your {', '.join(mine[:5])}" if mine else ""),
                            external=True, personal=True, team=True)
        sent += 1
    elif state in DONE_OK:
        if mine and d.alert_my_tickets:
            alerter.raise_alert(dict(base, summary=f"{', '.join(mine[:6])} deployed by {who} ({short})"), "deployed",
                                f"{env}, {shipped['commits'] or 0} commits", external=True, team=False)
            sent += 1
        if prod:   # team channels hear about production releases
            alerter.raise_alert(dict(base, summary=f"{short} by {who}: {len(shipped['tickets'])} tickets, "
                                                   f"{shipped['commits'] or 0} commits"), "deployed",
                                ", ".join(shipped["tickets"][:10]), personal=False, team=True)
    return sent


def _sync_runs(conn, settings, client, repo, since) -> int:
    """Deploy pipelines in GitHub Actions, with step-by-step progress for running ones."""
    kws = [k.lower() for k in settings.deploy.workflow_keywords]
    runs = [r for r in client.workflow_runs(repo, since.date().isoformat())
            if any(k in (r.get("name") or "").lower() for k in kws)]
    for r in runs:
        progress = None
        if r.get("status") in ("in_progress", "queued", "waiting", "requested", "pending") and hasattr(client, "run_jobs"):
            try:
                jobs = client.run_jobs(repo, r["id"])
                steps = [s for j in jobs for s in (j.get("steps") or [])]
                done = sum(1 for s in steps if s.get("status") == "completed")
                current = next((f"{j['name']}: {s['name']}" for j in jobs for s in (j.get("steps") or [])
                                if s.get("status") == "in_progress"), None)
                progress = {"done": done, "total": len(steps), "current": current,
                            "jobs": [{"name": j.get("name"), "status": j.get("status"), "conclusion": j.get("conclusion")}
                                     for j in jobs]}
            except Exception as e:
                log.debug("jobs for run %s failed: %s", r["id"], e)
        state = (r.get("conclusion") or r.get("status") or "").upper()
        start, end = parse_ts(r.get("run_started_at") or r.get("created_at")), parse_ts(r.get("updated_at"))
        conn.execute(
            "INSERT OR REPLACE INTO deployments(id, repo, environment, sha, state, created, updated, url, kind, creator, ref, "
            "description, progress, run_id, workflow, duration_s) VALUES(?,?,?,?,?,?,?,?, 'run', ?,?,?,?,?,?,?)",
            (f"run:{r['id']}", repo, r.get("name"), r.get("head_sha"), state, r.get("created_at"), r.get("updated_at"),
             r.get("html_url"), (r.get("actor") or {}).get("login"), r.get("head_branch"), r.get("display_title"),
             json.dumps(progress) if progress else None, r["id"], r.get("name"),
             (end - start).total_seconds() if start and end and r.get("status") == "completed" else None))
    conn.commit()
    return len(runs)


# --------------------------------------------------------------------------- views
def _with_tickets(conn, d: dict) -> dict:
    keys = d.get("tickets") or []
    rows = {r["key"]: dict(r) for r in conn.execute(
        f"SELECT key, summary, status, assignee, url FROM issues WHERE key IN ({','.join('?' * len(keys))})", keys)} if keys else {}
    d["ticket_details"] = [rows.get(k, {"key": k}) for k in keys]
    return d


def overview(conn, settings: Settings, mine_only: bool = False) -> dict:
    """Latest deploy per repo and environment, recent deploys, and pipelines running now."""
    rows = [row_to_dict(r) for r in conn.execute("SELECT * FROM deployments WHERE kind='deployment' ORDER BY created DESC")]
    me = (get_state(conn, "jira_me") or "").lower()
    my_keys = {r["key"] for r in conn.execute("SELECT key FROM issues WHERE lower(assignee)=?", (me,))} if me else set()
    latest: dict = {}
    for r in rows:
        latest.setdefault((r["repo"], r["environment"]), r)
    envs = sorted(latest.values(), key=lambda r: (r["repo"], not is_production(settings, r["environment"]), r["environment"]))
    feed = []
    for r in rows[:150]:
        r["mine"] = sorted(set(r.get("tickets") or []) & my_keys)
        r["production"] = is_production(settings, r["environment"])
        if not mine_only or r["mine"]:
            feed.append(_with_tickets(conn, r))
    runs = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM deployments WHERE kind='run' ORDER BY created DESC LIMIT 40")]
    return {"environments": [_with_tickets(conn, dict(r, production=is_production(settings, r["environment"])))
                             for r in envs],
            "feed": feed[:100], "runs": runs,
            "running": [r for r in runs if r["state"] in ("IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "REQUESTED")]
                       + [r for r in rows if r["state"] in ("IN_PROGRESS", "QUEUED", "PENDING")],
            "last_sync": get_state(conn, "deploy_last_sync")}


def for_ticket(conn, key: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM deployments WHERE kind='deployment' AND tickets LIKE ? ORDER BY created DESC",
                          (f'%"{key}"%',)):
        d = row_to_dict(r)
        out.append({k: d[k] for k in ("repo", "environment", "state", "created", "creator", "sha", "log_url",
                                      "environment_url", "rollback", "url")})
    return out


def environments_for_tickets(conn) -> dict[str, list[str]]:
    """Jira key -> environments it's currently deployed to. Replays deploys oldest first, so a
    rollback takes its tickets back out of that environment."""
    where: dict = {}
    for r in conn.execute("SELECT repo, environment, tickets, rollback FROM deployments WHERE kind='deployment' "
                          "AND state IN ('SUCCESS','ACTIVE','INACTIVE') ORDER BY created"):
        place = (r["repo"], r["environment"])
        for k in json.loads(r["tickets"] or "[]"):
            if r["rollback"]:
                where.get(k, set()).discard(place)
            else:
                where.setdefault(k, set()).add(place)
    out: dict = {}
    for k, places in where.items():
        envs = sorted({e for _, e in places})
        if envs:
            out[k] = envs
    return out


def dora(conn, settings: Settings, days: int = 30, start: datetime | None = None, end: datetime | None = None) -> dict:
    """Delivery metrics from production deploys over the last `days` (or between start and end)."""
    start = start or datetime.now(timezone.utc) - timedelta(days=days)
    end = end or datetime.now(timezone.utc)
    days = max((end - start).days, 1)
    prod = [d for d in (row_to_dict(r) for r in conn.execute(
        "SELECT * FROM deployments WHERE kind='deployment' ORDER BY created"))
        if start <= (parse_ts(d["created"]) or start - timedelta(1)) < end]
    prod = [d for d in prod if is_production(settings, d["environment"])]
    finished = [d for d in prod if d["state"] in DONE_OK | DONE_BAD | {"INACTIVE"}]
    # A change failed if its deploy failed, or if it went out and was later rolled back.
    rolled_back = set()
    for i, d in enumerate(finished):
        if d["rollback"] and d["state"] in DONE_OK | {"INACTIVE"}:
            bad = next((x for x in reversed(finished[:i]) if x["repo"] == d["repo"]
                        and x["environment"] == d["environment"] and x["state"] not in DONE_BAD
                        and not x["rollback"]), None)
            if bad:
                rolled_back.add(bad["id"])
    is_failed = lambda d: d["state"] in DONE_BAD or d["id"] in rolled_back
    changes = [d for d in finished if not d["rollback"]]       # a rollback isn't a change of its own
    failed = [d for d in changes if is_failed(d)]
    lead = []
    for d in changes:
        if d["state"] in DONE_BAD:
            continue
        for num in d.get("prs") or []:
            pr = conn.execute("SELECT merged_at FROM github_prs WHERE repo=? AND number=?", (d["repo"], num)).fetchone()
            if pr and pr["merged_at"]:
                lead.append((parse_ts(d["created"]) - parse_ts(pr["merged_at"])).total_seconds() / 3600)
    restore = []
    for i, d in enumerate(finished):
        if d in failed:
            # Restored by the next good deploy, or by a rollback, in the same place.
            nxt = next((x for x in finished[i + 1:] if x["repo"] == d["repo"] and x["environment"] == d["environment"]
                        and x["state"] not in DONE_BAD), None)
            if nxt:
                restore.append((parse_ts(nxt["created"]) - parse_ts(d["created"])).total_seconds() / 3600)
    med = lambda xs: round(sorted(xs)[len(xs) // 2], 1) if xs else None
    return {"days": days, "deploys": len(changes), "per_week": round(len(changes) / (days / 7), 1),
            "change_failure_rate": round(len(failed) / len(changes) * 100) if changes else None,
            "lead_time_hours": med(lead), "time_to_restore_hours": med(restore),
            "failed": [dict({k: d[k] for k in ("repo", "environment", "state", "created")},
                        rolled_back=d["id"] in rolled_back) for d in failed]}
