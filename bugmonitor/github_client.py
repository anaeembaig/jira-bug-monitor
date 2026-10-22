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

    def branches(self, repo: str) -> dict:
        owner, name = repo.split("/", 1)
        return self.query(BRANCHES_Q, {"owner": owner, "name": name}).get("repository") or {}
