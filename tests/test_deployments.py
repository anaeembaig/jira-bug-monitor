"""Deployments: what shipped, who deployed, progress, rollbacks, alerts, QA, DORA."""
from datetime import datetime, timedelta, timezone

import pytest

from bugmonitor import deployments as dep, service, team
from bugmonitor.config import Route, load_settings
from bugmonitor.db import connect
from bugmonitor.github_sync import sync_github
from bugmonitor.sync import sync_once, sync_github_safe
from test_monitor import FakeGitHub, gh_pr
from test_team import TeamJira, t

NOW = datetime.now(timezone.utc)
iso = lambda h: (NOW - timedelta(hours=h)).isoformat()
REPO = "tcp-software/web-schedule"


def deployment(id_, env, sha, created_h, states, by="sara-k"):
    return {"id": id_, "environment": env, "state": states[-1][0], "createdAt": iso(created_h),
            "updatedAt": iso(states[-1][1]), "commitOid": sha, "ref": {"name": "main"}, "creator": {"login": by},
            "description": "", "statuses": {"nodes": [
                {"state": s, "createdAt": iso(h), "logUrl": f"https://ci/{id_}", "environmentUrl": f"https://{env}.app",
                 "creator": {"login": by}, "description": ""} for s, h in reversed(states)]}}


