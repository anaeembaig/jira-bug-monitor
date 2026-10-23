"""Dashboard hardening, retention, and file permissions."""
import json
import os
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from bugmonitor import jobs
from bugmonitor.config import load_settings
from bugmonitor.dashboard import _host_ok, serve
from bugmonitor.db import connect


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[jira]\nbase_url = 'https://x.atlassian.net'\n")
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "data" / "s.db"))
    s = load_settings(cfg)
    s.dashboard.port = 0
    return s, connect(s.db_path)


def get(url, host=None):
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    return urllib.request.urlopen(req)


def test_host_header_and_security_headers(env):
    s, conn = env
    httpd = serve(s)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    r = get(base + "/api/summary")
    assert r.status == 200 and r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/api/summary", host="evil.example.com")      # DNS rebinding attempt
    assert e.value.code == 403
    assert json.loads(get(base + "/api/audit").read()) == []
    httpd.shutdown()


def test_refuses_network_without_opt_in(env):
    s, conn = env
    s.dashboard.host = "0.0.0.0"
    with pytest.raises(SystemExit, match="no login"):
        serve(s)
    assert _host_ok(s, "localhost:8765") and not _host_ok(s, "10.0.0.5:8765")
    s.dashboard.allow_network, s.dashboard.host = True, "10.0.0.5"
    assert _host_ok(s, "10.0.0.5:8765")


def test_housekeeping_retention_and_permissions(env):
    s, conn = env
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO alerts(key, ts, kind, message, acknowledged) VALUES('A', ?, 'x', 'm', 1)", (old,))
    conn.execute("INSERT INTO alerts(key, ts, kind, message, acknowledged) VALUES('B', ?, 'x', 'm', 0)", (old,))  # unread: kept
    conn.execute("INSERT INTO alerts(key, ts, kind, message, acknowledged) VALUES('C', ?, 'x', 'm', 1)", (new,))
    conn.execute("INSERT INTO events(key, ts, kind, source) VALUES('GONE-1', ?, 'status', 'jira')", (new,))
    conn.commit()
    out = jobs.housekeeping(conn, s)
    assert out["alerts"] == 1 and out["orphan_events"] == 1
    assert {r[0] for r in conn.execute("SELECT key FROM alerts")} == {"B", "C"}
    assert jobs.housekeeping(conn, s) is None                      # once a day
    if os.name == "posix":
        assert oct(os.stat(s.config_file).st_mode)[-3:] == "600"
        assert oct(os.stat(s.db_path).st_mode)[-3:] == "600"
        assert oct(os.stat(s.db_path.parent).st_mode)[-3:] == "700"
