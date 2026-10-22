"""Urgency scoring, time-in-status math, and fix-time estimates."""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone

from .config import Settings
from .db import get_state

URGENT_WORDS = (
    "customer", "escalat", "regression", "production", "prod ", "outage",
    "security", "sla", "data loss", "crash", "payroll", "down",
)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    # Jira sends "+0500" style offsets; Python wants "+05:00".
    if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- urgency
def urgency_score(issue: dict, settings: Settings, now: datetime | None = None) -> float:
    """0-100. Weights: priority 55, age 10, unassigned 10, urgent keywords 15,
    reopens 10, stale (no update in 7+ days) 5. Tune here to match your team."""
    if issue.get("status_category") == "done":
        return 0.0
    now = now or _now()
    levels = max(len(settings.priority_order), 1)
    score = settings.priority_rank(issue.get("priority")) / levels * 55

    created = parse_ts(issue.get("created"))
    if created:
        score += min((now - created).days, 30) / 30 * 10

    if not issue.get("assignee"):
        score += 10

    labels = issue.get("labels") or []
    if isinstance(labels, str):
        labels = json.loads(labels or "[]")
    text = f" {issue.get('summary', '')} {' '.join(labels)} ".lower()
    score += min(sum(5 for w in URGENT_WORDS if w in text), 15)

    score += min((issue.get("reopen_count") or 0) * 5, 10)

    updated = parse_ts(issue.get("updated"))
    if updated and (now - updated).days >= 7:
        score += 5
    return round(min(score, 100.0), 1)


# --------------------------------------------------------------------------- time in status
def status_categories(conn) -> dict[str, str]:
    return json.loads(get_state(conn, "status_categories", "{}"))


def status_durations(conn, key: str, now: datetime | None = None) -> dict:
    """Seconds spent in each status, plus total 'active' (in-progress category) time."""
    now = now or _now()
    issue = conn.execute(
        "SELECT created, resolved, status, status_category FROM issues WHERE key=?", (key,)
    ).fetchone()
    if not issue:
        return {"by_status": {}, "active_sec": 0, "has_history": False}
    cats = status_categories(conn)
    changes = conn.execute(
        "SELECT ts, from_val, to_val FROM events WHERE key=? AND kind='status' ORDER BY ts",
        (key,),
    ).fetchall()

    start = parse_ts(issue["created"]) or now
    current = changes[0]["from_val"] if changes else issue["status"]
    by_status: dict[str, float] = {}
    for ch in changes:
        t = parse_ts(ch["ts"]) or start
        by_status[current] = by_status.get(current, 0) + max((t - start).total_seconds(), 0)
        start, current = t, ch["to_val"]
    end = parse_ts(issue["resolved"]) if issue["status_category"] == "done" else now
    end = end or now
    by_status[current] = by_status.get(current, 0) + max((end - start).total_seconds(), 0)

    active = sum(v for s, v in by_status.items() if cats.get(s) == "indeterminate")
    return {"by_status": by_status, "active_sec": active, "has_history": bool(changes)}


# --------------------------------------------------------------------------- estimates
def _pct(values: list[float], p: float) -> float:
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    idx = (len(values) - 1) * p
    lo, hi = int(idx), min(int(idx) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (idx - lo)


def _fmt_hours(h: float) -> str:
    if h < 1:
        return f"{round(h * 60)} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} days"


def estimate_fix(conn, key: str, min_samples: int = 5) -> dict:
    issue = conn.execute("SELECT * FROM issues WHERE key=?", (key,)).fetchone()
    if not issue:
        return {"key": key, "error": "Ticket not found in local store. Run sync first."}
    comps = set(json.loads(issue["components"] or "[]"))

    resolved = conn.execute(
        "SELECT key, project, priority, components, created, resolved FROM issues "
        "WHERE status_category='done' AND resolved IS NOT NULL AND key != ?",
        (key,),
    ).fetchall()

    def tier(name, pred):
        return name, [r for r in resolved if pred(r)]

    tiers = [
        tier("same project, component, and priority", lambda r: r["project"] == issue["project"]
             and r["priority"] == issue["priority"]
             and comps & set(json.loads(r["components"] or "[]"))),
        tier("same project and priority", lambda r: r["project"] == issue["project"]
             and r["priority"] == issue["priority"]),
        tier("same project", lambda r: r["project"] == issue["project"]),
        tier("all resolved bugs", lambda r: True),
    ]
    basis, sample = next(((n, s) for n, s in tiers if len(s) >= min_samples), (None, []))
    if not sample:
        basis, sample = max(tiers, key=lambda t: len(t[1]))

    result: dict = {
        "key": key,
        "priority": issue["priority"],
        "status": issue["status"],
        "jira_original_estimate_h": round(issue["original_estimate_sec"] / 3600, 1)
        if issue["original_estimate_sec"] else None,
        "basis": basis,
        "sample_size": len(sample),
    }
    if not sample:
        result["summary"] = (
            "No resolved bugs in history yet, so no data-based estimate. "
            + (f"Jira original estimate: {result['jira_original_estimate_h']} h."
               if result["jira_original_estimate_h"] else "")
        ).strip()
        result["confidence"] = "none"
        return result

    active, cycle = [], []
    for r in sample:
        c, rs = parse_ts(r["created"]), parse_ts(r["resolved"])
        if c and rs:
            cycle.append((rs - c).total_seconds() / 3600)
        d = status_durations(conn, r["key"])
        if d["has_history"] and d["active_sec"] > 0:
            active.append(d["active_sec"] / 3600)

    use_active = len(active) >= max(3, len(sample) // 2)
    hours = active if use_active else cycle
    metric = "hands-on time (in-progress statuses)" if use_active else "open-to-resolved time"
    p50, p80 = _pct(hours, 0.5), _pct(hours, 0.8)

    mine = status_durations(conn, key)
    elapsed = (mine["active_sec"] if use_active else
               (_now() - (parse_ts(issue["created"]) or _now())).total_seconds()) / 3600
    remaining_p50 = max(p50 - elapsed, 0)
    remaining_p80 = max(p80 - elapsed, 0)

    n = len(sample)
    confidence = ("high" if n >= 20 and basis.startswith("same project,") or n >= 30
                  else "medium" if n >= min_samples else "low")

    result.update({
        "metric": metric,
        "typical_h": round(p50, 1),
        "p80_h": round(p80, 1),
        "elapsed_h": round(elapsed, 1),
        "remaining_typical_h": round(remaining_p50, 1),
        "remaining_p80_h": round(remaining_p80, 1),
        "confidence": confidence,
        "cycle_median_h": round(statistics.median(cycle), 1) if cycle else None,
    })
    over = elapsed > p80
    result["summary"] = (
        f"Similar bugs ({basis}, n={n}) take about {_fmt_hours(p50)} of {metric}; "
        f"8 in 10 finish within {_fmt_hours(p80)}. "
        + (f"This one has used {_fmt_hours(elapsed)} and is past the usual range — worth a check-in."
           if over else
           f"About {_fmt_hours(remaining_p50)} likely remains "
           f"(up to {_fmt_hours(remaining_p80)}).")
        + f" Confidence: {confidence}."
    )
    if result["jira_original_estimate_h"]:
        result["summary"] += f" Jira original estimate: {result['jira_original_estimate_h']} h."
    return result
