"""Pull bugs from Jira into the local store, record history, and raise alerts."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from .alerts import Alerter
from .analytics import parse_ts, status_categories, urgency_score
from .config import Settings
from .db import get_state, now_iso, row_to_dict, set_state
from .jira_client import JiraClient
from .locks import FileLock

log = logging.getLogger("bugmonitor.sync")
TRACKED_FIELDS = {"status": "status", "assignee": "assignee", "priority": "priority", "Sprint": "sprint",
                  "resolution": "resolution"}


def _name(obj, attr="name"):
    return obj.get(attr) if isinstance(obj, dict) else None


def map_issue(raw: dict, client: JiraClient, custom: dict | None = None) -> dict:
    f = raw.get("fields", {})
    status = f.get("status") or {}
    custom = custom or {}
    sprints = f.get(custom.get("sprint", ""), None) or []
    points = f.get(custom.get("points", ""), None)
    return {
        "sprint_ids": [s_.get("id") for s_ in sprints if isinstance(s_, dict)],
        "_sprints": [s_ for s_ in sprints if isinstance(s_, dict)],
        "story_points": points if isinstance(points, (int, float)) else None,
        "fix_versions": [v.get("name") for v in f.get("fixVersions") or [] if isinstance(v, dict)],
        "_comments": ((f.get("comment") or {}).get("comments") or []),
        "description": _rich_text(f.get("description"))[:6000] or None,
        "environment": _rich_text(f.get("environment"))[:1000] or None,
        "affects_versions": [v.get("name") for v in f.get("versions") or [] if isinstance(v, dict)],
        "key": raw["key"],
        "id": raw.get("id"),
        "project": _name(f.get("project"), "key"),
        "issuetype": _name(f.get("issuetype")),
        "summary": f.get("summary") or "",
        "status": status.get("name"),
        "status_category": (status.get("statusCategory") or {}).get("key"),
        "priority": _name(f.get("priority")),
        "assignee": _name(f.get("assignee"), "displayName"),
        "assignee_id": _name(f.get("assignee"), "accountId"),
        "reporter": _name(f.get("reporter"), "displayName"),
        "created": f.get("created"),
        "updated": f.get("updated"),
        "resolved": f.get("resolutiondate"),
        "components": [c.get("name") for c in f.get("components") or []],
        "labels": f.get("labels") or [],
        "original_estimate_sec": f.get("timeoriginalestimate"),
        "time_spent_sec": f.get("timespent"),
        "url": client.browse_url(raw["key"]),
    }


def _rich_text(value) -> str:
    """Jira rich text (ADF) or plain text, flattened."""
    if isinstance(value, dict):
        return _adf_text(value, []).strip()
    return (value or "").strip() if isinstance(value, str) else ""


UPSERT_COLS = ["key", "id", "project", "issuetype", "summary", "status", "status_category",
               "priority", "assignee", "assignee_id", "reporter", "created", "updated", "resolved",
               "components", "labels", "original_estimate_sec", "time_spent_sec", "url", "urgency",
               "reopen_count", "first_seen", "last_synced", "in_bugs", "in_mine", "in_team",
               "story_points", "sprint_ids", "fix_versions", "in_watch", "description", "environment",
               "affects_versions"]


def _upsert(conn, d: dict, first_seen: str | None, scope: str) -> None:
    """Insert or update a ticket. `scope` (bugs | mine) is switched on; the other is kept."""
    vals = dict(d, components=json.dumps(d["components"]), labels=json.dumps(d["labels"]),
                sprint_ids=json.dumps(d.get("sprint_ids") or []),
                fix_versions=json.dumps(d.get("fix_versions") or []),
                affects_versions=json.dumps(d.get("affects_versions") or []),
                first_seen=first_seen, last_synced=now_iso(),
                in_bugs=int(scope == "bugs"), in_mine=int(scope == "mine"), in_team=int(scope == "team"),
                in_watch=int(scope == "watch"))
    keep = {"key", "first_seen", "in_bugs", "in_mine", "in_team", "in_watch"}
    conn.execute(
        f"INSERT INTO issues({','.join(UPSERT_COLS)}) VALUES({','.join('?' * len(UPSERT_COLS))}) "
        f"ON CONFLICT(key) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in UPSERT_COLS if c not in keep)
        + f", in_{scope}=1",
        [vals.get(c) for c in UPSERT_COLS],
    )


def _ingest_changelog(conn, key: str, histories: list[dict]) -> None:
    for h in histories:
        author = _name(h.get("author"), "displayName")
        for item in h.get("items", []):
            kind = TRACKED_FIELDS.get(item.get("field"))
            if not kind:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO events(key, ts, kind, from_val, to_val, author, source, ext_id)"
                " VALUES(?,?,?,?,?,?, 'jira', ?)",
                (key, h.get("created"), kind, item.get("fromString"), item.get("toString"),
                 author, f"{h.get('id')}"),
            )


def _diff_events(conn, old: dict, new: dict) -> None:
    for kind in ("status", "assignee", "priority"):
        if old.get(kind) != new.get(kind):
            conn.execute(
                "INSERT OR IGNORE INTO events(key, ts, kind, from_val, to_val, source, ext_id)"
                " VALUES(?,?,?,?,?, 'jira', ?)",
                (new["key"], new["updated"], kind, old.get(kind), new.get(kind),
                 f"diff-{new['updated']}"),
            )


def _reopen_count(conn, key: str, cats: dict) -> int:
    rows = conn.execute(
        "SELECT from_val, to_val FROM events WHERE key=? AND kind='status'", (key,)
    ).fetchall()
    return sum(1 for r in rows if cats.get(r["from_val"]) == "done"
               and cats.get(r["to_val"]) not in (None, "done"))


def _forget(conn, keys: list[str]) -> int:
    """Drop tickets from the dashboard. Local work logs are kept for your records."""
    for k in keys:
        for table in ("issues", "events", "alerts", "jira_comments"):
            conn.execute(f"DELETE FROM {table} WHERE key=?", (k,))
    return len(keys)


def sync_once(conn, settings: Settings, client: JiraClient, alerter: Alerter | None = None) -> dict:
    """Run one sync. Only one process may sync at a time; others skip instead of waiting."""
    lock = FileLock(settings.db_path.parent / "sync.lock")
    if not lock.acquire():
        return {"skipped": True,
                "reason": f"Another sync is already running (pid {lock.holder_pid()})."}
    try:
        return _sync_locked(conn, settings, client, alerter)
    finally:
        lock.release()


class _Ctx:
    """Shared state for one sync run."""

    def __init__(self, conn, settings, client, alerter, started):
        self.conn, self.s, self.client, self.alerter, self.started = conn, settings, client, alerter, started
        self.cats = status_categories(conn)
        self.me_id, self.me_name = _whoami(conn, client)
        self.custom = _custom_fields(conn, client, settings)
        self.extra = [f for f in (self.custom.get("sprint"), self.custom.get("points")) if f]
        if settings.jira.fetch_comments:
            self.extra.append("comment")
        if settings.jira.fetch_description:
            from .jira_client import DETAIL_FIELDS
            self.extra += DETAIL_FIELDS
        self.watched = {r[0] for r in conn.execute("SELECT key FROM watches")}
        self.stats = {"seen": 0, "new": 0, "changed": 0, "alerts": 0, "removed": 0,
                      "first_sync": False, "mine": 0, "team": 0}

    def is_me(self, d: dict | None) -> bool:
        if not d:
            return False
        if self.me_id and d.get("assignee_id"):
            return d["assignee_id"] == self.me_id
        return bool(self.me_name) and d.get("assignee") == self.me_name

    def alert(self, d, kind, detail="", external=None, personal=True):
        self.alerter.raise_alert(d, kind, detail, external=external, personal=personal)
        self.stats["alerts"] += 1


def _custom_fields(conn, client, settings) -> dict:
    """Find the Sprint and Story Points field IDs once (they differ per Jira site)."""
    cached = get_state(conn, "custom_fields")
    if cached:
        found = json.loads(cached)
    else:
        found = {}
        if hasattr(client, "fields"):
            try:
                for f in client.fields():
                    custom = ((f.get("schema") or {}).get("custom") or "")
                    name = (f.get("name") or "").lower()
                    if custom.endswith("gh-sprint"):
                        found["sprint"] = f["id"]
                    elif name in ("story points", "story point estimate") and "points" not in found:
                        found["points"] = f["id"]
            except Exception as e:
                log.warning("Couldn't read Jira fields: %s", e)
        set_state(conn, "custom_fields", json.dumps(found))
    if settings.team.story_points_field:
        found["points"] = settings.team.story_points_field
    return found


def _adf_text(node, mentions: list) -> str:
    """Flatten Atlassian Document Format to text, collecting @mentioned account IDs."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "mention":
        attrs = node.get("attrs") or {}
        if attrs.get("id"):
            mentions.append(attrs["id"])
        return attrs.get("text") or "@someone"
    inner = "".join(_adf_text(c, mentions) for c in node.get("content") or [])
    return inner + ("\n" if t in ("paragraph", "heading", "listItem", "codeBlock") else "")


