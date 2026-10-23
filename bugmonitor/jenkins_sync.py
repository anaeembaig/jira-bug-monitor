"""Jenkins: builds with stages and who triggered them, test reports, build-break alerts, and deploy jobs.

Deploy jobs (names matching [jenkins] deploy_jobs, or the [deployments] workflow keywords) are recorded
as deployments, so they show on the Deployments tab, feed QA handoff and delivery metrics, and get
release notes drafted when they reach production.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from .alerts import Alerter
from .analytics import parse_ts
from .config import Settings
from .db import get_state, now_iso, row_to_dict, set_state
from .locks import FileLock

log = logging.getLogger("bugmonitor.jenkins")
STATE = {"SUCCESS": "SUCCESS", "FAILURE": "FAILURE", "UNSTABLE": "FAILURE", "ABORTED": "CANCELLED", "NOT_BUILT": "SKIPPED"}
RED = {"FAILURE", "UNSTABLE"}
REPO_RE = re.compile(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?/?$", re.I)
PR_RE = re.compile(r"(?:\(#|Merge pull request #)(\d+)")


def _iso_ms(ms) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).replace(microsecond=0).isoformat() if ms else None


def _action(build: dict, key: str):
    return next((a[key] for a in build.get("actions") or [] if isinstance(a, dict) and a.get(key)), None)


def is_deploy_job(settings: Settings, name: str) -> bool:
    n = name.lower()
    pats = settings.jenkins.deploy_jobs
    if pats:
        return any(fnmatch.fnmatch(n, p.lower()) or p.lower() in n for p in pats)
    return any(k.lower() in n for k in settings.deploy.workflow_keywords)


def _environment(settings: Settings, job: dict, params: dict) -> str:
    from .deployments import env_name
    for p in settings.jenkins.env_params:
        for k, v in params.items():
            if k.upper() == p.upper() and v:
                return env_name(settings, str(v))
    tail = re.split(r"[-_ /]", job["full_name"])[-1]
    return env_name(settings, tail if tail.lower() not in ("deploy", "release") else "unknown")


def _pipeline(job: dict) -> str:
    """Branch jobs of one multibranch project share a pipeline name, so the same tests line up."""
    full = job["full_name"]
    return full.rsplit("/", 1)[0] if job.get("branch") and "/" in full else full


def map_build(settings: Settings, job: dict, b: dict, prefixes: set[str]) -> dict:
    from .github_sync import jira_keys
    rev = _action(b, "lastBuiltRevision") or {}
    remotes = _action(b, "remoteUrls") or []
    repo = next((m.group(1) for u in remotes if (m := REPO_RE.search(u or ""))), None)
    params = {p.get("name"): p.get("value") for p in (_action(b, "parameters") or []) if isinstance(p, dict) and p.get("name")}
    causes = _action(b, "causes") or []
    who = next((c.get("userName") or c.get("userId") for c in causes if c.get("userName") or c.get("userId")), None)
    changes = [{"msg": (i.get("msg") or "")[:300], "sha": (i.get("commitId") or "")[:12],
                "author": (i.get("author") or {}).get("fullName")}
               for cs in b.get("changeSets") or [] for i in cs.get("items") or []]
    branch = job.get("branch") or next((br.get("name") for br in rev.get("branch") or []), None)
    if branch and branch.startswith("origin/"):
        branch = branch[7:]
    text = " ".join([c["msg"] for c in changes] + [branch or ""])
    deploy = is_deploy_job(settings, job["full_name"])
    started = _iso_ms(b.get("timestamp"))
    return {
        "id": f"{job['full_name']}#{b['number']}", "job": job["full_name"], "job_url": job["url"], "branch": branch,
        "pipeline": _pipeline(job),
        "number": b["number"], "result": b.get("result"), "building": int(bool(b.get("building"))), "started": started,
        "duration_s": round((b.get("duration") or 0) / 1000, 1) or None, "url": b.get("url"),
        "estimated_s": round((b.get("estimatedDuration") or 0) / 1000, 1) or None,
        "triggered_by": who or next((c.get("shortDescription") for c in causes), None),
        "causes": [c.get("shortDescription") for c in causes if c.get("shortDescription")],
        "sha": rev.get("SHA1"), "repo": repo, "changes": changes,
        "tickets": jira_keys(text, prefixes), "prs": sorted({int(n) for c in changes for n in PR_RE.findall(c["msg"])}),
        "is_deploy": int(deploy), "environment": _environment(settings, job, params) if deploy else None,
        "parameters": {k: (str(v)[:200] if v is not None else None) for k, v in params.items()
                       if not re.search(r"pass|secret|token|key", k or "", re.I)},
    }


COLS = ["id", "job", "job_url", "branch", "number", "result", "building", "started", "duration_s", "url", "triggered_by",
        "causes", "sha", "repo", "changes", "tickets", "stages", "tests", "is_deploy", "environment", "parameters", "pipeline"]
JSON = ("causes", "changes", "tickets", "stages", "tests", "parameters")


KEEP_DAYS = 40      # builds and test results are kept this long so a monthly report covers the whole month


def _jobs(conn, settings: Settings, client) -> list[dict]:
    """Jobs built recently, with their last build number. One request per folder; falls back to the last
    good list (and records the error) when Jenkins can't be reached."""
    roots = settings.jenkins.jobs or [""]
    jobs: list[dict] = []
    errors = []
    for root in roots:
        try:
            jobs += client.list_jobs(root)
        except Exception as e:
            errors.append(f"{root or 'top level'}: {e}")
            log.warning("Jenkins jobs under %s: %s", root or "(top)", e)
    if errors and not jobs:
        set_state(conn, "jenkins_last_error", f"{now_iso()} {errors[0]}")
        cached = get_state(conn, "jenkins_jobs")
        return json.loads(cached) if cached else []
    since_ms = (datetime.now(timezone.utc) - timedelta(days=settings.jenkins.days)).timestamp() * 1000
    ex = [e.lower() for e in settings.jenkins.exclude]
    jobs = [j for j in jobs if (j.get("last_build") or 0) >= since_ms and not any(e in j["full_name"].lower() for e in ex)]
    jobs.sort(key=lambda j: -(j.get("last_build") or 0))
    jobs = jobs[:settings.jenkins.max_jobs]
    set_state(conn, "jenkins_jobs", json.dumps(jobs))
    return jobs


