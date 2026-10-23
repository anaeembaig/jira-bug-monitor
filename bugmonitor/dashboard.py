"""Local live dashboard (http://127.0.0.1:8765 by default). Standard library only."""
from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import service
from .config import Settings
from .db import connect

log = logging.getLogger("bugmonitor.dashboard")
STATIC = Path(__file__).parent / "static"


def _background_sync(settings: Settings) -> None:
    """Triggered by the dashboard's Sync button. The sync lock prevents overlaps."""
    from .jira_client import JiraClient
    from .sync import sync_github_safe, sync_once
    conn = connect(settings.db_path)
    try:
        j = settings.jira
        sync_once(conn, settings, JiraClient(j.base_url, j.email, j.api_token))
        sync_github_safe(conn, settings)
    except Exception as e:
        log.warning("Manual sync failed: %s", e)
    finally:
        conn.close()


def _public_config(settings: Settings) -> dict:
    """What the dashboard needs to know about the setup, without any secrets."""
    return {"team_enabled": bool(settings.team.jql), "team": settings.team.name,
            "github_enabled": settings.github.enabled, "members": len(settings.team.members)}


def make_handler(settings: Settings):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, default=str).encode(), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            conn = connect(settings.db_path)
            try:
                if u.path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(),
                                      "text/html; charset=utf-8")
                if u.path == "/api/summary":
                    return self._json(service.summary(conn))
                if u.path == "/api/overview":
                    return self._json(service.overview(conn, settings, int(q.get("days", 60))))
                m = re.fullmatch(r"/api/team/(board|sprint|releases|reviews|qa|stuck)", u.path)
                if m:
                    from . import team
                    return self._json(getattr(team, m.group(1))(conn, settings))
                if u.path == "/api/config":
                    return self._json(_public_config(settings))
                if u.path == "/api/timelog":
                    from .timelog import timesheet
                    return self._json(timesheet(conn, settings, int(q.get("days", 0)) or None, q.get("person")))
                if u.path == "/api/timelog/team":
                    from .timelog import team_time
                    return self._json(team_time(conn, settings, int(q.get("days", 10))))
                if u.path == "/api/mine":
                    return self._json(service.my_tickets(conn))
                if u.path == "/api/issues":
                    return self._json(service.list_issues(conn, q.get("scope", "all")))
                if u.path == "/api/github/summary":
                    return self._json(dict(service.github_summary(conn),
                                           enabled=settings.github.enabled))
                if u.path == "/api/github/prs":
                    return self._json(service.list_prs(conn, q.get("relation"), q.get("state")))
                if u.path == "/api/github/pr":
                    return self._json(service.get_pr(conn, q.get("repo", ""), int(q.get("number", 0))))
                if u.path == "/api/github/branches":
                    return self._json(service.list_branches(conn))
                if u.path == "/api/open":
                    return self._json(service.list_open(
                        conn, q.get("project"), q.get("assignee"), q.get("priority"),
                        q.get("status"), q.get("unassigned") == "1", int(q.get("limit", 500))))
                if u.path == "/api/new":
                    return self._json(service.list_new(conn, float(q.get("hours", 24))))
                if u.path == "/api/alerts":
                    return self._json(service.list_alerts(conn, q.get("all") != "1", 50))
                if u.path == "/api/active":
                    return self._json(service.active_work(conn))
                if u.path == "/api/work":
                    return self._json(service.work_report(conn, float(q.get("days", 7))))
                m = re.fullmatch(r"/api/ticket/([A-Za-z0-9_-]+)", u.path)
                if m:
                    return self._json(service.get_ticket(conn, m.group(1)))
                self._json({"error": "not found"}, 404)
            finally:
                conn.close()

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_POST(self):
            u = urlparse(self.path)
            # Only the dashboard itself may change things: browsers can't send this header
            # cross-site without a CORS preflight, which this server never approves.
            if self.headers.get("X-Bugmonitor") != "1":
                return self._json({"error": "forbidden"}, 403)
            conn = connect(settings.db_path)
            try:
                if u.path == "/api/timelog/log":
                    from .jira_client import JiraClient
                    from .timelog import log_to_jira
                    b = self._body()
                    j = settings.jira
                    return self._json(log_to_jira(conn, settings, JiraClient(j.base_url, j.email, j.api_token),
                                                  b.get("key", ""), float(b.get("minutes", 0)), b.get("day"),
                                                  b.get("comment", "")))
                if u.path == "/api/sync":
                    threading.Thread(target=_background_sync, args=(settings,), daemon=True).start()
                    return self._json({"started": True})
                m = re.fullmatch(r"/api/alerts/(\d+|all)/ack", u.path)
                if m:
                    aid = None if m.group(1) == "all" else int(m.group(1))
                    return self._json(service.ack_alerts(conn, aid))
                self._json({"error": "not found"}, 404)
            finally:
                conn.close()

    return Handler


def serve(settings: Settings, stop_event=None) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((settings.dashboard.host, settings.dashboard.port),
                                make_handler(settings))
    log.info("Dashboard on http://%s:%s", settings.dashboard.host, settings.dashboard.port)
    return httpd
