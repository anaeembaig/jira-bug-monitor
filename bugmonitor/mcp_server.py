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
def sync_now() -> dict:
    """Pull the latest changes from Jira right now instead of waiting for the next poll."""
    try:
        client = JiraClient(settings.jira.base_url, settings.jira.email, settings.jira.api_token)
        return sync_once(conn, settings, client, Alerter(settings, conn))
    except JiraError as e:
        return {"error": str(e)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