def sync_jenkins(conn, settings: Settings, client, alerter: Alerter | None = None, gh_client=None,
                 force: bool = False) -> dict:
    jk = settings.jenkins
    if not jk.enabled:
        return {"skipped": True}
    last = parse_ts(get_state(conn, "jenkins_last_sync"))
    if not force and last and (datetime.now(timezone.utc) - last).total_seconds() < 240:
        return {"skipped": "recent"}
    from .github_sync import known_prefixes
    alerter = alerter or Alerter(settings, conn)
    first = get_state(conn, "jenkins_first_done") is None
    prefixes = known_prefixes(conn, settings)
    since = datetime.now(timezone.utc) - timedelta(days=jk.days)
    stats = {"jobs": 0, "builds": 0, "tests": 0, "deploys": 0, "alerts": 0, "unchanged": 0}
    seen_last = json.loads(get_state(conn, "jenkins_seen") or "{}")
    jobs = _jobs(conn, settings, client)
    error = get_state(conn, "jenkins_last_error") if not jobs else ""
    for job in jobs:
        stats["jobs"] += 1
        name = job["full_name"]
        running_here = conn.execute("SELECT 1 FROM jenkins_builds WHERE job=? AND building=1 LIMIT 1", (name,)).fetchone()
        mark = f"{job.get('last_number')}:{int(bool(job.get('last_building')))}"
        if job.get("last_number") is not None and seen_last.get(name) == mark and not running_here:
            stats["unchanged"] += 1
            continue                                         # nothing new on this job since last time
        try:
            builds = client.builds(name, 15)
        except Exception as e:
            log.warning("Jenkins builds for %s: %s", name, e)
            continue
        seen_last[name] = mark
        for b in builds:
            started = _iso_ms(b.get("timestamp"))
            if not started or parse_ts(started) < since:
                continue
            bid = f"{name}#{b['number']}"
            old = row_to_dict(conn.execute("SELECT * FROM jenkins_builds WHERE id=?", (bid,)).fetchone())
            if old and not old["building"] and not b.get("building"):
                continue                                     # finished and already stored
            d = map_build(settings, job, b, prefixes)
            # Stages for running builds (live progress) and the moment they finish.
            watch = d["building"] or (old and old["building"])
            d["stages"] = client.stages(name, b["number"]) if watch and "Workflow" in job.get("class", "") \
                else (old or {}).get("stages")
            row = dict(d, **{k: json.dumps(d.get(k)) if d.get(k) is not None else None for k in JSON})
            conn.execute(f"INSERT OR REPLACE INTO jenkins_builds({','.join(COLS)}) VALUES({','.join('?' * len(COLS))})",
                         [row.get(c) for c in COLS])
            stats["builds"] += 1
            if d["is_deploy"]:
                stats["deploys"] += 1
                stats["alerts"] += _record_deploy(conn, settings, alerter, d, old, first, gh_client, prefixes)
        conn.commit()
    set_state(conn, "jenkins_seen", json.dumps(seen_last))
    _settle_stale(conn, settings, client, {j["full_name"] for j in jobs}, alerter, gh_client, prefixes)
    if jk.fetch_tests:
        stats["tests"] = _read_tests(conn, settings, client)
    stats["alerts"] += _break_alerts(conn, settings, alerter, first)
    keep = datetime.now(timezone.utc) - timedelta(days=max(jk.days, KEEP_DAYS))
    conn.execute("DELETE FROM jenkins_builds WHERE started < ?", (keep.isoformat(),))
    set_state(conn, "jenkins_first_done", get_state(conn, "jenkins_first_done") or now_iso())
    set_state(conn, "jenkins_last_sync", now_iso())
    set_state(conn, "jenkins_last_error", error or "")
    conn.commit()
    return stats


