"""Team scope sync and team views."""
from datetime import datetime, timedelta, timezone

import pytest

from bugmonitor import service, team
from bugmonitor.config import load_settings
from bugmonitor.db import connect
from bugmonitor.sync import sync_once
from test_monitor import FakeJira, history, issue, ts

NOW = datetime.now(timezone.utc)
SPRINT_F, POINTS_F = "customfield_10020", "customfield_10016"


class TeamJira(FakeJira):
    def search(self, jql, extra_fields=None):
        if "TEAMSCOPE" in jql:
            self.queries.append(jql)
            yield from self.issues.values()
            return
        yield from super().search(jql, extra_fields)

    def fields(self):
        return [{"id": SPRINT_F, "name": "Sprint", "schema": {"custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
                {"id": POINTS_F, "name": "Story point estimate", "schema": {}}]

    def versions(self, project):
        return [{"id": 1, "name": "2026.11", "released": False, "archived": False,
                 "releaseDate": (NOW + timedelta(days=5)).date().isoformat()}] if project == "HUM" else []


def sp(id_, state="active", start_days=6, end_days=8, name=None):
    return {"id": id_, "name": name or f"Sprint {id_}", "state": state, "boardId": 7,
            "startDate": (NOW - timedelta(days=start_days)).isoformat(),
            "endDate": (NOW + timedelta(days=end_days)).isoformat(), "goal": "Ship overtime fixes"}


def t(key, status="To Do", assignee="Sam Lee", itype="Story", points=None, sprints=(), versions=(), **kw):
    i = issue(key, status, assignee=assignee, itype=itype, **kw)
    i["fields"][SPRINT_F] = list(sprints)
    i["fields"][POINTS_F] = points
    i["fields"]["fixVersions"] = [{"name": v} for v in versions]
    return i


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "t.db"))
    s = load_settings(tmp_path / "none.toml")
    s.alerts.desktop = False
    s.team.jql = "TEAMSCOPE project in (HUM)"
    s.team.members = [{"name": "Sam Lee", "github": "sam"}, {"name": "Ali Naeem", "github": "anaeem"},
                      {"name": "Lena Ortiz"}]
    return s, connect(s.db_path), TeamJira()


def test_team_board_sprint_releases(env):
    s, conn, j = env
    j.myself = lambda: {"displayName": "Ali Naeem", "accountId": "acc-AliNaeem"}
    cur, old = sp(11), sp(10, state="closed", start_days=20, end_days=-7)
    j.issues["HUM-1"] = t("HUM-1", "In Progress", points=5, sprints=[old, cur], created_h=24 * 15, updated_h=24 * 7,
                          versions=["2026.11"])
    j.logs["HUM-1"] = [history(1, 24 * 7, "To Do", "In Progress")]        # 7 days in progress -> stuck (limit 5)
    j.issues["HUM-2"] = t("HUM-2", "Done", points=3, sprints=[cur], created_h=24 * 8, updated_h=24, resolved_h=24,
                          versions=["2026.11"])
    j.issues["HUM-3"] = t("HUM-3", "To Do", assignee="Ali Naeem", points=2, sprints=[cur], created_h=24,
                          versions=["2026.11"])                            # added after sprint start
    j.issues["HUM-4"] = t("HUM-4", "In QA", assignee=None, itype="Bug", priority="Critical", created_h=48,
                          versions=["2026.11"])
    st = sync_once(conn, s, j)
    assert st["team"] == 4 and st["alerts"] == 0
    assert any("TEAMSCOPE" in q for q in j.queries)

    b = team.board(conn, s)
    people = [r["person"] for r in b["rows"]]
    assert people[:3] == ["Sam Lee", "Ali Naeem", "Lena Ortiz"] and people[-1] == "Unassigned"
    sam = b["rows"][0]
    assert sam["wip"] == 1 and sam["stuck"] == 1 and sam["points_open"] == 5 and sam["done_7d"] == 1
    assert b["statuses"] == ["To Do", "In Progress", "In QA", "Done"]

    spr = team.sprint(conn, s)["sprints"]
    assert len(spr) == 1 and spr[0]["name"] == "Sprint 11" and spr[0]["unit"] == "points"
    assert spr[0]["total"] == 10 and spr[0]["by_category"]["done"] == 3 and spr[0]["done_pct"] == 30
    assert [x["key"] for x in spr[0]["added_after_start"]] == ["HUM-3"]
    assert [x["key"] for x in spr[0]["carried_over"]] == ["HUM-1"]
    assert spr[0]["burnup"][-1] == {"date": NOW.date().isoformat(), "scope": 10, "done": 3}
    assert spr[0]["burnup"][0]["scope"] == 8   # before HUM-3 was added

    rel = team.releases(conn, s)["releases"]
    assert rel[0]["name"] == "2026.11" and rel[0]["total"] == 4 and rel[0]["done"] == 1 and rel[0]["progress"] == 25
    assert [x["key"] for x in rel[0]["blockers"]] == ["HUM-4"] and 4 < rel[0]["days_left"] <= 6

    qa = team.qa(conn, s)["tickets"]
    assert [x["key"] for x in qa] == ["HUM-4"] and qa[0]["testable"] == "No PR linked"
    stuck = team.stuck(conn, s)["items"]
    assert [x["key"] for x in stuck] == ["HUM-1"] and stuck[0]["days"] >= 7

    # Stories in the team scope stay out of bug views.
    assert {i["key"] for i in service.list_issues(conn)} == {"HUM-4"}