def _store_comments(ctx: "_Ctx", d: dict, quiet: bool, new_ticket: bool = False) -> None:
    conn = ctx.conn
    for c in d.get("_comments") or []:
        if not c.get("id"):
            continue
        mentions: list = []
        body = c.get("body")
        text = _adf_text(body, mentions).strip() if isinstance(body, dict) else (body or "")
        author = c.get("author") or {}
        exists = conn.execute("SELECT 1 FROM jira_comments WHERE id=?", (c["id"],)).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO jira_comments(id, key, author, author_id, created, body, mentions) "
            "VALUES(?,?,?,?,?,?,?)",
            (c["id"], d["key"], author.get("displayName"), author.get("accountId"), c.get("created"),
             text[:2000], json.dumps(mentions)))
        if (not exists and not quiet and ctx.s.alerts.mentions and ctx.me_id
                and ctx.me_id in mentions and author.get("accountId") != ctx.me_id):
            snippet = " ".join(text.split())[:180]
            ctx.alert(d, "mentioned", f"{author.get('displayName')}: {snippet}", external=True)
        elif (not exists and not quiet and not new_ticket and d["key"] in ctx.watched and not ctx.is_me(d)
              and author.get("accountId") != ctx.me_id):
            snippet = " ".join(text.split())[:180]
            ctx.alert(d, "watched", f"comment from {author.get('displayName')}: {snippet}", external=True)


