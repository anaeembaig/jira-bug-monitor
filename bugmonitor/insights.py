"""Engineering insights: cycle time by stage, PR health, bug quality, estimate accuracy, throughput."""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone

from .analytics import parse_ts, status_categories, status_durations
from .config import Settings
from .db import row_to_dict


def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def _p(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, int(round((len(xs) - 1) * q)))], 1)


def cycle_time(conn, settings: Settings, days: int = 30) -> dict:
    """For tickets finished in the window: median hours in each status, and total cycle time."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    cats = status_categories(conn)
    done = [r for r in conn.execute(
        "SELECT key, issuetype, created, resolved FROM issues WHERE status_category='done' "
        "AND (in_team=1 OR in_bugs=1 OR in_mine=1)") if (parse_ts(r["resolved"]) or since) >= since]
    per_status: dict = {}
    cycle, lead, by_type = [], [], {}
    for r in done:
        d = status_durations(conn, r["key"])
        for st, sec in d["by_status"].items():
            if cats.get(st) != "done":
                per_status.setdefault(st, []).append(sec / 3600)
        if d["active_sec"]:
            cycle.append(d["active_sec"] / 3600)
        c, rs = parse_ts(r["created"]), parse_ts(r["resolved"])
        if c and rs:
            lead.append((rs - c).total_seconds() / 3600)
            by_type.setdefault(r["issuetype"] or "Other", []).append((rs - c).total_seconds() / 3600)
    stages = sorted(({"status": st, "category": cats.get(st), "median_hours": _med(v), "p80_hours": _p(v, 0.8),
                      "tickets": len(v)} for st, v in per_status.items()),
                    key=lambda x: ({"new": 0, "indeterminate": 1}.get(x["category"], 2), -(x["median_hours"] or 0)))
    return {"days": days, "finished": len(done), "stages": stages,
            "cycle_median_hours": _med(cycle), "cycle_p80_hours": _p(cycle, 0.8),
            "lead_median_hours": _med(lead), "lead_p80_hours": _p(lead, 0.8),
            "by_type": [{"type": k, "median_hours": _med(v), "tickets": len(v)} for k, v in sorted(by_type.items())]}


def pr_health(conn, settings: Settings, days: int = 30) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    prs = [row_to_dict(r) for r in conn.execute("SELECT * FROM github_prs WHERE created >= ?", (since,))]
    first_review, to_merge, rounds, sizes, by_author = [], [], [], [], {}
    for p in prs:
        det = p.get("details") or {}
        created = parse_ts(p["created"])
        reviews = [c for c in det.get("conversation") or [] if c.get("kind") in ("review", "thread")
                   and (c.get("author") or "").lower() != (p["author"] or "").lower()]
        times = [parse_ts(c["at"]) for c in reviews if c.get("at")]
        if created and times:
            first_review.append((min(times) - created).total_seconds() / 3600)
        if created and p["merged_at"]:
            to_merge.append((parse_ts(p["merged_at"]) - created).total_seconds() / 3600)
        rounds.append(sum(1 for c in det.get("conversation") or [] if c.get("kind") == "review"
                          and c.get("state") == "CHANGES_REQUESTED"))
        size = (p["additions"] or 0) + (p["deletions"] or 0)
        sizes.append(size)
        a = by_author.setdefault(p["author"], {"author": p["author"], "opened": 0, "merged": 0})
        a["opened"] += 1
        a["merged"] += int(p["state"] == "MERGED")
    buckets = [("Small (<100 lines)", 0, 99), ("Medium (100-499)", 100, 499), ("Large (500-999)", 500, 999),
               ("Huge (1000+)", 1000, 10 ** 9)]
    return {"days": days, "prs": len(prs), "merged": sum(1 for p in prs if p["state"] == "MERGED"),
            "first_review_median_hours": _med(first_review), "merge_median_hours": _med(to_merge),
            "changes_requested_rate": round(sum(1 for r in rounds if r) / len(rounds) * 100) if rounds else None,
            "median_size": _med(sizes),
            "sizes": [{"label": l, "n": sum(1 for s in sizes if lo <= s <= hi)} for l, lo, hi in buckets],
            "by_author": sorted(by_author.values(), key=lambda a: -a["opened"])}


def bug_quality(conn, settings: Settings, days: int = 60) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    bugs = [row_to_dict(r) for r in conn.execute("SELECT * FROM issues WHERE in_bugs=1")]
    bugs = [b for b in bugs if (parse_ts(b["created"]) or since) >= since]
    resolved = [b for b in bugs if b["status_category"] == "done"]
    reopened = [b for b in bugs if (b["reopen_count"] or 0) > 0]
    esc = {l.lower() for l in settings.team.escape_labels}
    escaped = [b for b in bugs if esc & {l.lower() for l in b["labels"] or []}]
    comp: dict = {}
    for b in bugs:
        for c in b["components"] or ["(none)"]:
            x = comp.setdefault(c, {"label": c, "n": 0, "open": 0})
            x["n"] += 1
            x["open"] += int(b["status_category"] != "done")
    weekly: dict = {}
    for b in bugs:
        c = parse_ts(b["created"])
        if c:
            wk = (c.date() - timedelta(days=c.weekday())).isoformat()
            weekly.setdefault(wk, {"week": wk, "created": 0, "escaped": 0})
            weekly[wk]["created"] += 1
            weekly[wk]["escaped"] += int(b in escaped)
    return {"days": days, "bugs": len(bugs), "resolved": len(resolved),
            "reopen_rate": round(len(reopened) / len(resolved) * 100) if resolved else None,
            "escape_rate": round(len(escaped) / len(bugs) * 100) if bugs else None,
            "escaped": [{"key": b["key"], "summary": b["summary"], "labels": b["labels"]} for b in escaped[:20]],
            "by_component": sorted(comp.values(), key=lambda x: -x["n"])[:10],
            "weekly": sorted(weekly.values(), key=lambda w: w["week"])}


def estimate_accuracy(conn, settings: Settings, days: int = 60) -> dict:
    """Jira original estimate vs. logged time (or hands-on time when nothing was logged)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = [r for r in conn.execute(
        "SELECT key, issuetype, assignee, original_estimate_sec, time_spent_sec, resolved FROM issues "
        "WHERE status_category='done' AND original_estimate_sec > 0") if (parse_ts(r["resolved"]) or since) >= since]
    items, ratios = [], []
    for r in rows:
        actual = r["time_spent_sec"] or status_durations(conn, r["key"])["active_sec"]
        if not actual:
            continue
        ratio = actual / r["original_estimate_sec"]
        ratios.append(ratio)
        items.append({"key": r["key"], "type": r["issuetype"], "assignee": r["assignee"],
                      "estimate_hours": round(r["original_estimate_sec"] / 3600, 1),
                      "actual_hours": round(actual / 3600, 1), "ratio": round(ratio, 2),
                      "source": "logged" if r["time_spent_sec"] else "hands-on"})
    within = sum(1 for x in ratios if 0.75 <= x <= 1.33)
    return {"days": days, "tickets": len(items), "median_ratio": _med(ratios),
            "within_25pct": round(within / len(ratios) * 100) if ratios else None,
            "over": sum(1 for x in ratios if x > 1.33), "under": sum(1 for x in ratios if x < 0.75),
            "worst": sorted(items, key=lambda i: -abs(i["ratio"] - 1))[:10]}


def throughput(conn, settings: Settings, weeks: int = 8) -> dict:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(weeks=weeks)).date()
    start -= timedelta(days=start.weekday())
    out = {}
    w = start
    while w <= now.date():
        out[w.isoformat()] = {"week": w.isoformat(), "resolved": 0, "points": 0.0}
        w += timedelta(days=7)
    for r in conn.execute("SELECT resolved, story_points FROM issues WHERE status_category='done' AND (in_team=1 OR in_mine=1)"):
        t = parse_ts(r["resolved"])
        if t:
            wk = (t.date() - timedelta(days=t.weekday())).isoformat()
            if wk in out:
                out[wk]["resolved"] += 1
                out[wk]["points"] += r["story_points"] or 0
    return {"weeks": list(out.values())}


def all_insights(conn, settings: Settings, days: int = 30) -> dict:
    from .deployments import dora
    return {"cycle": cycle_time(conn, settings, days), "prs": pr_health(conn, settings, days),
            "bugs": bug_quality(conn, settings, max(days, 60)), "estimates": estimate_accuracy(conn, settings, max(days, 60)),
            "throughput": throughput(conn, settings), "delivery": dora(conn, settings, days)}
