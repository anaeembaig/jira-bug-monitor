"""v1.1: standup, start work, watch/snooze, actions, search, quality, SLA, hotspots, planning,
code health, timesheet export, and suggested worklogs."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from bugmonitor import codehealth, planning, quality, service, timelog, workflow
from bugmonitor.alerts import Alerter
from bugmonitor.config import Route, load_settings
from bugmonitor.dashboard import _extra_get
from bugmonitor.db import connect, set_state
from bugmonitor.github_sync import sync_github
from bugmonitor.sync import sync_once
from test_monitor import FakeGitHub, gh_pr, history, issue
from test_team import TeamJira, sp, t

NOW = datetime.now(timezone.utc)
REPO = "tcp-software/web-schedule"
GOOD_DESC = ("Steps to reproduce:\n1. Open the schedule in Chrome\n2. Drag a shift to Monday\n"
             "Expected: the shift moves. Actual: it snaps back and shows an error on the overtime rules page.")


def doc(text):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}]}
                                       for line in text.split("\n")]}


def bug(key, desc="", status="To Do", reporter=None, **kw):
    i = t(key, status, itype="Bug", **kw)
    i["fields"]["description"] = doc(desc) if desc else None
    if reporter:
        i["fields"]["reporter"] = {"displayName": reporter}
    return i


def blocked(key, **kw):
    i = t(key, "In Progress", **kw)
    i["fields"]["status"] = {"name": "Blocked", "statusCategory": {"key": "indeterminate"}}
    return i


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "v11.db"))
    s = load_settings(tmp_path / "none.toml")
    s.alerts.desktop = False
    s.alerts.teams_webhook = "https://private"
    s.team.jql = "TEAMSCOPE project in (HUM)"
    s.team.members = [{"name": "Ali Naeem", "github": "anaeem"}, {"name": "Sam Lee", "github": "sam"},
                      {"name": "Lena Ortiz", "github": "lena"}]
    s.timelog.workdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    posts = []

    class R:
        status_code = 202; text = ""

    monkeypatch.setattr("bugmonitor.alerts.requests.post", lambda url, json, timeout: posts.append((url, json)) or R())
    j = TeamJira()
    j.myself = lambda: {"displayName": "Ali Naeem", "accountId": "acc-AliNaeem"}
    return s, connect(s.db_path), j, posts


def kinds(conn):
    return [a["kind"] for a in service.list_alerts(conn)]


# --------------------------------------------------------------------------- quality and similar tickets
def test_quality_check_and_report(env):
    s, conn, j, posts = env
    j.issues["HUM-1"] = bug("HUM-1", GOOD_DESC, priority="High")
    j.issues["HUM-2"] = bug("HUM-2", "It is broken", reporter="Dana Cole")
    j.issues["HUM-3"] = t("HUM-3", "To Do", itype="Story")
    sync_once(conn, s, j)
    good = quality.quality_check(service.get_ticket(conn, "HUM-1"), s)
    assert good["applies"] and good["problems"] == [] and good["score"] == 100
    bad = quality.quality_check(service.get_ticket(conn, "HUM-2"), s)
    codes = {p["code"] for p in bad["problems"]}
    assert {"description", "steps", "expected", "environment"} <= codes
    assert "steps to reproduce" in bad["note"]
    assert not quality.quality_check(service.get_ticket(conn, "HUM-3"), s)["applies"]
    rep = quality.quality_report(conn, s)
    assert rep["checked"] == 2 and rep["incomplete"] == 1 and rep["items"][0]["key"] == "HUM-2"


def test_similar_tickets_feed_the_estimate(env):
    s, conn, j, posts = env
    for i, n in enumerate(range(10, 14)):
        k = f"HUM-{n}"
        j.issues[k] = bug(k, "overtime rules page error when dragging a shift", status="Done",
                          summary="Overtime rules error when dragging shift", created_h=100, updated_h=50, resolved_h=50)
        j.logs[k] = [history(1, 90, "To Do", "In Progress"), history(2, 90 - (i + 2), "In Progress", "Done")]
    j.issues["HUM-20"] = bug("HUM-20", "error on overtime rules after dragging a shift",
                             summary="Dragging shift shows overtime rules error")
    j.issues["HUM-21"] = bug("HUM-21", "login page logo is blurry", summary="Blurry logo on login")
    j.issues["HUM-22"] = t("HUM-22", "In Progress")          # so "In Progress" is known as an active status
    sync_once(conn, s, j)
    sim = quality.similar_tickets(conn, "HUM-20")
    assert [x["key"] for x in sim][:4] and all(x["key"].startswith("HUM-1") for x in sim)
    assert "HUM-21" not in [x["key"] for x in sim]
    est = service.get_ticket(conn, "HUM-20")["estimate"]
    assert est["similar"]["median_hours"] == pytest.approx(3.5, abs=0.6)
    assert "most similar finished tickets" in est["summary"]


# --------------------------------------------------------------------------- regression suspects and hotspots
def _pr_with_files(num, title, files, merged_h, keys_in_title=""):
    p = gh_pr(num, f"{keys_in_title} {title}".strip(), f"b{num}", author="sam", state="MERGED")
    p["mergedAt"] = (NOW - timedelta(hours=merged_h)).isoformat()
    p["files"] = {"nodes": [{"path": f, "additions": 3, "deletions": 1} for f in files]}
    return p


def test_regression_suspects_and_hotspots(env):
    s, conn, j, posts = env
    s.github.enabled = True
    j.issues["HUM-30"] = bug("HUM-30", "Overtime rules page shows wrong totals", summary="Overtime totals wrong",
                             comps=("Overtime",), created_h=2)
    j.issues["HUM-31"] = bug("HUM-31", GOOD_DESC, summary="Old overtime bug", status="Done", resolved_h=20,
                             comps=("Overtime",))
    sync_once(conn, s, j)
    gh = FakeGitHub()
    gh.prs["a"] = _pr_with_files(41, "Refactor overtime totals", ["src/overtime/totals.py", "src/overtime/rules.py"], 30)
    gh.prs["b"] = _pr_with_files(42, "Update login logo", ["web/login/logo.svg"], 20)
    gh.prs["c"] = _pr_with_files(43, "fix overtime rule", ["src/overtime/rules.py"], 25, "HUM-31")
    sync_github(conn, s, gh)
    sus = quality.regression_suspects(conn, s, "HUM-30")
    assert sus[0]["number"] == 41 and any("overtime" in r for r in sus[0]["reasons"])
    assert 42 not in [x["number"] for x in sus]
    hot = quality.hotspots(conn, s)
    assert hot["fix_prs"] == 1 and hot["files"][0]["path"] == "src/overtime/rules.py"
    assert hot["files"][0]["tickets"] == ["HUM-31"]


# --------------------------------------------------------------------------- SLA
def test_sla_timers_and_alerts(env):
    s, conn, j, posts = env
    s.sla.enabled = True
    s.sla.route = "web"
    s.routes = [Route(name="web", webhook="https://web", events=["sla"])]
    j.issues["HUM-40"] = bug("HUM-40", GOOD_DESC, priority="Critical", assignee="Ali Naeem", created_h=3)   # 4h response
    j.issues["HUM-41"] = bug("HUM-41", GOOD_DESC, priority="Critical", assignee=None, created_h=10)
    j.issues["HUM-42"] = bug("HUM-42", GOOD_DESC, priority="Low", assignee="Sam Lee", created_h=1)
    sync_once(conn, s, j)
    st = quality.sla_status(conn, service.get_ticket(conn, "HUM-41"), s)
    assert st["response"]["state"] == "breached" and st["state"] == "breached"
    al = Alerter(s, conn)
    assert quality.sla_alerts(conn, s, al) == 0                  # first run only records what's already late
    j.issues["HUM-43"] = bug("HUM-43", GOOD_DESC, priority="Critical", assignee="Ali Naeem", created_h=3.5, updated_h=0)
    sync_once(conn, s, j)
    posts.clear()
    assert quality.sla_alerts(conn, s, al) == 1                  # HUM-43: response due in 30 minutes
    assert kinds(conn)[0] == "sla_warning" and [u for u, _ in posts] == ["https://private"]
    set_state(conn, "sla:HUM-43:response:breached", "")
    conn.execute("UPDATE issues SET created=? WHERE key='HUM-43'", ((NOW - timedelta(hours=5)).isoformat(),))
    posts.clear()
    assert quality.sla_alerts(conn, s, al) == 1
    assert kinds(conn)[0] == "sla_breach" and sorted(u for u, _ in posts) == ["https://private", "https://web"]
    ov = quality.sla_overview(conn, s)
    assert ov["breached"] >= 2 and ov["items"][0]["sla"]["state"] == "breached"


# --------------------------------------------------------------------------- watch, snooze, actions, search
class WatchJira(TeamJira):
    def search(self, jql, extra_fields=None):
        if jql.startswith("key in ("):
            keys = jql[8:-1].split(",")
            yield from (self.issues[k] for k in keys if k in self.issues)
            return
        yield from super().search(jql, extra_fields)


def test_watch_list_alerts_and_snooze(env):
    s, conn, j, posts = env
    j = WatchJira()
    j.myself = lambda: {"displayName": "Ali Naeem", "accountId": "acc-AliNaeem"}
    j.issues["OPS-5"] = t("OPS-5", "To Do", itype="Task", assignee="Lena Ortiz")     # outside every scope
    workflow.watch(conn, "ops-5")
    sync_once(conn, s, j)
    assert conn.execute("SELECT in_watch FROM issues WHERE key='OPS-5'").fetchone()[0] == 1
    j.issues["OPS-5"] = t("OPS-5", "In Progress", itype="Task", assignee="Lena Ortiz", updated_h=0)
    posts.clear()
    sync_once(conn, s, j)
    assert kinds(conn)[0] == "watched" and [u for u, _ in posts] == ["https://private"]
    workflow.snooze(conn, "OPS-5", 2)
    j.issues["OPS-5"] = t("OPS-5", "Done", itype="Task", assignee="Lena Ortiz", updated_h=0, resolved_h=0)
    j.issues["OPS-5"]["fields"]["updated"] = NOW.isoformat()
    posts.clear()
    sync_once(conn, s, j)
    a = service.list_alerts(conn)[0]
    assert a["kind"] == "watched" and "snoozed" in a["channels"] and posts == []
    workflow.unwatch(conn, "OPS-5")
    sync_once(conn, s, j)
    assert conn.execute("SELECT in_watch FROM issues WHERE key='OPS-5'").fetchone()[0] == 0
    assert workflow.watches(conn) == []


class ActionJira:
    def __init__(self):
        self.calls = []

    def transitions(self, key):
        return [{"id": "21", "name": "Start progress", "to": {"name": "In Progress"}},
                {"id": "31", "name": "Done", "to": {"name": "Done"}}]

    def transition(self, key, tid):
        self.calls.append(("transition", key, tid))

    def assign(self, key, acc):
        self.calls.append(("assign", key, acc))

    def add_comment(self, key, text):
        self.calls.append(("comment", key, text))


def test_ticket_actions_are_opt_in_and_audited(env):
    s, conn, j, posts = env
    set_state(conn, "jira_me_id", "acc-me")
    c = ActionJira()
    assert "error" in workflow.ticket_action(conn, s, c, "HUM-1", "assign_me")
    assert len(workflow.ticket_action(conn, s, c, "HUM-1", "transitions")["transitions"]) == 2
    s.jira.allow_actions = True
    assert workflow.ticket_action(conn, s, c, "hum-1", "transition", "in progress")["status"] == "In Progress"
    assert "error" in workflow.ticket_action(conn, s, c, "HUM-1", "transition", "Closed")
    workflow.ticket_action(conn, s, c, "HUM-1", "assign_me")
    workflow.ticket_action(conn, s, c, "HUM-1", "comment", "Looking into it")
    assert c.calls == [("transition", "HUM-1", "21"), ("assign", "HUM-1", "acc-me"), ("comment", "HUM-1", "Looking into it")]
    assert [r[0] for r in conn.execute("SELECT action FROM audit ORDER BY id")] == ["jira_transition", "jira_assign", "jira_comment"]


def test_search_start_work_and_branch_names(env):
    s, conn, j, posts = env
    j.issues["HUM-50"] = bug("HUM-50", "Payroll export drops night differential", summary="Export missing column",
                             assignee="Ali Naeem")
    sync_once(conn, s, j)
    s.github.enabled = True
    gh = FakeGitHub()
    gh.prs["p"] = gh_pr(77, "HUM-12 payroll fix", "feature/HUM-12-payroll")
    gh.refs[REPO] = [{"name": f"feature/HUM-{n}-x", "target": {"oid": "abc", "committedDate": NOW.isoformat(),
                                                             "messageHeadline": "m", "author": {"user": {"login": "anaeem"}}},
                      "associatedPullRequests": {"nodes": []}} for n in (12, 13)]
    sync_github(conn, s, gh)
    res = workflow.search(conn, "night differential")
    assert res["groups"][0]["type"] == "Tickets" and res["groups"][0]["items"][0]["key"] == "HUM-50"
    assert workflow.search(conn, "#77")["groups"][0]["type"] == "Pull requests"
    brief = workflow.start_work(conn, s, "HUM-50", start_timer=True)
    assert brief["branch"] == "feature/HUM-50-export-missing-column"
    assert brief["git"][-1] == "git switch -c feature/HUM-50-export-missing-column origin/main"
    assert brief["timer"]["ok"] and service.active_work(conn)[0]["key"] == "HUM-50"
    assert "error" in workflow.start_work(conn, s, "NOPE-1")


def test_standup(env):
    s, conn, j, posts = env
    j.issues["HUM-60"] = t("HUM-60", "In Progress", assignee="Ali Naeem")
    j.issues["HUM-61"] = blocked("HUM-61", assignee="Ali Naeem")
    j.issues["HUM-62"] = t("HUM-62", "To Do", assignee="Ali Naeem")
    sync_once(conn, s, j)
    yday = datetime.now().astimezone() - timedelta(days=1)
    conn.execute("INSERT INTO worklogs(id, key, author, author_id, started, seconds, comment) VALUES('w1','HUM-60',"
                 "'Ali Naeem','acc-AliNaeem',?,7200,'wired up export')", (yday.replace(hour=10).isoformat(),))
    conn.commit()
    su = workflow.standup(conn, s)
    assert [x["key"] for x in su["yesterday"]] == ["HUM-60"] and "2h logged" in su["yesterday"][0]["detail"]
    assert [x["key"] for x in su["today"]] == ["HUM-60", "HUM-62"]          # blocked work is listed under blockers only
    assert [x["key"] for x in su["blockers"]] == ["HUM-61"]
    assert su["text"].startswith("Yesterday:") and "Blockers:\n- HUM-61" in su["text"]
    card = workflow.standup_card(conn, s)
    assert card["type"] == "AdaptiveCard"
    team = workflow.team_standup(conn, s)
    assert [p["person"] for p in team["people"]] == ["Ali Naeem", "Sam Lee", "Lena Ortiz"]


# --------------------------------------------------------------------------- planning
def test_forecast_capacity_and_readiness(env):
    s, conn, j, posts = env
    s.team.time_off = [{"name": "Sam Lee", "start": (datetime.now().date()).isoformat(),
                        "end": (datetime.now().date() + timedelta(days=1)).isoformat()}]
    s.team.hours_per_point = 2
    for n in range(20):          # steady throughput: one finished ticket per day
        k = f"HUM-{100 + n}"
        j.issues[k] = t(k, "Done", created_h=24 * (n + 3), updated_h=24 * (n + 1), resolved_h=24 * (n + 1) + 1,
                        versions=())
    sprint = sp(5, start_days=3, end_days=10)
    j.issues["HUM-200"] = t("HUM-200", "In Progress", assignee="Ali Naeem", points=3, sprints=[sprint],
                            versions=("2026.11",))
    j.issues["HUM-201"] = t("HUM-201", "To Do", assignee="Sam Lee", points=5, sprints=[sprint], versions=("2026.11",))
    j.issues["HUM-202"] = t("HUM-202", "Done", assignee="Sam Lee", points=2, sprints=[sprint], versions=("2026.11",),
                            resolved_h=5, updated_h=5)
    sync_once(conn, s, j)
    f = planning.forecast(conn, s)
    # Finished work has no points, so the sprint is forecast in tickets.
    assert f["history_ok"] and f["sprints"][0]["remaining"] == 2 and f["sprints"][0]["unit"] == "tickets"
    assert f["sprints"][0]["probability"] is not None
    assert f["releases"][0]["remaining"] == 2 and f["releases"][0]["probability"] is not None
    cap = planning.capacity(conn, s)
    sam = next(p for p in cap["people"] if p["person"] == "Sam Lee")
    ali = next(p for p in cap["people"] if p["person"] == "Ali Naeem")
    assert len(sam["days_off"]) == 2 and sam["available_hours"] == ali["available_hours"] - 12
    assert sam["assigned_hours"] == 10 and sam["tickets"][0]["basis"] == "5 points"
    rd = planning.readiness(conn, s)["releases"][0]
    checks = {c["name"]: c for c in rd["checks"]}
    assert not rd["ready"] and checks["All tickets done"]["detail"] == "1 of 3 done"
    assert checks["No open high-priority bugs"]["ok"] and checks["Deployed to a test environment"]["ok"] is None


# --------------------------------------------------------------------------- code health
class CodeGH(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.runs, self.sec = [], {"dependabot": [], "code_scanning": []}

    def default_branch(self, repo):
        return "main"

    def ci_runs(self, repo, branch, since):
        return [r for r in self.runs if r["head_branch"] == branch]

    def security_alerts(self, repo):
        return self.sec


def run(id_, concl, hours_ago, attempt=1, actor="sam", minutes=10):
    created = NOW - timedelta(hours=hours_ago)
    return {"id": id_, "name": "CI", "head_branch": "main", "head_sha": f"sha{id_}", "status": "completed",
            "conclusion": concl, "created_at": created.isoformat(), "run_started_at": created.isoformat(),
            "updated_at": (created + timedelta(minutes=minutes)).isoformat(), "run_attempt": attempt,
            "actor": {"login": actor}, "head_commit": {"author": {"name": actor}, "message": f"change {id_}"},
            "html_url": f"https://gh/run/{id_}"}


def test_ci_health_and_alerts(env):
    s, conn, j, posts = env
    s.github.enabled = True
    s.github.repos = [REPO]
    s.routes = [Route(name="web", webhook="https://web", events=["ci", "security"])]
    set_state(conn, "github_login", "anaeem")
    gh = CodeGH()
    gh.runs = [run(1, "success", 10), run(2, "success", 8, attempt=2)]
    codehealth.sync_ci(conn, s, gh, force=True)                 # first sync: history only
    gh.runs.append(run(3, "failure", 1, actor="anaeem"))
    posts.clear()
    st = codehealth.sync_ci(conn, s, gh, force=True)
    assert st["alerts"] == 1 and kinds(conn)[0] == "ci_failed"
    assert sorted(u for u, _ in posts) == ["https://private", "https://web"]      # you broke it, and the channel
    assert codehealth.sync_ci(conn, s, gh, force=True)["alerts"] == 0           # once
    ov = codehealth.ci_overview(conn, s)
    p = ov["pipelines"][0]
    assert p["failing"] and p["failed_in_a_row"] == 1 and p["success_rate"] == 67 and p["median_minutes"] == 10
    assert ov["flaky"][0]["attempt"] == 2 and ov["recent_failures"][0]["who"] == "anaeem"


def test_security_alerts_and_reviewer_suggestions(env):
    s, conn, j, posts = env
    s.github.enabled = True
    s.github.repos = [REPO]
    s.routes = [Route(name="web", webhook="https://web", events=["security"])]
    j.issues["HUM-70"] = t("HUM-70", "To Do", summary="Upgrade lodash for GHSA-1234")
    sync_once(conn, s, j)
    gh = CodeGH()
    dep = lambda n, sev: {"number": n, "state": "open", "html_url": f"https://gh/dep/{n}", "created_at": NOW.isoformat(),
                          "security_advisory": {"ghsa_id": f"GHSA-{n}234", "cve_id": "CVE-2026-1", "summary": "Prototype pollution",
                                                "severity": sev},
                          "security_vulnerability": {"package": {"name": "lodash"}, "severity": sev}}
    gh.sec = {"dependabot": [dep(1, "high")], "code_scanning": []}
    codehealth.sync_security(conn, s, gh, force=True)
    gh.sec["dependabot"].append(dep(2, "critical"))
    gh.sec["code_scanning"] = [{"number": 9, "state": "open", "html_url": "https://gh/cs/9", "created_at": NOW.isoformat(),
                                "rule": {"id": "js/xss", "description": "Reflected XSS", "security_severity_level": "medium"},
                                "most_recent_instance": {"location": {"path": "web/app.js"}}}]
    posts.clear()
    st = codehealth.sync_security(conn, s, gh, force=True)
    assert st["alerts"] == 1 and kinds(conn)[0] == "security"          # critical yes; medium is below "high"
    ov = codehealth.security_overview(conn, s)
    assert ov["open"] == 3 and ov["by_severity"]["critical"] == 1 and ov["items"][0]["severity"] == "critical"
    assert next(i for i in ov["items"] if i["number"] == 1)["jira_keys"] == ["HUM-70"]
    gh.sec = {"dependabot": [], "code_scanning": []}
    codehealth.sync_security(conn, s, gh, force=True)
    assert codehealth.security_overview(conn, s)["open"] == 0                  # fixed alerts disappear

    # Reviewer suggestions: Lena changed and reviewed these files before; Sam has a full queue.
    past = gh_pr(80, "Overtime totals", "b80", author="lena", state="MERGED")
    past["files"] = {"nodes": [{"path": "src/overtime/totals.py"}]}
    past2 = gh_pr(81, "Overtime rules", "b81", author="sam", state="MERGED")
    past2["files"] = {"nodes": [{"path": "src/overtime/totals.py"}]}
    mine = gh_pr(90, "Fix totals", "b90", author="anaeem")
    mine["files"] = {"nodes": [{"path": "src/overtime/totals.py"}]}
    busy = [gh_pr(91 + i, "x", f"x{i}", author="lena", requested=("sam",)) for i in range(3)]
    gh.prs = {p["id"]: p for p in [past, past2, mine] + busy}
    sync_github(conn, s, gh)
    sug = codehealth.reviewer_suggestions(conn, s, REPO, 90)["suggestions"]
    assert sug[0]["login"] == "lena" and any("waiting" in r for r in sug[1]["reasons"])
    assert codehealth.my_review_suggestions(conn, s)[0]["number"] == 90


# --------------------------------------------------------------------------- time
def test_timesheet_export_and_suggestions(env):
    s, conn, j, posts = env
    j.issues["HUM-80"] = t("HUM-80", "In Progress", assignee="Ali Naeem")
    j.issues["HUM-81"] = t("HUM-81", "In Progress", assignee="Ali Naeem")
    sync_once(conn, s, j)
    day = datetime.now().astimezone() - timedelta(days=1)
    conn.executemany("INSERT INTO worklogs(id, key, author, author_id, started, seconds, comment) VALUES(?,?,?,?,?,?,?)",
                     [("a", "HUM-80", "Ali Naeem", "acc-AliNaeem", day.replace(hour=9).isoformat(), 3600 * 3, "export"),
                      ("b", "HUM-80", "Sam Lee", "acc-SamLee", day.replace(hour=9).isoformat(), 3600, "")])
    # Claude tracked 2h on HUM-81 yesterday; nothing logged.
    conn.executemany("INSERT INTO work_log(key, ts, action, minutes) VALUES(?,?,?,?)",
                     [("HUM-81", day.replace(hour=13).astimezone(timezone.utc).isoformat(), "note", 120)])
    conn.commit()
    mine = timelog.export_csv(conn, s, 7).splitlines()
    assert mine[0].startswith("Date,Person,Ticket") and len(mine) == 3 and mine[-1].startswith("Total") and ",3," in mine[1]
    team = timelog.export_csv(conn, s, 7, team=True).splitlines()
    assert len(team) == 4 and team[-1].split(",")[6] == "4"
    sug = timelog.suggestions(conn, s, 3)
    hit = next(x for x in sug if x["key"] == "HUM-81" and x["date"] == day.date().isoformat())
    assert hit["hours"] == 2 and "Claude" in hit["reason"]
    timelog.dismiss_suggestion(conn, hit["date"], "HUM-81")
    assert not [x for x in timelog.suggestions(conn, s, 3) if x["key"] == "HUM-81" and x["date"] == hit["date"]]


def test_dashboard_endpoints_answer(env):
    s, conn, j, posts = env
    j.issues["HUM-90"] = bug("HUM-90", GOOD_DESC)
    sync_once(conn, s, j)
    for path in ("/api/standup", "/api/standup/team", "/api/watches", "/api/quality", "/api/sla", "/api/hotspots",
                 "/api/forecast", "/api/capacity", "/api/readiness", "/api/ci", "/api/security", "/api/reviewers/mine",
                 "/api/timelog/suggestions", "/api/insights/HUM-90", "/api/start/HUM-90"):
        out = _extra_get(conn, s, path, {"q": "HUM"})
        assert out is not None and not (isinstance(out, dict) and out.get("error")), path
    body, ctype, name = _extra_get(conn, s, "/api/timelog/export.csv", {"days": "7"})
    assert ctype.startswith("text/csv") and name.endswith(".csv")
    assert json.dumps(_extra_get(conn, s, "/api/search", {"q": "HUM-90"}))


# --------------------------------------------------------------------------- review regressions
def test_watching_a_new_ticket_does_not_replay_its_comments(env):
    s, conn, j, posts = env
    j = WatchJira()
    j.myself = lambda: {"displayName": "Ali Naeem", "accountId": "acc-AliNaeem"}
    i = t("OPS-7", "To Do", itype="Task", assignee="Lena Ortiz")
    i["fields"]["comment"] = {"comments": [{"id": str(n), "author": {"displayName": "Sam Lee", "accountId": "acc-SamLee"},
                                            "created": NOW.isoformat(), "body": doc(f"note {n}")} for n in range(5)]}
    j.issues["OPS-7"] = i
    sync_once(conn, s, j)                       # first sync: quiet
    workflow.watch(conn, "OPS-7")
    j.issues.pop("OPS-7")                       # leave every scope, so the watch pass stores it fresh
    conn.execute("DELETE FROM issues WHERE key='OPS-7'"); conn.execute("DELETE FROM jira_comments"); conn.commit()
    j.issues["OPS-7"] = i
    sync_once(conn, s, j)
    assert "watched" not in kinds(conn)


def test_one_bad_watched_key_does_not_block_the_rest(env):
    s, conn, j, posts = env

    class Picky(WatchJira):
        def search(self, jql, extra_fields=None):
            if jql.startswith("key in (") and "NOPE-1" in jql:
                raise RuntimeError("400 An issue with key 'NOPE-1' does not exist")
            if jql == "key = NOPE-1":
                raise RuntimeError("400 does not exist")
            if jql.startswith("key = "):
                k = jql[6:]
                yield from ([self.issues[k]] if k in self.issues else [])
                return
            yield from super().search(jql, extra_fields)

    j = Picky()
    j.myself = lambda: {"displayName": "Ali Naeem", "accountId": "acc-AliNaeem"}
    j.issues["OPS-8"] = t("OPS-8", "To Do", itype="Task")
    workflow.watch(conn, "OPS-8"); workflow.watch(conn, "NOPE-1")
    sync_once(conn, s, j)
    assert conn.execute("SELECT in_watch FROM issues WHERE key='OPS-8'").fetchone()[0] == 1
    bad = next(w for w in workflow.watches(conn) if w["key"] == "NOPE-1")
    assert bad["error"] and "Can't load" in bad["summary"]


def test_watched_bug_gets_one_alert_per_change(env):
    s, conn, j, posts = env
    j.issues["HUM-95"] = bug("HUM-95", GOOD_DESC, status="Done", assignee="Sam Lee", resolved_h=5)
    sync_once(conn, s, j)
    workflow.watch(conn, "HUM-95")
    j.issues["HUM-95"] = bug("HUM-95", GOOD_DESC, status="In Progress", assignee="Sam Lee", updated_h=0)
    j.issues["HUM-95"]["fields"]["updated"] = NOW.isoformat()
    sync_once(conn, s, j)
    assert [k for k in kinds(conn) if k in ("reopened", "watched")] == ["reopened"]


def test_security_fetch_failure_keeps_alerts(env, monkeypatch):
    s, conn, j, posts = env
    s.github.enabled = True
    s.github.repos = [REPO]
    gh = CodeGH()
    gh.sec = {"dependabot": [{"number": 1, "state": "open", "html_url": "u", "created_at": NOW.isoformat(),
                              "security_advisory": {"summary": "x", "severity": "critical"},
                              "security_vulnerability": {"package": {"name": "p"}, "severity": "critical"}}], "code_scanning": []}
    codehealth.sync_security(conn, s, gh, force=True)
    gh.security_alerts = lambda repo: (_ for _ in ()).throw(RuntimeError("502"))
    codehealth.sync_security(conn, s, gh, force=True)
    assert codehealth.security_overview(conn, s)["open"] == 1


def test_csv_cells_cannot_become_formulas(env):
    s, conn, j, posts = env
    set_state(conn, "jira_me_id", "acc-AliNaeem")
    conn.execute("INSERT INTO worklogs(id, key, author, author_id, started, seconds, comment) VALUES('z','HUM-1','Ali Naeem',"
                 "'acc-AliNaeem',?,3600,'=HYPERLINK(\"http://x\")')", (NOW.isoformat(),))
    conn.commit()
    row = timelog.export_csv(conn, s, 3).splitlines()[1]
    assert "'=HYPERLINK" in row


def test_no_workdays_never_hangs(env):
    s, conn, j, posts = env
    s.timelog.workdays = []
    assert planning.capacity(conn, s)["workdays"] == 0
    assert planning.forecast(conn, s)["sprints"] == []


def test_ci_alerts_reach_project_limited_channels(env):
    s, conn, j, posts = env
    s.routes = [Route(name="web", webhook="https://web", projects=["HUM"], events=["ci"])]
    Alerter(s, conn).raise_alert({"key": "web-core:main", "summary": "CI failed", "url": "u", "priority": None, "project": ""},
                                 "ci_failed", "", personal=False)
    assert [u for u, _ in posts] == ["https://web"]