def _store_sprints(conn, d: dict) -> None:
    for sp in d.get("_sprints") or []:
        conn.execute(
            "INSERT OR REPLACE INTO sprints(id, board_id, name, state, start, end, complete, goal) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (sp.get("id"), sp.get("boardId"), sp.get("name"), sp.get("state"), sp.get("startDate"),
             sp.get("endDate"), sp.get("completeDate"), sp.get("goal")))


def _refresh_versions(conn, client, settings, keys: list[str]) -> None:
    """Fix versions for the projects in scope, at most once an hour."""
    last = get_state(conn, "versions_refreshed")
    if last and (datetime.now(timezone.utc) - parse_ts(last)).total_seconds() < 3600:
        return
    if not hasattr(client, "versions"):
        return
    for p in keys:
        try:
            for v in client.versions(p):
                conn.execute(
                    "INSERT OR REPLACE INTO versions(id, project, name, released, archived, release_date, "
                    "description) VALUES(?,?,?,?,?,?,?)",
                    (str(v.get("id")), p, v.get("name"), int(bool(v.get("released"))),
                     int(bool(v.get("archived"))), v.get("releaseDate"), v.get("description")))
        except Exception as e:
            log.warning("Couldn't read versions for %s: %s", p, e)
    set_state(conn, "versions_refreshed", now_iso())
    conn.commit()


