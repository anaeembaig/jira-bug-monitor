"""Read and change settings from the dashboard. Secrets never leave this module in clear text."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from . import vault
from .config import Settings, load_settings
from .db import now_iso
from .notify_setup import set_config_values

# Simple settings the dashboard may change: section -> {key: type}
EDITABLE = {
    "alerts": {"new_bug": bool, "priority_raised": bool, "reopened": bool, "assigned_to_me": bool,
               "my_status_changes": bool, "mentions": bool, "stuck_reminders": bool, "desktop": bool,
               "external_min_priority": str},
    "notify": {"quiet_hours": str, "quiet_weekends": bool, "urgent_bypasses_quiet": bool, "digest_time": str,
               "digest_personal": bool, "digest_team": bool, "weekly_report_day": str, "sync_health": bool,
               "standup_time": str, "standup_team": bool},
    "timelog": {"enabled": bool, "daily_target_hours": float, "reminder_time": str, "recap_in_digest": bool,
                "workdays": list, "allow_jira_write": bool},
    "deployments": {"alert_my_tickets": bool, "alert_failures": bool, "alert_rollbacks": bool},
    "team": {"name": str, "pr_review_hours": float, "focus_hours_per_day": float, "hours_per_point": float},
    "github": {"alerts_to_external": bool},
    "sla": {"enabled": bool, "warn_at": float, "route": str},
    "quality": {"enabled": bool, "min_description_words": int, "require_steps": bool, "require_expected": bool,
                "require_environment": bool, "require_version": bool},
    "code_health": {"ci": bool, "alert_ci_failures": bool, "security": bool, "alert_security": bool,
                    "security_min_severity": str},
    "jenkins": {"enabled": bool, "url": str, "user": str, "alert_failures": bool, "fetch_tests": bool, "days": int},
    "reports": {"monthly": bool, "sprint_review": bool, "time": str},
}
SEVERITIES = ("low", "medium", "high", "critical")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
NAME_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,40}$")


def public(settings: Settings) -> dict:
    """Everything the Settings tab shows. Webhook URLs and tokens are reduced to 'set' or 'not set'."""
    section = {"alerts": settings.alerts, "notify": settings.notify, "timelog": settings.timelog,
               "deployments": settings.deploy, "team": settings.team, "github": settings.github,
               "sla": settings.sla, "quality": settings.quality, "code_health": settings.code,
               "jenkins": settings.jenkins, "reports": settings.reports}
    values = {s: {k: getattr(obj, k) for k in EDITABLE[s]} for s, obj in section.items()}
    return {
        "values": values,
        "routes": [{"name": r.name, "projects": r.projects, "events": r.events, "min_priority": r.min_priority,
                    "digest": r.digest, "mention_assignee": r.mention_assignee, "webhook_set": bool(r.webhook)}
                   for r in settings.routes],
        "members": settings.team.members,
        "time_off": [{k: str(t.get(k) or "") for k in ("name", "start", "end", "hours")} for t in settings.team.time_off],
        "escalations": [{"name": e.name, "min_priority": e.min_priority, "unassigned_hours": e.unassigned_hours,
                         "open_hours": e.open_hours, "projects": e.projects, "route": e.route}
                        for e in settings.escalations],
        "secrets": {k: bool(v) for k, v in settings.secret_sources.items()},
        "private_webhook_set": bool(settings.alerts.teams_webhook),
        "priorities": settings.priority_order,
        "events": ["new_bug", "priority_raised", "reopened", "escalation", "stuck", "deployment", "sla", "ci", "security"],
        "config_file": str(settings.config_file),
    }


def _coerce(kind, v):
    if kind is bool:
        return bool(v)
    if kind is float:
        f = float(v)
        if not 0 <= f <= 1000:
            raise ValueError("number out of range")
        return f
    if kind is int:
        i = int(float(v))
        if not 0 <= i <= 10000:
            raise ValueError("number out of range")
        return i
    if kind is list:
        return [str(x)[:20] for x in (v or [])][:7]
    return str(v or "")[:200]


def _validate(section: str, key: str, value):
    if key == "workdays" and not value:
        raise ValueError("Pick at least one workday")
    if key == "warn_at" and not 0.1 <= value <= 1:
        raise ValueError("Warn at must be between 0.1 and 1 (for example 0.8 = 80% of the time used)")
    if key == "security_min_severity" and value not in SEVERITIES:
        raise ValueError("Security severity must be low, medium, high, or critical")
    if section == "jenkins" and key == "url" and value and not re.match(r"^https://[^\s/]+(/[^\s]*)?$", value):
        raise ValueError("Jenkins address must start with https:// (to use http, edit the settings file)")
    if (key in ("digest_time", "reminder_time", "standup_time") or (section == "reports" and key == "time")) \
            and value and not TIME_RE.match(value):
        raise ValueError(f"{key} must look like 09:30")
    if key == "quiet_hours" and value:
        parts = value.split("-")
        if len(parts) != 2 or not all(TIME_RE.match(p.strip()) for p in parts):
            raise ValueError("quiet_hours must look like 20:00-08:00")


def _replace_table_array(path: Path, name: str, items: list[dict]) -> None:
    from .tomledit import set_table_array
    set_table_array(path, name, items)


def save(conn, settings: Settings, body: dict) -> dict:
    path = settings.config_file
    if not path or not path.exists():
        return {"error": "No settings file found. Run bugmonitor init first."}
    changed: list[str] = []
    original = path.read_text(encoding="utf-8")
    try:
        updates: dict = {}
        for section, kv in (body.get("values") or {}).items():
            allowed = EDITABLE.get(section, {})
            for key, value in (kv or {}).items():
                if key not in allowed:
                    continue
                v = _coerce(allowed[key], value)
                _validate(section, key, v)
                updates.setdefault(section, {})[key] = v
        if updates:
            changed += set_config_values(path, updates)

        if "private_webhook" in body and body["private_webhook"]:
            if vault.set_secret("teams_webhook", body["private_webhook"].strip()):
                set_config_values(path, {"alerts": {"teams_webhook": ""}})
            else:
                set_config_values(path, {"alerts": {"teams_webhook": body["private_webhook"].strip()}})
            changed.append("private Teams webhook")

        if "routes" in body:
            items = []
            existing = {r.name: r for r in settings.routes}
            for r in body["routes"]:
                name = (r.get("name") or "").strip()
                if not NAME_RE.match(name):
                    raise ValueError(f"Route name '{name}' must be 1-40 letters, numbers, spaces, - _ .")
                hook = (r.get("webhook") or "").strip()
                if hook and not hook.startswith("https://"):
                    raise ValueError(f"Route '{name}': the webhook must start with https://")
                ref = f"keychain:route-{name}"
                if hook:
                    if not vault.set_secret(f"route-{name}", hook):
                        ref = hook  # no credential store: keep it in the file
                elif name in existing and existing[name].webhook:
                    old = _raw_route_webhook(path, name)
                    ref = old or ref
                else:
                    raise ValueError(f"Route '{name}' needs a webhook URL")
                items.append({"name": name, "webhook": ref,
                              "projects": [p.strip().upper() for p in r.get("projects") or [] if p.strip()],
                              "events": [e for e in r.get("events") or [] if e in public(settings)["events"]],
                              "min_priority": r.get("min_priority") or "", "digest": bool(r.get("digest")),
                              "mention_assignee": bool(r.get("mention_assignee", True))})
            _replace_table_array(path, "routes", items)
            changed.append(f"{len(items)} team channel(s)")

        if "members" in body:
            members = [{"name": (m.get("name") or "").strip()[:80], "github": (m.get("github") or "").strip()[:40],
                        "email": (m.get("email") or "").strip()[:120]} for m in body["members"] if (m.get("name") or "").strip()]
            _replace_table_array(path, "team.members", members)
            changed.append(f"{len(members)} teammate(s)")

        if "time_off" in body:
            from datetime import date
            rows = []
            for t in body["time_off"]:
                name = (t.get("name") or "").strip()[:80]
                if not name:
                    continue
                try:
                    a = date.fromisoformat(str(t.get("start") or ""))
                    b = date.fromisoformat(str(t.get("end") or t.get("start")))
                except ValueError:
                    raise ValueError(f"Time off for {name} needs dates like 2026-11-02")
                if b < a:
                    raise ValueError(f"Time off for {name} ends before it starts")
                row = {"name": name, "start": a.isoformat(), "end": b.isoformat()}
                if str(t.get("hours") or "").strip():
                    row["hours"] = float(t["hours"])
                rows.append(row)
            _replace_table_array(path, "team.time_off", rows)
            changed.append(f"{len(rows)} time off entr{'y' if len(rows) == 1 else 'ies'}")

        if "escalations" in body:
            esc = [{"name": (e.get("name") or "Escalation").strip()[:60], "min_priority": e.get("min_priority") or "Critical",
                    "unassigned_hours": float(e.get("unassigned_hours") or 0), "open_hours": float(e.get("open_hours") or 0),
                    "projects": [p.strip().upper() for p in e.get("projects") or [] if p.strip()],
                    "route": (e.get("route") or "").strip()} for e in body["escalations"]]
            _replace_table_array(path, "escalations", esc)
            changed.append(f"{len(esc)} escalation rule(s)")
    except (ValueError, TypeError, AssertionError, tomllib.TOMLDecodeError) as e:
        path.write_text(original, encoding="utf-8")   # all or nothing
        return {"error": str(e)}

    reload_into(settings)
    conn.execute("INSERT INTO audit(ts, actor, action, detail) VALUES(?,?,?,?)",
                 (now_iso(), "dashboard", "settings", json.dumps(changed)[:1000]))
    conn.commit()
    return {"ok": True, "changed": changed, "settings": public(settings)}


def _raw_route_webhook(path: Path, name: str) -> str:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return next((r.get("webhook", "") for r in raw.get("routes", []) if r.get("name") == name), "")


def reload_into(settings: Settings) -> None:
    """Apply saved changes to the running app (poller and dashboard share this object)."""
    fresh = load_settings(settings.config_file)
    settings.__dict__.update(fresh.__dict__)


def test_route(settings: Settings, name: str) -> dict:
    from .alerts import Alerter, summary_card
    from .db import connect
    r = settings.route(name)
    if not r or not r.webhook:
        return {"error": f"No webhook for '{name}'"}
    card = summary_card(f"Bug Monitor is connected to {name}",
                        [(f"This channel will get: {', '.join(r.events)}", f"Projects: {', '.join(r.projects) or 'all'}", None)],
                        settings.dashboard, "Good")
    ok = Alerter(settings, connect(settings.db_path)).post_card(r.webhook, card)
    return {"ok": ok} if ok else {"error": "Teams didn't accept the message. Check the webhook URL."}
