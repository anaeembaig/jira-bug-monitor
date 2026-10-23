"""v1.2: Jenkins builds and deploys, test results and flaky tests, release notes on deploy, reports."""
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from bugmonitor import codehealth, jenkins_sync, jobs, reports, service, testhealth
from bugmonitor.alerts import Alerter
from bugmonitor.config import Route, load_settings
from bugmonitor.dashboard import _extra_get
from bugmonitor.db import connect, set_state
from bugmonitor.deployments import release_notes_list
from bugmonitor.jenkins_client import JenkinsClient
from bugmonitor.sync import sync_once
from test_team import TeamJira, sp, t
from test_v11 import CodeGH, run

NOW = datetime.now(timezone.utc)
ms = lambda h: int((NOW - timedelta(hours=h)).timestamp() * 1000)


def jbuild(n, result, hours_ago, sha="abc", msgs=(), who="Sam Lee", building=False, params=None, branch=None):
    actions = [{"causes": [{"shortDescription": f"Started by user {who}", "userName": who}]},
               {"lastBuiltRevision": {"SHA1": sha, "branch": [{"name": f"origin/{branch or 'main'}"}]},
                "remoteUrls": ["https://github.com/tcp-software/web-schedule.git"]}]
    if params:
        actions.append({"parameters": [{"name": k, "value": v} for k, v in params.items()]})
    return {"number": n, "url": f"https://jenkins/job/x/{n}/", "result": None if building else result, "building": building,
            "timestamp": ms(hours_ago), "duration": 0 if building else 600000, "estimatedDuration": 600000,
            "actions": actions, "changeSets": [{"items": [{"msg": m, "commitId": sha + str(i), "author": {"fullName": who}}
                                                         for i, m in enumerate(msgs)]}]}


class FakeJenkins:
    """Same interface as JenkinsClient. Data is keyed by job URL (builds) and build URL (stages, tests)."""
    def __init__(self):
        self.jobs = []
        self.b = {}          # job url -> builds (newest first)
        self.st = {}
        self.tr = {}

    def _job(self, full_name):
        return next(j for j in self.jobs if j["full_name"] == full_name)

    def _build(self, full_name, number):
        return next((b for b in self.b.get(self._job(full_name)["url"], []) if b["number"] == number), None)

    def list_jobs(self, root=""):
        return self.jobs

    def builds(self, full_name, limit=20):
        return self.b.get(self._job(full_name)["url"], [])[:limit]

    def build(self, full_name, number):
        return self._build(full_name, number)

    def stages(self, full_name, number):
        b = self._build(full_name, number)
        return self.st.get(b["url"]) if b else None

    def test_report(self, full_name, number):
        b = self._build(full_name, number)
        rep = self.tr.get(b["url"]) if b else None
        if rep and "totalCount" not in rep:
            rep = dict(rep, totalCount=sum(len(su["cases"]) for su in rep["suites"]))
        return rep


def report(*cases):
    return {"failCount": sum(1 for c in cases if c[1] == "FAILED"), "passCount": 0, "skipCount": 0,
            "suites": [{"cases": [{"className": c[0].rsplit(".", 1)[0], "name": c[0].rsplit(".", 1)[1], "status": c[1],
                                   "errorDetails": "boom" if c[1] == "FAILED" else None} for c in cases]}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "v12.db"))
    s = load_settings(tmp_path / "none.toml")
    s.alerts.desktop = False
    s.alerts.teams_webhook = "https://private"
    s.team.jql = "TEAMSCOPE project in (HUM)"
    s.team.members = [{"name": "Ali Naeem", "github": "anaeem"}, {"name": "Sam Lee", "github": "sam"}]
    s.timelog.workdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    s.jenkins.enabled = True
    s.jenkins.user = "ali"
    s.routes = [Route(name="web", webhook="https://web", events=["ci", "deployment"])]
    posts = []

    class R:
        status_code = 202; text = ""

    monkeypatch.setattr("bugmonitor.alerts.requests.post", lambda url, json, timeout: posts.append((url, json)) or R())
    j = TeamJira()
    j.myself = lambda: {"displayName": "Ali Naeem", "accountId": "acc-AliNaeem"}
    j.issues["HUM-1"] = t("HUM-1", "In QA", assignee="Ali Naeem", summary="Split shift totals", itype="Bug")
    j.issues["HUM-2"] = t("HUM-2", "Done", assignee="Sam Lee", summary="Payroll column", resolved_h=2, updated_h=2)
    conn = connect(s.db_path)
    sync_once(conn, s, j)
    return s, conn, j, posts


