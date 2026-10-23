"""Keep tests away from the real OS credential store and the developer's config."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_secrets(monkeypatch, tmp_path):
    store: dict = {}
    monkeypatch.setattr("bugmonitor.vault.get_secret", lambda name: store.get(name, ""))
    monkeypatch.setattr("bugmonitor.vault.set_secret", lambda name, value: store.__setitem__(name, value) or True)
    monkeypatch.setattr("bugmonitor.vault.delete_secret", lambda name: store.pop(name, None))
    monkeypatch.setattr("bugmonitor.vault.store_available", lambda: True)
    monkeypatch.setenv("BUGMON_CONFIG", str(tmp_path / "none.toml"))
    for k in ("JIRA_API_TOKEN", "GITHUB_TOKEN", "BUGMON_TEAMS_WEBHOOK", "BUGMON_SLACK_WEBHOOK"):
        monkeypatch.delenv(k, raising=False)
    return store
