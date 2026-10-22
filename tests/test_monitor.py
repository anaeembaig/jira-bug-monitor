"""End-to-end tests against a fake Jira. Run: python -m pytest -q"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bugmonitor import service
from bugmonitor.alerts import Alerter
from bugmonitor.analytics import estimate_fix, urgency_score
from bugmonitor.config import load_settings
from bugmonitor.db import connect, set_state
from bugmonitor.sync import sync_once

NOW = datetime.now(timezone.utc)
CATS = {"To Do": "new", "In Progress": "indeterminate", "Code Review": "indeterminate",
        "Done": "done"}


def ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def issue(key, status="To Do", priority="Medium", created_h=1, updated_h=None, resolved_h=None,
          assignee="Sam Lee", comps=("Scheduling",), summary="Something broke"):
    return {"key": key, "id": key.split("-")[1], "fields": {
        "summary": summary, "status": {"name": status, "statusCategory": {"key": CATS[status]}},
        "priority": {"name": priority} if priority else None,
        "assignee": {"displayName": assignee} if assignee else None,
        "reporter": {"displayName": "QA Bot"},
        "created": ts(created_h), "updated": ts(updated_h if updated_h is not None else created_h),
        "resolutiondate": ts(resolved_h) if resolved_h is not None else None,
        "components": [{"name": c} for c in comps], "labels": [],
        "timeoriginalestimate": None, "timespent": None,
        "project": {"key": key.split("-")[0]}, "issuetype": {"name": "Bug"}}}


class FakeJira:
    def __init__(self):
        self.issues, self.logs, self.queries = {}, {}, []

    def search(self, jql):
        self.queries.append(jql)
        yield from self.issues.values()

    def changelog(self, key):
        yield from self.logs.get(key, [])

    def browse_url(self, key):
        return f"https://example.atlassian.net/browse/{key}"


def history(hid, hours_ago, frm, to):
    return {"id": str(hid), "created": ts(hours_ago), "author": {"displayName": "Sam Lee"},
            "items": [{"field": "status", "fromString": frm, "toString": to}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("BUGMON_CONFIG", str(tmp_path / "none.toml"))
    s = load_settings()
    s.alerts.desktop = False
    conn = connect(s.db_path)
    return s, conn, FakeJira()


def seed_history(jira):
    # 6 resolved HUM bugs, each ~4h in progress (plus some review time)
    for i in range(6):
        k = f"HUM-{100 + i}"
        jira.issues[k] = issue(k, "Done", created_h=200 + i, updated_h=100, resolved_h=100)
        jira.logs[k] = [history(i * 10 + 1, 150, "To Do", "In Progress"),
                        history(i * 10 + 2, 146 - i, "In Progress", "Done")]


def test_first_sync_backfills_without_alert_flood(env):
    s, conn, jira = env
    seed_history(jira)
    jira.issues["HUM-1"] = issue("HUM-1")
    stats = sync_once(conn, s, jira, Alerter(s, conn))
    assert stats["first_sync"] and stats["new"] == 7 and stats["alerts"] == 0
    assert "statusCategory != Done" in jira.queries[0]


def test_new_bug_priority_and_reopen_alerts(env):
    s, conn, jira = env
    seed_history(jira)
    sync_once(conn, s, jira)

    jira.issues["HUM-2"] = issue("HUM-2", priority="High", assignee=None,
                                 summary="Customer payroll export fails")
    stats = sync_once(conn, s, jira)
    assert stats["alerts"] == 1
    assert "updated >= -" in jira.queries[-1]
    a = service.list_alerts(conn)
    assert a[0]["kind"] == "new_bug" and a[0]["key"] == "HUM-2"
    assert a[0]["channels"] == ["dashboard"]

    jira.issues["HUM-2"] = issue("HUM-2", priority="Highest", updated_h=0.5, assignee=None)
    sync_once(conn, s, jira)
    assert service.list_alerts(conn)[0]["kind"] == "priority_raised"

    jira.issues["HUM-100"] = issue("HUM-100", "To Do", updated_h=0.1, created_h=200)
    jira.logs["HUM-100"].append(history(999, 0.1, "Done", "To Do"))
    sync_once(conn, s, jira)
    assert service.list_alerts(conn)[0]["kind"] == "reopened"
    t = service.get_ticket(conn, "HUM-100")
    assert t["reopen_count"] == 1

    ack = service.ack_alerts(conn)
    assert ack["acknowledged"] == 3 and service.list_alerts(conn) == []


def test_urgency_orders_open_bugs(env):
    s, conn, jira = env
    jira.issues["HUM-1"] = issue("HUM-1", priority="Low")
    jira.issues["HUM-2"] = issue("HUM-2", priority="Highest", assignee=None,
                                 summary="Production outage for customer")
    sync_once(conn, s, jira)
    keys = [r["key"] for r in service.list_open(conn)]
    assert keys == ["HUM-2", "HUM-1"]
    done = {"status_category": "done"}
    assert urgency_score(done, s) == 0


def test_estimate_uses_similar_history(env):
    s, conn, jira = env
    seed_history(jira)
    jira.issues["HUM-1"] = issue("HUM-1", "In Progress", created_h=10, updated_h=1)
    jira.logs["HUM-1"] = [history(500, 1, "To Do", "In Progress")]
    sync_once(conn, s, jira)
    e = estimate_fix(conn, "HUM-1")
    assert e["sample_size"] == 6
    assert e["basis"] == "same project, component, and priority"
    assert e["metric"].startswith("hands-on")
    assert e["typical_h"] == pytest.approx(6.5, abs=0.1)  # median of 4..9h
    assert e["elapsed_h"] == pytest.approx(1.0, abs=0.1)
    assert "Similar bugs" in e["summary"]


def test_work_log_shared_across_sessions(env, tmp_path):
    s, conn, jira = env
    jira.issues["HUM-1"] = issue("HUM-1")
    sync_once(conn, s, jira)
    service.log_work(conn, "hum-1", "start", "Looking at repro", actor="ali", session="cli")

    other = connect(s.db_path)  # a second Claude session = a second connection
    active = service.active_work(other)
    assert active and active[0]["key"] == "HUM-1" and active[0]["session"] == "cli"

    service.log_work(other, "HUM-1", "note", minutes=30, actor="ali", session="desktop")
    service.log_work(other, "HUM-1", "done", "Fixed null check", actor="ali", session="desktop")
    assert service.active_work(conn) == []
    assert service.work_minutes(conn, "HUM-1") >= 30
    kinds = [e["kind"] for e in service.get_ticket(conn, "HUM-1")["timeline"]]
    assert kinds[0] == "created" and "work:start" in kinds and "work:done" in kinds
    assert service.log_work(conn, "HUM-1", "dance")["error"]


def test_summary_and_sync_health(env):
    s, conn, jira = env
    seed_history(jira)
    jira.issues["HUM-1"] = issue("HUM-1", assignee=None)
    sync_once(conn, s, jira)
    sm = service.summary(conn)
    assert sm["open"] == 1 and sm["new_last_24h"] == 1 and sm["unassigned_open"] == 1
    assert sm["sync"]["last_sync_finished"] and not sm["sync"]["last_sync_error"]


def test_external_channels_respect_min_priority(env, monkeypatch):
    s, conn, jira = env
    s.alerts.slack_webhook = "https://hooks.example/slack"
    s.alerts.teams_webhook = "https://hooks.example/teams"
    s.alerts.external_min_priority = "High"
    sent = []

    class R:
        status_code = 200; text = ""

    monkeypatch.setattr("bugmonitor.alerts.requests.post",
                        lambda url, json, timeout: sent.append((url, json)) or R())
    al = Alerter(s, conn)
    al.raise_alert({"key": "A-1", "summary": "x", "priority": "Low", "url": "u"}, "new_bug")
    assert sent == []
    al.raise_alert({"key": "A-2", "summary": "y", "priority": "Highest", "url": "u"}, "new_bug")
    assert {u for u, _ in sent} == {"https://hooks.example/slack", "https://hooks.example/teams"}
    teams = next(p for u, p in sent if "teams" in u)
    assert teams["attachments"][0]["content"]["type"] == "AdaptiveCard"


def test_jira_client_paginates(monkeypatch):
    from bugmonitor.jira_client import JiraClient
    pages = [{"issues": [{"key": "A-1"}], "nextPageToken": "t2", "isLast": False},
             {"issues": [{"key": "A-2"}], "isLast": True}]
    bodies = []

    class R:
        def __init__(self, d): self.d, self.status_code, self.ok, self.headers = d, 200, True, {}
        def json(self): return self.d

    c = JiraClient("https://x.atlassian.net/", "e@x.com", "tok")
    monkeypatch.setattr(c.session, "request",
                        lambda m, url, timeout, **kw: bodies.append((m, url, kw.get("json"))) or R(pages.pop(0)))
    keys = [i["key"] for i in c.search("issuetype = Bug")]
    assert keys == ["A-1", "A-2"]
    assert bodies[0][1] == "https://x.atlassian.net/rest/api/3/search/jql"
    assert bodies[1][2]["nextPageToken"] == "t2"


def test_other_sessions_can_write_during_sync(env):
    """Regression: a long sync must not hold the write lock while waiting on Jira."""
    import sqlite3
    s, conn, jira = env
    seed_history(jira)
    other = sqlite3.connect(s.db_path, timeout=0.2)  # fails fast if the DB is locked
    writes = []

    class SlowJira(FakeJira):
        def changelog(self, key):
            other.execute("INSERT INTO sync_state(k, v) VALUES(?, '1') "
                          "ON CONFLICT(k) DO UPDATE SET v='1'", (f"probe-{key}",))
            other.commit()
            writes.append(key)
            yield from super().changelog(key)

    slow = SlowJira()
    slow.issues, slow.logs = jira.issues, jira.logs
    sync_once(conn, s, slow)
    assert len(writes) == len(jira.issues)


def test_scope_change_and_retention_prune_old_tickets(env):
    s, conn, jira = env
    jira.issues["HUMFE-1"] = issue("HUMFE-1", created_h=24)
    jira.issues["OTHER-1"] = issue("OTHER-1", created_h=24)
    jira.issues["HUMFE-OLD"] = issue("HUMFE-9", created_h=24 * 90)
    sync_once(conn, s, jira)
    assert conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 3
    service.log_work(conn, "OTHER-1", "note", minutes=10)

    # User narrows scope: Jira now only returns HUMFE bugs.
    s.jira.jql = 'issuetype = Bug AND project = "HUM_Front"'
    s.jira.retention_days = 60
    del jira.issues["OTHER-1"]
    jira.issues["HUMFE-2"] = issue("HUMFE-2", priority="Highest")
    stats = sync_once(conn, s, jira)

    keys = {r["key"] for r in conn.execute("SELECT key FROM issues")}
    assert keys == {"HUMFE-1", "HUMFE-2"}          # other project and 90-day-old bug gone
    assert stats["first_sync"] and stats["alerts"] == 0   # re-sync doesn't flood alerts
    assert service.work_minutes(conn, "OTHER-1") == 10    # personal work log kept

    # Next normal sync is incremental again and alerts work.
    jira.issues["HUMFE-3"] = issue("HUMFE-3")
    stats = sync_once(conn, s, jira)
    assert not stats["first_sync"] and stats["alerts"] == 1


# ------------------------------------------------------------------ GitHub
def gh_pr(num, title, head, author="anaeem", state="OPEN", review=None, checks="SUCCESS",
          requested=(), draft=False, repo="tcp-software/web-schedule"):
    return {"id": f"PR_{num}", "number": num, "title": title, "url": f"https://github.com/{repo}/pull/{num}",
            "state": state, "isDraft": draft, "createdAt": ts(30), "updatedAt": ts(1),
            "mergedAt": ts(1) if state == "MERGED" else None, "closedAt": None,
            "headRefName": head, "baseRefName": "main", "reviewDecision": review, "mergeable": "MERGEABLE",
            "additions": 10, "deletions": 2, "repository": {"nameWithOwner": repo},
            "author": {"login": author}, "assignees": {"nodes": []},
            "reviewRequests": {"nodes": [{"requestedReviewer": {"login": r}} for r in requested]},
            "latestReviews": {"nodes": []},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": checks} if checks else None}}]}}


class FakeGitHub:
    def __init__(self):
        self.prs = {}
        self.refs = {}

    def viewer(self):
        return "anaeem"

    def search_prs(self, q):
        if "review-requested" in q:
            return [p for p in self.prs.values() if any(
                r["requestedReviewer"]["login"] == "anaeem" for r in p["reviewRequests"]["nodes"])]
        if "reviewed-by" in q:
            return []
        return list(self.prs.values())

    def branches(self, repo):
        return {"defaultBranchRef": {"name": "main"}, "refs": {"nodes": self.refs.get(repo, [])}}


def gh_ref(name, login="anaeem", hours=2, pr=None):
    return {"name": name, "target": {"oid": "abc1234567890", "committedDate": ts(hours),
            "messageHeadline": "wip", "author": {"user": {"login": login}},
            "statusCheckRollup": {"state": "SUCCESS"}},
            "associatedPullRequests": {"nodes": [pr] if pr else []}}


def test_github_sync_links_statuses_and_alerts(env):
    from bugmonitor.github_sync import sync_github
    s, conn, jira = env
    s.github.enabled = True
    jira.issues["HUMFE-4233"] = issue("HUMFE-4233")
    sync_once(conn, s, jira)

    gh = FakeGitHub()
    gh.prs["a"] = gh_pr(2520, "HUMFE-4233 fix overtime rounding", "humfe-4233-overtime")
    gh.prs["b"] = gh_pr(811, "Refactor grid", "grid", author="teammate", requested=["anaeem"],
                        repo="tcp-software/web-ui")
    gh.refs["tcp-software/web-schedule"] = [
        gh_ref("main"), gh_ref("humfe-4233-overtime", pr={"number": 2520, "state": "OPEN", "url": "u"}),
        gh_ref("someone-elses", login="teammate"), gh_ref("ancient", hours=24 * 90)]
    st = sync_github(conn, s, gh)
    assert st == {"prs": 2, "branches": 1, "alerts": 0}           # first sync: no alert flood

    prs = {p["number"]: p for p in service.list_prs(conn)}
    assert prs[2520]["jira_keys"] == ["HUMFE-4233"]
    assert prs[2520]["status"] == "Awaiting review"
    assert prs[811]["relations"] == ["review_requested"]
    t = service.get_ticket(conn, "HUMFE-4233")
    assert t["prs"][0]["number"] == 2520 and t["branches"][0]["name"] == "humfe-4233-overtime"
    assert service.list_issues(conn)[0]["prs"][0]["number"] == 2520

    # Changes: my PR's checks fail, then a new review request arrives.
    gh.prs["a"] = gh_pr(2520, "HUMFE-4233 fix overtime rounding", "humfe-4233-overtime", checks="FAILURE")
    gh.prs["c"] = gh_pr(900, "Hotfix", "hotfix", author="lead", requested=["anaeem"])
    st = sync_github(conn, s, gh)
    kinds = sorted(a["kind"] for a in service.list_alerts(conn))
    assert kinds == ["checks_failed", "review_requested"]
    assert service.github_summary(conn)["failing"] == 1
    assert service.github_summary(conn)["needs_my_review"] == 2

    gh.prs["a"] = gh_pr(2520, "HUMFE-4233 fix overtime rounding", "humfe-4233-overtime",
                        state="MERGED", review="APPROVED")
    sync_github(conn, s, gh)
    assert {a["kind"] for a in service.list_alerts(conn)} >= {"pr_approved", "pr_merged"}


def test_pr_status_labels():
    from bugmonitor.github_sync import pr_status
    base = {"state": "OPEN", "is_draft": 0, "review_decision": None, "checks": "SUCCESS"}
    assert pr_status(base) == "Awaiting review"
    assert pr_status(dict(base, is_draft=1)) == "Draft"
    assert pr_status(dict(base, checks="FAILURE")) == "Checks failing"
    assert pr_status(dict(base, review_decision="APPROVED")) == "Ready to merge"
    assert pr_status(dict(base, review_decision="APPROVED", checks="PENDING")) == "Approved"
    assert pr_status(dict(base, review_decision="CHANGES_REQUESTED")) == "Changes requested"
    assert pr_status(dict(base, state="MERGED")) == "Merged"


def test_jira_key_extraction():
    from bugmonitor.github_sync import jira_keys
    assert jira_keys("humfe-4233-fix HUM-12 and UTF-8", {"HUMFE", "HUM"}) == ["HUMFE-4233", "HUM-12"]
    assert jira_keys("feature/HUMFE-1_x", set()) == ["HUMFE-1"]


def test_assigned_to_me_alert(env):
    s, conn, jira = env
    jira.myself = lambda: {"displayName": "Ali Naeem"}
    jira.issues["HUM-5"] = issue("HUM-5", assignee=None, priority="Low")
    sync_once(conn, s, jira)
    jira.issues["HUM-5"] = issue("HUM-5", assignee="Ali Naeem", priority="Low", updated_h=0.1)
    sync_once(conn, s, jira)
    a = service.list_alerts(conn)
    assert a[0]["kind"] == "assigned_to_me"


def test_overview_shapes(env):
    s, conn, jira = env
    seed_history(jira)
    jira.issues["HUM-1"] = issue("HUM-1", priority="High", assignee=None)
    jira.issues["HUM-2"] = issue("HUM-2", created_h=24 * 10)
    sync_once(conn, s, jira)
    o = service.overview(conn, s)
    assert o["kpis"]["open"] == 2 and o["kpis"]["unassigned"] == 1 and o["kpis"]["high_open"] == 1
    assert len(o["backlog"]) == 60 and o["backlog"][-1]["open"] == 2
    assert sum(w["created"] for w in o["weekly"]) == 8 and sum(w["resolved"] for w in o["weekly"]) == 6
    assert [b["label"] for b in o["by_priority"]][:2] == ["High", "Medium"]
    assert sum(b["n"] for b in o["aging"]) == 2
    assert {i["key"] for i in service.list_issues(conn, "resolved")} == {f"HUM-{100+i}" for i in range(6)}


def test_github_client_paginates_and_reports_sso(monkeypatch):
    from bugmonitor.github_client import GitHubClient, GitHubError
    pages = [{"data": {"search": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [{"id": 1}]}}},
             {"data": {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"id": 2}, {}]}}}]
    sent = []

    class R:
        def __init__(self, d, code=200): self.d, self.status_code, self.ok, self.text = d, code, code < 400, ""
        def json(self): return self.d

    c = GitHubClient("tok")
    monkeypatch.setattr(c.s, "post", lambda url, json, timeout: sent.append(json) or R(pages.pop(0)))
    assert [n["id"] for n in c.search_prs("is:pr involves:me")] == [1, 2]
    assert sent[1]["variables"]["after"] == "c1"
    assert c.s.headers["Authorization"] == "Bearer tok"

    monkeypatch.setattr(c.s, "post", lambda url, json, timeout: R(
        {"errors": [{"message": "Resource protected by organization SAML enforcement."}]}))
    with pytest.raises(GitHubError, match="authorize the token"):
        c.viewer()
