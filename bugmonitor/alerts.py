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
}


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


class Alerter:
    def __init__(self, settings: Settings, conn):
        self.s = settings
        self.a = settings.alerts
        self.conn = conn

    def _external_ok(self, priority: str | None) -> bool:
        return self.s.priority_rank(priority) >= self.s.priority_rank(self.a.external_min_priority)

    def raise_alert(self, issue: dict, kind: str, detail: str = "") -> int:
        title = f"{TITLES.get(kind, kind)}: {issue['key']} ({issue.get('priority') or 'No priority'})"
        body = issue.get("summary", "")
        if detail:
            body = f"{detail} — {body}"
        message = f"{title} — {body}"
        channels = ["dashboard"]

        if self.a.desktop and desktop_notify(title, body):
            channels.append("desktop")
        if self._external_ok(issue.get("priority")):
            if self.a.slack_webhook and self._slack(title, body, issue):
                channels.append("slack")
            if self.a.teams_webhook and self._teams(title, body, issue):
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

    def _teams(self, title, body, issue) -> bool:
        # Works with a Teams "Workflows" webhook (Power Automate), which expects an Adaptive Card.
        card = {
            "type": "AdaptiveCard", "version": "1.4",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": [
                {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium",
                 "wrap": True},
                {"type": "TextBlock", "text": body, "wrap": True},
                {"type": "FactSet", "facts": [
                    {"title": "Status", "value": issue.get("status") or ""},
                    {"title": "Assignee", "value": issue.get("assignee") or "Unassigned"},
                ]},
            ],
            "actions": [{"type": "Action.OpenUrl", "title": "Open in Jira",
                         "url": issue.get("url") or ""}],
        }
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
