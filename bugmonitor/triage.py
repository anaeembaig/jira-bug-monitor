"""Triage helpers: likely duplicates, suggested component/assignee/priority, release notes.

These are fast, local heuristics (word overlap with past tickets). For judgment calls,
the Claude tools expose the same data so Claude can reason over it in conversation.
"""
from __future__ import annotations

import json
import re
from collections import Counter

from .config import Settings
from .db import row_to_dict

STOP = set("a an the to of in on for and or is are be with when after before not no it its this that from by at as "
           "into does doesn't dont don't can't cannot should page user users error issue bug fix".split())
URGENT = {"crash": "Critical", "outage": "Blocker", "down": "Critical", "data loss": "Blocker", "security": "Critical",
          "payroll": "Major", "production": "Major", "customer": "Major", "regression": "Major"}


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in STOP}


def _sim(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def duplicates(conn, settings: Settings, threshold: float = 0.5) -> list[dict]:
    bugs = [row_to_dict(r) for r in conn.execute(
        "SELECT key, summary, status, status_category, components, created, url, assignee FROM issues WHERE in_bugs=1")]
    open_ = [b for b in bugs if b["status_category"] != "done"]
    ws = {b["key"]: words(b["summary"]) for b in bugs}
    out = []
    for i, a in enumerate(open_):
        for b in bugs:
            if b["key"] == a["key"] or (b["status_category"] != "done" and b["key"] < a["key"] and b in open_[:i]):
                continue
            s = _sim(ws[a["key"]], ws[b["key"]])
            if s >= threshold:
                out.append({"key": a["key"], "summary": a["summary"], "url": a["url"], "match": b["key"],
                            "match_summary": b["summary"], "match_status": b["status"], "match_url": b["url"],
                            "score": round(s, 2)})
    seen, uniq = set(), []
    for d in sorted(out, key=lambda d: -d["score"]):
        pair = tuple(sorted((d["key"], d["match"])))
        if pair not in seen:
            seen.add(pair)
            uniq.append(d)
    return uniq[:50]


def suggestions(conn, settings: Settings, limit: int = 30) -> list[dict]:
    """For open bugs missing an assignee or component: suggestions from the most similar past tickets."""
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT key, summary, priority, assignee, components, status_category, url FROM issues WHERE in_bugs=1 OR in_team=1")]
    history = [r for r in rows if r["assignee"] or r["components"]]
    out = []
    for r in rows:
        if r["status_category"] == "done" or (r["assignee"] and r["components"]):
            continue
        w = words(r["summary"])
        near = sorted(((_sim(w, words(h["summary"])), h) for h in history if h["key"] != r["key"]), key=lambda x: -x[0])[:8]
        near = [(s, h) for s, h in near if s > 0.15]
        who = Counter(h["assignee"] for _, h in near if h["assignee"]).most_common(1)
        comp = Counter(c for _, h in near for c in h["components"] or []).most_common(1)
        text = (r["summary"] or "").lower()
        pri = next((p for k, p in URGENT.items() if k in text), None)
        if pri and settings.priority_rank(pri) <= settings.priority_rank(r["priority"]):
            pri = None
        if not (who or comp or pri):
            continue
        out.append({"key": r["key"], "summary": r["summary"], "url": r["url"], "priority": r["priority"],
                    "suggest_assignee": who[0][0] if who and not r["assignee"] else None,
                    "suggest_component": comp[0][0] if comp and not r["components"] else None,
                    "suggest_priority": pri,
                    "because": [h["key"] for _, h in near[:3]]})
    return out[:limit]


def release_notes(conn, settings: Settings, version: str | None = None, project: str | None = None,
                  deployment_id: str | None = None) -> dict:
    """Draft release notes from a Jira version or from what a deployment shipped."""
    if deployment_id:
        d = conn.execute("SELECT environment, repo, created, tickets FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        keys = json.loads(d["tickets"] or "[]") if d else []
        title = f"{d['repo'].split('/')[-1]} to {d['environment']}" if d else "Release notes"
        rows = [row_to_dict(r) for r in conn.execute(
            f"SELECT key, summary, issuetype, status, status_category FROM issues WHERE key IN ({','.join('?' * len(keys))})", keys)] if keys else []
        rows += [{"key": k, "summary": "", "issuetype": "Other", "status": None, "status_category": None}
                 for k in keys if k not in {r["key"] for r in rows}]
    else:
        rows = [row_to_dict(r) for r in conn.execute(
            "SELECT key, summary, issuetype, status, status_category, fix_versions, project FROM issues")]
        rows = [r for r in rows if version in (r["fix_versions"] or []) and (not project or r["project"] == project)]
        title = f"{project + ' ' if project else ''}{version}"
    groups = {"Fixes": [], "Improvements": [], "Other": []}
    for r in rows:
        t = (r["issuetype"] or "").lower()
        g = "Fixes" if t == "bug" else "Improvements" if t in ("story", "improvement", "feature", "new feature", "epic") else "Other"
        groups[g].append(r)
    text = [f"Release notes: {title}", ""]
    for g, items in groups.items():
        if items:
            text.append(g)
            text += [f"- {r['summary'] or r['key']} ({r['key']})" for r in sorted(items, key=lambda r: r["key"])]
            text.append("")
    not_done = [r["key"] for r in rows if r.get("status_category") and r["status_category"] != "done"]
    return {"title": title, "tickets": len(rows), "groups": {k: [r["key"] for r in v] for k, v in groups.items()},
            "text": "\n".join(text).strip(), "not_done": not_done}
