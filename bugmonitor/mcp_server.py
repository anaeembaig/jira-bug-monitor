"""MCP server so any Claude session (Claude Code CLI or Claude Desktop) can use the monitor.

Every session starts its own copy of this server over stdio, and they all share one
SQLite database, so work logged from one session is visible in every other.
"""
from __future__ import annotations

import getpass
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

from . import service
from .alerts import Alerter
from .config import load_settings
from .db import connect
from .jira_client import JiraClient, JiraError
from .sync import sync_once

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

settings = load_settings()
conn = connect(settings.db_path)
ACTOR = os.environ.get("BUGMON_ACTOR") or getpass.getuser()
SESSION = os.environ.get("BUGMON_SESSION", "claude")

mcp = FastMCP(
    "jira-bug-monitor",
    instructions=(
        "Local Jira bug monitor. Use bug_summary or list_open_bugs for the live picture, "
        "list_new_bugs for recent arrivals, get_ticket for one ticket's full history and fix "
        "estimate, and log_work whenever the user starts, pauses, or finishes work on a ticket "
        "so their records stay complete across sessions."
    ),
)


@mcp.tool()
def bug_summary() -> dict:
    """Live overview: open bug counts by priority, status, and project; new in last 24h;
    unassigned; unread alerts; and sync health."""
    return service.summary(conn)


@mcp.tool()
def list_open_bugs(project: str | None = None, assignee: str | None = None,
                   priority: str | None = None, status: str | None = None,
                   unassigned_only: bool = False, limit: int = 25) -> list[dict]:
    """Open bugs sorted by urgency score (0-100, highest first). Filters are optional.
    assignee matches part of a display name."""
    return service.list_open(conn, project, assignee, priority, status, unassigned_only, limit)


@mcp.tool()
def list_new_bugs(hours: float = 24) -> list[dict]:
    """Bugs created in Jira within the last N hours, newest first."""
    return service.list_new(conn, hours)


@mcp.tool()
def get_ticket(key: str) -> dict:
    """Full record for one ticket: current fields, urgency, time spent in each status,
    complete timeline (Jira changes + local work log), alerts, and a fix-time estimate."""
    return service.get_ticket(conn, key)


@mcp.tool()
def estimate_fix(key: str) -> dict:
    """Estimate how long a bug will take to fix, based on how long similar resolved bugs
    took (same project, component, priority), minus time already spent."""
    from .analytics import estimate_fix as _est
    return _est(conn, key.strip().upper())


@mcp.tool()
def log_work(key: str, action: str, note: str = "", minutes: float | None = None) -> dict:
    """Record work on a ticket. action: start | stop | note | blocked | done.
    start/stop pairs are timed automatically; pass minutes to log time directly."""
    return service.log_work(conn, key, action, note, minutes, actor=ACTOR, session=SESSION)


@mcp.tool()
def active_work() -> list[dict]:
    """Tickets currently being worked on (latest local entry is 'start'), from any session."""
    return service.active_work(conn)


@mcp.tool()
def work_report(days: float = 7) -> list[dict]:
    """Time logged per ticket over the last N days, from all Claude sessions."""
    return service.work_report(conn, days)


@mcp.tool()
def list_alerts(unacknowledged_only: bool = True, limit: int = 25) -> list[dict]:
    """Recent alerts: new bugs, priority increases, and reopened bugs."""
    return service.list_alerts(conn, unacknowledged_only, limit)


@mcp.tool()
def acknowledge_alerts(alert_id: int | None = None) -> dict:
    """Mark one alert (by id) or all unread alerts as read."""
    return service.ack_alerts(conn, alert_id)


@mcp.tool()
def my_tickets(status: str | None = None) -> dict:
    """Tickets assigned to you (any type), grouped by status (To Do, In Progress, In QA,
    Done, ...), each with hours spent in every status and total time. Optional status filter."""
    m = service.my_tickets(conn)
    if status:
        m["groups"] = [g for g in m["groups"] if g["status"].lower() == status.lower()]
    return m


@mcp.tool()
def engineering_insights(days: int = 30) -> dict:
    """Cycle time by stage, PR health (time to first review, time to merge, size, changes
    requested), bug quality (reopen and escape rates, by component), estimate accuracy,
    weekly throughput, and production delivery metrics."""
    from .insights import all_insights
    return all_insights(conn, settings, days)


@mcp.tool()
def weekly_report(person: str | None = None, days: int = 7) -> dict:
    """What got done: resolved tickets, merged PRs, deploys, time logged, as data plus ready-to-paste
    text. person: blank for you, "team" for the whole team, or a teammate's Jira name."""
    from .reports import as_text, weekly
    rep = weekly(conn, settings, person, days)
    return dict(rep, text=as_text(rep))


@mcp.tool()
def triage_help() -> dict:
    """Likely duplicate bugs, and suggested assignee/component/priority for open bugs missing them,
    based on similar past tickets. Use get_ticket for the details before acting on a suggestion."""
    from .triage import duplicates, suggestions
    return {"duplicates": duplicates(conn, settings), "suggestions": suggestions(conn, settings)}


@mcp.tool()
def draft_release_notes(version: str | None = None, project: str | None = None,
                        deployment_id: str | None = None) -> dict:
    """Release notes grouped into fixes and improvements, from a Jira fix version or from what a
    deployment shipped. Polish the wording for the user before they share it."""
    from .triage import release_notes
    return release_notes(conn, settings, version, project, deployment_id)


