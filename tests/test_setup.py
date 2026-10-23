"""Setup wizard, credential store, and config location."""
import tomllib

import pytest

from bugmonitor import init_wizard
from bugmonitor.config import config_path, load_settings


class FakeJiraLogin:
    def __init__(self, site, email, token):
        if token != "good-token":
            raise RuntimeError("401")
        self.site = site

    def myself(self):
        return {"displayName": "Dana Dev", "accountId": "acc-1"}

    def projects(self):
        return [{"key": "WEB", "name": "Web"}, {"key": "API", "name": "Api"}, {"key": "OPS", "name": "Ops"}]


class FakeGH:
    def __init__(self, token):
        self.token = token

    def viewer(self):
        return "dana-dev"


def scripted(answers, secrets):
    answers, secrets, said = list(answers), list(secrets), []
    return init_wizard.IO(ask=lambda q: answers.pop(0), secret=lambda q: secrets.pop(0),
                          out=said.append), said


def test_wizard_first_run_writes_config_and_keeps_tokens_out(tmp_path, _isolated_secrets, monkeypatch):
    cfg = tmp_path / "cfg" / "config.toml"
    monkeypatch.setattr("bugmonitor.install_claude.main", lambda: None)
    io, said = scripted(
        answers=["acme", "dana@acme.com",          # site (short form), email
                 "y",                              # wrong token -> try again
                 "web, api", "30",                 # projects, days
                 "Major",                          # Teams threshold
                 "y", "acme-org",                  # GitHub on, orgs
                 "y", "Web team",                  # team views, team name
                 "y",                              # use credential store
                 "n"],                             # connect Claude
        secrets=["bad-token", "good-token", "https://teams/hook", "ghp_x"])
    assert init_wizard.run(io, str(cfg), jira_factory=FakeJiraLogin, github_factory=FakeGH) == 0

    data = tomllib.loads(cfg.read_text())
    assert data["jira"]["base_url"] == "https://acme.atlassian.net"
    assert data["jira"]["api_token"] == "" and data["alerts"]["teams_webhook"] == ""
    assert data["jira"]["jql"] == "issuetype = Bug AND project in (WEB, API) AND created >= -30d"
    assert data["jira"]["my_jql"].startswith("assignee = currentUser() AND project in (WEB, API)")
    assert data["github"]["enabled"] is True and data["github"]["orgs"] == ["acme-org"]
    assert data["github"]["jira_prefixes"] == ["WEB", "API"]
    assert data["team"]["jql"].startswith("project in (WEB, API) AND") and data["team"]["name"] == "Web team"
    assert data["alerts"]["external_min_priority"] == "Major"
    assert _isolated_secrets == {"jira_api_token": "good-token", "teams_webhook": "https://teams/hook",
                                 "github_token": "ghp_x"}
    assert any("didn't work" in s for s in said) and any("Connected as Dana Dev" in s for s in said)

    s = load_settings(cfg)   # tokens come back from the credential store
    assert s.jira.api_token == "good-token" and s.alerts.teams_webhook == "https://teams/hook"
    assert s.secret_sources["jira_api_token"] == "keychain"
    assert s.db_path == cfg.parent / "data" / "monitor.db"


def test_wizard_rerun_keeps_saved_values(tmp_path, _isolated_secrets, monkeypatch):
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr("bugmonitor.install_claude.main", lambda: None)
    first, _ = scripted(["acme", "dana@acme.com", "WEB", "60", "Normal", "n", "n", "y", "n"],
                        ["good-token", ""])
    init_wizard.run(first, str(cfg), jira_factory=FakeJiraLogin)
    again, _ = scripted(["", "", "", "", "", "n", "n", "y", "n"], ["", ""])   # accept every default
    init_wizard.run(again, str(cfg), jira_factory=FakeJiraLogin)
    data = tomllib.loads(cfg.read_text())
    assert data["jira"]["base_url"] == "https://acme.atlassian.net" and "project in (WEB)" in data["jira"]["jql"]
    assert _isolated_secrets["jira_api_token"] == "good-token"


def test_unknown_project_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("bugmonitor.install_claude.main", lambda: None)
    io, said = scripted(["acme", "d@a.com", "NOPE", "WEB", "60", "Normal", "n", "n", "y", "n"], ["good-token", ""])
    assert init_wizard.run(io, str(tmp_path / "c.toml"), jira_factory=FakeJiraLogin) == 0
    assert any("Unknown project keys: NOPE" in s for s in said)


def test_move_secrets_out_of_config(tmp_path, _isolated_secrets):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[jira]\napi_token = "tok"\n[alerts]\nteams_webhook = "https://hook"\n[github]\ntoken = ""\n')
    assert init_wizard.move_secrets(str(cfg), out=lambda *_: None) == 0
    data = tomllib.loads(cfg.read_text())
    assert data["jira"]["api_token"] == "" and data["alerts"]["teams_webhook"] == ""
    assert _isolated_secrets == {"jira_api_token": "tok", "teams_webhook": "https://hook"}
    assert load_settings(cfg).jira.api_token == "tok"


def test_env_beats_config_beats_store(tmp_path, monkeypatch, _isolated_secrets):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[jira]\napi_token = "from-config"\n[[routes]]\nname = "fe"\nwebhook = "keychain:fe-hook"\n')
    _isolated_secrets.update({"jira_api_token": "from-store", "fe-hook": "https://fe"})
    assert load_settings(cfg).jira.api_token == "from-config"
    monkeypatch.setenv("JIRA_API_TOKEN", "from-env")
    s = load_settings(cfg)
    assert s.jira.api_token == "from-env" and s.routes[0].webhook == "https://fe"


def test_config_location_order(tmp_path, monkeypatch):
    monkeypatch.setenv("BUGMON_CONFIG", str(tmp_path / "x.toml"))
    assert config_path() == tmp_path / "x.toml"
    monkeypatch.delenv("BUGMON_CONFIG")
    monkeypatch.setattr("bugmonitor.config.ROOT", tmp_path / "checkout")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr("bugmonitor.config.platform.system", lambda: "Linux")
    assert config_path() == tmp_path / "xdg" / "bugmonitor" / "config.toml"
    (tmp_path / "checkout").mkdir()
    (tmp_path / "checkout" / "config.toml").write_text("")
    assert config_path() == tmp_path / "checkout" / "config.toml"   # existing checkout setups keep working
