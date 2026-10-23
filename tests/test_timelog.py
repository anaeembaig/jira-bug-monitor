"""Daily time log: Jira worklogs, Claude-tracked time, activity, reminders, Jira write."""
from datetime import datetime, timedelta, timezone

import pytest

from bugmonitor import service, timelog
from bugmonitor.alerts import Alerter
from bugmonitor.config import load_settings
from bugmonitor.db import connect, set_state
from bugmonitor.sync import sync_once
from test_monitor import history, issue
from test_team import TeamJira, t

LOCAL_TODAY = datetime.now().astimezone().date()


def started(days_ago=0, hour=10):
    d = datetime.combine(LOCAL_TODAY - timedelta(days=days_ago), datetime.min.time()).replace(hour=hour).astimezone()
    return d.strftime("%Y-%m-%dT%H:%M:%S.000%z")


class TimeJira(TeamJira):
    def __init__(self):
        super().__init__()
        self.wl = {}
        self.added = []

    def search(self, jql, extra_fields=None):
        if "worklog" in jql:
            self.queries.append(jql)
            yield from (self.issues[k] for k in self.wl if k in self.issues)
            return
        yield from super().search(jql, extra_fields)

    def worklogs(self, key, started_after_ms=None):
        return self.wl.get(key, [])

    def add_worklog(self, key, seconds, started_, comment=""):
        self.added.append((key, seconds, started_, comment))
        return {"id": 999, "started": started_, "author": {"displayName": "Ali Naeem", "accountId": "acc-AliNaeem"}}


def wl(id_, who, hours, days_ago=0, comment=""):
    return {"id": id_, "author": {"displayName": who, "accountId": "acc-" + who.replace(" ", "")},
            "started": started(days_ago), "timeSpentSeconds": int(hours * 3600), "updated": started(days_ago),
            "comment": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}]}
            if comment else None}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "tl.db"))
    s = load_settings(tmp_path / "none.toml")
    s.alerts.desktop = False
    s.alerts.teams_webhook = "https://private"
    s.timelog.workdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]   # deterministic in any weekday
    s.team.jql = "TEAMSCOPE"
    s.team.members = [{"name": "Ali Naeem"}, {"name": "Sam Lee"}]
    posts = []

    class R:
        status_code = 202; text = ""

    monkeypatch.setattr("bugmonitor.alerts.requests.post", lambda url, json, timeout: posts.append(json) or R())
    j = TimeJira()
    j.myself = lambda: {"displayName": "Ali Naeem", "accountId": "acc-AliNaeem"}
    return s, connect(s.db_path), j, posts