def project_keys(*jqls: str) -> list[str]:
    keys: list[str] = []
    for q in jqls:
        m = re.search(r"project\s+in\s*\(([^)]*)\)", q or "", re.I)
        found = [k.strip().strip("'\"") for k in m.group(1).split(",")] if m else \
            re.findall(r"project\s*=\s*['\"]?([A-Za-z0-9_]+)", q or "", re.I)
        keys += [k.upper() for k in found if k and k.upper() not in keys]
    return keys


def _whoami(conn, client) -> tuple[str, str]:
    me_id, me_name = get_state(conn, "jira_me_id"), get_state(conn, "jira_me")
    if (me_id is None or me_name is None) and hasattr(client, "myself"):
        try:
            me = client.myself()
            me_id, me_name = me.get("accountId") or "", me.get("displayName") or ""
            set_state(conn, "jira_me_id", me_id)
            set_state(conn, "jira_me", me_name)
        except Exception:
            pass
    return me_id or "", me_name or ""


def _process(ctx: _Ctx, raw: dict, scope: str, quiet: bool) -> None:
    """Store one ticket from either query and raise any alerts. `quiet` = initial load."""
    conn, s, a = ctx.conn, ctx.s, ctx.s.alerts
    d = map_issue(raw, ctx.client, ctx.custom)
    if d["status"] and d["status_category"]:
        ctx.cats[d["status"]] = d["status_category"]
    old = row_to_dict(conn.execute("SELECT * FROM issues WHERE key=?", (d["key"],)).fetchone())
    entered_bugs = scope == "bugs" and old is not None and not old.get("in_bugs")
    if old and old["updated"] == d["updated"]:
        # Unchanged, but make sure it's flagged for this scope.
        conn.execute(f"UPDATE issues SET in_{scope}=1 WHERE key=?", (d["key"],))
        conn.commit()
        if entered_bugs and not quiet:
            _new_bug_alerts(ctx, d)
        return

    # Network first, writes second: never hold the database lock while waiting on Jira.
    histories = list(ctx.client.changelog(d["key"])) if s.jira.fetch_changelog else []
    if old is None:
        ctx.stats["new"] += 1
        conn.execute(
            "INSERT OR IGNORE INTO events(key, ts, kind, to_val, author, source, ext_id)"
            " VALUES(?,?, 'created', ?, ?, 'jira', 'created')",
            (d["key"], d["created"], d["status"], d["reporter"]))
    else:
        ctx.stats["changed"] += 1
    if s.jira.fetch_changelog:
        _ingest_changelog(conn, d["key"], histories)
    elif old:
        _diff_events(conn, old, d)
    d["reopen_count"] = _reopen_count(conn, d["key"], ctx.cats)
    d["urgency"] = urgency_score(d, s)
    _upsert(conn, d, first_seen=now_iso(), scope=scope)
    _store_sprints(conn, d)
    _store_comments(ctx, d, quiet, new_ticket=old is None)
    conn.commit()  # release the write lock after each ticket

    if quiet:
        return
    open_now = d["status_category"] != "done"
    if old is None or entered_bugs:
        if scope == "bugs":
            _new_bug_alerts(ctx, d)
        elif scope == "mine" and a.assigned_to_me and open_now:
            ctx.alert(d, "assigned_to_me", d.get("issuetype") or "", external=True)
        if old is None:
            return
    fired = False
    if old.get("in_bugs") and a.priority_raised and \
            s.priority_rank(d["priority"]) > s.priority_rank(old["priority"]):
        ctx.alert(d, "priority_raised", f"{old['priority']} → {d['priority']}")
        fired = True
    if old.get("in_bugs") and a.reopened and old["status_category"] == "done" and open_now:
        ctx.alert(d, "reopened", f"now {d['status']}")
        fired = True
    if a.assigned_to_me and ctx.is_me(d) and not ctx.is_me(old) and open_now:
        if not entered_bugs:  # _new_bug_alerts already covered it
            ctx.alert(d, "assigned_to_me", f"previously {old['assignee'] or 'unassigned'}", external=True)
        fired = True
    if ctx.is_me(d):
        if not fired and a.my_status_changes and old["status"] != d["status"]:
            ctx.alert(d, "my_status", f"{old['status']} → {d['status']}", external=True)
    elif not fired and d["key"] in ctx.watched:
        changes = [f"{label}: {old[f] or '—'} → {d[f] or '—'}" for f, label in
                   (("status", "status"), ("assignee", "assignee"), ("priority", "priority")) if old[f] != d[f]]
        if changes:
            ctx.alert(d, "watched", "; ".join(changes), external=True)