def kinds(conn):
    return [a["kind"] for a in service.list_alerts(conn)]


def test_jenkins_builds_breaks_tests_and_flaky(env):
    s, conn, j, posts = env
    jk = FakeJenkins()
    main = {"name": "main", "full_name": "web-schedule/main", "url": "https://jenkins/job/web-schedule/job/main/",
            "class": "WorkflowJob", "branch": "main", "last_build": ms(1)}
    pr = {"name": "PR-7", "full_name": "web-schedule/PR-7", "url": "https://jenkins/job/web-schedule/job/PR-7/",
          "class": "WorkflowJob", "branch": "PR-7", "last_build": ms(1)}
    jk.jobs = [main, pr]
    jk.b[main["url"]] = [jbuild(2, "SUCCESS", 5, sha="s2"), jbuild(1, "SUCCESS", 8, sha="s1")]
    jk.tr = {"https://jenkins/job/x/2/": report(("app.ShiftTest.total", "PASSED"), ("app.PayTest.col", "PASSED"))}
    jenkins_sync.sync_jenkins(conn, s, jk, force=True)                  # first sync: history, no alerts
    assert conn.execute("SELECT COUNT(*) FROM jenkins_builds").fetchone()[0] == 2
    assert kinds(conn) == []

    # Main breaks with a test failure caused by Ali's commit: you and the channel hear about it.
    jk.b[main["url"]].insert(0, jbuild(3, "UNSTABLE", 1, sha="s3", msgs=["HUM-1 fix totals"], who="Ali Naeem"))
    jk.tr["https://jenkins/job/x/3/"] = report(("app.ShiftTest.total", "FAILED"), ("app.PayTest.col", "PASSED"))
    posts.clear()
    st = jenkins_sync.sync_jenkins(conn, s, jk, force=True)
    assert st["alerts"] == 1 and kinds(conn)[0] == "ci_failed"
    assert sorted(u for u, _ in posts) == ["https://private", "https://web"]
    assert jenkins_sync.sync_jenkins(conn, s, jk, force=True)["alerts"] == 0        # once
    b = conn.execute("SELECT * FROM jenkins_builds WHERE id='web-schedule/main#3'").fetchone()
    assert json.loads(b["tickets"]) == ["HUM-1"] and b["repo"] == "tcp-software/web-schedule" and b["pipeline"] == "web-schedule"
    assert json.loads(b["tests"]) == {"total": 2, "failed": 1, "skipped": 0}
    assert service.get_ticket(conn, "HUM-1")["builds"][0]["number"] == 3

    # A PR build of the same commit passes the same test: that's flaky, not broken.
    jk.b[pr["url"]] = [jbuild(9, "SUCCESS", 0.5, sha="s3", branch="PR-7")]
    jk.tr["https://jenkins/job/x/9/"] = report(("app.ShiftTest.total", "PASSED"), ("app.PayTest.col", "PASSED"))
    jenkins_sync.sync_jenkins(conn, s, jk, force=True)
    f = testhealth.flaky_tests(conn, s)
    assert [x["test"] for x in f["flaky"]] == ["app.ShiftTest.total"] and f["flaky"][0]["same_commit"] == 1
    assert f["flaky"][0]["last_error"] == "boom"

    ov = codehealth.ci_overview(conn, s)
    jp = next(p for p in ov["pipelines"] if p["source"] == "jenkins" and p["workflow"] == "web-schedule/main")
    assert jp["failing"] and jp["success_rate"] == 67
    assert ov["recent_failures"][0]["who"] == "Ali Naeem"


