"""Alert delivery: dashboard (database), desktop, Slack, Teams, and email."""
from __future__ import annotations

import json
import logging
from datetime import datetime
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
    "mentioned": "You were mentioned",
    "stuck": "Stuck",
    "escalation": "Escalation",
    "sync_failing": "Bug Monitor can't sync",
    "timelog": "Time log",
    "deployed": "Deployed",
    "deploy_failed": "Deploy failed",
    "rollback": "Rollback",
    "review_requested": "Review requested",
    "checks_failed": "Checks failing",
    "pr_approved": "PR approved",
    "changes_requested": "Changes requested",
    "pr_merged": "PR merged",
    "pr_comment": "New comment on your PR",
    "merge_conflicts": "Merge conflicts",
    "watched": "Watched ticket changed",
    "sla_warning": "SLA due soon",
    "sla_breach": "SLA breached",
    "ci_failed": "Build broken",
    "security": "Security alert",
}


def summary_card(heading: str, items: list[tuple], dash, color: str = "Default") -> dict:
    body = [{"type": "TextBlock", "text": heading, "weight": "Bolder", "size": "Medium", "wrap": True,
             "color": color}]
    for title, text, url in items[:15]:
        line = f"[{title}]({url})" if url else title
        body.append({"type": "TextBlock", "text": line, "wrap": True, "spacing": "Small"})
        if text:
            body.append({"type": "TextBlock", "text": text, "wrap": True, "isSubtle": True,
                         "spacing": "None", "maxLines": 2})
    if len(items) > 15:
        body.append({"type": "TextBlock", "text": f"…and {len(items) - 15} more on the dashboard",
                     "isSubtle": True, "wrap": True})
    return {"type": "AdaptiveCard", "version": "1.4",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "body": body,
            "actions": [{"type": "Action.OpenUrl", "title": "Open dashboard",
                         "url": f"http://{dash.host}:{dash.port}"}]}


def teams_card(title: str, issue: dict, kind: str, detail: str, dash, mention: dict | None = None) -> dict:
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
    card = {"type": "AdaptiveCard", "version": "1.4",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": body, "actions": actions}
    if mention:
        tag = f"<at>{mention['name']}</at>"
        body.append({"type": "TextBlock", "text": f"Assigned to {tag}", "wrap": True})
        card["msteams"] = {"entities": [{"type": "mention", "text": tag,
                                         "mentioned": {"id": mention["email"], "name": mention["name"]}}]}
    return card


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
    "mentioned": "Accent", "stuck": "Warning", "escalation": "Attention", "sync_failing": "Attention",
    "deployed": "Good", "deploy_failed": "Attention", "rollback": "Warning",
    "watched": "Default", "sla_warning": "Warning", "sla_breach": "Attention", "ci_failed": "Attention",
    "security": "Attention",
}


# Events that go to team channels ([[routes]]); everything else is personal.
TEAM_EVENTS = {"new_bug", "priority_raised", "reopened", "escalation", "stuck",
               "deployed", "deploy_failed", "rollback", "sla_breach", "ci_failed", "security"}
DEPLOY_KINDS = {"deployed", "deploy_failed", "rollback"}  # all match a route's "deployment" event
# The event name a route lists in `events` for each alert kind (default: the kind itself).
ROUTE_EVENT = {**{k: "deployment" for k in DEPLOY_KINDS}, "sla_breach": "sla", "ci_failed": "ci"}
# Kinds a route sends regardless of its min_priority.
ANY_PRIORITY = {"escalation", "stuck", "sla_breach", "ci_failed", "security"} | DEPLOY_KINDS
REPO_KINDS = {"ci_failed", "security"}      # about a repo, not a Jira project: every route with the event gets them


def _hhmm(text: str):
    h, m = (text.strip() + ":0").split(":")[:2]
    return int(h) * 60 + int(m)


def in_quiet_hours(notify, now: datetime | None = None) -> bool:
    now = now or datetime.now()  # local time on purpose
    if notify.quiet_weekends and now.weekday() >= 5:
        return True
    if not notify.quiet_hours or "-" not in notify.quiet_hours:
        return False
    start, end = (_hhmm(x) for x in notify.quiet_hours.split("-", 1))
    t = now.hour * 60 + now.minute
    return (start <= t < end) if start < end else (t >= start or t < end)


