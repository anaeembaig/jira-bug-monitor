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
        "so their records stay complete across sessions. When the user starts a ticket, use "
        "start_work. For 'what did I do' or standup questions use my_standup. Use "
        "search_everything when unsure where something is. Confirm before ticket_action or "
        "log_time_to_jira, since those change Jira."
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


# --------------------------------------------------------------------------- daily workflow
@mcp.tool()
def my_standup() -> dict:
    """Your standup: what you did since the last workday (worklogs, status changes, PRs, reviews,
    Claude work notes), what's next today, and blockers. Includes ready-to-paste text."""
    from .workflow import standup
    return standup(conn, settings)


@mcp.tool()
def team_standup() -> dict:
    """Per teammate: what changed since the last workday, what's in progress, and what's stuck."""
    from .workflow import team_standup as ts
    return ts(conn, settings)


@mcp.tool()
def start_work(key: str, start_timer: bool = True) -> dict:
    """Start on a ticket: description, report gaps, recent comments, linked PRs and branches, similar
    finished tickets and how they were fixed, likely regression causes, SLA, estimate, and a branch
    name that follows your naming, with git commands. start_timer logs a 'start' work entry.
    Run the git commands for the user if they want, then use log_work as work continues."""
    from .workflow import start_work as sw
    return sw(conn, settings, key, actor=ACTOR, session=SESSION, start_timer=start_timer)


@mcp.tool()
def watch_ticket(key: str, note: str = "", stop: bool = False) -> dict:
    """Follow a ticket you don't own: alerts for its status, assignee, priority, and comments.
    stop=True removes it from the watch list."""
    from .workflow import unwatch, watch
    return unwatch(conn, key) if stop else watch(conn, key, note)


@mcp.tool()
def snooze_ticket(key: str, hours: float = 24, stop: bool = False) -> dict:
    """Mute your alerts for a ticket for N hours (team channels aren't affected). stop=True un-mutes."""
    from .workflow import snooze, unsnooze
    return unsnooze(conn, key) if stop else snooze(conn, key, hours)


@mcp.tool()
def search_everything(query: str) -> dict:
    """Search tickets (key, summary, description, assignee), PRs, branches, deployments (sha,
    environment, ticket), worklog comments, Claude work notes, and security alerts."""
    from .workflow import search
    return search(conn, query)


@mcp.tool()
def ticket_action(key: str, action: str, value: str = "") -> dict:
    """Change a Jira ticket for the user. action: transitions (list options), transition (value =
    target status or transition name), assign_me, unassign, comment (value = text).
    Needs [jira] allow_actions = true. Always confirm with the user before changing a ticket."""
    from .workflow import ticket_action as act
    if action != "transitions" and not settings.jira.allow_actions:
        return {"error": "Ticket actions are off. Set allow_actions = true under [jira] in config.toml."}
    try:
        client = JiraClient(settings.jira.base_url, settings.jira.email, settings.jira.api_token)
        return act(conn, settings, client, key, action, value)
    except JiraError as e:
        return {"error": str(e)}


# --------------------------------------------------------------------------- bug quality
@mcp.tool()
def bug_report_quality(key: str | None = None) -> dict:
    """With a key: what that bug report is missing (steps, expected result, environment...) and a
    drafted note for the reporter. Without: every open bug with gaps, by problem and reporter."""
    from .quality import quality_check, quality_report
    if key:
        t = service.get_ticket(conn, key)
        return t if t.get("error") else quality_check(t, settings)
    return quality_report(conn, settings)


@mcp.tool()
def regression_suspects(key: str) -> list[dict]:
    """PRs merged or deployed in the two weeks before this bug was reported that touch the same area,
    with reasons. Use them as leads, not conclusions."""
    from .quality import regression_suspects as rs
    return rs(conn, settings, key)


@mcp.tool()
def similar_tickets(key: str) -> list[dict]:
    """Finished tickets most like this one, with hands-on hours and the PRs that fixed them."""
    from .quality import similar_tickets as st
    return st(conn, key, limit=8)


@mcp.tool()
def sla_status(key: str | None = None) -> dict:
    """SLA response and fix timers. With a key: that ticket. Without: breached and at-risk bugs and
    compliance over the last 30 days. Needs [sla] enabled = true."""
    from .quality import sla_overview, sla_status as one
    if key:
        t = service.get_ticket(conn, key)
        return t if t.get("error") else (one(conn, t, settings) or {"note": "No SLA target for this priority."})
    return sla_overview(conn, settings)


@mcp.tool()
def bug_hotspots(days: int = 90) -> dict:
    """Files and folders most often changed by bug-fix PRs, and components with the most bugs."""
    from .quality import hotspots
    return hotspots(conn, settings, days)


# --------------------------------------------------------------------------- planning
@mcp.tool()
def delivery_forecast() -> dict:
    """Monte Carlo forecast for the active sprint and upcoming releases: chance of finishing on time,
    likely and safe finish dates, from the last 30 workdays of throughput."""
    from .planning import forecast
    return forecast(conn, settings)


@mcp.tool()
def team_capacity() -> dict:
    """Each teammate's available focus hours (minus time off) against the remaining work assigned to
    them in the active sprint (or the next 10 workdays)."""
    from .planning import capacity
    return capacity(conn, settings)


@mcp.tool()
def release_readiness() -> dict:
    """Go/no-go checklist per unreleased version: tickets done, no high-priority bugs, PRs merged,
    QA finished, deployed to a test environment, code linked, release date."""
    from .planning import readiness
    return readiness(conn, settings)


# --------------------------------------------------------------------------- code health
@mcp.tool()
def ci_health() -> dict:
    """GitHub Actions on default branches: what's failing now, success rates, build times, flaky
    runs (passed on retry), and recent failures."""
    from .codehealth import ci_overview
    return ci_overview(conn, settings)


@mcp.tool()
def suggest_reviewers(repo: str | None = None, number: int | None = None) -> dict | list:
    """Reviewers who know the changed files and have room to review. With repo and number: that PR.
    Without: your open PRs that have no reviewer yet."""
    from .codehealth import my_review_suggestions, reviewer_suggestions
    if repo and number:
        return reviewer_suggestions(conn, settings, repo, number)
    return my_review_suggestions(conn, settings)


@mcp.tool()
def security_alerts() -> dict:
    """Open Dependabot and code scanning alerts across your repos, by severity, with linked Jira tickets."""
    from .codehealth import security_overview
    return security_overview(conn, settings)


# --------------------------------------------------------------------------- time
@mcp.tool()
def suggested_worklogs(days: int = 7) -> list[dict]:
    """Worklogs the user probably forgot to log, with hours and a comment. Offer to log them with
    log_time_to_jira after the user confirms."""
    from .timelog import suggestions
    return suggestions(conn, settings, days)


@mcp.tool()
def export_timesheet(days: int = 14, team: bool = False, person: str | None = None) -> str:
    """Worklogs as CSV (date, person, ticket, summary, hours, comments). Save it as a .csv file for Excel."""
    from .timelog import export_csv
    return export_csv(conn, settings, days, person, team)


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
