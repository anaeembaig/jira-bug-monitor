"""Command line: bugmonitor <command>   (or: python -m bugmonitor <command>)

  init       Guided setup: logins, projects, Teams, GitHub, team views (run again to change)

  run        Poll Jira on a schedule and serve the dashboard (leave this running)
  sync       Run one sync now and exit
  check      Test the Jira connection and show the JQL scope
  dashboard  Serve the dashboard only
  test-alert Send a test alert through every enabled channel
  mcp        Start the MCP server on stdio (Claude starts this for you)
  install-claude  Connect the monitor to Claude Desktop and Claude Code
  doctor     Show version, running copies, and what holds the database
  notifications  Show your notifications; add --activate to turn them on, --preview to test Teams
  secrets    Show where each token is stored; add --move to move them into the credential store
  audit      Show recent changes made from the dashboard or by Claude
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser

from .config import load_settings
from .db import connect


def _client(s):
    from .jira_client import JiraClient
    return JiraClient(s.jira.base_url, s.jira.email, s.jira.api_token)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bugmonitor", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["run", "sync", "check", "dashboard", "test-alert", "mcp", "install-claude", "doctor",
                                        "notifications", "init", "secrets", "audit"])
    p.add_argument("--config", help="Path to config.toml")
    p.add_argument("--no-browser", action="store_true", help="Don't open the dashboard on start")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--activate", action="store_true", help="notifications: turn on the recommended set")
    p.add_argument("--preview", action="store_true", help="notifications: send a sample to Teams")
    p.add_argument("--move", action="store_true", help="secrets: move tokens into the credential store")
    a = p.parse_args(argv)

    if a.command == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return 0

    if a.command == "init":
        from .init_wizard import run as init
        return init(path=a.config)

    if a.command == "secrets":
        from .init_wizard import move_secrets, secrets_status
        return move_secrets(a.config) if a.move else secrets_status(a.config)

    if a.command == "notifications":
        from .notify_setup import main as notifications
        return notifications([f for f, on in (("--activate", a.activate), ("--preview", a.preview)) if on],
                             a.config)

    if a.command == "doctor":
        from .doctor import main as doctor
        return doctor()

    if a.command == "install-claude":
        from .install_claude import main as install
        install()
        return 0

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = load_settings(a.config)
    if not s.config_file or not s.config_file.exists():
        print(f"No settings found at {s.config_file}. Run `bugmonitor init` first.")
        return 1

    if a.command == "audit":
        conn = connect(s.db_path)
        rows = conn.execute("SELECT ts, actor, action, detail FROM audit ORDER BY id DESC LIMIT 50").fetchall()
        for r in rows:
            print(f"{r['ts']}  {r['actor']:16} {r['action']:14} {r['detail']}")
        print("No changes recorded." if not rows else "")
        return 0

    if a.command == "check":
        me = _client(s).myself()
        print(f"Connected to {s.jira.base_url} as {me.get('displayName')} ({me.get('emailAddress')})")
        print(f"Watching JQL: {s.jira.jql}")
        print(f"Database: {s.db_path}")
        if s.github.enabled:
            from .github_client import GitHubClient, resolve_token
            gh = GitHubClient(resolve_token(s.github.token))
            print(f"GitHub: connected as {gh.viewer()}"
                  + (f" (orgs: {', '.join(s.github.orgs)})" if s.github.orgs else ""))
        if s.jenkins.enabled:
            from .jenkins_client import JenkinsClient
            jk = JenkinsClient(s.jenkins.url, s.jenkins.user, s.jenkins.token, s.jenkins.ca_bundle)
            print(f"Jenkins: connected to {s.jenkins.url} as {jk.whoami() or s.jenkins.user}")
        return 0

    conn = connect(s.db_path)

    if a.command == "sync":
        from .sync import sync_github_safe, sync_once
        print("Jira:", sync_once(conn, s, _client(s)))
        if s.github.enabled:
            print("GitHub:", sync_github_safe(conn, s))
        if s.jenkins.enabled:
            from .jenkins_sync import sync_jenkins_safe
            print("Jenkins:", sync_jenkins_safe(conn, s, force=True))
        return 0

    if a.command == "test-alert":
        from .alerts import Alerter
        aid = Alerter(s, conn).raise_alert(
            {"key": "TEST-1", "summary": "Test alert from Jira Bug Monitor",
             "priority": s.priority_order[0], "status": "Open", "assignee": None,
             "url": s.jira.base_url or "http://localhost"}, "new_bug")
        conn.commit()
        row = conn.execute("SELECT channels FROM alerts WHERE id=?", (aid,)).fetchone()
        print(f"Test alert sent to: {row['channels']}")
        return 0

    from .locks import FileLock
    instance = FileLock(s.db_path.parent / f"{a.command}.lock")
    if not instance.acquire():
        print(f"bugmonitor {a.command} is already running (pid {instance.holder_pid()}). "
              f"Stop that one first, or run: python -m bugmonitor doctor")
        return 1

    from . import __version__
    from .dashboard import serve
    print(f"Jira Bug Monitor {__version__}")
    print(f"Jira filter: {s.jira.jql}")
    if not s.jira.retention_days:
        print("Note: retention_days is not set, so old tickets are kept. Set it to 60 in config.toml.")
    httpd = serve(s)
    url = f"http://{s.dashboard.host}:{s.dashboard.port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"Dashboard: {url}")
    if not a.no_browser:
        webbrowser.open(url)

    try:
        if a.command == "run":
            from .sync import run_forever
            print(f"Polling Jira every {s.jira.poll_minutes} min. Press Ctrl+C to stop.")
            run_forever(conn, s, _client(s))
        else:
            threading.Event().wait()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.shutdown()
    return 0


def _main_with_help() -> int:
    import sqlite3
    try:
        return main()
    except sqlite3.OperationalError as e:
        if "locked" not in str(e):
            raise
        print("The database is busy: another copy of the monitor is holding it.\n"
              "Run this to see which one:  .venv/bin/python -m bugmonitor doctor")
        return 1


if __name__ == "__main__":
    sys.exit(_main_with_help())