def test_team_scope_cleanup_and_review_queue(env):
    from bugmonitor.github_sync import sync_github
    from test_monitor import FakeGitHub, gh_pr
    s, conn, j = env
    j.issues["HUM-1"] = t("HUM-1")
    j.issues["HUM-2"] = t("HUM-2")
    sync_once(conn, s, j)
    del j.issues["HUM-2"]
    conn.execute("DELETE FROM sync_state WHERE k='team_full_date'")   # simulate the next day's full pass
    sync_once(conn, s, j)
    assert {r[0] for r in conn.execute("SELECT key FROM issues")} == {"HUM-1"}

    s.github.enabled = True
    gh = FakeGitHub()
    gh.search_prs = lambda q: ([gh_pr(70, "HUM-1 grid", "hum-1", author="sam", requested=["lena"])]
                               if "author:sam" in q else [])
    sync_github(conn, s, gh)
    pr = service.list_prs(conn)[0]
    assert pr["relations"] == ["team"]
    conn.execute("UPDATE github_prs SET created=?", ((NOW - timedelta(hours=30)).isoformat(),))
    q = team.reviews(conn, s)
    assert q["queue"][0]["number"] == 70 and q["queue"][0]["overdue"] and q["load"] == [{"reviewer": "lena", "pending": 1}]
    assert [i["type"] for i in team.stuck(conn, s)["items"]] == ["pr"]


def test_mentions_alert_once(env):
    s, conn, j = env
    j.myself = lambda: {"displayName": "Ali Naeem", "accountId": "acc-AliNaeem"}
    s.jira.fetch_comments = True
    i = t("HUM-9", assignee="Sam Lee")
    j.issues["HUM-9"] = i
    sync_once(conn, s, j)
    i = t("HUM-9", assignee="Sam Lee", updated_h=0.1)
    i["fields"]["comment"] = {"comments": [{
        "id": "c1", "created": ts(0.1), "author": {"displayName": "Sam Lee", "accountId": "acc-SamLee"},
        "body": {"type": "doc", "content": [{"type": "paragraph", "content": [
            {"type": "mention", "attrs": {"id": "acc-AliNaeem", "text": "@Ali Naeem"}},
            {"type": "text", "text": " can you check the payroll export?"}]}]}}]}
    j.issues["HUM-9"] = i
    sync_once(conn, s, j)
    a = service.list_alerts(conn)
    assert [x["kind"] for x in a] == ["mentioned"] and "can you check the payroll export" in a[0]["message"]
    row = conn.execute("SELECT body, mentions FROM jira_comments WHERE id='c1'").fetchone()
    assert row["body"].startswith("@Ali Naeem can you check") and "acc-AliNaeem" in row["mentions"]
    i2 = dict(i); i2["fields"] = dict(i["fields"], updated=ts(0.05))
    j.issues["HUM-9"] = i2
    sync_once(conn, s, j)
    assert len(service.list_alerts(conn)) == 1          # the same comment never alerts twice