def test_jenkins_deploy_job_records_deploy_and_drafts_release_notes(env):
    s, conn, j, posts = env
    jk = FakeJenkins()
    dep = {"name": "deploy-web", "full_name": "deploy-web", "url": "https://jenkins/job/deploy-web/", "class": "WorkflowJob",
           "branch": None, "last_build": ms(1)}
    jk.jobs = [dep]
    jk.b[dep["url"]] = [jbuild(40, "SUCCESS", 30, sha="d0", params={"ENVIRONMENT": "production"})]
    jenkins_sync.sync_jenkins(conn, s, jk, force=True)
    # Running now: live stage progress on the Deployments tab.
    jk.b[dep["url"]].insert(0, jbuild(41, None, 0.2, sha="d1", building=True, who="Omar A",
                                      msgs=["HUM-1 split shift totals (#50)", "HUM-2 payroll column"],
                                      params={"ENVIRONMENT": "production", "API_TOKEN": "secret"}))
    jk.st["https://jenkins/job/x/41/"] = [{"name": "Build", "status": "SUCCESS", "duration_s": 60},
                                         {"name": "Deploy", "status": "IN_PROGRESS", "duration_s": 5}]
    jenkins_sync.sync_jenkins(conn, s, jk, force=True)
    d = conn.execute("SELECT * FROM deployments WHERE id='jenkins:deploy-web#41'").fetchone()
    assert d["state"] == "IN_PROGRESS" and json.loads(d["progress"]) == {"done": 1, "total": 2, "current": "Deploy"}
    b = conn.execute("SELECT parameters FROM jenkins_builds WHERE id='deploy-web#41'").fetchone()
    assert "API_TOKEN" not in json.loads(b["parameters"])                  # secrets never stored
    # It finishes: you hear your ticket shipped, the channel hears about production, notes get drafted.
    jk.b[dep["url"]][0] = jbuild(41, "SUCCESS", 0.2, sha="d1", who="Omar A",
                                 msgs=["HUM-1 split shift totals (#50)", "HUM-2 payroll column"],
                                 params={"ENVIRONMENT": "production"})
    posts.clear()
    jenkins_sync.sync_jenkins(conn, s, jk, force=True)
    d = conn.execute("SELECT * FROM deployments WHERE id='jenkins:deploy-web#41'").fetchone()
    assert d["state"] == "SUCCESS" and d["creator"] == "Omar A" and d["previous_sha"] == "d0"
    assert json.loads(d["tickets"]) == ["HUM-1", "HUM-2"] and json.loads(d["prs"]) == [50]
    assert "deployed" in kinds(conn) and "https://web" in [u for u, _ in posts]
    notes = {n["deployment_id"]: n for n in release_notes_list(conn)}       # earlier production deploys get notes too
    n41 = notes["jenkins:deploy-web#41"]
    assert "Split shift totals (HUM-1)" in n41["text"] and n41["environment"] == "production" and n41["tickets"] == 2
    jenkins_sync.sync_jenkins(conn, s, jk, force=True)
    assert len(release_notes_list(conn)) == len(notes)                      # drafted once


def test_github_junit_artifacts_and_branch_checks(env):
    s, conn, j, posts = env
    s.github.enabled = True
    s.github.repos = ["tcp-software/web-schedule"]

    def junit(*cases):
        x = "<testsuites><testsuite name='s'>" + "".join(
            f"<testcase classname='{c.rsplit('.', 1)[0]}' name='{c.rsplit('.', 1)[1]}'>"
            + ("<failure message='expected 8 got 7'/>" if bad else "") + "</testcase>" for c, bad in cases) + "</testsuite></testsuites>"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("results/junit.xml", x)
        return buf.getvalue()

    class GH(CodeGH):
        def run_artifacts(self, repo, run_id):
            return [{"name": "test-results", "archive_download_url": f"zip/{run_id}", "size_in_bytes": 100, "expired": False},
                    {"name": "build-output", "archive_download_url": "nope", "size_in_bytes": 100}]

        def download_artifact(self, url, max_bytes):
            rid = int(url.split("/")[1])
            return junit(("api.Clock.punch", rid == 2), ("api.Clock.edit", False))

        def branch_checks(self, repo, branch):
            return {"sha": "abc", "checks": [{"name": "Jenkins", "state": "success", "app": "status", "url": "u", "at": None}]}

    gh = GH()
    gh.runs = [run(1, "success", 10), run(2, "failure", 6), run(3, "success", 3)]
    for r in gh.runs:
        r["head_sha"] = "same" if r["id"] in (2, 3) else "other"
    codehealth.sync_ci(conn, s, gh, force=True)
    st = testhealth.ingest_github_tests(conn, s, gh)
    assert st == {"runs": 3, "with_tests": 3}
    f = testhealth.flaky_tests(conn, s)
    assert f["flaky"][0]["test"] == "api.Clock.punch" and f["flaky"][0]["last_error"] == "expected 8 got 7"
    assert testhealth.ingest_github_tests(conn, s, gh)["runs"] == 0           # read once
    ov = codehealth.ci_overview(conn, s)
    assert ov["branch_checks"][0]["checks"][0]["name"] == "Jenkins" and ov["branch_checks"][0]["branch"] == "main"