def _settle_stale(conn, settings, client, listed: set[str], alerter, gh_client, prefixes) -> None:
    """A build still marked running whose job wasn't checked this time (dropped from the list, or deleted):
    look it up directly once it's well past its expected time."""
    now = datetime.now(timezone.utc)
    for r in [row_to_dict(x) for x in conn.execute("SELECT * FROM jenkins_builds WHERE building=1")]:
        started = parse_ts(r["started"]) or now
        if r["job"] in listed and (now - started).total_seconds() < 6 * 3600:
            continue
        try:
            b = client.build(r["job"], r["number"])
        except Exception as e:
            log.debug("Stale build %s: %s", r["id"], e)
            continue
        if b is None:
            conn.execute("UPDATE jenkins_builds SET building=0, result='ABORTED' WHERE id=?", (r["id"],))
            conn.execute("UPDATE deployments SET state='CANCELLED', progress=NULL WHERE id=?", (f"jenkins:{r['id']}",))
        elif not b.get("building"):
            conn.execute("UPDATE jenkins_builds SET building=0, result=?, duration_s=? WHERE id=?",
                         (b.get("result"), round((b.get("duration") or 0) / 1000, 1) or None, r["id"]))
            if r["is_deploy"]:
                d = dict(r, building=0, result=b.get("result"))
                _record_deploy(conn, settings, alerter, d, r, False, gh_client, prefixes)
    conn.commit()


def _read_tests(conn, settings: Settings, client, limit: int = 40) -> int:
    """JUnit reports for finished builds not read yet, newest first, a few per sync."""
    from .testhealth import jenkins_cases, jenkins_totals, store_report
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM jenkins_builds WHERE building=0 AND is_deploy=0 AND 'jk:' || id NOT IN (SELECT build_id FROM test_runs) "
        "ORDER BY started DESC LIMIT ?", (limit,))]
    read = 0
    for d in rows:
        bid = f"jk:{d['id']}"
        try:
            rep = client.test_report(d["job"], d["number"])
        except Exception as e:
            tries = int(get_state(conn, f"jk_test_err:{d['id']}") or 0) + 1
            set_state(conn, f"jk_test_err:{d['id']}", str(tries))
            log.debug("Test report for %s: %s", d["id"], e)
            if tries < 3:
                continue
            rep = None                                       # give up on this one: record "no report"
        pipeline = d["pipeline"] or d["job"]
        if rep is None:
            store_report(conn, bid, "jenkins", pipeline, d["sha"], d["started"], d["url"], None, d["branch"], d["result"])
            continue
        totals = jenkins_totals(rep)
        store_report(conn, bid, "jenkins", pipeline, d["sha"], d["started"], d["url"], jenkins_cases(rep), d["branch"],
                     d["result"], totals)
        conn.execute("UPDATE jenkins_builds SET tests=? WHERE id=?",
                     (json.dumps({"total": totals[0], "failed": totals[1], "skipped": totals[2]}), d["id"]))
        read += 1
    conn.commit()
    return read


