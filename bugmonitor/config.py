"""Load settings from config.toml, the OS credential store, and environment variables.

Where config.toml lives (first match wins):
  1. $BUGMON_CONFIG
  2. config.toml next to the code (a git checkout, the original setup)
  3. the per-user config folder (a `pipx install`):
       macOS   ~/Library/Application Support/bugmonitor/config.toml
       Windows %APPDATA%\\bugmonitor\\config.toml
       Linux   ~/.config/bugmonitor/config.toml
A relative db_path is resolved next to config.toml.
"""
from __future__ import annotations

import os
import platform
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def user_config_dir() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "bugmonitor"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home())) / "bugmonitor"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "bugmonitor"


def config_path(path: str | os.PathLike | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    if os.environ.get("BUGMON_CONFIG"):
        return Path(os.environ["BUGMON_CONFIG"]).expanduser()
    local = ROOT / "config.toml"
    if local.exists():
        return local
    return user_config_dir() / "config.toml"


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
    fetch_comments: bool = True    # needed for @mention alerts and comments in ticket details


@dataclass
class AlertSettings:
    new_bug: bool = True
    priority_raised: bool = True
    reopened: bool = True
    alert_on_first_sync: bool = False
    assigned_to_me: bool = True      # a ticket gets assigned to you
    my_status_changes: bool = True   # one of your tickets moves to another status
    mentions: bool = True            # someone @mentions you in a Jira comment
    stuck_reminders: bool = True     # your ticket or PR has been stuck too long (once a day)
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
    token: str = ""                 # blank = credential store, then `gh auth token`
    username: str = ""              # blank = detect from the token
    orgs: list[str] = field(default_factory=list)   # limit PR search, e.g. ["tcp-software"]
    repos: list[str] = field(default_factory=list)  # extra repos for branches and deployments
    jira_prefixes: list[str] = field(default_factory=list)  # blank = projects in your Jira scope
    days: int = 60
    alerts_to_external: bool = True  # send PR alerts to Teams/Slack/email too
    production_environments: list[str] = field(default_factory=lambda: ["production", "prod"])


@dataclass
class TeamSettings:
    name: str = ""
    # Every ticket the team works on (any type). Blank disables team views.
    jql: str = ""
    # [[team.members]] name = "Jira display name", github = "login", email = "for Teams @mentions"
    members: list[dict] = field(default_factory=list)
    qa_statuses: list[str] = field(default_factory=lambda: ["Ready for QA", "In QA", "QA", "Testing"])
    review_statuses: list[str] = field(default_factory=lambda: ["Code Review", "In Review", "Review"])
    # Days in a status before a ticket counts as stuck. "*" applies to other in-progress statuses.
    stuck_days: dict = field(default_factory=lambda: {"*": 5, "Code Review": 2, "In QA": 3})
    pr_review_hours: float = 24     # an open PR waiting longer than this for review is stuck
    boards: list[int] = field(default_factory=list)  # Jira boards for sprints (blank = find by project)
    story_points_field: str = ""    # blank = detect
    escape_labels: list[str] = field(default_factory=lambda: ["production", "prod", "customer", "escalation"])


@dataclass
class NotifySettings:
    quiet_hours: str = ""           # e.g. "20:00-08:00"; non-urgent external alerts wait
    quiet_weekends: bool = False
    urgent_bypasses_quiet: bool = True
    digest_time: str = ""           # e.g. "09:30"; blank turns daily digests off
    digest_personal: bool = True    # your summary to your private webhook
    digest_team: bool = True        # team summary to routes with digest = true
    weekly_report_day: str = ""     # e.g. "Friday"; posts the weekly report to digest routes
    sync_health: bool = True        # tell you when syncing keeps failing


@dataclass
class Route:
    """[[routes]]: send team events to a Teams channel (one Workflows webhook per channel)."""
    name: str = ""
    webhook: str = ""
    projects: list[str] = field(default_factory=list)  # blank = all projects
    events: list[str] = field(default_factory=lambda: ["new_bug", "priority_raised", "reopened",
                                                       "escalation", "stuck"])
    min_priority: str = ""          # blank = use alerts.external_min_priority
    digest: bool = False            # also post the daily team digest and weekly report here
    mention_assignee: bool = True


@dataclass
class Escalation:
    """[[escalations]]: e.g. a Critical bug still unassigned after 2 hours."""
    name: str = ""
    min_priority: str = "Critical"
    unassigned_hours: float = 0     # fire when unassigned this long (0 = ignore)
    open_hours: float = 0           # fire when not resolved this long (0 = ignore)
    projects: list[str] = field(default_factory=list)
    route: str = ""                 # route name; blank = your private webhook


@dataclass
class TimelogSettings:
    enabled: bool = True
    days: int = 14                   # how far back to show and sync worklogs
    daily_target_hours: float = 8
    workdays: list[str] = field(default_factory=lambda: ["Mon", "Tue", "Wed", "Thu", "Fri"])
    reminder_time: str = "17:30"     # end-of-day reminder when you've logged less than the target ("" = off)
    recap_in_digest: bool = True     # yesterday's hours in the morning digest
    team: bool = True                # teammates' worklogs on team tickets (needs [team] jql)
    allow_jira_write: bool = False   # let the dashboard and Claude log time to Jira for you


@dataclass
class DeploySettings:
    enabled: bool = True             # needs [github] enabled
    days: int = 30
    # Map raw environment names to friendly ones, e.g. {"prod-us-east-1" = "Production"}
    aliases: dict = field(default_factory=dict)
    alert_my_tickets: bool = True    # your tickets reach an environment
    alert_failures: bool = True      # a deploy that includes your tickets fails, or any production deploy fails
    alert_rollbacks: bool = True
    workflow_keywords: list[str] = field(default_factory=lambda: ["deploy", "release"])  # Actions runs to track


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
    team: TeamSettings = field(default_factory=TeamSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    routes: list[Route] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)
    timelog: TimelogSettings = field(default_factory=TimelogSettings)
    deploy: DeploySettings = field(default_factory=DeploySettings)
    config_file: Path | None = None
    secret_sources: dict = field(default_factory=dict)  # name -> env | keychain | config | ""

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

    def at_least(self, name: str | None, threshold: str | None) -> bool:
        """True if `name` is at or above `threshold`. Unknown priorities are never filtered out."""
        if not threshold or not self.known_priority(name):
            return True
        return self.priority_rank(name) >= self.priority_rank(threshold)

    def is_high(self, name: str | None) -> bool:
        return (name or "").lower() in (p.lower() for p in self.high_priorities)

    def member(self, name: str | None = None, github: str | None = None) -> dict | None:
        for m in self.team.members:
            if name and (m.get("name") or "").lower() == name.lower():
                return m
            if github and (m.get("github") or "").lower() == github.lower():
                return m
        return None

    def route(self, name: str) -> Route | None:
        return next((r for r in self.routes if r.name == name), None)


def _apply(dc, data: dict):
    for k, v in (data or {}).items():
        if hasattr(dc, k):
            setattr(dc, k, v)
    return dc


def _secret(env: str, configured: str, name: str, sources: dict) -> str:
    """Environment wins, then config.toml, then the OS credential store."""
    from .vault import get_secret
    if os.environ.get(env):
        sources[name] = "env"
        return os.environ[env]
    if configured and not configured.startswith("keychain:"):
        sources[name] = "config"
        return configured
    key = configured.split(":", 1)[1] if configured.startswith("keychain:") else name
    value = get_secret(key)
    sources[name] = "keychain" if value else ""
    return value


def load_settings(path: str | os.PathLike | None = None, *, secrets: bool = True) -> Settings:
    cfg_path = config_path(path)
    raw: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)

    jira = _apply(JiraSettings(), raw.get("jira", {}))
    alerts = _apply(AlertSettings(), raw.get("alerts", {}))
    dash = _apply(DashboardSettings(), raw.get("dashboard", {}))
    gh = _apply(GitHubSettings(), raw.get("github", {}))
    team = _apply(TeamSettings(), raw.get("team", {}))
    notify = _apply(NotifySettings(), raw.get("notify", {}))
    routes = [_apply(Route(), r) for r in raw.get("routes", [])]
    escalations = [_apply(Escalation(), e) for e in raw.get("escalations", [])]
    timelog = _apply(TimelogSettings(), raw.get("timelog", {}))
    deploy = _apply(DeploySettings(), raw.get("deployments", {}))

    jira.base_url = os.environ.get("JIRA_BASE_URL", jira.base_url).rstrip("/")
    jira.email = os.environ.get("JIRA_EMAIL", jira.email)
    sources: dict = {}
    if secrets:
        jira.api_token = _secret("JIRA_API_TOKEN", jira.api_token, "jira_api_token", sources)
        gh.token = _secret("GITHUB_TOKEN", gh.token, "github_token", sources)
        alerts.teams_webhook = _secret("BUGMON_TEAMS_WEBHOOK", alerts.teams_webhook, "teams_webhook", sources)
        alerts.slack_webhook = _secret("BUGMON_SLACK_WEBHOOK", alerts.slack_webhook, "slack_webhook", sources)
        alerts.smtp_password = _secret("BUGMON_SMTP_PASSWORD", alerts.smtp_password, "smtp_password", sources)
        for r in routes:
            if r.webhook.startswith("keychain:"):
                r.webhook = _secret("", r.webhook, f"route:{r.name}", sources)

    storage = raw.get("storage", {})
    db = Path(os.environ.get("BUGMON_DB", storage.get("db_path", "data/monitor.db"))).expanduser()
    if not db.is_absolute():
        db = cfg_path.parent / db

    pr = raw.get("priority", {})
    priority_order = pr.get("order", list(DEFAULT_PRIORITY_ORDER))
    high = pr.get("high", list(DEFAULT_HIGH))
    return Settings(jira, alerts, dash, gh, db, priority_order, high, team, notify, routes,
                    escalations, timelog, deploy, cfg_path, sources)