def _new_bug_alerts(ctx: _Ctx, d: dict) -> None:
    """A bug that just came into view. If it's yours, you get one "assigned to you" message;
    team channels still get the new-bug alert."""
    a = ctx.s.alerts
    if d["status_category"] == "done":
        return
    mine = ctx.is_me(d)
    if a.new_bug:
        ctx.alert(d, "new_bug", "assigned to you" if mine else "", personal=not (mine and a.assigned_to_me))
    if mine and a.assigned_to_me:
        ctx.alert(d, "assigned_to_me", d.get("issuetype") or "Bug", external=True)


def _sync_locked(conn, settings: Settings, client: JiraClient, alerter: Alerter | None) -> dict:
    started = datetime.now(timezone.utc)
    ctx = _Ctx(conn, settings, client, alerter or Alerter(settings, conn), started)
    stats = ctx.stats
    last = get_state(conn, "last_sync_started")
    base = settings.jira.jql
    # Rows stored under a different (or unknown) JQL get cleaned up after a full re-sync.
    prev_scope = get_state(conn, "jql_scope")
    has_rows = conn.execute("SELECT 1 FROM issues WHERE in_bugs=1 LIMIT 1").fetchone() is not None
    scope_changed = has_rows and prev_scope != base
    first_sync = last is None or scope_changed
    stats["first_sync"] = first_sync
    set_state(conn, "jql_scope_pending", base)
    set_state(conn, "sync_progress", "0")
    conn.commit()

    today = datetime.now().date().isoformat()
    # Once a day, re-read the whole bug scope so tickets that moved out of it (another project,
    # no longer a Bug) leave the dashboard. Incremental syncs can't see those.
    bugs_full = first_sync or get_state(conn, "bugs_full_date") != today
    if bugs_full:
        jql = (f"({base}) AND (statusCategory != Done OR updated >= -{settings.jira.backfill_days}d)"
               " ORDER BY updated ASC")
    else:
        minutes = int((started - parse_ts(last)).total_seconds() // 60) + 5  # overlap for safety
        jql = f"({base}) AND updated >= -{minutes}m ORDER BY updated ASC"

    cutoff = (started - timedelta(days=settings.jira.retention_days)
              if settings.jira.retention_days else None)
    seen: set[str] = set()
    for raw in client.search(jql, extra_fields=ctx.extra):
        created = parse_ts((raw.get("fields") or {}).get("created"))
        if cutoff and (created or started) < cutoff:
            continue  # outside the retention window: never store or alert
        stats["seen"] += 1
        seen.add(raw["key"])
        _process(ctx, raw, "bugs", quiet=first_sync and not settings.alerts.alert_on_first_sync)
        if stats["seen"] % 25 == 0:
            set_state(conn, "sync_progress", str(stats["seen"]))
            conn.commit()
            log.info("Synced %s tickets so far...", stats["seen"])

    # ---- my tickets: small set, so re-read in full every time
    my_jql = settings.jira.my_jql.strip()
    if my_jql:
        mine_first = get_state(conn, "my_jql_scope") != my_jql
        mine_seen: set[str] = set()
        for raw in client.search(f"({my_jql}) ORDER BY updated DESC", extra_fields=ctx.extra):
            mine_seen.add(raw["key"])
            _process(ctx, raw, "mine", quiet=mine_first)
        for r in conn.execute("SELECT key FROM issues WHERE in_mine=1").fetchall():
            if r["key"] not in mine_seen:
                conn.execute("UPDATE issues SET in_mine=0 WHERE key=?", (r["key"],))
        stats["mine"] = len(mine_seen)
        set_state(conn, "my_jql_scope", my_jql)
    else:
        conn.execute("UPDATE issues SET in_mine=0")

    # ---- team tickets: incremental, with a full re-read once a day to drop what left scope
    team_jql = settings.team.jql.strip()
    if team_jql:
        team_first = get_state(conn, "team_jql_scope") != team_jql
        today = datetime.now().date().isoformat()
        full = team_first or get_state(conn, "team_full_date") != today
        last_t = get_state(conn, "team_last_sync")
        if full or not last_t:
            q = f"({team_jql}) ORDER BY updated DESC"
        else:
            minutes = int((started - parse_ts(last_t)).total_seconds() // 60) + 5
            q = f"({team_jql}) AND updated >= -{minutes}m ORDER BY updated DESC"
        team_seen: set[str] = set()
        for raw in client.search(q, extra_fields=ctx.extra):
            team_seen.add(raw["key"])
            _process(ctx, raw, "team", quiet=team_first)
        if full:
            for r in conn.execute("SELECT key FROM issues WHERE in_team=1").fetchall():
                if r["key"] not in team_seen:
                    conn.execute("UPDATE issues SET in_team=0 WHERE key=?", (r["key"],))
            set_state(conn, "team_full_date", today)
        stats["team"] = len(team_seen)
        set_state(conn, "team_jql_scope", team_jql)
        set_state(conn, "team_last_sync", started.isoformat())
    else:
        conn.execute("UPDATE issues SET in_team=0")
    # ---- your watch list: tickets you follow without owning them
    watched = sorted(ctx.watched)
    for i in range(0, len(watched), 50):
        chunk = watched[i:i + 50]
        try:
            raws = list(client.search(f"key in ({','.join(chunk)})", extra_fields=ctx.extra))
        except Exception:           # one deleted or hidden ticket fails the whole JQL: go one by one
            raws = []
            for k in chunk:
                try:
                    raws += list(client.search(f"key = {k}", extra_fields=ctx.extra))
                    set_state(conn, f"watch_error:{k}", "")
                except Exception as e:
                    set_state(conn, f"watch_error:{k}", str(e)[:200])
                    log.warning("Watched ticket %s: %s", k, e)
        for raw in raws:
            _process(ctx, raw, "watch", quiet=False)
    conn.execute(f"UPDATE issues SET in_watch=0 WHERE key NOT IN ({','.join('?' * len(watched))})", watched)
    _refresh_versions(conn, client, settings, project_keys(base, team_jql))
    try:
        from .timelog import sync_worklogs
        stats["worklogs"] = sync_worklogs(conn, settings, client, ctx.me_id)
    except Exception as e:
        log.warning("Worklog sync failed: %s", e)

    # ---- keep the store in scope (your own tickets are kept while they're yours)
    if bugs_full:
        backfill = started - timedelta(days=settings.jira.backfill_days)
        for r in conn.execute("SELECT key, status_category, updated FROM issues WHERE in_bugs=1").fetchall():
            # Done tickets older than the backfill window aren't in the full query; keep those.
            old_done = r["status_category"] == "done" and (parse_ts(r["updated"]) or started) < backfill
            if r["key"] not in seen and (scope_changed or not old_done):
                conn.execute("UPDATE issues SET in_bugs=0 WHERE key=?", (r["key"],))
        set_state(conn, "bugs_full_date", today)
    if cutoff:
        for r in conn.execute("SELECT key, created FROM issues WHERE in_bugs=1").fetchall():
            if (parse_ts(r["created"]) or started) < cutoff:
                conn.execute("UPDATE issues SET in_bugs=0 WHERE key=?", (r["key"],))
    orphans = [r["key"] for r in conn.execute(
        "SELECT key FROM issues WHERE in_bugs=0 AND in_mine=0 AND in_team=0 AND in_watch=0")]
    stats["removed"] += _forget(conn, orphans)

    # Age changes urgency even when Jira doesn't, so re-score every open ticket.
    for row in conn.execute("SELECT * FROM issues WHERE status_category != 'done'").fetchall():
        d = row_to_dict(row)
        conn.execute("UPDATE issues SET urgency=? WHERE key=?", (urgency_score(d, settings), d["key"]))

    set_state(conn, "status_categories", json.dumps(ctx.cats))
    set_state(conn, "last_sync_started", started.isoformat())
    set_state(conn, "last_sync_finished", now_iso())
    set_state(conn, "last_sync_stats", json.dumps(stats))
    set_state(conn, "jql_scope", base)
    set_state(conn, "sync_progress", "")
    set_state(conn, "last_sync_error", "")
    conn.commit()
    log.info("Sync done: %s", stats)
    return stats


def sync_github_safe(conn, settings: Settings, alerter: Alerter | None = None) -> dict | None:
    """Run the GitHub sync if enabled; record errors instead of raising."""
    if not settings.github.enabled:
        return None
    from .github_client import GitHubClient, resolve_token
    lock = FileLock(settings.db_path.parent / "github.lock")
    if not lock.acquire():
        return {"skipped": True, "reason": f"Another GitHub sync is already running (pid {lock.holder_pid()})."}
    try:
        return _sync_github(conn, settings, alerter, GitHubClient, resolve_token)
    finally:
        lock.release()


def _sync_github(conn, settings, alerter, GitHubClient, resolve_token):
    try:
        from .deployments import sync_deployments
        from .github_sync import sync_github
        client = GitHubClient(resolve_token(settings.github.token))
        out = sync_github(conn, settings, client, alerter)
        from .codehealth import sync_ci, sync_security
        for name, fn in (("deployments", sync_deployments), ("ci", sync_ci), ("security", sync_security)):
            try:
                out[name] = fn(conn, settings, client, alerter)
            except Exception as e:      # one part failing never stops the others
                conn.rollback()
                log.warning("%s sync failed: %s", name.title(), e)
                out[name] = {"error": str(e)}
        return out
    except Exception as e:
        conn.rollback()
        set_state(conn, "github_last_error", f"{now_iso()} {e}")
        conn.commit()
        log.warning("GitHub sync failed: %s", e)
        return {"error": str(e)}


def run_forever(conn, settings: Settings, client: JiraClient, stop_event=None) -> None:
    alerter = Alerter(settings, conn)
    interval = max(settings.jira.poll_minutes, 1) * 60
    while not (stop_event and stop_event.is_set()):
        try:
            sync_once(conn, settings, client, alerter)
            set_state(conn, "sync_fail_count", "0")
            conn.commit()
        except Exception as e:
            conn.rollback()
            set_state(conn, "last_sync_error", f"{now_iso()} {e}")
            fails = int(get_state(conn, "sync_fail_count", "0") or 0) + 1
            set_state(conn, "sync_fail_count", str(fails))
            conn.commit()
            log.exception("Sync failed")
            if fails == 3 and settings.notify.sync_health:
                alerter.raise_alert({"key": "Bug Monitor", "summary": str(e)[:300], "url": None},
                                    "sync_failing", "3 syncs in a row failed", external=True, team=False)
        sync_github_safe(conn, settings, alerter)
        from .jobs import run_all
        run_all(conn, settings, alerter)
        if stop_event:
            stop_event.wait(interval)
        else:
            time.sleep(interval)
