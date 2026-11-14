"""Plane (cloud or self-hosted) behind the ticket contract.

Create/test keep the existing PlaneWorkTrackerAdapter code path. Everything else goes through the
shared HTTP helper at the connection's base URL (``api_base_url``; a self-hosted Plane such as
https://plane.local.sean.network is reached because it IS the configured base URL), with
``X-API-Key``.

Epics: Plane modules and cycles are NOT epics. An epic is an issue that is the PARENT of other
issues, carries the connection's epic label (settings.epic_label, default "epic"), or whose
issue type is named Epic. Children are issues whose ``parent`` is it.

States are read by GROUP: backlog/unstarted/triage -> todo, started -> doing,
completed/cancelled -> done; a state NAME in the connection's status mapping wins (Plane has no
review group, so review is mapped by name, default "In Review").

Webhooks: the existing HMAC ``X-Plane-Signature`` check (plane_webhooks.verify_signature).
"""

from __future__ import annotations

import json
import re
import ssl
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from ....services.work_tracker_adapters import IssueCreateResult, PlaneWorkTrackerAdapter
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
from ..text import acceptance_criteria, html_to_markdown, markdown_to_html

GROUP_CATEGORY = {"backlog": "todo", "unstarted": "todo", "triage": "todo", "started": "doing",
                  "completed": "done", "cancelled": "done"}
#: Where a category goes when no state name is mapped: the groups to try, in order.
CATEGORY_GROUPS = {"todo": ("unstarted", "backlog"), "doing": ("started",),
                   "done": ("completed",), "review": ()}
PER_PAGE = 100
MAX_SCAN = 2000
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_KEY = re.compile(r"^([A-Z][A-Z0-9_]+)-(\d+)$")


