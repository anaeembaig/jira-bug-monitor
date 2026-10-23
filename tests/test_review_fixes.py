"""Regression tests for alert de-duplication, scope re-reads, held messages, and live settings."""
import dataclasses

import pytest

from bugmonitor import service
from bugmonitor.alerts import Alerter
from bugmonitor.config import load_settings
from bugmonitor.db import connect, set_state
from bugmonitor.sync import sync_once
from test_monitor import history, issue
from test_team import TeamJira, t


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "r.db"))
    s = load_settings(tmp_path / "none.toml")
    s.alerts.desktop = False
    s.alerts.teams_webhook = "https://private"
    posts = []

    class R:
        status_code = 202; text = ""

    monkeypatch.setattr("bugmonitor.alerts.requests.post", lambda url, json, timeout: posts.append((url, json)) or R())
    j = TeamJira()
    j.myself = lambda: {"displayName": "Ali Naeem", "accountId": "acc-AliNaeem"}
    j.issues["HUM-0"] = issue("HUM-0", "To Do", assignee="Sam Lee")
    conn = connect(s.db_path)
    sync_once(conn, s, j)                      # first sync: quiet
    posts.clear()
    return s, conn, j, posts


def alerts(conn):
    return [(a["kind"], a["key"]) for a in service.list_alerts(conn)]


def test_new_bug_assigned_to_me_sends_one_private_message(env):
    s, conn, j, posts = env
    j.issues["HUM-1"] = issue("HUM-1", "To Do", priority="High", assignee="Ali Naeem", updated_h=0)
    sync_once(conn, s, j)
    kinds = sorted(k for k, key in alerts(conn) if key == "HUM-1")
    assert kinds == ["assigned_to_me", "new_bug"]
    assert [u for u, _ in posts] == ["https://private"]


def test_reopened_my_ticket_does_not_also_send_status_change(env):
    s, conn, j, posts = env
    j.issues["HUM-2"] = issue("HUM-2", "Done", assignee="Ali Naeem", updated_h=2, resolved_h=2)
    sync_once(conn, s, j)
    posts.clear()
    j.issues["HUM-2"] = issue("HUM-2", "In Progress", assignee="Ali Naeem", updated_h=0)
    j.logs["HUM-2"] = [history(1, 0.1, "Done", "In Progress")]
    sync_once(conn, s, j)
    kinds = [k for k, key in alerts(conn) if key == "HUM-2"]
    assert "reopened" in kinds and "my_status" not in kinds


def test_daily_full_pass_drops_and_readds_bug_scope(env):
    s, conn, j, posts = env
    s.team.jql = "TEAMSCOPE project in (HUM)"
    j.issues["HUM-3"] = t("HUM-3", "To Do", assignee="Sam Lee", itype="Bug", priority="High", updated_h=0)
    sync_once(conn, s, j)
    assert conn.execute("SELECT in_bugs FROM issues WHERE key='HUM-3'").fetchone()[0] == 1

    # Retyped to a Story: an incremental sync can't tell, the next day's full pass can.
    j.issues["HUM-3"]["fields"]["issuetype"] = {"name": "Story"}
    set_state(conn, "bugs_full_date", "2000-01-01")
    conn.commit()
    sync_once(conn, s, j)
    assert conn.execute("SELECT in_bugs, in_team FROM issues WHERE key='HUM-3'").fetchone()[:] == (0, 1)

    # Back to a Bug without a newer `updated`: it re-enters the bug scope and alerts as new.
    j.issues["HUM-3"]["fields"]["issuetype"] = {"name": "Bug"}
    before = len(alerts(conn))
    sync_once(conn, s, j)
    assert conn.execute("SELECT in_bugs FROM issues WHERE key='HUM-3'").fetchone()[0] == 1
    assert [k for k, key in alerts(conn)[: len(alerts(conn)) - before]] == ["new_bug"]


def test_held_messages_survive_a_failed_delivery(env, monkeypatch):
    s, conn, j, posts = env
    monkeypatch.setattr("bugmonitor.alerts.in_quiet_hours", lambda n, now=None: True)
    al = Alerter(s, conn)
    al.raise_alert({"key": "HUM-9", "summary": "x", "priority": "Normal", "url": "u"}, "my_status", external=True)
    monkeypatch.setattr("bugmonitor.alerts.in_quiet_hours", lambda n, now=None: False)
    monkeypatch.setattr(al, "post_card", lambda url, card: False)
    assert al.flush_outbox() == 0
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1   # kept for next time
    monkeypatch.undo()
    monkeypatch.setattr("bugmonitor.alerts.in_quiet_hours", lambda n, now=None: False)
    monkeypatch.setattr(al, "post_card", lambda url, card: posts.append(url) or True)
    assert al.flush_outbox() == 1
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_alerter_uses_reloaded_settings(env):
    s, conn, j, posts = env
    al = Alerter(s, conn)
    s.alerts = dataclasses.replace(s.alerts, teams_webhook="https://new-private")
    al.raise_alert({"key": "HUM-8", "summary": "x", "priority": "High", "url": "u"}, "my_status", external=True)
    assert [u for u, _ in posts] == ["https://new-private"]
