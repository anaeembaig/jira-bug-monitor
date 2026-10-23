"""Read-only GitHub GraphQL client for your pull requests and branches."""
from __future__ import annotations

import shutil
import subprocess
import time

import requests

API = "https://api.github.com/graphql"

PR_FIELDS = """
... on PullRequest {
  id number title url state isDraft createdAt updatedAt mergedAt closedAt
  headRefName baseRefName reviewDecision mergeable mergeStateStatus additions deletions
  repository { nameWithOwner }
  author { login }
  assignees(first: 10) { nodes { login } }
  labels(first: 20) { nodes { name color } }
  reviewRequests(first: 20) { nodes { requestedReviewer {
    ... on User { login } ... on Team { name } } } }
  latestReviews(first: 20) { nodes { author { login } state submittedAt url } }
  reviews(last: 30) { nodes { id author { login } state submittedAt url body } }
  comments(last: 20) { totalCount nodes { id author { login } body createdAt url } }
  reviewThreads(first: 50) { totalCount nodes { isResolved isOutdated path line
    comments(first: 1) { totalCount nodes { id author { login } body createdAt url } } } }
  files(first: 80) { nodes { path additions deletions } }
  commits(last: 1) { nodes { commit { statusCheckRollup { state
    contexts(first: 40) { nodes {
      ... on CheckRun { name conclusion status detailsUrl }
      ... on StatusContext { context state targetUrl } } } } } } }
}
"""

SEARCH_Q = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 25, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { %s }
  }
}
""" % PR_FIELDS

BRANCHES_Q = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    refs(refPrefix: "refs/heads/", first: 60,
         orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) {
      nodes {
        name
        target { ... on Commit {
          oid committedDate messageHeadline
          author { user { login } }
          statusCheckRollup { state }
        } }
        associatedPullRequests(first: 1, orderBy: {field: UPDATED_AT, direction: DESC}) {
          nodes { number state url isDraft }
        }
      }
    }
  }
}
"""


DEPLOYMENTS_Q = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    deployments(first: 40, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        id databaseId environment state createdAt updatedAt description task commitOid
        ref { name } creator { login }
        statuses(first: 20) { nodes { state description createdAt logUrl environmentUrl creator { login } } }
      }
    }
  }
}
"""


class GitHubError(RuntimeError):
    pass


def resolve_token(configured: str) -> str:
    """Use the configured token, or fall back to the GitHub CLI's login if installed."""
    if configured:
        return configured
    gh = shutil.which("gh")
    if gh:
        try:
            out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10)
            return out.stdout.strip()
        except Exception:
            pass
    return ""


