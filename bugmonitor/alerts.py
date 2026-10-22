"""Alert delivery: dashboard (database), desktop, Slack, Teams, and email."""
from __future__ import annotations

import json
import logging
import platform
import shutil
import smtplib
import subprocess
from email.message import EmailMessage

import requests

from .config import Settings
from .db import now_iso

log = logging.getLogger("bugmonitor.alerts")

TITLES = {
    "new_bug": "New bug",
    "priority_raised": "Priority raised",
    "reopened": "Bug reopened",
    "assigned_to_me": "Assigned to you",
    "my_status": "Your ticket moved",
    "review_requested": "Review requested",
    "checks_failed": "Checks failing",
    "pr_approved": "PR approved",
    "changes_requested": "Changes requested",
    "pr_merged": "PR merged",
    "pr_comment": "New comment on your PR",
    "merge_conflicts": "Merge conflicts",
}


def teams_card(title: str, issue: dict, kind: str, detail: str, dash) -> dict:
    url = issue.get("url") or ""
    is_pr = "github.com" in url
    facts = [{"title": t, "value": v} for t, v in (
        ("Type", issue.get("issuetype") or ("Pull request" if is_pr else "")),
        ("Priority", issue.get("priority") or ""),
        ("Status", issue.get("status") or ""),
        ("Author" if is_pr else "Assignee", issue.get("assignee") or ("" if is_pr else "Unassigned")),
    ) if v]
    body = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True,
         "color": CARD_COLOR.get(kind, "Default")},
        {"type": "TextBlock", "text": issue.get("summary") or "", "wrap": True, "spacing": "Small"},
    ]
    if detail:
        body.append({"type": "TextBlock", "text": detail, "wrap": True, "isSubtle": True,
                     "spacing": "Small", "maxLines": 4})
    if facts:
        body.append({"type": "FactSet", "facts": facts})
    actions = []
    if url:
        actions.append({"type": "Action.OpenUrl", "title": "Open in GitHub" if is_pr else "Open in Jira",
                        "url": url})
    actions.append({"type": "Action.OpenUrl", "title": "Open dashboard",
                    "url": f"http://{dash.host}:{dash.port}"})
    return {"type": "AdaptiveCard", "version": "1.4",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": body, "actions": actions}


def desktop_notify(title: str, body: str) -> bool:
    system = platform.system()
    try:
        if system == "Darwin":
            safe = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe(body)}" with title "{safe(title)}"'],
                check=False, timeout=10,
            )
            return True
        if system == "Windows":
            try:
                from plyer import notification  # optional dependency
                notification.notify(title=title[:63], message=body[:255], timeout=10)
                return True
            except Exception:
                ps = (
                    "[reflection.assembly]::loadwithpartialname('System.Windows.Forms')|Out-Null;"
                    "$n=New-Object System.Windows.Forms.NotifyIcon;"
                    "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                    f"$n.ShowBalloonTip(10000,'{title[:63].replace(chr(39), '')}',"
                    f"'{body[:255].replace(chr(39), '')}',"
                    "[System.Windows.Forms.ToolTipIcon]::Info);Start-Sleep 11;$n.Dispose()"
                )
                subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                                  "-Command", ps])
                return True
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], check=False, timeout=10)
            return True
    except Exception as e:  # never let a notification failure stop the sync
        log.warning("Desktop notification failed: %s", e)
    return False


# Adaptive Card text colors per alert kind (Attention = red, Warning = amber, Good = green).
CARD_COLOR = {
    "new_bug": "Default", "priority_raised": "Warning", "reopened": "Warning",
    "assigned_to_me": "Accent", "my_status": "Accent", "review_requested": "Accent",
    "checks_failed": "Attention", "merge_conflicts": "Attention", "changes_requested": "Warning",
    "pr_approved": "Good", "pr_merged": "Good", "pr_comment": "Default",
}


class Alerter:
    def __init__(self, settings: Settings, conn):
        self.s = settings
        self.a = settings.alerts
        self.conn = conn

    def _external_ok(self, priority: str | None) -> bool:
        # A priority we can't rank is never silently dropped.
        if not self.s.known_priority(priority):
            return True
        return self.s.priority_rank(priority) >= self.s.priority_rank(self.a.external_min_priority)

    def raise_alert(self, issue: dict, kind: str, detail: str = "",
                    external: bool | None = None) -> int:
        """external=None: Teams/Slack/email only at or above external_min_priority."""
        pri = f" ({issue['priority']})" if issue.get("priority") else ""
        title = f"{TITLES.get(kind, kind)}: {issue['key']}{pri}"
        body = issue.get("summary", "")
        if detail:
            body = f"{detail} — {body}"
        message = f"{title} — {body}"
        channels = ["dashboard"]

        if self.a.desktop and desktop_notify(title, body):
            channels.append("desktop")
        if external if external is not None else self._external_ok(issue.get("priority")):
            if self.a.slack_webhook and self._slack(title, body, issue):
                channels.append("slack")
            if self.a.teams_webhook and self._teams(title, body, issue, kind, detail):
                channels.append("teams")
            if self.a.email_enabled and self.a.email_to and self._email(title, body, issue):
                channels.append("email")

        cur = self.conn.execute(
            "INSERT INTO alerts(key, ts, kind, priority, message, channels) VALUES(?,?,?,?,?,?)",
            (issue["key"], now_iso(), kind, issue.get("priority"), message, json.dumps(channels)),
        )
        self.conn.commit()
        return cur.lastrowid

    # ----------------------------------------------------------------- channels
    def _slack(self, title, body, issue) -> bool:
        payload = {
            "text": f"{title}: {body}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"*<{issue.get('url')}|{title}>*\n{body}"}},
                {"type": "context", "elements": [{"type": "mrkdwn",
                 "text": f"Status: {issue.get('status')} · Assignee: "
                         f"{issue.get('assignee') or 'Unassigned'}"}]},
            ],
        }
        return self._post(self.a.slack_webhook, payload, "Slack")

    def _teams(self, title, body, issue, kind: str = "", detail: str = "") -> bool:
        # Works with a Teams "Workflows" webhook (Power Automate), which expects an Adaptive Card.
        card = teams_card(title, issue, kind, detail, self.s.dashboard)
        payload = {"type": "message", "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}
        return self._post(self.a.teams_webhook, payload, "Teams")

    def _post(self, url, payload, name) -> bool:
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code >= 300:
                log.warning("%s webhook returned %s: %s", name, r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as e:
            log.warning("%s webhook failed: %s", name, e)
            return False

    def _email(self, title, body, issue) -> bool:
        msg = EmailMessage()
        msg["Subject"] = f"[Bug Monitor] {title}"
        msg["From"] = self.a.email_from or self.a.smtp_user
        msg["To"] = ", ".join(self.a.email_to)
        msg.set_content(
            f"{body}\n\nStatus: {issue.get('status')}\n"
            f"Assignee: {issue.get('assignee') or 'Unassigned'}\n{issue.get('url')}\n"
        )
        try:
            with smtplib.SMTP(self.a.smtp_host, self.a.smtp_port, timeout=20) as smtp:
                if self.a.smtp_starttls:
                    smtp.starttls()
                if self.a.smtp_user:
                    smtp.login(self.a.smtp_user, self.a.smtp_password)
                smtp.send_message(msg)
            return True
        except Exception as e:
            log.warning("Email alert failed: %s", e)
            return False
