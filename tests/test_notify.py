"""Team channel routing, mentions, quiet hours, escalations, stuck reminders, digests."""
from datetime import datetime, timedelta, timezone

import pytest

from bugmonitor import jobs, service
from bugmonitor.alerts import Alerter, in_quiet_hours
from bugmonitor.config import Escalation, Route, load_settings
from bugmonitor.db import connect
from bugmonitor.sync import sync_once
from test_monitor import history, issue
from test_team import TeamJira, t

NOW = datetime.now(timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "n.db"))
    s = load_settings(tmp_path / "none.toml")
    s.alerts.desktop = False
    s.alerts.teams_webhook = "https://private"
    s.alerts.external_min_priority = "Normal"
    s.team.members = [{"name": "Sam Lee", "github": "sam", "email": "sam@acme.com"}]
    s.routes = [Route(name="web", webhook="https://web", projects=["WEB"], min_priority="Major", digest=True),
                Route(name="api", webhook="https://api", projects=["API"])]
    posts = []

    class R:
        status_code = 202; text = ""

    monkeypatch.setattr("bugmonitor.alerts.requests.post", lambda url, json, timeout: posts.append((url, json)) or R())
    return s, connect(s.db_path), posts


def card(post):
    return post[1]["attachments"][0]["content"]


def test_routes_by_project_priority_and_mentions(env):
    s, conn, posts = env
    al = Alerter(s, conn)
    al.raise_alert({"key": "WEB-1", "project": "WEB", "summary": "x", "priority": "Critical",
                    "assignee": "Sam Lee", "url": "u"}, "new_bug")
    urls = [u for u, _ in posts]
    assert urls == ["https://private", "https://web"]
    web = card(posts[1])
    assert web["msteams"]["entities"][0]["mentioned"] == {"id": "sam@acme.com", "name": "Sam Lee"}
    posts.clear()
    al.raise_alert({"key": "WEB-2", "project": "WEB", "summary": "x", "priority": "Normal", "url": "u"}, "new_bug")
    assert [u for u, _ in posts] == ["https://private"]          # below the web channel's Major threshold
    posts.clear()
    al.raise_alert({"key": "API-3", "project": "API", "summary": "x", "priority": "Minor", "url": "u"}, "new_bug")
    assert posts == []                                            # below Normal everywhere
    al.raise_alert({"key": "API-4", "project": "API", "summary": "x", "priority": "Normal", "url": "u"},
                   "my_status", external=True)
    assert [u for u, _ in posts] == ["https://private"]          # personal events never reach channels