class Alerter:
    def __init__(self, settings: Settings, conn):
        self.s = settings
        self.conn = conn

    @property
    def a(self):
        # Read live so a settings reload from the dashboard takes effect right away.
        return self.s.alerts

    def _external_ok(self, priority: str | None) -> bool:
        # A priority we can't rank is never silently dropped.
        return self.s.at_least(priority, self.a.external_min_priority)

    def _urgent(self, kind: str, issue: dict) -> bool:
        return kind in ("escalation", "sync_failing", "deploy_failed", "rollback", "sla_breach") or (
            kind in ("new_bug", "reopened", "priority_raised") and self.s.is_high(issue.get("priority")))

    def _snoozed(self, key: str | None) -> bool:
        if not key:
            return False
        r = self.conn.execute("SELECT until FROM snoozes WHERE key=?", (key,)).fetchone()
        return bool(r) and (r["until"] or "") > now_iso()

    def _routes_for(self, issue: dict, kind: str, route: str | None) -> list:
        if route is not None:
            r = self.s.route(route)
            return [r] if r and r.webhook else []
        if kind not in TEAM_EVENTS:
            return []
        projects = set(issue.get("projects") or [issue.get("project") or (issue.get("key") or "").split("-")[0]])
        out = []
        for r in self.s.routes:
            event = ROUTE_EVENT.get(kind, kind)
            if not r.webhook or event not in r.events:
                continue
            if r.projects and kind not in REPO_KINDS and not projects & {p.upper() for p in r.projects}:
                continue
            if kind not in ANY_PRIORITY and not self.s.at_least(
                    issue.get("priority"), r.min_priority or self.a.external_min_priority):
                continue
            out.append(r)
        return out

    def raise_alert(self, issue: dict, kind: str, detail: str = "", external: bool | None = None,
                    personal: bool = True, route: str | None = None, team: bool = True) -> int:
        """Record an alert and deliver it.

        personal: also send to your own desktop/Teams/Slack/email.
          external=None means Teams/Slack/email only at or above external_min_priority.
        Team channels ([[routes]]) get team events that match their projects and priority,
        or exactly `route` when one is named (escalations).
        """
        pri = f" ({issue['priority']})" if issue.get("priority") else ""
        title = f"{TITLES.get(kind, kind)}: {issue['key']}{pri}"
        body = issue.get("summary", "")
        if detail:
            body = f"{detail} — {body}"
        message = f"{title} — {body}"
        channels = ["dashboard"]
        urgent = self._urgent(kind, issue)

        if personal and self._snoozed(issue.get("key")):
            personal = False                    # muted for you; team channels still hear about it
            channels.append("snoozed")
        if personal:
            if self.a.desktop and desktop_notify(title, body):
                channels.append("desktop")
            if external if external is not None else self._external_ok(issue.get("priority")):
                channels += self._deliver("private", title, body, issue, kind, detail, urgent)
        for r in (self._routes_for(issue, kind, route) if team else []):
            channels += self._deliver(f"route:{r.name}", title, body, issue, kind, detail, urgent, r)

        cur = self.conn.execute(
            "INSERT INTO alerts(key, ts, kind, priority, message, channels) VALUES(?,?,?,?,?,?)",
            (issue["key"], now_iso(), kind, issue.get("priority"), message, json.dumps(channels)),
        )
        self.conn.commit()
        return cur.lastrowid

    def _deliver(self, target, title, body, issue, kind, detail, urgent, route=None) -> list[str]:
        n = self.s.notify
        if in_quiet_hours(n) and not (urgent and n.urgent_bypasses_quiet):
            self.conn.execute("INSERT INTO outbox(ts, target, title, body, url) VALUES(?,?,?,?,?)",
                              (now_iso(), target, title, body, issue.get("url")))
            return [f"{target.replace('private', 'teams')} (held for quiet hours)"]
        if route is not None:
            mention = None
            if route.mention_assignee and issue.get("assignee"):
                m = self.s.member(name=issue["assignee"])
                if m and m.get("email"):
                    mention = {"name": issue["assignee"], "email": m["email"]}
            card = teams_card(title, issue, kind, detail, self.s.dashboard, mention)
            return [f"teams:{route.name}"] if self.post_card(route.webhook, card) else []
        sent = []
        if self.a.slack_webhook and self._slack(title, body, issue):
            sent.append("slack")
        if self.a.teams_webhook and self._teams(title, body, issue, kind, detail):
            sent.append("teams")
        if self.a.email_enabled and self.a.email_to and self._email(title, body, issue):
            sent.append("email")
        return sent

    def post_card(self, webhook: str, card: dict) -> bool:
        payload = {"type": "message", "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}
        return self._post(webhook, payload, "Teams")

    def flush_outbox(self) -> int:
        """After quiet hours: one summary message per target instead of a burst."""
        if in_quiet_hours(self.s.notify):
            return 0
        rows = self.conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()
        by_target: dict = {}
        for r in rows:
            by_target.setdefault(r["target"], []).append(r)
        sent = 0
        for target, items in by_target.items():
            title = f"While notifications were paused: {len(items)} update{'s' * (len(items) != 1)}"
            card = summary_card(title, [(i["title"], i["body"], i["url"]) for i in items], self.s.dashboard)
            if target == "private":
                has_target = bool(self.a.teams_webhook or self.a.slack_webhook
                                  or (self.a.email_enabled and self.a.email_to))
                ok = False
                if self.a.teams_webhook:
                    ok = self.post_card(self.a.teams_webhook, card) or ok
                lines = "\n".join(f"- {i['title']}: {i['body']}" + (f" ({i['url']})" if i["url"] else "")
                                   for i in items)
                if self.a.slack_webhook:
                    ok = self._post(self.a.slack_webhook, {"text": f"{title}\n{lines}"}, "Slack") or ok
                if self.a.email_enabled and self.a.email_to:
                    ok = self._email(title, lines, None) or ok
            else:
                r = self.s.route(target.split(":", 1)[1])
                has_target = bool(r and r.webhook)
                ok = has_target and self.post_card(r.webhook, card)
            # Keep held messages until they're delivered; drop them only if their destination is gone.
            if ok or not has_target:
                self.conn.execute(f"DELETE FROM outbox WHERE id IN ({','.join('?' * len(items))})",
                                  [i["id"] for i in items])
                sent += int(ok)
        self.conn.commit()
        return sent

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
        msg.set_content(body + "\n" if issue is None else
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
