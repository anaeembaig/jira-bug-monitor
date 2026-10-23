"""`bugmonitor init`: a guided setup anyone on the team can run.

Asks for the Jira site, login, projects, Teams webhook, and GitHub token; tests each
login; writes config.toml; keeps tokens in the OS credential store; connects Claude.
Run it again any time to change answers (current values are the defaults).
"""
from __future__ import annotations

import getpass
import re
import shutil
from pathlib import Path
from typing import Callable

from . import vault
from .config import config_path, load_settings
from .notify_setup import set_config_values

TEMPLATE = Path(__file__).parent / "config.example.toml"


class IO:
    """Console I/O, swappable in tests."""

    def __init__(self, ask: Callable[[str], str] = input, secret: Callable[[str], str] = getpass.getpass,
                 out: Callable[[str], None] = print):
        self.ask_raw, self.secret_raw, self.out = ask, secret, out

    def ask(self, q: str, default: str = "") -> str:
        a = self.ask_raw(f"{q}{f' [{default}]' if default else ''}: ").strip()
        return a or default

    def yes(self, q: str, default: bool = True) -> bool:
        a = self.ask(f"{q} ({'Y/n' if default else 'y/N'})").lower()
        return default if not a else a.startswith("y")

    def secret(self, q: str, have: bool) -> str:
        return self.secret_raw(f"{q}{' (Enter keeps the saved one)' if have else ''}: ").strip()


