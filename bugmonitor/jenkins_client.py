"""Read-only Jenkins JSON API client: jobs (folders and multibranch), builds, stages, and test reports.

Every request goes to the configured Jenkins address. URLs that Jenkins returns (which follow its own
"root URL" setting and can point at localhost or plain http) are never followed, so the token can't leak.
"""
from __future__ import annotations

import time
from urllib.parse import quote, unquote

import requests

FOLDER_CLASSES = ("Folder", "OrganizationFolder", "WorkflowMultiBranchProject", "ComputedFolder")
CHANGES = "items[msg,commitId,author[fullName,id]]"
BUILD_TREE = ("builds[number,result,building,timestamp,duration,estimatedDuration,"
              "actions[causes[shortDescription,userId,userName],lastBuiltRevision[SHA1,branch[name]],remoteUrls,"
              "parameters[name,value]],changeSets[" + CHANGES + "],changeSet[" + CHANGES + "]]{0,%d}")
CASES = "cases[className,name,status,errorDetails]"


class JenkinsError(RuntimeError):
    pass


class JenkinsClient:
    def __init__(self, url: str, user: str, token: str, ca_bundle: str = "", timeout: int = 30):
        if not (url and user and token):
            raise JenkinsError("Jenkins isn't configured. Set url and user under [jenkins] and add your API token "
                               "with `bugmonitor init`.")
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.auth = (user, token)
        self.s.headers.update({"Accept": "application/json"})
        if ca_bundle:
            self.s.verify = ca_bundle
        self.timeout = timeout

    # ---- URLs: always built from the configured address
    @staticmethod
    def job_path(full_name: str) -> str:
        return "/".join(f"job/{quote(part, safe='')}" for part in full_name.split("/"))

    def job_url(self, full_name: str) -> str:
        return f"{self.url}/{self.job_path(full_name)}/"

    def build_url(self, full_name: str, number: int) -> str:
        return f"{self.job_url(full_name)}{int(number)}/"

    def get(self, path_or_url: str, params: dict | None = None, allow_404: bool = False):
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else f"{self.url}/{path_or_url.lstrip('/')}"
        if not url.startswith(self.url + "/"):
            raise JenkinsError(f"Refusing to send your Jenkins token to {url.split('/')[2] if '//' in url else url}")
        for attempt in range(3):
            try:
                r = self.s.get(url, params=params, timeout=self.timeout, allow_redirects=False)
            except requests.exceptions.SSLError as e:
                raise JenkinsError(f"Jenkins certificate isn't trusted ({e.__class__.__name__}). "
                                   "Set ca_bundle under [jenkins] to your company's CA file.") from e
            if r.status_code in (502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 404 and allow_404:
                return None
            if r.status_code in (301, 302, 303, 307, 308):
                raise JenkinsError(f"Jenkins redirected to {r.headers.get('Location', '?')}. "
                                   "Use that address as [jenkins] url.")
            if r.status_code in (401, 403):
                raise JenkinsError(f"Jenkins rejected the login ({r.status_code}). Check [jenkins] user and the API token.")
            if not r.ok:
                raise JenkinsError(f"Jenkins {r.status_code} on {url}: {r.text[:200]}")
            return r.json()
        raise JenkinsError(f"Jenkins kept failing on {url}")

    def whoami(self) -> str:
        me = self.get("me/api/json", {"tree": "id,fullName"}) or {}
        return me.get("fullName") or me.get("id") or ""

    def list_jobs(self, root: str = "", depth: int = 4) -> list[dict]:
        """Every buildable job under root (folders and multibranch projects are walked), with its last build."""
        out: list[dict] = []
        tree = "jobs[name,fullName,_class,color,lastBuild[number,building,timestamp]]"

        def leaf(j: dict, full_name: str, multibranch: bool) -> dict:
            lb = j.get("lastBuild") or {}
            return {"name": j.get("name"), "full_name": full_name, "url": self.job_url(full_name),
                    "class": (j.get("_class") or "").rsplit(".", 1)[-1], "color": j.get("color"),
                    # In a multibranch project each child job is one branch; its name is the branch.
                    "branch": unquote(j.get("name") or "") if multibranch else None,
                    "last_build": lb.get("timestamp"), "last_number": lb.get("number"), "last_building": bool(lb.get("building"))}

        def walk(full: str, level: int, parent_cls: str = ""):
            data = self.get((self.job_path(full) + "/" if full else "") + "api/json", {"tree": tree}, allow_404=True) or {}
            for j in data.get("jobs") or []:
                cls = (j.get("_class") or "").rsplit(".", 1)[-1]
                full_name = j.get("fullName") or f"{full}/{j['name']}".strip("/")
                if any(cls.endswith(c) for c in FOLDER_CLASSES):
                    if level < depth:
                        walk(full_name, level + 1, cls)
                    continue
                out.append(leaf(j, full_name, parent_cls.endswith(("MultiBranchProject", "OrganizationFolder"))))
        if root:
            info = self.get(self.job_path(root) + "/api/json",
                            {"tree": "_class,name,fullName,color,lastBuild[number,building,timestamp]"}, allow_404=True)
            if not info:
                return []
            cls = (info.get("_class") or "").rsplit(".", 1)[-1]
            if not any(cls.endswith(c) for c in FOLDER_CLASSES):
                return [leaf(info, info.get("fullName") or root, False)]
            walk(root, 1, cls)
            return out
        walk("", 0)
        return out

    def builds(self, full_name: str, limit: int = 20) -> list[dict]:
        data = self.get(self.job_path(full_name) + "/api/json", {"tree": BUILD_TREE % limit}) or {}
        out = data.get("builds") or []
        for b in out:
            b["url"] = self.build_url(full_name, b["number"])
            # Freestyle and matrix jobs report one changeSet; pipelines report a list.
            if b.get("changeSet") and not b.get("changeSets"):
                b["changeSets"] = [b["changeSet"]]
        return out

    def build(self, full_name: str, number: int) -> dict | None:
        b = self.get(self.job_path(full_name) + f"/{int(number)}/api/json",
                     {"tree": "number,result,building,timestamp,duration"}, allow_404=True)
        if b:
            b["url"] = self.build_url(full_name, number)
        return b

    def stages(self, full_name: str, number: int) -> list[dict] | None:
        """Pipeline stages (needs the Pipeline Stage View plugin); None if unavailable."""
        try:
            data = self.get(self.job_path(full_name) + f"/{int(number)}/wfapi/describe", allow_404=True)
        except JenkinsError:
            return None
        if not data:
            return None
        return [{"name": s.get("name"), "status": s.get("status"), "duration_s": round((s.get("durationMillis") or 0) / 1000, 1)}
                for s in data.get("stages") or []]

    def test_report(self, full_name: str, number: int) -> dict | None:
        """JUnit results; None if the build has no test report. Cases are only fetched when something failed,
        so big passing suites cost one small request."""
        path = self.job_path(full_name) + f"/{int(number)}/testReport/api/json"
        summary = self.get(path, {"tree": "passCount,failCount,skipCount,totalCount"}, allow_404=True)
        if summary is None:
            return None
        if not summary.get("failCount"):
            return summary
        return self.get(path, {"tree": "passCount,failCount,skipCount,totalCount,suites[" + CASES + "],"
                                       "childReports[result[passCount,failCount,skipCount,suites[" + CASES + "]]]"},
                        allow_404=True) or summary