class DeployGH(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.deps, self.cmp, self.runs, self.jobs = [], {}, [], {}

    def deployments(self, repo):
        return list(reversed(self.deps))        # newest first, like GitHub

    def compare(self, repo, base, head):
        if self.cmp.get((base, head)) == "error":
            raise RuntimeError("502 Bad Gateway")
        return self.cmp.get((base, head), {"status": "ahead", "total_commits": 0, "commits": []})

    def workflow_runs(self, repo, since):
        return self.runs

    def run_jobs(self, repo, run_id):
        return self.jobs.get(run_id, [])


def commit(msg, who):
    return {"commit": {"message": msg, "author": {"name": who}}, "author": {"login": who}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUGMON_DB", str(tmp_path / "d.db"))
    s = load_settings(tmp_path / "none.toml")
    s.alerts.desktop = False
    s.alerts.teams_webhook = "https://private"
    s.github.enabled = True
    s.github.repos = [REPO]
    s.team.jql = "TEAMSCOPE"
    s.routes = [Route(name="web", webhook="https://web", events=["deployment"])]
    posts = []

    class R:
        status_code = 202; text = ""

    monkeypatch.setattr("bugmonitor.alerts.requests.post", lambda url, json, timeout: posts.append((url, json)) or R())
    j = TeamJira()
    j.myself = lambda: {"displayName": "Ali Naeem", "accountId": "acc-AliNaeem"}
    j.issues["HUM-1"] = t("HUM-1", "In QA", assignee="Ali Naeem")
    j.issues["HUM-2"] = t("HUM-2", "In Progress", assignee="Sam Lee")
    conn = connect(s.db_path)
    sync_once(conn, s, j)
    gh = DeployGH()
    gh.prs["p"] = gh_pr(50, "HUM-1 fix rounding", "hum-1-fix", state="MERGED")
    gh.prs["p"]["mergedAt"] = iso(30)
    return s, conn, gh, posts


def kinds(conn):
    return [a["kind"] for a in service.list_alerts(conn)]


def test_deploy_flow_what_shipped_alerts_rollback_and_metrics(env):
    s, conn, gh, posts = env
    gh.deps = [deployment("D1", "staging", "a1", 72, [("SUCCESS", 71)]),
               deployment("D0", "production", "a0", 70, [("SUCCESS", 69)])]
    sync_github(conn, s, gh)
    dep.sync_deployments(conn, s, gh)              # first run: history only, no alerts
    assert kinds(conn) == []

    # New staging deploy, still running: shows progress, no alert yet.
    gh.deps.append(deployment("D2", "staging", "a2", 2, [("QUEUED", 2), ("IN_PROGRESS", 1.9)]))
    gh.cmp[("a1", "a2")] = {"status": "ahead", "total_commits": 2, "commits": [
        commit("HUM-1 fix rounding (#50)", "anaeem"), commit("Bump deps", "dependabot")]}
    dep.sync_deployments(conn, s, gh)
    ov = dep.overview(conn, s)
    d2 = next(d for d in ov["feed"] if d["id"] == "D2")
    assert d2["state"] == "IN_PROGRESS" and d2["tickets"] == ["HUM-1"] and d2["prs"] == [50]
    assert d2["commits"] == 2 and d2["authors"] == ["anaeem", "dependabot"] and d2["creator"] == "sara-k"
    assert d2["mine"] == ["HUM-1"] and any(r["id"] == "D2" for r in ov["running"])
    assert kinds(conn) == []

    # It finishes: you hear your ticket reached staging (not the channel; staging isn't production).
    gh.deps[-1] = deployment("D2", "staging", "a2", 2, [("QUEUED", 2), ("IN_PROGRESS", 1.9), ("SUCCESS", 1.5)])
    posts.clear()
    dep.sync_deployments(conn, s, gh)
    assert kinds(conn) == ["deployed"] and [u for u, _ in posts] == ["https://private"]
    d2 = next(d for d in dep.overview(conn, s)["feed"] if d["id"] == "D2")
    assert d2["duration_s"] == pytest.approx(1800, abs=5) and [x["state"] for x in d2["statuses"]] == ["QUEUED", "IN_PROGRESS", "SUCCESS"]
    assert team.qa(conn, s)["tickets"][0]["testable"] == "Deployed"
    assert team.qa(conn, s)["tickets"][0]["environments"] == ["staging"]
    assert dep.for_ticket(conn, "HUM-1")[0]["environment"] == "staging"
    assert service.get_ticket(conn, "HUM-1")["deployments"][0]["creator"] == "sara-k"

    # Production deploy fails: you and the channel both hear about it.
    gh.deps.append(deployment("D3", "production", "a2", 1, [("IN_PROGRESS", 1), ("FAILURE", 0.8)], by="omar-a"))
    gh.cmp[("a0", "a2")] = {"status": "ahead", "total_commits": 2, "commits": [commit("HUM-1 fix rounding (#50)", "anaeem")]}
    posts.clear()
    dep.sync_deployments(conn, s, gh)
    assert kinds(conn)[0] == "deploy_failed" and sorted(u for u, _ in posts) == ["https://private", "https://web"]

    # Retry succeeds; then someone redeploys the old commit: a rollback.
    gh.deps.append(deployment("D4", "production", "a2", 0.5, [("SUCCESS", 0.4)], by="omar-a"))
    dep.sync_deployments(conn, s, gh)
    assert kinds(conn)[0] == "deployed"
    gh.deps.append(deployment("D5", "production", "a0", 0.2, [("SUCCESS", 0.1)], by="omar-a"))
    gh.cmp[("a2", "a0")] = {"status": "behind", "total_commits": 0, "commits": []}
    dep.sync_deployments(conn, s, gh)
    assert kinds(conn)[0] == "rollback"
    latest = {(e["repo"], e["environment"]): e for e in dep.overview(conn, s)["environments"]}
    assert latest[(REPO, "production")]["id"] == "D5" and latest[(REPO, "production")]["rollback"] == 1

    rb = next(d for d in dep.overview(conn, s)["feed"] if d["id"] == "D5")
    assert rb["tickets"] == ["HUM-1"]                                             # what the rollback removed
    assert dep.environments_for_tickets(conn)["HUM-1"] == ["staging"]           # no longer in production

    m = dep.dora(conn, s, 30)
    # Changes: D0, D3, D4. D3 failed outright; D4 was rolled back by D5.
    assert m["deploys"] == 3 and m["change_failure_rate"] == 67
    assert {f["rolled_back"] for f in m["failed"]} == {True, False}
    assert m["lead_time_hours"] == pytest.approx(29.5, abs=0.2)                  # PR merged 30h ago -> D4
    assert m["time_to_restore_hours"] == pytest.approx(0.5, abs=0.05)            # D3 -> D4 (0.5h), D4 -> D5 (0.3h)


def test_failed_compare_is_retried_then_alerts(env):
    s, conn, gh, posts = env
    gh.deps = [deployment("D0", "staging", "a0", 70, [("SUCCESS", 69)])]
    sync_github(conn, s, gh)
    dep.sync_deployments(conn, s, gh)
    gh.deps.append(deployment("D1", "staging", "a1", 1, [("SUCCESS", 0.9)]))
    gh.cmp[("a0", "a1")] = "error"
    dep.sync_deployments(conn, s, gh)              # compare fails: tickets unknown, no alert yet
    row = conn.execute("SELECT tickets FROM deployments WHERE id='D1'").fetchone()
    assert row["tickets"] is None and kinds(conn) == []
    gh.cmp[("a0", "a1")] = {"status": "ahead", "total_commits": 1, "commits": [commit("HUM-1 fix (#50)", "anaeem")]}
    dep.sync_deployments(conn, s, gh)              # retried and now known: one alert
    assert kinds(conn) == ["deployed"]
    dep.sync_deployments(conn, s, gh)
    assert kinds(conn) == ["deployed"]


def test_pipeline_progress_and_aliases(env):
    s, conn, gh, posts = env
    s.deploy.aliases = {"prod-us-east-1": "Production"}
    s.github.production_environments = ["production"]
    assert dep.env_name(s, "PROD-US-EAST-1") == "Production" and dep.is_production(s, "Production")
    gh.runs = [{"id": 9, "name": "Deploy web", "status": "in_progress", "conclusion": None, "head_sha": "a9",
                "created_at": iso(0.2), "updated_at": iso(0.1), "run_started_at": iso(0.2),
                "html_url": "https://gh/run/9", "actor": {"login": "sara-k"}, "head_branch": "main",
                "display_title": "Release 2026.11"},
               {"id": 10, "name": "Unit tests", "status": "completed", "conclusion": "success"}]
    gh.jobs[9] = [{"name": "build", "status": "completed", "conclusion": "success",
                   "steps": [{"name": "checkout", "status": "completed"}, {"name": "build", "status": "completed"}]},
                  {"name": "deploy", "status": "in_progress", "conclusion": None,
                   "steps": [{"name": "push image", "status": "completed"}, {"name": "migrate db", "status": "in_progress"},
                             {"name": "smoke test", "status": "queued"}]}]
    sync_github(conn, s, gh)
    dep.sync_deployments(conn, s, gh)
    runs = dep.overview(conn, s)["runs"]
    assert [r["workflow"] for r in runs] == ["Deploy web"]                     # tests aren't deploys
    p = runs[0]["progress"]
    assert p["done"] == 3 and p["total"] == 5 and p["current"] == "deploy: migrate db"
    assert runs[0]["creator"] == "sara-k" and dep.overview(conn, s)["running"][0]["id"] == "run:9"


def test_disabled_without_github(env):
    s, conn, gh, posts = env
    s.github.enabled = False
    assert dep.sync_deployments(conn, s, gh) == {"skipped": True}
