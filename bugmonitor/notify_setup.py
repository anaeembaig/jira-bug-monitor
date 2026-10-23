"""`python -m bugmonitor notifications`: see, activate, and preview your notifications.

  notifications              Show which notifications are on and where they go
  notifications --activate   Turn on the recommended set in config.toml (backup kept)
  notifications --preview    Send one sample Teams message listing what you'll receive
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Settings, config_path as resolve_config, load_settings

EVENTS = [
    # (config key, section, label, who it reaches beyond the dashboard)
    ("new_bug", "alerts", "New bug in your projects", "priority"),
    ("priority_raised", "alerts", "A bug's priority goes up", "priority"),
    ("reopened", "alerts", "A resolved bug is reopened", "priority"),
    ("assigned_to_me", "alerts", "A ticket is assigned to you", "always"),
    ("my_status_changes", "alerts", "Your ticket changes status", "always"),
    ("alerts_to_external", "github", "PR: review requested, comments, approved, "
                                     "changes requested, checks failing, conflicts, merged", "github"),
]


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v)
    return f"'{s}'" if '"' in s and "'" not in s else json.dumps(s)


def set_config_values(path: Path, updates: dict[str, dict], only_if_missing: set[str] = frozenset()) -> list[str]:
    """Edit config.toml in place, keeping comments and everything else. Returns changes made.
    Keys named in only_if_missing (as "section.key") are added but never overwritten."""
    from .tomledit import set_values
    return set_values(Path(path), updates, only_if_missing)


def recommended(settings: Settings) -> tuple[dict, set]:
    m = re.search(r"project\s+in\s*\([^)]*\)|project\s*=\s*\S+", settings.jira.jql, re.I)
    projects = f" AND {m.group(0)}" if m else ""
    updates = {
        "alerts": {"new_bug": True, "priority_raised": True, "reopened": True,
                   "assigned_to_me": True, "my_status_changes": True,
                   "external_min_priority": "Normal", "desktop": True},
        "jira": {"my_jql": f"assignee = currentUser(){projects} AND updated >= -60d"},
        "github": {"alerts_to_external": True},
    }
    return updates, {"jira.my_jql"}


def status(settings: Settings) -> list[str]:
    a, g = settings.alerts, settings.github
    out = ["Notifications", ""]
    out.append(f"  Teams:   {'connected' if a.teams_webhook else 'NOT SET (add teams_webhook under [alerts])'}")
    out.append(f"  Desktop: {'on' if a.desktop else 'off'}")
    out.append(f"  Slack:   {'connected' if a.slack_webhook else 'off'}")
    out.append(f"  Email:   {'on' if a.email_enabled else 'off'}")
    out.append("")
    for key, section, label, reach in EVENTS:
        on = getattr(a if section == "alerts" else g, key, False)
        if reach == "priority":
            where = f"Teams at {a.external_min_priority}+ priority"
        elif reach == "github":
            where = "Teams" if g.enabled else "GitHub is not enabled ([github] enabled = true)"
        else:
            where = "Teams, any priority"
        out.append(f"  [{'on ' if on else 'off'}] {label}" + (f"  ->  {where}" if on else ""))
    out.append("")
    out.append(f"  My tickets filter: {settings.jira.my_jql or '(off)'}")
    if not a.teams_webhook:
        out.append("\n  Nothing reaches Teams until teams_webhook is set.")
    return out


def preview(settings: Settings) -> str:
    """One Teams card listing every active notification, so you can see them arrive."""
    from .alerts import Alerter
    from .db import connect
    if not settings.alerts.teams_webhook:
        return "teams_webhook is not set, so there's nothing to preview."
    a, g = settings.alerts, settings.github
    active = [label for key, section, label, _ in EVENTS
              if getattr(a if section == "alerts" else g, key, False)
              and not (section == "github" and not g.enabled)]
    card_items = [{"type": "TextBlock", "text": f"- {x}", "wrap": True, "spacing": "None"} for x in active]
    payload = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"type": "AdaptiveCard", "version": "1.4",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "body": [{"type": "TextBlock", "text": "Bug Monitor notifications are on",
                              "weight": "Bolder", "size": "Medium", "color": "Good"},
                             {"type": "TextBlock", "wrap": True, "isSubtle": True,
                              "text": f"New-bug alerts arrive at {a.external_min_priority}+ priority. "
                                      "You'll get a message here when:"}] + card_items,
                    "actions": [{"type": "Action.OpenUrl", "title": "Open dashboard",
                                 "url": f"http://{settings.dashboard.host}:{settings.dashboard.port}"}]}}]}
    ok = Alerter(settings, connect(settings.db_path))._post(a.teams_webhook, payload, "Teams")
    return "Preview sent to Teams." if ok else "Teams didn't accept the message. Check the webhook URL."


def main(argv: list[str], config_path: str | None = None) -> int:
    path = resolve_config(config_path)
    settings = load_settings(path)
    if "--activate" in argv:
        updates, keep = recommended(settings)
        changes = set_config_values(path, updates, keep)
        print("\n".join(changes) if changes else "Already active. Nothing to change.")
        if changes:
            print(f"\nSaved {path.name} (previous version: {path.name}.bak). Restart ./start.sh to apply.")
        settings = load_settings(path)
    print("\n".join(status(settings)))
    if "--preview" in argv:
        print("\n" + preview(settings))
    return 0
