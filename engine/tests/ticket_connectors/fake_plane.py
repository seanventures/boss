"""A fake Plane (/api/v1) with the real response shapes: cursor pagination
({results, next_cursor, next_page_results, ...}), states with groups, labels by uuid, expand=
state,labels, issues by uuid and by IDENT-N, description_html, comment_html, links,
issue-attachments, X-API-Key auth, and HMAC X-Plane-Signature webhooks."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import uuid
from typing import Any

from . import dataset as D
from .fake_http import FakeServer, Request

TOKEN = "plane-api-key-fake-0123456789"
SLUG = "acme"
WEBHOOK_SECRET = "plane-webhook-secret"
GROUPS = {"Backlog": "backlog", "To Do": "unstarted", "In Progress": "started",
          "In Review": "started", "Done": "completed", "Cancelled": "cancelled"}


def _uuid() -> str:
    return str(uuid.uuid4())


def html(markdown: str) -> str:
    out = []
    for block in markdown.split("\n\n"):
        lines = block.split("\n")
        if block.startswith("## "):
            out.append(f"<h2>{block[3:]}</h2>")
        elif all(line.startswith("- ") for line in lines):
            out.append("<ul>" + "".join(f"<li><p>{line[2:]}</p></li>" for line in lines) + "</ul>")
        else:
            out.append(f"<p>{block}</p>")
    return "".join(out)


class FakePlane(FakeServer):
    def __init__(self) -> None:
        super().__init__()
        self.projects = []
        for spec in [D.PROJECT] + D.OTHER_PROJECTS:
            self.projects.append({"id": _uuid(), "identifier": spec["key"], "name": spec["name"],
                                  "workspace": SLUG})
        self.kan = self.projects[0]["id"]
        self.states = {name: {"id": _uuid(), "name": name, "group": group,
                              "default": name == "To Do", "project": self.kan}
                       for name, group in GROUPS.items()}
        self.labels = {name: {"id": _uuid(), "name": name, "project": self.kan}
                       for name in ("boss", "payments", "bug", "epic", "story")}
        self.members = {}
        self.issues: dict[str, dict[str, Any]] = {}
        self.by_seq: dict[int, str] = {}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.links: dict[str, list[dict[str, Any]]] = {}
        self.attachments: dict[str, list[dict[str, Any]]] = {}
        for n, item in D.ISSUES.items():
            self._add(n, copy.deepcopy(item))
        for n, item in D.ISSUES.items():
            if isinstance(item.get("parent"), int):
                self.issues[self.by_seq[n]]["parent"] = self.by_seq[item["parent"]]
        self._routes()

    def _person(self, name: Any) -> Any:
        if not name:
            return None
        if name not in self.members:
            self.members[name] = {"id": _uuid(), "display_name": name, "first_name": name}
        return self.members[name]

    def _add(self, n: int, item: dict[str, Any]) -> dict[str, Any]:
        issue_id = _uuid()
        labels = list(item.get("labels") or [])
        if item["type"] == "Epic":
            labels.append("epic")
        if item["type"] in ("Bug", "Story"):
            labels.append(item["type"].lower())
        issue = {
            "id": issue_id, "sequence_id": n, "name": item["title"], "project": self.kan,
            "workspace": SLUG,
            "description_html": html(item.get("body") or ""),
            "description_stripped": item.get("body") or "",
            "state": self.states[item["status"]]["id"],
            "priority": str(item.get("priority") or "none").lower(),
            "labels": [self.labels[lb]["id"] for lb in labels],
            "assignees": [self._person(item["assignee"])["id"]] if item.get("assignee") else [],
            "created_by": self._person(item.get("reporter") or "Grace Hopper")["id"],
            "parent": None, "updated_at": f"2026-11-{1 + n % 12:02d}T10:00:00Z",
            "type_id": None,
        }
        self.issues[issue_id] = issue
        self.by_seq[n] = issue_id
        self.comments[issue_id] = [
            {"id": _uuid(), "comment_html": f"<p>{text}</p>", "comment_stripped": text,
             "actor": self._person(author)["id"],
             "actor_detail": {"id": self._person(author)["id"], "display_name": author},
             "created_at": f"2026-11-0{i + 1}T09:00:00Z", "issue": issue_id}
            for i, (author, text) in enumerate(item.get("comments") or [])]
        self.links[issue_id] = []
        self.attachments[issue_id] = [
            {"id": _uuid(), "attributes": {"name": name, "size": size, "type": "image/png"},
             "asset_url": f"{self.base}/assets/{name}"} for name, size in item.get("attachments")
            or []]
        return issue

    def ticket_url(self, n: int) -> str:
        return f"{self.base}/{SLUG}/projects/{self.kan}/issues/{self.by_seq[n]}"

    def authorized(self, request: Request) -> bool:
        return request.headers.get("x-api-key") == TOKEN

    # -- shapes ---------------------------------------------------------------------------------

    def _issue_json(self, issue: dict[str, Any], expand: str) -> dict[str, Any]:
        out = dict(issue)
        fields = set(expand.split(",")) if expand else set()
        state = next(s for s in self.states.values() if s["id"] == issue["state"])
        if "state" in fields:
            out["state"] = {"id": state["id"], "name": state["name"], "group": state["group"],
                            "color": "#000"}
        if "labels" in fields:
            out["labels"] = [{"id": lb["id"], "name": lb["name"], "color": "#fff"}
                             for lb in self.labels.values() if lb["id"] in issue["labels"]]
        if "assignees" in fields:
            out["assignees"] = [m for m in self.members.values() if m["id"] in issue["assignees"]]
        return out

    def _page(self, r: Request, rows: list[Any]):
        per_page = min(int(r.q("per_page") or 100), 100)
        cursor = r.q("cursor") or f"{per_page}:0:0"
        page = int(cursor.split(":")[1])
        chunk = rows[page * per_page:(page + 1) * per_page]
        more = (page + 1) * per_page < len(rows)
        return 200, {"grouped_by": None, "sub_grouped_by": None, "total_count": len(rows),
                     "next_cursor": f"{per_page}:{page + 1}:0",
                     "prev_cursor": f"{per_page}:{max(page - 1, 0)}:1",
                     "next_page_results": more, "prev_page_results": page > 0,
                     "count": len(chunk), "total_pages": -(-len(rows) // per_page),
                     "total_results": len(rows), "extra_stats": None, "results": chunk}, {}

    def _project_ok(self, pid: str) -> bool:
        return any(p["id"] == pid for p in self.projects)

    # -- routes ---------------------------------------------------------------------------------

    def _routes(self) -> None:
        ws = rf"/api/v1/workspaces/{SLUG}"
        proj = ws + r"/projects/([0-9a-f-]{36})"
        issue = proj + r"/issues/([0-9a-f-]{36})"
        self.route("GET", ws + r"/projects/", lambda r: self._page(r, self.projects))
        self.route("GET", ws + r"/members/", lambda r: (200, list(self.members.values()), {}))
        self.route("GET", proj + r"/", self.get_project)
        self.route("GET", proj + r"/states/", lambda r, p: self._page(r, [
            s for s in self.states.values() if s["project"] == p]))
        self.route("GET", proj + r"/labels/", lambda r, p: self._page(r, [
            lb for lb in self.labels.values() if lb["project"] == p]))
        self.route("GET", proj + r"/issues/", self.list_issues)
        self.route("POST", proj + r"/issues/", self.create)
        self.route("GET", issue + r"/", self.get_issue)
        self.route("PATCH", issue + r"/", self.patch_issue)
        self.route("GET", issue + r"/comments/", lambda r, p, i: self._page(
            r, self.comments.get(i, [])) if i in self.issues else (404, {"error": "Not found"}, {}))
        self.route("POST", issue + r"/comments/", self.add_comment)
        self.route("GET", issue + r"/links/", lambda r, p, i: (200, list(self.links.get(i, [])),
                                                               {}))
        self.route("POST", issue + r"/links/", self.add_link)
        self.route("GET", issue + r"/issue-attachments/", lambda r, p, i: (
            200, list(self.attachments.get(i, [])), {}))
        self.route("GET", ws + r"/issues/([A-Z]+)-(\d+)/", self.by_identifier)

    def get_project(self, r: Request, pid: str):
        found = next((p for p in self.projects if p["id"] == pid), None)
        return (200, found, {}) if found else (404, {"error": "Project not found"}, {})

    def list_issues(self, r: Request, pid: str):
        if not self._project_ok(pid):
            return 404, {"error": "Project not found"}, {}
        rows = sorted((i for i in self.issues.values() if i["project"] == pid),
                      key=lambda i: i["sequence_id"])
        return self._page(r, [self._issue_json(i, r.q("expand") or "") for i in rows])

    def get_issue(self, r: Request, pid: str, iid: str):
        if iid not in self.issues:
            return 404, {"error": "Issue not found"}, {}
        return 200, self._issue_json(self.issues[iid], r.q("expand") or ""), {}

    def by_identifier(self, r: Request, ident: str, seq: str):
        project = next((p for p in self.projects if p["identifier"] == ident), None)
        iid = self.by_seq.get(int(seq)) if project and project["id"] == self.kan else None
        if not iid:
            return 404, {"error": "Issue not found"}, {}
        return 200, self._issue_json(self.issues[iid], r.q("expand") or ""), {}

    def patch_issue(self, r: Request, pid: str, iid: str):
        if iid not in self.issues:
            return 404, {"error": "Issue not found"}, {}
        body = r.json()
        if "state" in body and not any(s["id"] == body["state"] for s in self.states.values()):
            return 400, {"state": ["Invalid pk"]}, {}
        self.issues[iid].update({k: v for k, v in body.items() if k in ("state", "name")})
        return 200, self._issue_json(self.issues[iid], ""), {}

    def add_comment(self, r: Request, pid: str, iid: str):
        if iid not in self.issues:
            return 404, {"error": "Issue not found"}, {}
        row = {"id": _uuid(), "comment_html": r.json().get("comment_html"),
               "actor": "bot", "actor_detail": {"display_name": "BOSS bot"},
               "created_at": "2026-11-13T12:00:00Z", "issue": iid}
        self.comments[iid].append(row)
        return 201, row, {}

    def add_link(self, r: Request, pid: str, iid: str):
        body = r.json()
        if any(lb["url"] == body.get("url") for lb in self.links[iid]):
            return 400, {"error": "URL already exists for this Issue"}, {}
        row = {"id": _uuid(), "url": body.get("url"), "title": body.get("title"), "issue": iid}
        self.links[iid].append(row)
        return 201, row, {}

    def create(self, r: Request, pid: str):
        body = r.json()
        n = max(i["sequence_id"] for i in self.issues.values()) + 1
        issue = self._add(n, {"title": body.get("name"), "type": "Task", "status": "To Do",
                              "body": ""})
        issue["description_html"] = body.get("description_html") or ""
        issue["labels"] = list(body.get("labels") or [])
        issue["parent"] = body.get("parent")
        return 201, self._issue_json(issue, ""), {}

    # -- webhooks -------------------------------------------------------------------------------

    def webhook(self, n: int, action: str = "updated") -> tuple[dict[str, str], bytes]:
        body = json.dumps({"event": "issue", "action": action, "webhook_id": _uuid(),
                           "workspace_id": _uuid(),
                           "data": {"id": self.by_seq[n], "project": self.kan,
                                    "name": self.issues[self.by_seq[n]]["name"]},
                           "activity": {"actor": {"display_name": "Ada Lovelace"},
                                        "field": "state"}}).encode()
        sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return {"X-Plane-Signature": sig, "X-Plane-Event": "issue"}, body