class GitHubClient:
    def __init__(self, token: str, timeout: int = 30):
        if not token:
            raise GitHubError("GitHub is not configured. Set [github] token in config.toml.")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json"})
        self.timeout = timeout

    def query(self, q: str, variables: dict | None = None) -> dict:
        for attempt in range(4):
            r = self.s.post(API, json={"query": q, "variables": variables or {}},
                            timeout=self.timeout)
            if r.status_code in (502, 503) or (r.status_code == 403 and "rate" in r.text.lower()):
                time.sleep(min(2 ** attempt * 5, 60))
                continue
            if r.status_code == 401:
                raise GitHubError("GitHub rejected the token (401). Check [github] token.")
            if not r.ok:
                raise GitHubError(f"GitHub {r.status_code}: {r.text[:300]}")
            data = r.json()
            if data.get("errors") and not data.get("data"):
                msg = data["errors"][0].get("message", "unknown error")
                if "SAML" in msg or "SSO" in msg.upper():
                    msg += " (authorize the token for your organization's SSO on github.com)"
                raise GitHubError(f"GitHub: {msg}")
            return data.get("data") or {}
        raise GitHubError("GitHub kept failing after retries")

    def viewer(self) -> str:
        return self.query("query { viewer { login } }")["viewer"]["login"]

    def search_prs(self, q: str, limit: int = 300) -> list[dict]:
        out, after = [], None
        while len(out) < limit:
            res = self.query(SEARCH_Q, {"q": q, "after": after})["search"]
            out += [n for n in res["nodes"] if n]
            if not res["pageInfo"]["hasNextPage"]:
                break
            after = res["pageInfo"]["endCursor"]
        return out

    def rest(self, path: str, params: dict | None = None) -> dict:
        r = self.s.get(f"https://api.github.com{path}", params=params, timeout=self.timeout)
        if r.status_code == 404:
            return {}
        if not r.ok:
            raise GitHubError(f"GitHub {r.status_code} on {path}: {r.text[:200]}")
        return r.json()

    def deployments(self, repo: str) -> list[dict]:
        owner, name = repo.split("/", 1)
        data = self.query(DEPLOYMENTS_Q, {"owner": owner, "name": name}).get("repository") or {}
        return ((data.get("deployments") or {}).get("nodes") or [])

    def compare(self, repo: str, base: str, head: str) -> dict:
        return self.rest(f"/repos/{repo}/compare/{base}...{head}", {"per_page": 250})

    def workflow_runs(self, repo: str, since: str) -> list[dict]:
        return (self.rest(f"/repos/{repo}/actions/runs", {"per_page": 30, "created": f">={since}"})
                .get("workflow_runs") or [])

    def run_jobs(self, repo: str, run_id: int) -> list[dict]:
        return self.rest(f"/repos/{repo}/actions/runs/{run_id}/jobs", {"per_page": 50}).get("jobs") or []

    def ci_runs(self, repo: str, branch: str, since: str) -> list[dict]:
        return (self.rest(f"/repos/{repo}/actions/runs", {"per_page": 60, "branch": branch, "created": f">={since}"})
                .get("workflow_runs") or [])

    def run_artifacts(self, repo: str, run_id: int) -> list[dict]:
        return self.rest(f"/repos/{repo}/actions/runs/{run_id}/artifacts", {"per_page": 50}).get("artifacts") or []

    def download_artifact(self, url: str, max_bytes: int) -> bytes | None:
        """Artifact zip. GitHub redirects to storage; requests drops the token on the cross-host redirect."""
        with self.s.get(url, timeout=60, stream=True) as r:
            if not r.ok:
                return None
            data = bytearray()
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > max_bytes:
                    return None
            return bytes(data)

    def branch_checks(self, repo: str, branch: str) -> dict:
        """Check runs and commit statuses on the head of a branch (Jenkins usually reports as a status)."""
        runs = self.rest(f"/repos/{repo}/commits/{branch}/check-runs", {"per_page": 100}).get("check_runs") or []
        status = self.rest(f"/repos/{repo}/commits/{branch}/status") or {}
        return {"sha": status.get("sha") or (runs[0].get("head_sha") if runs else None),
                "checks": [{"name": c.get("name"), "state": (c.get("conclusion") or c.get("status") or "").lower(),
                            "app": (c.get("app") or {}).get("name"), "url": c.get("html_url"),
                            "at": c.get("completed_at") or c.get("started_at")} for c in runs]
                          + [{"name": st.get("context"), "state": (st.get("state") or "").lower(), "app": "status",
                              "url": st.get("target_url"), "at": st.get("updated_at")} for st in status.get("statuses") or []]}

    def default_branch(self, repo: str) -> str | None:
        return (self.rest(f"/repos/{repo}") or {}).get("default_branch")

    def security_alerts(self, repo: str) -> dict:
        """Open Dependabot and code scanning alerts. Repos without access or without the feature return []."""
        out = {}
        for kind, path in (("dependabot", "dependabot/alerts"), ("code_scanning", "code-scanning/alerts")):
            r = self.s.get(f"https://api.github.com/repos/{repo}/{path}", params={"state": "open", "per_page": 100},
                           timeout=self.timeout)
            if r.ok and isinstance(r.json(), list):
                out[kind] = r.json()
                continue
            text = (r.text or "").lower()
            if r.status_code == 404 or (r.status_code == 403 and any(
                    w in text for w in ("disabled", "not enabled", "must be enabled", "no analysis", "not authorized"))):
                out[kind] = []      # the feature is off for this repo, or the token can't see it: nothing to show
                continue
            raise GitHubError(f"GitHub {r.status_code} on {repo} {kind} alerts: {r.text[:200]}")
        return out

    def branches(self, repo: str) -> dict:
        owner, name = repo.split("/", 1)
        return self.query(BRANCHES_Q, {"owner": owner, "name": name}).get("repository") or {}