def _record_deploy(conn, settings, alerter, d: dict, old: dict | None, quiet: bool, gh_client, prefixes) -> int:
    from .deployments import DONE_OK, _alert, _what_shipped, draft_release_notes, is_production
    state = "IN_PROGRESS" if d["building"] else STATE.get(d["result"] or "", "FAILURE")
    repo = d["repo"] or d["job"]
    dep_id = f"jenkins:{d['id']}"
    prev = conn.execute("SELECT sha FROM deployments WHERE repo=? AND environment=? AND kind='deployment' AND created < ? "
                        "AND state IN ('SUCCESS','ACTIVE','INACTIVE') AND sha IS NOT NULL ORDER BY created DESC LIMIT 1",
                        (repo, d["environment"], d["started"])).fetchone()
    changes = d.get("changes") or []
    prs = d.get("prs") or sorted({int(n) for c in changes for n in PR_RE.findall(c.get("msg") or "")})
    shipped = {"commits": len(changes), "tickets": d.get("tickets") or [], "prs": prs,
               "authors": sorted({c["author"] for c in changes if c.get("author")}), "rollback": False}
    if not changes and prev and d["sha"] and d["repo"] and gh_client is not None:
        try:
            got = _what_shipped(conn, gh_client, d["repo"], prev["sha"], d["sha"], prefixes)
            if got.get("tickets") is not None:
                shipped = got
        except Exception as e:
            log.debug("Compare for %s: %s", dep_id, e)
    stages = d.get("stages") or []
    done = sum(1 for s in stages if s["status"] in ("SUCCESS", "FAILED", "UNSTABLE", "ABORTED"))
    current = next((s["name"] for s in stages if s["status"] == "IN_PROGRESS"), None)
    progress = {"done": done, "total": len(stages), "current": current} if d["building"] and stages else None
    statuses = [{"state": STATE.get(s["status"], s["status"]), "at": None, "description": s["name"],
                 "log_url": d["url"], "environment_url": None, "by": None} for s in stages]
    conn.execute(
        "INSERT OR REPLACE INTO deployments(id, repo, environment, sha, state, created, updated, url, kind, creator, ref, "
        "description, statuses, log_url, environment_url, commits, tickets, prs, authors, previous_sha, rollback, "
        "duration_s, progress, workflow) VALUES(?,?,?,?,?,?,?,?, 'deployment', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (dep_id, repo, d["environment"], d["sha"], state, d["started"], now_iso(), d["url"], d["triggered_by"],
         d["branch"], f"Jenkins {d['job']} #{d['number']}", json.dumps(statuses), d["url"] + "console" if d["url"] else None,
         None, shipped["commits"], json.dumps(shipped["tickets"] or []), json.dumps(shipped["prs"] or []),
         json.dumps(shipped["authors"] or []), prev["sha"] if prev else None, int(bool(shipped.get("rollback"))),
         d["duration_s"] if not d["building"] else None, json.dumps(progress) if progress else None, "jenkins"))
    if state in DONE_OK and is_production(settings, d["environment"]) and not shipped.get("rollback"):
        draft_release_notes(conn, settings, dep_id)
    conn.commit()
    was = ("IN_PROGRESS" if old["building"] else STATE.get(old["result"] or "", "FAILURE")) if old else None
    if quiet or state == "IN_PROGRESS" or was == state:
        return 0
    n = {"creator": {"login": d["triggered_by"]}, "commitOid": d["sha"] or ""}
    return _alert(conn, settings, alerter, repo, d["environment"], n, state, dict(shipped, tickets=shipped["tickets"] or []),
                  d["url"])


def _is_main(settings: Settings, b) -> bool:
    if b["is_deploy"]:
        return False
    return b["branch"] is None or b["branch"] in settings.code.ci_branches


