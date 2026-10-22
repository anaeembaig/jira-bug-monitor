"""Load settings from config.toml with environment-variable overrides."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class JiraSettings:
    base_url: str = ""
    email: str = ""
    api_token: str = ""
    jql: str = "issuetype = Bug"
    poll_minutes: float = 5
    backfill_days: int = 90
    fetch_changelog: bool = True
    retention_days: int = 0        # drop tickets created longer ago than this (0 = keep all)


@dataclass
class AlertSettings:
    new_bug: bool = True
    priority_raised: bool = True
    reopened: bool = True
    alert_on_first_sync: bool = False
    assigned_to_me: bool = True      # a bug gets assigned to you
    # Slack, Teams, and email only fire at or above this priority.
    # Desktop and dashboard alerts fire for everything.
    external_min_priority: str = "Medium"
    desktop: bool = True
    slack_webhook: str = ""
    teams_webhook: str = ""
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    email_from: str = ""
    email_to: list[str] = field(default_factory=list)


@dataclass
class GitHubSettings:
    enabled: bool = False
    token: str = ""                 # blank = use `gh auth token` if the GitHub CLI is installed
    username: str = ""              # blank = detect from the token
    orgs: list[str] = field(default_factory=list)   # limit PR search, e.g. ["tcp-software"]
    repos: list[str] = field(default_factory=list)  # extra repos to scan for your branches
    jira_prefixes: list[str] = field(default_factory=list)  # blank = projects in your Jira scope
    days: int = 60
    alerts_to_external: bool = True  # send PR alerts to Teams/Slack/email too


@dataclass
class DashboardSettings:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class Settings:
    jira: JiraSettings
    alerts: AlertSettings
    dashboard: DashboardSettings
    github: GitHubSettings
    db_path: Path
    priority_order: list[str]

    def priority_rank(self, name: str | None) -> int:
        """Higher number = more urgent. Unknown priorities rank lowest."""
        if not name:
            return 0
        order = [p.lower() for p in self.priority_order]
        n = name.lower()
        return len(order) - order.index(n) if n in order else 0


def _apply(dc, data: dict):
    for k, v in (data or {}).items():
        if hasattr(dc, k):
            setattr(dc, k, v)
    return dc


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    cfg_path = Path(path or os.environ.get("BUGMON_CONFIG", ROOT / "config.toml"))
    raw: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)

    jira = _apply(JiraSettings(), raw.get("jira", {}))
    alerts = _apply(AlertSettings(), raw.get("alerts", {}))
    dash = _apply(DashboardSettings(), raw.get("dashboard", {}))
    gh = _apply(GitHubSettings(), raw.get("github", {}))
    gh.token = os.environ.get("GITHUB_TOKEN", gh.token)

    # Secrets from the environment win over the file.
    jira.base_url = os.environ.get("JIRA_BASE_URL", jira.base_url).rstrip("/")
    jira.email = os.environ.get("JIRA_EMAIL", jira.email)
    jira.api_token = os.environ.get("JIRA_API_TOKEN", jira.api_token)
    alerts.slack_webhook = os.environ.get("BUGMON_SLACK_WEBHOOK", alerts.slack_webhook)
    alerts.teams_webhook = os.environ.get("BUGMON_TEAMS_WEBHOOK", alerts.teams_webhook)
    alerts.smtp_password = os.environ.get("BUGMON_SMTP_PASSWORD", alerts.smtp_password)

    storage = raw.get("storage", {})
    db = Path(os.environ.get("BUGMON_DB", storage.get("db_path", "data/monitor.db"))).expanduser()
    if not db.is_absolute():
        db = ROOT / db

    priority_order = raw.get("priority", {}).get(
        "order", ["Highest", "High", "Medium", "Low", "Lowest"]
    )
    return Settings(jira, alerts, dash, gh, db, priority_order)
