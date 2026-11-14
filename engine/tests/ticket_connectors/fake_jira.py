"""A fake Jira Cloud (REST v3) with the real response shapes: ADF bodies and comments, the
token-paginated /search/jql, offset-paginated /project/search, statusCategory, transitions,
remote links, 401 errorMessages, 429 Retry-After — and Atlassian OAuth 2.0 (3LO): /authorize
codes, /oauth/token (code exchange + ROTATING refresh tokens), accessible-resources, and the
api.atlassian.com/ex/jira/{cloudId} API base."""

from __future__ import annotations

import base64
import copy
import itertools
import re
import threading
import time
import uuid
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from . import dataset as D
from .fake_http import FakeServer, Request

EMAIL = "bot@example.com"
TOKEN = "jira-api-token-fake-0123456789"
CLIENT_ID = "jira-oauth-client"
CLIENT_SECRET = "jira-oauth-secret-fake-0123456789"
CLOUD_ID = "11111111-2222-3333-4444-555555555555"
STATUS_DEFS = {
    "To Do": ("10000", "new"), "In Progress": ("3", "indeterminate"),
    "In Review": ("10001", "indeterminate"), "Done": ("10002", "done"),
}
CATEGORY_NAMES = {"new": "To Do", "indeterminate": "In Progress", "done": "Done"}


def adf(markdown: str) -> dict[str, Any]:
    content = []
    for block in markdown.split("\n\n"):
        lines = block.split("\n")
        if block.startswith("## "):
            content.append({"type": "heading", "attrs": {"level": 2},
                            "content": [{"type": "text", "text": block[3:]}]})
        elif all(line.startswith("- ") for line in lines):
            content.append({"type": "bulletList", "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": [
                    {"type": "text", "text": line[2:]}]}]} for line in lines]})
        else:
            content.append({"type": "paragraph", "content": [{"type": "text", "text": block}]})
    return {"type": "doc", "version": 1, "content": content}


def _user(name: Optional[str]) -> Optional[dict[str, Any]]:
    if not name:
        return None
    return {"accountId": name.lower().replace(" ", "-"), "displayName": name, "active": True}