@mcp.tool()
def deployments(mine_only: bool = False) -> dict:
    """Deployments to every environment: latest per environment, recent deploys with who deployed,
    when, state and status history, duration, logs, and the Jira tickets, PRs, and commits each
    shipped; plus deploy pipelines running now with step progress. mine_only = only deploys
    that include your tickets."""
    from .deployments import overview
    return overview(conn, settings, mine_only)


@mcp.tool()
def ticket_deployments(key: str) -> list[dict]:
    """Where a Jira ticket has been deployed: environment, when, by whom, state, logs."""
    from .deployments import for_ticket
    return for_ticket(conn, key.strip().upper())


@mcp.tool()
def delivery_metrics(days: int = 30) -> dict:
    """Production delivery metrics (DORA): deploys per week, change failure rate, lead time
    from merge to production, and time to restore after a failed deploy."""
    from .deployments import dora
    return dora(conn, settings, days)


@mcp.tool()
def my_timesheet(days: int = 7, person: str | None = None) -> dict:
    """Day-by-day time: hours logged in Jira per ticket, time tracked from Claude sessions,
    and what you did on each ticket (status changes, comments, PRs). Shows short days against
    your daily target and tickets you worked on without logging time. person = a teammate's name."""
    from .timelog import timesheet
    return timesheet(conn, settings, days, person)


@mcp.tool()
def team_timesheet(days: int = 10) -> dict:
    """Hours each teammate logged in Jira per day."""
    from .timelog import team_time
    return team_time(conn, settings, days)


@mcp.tool()
def log_time_to_jira(key: str, minutes: float, day: str | None = None, comment: str = "") -> dict:
    """Add a worklog in Jira (writes to Jira). Only works when [timelog] allow_jira_write = true.
    day is YYYY-MM-DD (default today). Confirm the details with the user before calling."""
    from .timelog import log_to_jira
    client = JiraClient(settings.jira.base_url, settings.jira.email, settings.jira.api_token)
    return log_to_jira(conn, settings, client, key, minutes, day, comment)


@mcp.tool()
def team_board() -> dict:
    """Team board: every teammate's tickets by status, with work in progress, open points,
    and stuck counts per person. Needs [team] jql."""
    from . import team
    return team.board(conn, settings)


@mcp.tool()
def sprint_status() -> dict:
    """Active sprint(s): progress, scope added after the start, carried-over work, burnup, tickets."""
    from . import team
    return team.sprint(conn, settings)


@mcp.tool()
def release_status() -> dict:
    """Unreleased Jira versions: progress, days left, blocking high-priority bugs, open PRs."""
    from . import team
    return team.releases(conn, settings)


@mcp.tool()
def review_queue() -> dict:
    """Team PRs waiting on review, oldest first, plus how many reviews each person has pending."""
    from . import team
    return team.reviews(conn, settings)


@mcp.tool()
def qa_handoff() -> dict:
    """Tickets in QA statuses and whether they're testable: PR merged, deployed, or still open."""
    from . import team
    return team.qa(conn, settings)


@mcp.tool()
def stuck_work(only_mine: bool = False) -> dict:
    """Tickets in a status longer than the team's limit, and PRs waiting too long for review."""
    from . import team
    return team.stuck(conn, settings, only_mine)


@mcp.tool()
def bug_trends(days: int = 60) -> dict:
    """60-day picture: weekly created vs resolved, open backlog by day, open bugs by status,
    priority, assignee, and age, plus KPIs like median time to resolve."""
    return service.overview(conn, settings, days)


@mcp.tool()
def my_pull_requests(relation: str | None = None, state: str | None = None) -> list[dict]:
    """Your GitHub PRs from the last 60 days with live status (review decision, approvals,
    pending reviewers, comment and unresolved-thread counts, CI checks) and linked Jira keys. relation: authored | review_requested | reviewed | assigned.
    state: OPEN | MERGED | CLOSED."""
    return service.list_prs(conn, relation, state)


@mcp.tool()
def get_pull_request(repo: str, number: int) -> dict:
    """Full details for one PR: who approved or requested changes, who is still pending,
    every comment and review thread (with file and resolved state), CI checks, labels,
    merge state, and the linked Jira tickets with their current status.
    repo can be "web-schedule" or "tcp-software/web-schedule"."""
    return service.get_pr(conn, repo, number)


@mcp.tool()
def my_branches() -> list[dict]:
    """Your recent branches with latest commit, CI status, linked PR, and Jira keys."""
    return service.list_branches(conn)


@mcp.tool()
def github_summary() -> dict:
    """Counts: your open PRs by status, PRs waiting on your review, failing checks."""
    return service.github_summary(conn)


@mcp.tool()
def sync_now() -> dict:
    """Pull the latest from Jira (and GitHub, if enabled) right now."""
    from .sync import sync_github_safe
    try:
        client = JiraClient(settings.jira.base_url, settings.jira.email, settings.jira.api_token)
        out = {"jira": sync_once(conn, settings, client, Alerter(settings, conn))}
    except JiraError as e:
        out = {"jira": {"error": str(e)}}
    out["github"] = sync_github_safe(conn, settings)
    return out


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