def _results(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return list(data.get("results") or [])
    return []


class PlaneTicketAdapter(PlaneWorkTrackerAdapter, TicketAdapterBase):
    # -- auth / base -------------------------------------------------------------------------

    def auth_headers(self, conn: TicketConnection, token: str) -> dict[str, str]:
        return {"X-API-Key": token}

    def api_base(self, conn: TicketConnection) -> str:
        return str(conn.get("api_base_url") or conn.get("web_base_url") or
                   self.manifest.default_base_url).rstrip("/")

    def web_base(self, conn: TicketConnection) -> str:
        return str(conn.get("web_base_url") or conn.get("api_base_url") or
                   "https://app.plane.so").rstrip("/")

    def ssl_context(self, conn: TicketConnection) -> Optional[ssl.SSLContext]:
        try:
            from ....config import get_settings
            from ....services.plane_client import plane_ssl_context

            return plane_ssl_context(conn.app_settings or get_settings())
        except Exception:  # noqa: BLE001 — the default context
            return None

    def _ws(self, conn: TicketConnection) -> str:
        slug = str(conn.get("workspace_slug") or "").strip("/")
        if not slug:
            raise TicketSystemError("plane: the connection needs a workspace_slug",
                                    provider=self.provider_key)
        return f"/api/v1/workspaces/{quote(slug)}"

    # -- lookups (cached on the connection for one piece of work) -----------------------------

    def _projects(self, conn: TicketConnection, http: Any = None) -> list[dict[str, Any]]:
        if "plane_projects" not in conn.cache:
            http = http or self.session(conn)
            conn.cache["plane_projects"] = list(self._pages(http, f"{self._ws(conn)}/projects/",
                                                            {}, limit=500))
        return conn.cache["plane_projects"]

    def _project(self, conn: TicketConnection, project: Optional[str],
                 http: Any = None) -> dict[str, Any]:
        wanted = str(project or conn.get("project_id") or "").strip()
        if not wanted:
            raise TicketSystemError("plane: which project?", provider=self.provider_key)
        for item in self._projects(conn, http):
            if wanted in (str(item.get("id")), str(item.get("identifier") or "").upper()) or \
                    wanted.upper() == str(item.get("identifier") or "").upper():
                return item
        if _UUID.match(wanted):
            return {"id": wanted, "identifier": "", "name": wanted}
        raise NotFound(f"plane: no project {wanted}", provider=self.provider_key)

    def _states(self, conn: TicketConnection, project_id: str, http: Any) -> dict[str, dict]:
        cache_key = f"plane_states:{project_id}"
        if cache_key not in conn.cache:
            rows = _results(http.get_json(f"{self._ws(conn)}/projects/{project_id}/states/"))
            conn.cache[cache_key] = {str(r.get("id")): r for r in rows if isinstance(r, dict)}
        return conn.cache[cache_key]

    def _labels(self, conn: TicketConnection, project_id: str, http: Any) -> dict[str, str]:
        cache_key = f"plane_labels:{project_id}"
        if cache_key not in conn.cache:
            try:
                rows = _results(http.get_json(f"{self._ws(conn)}/projects/{project_id}/labels/"))
            except NotFound:
                rows = []
            conn.cache[cache_key] = {str(r.get("id")): str(r.get("name")) for r in rows
                                     if isinstance(r, dict)}
        return conn.cache[cache_key]

    def _members(self, conn: TicketConnection, http: Any) -> dict[str, str]:
        """{user id: display name} of the workspace (Plane sends people as ids)."""
        if "plane_members" not in conn.cache:
            try:
                rows = _results(http.get_json(f"{self._ws(conn)}/members/"))
            except (NotFound, TicketSystemError):
                rows = []
            conn.cache["plane_members"] = {
                str(r.get("id") or (r.get("member") or {}).get("id")):
                str(r.get("display_name") or (r.get("member") or {}).get("display_name") or "")
                for r in rows if isinstance(r, dict)}
        return conn.cache["plane_members"]

    def _person(self, conn: TicketConnection, value: Any, http: Any) -> Optional[str]:
        if isinstance(value, dict):
            return value.get("display_name") or value.get("email") or value.get("id")
        if not value:
            return None
        return self._members(conn, http).get(str(value)) or str(value)

    def _pages(self, http: Any, path: str, params: dict[str, Any], *, limit: int):
        def fetch(cursor: Optional[str]) -> tuple[list[Any], Optional[str]]:
            data = http.get_json(path, params={**params, "per_page": PER_PAGE, "cursor": cursor})
            if isinstance(data, list):
                return data, None
            more = bool((data or {}).get("next_page_results"))
            return _results(data), ((data or {}).get("next_cursor") if more else None)

        return http.paginate(fetch, limit=limit)

    def _scan(self, conn: TicketConnection, project: dict[str, Any], http: Any) -> list[dict]:
        cache_key = f"plane_scan:{project['id']}"
        if cache_key not in conn.cache:
            conn.cache[cache_key] = list(self._pages(
                http, f"{self._ws(conn)}/projects/{project['id']}/issues/",
                {"expand": "state,labels,assignees"}, limit=MAX_SCAN))
        return conn.cache[cache_key]

    # -- normalising --------------------------------------------------------------------------

    def _label_names(self, conn: TicketConnection, issue: dict[str, Any], project_id: str,
                     http: Any) -> list[str]:
        names = []
        lookup: Optional[dict[str, str]] = None
        for item in issue.get("labels") or issue.get("label_details") or []:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
            elif isinstance(item, str):
                if _UUID.match(item):
                    lookup = lookup if lookup is not None else self._labels(conn, project_id, http)
                    names.append(lookup.get(item, item))
                else:
                    names.append(item)
        return names

    def _state(self, conn: TicketConnection, issue: dict[str, Any], project_id: str,
               http: Any) -> dict[str, Any]:
        state = issue.get("state") or issue.get("state_detail")
        if isinstance(state, dict):
            return state
        if state:
            return self._states(conn, project_id, http).get(str(state), {"id": state})
        return {}

    def _type(self, conn: TicketConnection, issue: dict[str, Any], labels: list[str],
              is_parent: bool) -> tuple[str, Optional[str]]:
        raw = issue.get("type") or issue.get("type_detail")
        type_name = raw.get("name") if isinstance(raw, dict) else None
        epic_label = str(self.settings(conn).get("epic_label") or "epic").lower()
        if normalise_type(type_name) == "epic" or epic_label in {lb.lower() for lb in labels} \
                or is_parent:
            return "epic", type_name or ("epic" if epic_label in {lb.lower() for lb in labels}
                                         else None)
        if type_name:
            return normalise_type(type_name), type_name
        for label in labels:
            if normalise_type(label) in ("bug", "story", "task"):
                return normalise_type(label), label
        return "task", None

    def _ticket(self, conn: TicketConnection, issue: dict[str, Any], project: dict[str, Any],
                http: Any, *, comments: Optional[list[TicketComment]] = None,
                parent_key: Optional[str] = None, children_count: Optional[int] = None,
                attachments: Optional[list[TicketAttachment]] = None) -> Ticket:
        project_id = str(project.get("id") or issue.get("project") or "")
        labels = self._label_names(conn, issue, project_id, http)
        state = self._state(conn, issue, project_id, http)
        identifier = str(project.get("identifier") or "")
        sequence = issue.get("sequence_id")
        key = f"{identifier}-{sequence}" if identifier and sequence is not None else str(issue["id"])
        if children_count is None and isinstance(issue.get("sub_issues_count"), int):
            children_count = issue["sub_issues_count"]
        kind, raw_type = self._type(conn, issue, labels, bool(children_count))
        body = html_to_markdown(str(issue.get("description_html") or "")) or \
            str(issue.get("description_stripped") or "")
        assignees = issue.get("assignees") or []
        assignee = self._person(conn, assignees[0], http) if assignees else None
        reporter = self._person(conn, issue.get("created_by_detail") or issue.get("created_by"),
                                http)
        priority = issue.get("priority")
        return Ticket(
            provider=self.provider_key, key=key, id=str(issue["id"]),
            url=f"{self.web_base(conn)}/{conn.get('workspace_slug')}/projects/{project_id}/issues/"
                f"{issue['id']}",
            title=str(issue.get("name") or ""), body_markdown=body, type=kind, raw_type=raw_type,
            status=state.get("name"),
            status_category=self.category_for(conn, state.get("name"),
                                              GROUP_CATEGORY.get(str(state.get("group")), "todo")),
            priority=None if priority in (None, "none") else str(priority),
            labels=labels, assignee=assignee, reporter=reporter,
            acceptance_criteria=acceptance_criteria(body),
            comments=list(comments or []), parent_key=parent_key,
            children_count=children_count, attachments=list(attachments or []),
            updated_at=issue.get("updated_at"))

    def _key_of(self, conn: TicketConnection, issue_id: Optional[str], project: dict[str, Any],
                http: Any, scan: Optional[list[dict]] = None) -> Optional[str]:
        if not issue_id:
            return None
        found = next((i for i in scan or [] if str(i.get("id")) == str(issue_id)), None)
        if found is None:
            try:
                found = http.get_json(f"{self._ws(conn)}/projects/{project['id']}/issues/"
                                      f"{issue_id}/")
            except NotFound:
                return None
        identifier = project.get("identifier")
        if identifier and found and found.get("sequence_id") is not None:
            return f"{identifier}-{found['sequence_id']}"
        return str(issue_id)

    # -- refs ---------------------------------------------------------------------------------

    def known_keys(self, conn: Optional[TicketConnection]) -> set[str]:
        keys = super().known_keys(conn)
        if conn is not None:
            keys.update(str(p.get("identifier") or "").upper()
                        for p in conn.cache.get("plane_projects", []) if p.get("identifier"))
        return keys

    def ref_from_url(self, url: str, conn: Optional[TicketConnection] = None) -> Optional[TicketRef]:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        allowed = {"app.plane.so", "api.plane.so"}
        if conn is not None:
            allowed |= {(urlsplit(self.web_base(conn)).hostname or "").lower(),
                        (urlsplit(self.api_base(conn)).hostname or "").lower()}
        if host not in allowed:
            return None
        match = re.search(r"/projects/([0-9a-fA-F-]{36})/(?:issues|work-items)/"
                          r"([0-9a-fA-F-]{36})", parts.path)
        if match:
            return TicketRef(self.provider_key, id=match.group(2), project=match.group(1), url=url)
        match = re.search(r"/browse/([A-Z][A-Z0-9_]+-\d+)/?", parts.path)
        if match:
            return TicketRef(self.provider_key, key=match.group(1),
                             project=match.group(1).rsplit("-", 1)[0], url=url)
        return None

    def _issue(self, conn: TicketConnection, ref: TicketRef,
               http: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """(issue, project) for a ref by uuid or by KEY-N."""
        if ref.id and _UUID.match(ref.id):
            project = self._project(conn, ref.project, http)
            issue = http.get_json(f"{self._ws(conn)}/projects/{project['id']}/issues/{ref.id}/",
                                  params={"expand": "state,labels,assignees"})
            return issue, project
        key = str(ref.key or "").upper()
        match = _KEY.match(key)
        if not match:
            raise NotFound(f"plane: '{ref.label()}' is not an issue key", provider=self.provider_key)
        issue = None
        for path in (f"{self._ws(conn)}/issues/{key}/", f"{self._ws(conn)}/work-items/{key}/"):
            try:
                issue = http.get_json(path, params={"expand": "state,labels,assignees"})
                break
            except NotFound:
                continue
        if not isinstance(issue, dict) or not issue.get("id"):
            raise NotFound(f"plane: no issue {key}", provider=self.provider_key)
        project = self._project(conn, str(issue.get("project") or match.group(1)), http)
        return issue, project

    # -- read ---------------------------------------------------------------------------------

    def fetch_ticket(self, conn: TicketConnection, ref: TicketRef) -> Ticket:
        http = self.session(conn)
        issue, project = self._issue(conn, ref, http)
        root = f"{self._ws(conn)}/projects/{project['id']}/issues/{issue['id']}"
        limit = int(self.settings(conn).get("comments_limit") or 0)
        comments: list[TicketComment] = []
        if limit:
            rows = list(self._pages(http, f"{root}/comments/", {}, limit=500))
            for row in rows[-limit:]:
                actor = row.get("actor_detail") or {}
                comments.append(TicketComment(
                    actor.get("display_name") or row.get("actor"), row.get("created_at"),
                    html_to_markdown(str(row.get("comment_html") or ""))
                    or str(row.get("comment_stripped") or "")))
        attachments = []
        try:
            for row in _results(http.get_json(f"{root}/issue-attachments/")):
                attrs = row.get("attributes") or {}
                attachments.append(TicketAttachment(str(attrs.get("name") or row.get("name") or ""),
                                                    row.get("asset_url") or row.get("asset"),
                                                    attrs.get("size") or row.get("size")))
        except NotFound:
            attachments = []
        parent_key = self._key_of(conn, issue.get("parent"), project, http)
        children = issue.get("sub_issues_count")
        if not isinstance(children, int):
            scan = self._scan(conn, project, http)
            children = sum(1 for i in scan if str(i.get("parent") or "") == str(issue["id"]))
        return self._ticket(conn, issue, project, http, comments=comments, parent_key=parent_key,
                            children_count=children, attachments=attachments)

    # -- browse -------------------------------------------------------------------------------

    def _tickets_of_scan(self, conn: TicketConnection, project: dict[str, Any], http: Any,
                         rows: list[dict]) -> list[Ticket]:
        scan = self._scan(conn, project, http)
        child_counts: dict[str, int] = {}
        for item in scan:
            if item.get("parent"):
                child_counts[str(item["parent"])] = child_counts.get(str(item["parent"]), 0) + 1
        return [self._ticket(conn, row, project, http,
                             parent_key=self._key_of(conn, row.get("parent"), project, http, scan),
                             children_count=child_counts.get(str(row["id"]), 0))
                for row in rows]

    def search_tickets(self, conn: TicketConnection, query: str, project: Optional[str],
                       filters: dict[str, Any], limit: int) -> list[Ticket]:
        http = self.session(conn)
        text = query.strip()
        if _KEY.match(text.upper()):
            try:
                issue, found_project = self._issue(conn, TicketRef(self.provider_key,
                                                                   key=text.upper()), http)
                return self._tickets_of_scan(conn, found_project, http, [issue])
            except NotFound:
                return []
        proj = self._project(conn, project, http)
        tickets = self._tickets_of_scan(conn, proj, http, self._scan(conn, proj, http))
        wanted_labels = {str(lb).lower() for lb in filters.get("labels") or []}
        out = []
        for ticket in tickets:
            if text and text.lower() not in ticket.title.lower() and \
                    text.lower() not in ticket.body_markdown.lower():
                continue
            if filters.get("status_category") and ticket.status_category != \
                    filters["status_category"]:
                continue
            if filters.get("open_only") and ticket.status_category == "done":
                continue
            if filters.get("type") and ticket.type != filters["type"]:
                continue
            if wanted_labels and not wanted_labels <= {lb.lower() for lb in ticket.labels}:
                continue
            if filters.get("assignee") and ticket.assignee != filters["assignee"]:
                continue
            out.append(ticket)
            if len(out) >= limit:
                break
        return out

    def fetch_projects(self, conn: TicketConnection, query: Optional[str],
                       counts: bool) -> list[TicketProject]:
        http = self.session(conn)
        out = []
        for item in self._projects(conn, http):
            name = str(item.get("name") or "")
            identifier = str(item.get("identifier") or "")
            if query and query.lower() not in name.lower() and query.lower() != identifier.lower():
                continue
            open_count = epic_count = None
            if counts:
                tickets = self._tickets_of_scan(conn, item, http, self._scan(conn, item, http))
                open_count = sum(1 for t in tickets if t.status_category != "done")
                epic_count = sum(1 for t in tickets if t.type == "epic"
                                 and t.status_category != "done")
            out.append(TicketProject(
                key=identifier or str(item.get("id")), name=name, id=str(item.get("id")),
                url=f"{self.web_base(conn)}/{conn.get('workspace_slug')}/projects/"
                    f"{item.get('id')}/issues/", open_count=open_count, epic_count=epic_count))
        return out

    def fetch_epics(self, conn: TicketConnection, project: str) -> list[Ticket]:
        http = self.session(conn)
        proj = self._project(conn, project, http)
        tickets = self._tickets_of_scan(conn, proj, http, self._scan(conn, proj, http))
        return [t for t in tickets if t.type == "epic" and t.status_category != "done"]

    def fetch_children(self, conn: TicketConnection, epic_ref: TicketRef) -> list[Ticket]:
        http = self.session(conn)
        epic, proj = self._issue(conn, epic_ref, http)
        rows = [i for i in self._scan(conn, proj, http) if str(i.get("parent") or "") ==
                str(epic["id"])]
        return self._tickets_of_scan(conn, proj, http, rows)

    def fetch_unparented(self, conn: TicketConnection, project: str) -> list[Ticket]:
        http = self.session(conn)
        proj = self._project(conn, project, http)
        rows = [i for i in self._scan(conn, proj, http) if not i.get("parent")]
        return [t for t in self._tickets_of_scan(conn, proj, http, rows)
                if t.type != "epic" and t.status_category != "done"]

    # -- write --------------------------------------------------------------------------------

    def post_comment(self, conn: TicketConnection, ref: TicketRef, markdown: str) -> TicketComment:
        http = self.session(conn)
        issue, project = self._issue(conn, ref, http)
        row = http.post_json(f"{self._ws(conn)}/projects/{project['id']}/issues/{issue['id']}"
                             "/comments/", {"comment_html": markdown_to_html(markdown)}) or {}
        actor = row.get("actor_detail") or {}
        return TicketComment(actor.get("display_name") or row.get("actor"),
                             row.get("created_at"), markdown)

    def apply_transition(self, conn: TicketConnection, ref: TicketRef,
                         category: str) -> TransitionResult:
        http = self.session(conn)
        issue, project = self._issue(conn, ref, http)
        states = self._states(conn, str(project["id"]), http)
        current = self._state(conn, issue, str(project["id"]), http)
        mapped = self.settings(conn)["status_mapping"].get(category)
        done_ref = TicketRef(self.provider_key, id=str(issue["id"]), project=str(project["id"]))
        target = None
        if mapped:
            target = next((s for s in states.values()
                           if str(s.get("name") or "").lower() == mapped.lower()), None)
        if target is None and category == "review":
            target = next((s for s in states.values() if s.get("group") == "started"
                           and "review" in str(s.get("name") or "").lower()), None)
        if target is None:
            for group in CATEGORY_GROUPS[category]:
                in_group = [s for s in states.values() if s.get("group") == group]
                target = next((s for s in in_group if s.get("default")), None) or \
                    (in_group[0] if in_group else None)
                if target is not None:
                    break
        if target is None:
            names = ", ".join(sorted(str(s.get("name")) for s in states.values())) or "none"
            raise TicketSystemError(f"plane: no state for '{category}' (states: {names}). Set the "
                                    "connection's status mapping.", provider=self.provider_key)
        if str(current.get("id")) == str(target.get("id")):
            return TransitionResult(done_ref, current.get("name"), current.get("name"),
                                    category, False)
        http.request("PATCH", f"{self._ws(conn)}/projects/{project['id']}/issues/{issue['id']}/",
                     json_body={"state": str(target["id"])})
        conn.cache.pop(f"plane_scan:{project['id']}", None)
        return TransitionResult(done_ref, current.get("name"), target.get("name"), category, True)

    def add_link(self, conn: TicketConnection, ref: TicketRef, url: str, title: str) -> LinkResult:
        http = self.session(conn)
        issue, project = self._issue(conn, ref, http)
        root = f"{self._ws(conn)}/projects/{project['id']}/issues/{issue['id']}/links/"
        done_ref = TicketRef(self.provider_key, id=str(issue["id"]), project=str(project["id"]))
        if any(str(row.get("url")) == url for row in _results(http.get_json(root))):
            return LinkResult(done_ref, url, False)
        http.post_json(root, {"url": url, "title": title})
        return LinkResult(done_ref, url, True)

    def create_ticket(self, conn: TicketConnection, *, title: str, body_markdown: str,
                      type: str, labels: list[str], project: Optional[str],
                      parent: Optional[TicketRef]) -> IssueCreateResult:
        if not labels and parent is None and project is None:
            return super().create_ticket(conn, title=title, body_markdown=body_markdown,
                                         type=type, labels=labels, project=project, parent=parent)
        http = self.session(conn)
        proj = self._project(conn, project, http)
        body: dict[str, Any] = {"name": title, "description_html": markdown_to_html(body_markdown)}
        if labels:
            by_name = {v.lower(): k for k, v in self._labels(conn, str(proj["id"]), http).items()}
            body["labels"] = [by_name[lb.lower()] for lb in labels if lb.lower() in by_name]
        if parent is not None:
            parent_issue, _ = self._issue(conn, parent, http)
            body["parent"] = str(parent_issue["id"])
        issue = http.post_json(f"{self._ws(conn)}/projects/{proj['id']}/issues/", body) or {}
        ident = proj.get("identifier")
        key = f"{ident}-{issue.get('sequence_id')}" if ident and issue.get("sequence_id") else None
        return IssueCreateResult(
            external_issue_id=str(issue.get("id")), external_key=key,
            external_url=f"{self.web_base(conn)}/{conn.get('workspace_slug')}/projects/"
                         f"{proj['id']}/issues/{issue.get('id')}", external_state="Backlog",
            raw=issue)

    # -- webhooks -----------------------------------------------------------------------------

    def check_webhook(self, headers: dict[str, str], body: bytes, secret: str) -> bool:
        from ....services.plane_webhooks import PlaneWebhookService

        holder = SimpleNamespace(settings=SimpleNamespace(plane_webhook_secret=secret))
        return PlaneWebhookService.verify_signature(holder, body,  # type: ignore[arg-type]
                                                    headers.get("x-plane-signature", ""))

    def read_webhook(self, body: Any) -> TicketEvent:
        data = json.loads(body) if isinstance(body, (bytes, str)) else dict(body or {})
        event = str(data.get("event") or "")
        action = str(data.get("action") or "")
        payload = data.get("data") or {}
        if event in ("issue_comment", "comment"):
            kind = "commented"
            issue_id = payload.get("issue")
        else:
            kind = action if action in ("created", "updated", "deleted") else "other"
            issue_id = payload.get("id") if event in ("issue", "work_item") else None
        ref = TicketRef(self.provider_key, id=str(issue_id), project=str(payload.get("project")
                                                                         or "") or None) \
            if issue_id else None
        activity = data.get("activity") or {}
        actor = (activity.get("actor") or {}).get("display_name") if isinstance(
            activity.get("actor"), dict) else None
        changed = (str(activity["field"]),) if activity.get("field") else ()
        return TicketEvent(self.provider_key, kind, ref, actor, changed, f"{event}.{action}")