class FakeJira(FakeServer):
    def __init__(self, *, project_page_cap: int = 2) -> None:
        super().__init__()
        self.project_page_cap = project_page_cap
        self.counter = itertools.count(1000)
        self.issues: dict[str, dict[str, Any]] = {}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.links: dict[str, list[dict[str, Any]]] = {}
        self.projects = [D.PROJECT] + D.OTHER_PROJECTS
        for n, item in D.ISSUES.items():
            self._add(f"KAN-{n}", copy.deepcopy(item))
        # OAuth state
        self.codes: dict[str, str] = {}                 # code -> redirect_uri
        self.access: dict[str, float] = {}              # access token -> expiry
        self.refresh_tokens: set[str] = set()
        self.refreshes = 0
        self.refresh_delay = 0.0
        self.oauth_lock = threading.Lock()
        self.me_email = "ada@example.com"      # what /me answers for any signed-in token
        self._routes()

    # -- data ---------------------------------------------------------------------------------

    def _add(self, key: str, item: dict[str, Any]) -> dict[str, Any]:
        number = int(key.rsplit("-", 1)[1])
        parent = item.get("parent")
        issue = {
            "id": str(10000 + number), "key": key, "title": item["title"],
            "type": item["type"], "status": item["status"],
            "description": adf(item.get("body") or "") if item.get("body") else None,
            "labels": list(item.get("labels") or []), "assignee": item.get("assignee"),
            "reporter": item.get("reporter") or "Grace Hopper",
            "priority": item.get("priority") or "Medium",
            "parent": f"KAN-{parent}" if isinstance(parent, int) else parent,
            "attachments": item.get("attachments") or [],
            "updated": f"2026-11-{1 + number % 12:02d}T10:00:00.000+0000",
            "project": key.rsplit("-", 1)[0],
        }
        self.issues[key] = issue
        self.comments[key] = [
            {"id": str(next(self.counter)), "author": _user(author), "body": adf(text),
             "created": f"2026-11-0{i + 1}T09:00:00.000+0000"}
            for i, (author, text) in enumerate(item.get("comments") or [])]
        self.links[key] = []
        return issue

    def site(self) -> str:
        return self.base

    def _issue_json(self, issue: dict[str, Any], fields: Optional[list[str]] = None) -> dict:
        status_id, category = STATUS_DEFS[issue["status"]]
        all_fields = {
            "summary": issue["title"], "description": issue["description"],
            "issuetype": {"id": "1", "name": issue["type"], "subtask": False},
            "status": {"id": status_id, "name": issue["status"],
                       "statusCategory": {"id": 2, "key": category,
                                          "name": CATEGORY_NAMES[category]}},
            "priority": {"name": issue["priority"]}, "labels": issue["labels"],
            "assignee": _user(issue["assignee"]), "reporter": _user(issue["reporter"]),
            "parent": ({"id": self.issues[issue["parent"]]["id"], "key": issue["parent"]}
                       if issue["parent"] else None),
            "attachment": [{"id": str(i), "filename": name, "size": size,
                            "content": f"{self.base}/rest/api/3/attachment/content/{i}",
                            "mimeType": "image/png"}
                           for i, (name, size) in enumerate(issue["attachments"])],
            "updated": issue["updated"], "subtasks": [],
            "project": {"key": issue["project"], "name": D.PROJECT["name"]},
        }
        if fields:
            all_fields = {k: v for k, v in all_fields.items() if k in fields}
        return {"id": issue["id"], "key": issue["key"],
                "self": f"{self.base}/rest/api/3/issue/{issue['id']}", "fields": all_fields}

    # -- auth -----------------------------------------------------------------------------------

    def authorized(self, request: Request) -> bool:
        if request.path.startswith("/oauth/") or request.path == "/me":
            return True          # these check their own bearer
        header = request.headers.get("authorization", "")
        if request.path.startswith("/ex/jira/"):
            if not header.startswith("Bearer "):
                return False
            expiry = self.access.get(header[7:])
            return expiry is not None and expiry > time.time()
        expected = "Basic " + base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
        return header == expected

    # -- JQL (the clauses the adapter writes) ---------------------------------------------------

    def _match(self, issue: dict[str, Any], clause: str) -> bool:
        clause = clause.strip()
        if clause.startswith("(") and clause.endswith(")"):
            return all(self._match(issue, c) for c in clause[1:-1].split(" AND "))
        rules = [
            (r'project = "?([^"]+)"?', lambda m: issue["project"] == m.group(1)),
            (r'issuetype = "?([^"]+)"?', lambda m: issue["type"].lower() == m.group(1).lower()),
            (r"issuetype != Epic", lambda m: issue["type"] != "Epic"),
            (r"issuetype not in subTaskIssueTypes\(\)", lambda m: True),
            (r"statusCategory != Done", lambda m: STATUS_DEFS[issue["status"]][1] != "done"),
            (r'statusCategory = "?([^"]+)"?',
             lambda m: CATEGORY_NAMES[STATUS_DEFS[issue["status"]][1]] == m.group(1)),
            (r"parent = ([A-Z]+-\d+)", lambda m: issue["parent"] == m.group(1)),
            (r"parent is EMPTY", lambda m: not issue["parent"]),
            (r'key = "?([^"]+)"?', lambda m: issue["key"] == m.group(1)),
            (r'text ~ "([^"]*)"', lambda m: m.group(1).lower() in (issue["title"] + " " + str(
                issue["description"])).lower()),
            (r'labels = "([^"]+)"', lambda m: m.group(1) in issue["labels"]),
            (r'status = "([^"]+)"', lambda m: issue["status"] == m.group(1)),
            (r'assignee = "([^"]+)"', lambda m: issue["assignee"] == m.group(1)),
        ]
        for pattern, test in rules:
            m = re.fullmatch(pattern, clause)
            if m:
                return bool(test(m))
        raise ValueError(f"unsupported JQL clause: {clause}")

    def _query(self, jql: str) -> list[dict[str, Any]]:
        where = jql.split(" ORDER BY ")[0]
        clauses = [c for c in where.split(" AND ") if c.strip()]
        found = [i for i in self.issues.values() if all(self._match(i, c) for c in clauses)]
        return sorted(found, key=lambda i: int(i["key"].rsplit("-", 1)[1]))

    # -- routes ---------------------------------------------------------------------------------

    def _routes(self) -> None:
        for prefix in ("", r"/ex/jira/" + CLOUD_ID):
            api = prefix + "/rest/api/3"
            self.route("GET", api + r"/myself", lambda r: (200, {"accountId": "bot",
                                                                 "displayName": "BOSS bot"}, {}))
            self.route("GET", api + r"/issue/([A-Z]+-\d+)", self.get_issue)
            self.route("GET", api + r"/issue/([A-Z]+-\d+)/comment", self.list_comments)
            self.route("POST", api + r"/issue/([A-Z]+-\d+)/comment", self.add_comment)
            self.route("GET", api + r"/issue/([A-Z]+-\d+)/transitions", self.list_transitions)
            self.route("POST", api + r"/issue/([A-Z]+-\d+)/transitions", self.do_transition)
            self.route("GET", api + r"/issue/([A-Z]+-\d+)/remotelink", self.list_links)
            self.route("POST", api + r"/issue/([A-Z]+-\d+)/remotelink", self.add_link)
            self.route("POST", api + r"/issue", self.create)
            self.route("POST", api + r"/search/jql", self.search)
            self.route("POST", api + r"/search/approximate-count", self.count)
            self.route("GET", api + r"/project/search", self.project_search)
        self.route("POST", r"/oauth/token", self.token)
        self.route("GET", r"/oauth/token/accessible-resources", self.resources)
        # api.atlassian.com/me: who the signed-in account is (B2 compares its email).
        self.route("GET", r"/me", self.me)

    def _missing(self, key: str):
        return 404, {"errorMessages": ["Issue does not exist or you do not have permission to "
                                       "see it."], "errors": {}}, {}

    def get_issue(self, r: Request, key: str):
        if key not in self.issues:
            return self._missing(key)
        fields = (r.q("fields") or "").split(",") if r.q("fields") else None
        return 200, self._issue_json(self.issues[key], fields), {}

    def list_comments(self, r: Request, key: str):
        if key not in self.issues:
            return self._missing(key)
        rows = list(self.comments[key])
        if r.q("orderBy") == "-created":
            rows.reverse()
        limit = int(r.q("maxResults") or 50)
        return 200, {"startAt": 0, "maxResults": limit, "total": len(rows),
                     "comments": rows[:limit]}, {}

    def add_comment(self, r: Request, key: str):
        if key not in self.issues:
            return self._missing(key)
        body = r.json().get("body")
        if not isinstance(body, dict) or body.get("type") != "doc":
            return 400, {"errorMessages": ["Comment body is not valid ADF"]}, {}
        row = {"id": str(next(self.counter)), "author": _user("BOSS bot"), "body": body,
               "created": "2026-11-13T12:00:00.000+0000"}
        self.comments[key].append(row)
        return 201, row, {}

    def list_transitions(self, r: Request, key: str):
        if key not in self.issues:
            return self._missing(key)
        current = self.issues[key]["status"]
        return 200, {"transitions": [
            {"id": str(10 + i), "name": f"Move to {name}",
             "to": {"id": STATUS_DEFS[name][0], "name": name,
                    "statusCategory": {"key": STATUS_DEFS[name][1]}}}
            for i, name in enumerate(D.STATUSES) if name != current]}, {}

    def do_transition(self, r: Request, key: str):
        if key not in self.issues:
            return self._missing(key)
        wanted = str((r.json().get("transition") or {}).get("id"))
        for i, name in enumerate(D.STATUSES):
            if str(10 + i) == wanted and name != self.issues[key]["status"]:
                self.issues[key]["status"] = name
                return 204, None, {}
        return 400, {"errorMessages": ["Transition id is not valid for this issue."]}, {}

    def list_links(self, r: Request, key: str):
        return 200, list(self.links.get(key, [])), {}

    def add_link(self, r: Request, key: str):
        body = r.json()
        rows = self.links.setdefault(key, [])
        existing = next((x for x in rows if x["globalId"] == body.get("globalId")), None)
        if existing:
            existing["object"] = body["object"]
            return 200, {"id": existing["id"], "self": "x"}, {}
        row = {"id": next(self.counter), "globalId": body.get("globalId"), "object": body["object"]}
        rows.append(row)
        return 201, {"id": row["id"], "self": "x"}, {}

    def create(self, r: Request):
        fields = r.json().get("fields") or {}
        project = (fields.get("project") or {}).get("key")
        if project != "KAN":
            return 400, {"errorMessages": [], "errors": {"project": "valid project is required"}}, {}
        number = max(int(k.rsplit("-", 1)[1]) for k in self.issues) + 1
        key = f"KAN-{number}"
        issue = self._add(key, {"title": fields.get("summary"),
                                "type": (fields.get("issuetype") or {}).get("name") or "Task",
                                "status": "To Do", "labels": fields.get("labels") or [],
                                "parent": (fields.get("parent") or {}).get("key")})
        issue["description"] = fields.get("description")
        return 201, {"id": issue["id"], "key": key, "self": f"{self.base}/rest/api/3/issue/"
                     f"{issue['id']}"}, {}

    def search(self, r: Request):
        body = r.json()
        try:
            found = self._query(str(body.get("jql") or ""))
        except ValueError as error:
            return 400, {"errorMessages": [str(error)], "warningMessages": []}, {}
        start = int(body.get("nextPageToken") or 0)
        size = min(int(body.get("maxResults") or 50), 100)
        page = found[start:start + size]
        more = start + size < len(found)
        reply = {"issues": [self._issue_json(i, body.get("fields")) for i in page],
                 "isLast": not more}
        if more:
            reply["nextPageToken"] = str(start + size)
        return 200, reply, {}

    def count(self, r: Request):
        try:
            return 200, {"count": len(self._query(str(r.json().get("jql") or "")))}, {}
        except ValueError as error:
            return 400, {"errorMessages": [str(error)]}, {}

    def project_search(self, r: Request):
        query = (r.q("query") or "").lower()
        rows = [p for p in self.projects if not query or query in p["name"].lower()
                or query == p["key"].lower()]
        start = int(r.q("startAt") or 0)
        size = min(int(r.q("maxResults") or 50), self.project_page_cap)
        page = rows[start:start + size]
        return 200, {"startAt": start, "maxResults": size, "total": len(rows),
                     "isLast": start + size >= len(rows),
                     "values": [{"id": str(100 + i), "key": p["key"], "name": p["name"],
                                 "self": f"{self.base}/rest/api/3/project/{p['key']}"}
                                for i, p in enumerate(page, start=start)]}, {}

    # -- OAuth 2.0 (3LO) ------------------------------------------------------------------------

    def approve(self, authorize_url: str) -> tuple[str, str]:
        """What the person's browser does at auth.atlassian.com: consent, get a code back."""
        query = parse_qs(urlsplit(authorize_url).query)
        assert query["audience"] == ["api.atlassian.com"]
        assert query["client_id"] == [CLIENT_ID]
        assert "offline_access" in query["scope"][0].split()
        code = f"code-{uuid.uuid4().hex[:8]}"
        self.codes[code] = query["redirect_uri"][0]
        return code, query["state"][0]

    def _mint(self, expires_in: int = 3600) -> dict[str, Any]:
        access = f"at-{uuid.uuid4().hex}"
        refresh = f"rt-{uuid.uuid4().hex}"
        self.access[access] = time.time() + expires_in
        self.refresh_tokens.add(refresh)
        return {"access_token": access, "refresh_token": refresh, "expires_in": expires_in,
                "token_type": "Bearer",
                "scope": "read:jira-work write:jira-work read:jira-user offline_access"}

    def token(self, r: Request):
        body = r.json()
        if body.get("client_id") != CLIENT_ID or body.get("client_secret") != CLIENT_SECRET:
            return 401, {"error": "access_denied", "error_description": "Unauthorized"}, {}
        if body.get("grant_type") == "authorization_code":
            if self.codes.pop(str(body.get("code")), None) != body.get("redirect_uri"):
                return 403, {"error": "invalid_grant"}, {}
            return 200, self._mint(), {}
        if body.get("grant_type") == "refresh_token":
            with self.oauth_lock:
                if self.refresh_delay:
                    time.sleep(self.refresh_delay)
                old = str(body.get("refresh_token"))
                if old not in self.refresh_tokens:
                    # Atlassian rotates: a used (or unknown) refresh token is refused.
                    return 403, {"error": "unauthorized_client",
                                 "error_description": "refresh_token is invalid"}, {}
                self.refresh_tokens.discard(old)
                self.refreshes += 1
                return 200, self._mint(), {}
        return 400, {"error": "unsupported_grant_type"}, {}

    def resources(self, r: Request):
        header = r.headers.get("authorization", "")
        if not header.startswith("Bearer ") or self.access.get(header[7:], 0) < time.time():
            return 401, {"code": 401, "message": "Unauthorized"}, {}
        return 200, [{"id": "99999999-0000-0000-0000-000000000000",
                      "url": "https://someone-else.atlassian.net", "name": "other",
                      "scopes": ["read:jira-work"], "avatarUrl": ""},
                     {"id": CLOUD_ID, "url": self.base, "name": "fake",
                      "scopes": ["read:jira-work", "write:jira-work"], "avatarUrl": ""}], {}

    def me(self, r: Request):
        header = r.headers.get("authorization", "")
        if not header.startswith("Bearer ") or self.access.get(header[7:], 0) < time.time():
            return 401, {"code": 401, "message": "Unauthorized"}, {}
        return 200, {"account_id": "ada-lovelace", "email": self.me_email, "name": "Ada Lovelace",
                     "nickname": "ada"}, {}

    def expire_all_access(self) -> None:
        for token in list(self.access):
            self.access[token] = 0
