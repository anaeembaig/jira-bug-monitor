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
    # Tickets assigned to you, any type, for the My Tickets view. Blank disables it.
    my_jql: str = "assignee = currentUser() AND updated >= -60d"


@dataclass
class AlertSettings:
    new_bug: bool = True
    priority_raised: bool = True
    reopened: bool = True
    alert_on_first_sync: bool = False
    assigned_to_me: bool = True      # a ticket gets assigned to you
    my_status_changes: bool = True   # one of your tickets moves to another status
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


# Covers both Jira priority schemes (Highest..Lowest and Blocker..Trivial), most urgent first.
DEFAULT_PRIORITY_ORDER = ["Blocker", "Highest", "Critical", "High", "Major", "Medium",
                          "Normal", "Low", "Minor", "Lowest", "Trivial"]
DEFAULT_HIGH = ["Blocker", "Highest", "Critical", "High", "Major"]


@dataclass
class Settings:
    jira: JiraSettings
    alerts: AlertSettings
    dashboard: DashboardSettings
    github: GitHubSettings
    db_path: Path
    priority_order: list[str]
    high_priorities: list[str] = field(default_factory=lambda: list(DEFAULT_HIGH))

    def priority_rank(self, name: str | None) -> float:
        """Higher number = more urgent, on a 0..len(priority_order) scale.
        Names missing from the configured order fall back to the built-in order."""
        if not name:
            return 0
        n, levels = name.lower(), len(self.priority_order)
        order = [p.lower() for p in self.priority_order]
        if n in order:
            return levels - order.index(n)
        default = [p.lower() for p in DEFAULT_PRIORITY_ORDER]
        if n in default:
            return (len(default) - default.index(n)) / len(default) * levels
        return 0

    def known_priority(self, name: str | None) -> bool:
        n = (name or "").lower()
        return bool(n) and (n in (p.lower() for p in self.priority_order)
                            or n in (p.lower() for p in DEFAULT_PRIORITY_ORDER))

    def is_high(self, name: str | None) -> bool:
        return (name or "").lower() in (p.lower() for p in self.high_priorities)


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

    pr = raw.get("priority", {})
    priority_order = pr.get("order", list(DEFAULT_PRIORITY_ORDER))
    high = pr.get("high", list(DEFAULT_HIGH))
    return Settings(jira, alerts, dash, gh, db, priority_order, high)
