"""Insights, weekly report, triage, release notes."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from bugmonitor import insights, reports, triage
from bugmonitor.config import load_settings
from bugmonitor.db import connect, set_state
from bugmonitor.sync import sync_once
from test_monitor import history, issue
from test_team import TeamJira, t

NOW = datetime.now(timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "i.db"))
    s = load_settings(tmp_path / "none.toml")
    s.alerts.desktop = False
    s.team.jql = "TEAMSCOPE"
    s.team.members = [{"name": "Sam Lee", "github": "sam"}]
    j = TeamJira()
    j.myself = lambda: {"displayName": "Sam Lee", "accountId": "acc-SamLee"}
    # Two finished stories with known time in each status.
    for k, prog in (("HUM-1", 10), ("HUM-2", 30)):
        i = t(k, "Done", assignee="Sam Lee", created_h=100, updated_h=2, resolved_h=2, versions=["2026.11"], points=3)
        i["fields"]["timeoriginalestimate"] = 10 * 3600
        i["fields"]["timespent"] = prog * 3600
        j.issues[k] = i
        j.logs[k] = [history(int(k[-1]) * 10, 50, "To Do", "In Progress"), history(int(k[-1]) * 10 + 1, 50 - prog, "In Progress", "Done")]
    j.issues["HUM-3"] = issue("HUM-3", summary="Payroll export crashes on empty department", assignee=None, priority="Normal")
    j.issues["HUM-4"] = issue("HUM-4", "Done", summary="Payroll export crashes for empty department name", resolved_h=5,
                              updated_h=5, created_h=40)
    j.issues["HUM-4"]["fields"]["labels"] = ["customer"]
    j.issues["HUM-5"] = issue("HUM-5", summary="Kiosk login slow", assignee="Lena Ortiz", comps=("Kiosk",))
    s.jira.jql = "issuetype = Bug"
    conn = connect(s.db_path)
    sync_once(conn, s, j)
    return s, conn


def test_cycle_time_and_estimates(env):
    s, conn = env
    c = insights.cycle_time(conn, s, 30)
    st = {x["status"]: x for x in c["stages"]}
    assert st["In Progress"]["median_hours"] == pytest.approx(20, abs=0.1)   # 10 and 30 -> median 20
    assert st["To Do"]["median_hours"] == pytest.approx(50, abs=0.1)
    assert c["finished"] >= 3
    e = insights.estimate_accuracy(conn, s, 60)
    assert e["tickets"] == 2 and e["median_ratio"] == 2.0 and e["within_25pct"] == 50 and e["over"] == 1
    b = insights.bug_quality(conn, s, 60)
    assert b["escape_rate"] and b["escaped"][0]["key"] == "HUM-4"
    tp = insights.throughput(conn, s)
    assert sum(w["resolved"] for w in tp["weeks"]) == 3 and sum(w["points"] for w in tp["weeks"]) == 6


def test_pr_health(env):
    s, conn = env
    created = (NOW - timedelta(hours=30)).isoformat()
    det = {"conversation": [{"kind": "review", "state": "CHANGES_REQUESTED", "author": "lena", "at": (NOW - timedelta(hours=26)).isoformat()},
                            {"kind": "comment", "author": "sam", "at": (NOW - timedelta(hours=25)).isoformat()}]}
    conn.execute("INSERT INTO github_prs(id, repo, number, title, state, author, created, merged_at, additions, deletions, "
                 "relations, jira_keys, details) VALUES('x','o/r',1,'t','MERGED','sam',?,?,120,30,'[]','[\"HUM-1\"]',?)",
                 (created, (NOW - timedelta(hours=10)).isoformat(), json.dumps(det)))
    p = insights.pr_health(conn, s, 30)
    assert p["first_review_median_hours"] == pytest.approx(4, abs=0.1) and p["merge_median_hours"] == pytest.approx(20, abs=0.1)
    assert p["changes_requested_rate"] == 100 and p["sizes"][1]["n"] == 1


def test_weekly_report_text(env):
    s, conn = env
    rep = reports.weekly(conn, s, "Sam Lee")
    assert {r["key"] for r in rep["resolved"]} == {"HUM-1", "HUM-2", "HUM-4"} and rep["points"] == 6
    txt = reports.as_text(rep)
    assert txt.startswith("Sam Lee weekly report") and "(1 bug, 2 stories), 6 points" in txt
    assert "- HUM-1" in txt
    assert reports.weekly(conn, s, "team")["resolved"] and reports.weekly_card(conn, s)["body"][0]["text"].startswith("Team weekly")


def test_triage_duplicates_suggestions_release_notes(env):
    s, conn = env
    d = triage.duplicates(conn, s)
    assert d and {d[0]["key"], d[0]["match"]} == {"HUM-3", "HUM-4"}
    sug = {x["key"]: x for x in triage.suggestions(conn, s)}
    assert sug["HUM-3"]["suggest_assignee"] == "Sam Lee" and sug["HUM-3"]["suggest_priority"] == "Critical"
    rn = triage.release_notes(conn, s, "2026.11", "HUM")
    assert rn["tickets"] == 2 and rn["groups"]["Improvements"] == ["HUM-1", "HUM-2"]
    assert rn["text"].startswith("Release notes: HUM 2026.11") and rn["not_done"] == []
    conn.execute("UPDATE issues SET status_category='indeterminate' WHERE key='HUM-2'")
    assert triage.release_notes(conn, s, "2026.11", "HUM")["not_done"] == ["HUM-2"]
