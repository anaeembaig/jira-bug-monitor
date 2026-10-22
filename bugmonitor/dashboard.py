"""Local live dashboard (http://127.0.0.1:8765 by default). Standard library only."""
from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import service
from .config import Settings
from .db import connect

log = logging.getLogger("bugmonitor.dashboard")
STATIC = Path(__file__).parent / "static"


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

        def do_POST(self):
            u = urlparse(self.path)
            conn = connect(settings.db_path)
            try:
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
