"""Minimal Jira Cloud REST v3 client (read-only)."""
from __future__ import annotations

import time
from typing import Iterator

import requests

FIELDS = [
    "summary", "status", "priority", "assignee", "reporter", "created", "updated",
    "resolutiondate", "components", "labels", "timeoriginalestimate", "timespent",
    "project", "issuetype",
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

    def search(self, jql: str, page_size: int = 100) -> Iterator[dict]:
        """Yield every issue matching JQL using the enhanced search endpoint."""
        token = None
        while True:
            body = {"jql": jql, "fields": FIELDS, "maxResults": page_size}
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

    def browse_url(self, key: str) -> str:
        return f"{self.base_url}/browse/{key}"
