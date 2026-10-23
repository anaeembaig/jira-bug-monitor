"""Test results from Jenkins and GitHub Actions, and flaky-test detection.

Only failing tests are stored. A build with a report and no row for a test means the test passed there.
A test is flaky when it failed and passed on the same commit, or keeps flipping pass -> fail -> pass.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import datetime, timedelta, timezone

from .config import Settings

try:                                    # CI artifacts are untrusted input: refuse entity tricks if we can
    from defusedxml import ElementTree as ET
except ImportError:                     # pragma: no cover
    import xml.etree.ElementTree as ET  # nosec - falls back to the stdlib parser with size limits below

log = logging.getLogger("bugmonitor.testhealth")
FAILED = {"FAILED", "REGRESSION", "FAILURE", "ERROR"}
ARTIFACT_RE = re.compile(r"test|junit|report|result|surefire", re.I)
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_XML_BYTES = 10 * 1024 * 1024


def store_report(conn, build_id: str, source: str, pipeline: str, sha: str | None, ts: str | None, url: str | None,
                 cases: list[tuple[str, str, str]] | None, branch: str | None = None, result: str | None = None,
                 totals: tuple[int, int, int] | None = None) -> None:
    """cases: (test name, status, error); only failures need to be listed when `totals` gives the counts.
    cases None and no totals = this build has no test report."""
    if cases is None and totals is None:
        conn.execute("INSERT OR REPLACE INTO test_runs(build_id, source, pipeline, sha, ts, url, total, failed, skipped, "
                     "branch, result) VALUES(?,?,?,?,?,?,-1,0,0,?,?)", (build_id, source, pipeline, sha, ts, url, branch, result))
        return
    cases = cases or []
    failed = [(n, e) for n, st, e in cases if st.upper() in FAILED]
    total, n_failed, skipped = totals or (len(cases), len(failed), sum(1 for _, st, _ in cases if st.upper() == "SKIPPED"))
    conn.execute("INSERT OR REPLACE INTO test_runs(build_id, source, pipeline, sha, ts, url, total, failed, skipped, branch, "
                 "result) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (build_id, source, pipeline, sha, ts, url, total, n_failed, skipped, branch, result))
    conn.execute("DELETE FROM test_failures WHERE build_id=?", (build_id,))
    conn.executemany("INSERT OR REPLACE INTO test_failures(build_id, test, error) VALUES(?,?,?)",
                     [(build_id, n[:400], (e or "")[:600]) for n, e in failed[:500]])


def jenkins_cases(report: dict) -> list[tuple[str, str, str]]:
    suites = list(report.get("suites") or [])
    for child in report.get("childReports") or []:        # matrix and Maven module builds
        suites += (child.get("result") or {}).get("suites") or []
    out = []
    for suite in suites:
        for c in suite.get("cases") or []:
            name = f"{c.get('className') or ''}.{c.get('name') or ''}".strip(".")
            out.append((name, c.get("status") or "PASSED", (c.get("errorDetails") or "").strip()))
    return out


def jenkins_totals(report: dict) -> tuple[int, int, int]:
    p, f, sk = report.get("passCount") or 0, report.get("failCount") or 0, report.get("skipCount") or 0
    return (report.get("totalCount") or p + f + sk), f, sk


def junit_cases(xml_bytes: bytes) -> list[tuple[str, str, str]]:
    """Test cases from a JUnit XML file (testsuites or testsuite root)."""
    if len(xml_bytes) > MAX_XML_BYTES:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []
    out = []
    for tc in root.iter("testcase"):
        name = f"{tc.get('classname') or ''}.{tc.get('name') or ''}".strip(".")
        status, error = "PASSED", ""
        for child in tc:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag in ("failure", "error"):
                status, error = "FAILED", (child.get("message") or (child.text or "")).strip()
            elif tag == "skipped" and status == "PASSED":
                status = "SKIPPED"
        out.append((name, status, error))
    return out


def cases_from_zip(data: bytes) -> list[tuple[str, str, str]] | None:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None
    cases: list = []
    for info in z.infolist():
        if info.filename.lower().endswith(".xml") and info.file_size <= MAX_XML_BYTES:
            cases += junit_cases(z.read(info))
    return cases or None


# --------------------------------------------------------------------------- GitHub Actions artifacts
def ingest_github_tests(conn, settings: Settings, client, alerter=None, limit: int = 8) -> dict:
    """JUnit XML from Actions artifacts for finished default-branch runs not read yet. Each re-run attempt is
    read separately, so a run that failed and then passed on retry shows up as flaky."""
    if not (settings.github.enabled and settings.code.ci) or not hasattr(client, "run_artifacts"):
        return {"skipped": True}
    rows = conn.execute(
        "SELECT * FROM ci_runs WHERE status='completed' AND conclusion IN ('success','failure') "
        "AND 'gh:' || id || ':' || COALESCE(attempt, 1) NOT IN (SELECT build_id FROM test_runs) "
        "ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    read = 0
    for r in rows:
        build_id, pipeline = f"gh:{r['id']}:{r['attempt'] or 1}", f"{r['repo']} · {r['workflow']}"
        cases = None
        try:
            for a in client.run_artifacts(r["repo"], r["id"]):
                if a.get("expired") or not ARTIFACT_RE.search(a.get("name") or "") or (a.get("size_in_bytes") or 0) > MAX_ARTIFACT_BYTES:
                    continue
                data = client.download_artifact(a["archive_download_url"], MAX_ARTIFACT_BYTES)
                found = cases_from_zip(data) if data else None
                if found:
                    cases = (cases or []) + found
        except Exception as e:
            log.warning("Test artifacts for %s run %s: %s", r["repo"], r["id"], e)
            continue
        store_report(conn, build_id, "github", pipeline, r["sha"], r["created"], r["url"], cases,
                     branch=r["branch"], result=(r["conclusion"] or "").upper())
        read += cases is not None
    conn.commit()
    return {"runs": len(rows), "with_tests": read}


# --------------------------------------------------------------------------- analysis
def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


NOT_EVIDENCE = {"ABORTED", "CANCELLED", "NOT_BUILT", "SKIPPED"}


def flaky_tests(conn, settings: Settings, days: int = 14, limit: int = 50) -> dict:
    """Flaky: failed and passed on the same commit (anywhere in the pipeline), or flips pass -> fail -> pass
    twice on one branch. A run only counts as a pass for a test if it finished normally and ran about as
    many tests as the failing run, so a partial or aborted re-run never looks like a pass."""
    runs = [dict(r) for r in conn.execute("SELECT * FROM test_runs WHERE total >= 0 AND ts >= ? ORDER BY ts", (_since(days),))]
    ids = {r["build_id"] for r in runs}
    fails: dict = {}
    for f in conn.execute("SELECT build_id, test, error FROM test_failures"):
        if f["build_id"] in ids:
            fails.setdefault(f["build_id"], {})[f["test"]] = f["error"]
    by_pipeline: dict = {}
    for r in runs:
        by_pipeline.setdefault(r["pipeline"], []).append(r)
    tests: dict = {}

    def entry(name):
        return tests.setdefault(name, {"test": name, "pipelines": set(), "failures": 0, "runs": 0, "same_commit": 0,
                                       "flips": 0, "last_failed": None, "last_error": "", "last_url": None,
                                       "failing_now": False})

    for pipeline, rs in by_pipeline.items():
        names = {t for r in rs for t in fails.get(r["build_id"], {})}
        for name in names:
            failed_in = lambda r: name in fails.get(r["build_id"], {})
            t = entry(name)
            t["pipelines"].add(pipeline)
            # Same commit, any branch of this pipeline.
            by_sha: dict = {}
            for r in rs:
                if r["sha"]:
                    by_sha.setdefault(r["sha"], []).append(r)
            for sha_runs in by_sha.values():
                bad = [r for r in sha_runs if failed_in(r)]
                if not bad:
                    continue
                need = max(r["total"] for r in bad) * 0.9
                good = [r for r in sha_runs if not failed_in(r) and (r["result"] or "").upper() not in NOT_EVIDENCE
                        and r["total"] >= need]
                t["same_commit"] += bool(good)
            # Flips and "failing now", one branch at a time.
            branches: dict = {}
            for r in rs:
                branches.setdefault(r["branch"] or "", []).append(r)
            for br_runs in branches.values():
                first = next((i for i, r in enumerate(br_runs) if failed_in(r)), None)
                if first is None:
                    continue
                seq = br_runs[max(first - 1, 0):]
                states = [failed_in(r) for r in seq]
                t["flips"] += sum(1 for i in range(1, len(states) - 1) if states[i] and not states[i - 1] and not states[i + 1])
                t["runs"] += len(seq)
                t["failures"] += sum(states)
                if len(states) >= 2 and states[-1] and states[-2]:
                    t["failing_now"] = True
                last = [r for r in seq if failed_in(r)][-1]
                if not t["last_failed"] or last["ts"] > t["last_failed"]:
                    t["last_failed"], t["last_url"] = last["ts"], last["url"]
                    t["last_error"] = fails[last["build_id"]][name]
    out = []
    for t in tests.values():
        t["pipelines"] = sorted(t["pipelines"])
        t["fail_rate"] = round(t["failures"] / t["runs"] * 100) if t["runs"] else None
        t["flaky"] = t["same_commit"] >= 1 or t["flips"] >= 2
        t["score"] = t["same_commit"] * 3 + t["flips"] * 2 + (t["fail_rate"] or 0) / 100
        out.append(t)
    flaky = sorted((t for t in out if t["flaky"]), key=lambda t: -t["score"])
    broken = sorted((t for t in out if t["failing_now"] and not t["flaky"]), key=lambda t: t["last_failed"] or "", reverse=True)
    return {"days": days, "builds_with_tests": len(runs), "pipelines": len(by_pipeline), "flaky": flaky[:limit],
            "failing_now": broken[:limit], "tests_failed": len(out),
            "fail_rate": round(sum(1 for r in runs if r["failed"]) / len(runs) * 100) if runs else None}


def prune(conn, days: int) -> int:
    old = [r[0] for r in conn.execute("SELECT build_id FROM test_runs WHERE ts < ?", (_since(days + 1),))]
    for i in range(0, len(old), 500):
        chunk = old[i:i + 500]
        q = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM test_failures WHERE build_id IN ({q})", chunk)
        conn.execute(f"DELETE FROM test_runs WHERE build_id IN ({q})", chunk)
    return len(old)
