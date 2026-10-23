"""Sync your GitHub pull requests and branches, link them to Jira, and alert on changes."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from .alerts import Alerter
from .analytics import parse_ts
from .config import Settings
from .db import get_state, now_iso, set_state
from .github_client import GitHubClient

log = logging.getLogger("bugmonitor.github")
KEY_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,9}-\d+)(?![0-9])")


def jira_keys(text: str, prefixes: set[str]) -> list[str]:
    keys = []
    for m in KEY_RE.findall(text or ""):
        k = m.upper()
        if (not prefixes or k.split("-")[0] in prefixes) and k not in keys:
            keys.append(k)
    return keys


def known_prefixes(conn, settings: Settings) -> set[str]:
    configured = {p.upper() for p in settings.github.jira_prefixes}
    if configured:
        return configured
    return {r[0] for r in conn.execute("SELECT DISTINCT project FROM issues WHERE project IS NOT NULL")}


def pr_status(pr: dict) -> str:
    """One human label for where a PR stands."""
    if pr["state"] == "MERGED":
        return "Merged"
    if pr["state"] == "CLOSED":
        return "Closed"
    if pr.get("is_draft"):
        return "Draft"
    if pr.get("review_decision") == "CHANGES_REQUESTED":
        return "Changes requested"
    if pr.get("checks") in ("FAILURE", "ERROR"):
        return "Checks failing"
    if pr.get("merge_state") == "DIRTY":
        return "Merge conflicts"
    approved = pr.get("review_decision") == "APPROVED" or (
        pr.get("review_decision") is None and (pr.get("approvals") or 0) > 0)
    if approved:
        return "Ready to merge" if pr.get("checks") in (None, "SUCCESS") else "Approved"
    if pr.get("checks") in ("PENDING", "EXPECTED"):
        return "Checks running"
    return "Awaiting review"


def _clip(text: str | None, n: int = 1200) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def _details(n: dict, author: str) -> dict:
    """Reviewers, conversation, review threads, checks, and labels for one PR."""
    login = lambda o: ((o or {}).get("login") or "")
    reviewers = {}
    for r in (n.get("latestReviews") or {}).get("nodes", []) or []:
        who = login(r.get("author"))
        if who and who.lower() != author.lower():
            reviewers[who] = {"login": who, "state": r.get("state"), "at": r.get("submittedAt"),
                              "url": r.get("url"), "pending": False}
    pending = []
    for rr in (n.get("reviewRequests") or {}).get("nodes", []) or []:
        rv = rr.get("requestedReviewer") or {}
        who = rv.get("login") or (f"team:{rv['name']}" if rv.get("name") else "")
        if who:
            pending.append(who)
            reviewers.setdefault(who, {"login": who, "state": None, "at": None, "url": None})
            reviewers[who]["pending"] = True   # re-requested reviewers are pending again

    convo = []
    for c in (n.get("comments") or {}).get("nodes", []) or []:
        convo.append({"id": c["id"], "kind": "comment", "author": login(c.get("author")),
                      "body": _clip(c.get("body")), "at": c.get("createdAt"), "url": c.get("url")})
    for r in (n.get("reviews") or {}).get("nodes", []) or []:
        if (r.get("body") or "").strip() or r.get("state") in ("APPROVED", "CHANGES_REQUESTED"):
            convo.append({"id": r["id"], "kind": "review", "state": r.get("state"),
                          "author": login(r.get("author")), "body": _clip(r.get("body")),
                          "at": r.get("submittedAt"), "url": r.get("url")})
    threads, unresolved, thread_comments = [], 0, 0
    for t in (n.get("reviewThreads") or {}).get("nodes", []) or []:
        first = ((t.get("comments") or {}).get("nodes") or [None])[0]
        thread_comments += (t.get("comments") or {}).get("totalCount") or 0
        if not t.get("isResolved"):
            unresolved += 1
        if first:
            item = {"id": first["id"], "kind": "thread", "author": login(first.get("author")),
                    "body": _clip(first.get("body")), "at": first.get("createdAt"), "url": first.get("url"),
                    "path": t.get("path"), "line": t.get("line"), "resolved": bool(t.get("isResolved")),
                    "outdated": bool(t.get("isOutdated")),
                    "replies": max(((t.get("comments") or {}).get("totalCount") or 1) - 1, 0)}
            threads.append(item)
            convo.append(item)
    convo.sort(key=lambda c: c.get("at") or "", reverse=True)

    checks = []
    commits = (n.get("commits") or {}).get("nodes") or []
    rollup = ((commits[0].get("commit") or {}).get("statusCheckRollup") or {}) if commits else {}
    for c in ((rollup.get("contexts") or {}).get("nodes") or []):
        if "name" in c:
            state = c.get("conclusion") or c.get("status")
            checks.append({"name": c["name"], "state": state, "url": c.get("detailsUrl")})
        elif "context" in c:
            checks.append({"name": c["context"], "state": c.get("state"), "url": c.get("targetUrl")})

    return {
        "reviewers": sorted(reviewers.values(), key=lambda r: (not r["pending"], r["login"].lower())),
        "pending": pending,
        "approvals": sum(1 for r in reviewers.values() if r["state"] == "APPROVED" and not r["pending"]),
        "conversation": convo[:60],
        "unresolved_threads": unresolved,
        "comments_count": ((n.get("comments") or {}).get("totalCount") or 0) + thread_comments
                          + sum(1 for c in convo if c["kind"] == "review" and c["body"]),
        "checks": checks,
        "labels": [l["name"] for l in ((n.get("labels") or {}).get("nodes") or [])],
        "files": [{"path": f.get("path"), "additions": f.get("additions"), "deletions": f.get("deletions")}
                  for f in ((n.get("files") or {}).get("nodes") or []) if f and f.get("path")],
    }


def _map_pr(n: dict, me: str, prefixes: set[str]) -> dict:
    me_l = me.lower()
    author = ((n.get("author") or {}).get("login") or "")
    assignees = [a["login"] for a in (n.get("assignees") or {}).get("nodes", []) if a]
    requested = [((r.get("requestedReviewer") or {}).get("login") or "")
                 for r in (n.get("reviewRequests") or {}).get("nodes", [])]
    reviewers = [((r.get("author") or {}).get("login") or "")
                 for r in (n.get("latestReviews") or {}).get("nodes", [])]
    rel = []
    if author.lower() == me_l: rel.append("authored")
    if me_l in (x.lower() for x in requested): rel.append("review_requested")
    if me_l in (x.lower() for x in reviewers): rel.append("reviewed")
    if me_l in (x.lower() for x in assignees): rel.append("assigned")
    if not rel: rel.append("involved")
    commits = (n.get("commits") or {}).get("nodes") or []
    rollup = ((commits[0].get("commit") or {}).get("statusCheckRollup") or {}) if commits else {}
    det = _details(n, author)
    d = {
        "id": n["id"], "repo": n["repository"]["nameWithOwner"], "number": n["number"],
        "title": n["title"], "url": n["url"], "state": n["state"], "is_draft": int(bool(n["isDraft"])),
        "review_decision": n.get("reviewDecision"), "checks": rollup.get("state"),
        "mergeable": n.get("mergeable"), "head": n.get("headRefName"), "base": n.get("baseRefName"),
        "author": author, "relations": rel,
        "jira_keys": jira_keys(f"{n['title']} {n.get('headRefName', '')}", prefixes),
        "additions": n.get("additions"), "deletions": n.get("deletions"),
        "created": n.get("createdAt"), "updated": n.get("updatedAt"),
        "merged_at": n.get("mergedAt"), "closed_at": n.get("closedAt"),
        "merge_state": n.get("mergeStateStatus"), "labels": det["labels"],
        "approvals": det["approvals"], "pending_reviewers": det["pending"],
        "comments_count": det["comments_count"], "unresolved_threads": det["unresolved_threads"],
        "details": det,
    }
    d["status"] = pr_status(d)
    return d


PR_COLS = ["id", "repo", "number", "title", "url", "state", "is_draft", "review_decision", "checks",
           "mergeable", "head", "base", "author", "relations", "jira_keys", "additions", "deletions",
           "created", "updated", "merged_at", "closed_at", "status", "last_synced", "merge_state",
           "labels", "approvals", "pending_reviewers", "comments_count", "unresolved_threads", "details"]
JSON_COLS = ("relations", "jira_keys", "labels", "pending_reviewers", "details")


def _pr_alerts(old: dict | None, new: dict, me: str = "") -> list[tuple[str, str]]:
    """(kind, detail) pairs for changes worth telling you about."""
    out = []
    mine = "authored" in new["relations"]
    if old is None:
        if "review_requested" in new["relations"] and new["state"] == "OPEN":
            out.append(("review_requested", f"by {new['author']}"))
        return out
    if ("review_requested" in new["relations"] and "review_requested" not in old["relations"]
            and new["state"] == "OPEN"):
        out.append(("review_requested", f"by {new['author']}"))
    if mine:
        if new["checks"] in ("FAILURE", "ERROR") and old["checks"] not in ("FAILURE", "ERROR"):
            out.append(("checks_failed", new["head"] or ""))
        if new["review_decision"] != old["review_decision"]:
            if new["review_decision"] == "APPROVED":
                out.append(("pr_approved", ""))
            elif new["review_decision"] == "CHANGES_REQUESTED":
                out.append(("changes_requested", ""))
        elif (new.get("approvals") or 0) > (old.get("approvals") or 0) and new["review_decision"] is None:
            out.append(("pr_approved", ""))          # repos without required reviews
        if new["state"] == "MERGED" and old["state"] != "MERGED":
            out.append(("pr_merged", f"into {new['base']}"))
        if new.get("merge_state") == "DIRTY" and old.get("merge_state") != "DIRTY":
            out.append(("merge_conflicts", f"{new['head']} → {new['base']}"))
        # New comments or reviews from other people.
        seen = {c["id"] for c in ((old.get("details") or {}).get("conversation") or [])}
        fresh = [c for c in new["details"]["conversation"]
                 if c["id"] not in seen and c["author"].lower() not in (me.lower(), new["author"].lower())]
        if fresh and old.get("details") is not None:
            if len(fresh) == 1:
                c = fresh[0]
                where = f" on {c['path']}" if c.get("path") else ""
                out.append(("pr_comment", f"{c['author']}{where}: {_clip(c['body'], 160)}"))
            else:
                who = ", ".join(sorted({c["author"] for c in fresh}))
                out.append(("pr_comment", f"{len(fresh)} new from {who}"))
    return out


def sync_github(conn, settings: Settings, client: GitHubClient, alerter: Alerter | None = None) -> dict:
    g = settings.github
    alerter = alerter or Alerter(settings, conn)
    me = g.username or get_state(conn, "github_login") or client.viewer()
    set_state(conn, "github_login", me)
    since = (datetime.now(timezone.utc) - timedelta(days=g.days)).strftime("%Y-%m-%d")
    scope = " ".join(f"org:{o}" for o in g.orgs)
    prefixes = known_prefixes(conn, settings)
    first = get_state(conn, "github_last_sync") is None

    # ---- pull requests
    found: dict[str, dict] = {}
    queries = [(f"is:pr involves:{me} updated:>={since} {scope}", None),
               (f"is:pr review-requested:{me} is:open {scope}", None),
               (f"is:pr reviewed-by:{me} updated:>={since} {scope}", None)]
    # Team review queue: open PRs by teammates and PRs waiting on their reviews.
    for m in settings.team.members:
        login = (m.get("github") or "").strip()
        if login and login.lower() != me.lower():
            queries += [(f"is:pr is:open author:{login} {scope}", "team"),
                        (f"is:pr is:open review-requested:{login} {scope}", "team"),
                        # merged work, for hotspots, reviewer suggestions, and regression suspects
                        (f"is:pr is:merged author:{login} merged:>={since} {scope}", "team")]
    for q, extra in queries:
        for n in client.search_prs(q.strip()):
            pr = _map_pr(n, me, prefixes)
            rel = set(pr["relations"]) | ({extra} if extra else set())
            prev = found.get(pr["id"])
            if prev:
                rel |= set(prev["relations"])
            if len(rel) > 1:
                rel.discard("involved")
            pr["relations"] = sorted(rel)
            found[pr["id"]] = pr

    stats = {"prs": len(found), "branches": 0, "alerts": 0}
    old_rows = {}
    for r in conn.execute("SELECT * FROM github_prs"):
        d = dict(r)
        for k in JSON_COLS:
            d[k] = json.loads(d[k]) if d.get(k) else ([] if k != "details" else None)
        old_rows[r["id"]] = d
    for pr in found.values():
        if not first:
            for kind, detail in _pr_alerts(old_rows.get(pr["id"]), pr, me):
                alerter.raise_alert(
                    {"key": f"{pr['repo'].split('/')[-1]}#{pr['number']}", "summary": pr["title"],
                     "priority": None, "status": pr["status"], "assignee": pr["author"],
                     "url": pr["url"]}, kind, detail, external=g.alerts_to_external)
                stats["alerts"] += 1
        row = dict(pr, last_synced=now_iso(), **{k: json.dumps(pr[k]) for k in JSON_COLS})
        conn.execute(
            f"INSERT OR REPLACE INTO github_prs({','.join(PR_COLS)}) VALUES({','.join('?' * len(PR_COLS))})",
            [row.get(c) for c in PR_COLS])
    gone = [i for i in old_rows if i not in found]
    for i in gone:
        conn.execute("DELETE FROM github_prs WHERE id=?", (i,))
    conn.commit()

    # ---- branches: in repos you've opened PRs in, plus any configured repos
    repos = sorted({p["repo"] for p in found.values() if "authored" in p["relations"]} | set(g.repos))
    cutoff = datetime.now(timezone.utc) - timedelta(days=g.days)
    my_heads = {(p["repo"], p["head"]) for p in found.values() if "authored" in p["relations"]}
    branches = []
    for repo in repos:
        try:
            data = client.branches(repo)
        except Exception as e:  # one bad repo shouldn't stop the rest
            log.warning("Branches for %s failed: %s", repo, e)
            continue
        default = (data.get("defaultBranchRef") or {}).get("name")
        for ref in ((data.get("refs") or {}).get("nodes") or []):
            t = ref.get("target") or {}
            login = (((t.get("author") or {}).get("user") or {}).get("login") or "")
            committed = parse_ts(t.get("committedDate"))
            is_mine = login.lower() == me.lower() or (repo, ref["name"]) in my_heads
            if ref["name"] == default or not is_mine or (committed and committed < cutoff):
                continue
            prn = ((ref.get("associatedPullRequests") or {}).get("nodes") or [None])[0] or {}
            branches.append({
                "repo": repo, "name": ref["name"], "sha": (t.get("oid") or "")[:10],
                "committed": t.get("committedDate"), "message": t.get("messageHeadline"),
                "checks": (t.get("statusCheckRollup") or {}).get("state"),
                "pr_number": prn.get("number"), "pr_state": prn.get("state"), "pr_url": prn.get("url"),
                "jira_keys": json.dumps(jira_keys(ref["name"], prefixes)),
                "url": f"https://github.com/{repo}/tree/{ref['name']}",
            })
    cols = ["repo", "name", "sha", "committed", "message", "checks", "pr_number", "pr_state",
            "pr_url", "jira_keys", "url"]
    conn.execute("DELETE FROM github_branches WHERE repo IN (%s)" % ",".join("?" * len(repos)), repos) \
        if repos else None
    for b in branches:
        conn.execute(f"INSERT OR REPLACE INTO github_branches({','.join(cols)}) "
                     f"VALUES({','.join('?' * len(cols))})", [b[c] for c in cols])
    stats["branches"] = len(branches)
    set_state(conn, "github_last_sync", now_iso())
    set_state(conn, "github_last_error", "")
    conn.commit()
    log.info("GitHub sync done: %s", stats)
    return stats