def test_junit_parser_refuses_entity_tricks():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><testsuite><testcase name="&b;"/></testsuite>'
    assert testhealth.junit_cases(evil) == []


def test_monthly_and_sprint_reports(env):
    s, conn, j, posts = env
    sprint = sp(5, state="closed", start_days=14, end_days=-1, name="HUM Sprint 41")
    sprint["completeDate"] = (NOW - timedelta(hours=20)).isoformat()
    j.issues["HUM-10"] = t("HUM-10", "Done", assignee="Ali Naeem", points=3, sprints=[sprint], created_h=400,
                           resolved_h=30, updated_h=30)
    j.issues["HUM-11"] = t("HUM-11", "In Progress", assignee="Sam Lee", points=5, sprints=[sprint], created_h=400, updated_h=5)
    sync_once(conn, s, j)
    m = reports.monthly(conn, s)
    assert m["kind"] == "monthly" and m["title"].startswith("Monthly report: ")
    assert "Bugs\n- " in m["text"] and "Delivery" in m["text"] and "Builds and tests" in m["text"]
    sr = reports.sprint_review(conn, s)
    assert sr["title"] == "Sprint review: HUM Sprint 41" and sr["unit"] == "points"
    assert sr["completed"] == 3 and sr["completion_pct"] == 38 and [c["key"] for c in sr["carried_over"]] == ["HUM-11"]
    assert "Carried over\n- HUM-11" in sr["text"]
    # Scheduled: last month's report on the first workday, and the closed sprint's review, to your chat only.
    first = datetime.now().replace(day=1, hour=10, minute=0)
    posts.clear()
    sent = jobs.scheduled_reports(conn, s, Alerter(s, conn), first)
    assert any(x.startswith("monthly:") for x in sent) and "sprint:HUM Sprint 41" in sent
    assert {u for u, _ in posts} == {"https://private"}
    assert jobs.scheduled_reports(conn, s, Alerter(s, conn), first) == []      # once


def test_endpoints(env):
    s, conn, j, posts = env
    for path in ("/api/jenkins", "/api/tests/flaky", "/api/release-notes/auto", "/api/report/monthly",
                 "/api/report/sprints", "/api/ci"):
        out = _extra_get(conn, s, path, {})
        assert out is not None and not (isinstance(out, dict) and out.get("error")), path
    assert "error" in _extra_get(conn, s, "/api/report/sprint", {})             # no sprints yet: a clear message


def test_jenkins_client_needs_settings():
    with pytest.raises(Exception, match="isn't configured"):
        JenkinsClient("", "", "")
    assert JenkinsClient.job_path("web schedule/feature%2FHUM-1") == "job/web%20schedule/job/feature%252FHUM-1"


# --------------------------------------------------------------------------- review regressions
def _run(conn, bid, pipeline, sha, h, failed=(), total=10, branch="main", result="SUCCESS"):
    cases = [(f"t.{n}", "FAILED", "x") for n in failed]
    testhealth.store_report(conn, bid, "jenkins", pipeline, sha, (NOW - timedelta(hours=h)).isoformat(), "u", cases,
                            branch, result, (total, len(cases), 0))


def test_broken_pr_branch_is_not_flaky(env):
    s, conn, j, posts = env
    for i, (br, bad) in enumerate([("main", False), ("PR-1", True), ("main", False), ("PR-1", True), ("main", False)]):
        _run(conn, f"b{i}", "web", f"sha{i}", 10 - i, failed=("A",) if bad else (), branch=br)
    f = testhealth.flaky_tests(conn, s)
    assert f["flaky"] == [] and [t["test"] for t in f["failing_now"]] == ["t.A"]