def _break_alerts(conn, settings: Settings, alerter, quiet: bool) -> int:
    if not settings.jenkins.alert_failures:
        return 0
    me = {x.lower() for x in (get_state(conn, "jira_me"), get_state(conn, "github_login"), settings.jenkins.user) if x}
    sent = 0
    for (job,) in conn.execute("SELECT DISTINCT job FROM jenkins_builds WHERE is_deploy=0").fetchall():
        done = [row_to_dict(r) for r in conn.execute(
            "SELECT * FROM jenkins_builds WHERE job=? AND building=0 AND result IN ('SUCCESS','FAILURE','UNSTABLE') "
            "ORDER BY number DESC LIMIT 2", (job,))]
        if len(done) < 2 or not _is_main(settings, done[0]) or done[0]["result"] not in RED or done[1]["result"] != "SUCCESS":
            continue
        key = f"jk_alerted:{done[0]['id']}"
        if get_state(conn, key):
            continue
        set_state(conn, key, now_iso())
        if quiet:
            continue
        b = done[0]
        authors = {(c.get("author") or "").lower() for c in b.get("changes") or []}
        mine = bool(authors & me) or (b["triggered_by"] or "").lower() in me
        what = "tests failing" if b["result"] == "UNSTABLE" else "build failed"
        alerter.raise_alert({"key": f"{job}#{b['number']}", "summary": f"Jenkins {job}: {what}", "url": b["url"],
                             "status": b["result"], "assignee": b["triggered_by"], "priority": None, "project": ""},
                            "ci_failed", ", ".join(sorted({c.get("author") or "?" for c in b.get("changes") or []})[:4])
                            or (b["triggered_by"] or ""), external=True, personal=mine, team=True)
        sent += 1
    return sent


def sync_jenkins_safe(conn, settings: Settings, alerter: Alerter | None = None, force: bool = False) -> dict | None:
    if not settings.jenkins.enabled:
        return None
    from .jenkins_client import JenkinsClient
    lock = FileLock(settings.db_path.parent / "jenkins.lock")
    if not lock.acquire():
        return {"skipped": True, "reason": "Another Jenkins sync is running."}
    try:
        jk = settings.jenkins
        client = JenkinsClient(jk.url, jk.user, jk.token, jk.ca_bundle)
        gh = None
        if settings.github.enabled:
            try:
                from .github_client import GitHubClient, resolve_token
                gh = GitHubClient(resolve_token(settings.github.token))
            except Exception:
                gh = None
        return sync_jenkins(conn, settings, client, alerter, gh, force)
    except Exception as e:
        conn.rollback()
        set_state(conn, "jenkins_last_error", f"{now_iso()} {e}")
        conn.commit()
        log.warning("Jenkins sync failed: %s", e)
        return {"error": str(e)}
    finally:
        lock.release()


# --------------------------------------------------------------------------- views
def overview(conn, settings: Settings) -> dict:
    win = (datetime.now(timezone.utc) - timedelta(days=settings.jenkins.days)).isoformat()
    rows = [row_to_dict(r) for r in conn.execute("SELECT * FROM jenkins_builds WHERE started >= ? OR building=1 "
                                                 "ORDER BY started DESC LIMIT 200", (win,))]
    running = [r for r in rows if r["building"]]
    for r in running:
        st = r.get("stages") or []
        r["progress"] = {"done": sum(1 for s in st if s["status"] in ("SUCCESS", "FAILED", "UNSTABLE")), "total": len(st),
                         "current": next((s["name"] for s in st if s["status"] == "IN_PROGRESS"), None)} if st else None
    return {"enabled": settings.jenkins.enabled, "url": settings.jenkins.url,
            "last_sync": get_state(conn, "jenkins_last_sync"), "last_error": get_state(conn, "jenkins_last_error") or None,
            "running": running[:20], "recent": rows[:60]}


def for_ticket(conn, key: str, limit: int = 8) -> list[dict]:
    return [{k: r[k] for k in ("job", "number", "branch", "result", "building", "started", "url", "triggered_by",
                               "is_deploy", "environment")}
            for r in conn.execute("SELECT * FROM jenkins_builds WHERE tickets LIKE ? ORDER BY started DESC LIMIT ?",
                                  (f'%"{key}"%', limit))]