def _site(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and not url.startswith("http"):
        url = "https://" + url
    if url and "." not in url.split("//", 1)[-1]:
        url += ".atlassian.net"
    return url


def _projects_clause(keys: list[str]) -> str:
    return f"project in ({', '.join(keys)})" if keys else ""


def _keys_from_jql(jql: str) -> list[str]:
    m = re.search(r"project\s+in\s*\(([^)]*)\)", jql or "", re.I)
    if m:
        return [k.strip().strip("'\"") for k in m.group(1).split(",") if k.strip()]
    m = re.search(r"project\s*=\s*['\"]?([A-Za-z0-9_]+)", jql or "", re.I)
    return [m.group(1)] if m else []


def run(io: IO | None = None, path: str | None = None, jira_factory=None, github_factory=None,
        jenkins_factory=None) -> int:
    io = io or IO()
    from .jira_client import JiraClient, JiraError
    jira_factory = jira_factory or JiraClient
    cfg = config_path(path)
    current = load_settings(cfg)
    first_time = not cfg.exists()
    io.out(f"Bug Monitor setup. Settings file: {cfg}")
    if not vault.store_available():
        io.out("No credential store found; tokens will be saved in the settings file instead.")

    # ---------------------------------------------------------------- Jira
    io.out("\n1. Jira")
    site = _site(io.ask("Jira site (e.g. yourcompany.atlassian.net)",
                        "" if "YOUR-SITE" in current.jira.base_url else current.jira.base_url))
    email = io.ask("Your Jira email", "" if current.jira.email == "you@example.com" else current.jira.email)
    io.out("Create an API token at https://id.atlassian.com/manage-profile/security/api-tokens "
           "(choose 'Create API token', not the scoped one).")
    token = current.jira.api_token
    while True:
        entered = io.secret("Jira API token", have=bool(token))
        token = entered or token
        try:
            client = jira_factory(site, email, token)
            me = client.myself()
            io.out(f"Connected as {me.get('displayName')}.")
            break
        except (JiraError, Exception) as e:  # keep asking until it works or the user quits
            io.out(f"That didn't work: {e}")
            if not io.yes("Try again?"):
                return 1

    try:
        projects = client.projects()
    except Exception:
        projects = []
    known = {p["key"] for p in projects}
    default_keys = _keys_from_jql(current.jira.jql) or []
    if projects:
        io.out("Projects you can see: " + ", ".join(sorted(known))[:900])
    while True:
        keys = [k.strip().upper() for k in io.ask("Project keys to watch, comma separated",
                                                   ", ".join(default_keys)).split(",") if k.strip()]
        unknown = [k for k in keys if known and k not in known]
        if keys and not unknown:
            break
        io.out(f"Unknown project keys: {', '.join(unknown)}" if unknown else "Enter at least one project key.")
    days = int(io.ask("How many days of history to show", str(current.jira.retention_days or 60)))
    proj = _projects_clause(keys)

    # ---------------------------------------------------------------- Teams
    io.out("\n2. Teams notifications (optional)")
    io.out("In Teams (or make.powerautomate.com): Workflows > 'Post to a chat when a webhook request is "
           "received' > Post as Flow bot > Chat with Flow bot > you as recipient. Copy its URL.")
    webhook = current.alerts.teams_webhook
    entered = io.secret("Your private Teams webhook URL (blank to skip)", have=bool(webhook))
    webhook = entered or webhook
    min_pri = io.ask("Lowest priority for new-bug alerts in Teams", current.alerts.external_min_priority or "Normal")

    # ---------------------------------------------------------------- GitHub
    io.out("\n3. GitHub (optional)")
    gh_on = io.yes("Show your pull requests and branches?", current.github.enabled)
    gh_token, orgs = current.github.token, current.github.orgs
    if gh_on:
        from .github_client import GitHubClient, resolve_token
        github_factory = github_factory or GitHubClient
        io.out("Classic token with the 'repo' scope: https://github.com/settings/tokens/new "
               "(authorize it for your org's SSO if asked).")
        while True:
            entered = io.secret("GitHub token (blank uses the GitHub CLI login)", have=bool(gh_token))
            gh_token = entered or gh_token
            try:
                login = github_factory(resolve_token(gh_token)).viewer()
                io.out(f"GitHub: connected as {login}.")
                break
            except Exception as e:
                io.out(f"That didn't work: {e}")
                if not io.yes("Try again?"):
                    gh_on = False
                    break
        orgs = [o.strip() for o in io.ask("GitHub organizations to include, comma separated",
                                          ", ".join(orgs)).split(",") if o.strip()]

    # ---------------------------------------------------------------- Team
    io.out("\n4. Team views (optional)")
    team_on = io.yes("Turn on team views (team board, sprint, reviews, insights)?", bool(current.team.jql))
    team_jql = (current.team.jql or f"{proj} AND (statusCategory != Done OR updated >= -30d)") if team_on else ""
    team_name = io.ask("Team name", current.team.name) if team_on else current.team.name

    # ---------------------------------------------------------------- Jenkins
    io.out("\n5. Jenkins (optional)")
    jk = current.jenkins
    jk_on = io.yes("Show Jenkins builds, test results, and deploy jobs?", jk.enabled)
    jk_url, jk_user, jk_token, jk_ca = jk.url, jk.user, jk.token, jk.ca_bundle
    if jk_on:
        from .jenkins_client import JenkinsClient
        jenkins_factory = jenkins_factory or JenkinsClient
        jk_url = io.ask("Jenkins address, e.g. https://jenkins.example.com", jk_url).rstrip("/")
        if jk_url.startswith("http://") and not io.yes("That address isn't https, so your token would travel "
                                                        "unencrypted. Use it anyway?", False):
            jk_url = io.ask("Jenkins https address", jk_url.replace("http://", "https://", 1)).rstrip("/")
        jk_user = io.ask("Your Jenkins user ID", jk_user or email)
        io.out(f"Create an API token in Jenkins: your name (top right), Security (or Configure), Add new token. "
               f"{jk_url}/me/configure")
        while True:
            entered = io.secret("Jenkins API token", have=bool(jk_token))
            jk_token = entered or jk_token
            try:
                who = jenkins_factory(jk_url, jk_user, jk_token, jk_ca).whoami()
                io.out(f"Jenkins: connected as {who or jk_user}.")
                break
            except Exception as e:
                io.out(f"That didn't work: {e}")
                if "certificate" in str(e).lower():
                    jk_ca = io.ask("Path to your company's CA certificate file (ask IT), or blank to skip", jk_ca)
                if not io.yes("Try again?"):
                    jk_on = False
                    break

    # ---------------------------------------------------------------- save
    use_store = vault.store_available() and io.yes("Save tokens in your credential store (recommended)?")
    secrets = {"jira_api_token": token, "teams_webhook": webhook, "github_token": gh_token if gh_on else "",
               "jenkins_token": jk_token if jk_on else ""}
    stored = {}
    for name, value in secrets.items():
        if value and use_store and vault.set_secret(name, value):
            stored[name] = True

    if first_time:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TEMPLATE, cfg)
    updates = {
        "jira": {"base_url": site, "email": email,
                 "api_token": "" if stored.get("jira_api_token") else token,
                 "jql": f"issuetype = Bug AND {proj} AND created >= -{days}d",
                 "my_jql": f"assignee = currentUser() AND {proj} AND updated >= -{days}d",
                 "retention_days": days},
        "alerts": {"teams_webhook": "" if stored.get("teams_webhook") or not webhook else webhook,
                   "external_min_priority": min_pri},
        "github": {"enabled": gh_on, "orgs": orgs, "jira_prefixes": keys, "days": days,
                   "token": "" if stored.get("github_token") or not gh_on else (gh_token or "")},
        "team": {"jql": team_jql, "name": team_name},
        "jenkins": {"enabled": jk_on, "url": jk_url, "user": jk_user, "ca_bundle": jk_ca,
                    "token": "" if stored.get("jenkins_token") or not jk_on else (jk_token or "")},
    }
    changes = set_config_values(cfg, updates)
    io.out(f"\nSaved {cfg}" + (f" ({len(changes)} settings)" if changes else ""))
    if stored:
        io.out("Stored in your credential store: " + ", ".join(vault.NAMES[n] for n in stored))

    if io.yes("\nConnect Bug Monitor to Claude Desktop and Claude Code?"):
        from .install_claude import main as install_claude
        install_claude()
    io.out("\nDone. Start it with:  bugmonitor run")
    if team_on:
        io.out("Add teammates and team Teams channels later in the Settings tab, or under "
               "[[team.members]] and [[routes]] in the settings file.")
    return 0