def test_partial_or_aborted_rerun_is_not_a_pass(env):
    s, conn, j, posts = env
    _run(conn, "r1", "web", "same", 5, failed=("A",), total=200)
    _run(conn, "r2", "web", "same", 4, total=30)                         # partial run: test probably didn't run
    _run(conn, "r3", "web", "same", 3, total=200, result="ABORTED")
    assert testhealth.flaky_tests(conn, s)["flaky"] == []
    _run(conn, "r4", "web", "same", 2, total=199)                        # a full re-run that passed
    assert [t["test"] for t in testhealth.flaky_tests(conn, s)["flaky"]] == ["t.A"]


def test_github_retry_attempts_are_read_separately(env):
    s, conn, j, posts = env
    s.github.enabled = True
    s.github.repos = ["tcp-software/web-schedule"]

    class GH(CodeGH):
        def run_artifacts(self, repo, run_id):
            return [{"name": "junit", "archive_download_url": "z", "size_in_bytes": 10}]

        def download_artifact(self, url, max_bytes):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                bad = "<failure message='flaky'/>" if self.attempt == 1 else ""
                z.writestr("r.xml", f"<testsuite><testcase classname='api' name='punch'>{bad}</testcase></testsuite>")
            return buf.getvalue()

    gh = GH()
    gh.attempt = 1
    gh.runs = [run(5, "failure", 2)]
    codehealth.sync_ci(conn, s, gh, force=True)
    testhealth.ingest_github_tests(conn, s, gh, Alerter(s, conn))      # called the way the sync loop calls it
    gh.attempt = 2
    gh.runs = [run(5, "success", 2, attempt=2)]
    codehealth.sync_ci(conn, s, gh, force=True)
    testhealth.ingest_github_tests(conn, s, gh, Alerter(s, conn))
    assert [t["test"] for t in testhealth.flaky_tests(conn, s)["flaky"]] == ["api.punch"]


def test_jenkins_client_never_follows_foreign_urls(monkeypatch):
    c = JenkinsClient("https://ci.acme.com", "ali", "tok")
    with pytest.raises(Exception, match="Refusing"):
        c.get("http://localhost:8080/job/x/api/json")
    calls = []

    class R:
        status_code, ok, headers, text = 200, True, {}, ""

        def json(self):
            return {"builds": [{"number": 7, "url": "http://localhost:8080/job/web/7/"}]}

    monkeypatch.setattr(c.s, "get", lambda url, **kw: calls.append(url) or R())
    b = c.builds("web schedule/main")
    assert calls[0].startswith("https://ci.acme.com/job/web%20schedule/job/main/api/json")
    assert b[0]["url"] == "https://ci.acme.com/job/web%20schedule/job/main/7/"


def test_stale_running_build_is_settled(env):
    s, conn, j, posts = env
    jk = FakeJenkins()
    job = {"name": "main", "full_name": "api/main", "url": "https://jenkins/job/api/job/main/", "class": "WorkflowJob",
           "branch": "main", "last_build": ms(1)}
    jk.jobs = [job]
    jk.b[job["url"]] = [jbuild(5, None, 1, building=True)]
    jenkins_sync.sync_jenkins(conn, s, jk, force=True)
    assert conn.execute("SELECT building FROM jenkins_builds").fetchone()[0] == 1
    jk.jobs = []                                                          # job dropped from the list
    jk.jobs_hidden = [job]
    jk._job = lambda name: job
    jk.b[job["url"]] = [jbuild(5, "SUCCESS", 1)]
    conn.execute("UPDATE jenkins_builds SET started=?", ((NOW - timedelta(hours=7)).isoformat(),)); conn.commit()
    jenkins_sync.sync_jenkins(conn, s, jk, force=True)
    assert tuple(conn.execute("SELECT building, result FROM jenkins_builds").fetchone()) == (0, "SUCCESS")


def test_sprint_reviews_only_for_your_team(env):
    s, conn, j, posts = env
    conn.execute("INSERT INTO sprints(id, name, state, start, end, complete) VALUES(77, 'Other team sprint', 'closed', ?, ?, ?)",
                 ((NOW - timedelta(days=14)).isoformat(), NOW.isoformat(), (NOW - timedelta(hours=5)).isoformat()))
    conn.commit()
    assert reports.sprints_list(conn) == []
    assert "error" in reports.sprint_review(conn, s)
    s.reports.monthly = False          # (a missed monthly report is sent late on purpose; not under test here)
    assert jobs.scheduled_reports(conn, s, Alerter(s, conn), datetime.now().replace(day=15, hour=10)) == []