def test_quiet_hours_hold_then_summarize(env, monkeypatch):
    s, conn, posts = env
    s.notify.quiet_hours = "20:00-08:00"
    assert in_quiet_hours(s.notify, datetime(2026, 10, 22, 23, 0)) and in_quiet_hours(s.notify, datetime(2026, 10, 22, 7, 59))
    assert not in_quiet_hours(s.notify, datetime(2026, 10, 22, 8, 0))
    s.notify.quiet_weekends = True
    assert in_quiet_hours(s.notify, datetime(2026, 10, 24, 12, 0))   # Saturday
    s.notify.quiet_weekends = False

    monkeypatch.setattr("bugmonitor.alerts.in_quiet_hours", lambda n, now=None: True)
    al = Alerter(s, conn)
    al.raise_alert({"key": "WEB-1", "project": "WEB", "summary": "a", "priority": "Normal", "url": "u"}, "new_bug")
    al.raise_alert({"key": "WEB-2", "project": "WEB", "summary": "b", "priority": "Normal", "url": "u"}, "my_status",
                   external=True)
    al.raise_alert({"key": "WEB-3", "project": "WEB", "summary": "c", "priority": "Blocker", "url": "u"}, "new_bug")
    assert [u for u, _ in posts] == ["https://private", "https://web"]   # only the urgent Blocker went out
    assert any("held for quiet hours" in c for c in service.list_alerts(conn)[1]["channels"])
    assert al.flush_outbox() == 0                                        # still quiet
    monkeypatch.setattr("bugmonitor.alerts.in_quiet_hours", lambda n, now=None: False)
    posts.clear()
    assert al.flush_outbox() == 1
    assert [u for u, _ in posts] == ["https://private"]
    assert "2 updates" in card(posts[0])["body"][0]["text"]
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_escalations_fire_once(env):
    s, conn, posts = env
    s.escalations = [Escalation(name="Critical unassigned", min_priority="Critical", unassigned_hours=2, route="web"),
                     Escalation(name="Major open 3 days", min_priority="Major", open_hours=72)]
    s.jira.jql = "issuetype = Bug"
    j = TeamJira()
    j.issues["WEB-1"] = issue("WEB-1", priority="Critical", assignee=None, created_h=3)
    j.issues["WEB-2"] = issue("WEB-2", priority="Major", created_h=80)
    j.issues["WEB-3"] = issue("WEB-3", priority="Critical", assignee=None, created_h=1)   # too new
    sync_once(conn, s, j)
    posts.clear()
    assert jobs.escalations(conn, s, Alerter(s, conn)) == 2
    assert sorted(u for u, _ in posts) == ["https://private", "https://web"]
    assert "unassigned for 3 hours" in card([p for p in posts if p[0] == "https://web"][0])["body"][2]["text"]
    assert jobs.escalations(conn, s, Alerter(s, conn)) == 0           # never twice


def test_stuck_reminders_once_and_digests(env):
    s, conn, posts = env
    s.team.jql = "TEAMSCOPE"
    j = TeamJira()
    j.myself = lambda: {"displayName": "Ali Naeem", "accountId": "acc-AliNaeem"}
    j.issues["WEB-5"] = t("WEB-5", "In Progress", assignee="Ali Naeem", created_h=24 * 9, updated_h=24 * 8)
    j.logs["WEB-5"] = [history(1, 24 * 8, "To Do", "In Progress")]
    j.issues["WEB-6"] = t("WEB-6", "In Progress", assignee="Sam Lee", created_h=24 * 9, updated_h=24 * 8)
    j.logs["WEB-6"] = [history(2, 24 * 8, "To Do", "In Progress")]
    sync_once(conn, s, j)
    posts.clear()
    assert jobs.stuck_reminders(conn, s, Alerter(s, conn)) == 2
    urls = sorted(u for u, _ in posts)
    assert urls == ["https://private", "https://web", "https://web"]    # mine to me + channel; Sam's to channel
    assert jobs.stuck_reminders(conn, s, Alerter(s, conn)) == 0

    s.notify.digest_time = "09:30"
    posts.clear()
    early = datetime(2026, 10, 22, 9, 0)
    assert jobs.digests(conn, s, Alerter(s, conn), early) == []
    at = datetime(2026, 10, 22, 9, 31)
    assert jobs.digests(conn, s, Alerter(s, conn), at) == ["personal", "team:web"]
    texts = [b["text"] for _, p in posts for b in p["attachments"][0]["content"]["body"]]
    assert any("Your tickets" in x for x in texts) and any("Work in progress" in x for x in texts)
    assert any("Stuck" in x for x in texts)
    assert jobs.digests(conn, s, Alerter(s, conn), at) == []            # once a day


def test_sync_failure_alert_after_three(env, monkeypatch):
    from bugmonitor import sync as sync_mod
    s, conn, posts = env
    calls = {"n": 0}

    class Stop(Exception):
        pass

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("Jira 401")

    class Ev:
        def is_set(self):
            return calls["n"] >= 4

        def wait(self, t):
            pass

    monkeypatch.setattr(sync_mod, "sync_once", boom)
    sync_mod.run_forever(conn, s, object(), stop_event=Ev())
    kinds = [a["kind"] for a in service.list_alerts(conn)]
    assert kinds == ["sync_failing"]
    assert any(u == "https://private" for u, _ in posts)