def test_timesheet_combines_logged_tracked_and_activity(env):
    s, conn, j, posts = env
    j.issues["HUM-1"] = t("HUM-1", "In Progress", assignee="Ali Naeem", created_h=48, updated_h=1)
    j.logs["HUM-1"] = [{"id": "7", "created": started(0, 11), "author": {"displayName": "Ali Naeem"},
                        "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}]}]
    j.issues["HUM-2"] = t("HUM-2", "To Do", assignee="Sam Lee", created_h=48)
    j.issues["OPS-9"] = issue("OPS-9", itype="Task", assignee="Ali Naeem")          # outside every scope
    j.wl = {"HUM-1": [wl(1, "Ali Naeem", 3, 0, "fixed rounding"), wl(2, "Ali Naeem", 2, 1)],
            "HUM-2": [wl(3, "Sam Lee", 6, 0)],
            "OPS-9": [wl(4, "Ali Naeem", 1.5, 0)]}
    st = sync_once(conn, s, j)
    assert st["worklogs"]["worklogs"] == 4
    service.log_work(conn, "HUM-2", "note", minutes=45)                                # tracked in Claude, not logged

    ts = timelog.timesheet(conn, s, days=3)
    today = ts["days"][0]
    assert today["date"] == LOCAL_TODAY.isoformat() and today["logged_hours"] == 4.5
    rows = {r["key"]: r for r in today["tickets"]}
    assert rows["HUM-1"]["logged_hours"] == 3 and rows["HUM-1"]["comments"] == ["fixed rounding"]
    assert any("To Do → In Progress" in a for a in rows["HUM-1"]["activity"])
    assert rows["OPS-9"]["logged_hours"] == 1.5 and rows["OPS-9"]["summary"] == "Something broke"
    assert rows["HUM-2"]["logged_hours"] == 0 and rows["HUM-2"]["tracked_hours"] == 0.75
    assert today["unlogged_tickets"] == ["HUM-2"] and today["short_hours"] == 3.5
    assert ts["days"][1]["logged_hours"] == 2

    tt = timelog.team_time(conn, s, days=3)
    by = {r["person"]: r for r in tt["rows"]}
    assert by["Sam Lee"]["days"][LOCAL_TODAY.isoformat()] == 6 and by["Ali Naeem"]["total"] == 6.5
    assert timelog.timesheet(conn, s, 3, person="Sam Lee")["days"][0]["logged_hours"] == 6

    # A worklog deleted in Jira disappears on the next sync.
    j.wl["HUM-1"] = [wl(2, "Ali Naeem", 2, 1)]
    conn.execute("DELETE FROM sync_state WHERE k='worklogs_last_sync'")
    sync_once(conn, s, j)
    assert timelog.timesheet(conn, s, 3)["days"][0]["logged_hours"] == 1.5


def test_reminder_and_recap(env):
    s, conn, j, posts = env
    j.issues["HUM-1"] = t("HUM-1", "In Progress", assignee="Ali Naeem")
    j.wl = {"HUM-1": [wl(1, "Ali Naeem", 5, 0), wl(2, "Ali Naeem", 8, 1)]}
    sync_once(conn, s, j)
    s.timelog.reminder_time = "17:30"
    early = datetime.combine(LOCAL_TODAY, datetime.min.time()).replace(hour=17)
    late = early.replace(hour=18)
    assert timelog.reminder(conn, s, Alerter(s, conn), early) is None
    msg = timelog.reminder(conn, s, Alerter(s, conn), late)
    assert msg.startswith("5h of 8h logged today")
    assert "Time log: 5h of 8h" in posts[0]["attachments"][0]["content"]["body"][0]["text"]
    assert timelog.reminder(conn, s, Alerter(s, conn), late) is None          # once a day
    lines = timelog.recap_lines(conn, s)
    assert lines[0].endswith("8h logged across 1 tickets")


def test_jira_write_is_opt_in(env):
    s, conn, j, posts = env
    set_state(conn, "jira_me", "Ali Naeem"); set_state(conn, "jira_me_id", "acc-AliNaeem")
    assert "off" in timelog.log_to_jira(conn, s, j, "HUM-1", 30)["error"]
    s.timelog.allow_jira_write = True
    out = timelog.log_to_jira(conn, s, j, "hum-1", 90, LOCAL_TODAY.isoformat(), "pairing")
    assert out["ok"] and j.added[0][0] == "HUM-1" and j.added[0][1] == 5400 and j.added[0][3] == "pairing"
    assert timelog.timesheet(conn, s, 1)["days"][0]["logged_hours"] == 1.5
    assert conn.execute("SELECT action FROM audit").fetchone()[0] == "jira_worklog"
    assert "between" in timelog.log_to_jira(conn, s, j, "HUM-1", 0)["error"]


def test_dashboard_posts_need_header(env, tmp_path):
    import json, threading, urllib.request
    from bugmonitor.dashboard import serve
    s, conn, j, posts = env
    s.dashboard.port = 0
    httpd = serve(s)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/alerts/all/ack"
    req = urllib.request.Request(url, data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={"X-Bugmonitor": "1"})
    assert json.loads(urllib.request.urlopen(req).read())["acknowledged"] == 0
    httpd.shutdown()