def move_secrets(path: str | None = None, out=print) -> int:
    """`bugmonitor secrets --move`: move tokens from config.toml into the credential store."""
    cfg = config_path(path)
    if not vault.store_available():
        out("No credential store is available on this computer.")
        return 1
    import tomllib
    raw = tomllib.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    plan = {("jira", "api_token"): "jira_api_token", ("github", "token"): "github_token",
            ("jenkins", "token"): "jenkins_token",
            ("alerts", "teams_webhook"): "teams_webhook", ("alerts", "slack_webhook"): "slack_webhook",
            ("alerts", "smtp_password"): "smtp_password"}
    updates: dict = {}
    for (section, key), name in plan.items():
        value = (raw.get(section) or {}).get(key) or ""
        if value and not value.startswith("keychain:") and vault.set_secret(name, value):
            updates.setdefault(section, {})[key] = ""
            out(f"Moved {vault.NAMES[name]}")
    if not updates:
        out("No tokens found in the settings file. Nothing to move.")
        return 0
    set_config_values(cfg, updates)
    out(f"Removed them from {cfg} (backup: {cfg.name}.bak; delete it once things work).")
    return 0


def secrets_status(path: str | None = None, out=print) -> int:
    s = load_settings(path)
    labels = {"jira_api_token": "Jira API token", "github_token": "GitHub token", "jenkins_token": "Jenkins token",
              "teams_webhook": "Teams webhook", "slack_webhook": "Slack webhook", "smtp_password": "SMTP password"}
    for name, label in labels.items():
        src = s.secret_sources.get(name) or "not set"
        out(f"  {label:16} {src}")
    return 0
