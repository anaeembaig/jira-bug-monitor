"""Settings tab: save, validate, keep secrets out of the file, apply live."""
import tomllib

import pytest

from bugmonitor import settings_api
from bugmonitor.config import load_settings
from bugmonitor.db import connect

BASE = """# my settings
[jira]
base_url = "https://acme.atlassian.net"   # site
[alerts]
teams_webhook = ""
[team]
jql = "project = WEB"

[[team.members]]
name = "Old Person"

[dashboard]
port = 8765
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(BASE)
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "s.db"))
    s = load_settings(cfg)
    return s, connect(s.db_path), cfg


def test_save_everything(env, _isolated_secrets):
    s, conn, cfg = env
    pub = settings_api.public(s)
    assert pub["members"] == [{"name": "Old Person"}] and not pub["private_webhook_set"]
    body = {"values": {"alerts": {"mentions": False, "external_min_priority": "Major", "nonsense": 1},
                       "notify": {"quiet_hours": "20:00-08:00", "digest_time": "09:30"},
                       "timelog": {"daily_target_hours": 7.5, "workdays": ["Mon", "Tue", "Wed", "Thu"]}},
            "private_webhook": "https://me/hook",
            "routes": [{"name": "front end", "webhook": "https://fe/hook", "projects": ["web"],
                        "events": ["new_bug", "deployment", "bogus"], "min_priority": "Major", "digest": True}],
            "members": [{"name": "Sam Lee", "github": "sam", "email": "sam@acme.com"}, {"name": ""}],
            "escalations": [{"name": "Crit", "min_priority": "Critical", "unassigned_hours": 2, "route": "front end"}]}
    out = settings_api.save(conn, s, body)
    assert out["ok"], out
    text = cfg.read_text()
    data = tomllib.loads(text)
    assert "# my settings" in text and "# site" in text                          # comments kept
    assert data["alerts"]["mentions"] is False and "nonsense" not in data["alerts"]
    assert data["alerts"]["teams_webhook"] == "" and _isolated_secrets["teams_webhook"] == "https://me/hook"
    assert data["routes"][0]["webhook"] == "keychain:route-front end" and "https://fe/hook" not in text
    assert data["routes"][0]["projects"] == ["WEB"] and data["routes"][0]["events"] == ["new_bug", "deployment"]
    assert data["team"]["members"] == [{"name": "Sam Lee", "github": "sam", "email": "sam@acme.com"}]
    assert data["escalations"][0]["route"] == "front end"
    assert data["timelog"]["workdays"] == ["Mon", "Tue", "Wed", "Thu"]
    # Applied to the running app without a restart.
    assert s.alerts.external_min_priority == "Major" and s.routes[0].webhook == "https://fe/hook"
    assert s.alerts.teams_webhook == "https://me/hook" and s.timelog.daily_target_hours == 7.5
    assert conn.execute("SELECT action FROM audit").fetchone()[0] == "settings"

    # Saving again without re-entering the webhook keeps it.
    body2 = {"routes": [{"name": "front end", "webhook": "", "events": ["stuck"]}]}
    assert settings_api.save(conn, s, body2)["ok"]
    assert s.routes[0].webhook == "https://fe/hook" and s.routes[0].events == ["stuck"]
    assert settings_api.public(s)["routes"][0]["webhook_set"] is True


@pytest.mark.parametrize("body,msg", [
    ({"values": {"notify": {"digest_time": "9am"}}}, "digest_time"),
    ({"values": {"notify": {"quiet_hours": "late"}}}, "quiet_hours"),
    ({"routes": [{"name": "x", "webhook": "http://insecure"}]}, "https://"),
    ({"routes": [{"name": "bad/name", "webhook": "https://ok"}]}, "Route name"),
    ({"routes": [{"name": "new", "webhook": ""}]}, "needs a webhook"),
    ({"values": {"timelog": {"daily_target_hours": 5000}}}, "range"),
])
def test_rejects_bad_input_and_changes_nothing(env, body, msg):
    s, conn, cfg = env
    before = cfg.read_text()
    out = settings_api.save(conn, s, body)
    assert msg in out["error"] and cfg.read_text() == before


def test_partial_failure_rolls_back(env):
    s, conn, cfg = env
    before = cfg.read_text()
    out = settings_api.save(conn, s, {"values": {"alerts": {"mentions": False}},
                                      "routes": [{"name": "x", "webhook": "http://insecure"}]})
    assert "https://" in out["error"] and cfg.read_text() == before and s.alerts.mentions is True


TRICKY = """# top comment
[alerts]  # my alerts
desktop = true   # pop-ups
external_min_priority = "Normal"

[timelog]
workdays = [
  "Mon",
  "Tue",
]

[team]
name = "Web"
members = [{name = "Inline Person"}]

[[routes]]   # front-end channel
name = "fe"
webhook = "https://fe"
events = ["new_bug"]

# [[escalations]]
# name = "template kept as a comment"

[dashboard]
port = 8765
"""


def test_tricky_configs_are_preserved(tmp_path, _isolated_secrets, monkeypatch):
    """Regression: multi-line arrays, inline tables, header comments, emoji, repeated saves."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(TRICKY)
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "x.db"))
    s = load_settings(cfg)
    conn = connect(s.db_path)
    body = {"values": {"alerts": {"desktop": False}, "timelog": {"workdays": ["Mon", "Wed"]},
                       "team": {"name": "Web 🚀 team"}},
            "routes": [{"name": "fe", "webhook": "", "events": ["new_bug", "deployment"]}],
            "members": [{"name": "Sam Lee", "github": "sam", "email": ""}]}
    for _ in range(3):                                   # saving repeatedly never duplicates anything
        out = settings_api.save(conn, s, body)
        assert out.get("ok"), out
    text = cfg.read_text()
    data = tomllib.loads(text)
    assert len(data["routes"]) == 1 and data["routes"][0]["webhook"] == "https://fe"
    assert data["team"]["members"] == [{"name": "Sam Lee", "github": "sam", "email": ""}]
    assert data["timelog"]["workdays"] == ["Mon", "Wed"] and data["team"]["name"] == "Web 🚀 team"
    assert data["alerts"]["desktop"] is False and text.count("[alerts]") == 1
    for kept in ("# top comment", "# my alerts", "# pop-ups", "# [[escalations]]", "template kept as a comment"):
        assert kept in text, kept


def test_example_template_survives_settings_save(tmp_path, _isolated_secrets, monkeypatch):
    from bugmonitor.config import Path as _P
    import bugmonitor
    template = (_P(bugmonitor.__file__).parent / "config.example.toml").read_text()
    template = template.replace('# [[team.members]]\n# name = "Jira display name"', '[[team.members]]\nname = "Real Person"')
    cfg = tmp_path / "config.toml"
    cfg.write_text(template)
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "y.db"))
    s = load_settings(cfg)
    out = settings_api.save(connect(s.db_path), s, {"members": [{"name": "A"}, {"name": "B"}],
                                                     "values": {"notify": {"digest_time": "09:30"}}})
    assert out.get("ok"), out
    text = cfg.read_text()
    assert "# [[routes]]" in text and "# [[escalations]]" in text and "[timelog]" in text
    assert [m["name"] for m in tomllib.loads(text)["team"]["members"]] == ["A", "B"]
