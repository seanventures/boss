"""Jira Cloud (REST v3) behind the ticket contract.

Create/test keep the existing JiraWorkTrackerAdapter code path. Everything else goes through the
shared HTTP helper: JQL search (/rest/api/3/search/jql, token pagination), projects
(/rest/api/3/project/search, offset pagination), epics by issuetype = Epic, children by
``parent = KEY``, transitions (category from statusCategory.key), remote links for PR links, and
ADF bodies/comments rendered to markdown.

Webhooks: Jira admin webhooks registered WITH a secret send ``X-Hub-Signature: sha256=<hex
HMAC-SHA256 of the raw body>``; that is checked first. A webhook without one is registered with
``?token=<secret>`` on its URL, and the route passes that value as ``X-Boss-Webhook-Token``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlsplit

from ....services.work_tracker_adapters import IssueCreateResult, JiraWorkTrackerAdapter
from ..base import TicketAdapterBase, normalise_type
from ..contract import (
    LinkResult,
    NotFound,
    Ticket,
    TicketAttachment,
    TicketComment,
    TicketConnection,
    TicketEvent,
    TicketProject,
    TicketRef,
    TicketSystemError,
    TransitionResult,
)
from ..text import acceptance_criteria, adf_to_markdown, markdown_to_adf

#: Jira statusCategory.key -> BOSS category.
CATEGORY_OF = {"new": "todo", "undefined": "todo", "indeterminate": "doing", "done": "done"}
#: BOSS category -> the statusCategory.key a transition must reach (review has none in Jira).
TARGET_KEY = {"todo": "new", "doing": "indeterminate", "done": "done"}
JQL_CATEGORY = {"todo": '"To Do"', "doing": '"In Progress"', "done": "Done"}
BASE_FIELDS = ["summary", "description", "issuetype", "status", "priority", "labels", "assignee",
               "reporter", "parent", "attachment", "updated", "subtasks", "project"]
PAGE = 50
_KEY_ONLY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def _quote_jql(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


class JiraTicketAdapter(JiraWorkTrackerAdapter, TicketAdapterBase):
    # -- auth / base -------------------------------------------------------------------------

    def auth_headers(self, conn: TicketConnection, token: str) -> dict[str, str]:
        email = str(conn.get("email") or "").strip()
        return {"Authorization": "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()}

    def site(self, conn: TicketConnection) -> str:
        return str(conn.get("base_url") or "").rstrip("/")

    # -- refs ---------------------------------------------------------------------------------

    def ref_from_url(self, url: str, conn: Optional[TicketConnection] = None) -> Optional[TicketRef]:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        site_host = (urlsplit(self.site(conn)).hostname or "").lower() if conn else ""
        if not (host == site_host or (host.endswith(".atlassian.net") and not site_host)):
            return None
        match = re.search(r"/browse/([A-Z][A-Z0-9_]+-\d+)", parts.path)
        key = match.group(1) if match else None
        if key is None:
            selected = parse_qs(parts.query).get("selectedIssue", [""])[0]
            key = selected if _KEY_ONLY.match(selected or "") else None
        if key is None:
            match = re.search(r"/issues/([A-Z][A-Z0-9_]+-\d+)", parts.path)
            key = match.group(1) if match else None
        if key is None:
            return None
        return TicketRef(self.provider_key, key=key, url=f"{parts.scheme}://{parts.netloc}"
                         f"/browse/{key}", project=key.rsplit("-", 1)[0])

    def _key(self, ref: TicketRef) -> str:
        key = ref.key or ref.id
        if not key and ref.url:
            found = self.ref_from_url(ref.url)
            key = found.key if found else None
        if not key:
            raise NotFound(f"{self.provider_key}: no issue key in that reference",
                           provider=self.provider_key)
        return str(key)

    # -- normalising --------------------------------------------------------------------------

    def _ticket(self, conn: TicketConnection, issue: dict[str, Any], *,
                comments: Optional[list[TicketComment]] = None,
                children_count: Optional[int] = None) -> Ticket:
        fields = issue.get("fields") or {}
        settings = self.settings(conn)
        status = fields.get("status") or {}
        category_key = ((status.get("statusCategory") or {}).get("key") or "new")
        issuetype = (fields.get("issuetype") or {}).get("name")
        body = adf_to_markdown(fields.get("description"))
        ac_field = settings.get("acceptance_criteria_field") or ""
        key = str(issue.get("key") or "")
        subtasks = fields.get("subtasks")
        return Ticket(
            provider=self.provider_key, key=key, id=str(issue.get("id") or key),
            url=f"{self.site(conn)}/browse/{key}" if self.site(conn) else None,
            title=str(fields.get("summary") or ""), body_markdown=body,
            type=normalise_type(issuetype), raw_type=issuetype,
            status=status.get("name"),
            status_category=self.category_for(conn, status.get("name"),
                                              CATEGORY_OF.get(category_key, "todo")),
            priority=(fields.get("priority") or {}).get("name"),
            labels=[str(label) for label in fields.get("labels") or []],
            assignee=(fields.get("assignee") or {}).get("displayName"),
            reporter=(fields.get("reporter") or {}).get("displayName"),
            acceptance_criteria=acceptance_criteria(body, fields.get(ac_field) if ac_field
                                                    else None),
            comments=list(comments or []),
            parent_key=(fields.get("parent") or {}).get("key"),
            children_count=children_count if children_count is not None else (
                len(subtasks) if isinstance(subtasks, list) else None),
            attachments=[TicketAttachment(str(a.get("filename") or ""), a.get("content"),
                                          a.get("size")) for a in fields.get("attachment") or []],
            updated_at=fields.get("updated"))

    def _fields(self, conn: TicketConnection) -> list[str]:
        extra = self.settings(conn).get("acceptance_criteria_field")
        return BASE_FIELDS + ([extra] if extra else [])

    # -- read ---------------------------------------------------------------------------------

    def fetch_ticket(self, conn: TicketConnection, ref: TicketRef) -> Ticket:
        http = self.session(conn)
        key = self._key(ref)
        issue = http.get_json(f"/rest/api/3/issue/{quote(key)}",
                              params={"fields": ",".join(self._fields(conn))})
        limit = int(self.settings(conn).get("comments_limit") or 0)
        comments: list[TicketComment] = []
        if limit:
            page = http.get_json(f"/rest/api/3/issue/{quote(key)}/comment",
                                 params={"orderBy": "-created", "maxResults": limit})
            for item in reversed((page or {}).get("comments") or []):
                comments.append(TicketComment((item.get("author") or {}).get("displayName"),
                                              item.get("created"), adf_to_markdown(item.get("body"))))
        children = None
        if normalise_type(((issue.get("fields") or {}).get("issuetype") or {}).get("name")) == "epic":
            children = self._count(http, f"parent = {key}")
        return self._ticket(conn, issue, comments=comments, children_count=children)

    # -- browse -------------------------------------------------------------------------------

    def _count(self, http: Any, jql: str) -> Optional[int]:
        try:
            data = http.post_json("/rest/api/3/search/approximate-count", {"jql": jql})
        except NotFound:
            return None
        return int((data or {}).get("count") or 0)

    def _jql(self, conn: TicketConnection, jql: str, limit: int) -> list[Ticket]:
        http = self.session(conn)
        fields = self._fields(conn)

        def fetch(cursor: Optional[str]) -> tuple[list[Any], Optional[str]]:
            body: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": min(PAGE, limit)}
            if cursor:
                body["nextPageToken"] = cursor
            data = http.post_json("/rest/api/3/search/jql", body) or {}
            token = None if data.get("isLast", True) else data.get("nextPageToken")
            return list(data.get("issues") or []), token

        return [self._ticket(conn, issue) for issue in http.paginate(fetch, limit=limit)]

    def search_tickets(self, conn: TicketConnection, query: str, project: Optional[str],
                       filters: dict[str, Any], limit: int) -> list[Ticket]:
        clauses = []
        project = project or conn.get("project_key") or None
        if project:
            clauses.append(f"project = {_quote_jql(project)}")
        text = query.strip()
        if text:
            if _KEY_ONLY.match(text):
                clauses.append(f"key = {_quote_jql(text)}")
            else:
                clauses.append(f"text ~ {_quote_jql(text)}")
        category = filters.get("status_category")
        if category == "review":
            mapped = self.settings(conn)["status_mapping"].get("review")
            if mapped:
                clauses.append(f"status = {_quote_jql(mapped)}")
        elif category in JQL_CATEGORY:
            clauses.append(f"statusCategory = {JQL_CATEGORY[category]}")
        if filters.get("open_only"):
            clauses.append("statusCategory != Done")
        kind = filters.get("type")
        if kind:
            clauses.append(f"issuetype = {_quote_jql(str(kind).capitalize())}")
        for label in filters.get("labels") or []:
            clauses.append(f"labels = {_quote_jql(label)}")
        if filters.get("assignee"):
            clauses.append(f"assignee = {_quote_jql(filters['assignee'])}")
        if filters.get("jql"):
            clauses.append(f"({filters['jql']})")
        jql = " AND ".join(clauses) or "statusCategory != Done"
        return self._jql(conn, jql + " ORDER BY updated DESC", limit)

    def fetch_projects(self, conn: TicketConnection, query: Optional[str],
                       counts: bool) -> list[TicketProject]:
        http = self.session(conn)

        def fetch(cursor: Optional[int]) -> tuple[list[Any], Optional[int]]:
            start = int(cursor or 0)
            data = http.get_json("/rest/api/3/project/search",
                                 params={"query": query or None, "startAt": start,
                                         "maxResults": PAGE}) or {}
            values = list(data.get("values") or [])
            more = not data.get("isLast", True) and values
            return values, (start + len(values)) if more else None

        out = []
        for item in http.paginate(fetch, limit=500):
            key = str(item.get("key") or "")
            open_count = epic_count = None
            if counts:
                open_count = self._count(http, f"project = {_quote_jql(key)} AND "
                                               "statusCategory != Done")
                epic_count = self._count(http, f"project = {_quote_jql(key)} AND issuetype = "
                                               "Epic AND statusCategory != Done")
            out.append(TicketProject(key=key, name=str(item.get("name") or key),
                                     id=str(item.get("id") or key),
                                     url=f"{self.site(conn)}/browse/{key}" if self.site(conn)
                                     else None, open_count=open_count, epic_count=epic_count))
        return out

    def fetch_epics(self, conn: TicketConnection, project: str) -> list[Ticket]:
        epics = self._jql(conn, f"project = {_quote_jql(project)} AND issuetype = Epic AND "
                                "statusCategory != Done ORDER BY updated DESC", 100)
        http = self.session(conn)
        for epic in epics:
            epic.children_count = self._count(http, f"parent = {epic.key}")
        return epics

    def fetch_children(self, conn: TicketConnection, epic_ref: TicketRef) -> list[Ticket]:
        return self._jql(conn, f"parent = {self._key(epic_ref)} ORDER BY created ASC", 200)

    def fetch_unparented(self, conn: TicketConnection, project: str) -> list[Ticket]:
        return self._jql(conn, f"project = {_quote_jql(project)} AND parent is EMPTY AND "
                               "issuetype != Epic AND issuetype not in subTaskIssueTypes() AND "
                               "statusCategory != Done ORDER BY updated DESC", 200)

    # -- write --------------------------------------------------------------------------------

    def post_comment(self, conn: TicketConnection, ref: TicketRef, markdown: str) -> TicketComment:
        data = self.session(conn).post_json(f"/rest/api/3/issue/{quote(self._key(ref))}/comment",
                                            {"body": markdown_to_adf(markdown)}) or {}
        return TicketComment((data.get("author") or {}).get("displayName"), data.get("created"),
                             adf_to_markdown(data.get("body")) or markdown)

    def apply_transition(self, conn: TicketConnection, ref: TicketRef,
                         category: str) -> TransitionResult:
        http = self.session(conn)
        key = self._key(ref)
        issue = http.get_json(f"/rest/api/3/issue/{quote(key)}", params={"fields": "status"})
        status = ((issue or {}).get("fields") or {}).get("status") or {}
        current_name = status.get("name")
        current_key = (status.get("statusCategory") or {}).get("key")
        mapped = self.settings(conn)["status_mapping"].get(category)
        done_ref = TicketRef(self.provider_key, key=key)
        if (mapped and str(current_name or "").lower() == mapped.lower()) or \
                (not mapped and category in TARGET_KEY and current_key == TARGET_KEY[category]):
            return TransitionResult(done_ref, current_name, current_name, category, False)
        options = (http.get_json(f"/rest/api/3/issue/{quote(key)}/transitions") or {}) \
            .get("transitions") or []

        def to(option: dict[str, Any]) -> dict[str, Any]:
            return option.get("to") or {}

        chosen = None
        if mapped:
            chosen = next((o for o in options if str(to(o).get("name") or "").lower()
                           == mapped.lower()), None) or \
                next((o for o in options if str(o.get("name") or "").lower() == mapped.lower()),
                     None)
        if chosen is None and category == "review":
            chosen = next((o for o in options if "review" in str(to(o).get("name") or "").lower()),
                          None)
        if chosen is None and category in TARGET_KEY:
            matching = [o for o in options
                        if (to(o).get("statusCategory") or {}).get("key") == TARGET_KEY[category]]
            preferred = {"todo": "to do", "doing": "in progress", "done": "done"}[category]
            chosen = next((o for o in matching if str(to(o).get("name")).lower() == preferred),
                          matching[0] if matching else None)
        if chosen is None:
            names = ", ".join(sorted({str(to(o).get("name")) for o in options})) or "none"
            raise TicketSystemError(
                f"jira: {key} has no transition to {mapped or category} from {current_name}. "
                f"Reachable: {names}. Set the connection's status mapping for '{category}'.",
                provider=self.provider_key)
        http.request("POST", f"/rest/api/3/issue/{quote(key)}/transitions",
                     json_body={"transition": {"id": str(chosen.get("id"))}})
        return TransitionResult(done_ref, current_name, to(chosen).get("name"), category, True)

    def add_link(self, conn: TicketConnection, ref: TicketRef, url: str, title: str) -> LinkResult:
        http = self.session(conn)
        key = self._key(ref)
        existing = http.get_json(f"/rest/api/3/issue/{quote(key)}/remotelink") or []
        if any(((item.get("object") or {}).get("url") == url) for item in existing
               if isinstance(item, dict)):
            return LinkResult(TicketRef(self.provider_key, key=key), url, False)
        http.post_json(f"/rest/api/3/issue/{quote(key)}/remotelink",
                       {"globalId": "boss:" + hashlib.sha256(url.encode()).hexdigest()[:32],
                        "object": {"url": url, "title": title}})
        return LinkResult(TicketRef(self.provider_key, key=key), url, True)

    def create_ticket(self, conn: TicketConnection, *, title: str, body_markdown: str,
                      type: str, labels: list[str], project: Optional[str],
                      parent: Optional[TicketRef]) -> IssueCreateResult:
        project_key = str(project or conn.get("project_key") or "").strip()
        if not project_key:
            raise TicketSystemError("jira: which project? set project_key",
                                    provider=self.provider_key)
        type_name = {"bug": "Bug", "story": "Story", "epic": "Epic"}.get(
            type, str(conn.get("issue_type") or "Task"))
        fields: dict[str, Any] = {"project": {"key": project_key}, "summary": title,
                                  "issuetype": {"name": type_name},
                                  "description": markdown_to_adf(body_markdown or title)}
        if labels:
            fields["labels"] = [str(label).replace(" ", "-") for label in labels]
        if parent is not None:
            fields["parent"] = {"key": self._key(parent)}
        data = self.session(conn).post_json("/rest/api/3/issue", {"fields": fields}) or {}
        key = str(data.get("key") or "")
        return IssueCreateResult(external_issue_id=str(data.get("id") or key),
                                 external_key=key or None,
                                 external_url=f"{self.site(conn)}/browse/{key}" if key else None,
                                 external_state="To Do", raw=data)

    # -- webhooks -----------------------------------------------------------------------------

    def check_webhook(self, headers: dict[str, str], body: bytes, secret: str) -> bool:
        signature = headers.get("x-hub-signature", "")
        if signature:
            algorithm, _, digest = signature.partition("=")
            if algorithm.lower() != "sha256" or not digest:
                return False
            expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, digest.strip().lower())
        token = headers.get("x-boss-webhook-token", "")
        return bool(token) and hmac.compare_digest(token.encode(), secret.encode())

    def read_webhook(self, body: Any) -> TicketEvent:
        data = json.loads(body) if isinstance(body, (bytes, str)) else dict(body or {})
        event = str(data.get("webhookEvent") or "")
        kind = {"jira:issue_created": "created", "jira:issue_updated": "updated",
                "jira:issue_deleted": "deleted", "comment_created": "commented",
                "comment_updated": "commented"}.get(event, "other")
        issue = data.get("issue") or {}
        ref = None
        if issue.get("key"):
            self_url = str(issue.get("self") or "")
            site = self_url.split("/rest/", 1)[0] if "/rest/" in self_url else ""
            ref = TicketRef(self.provider_key, key=str(issue["key"]), id=str(issue.get("id") or ""),
                            url=f"{site}/browse/{issue['key']}" if site else None)
        changed = tuple(str(item.get("field")) for item in
                        ((data.get("changelog") or {}).get("items") or []) if item.get("field"))
        actor = (data.get("user") or (data.get("comment") or {}).get("author") or {}) \
            .get("displayName")
        return TicketEvent(self.provider_key, kind, ref, actor, changed, event)
