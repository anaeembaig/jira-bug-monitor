"""Minimal Jira Cloud REST v3 client (read-only)."""
from __future__ import annotations

import time
from typing import Iterator

import requests

FIELDS = [
    "summary", "status", "priority", "assignee", "reporter", "created", "updated",
    "resolutiondate", "components", "labels", "timeoriginalestimate", "timespent",
    "project", "issuetype", "fixVersions",
]


class JiraError(RuntimeError):
    pass


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, timeout: int = 30):
        if not (base_url and email and api_token):
            raise JiraError(
                "Jira is not configured. Set base_url and email in config.toml "
                "and JIRA_API_TOKEN in your environment."
            )
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (email, api_token)
        self.session.headers.update({"Accept": "application/json"})
        self.timeout = timeout

    def _request(self, method: str, path: str, **kw) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(5):
            r = self.session.request(method, url, timeout=self.timeout, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(wait, 60))
                continue
            if r.status_code == 401:
                raise JiraError("Jira rejected the credentials (401). Check email and API token.")
            if not r.ok:
                raise JiraError(f"Jira {r.status_code} on {path}: {r.text[:300]}")
            return r.json()
        raise JiraError(f"Jira kept failing on {path} after retries")

    def myself(self) -> dict:
        return self._request("GET", "/rest/api/3/myself")

    def search(self, jql: str, page_size: int = 100, extra_fields: list[str] | None = None) -> Iterator[dict]:
        """Yield every issue matching JQL using the enhanced search endpoint."""
        token = None
        fields = FIELDS + list(extra_fields or [])
        while True:
            body = {"jql": jql, "fields": fields, "maxResults": page_size}
            if token:
                body["nextPageToken"] = token
            data = self._request("POST", "/rest/api/3/search/jql", json=body)
            yield from data.get("issues", [])
            token = data.get("nextPageToken")
            if data.get("isLast", True) or not token:
                return

    def changelog(self, key: str) -> Iterator[dict]:
        start = 0
        while True:
            data = self._request(
                "GET", f"/rest/api/3/issue/{key}/changelog",
                params={"startAt": start, "maxResults": 100},
            )
            values = data.get("values", [])
            yield from values
            start += len(values)
            if data.get("isLast", True) or not values:
                return

    def projects(self) -> list[dict]:
        out, start = [], 0
        while True:
            data = self._request("GET", "/rest/api/3/project/search",
                                 params={"startAt": start, "maxResults": 100, "orderBy": "key"})
            vals = data.get("values", [])
            out += [{"key": p["key"], "name": p.get("name", "")} for p in vals]
            start += len(vals)
            if data.get("isLast", True) or not vals:
                return out

    def fields(self) -> list[dict]:
        return self._request("GET", "/rest/api/3/field")

    def comments(self, key: str, max_results: int = 50) -> list[dict]:
        data = self._request("GET", f"/rest/api/3/issue/{key}/comment",
                             params={"maxResults": max_results, "orderBy": "-created"})
        return data.get("comments", [])

    def worklogs(self, key: str, started_after_ms: int | None = None) -> list[dict]:
        out, start = [], 0
        while True:
            params = {"startAt": start, "maxResults": 1000}
            if started_after_ms:
                params["startedAfter"] = started_after_ms
            data = self._request("GET", f"/rest/api/3/issue/{key}/worklog", params=params)
            vals = data.get("worklogs", [])
            out += vals
            start += len(vals)
            if not vals or start >= data.get("total", 0):
                return out

    def add_worklog(self, key: str, seconds: int, started: str, comment: str = "") -> dict:
        """Write access: only used when [timelog] allow_jira_write = true."""
        body = {"timeSpentSeconds": int(seconds), "started": started}
        if comment:
            body["comment"] = {"type": "doc", "version": 1, "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": comment}]}]}
        return self._request("POST", f"/rest/api/3/issue/{key}/worklog", json=body)

    def versions(self, project: str) -> list[dict]:
        return self._request("GET", f"/rest/api/3/project/{project}/versions")

    # ---- Jira Software (boards and sprints)
    def boards(self, project: str) -> list[dict]:
        data = self._request("GET", "/rest/agile/1.0/board", params={"projectKeyOrId": project, "maxResults": 50})
        return data.get("values", [])

    def sprints(self, board_id: int, state: str = "active,future") -> list[dict]:
        data = self._request("GET", f"/rest/agile/1.0/board/{board_id}/sprint",
                             params={"state": state, "maxResults": 50})
        return data.get("values", [])

    def sprint_issues(self, sprint_id: int, extra_fields: list[str] | None = None) -> Iterator[dict]:
        start = 0
        while True:
            data = self._request("GET", f"/rest/agile/1.0/sprint/{sprint_id}/issue",
                                 params={"startAt": start, "maxResults": 100,
                                         "fields": ",".join(FIELDS + list(extra_fields or []))})
            issues = data.get("issues", [])
            yield from issues
            start += len(issues)
            if not issues or start >= data.get("total", 0):
                return

    def browse_url(self, key: str) -> str:
        return f"{self.base_url}/browse/{key}"
