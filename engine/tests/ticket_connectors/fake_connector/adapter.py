"""The scripted in-memory adapter: the dataset served from a dict, no HTTP."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from boss_engine.connectors.tickets.base import TicketAdapterBase, normalise_type
from boss_engine.connectors.tickets.contract import (
    AuthFailed, LinkResult, NotFound, Ticket, TicketAttachment, TicketComment, TicketConnection,
    TicketEvent, TicketProject, TicketRef, TransitionResult)
from boss_engine.connectors.tickets.text import acceptance_criteria
from boss_engine.services.work_tracker_adapters import IssueCreateResult

from tests.ticket_connectors import dataset as D

TOKEN = "fake-ticket-token-0123456789"
STORES: dict[str, dict[str, Any]] = {}


def new_store(name: str) -> dict[str, Any]:
    STORES[name] = {"issues": copy.deepcopy(D.ISSUES), "comments": {}, "links": {}}
    for n, item in STORES[name]["issues"].items():
        STORES[name]["comments"][n] = [TicketComment(a, f"2026-11-0{i + 1}T09:00:00Z", t)
                                       for i, (a, t) in enumerate(item.get("comments") or [])]
        STORES[name]["links"][n] = []
    return STORES[name]


class FakeTicketAdapter(TicketAdapterBase):
    provider_key = "fake_connector"
    display_name = "Fake tickets"

    def _store(self, conn: TicketConnection) -> dict[str, Any]:
        if conn.credential() != TOKEN:
            raise AuthFailed("fake: bad token", status=401, provider=self.provider_key)
        return STORES[str(conn.get("store"))]

    def test_connection(self, configuration: dict[str, Any]) -> dict[str, Any]:
        return {"ok": configuration.get("api_token") == TOKEN, "message": "fake"}

    def issue_url(self, configuration, *, external_project_id, external_issue_id,
                  external_key=None):
        return f"{configuration.get('base_url')}/browse/{external_key or external_issue_id}"

    def _n(self, ref: TicketRef) -> int:
        match = re.match(r"^KAN-(\d+)$", str(ref.key or ""))
        if not match:
            raise NotFound("fake: not a key", provider=self.provider_key)
        return int(match.group(1))

    def _ticket(self, conn: TicketConnection, n: int, store: dict[str, Any],
                with_comments: bool = False) -> Ticket:
        item = store["issues"].get(n)
        if item is None:
            raise NotFound(f"fake: no KAN-{n}", provider=self.provider_key)
        children = sum(1 for i in store["issues"].values() if i.get("parent") == n)
        return Ticket(
            provider=self.provider_key, key=f"KAN-{n}", id=str(n),
            url=f"{conn.get('base_url')}/browse/KAN-{n}", title=item["title"],
            body_markdown=item.get("body") or "", type=normalise_type(item["type"]),
            raw_type=item["type"], status=item["status"],
            status_category=self.category_for(conn, item["status"], D.CATEGORY[item["status"]]),
            priority=item.get("priority"), labels=list(item.get("labels") or []),
            assignee=item.get("assignee"), reporter=item.get("reporter"),
            acceptance_criteria=acceptance_criteria(item.get("body") or ""),
            comments=list(store["comments"][n])[-int(self.settings(conn)["comments_limit"]):]
            if with_comments else [],
            parent_key=f"KAN-{item['parent']}" if item.get("parent") else None,
            children_count=children,
            attachments=[TicketAttachment(a, f"{conn.get('base_url')}/a/{a}", s)
                         for a, s in item.get("attachments") or []],
            updated_at="2026-11-01T10:00:00Z")

    def ref_from_url(self, url: str, conn: Optional[TicketConnection] = None):
        parts = urlsplit(url)
        allowed = {"tickets.fake.example"} | ({(urlsplit(str(conn.get("base_url"))).hostname
                                                or "")} if conn else set())
        match = re.match(r"^/browse/(KAN-\d+)$", parts.path)
        if (parts.hostname or "") in allowed and match:
            return TicketRef(self.provider_key, key=match.group(1), url=url, project="KAN")
        return None

    def fetch_ticket(self, conn, ref):
        return self._ticket(conn, self._n(ref), self._store(conn), with_comments=True)

    def _all(self, conn):
        store = self._store(conn)
        return [self._ticket(conn, n, store) for n in sorted(store["issues"])]

    def search_tickets(self, conn, query, project, filters, limit):
        out = [t for t in self._all(conn) if (t.key == query if re.match(r"^KAN-\d+$", query)
                                              else query.lower() in t.title.lower())
               and (not filters.get("status_category")
                    or t.status_category == filters["status_category"])
               and set(filters.get("labels") or []) <= set(t.labels)]
        return out[:limit]

    def fetch_projects(self, conn, query, counts):
        self._store(conn)
        rows = [D.PROJECT] + D.OTHER_PROJECTS
        return [TicketProject(p["key"], p["name"], p["key"]) for p in rows
                if not query or query.lower() in p["name"].lower()]

    def fetch_epics(self, conn, project):
        return [t for t in self._all(conn) if t.type == "epic" and t.status_category != "done"]

    def fetch_children(self, conn, epic_ref):
        return [t for t in self._all(conn) if t.parent_key == epic_ref.key]

    def fetch_unparented(self, conn, project):
        return [t for t in self._all(conn) if not t.parent_key and t.type != "epic"
                and t.status_category != "done"]

    def post_comment(self, conn, ref, markdown):
        comment = TicketComment("BOSS bot", "2026-11-13T12:00:00Z", markdown)
        self._store(conn)["comments"][self._n(ref)].append(comment)
        return comment

    def apply_transition(self, conn, ref, category):
        store = self._store(conn)
        item = store["issues"][self._n(ref)]
        mapped = self.settings(conn)["status_mapping"].get(category)
        target = mapped or {v: k for k, v in D.CATEGORY.items()}[category]
        before = item["status"]
        item["status"] = target
        return TransitionResult(TicketRef(self.provider_key, key=ref.key), before, target,
                                category, before != target)

    def add_link(self, conn, ref, url, title):
        links = self._store(conn)["links"][self._n(ref)]
        if url in links:
            return LinkResult(ref, url, False)
        links.append(url)
        return LinkResult(ref, url, True)

    def create_ticket(self, conn, *, title, body_markdown, type, labels, project, parent):
        store = self._store(conn)
        n = max(store["issues"]) + 1
        store["issues"][n] = {"title": title, "type": type.capitalize(), "status": "To Do",
                              "body": body_markdown, "labels": labels,
                              "parent": self._n(parent) if parent else None}
        store["comments"][n], store["links"][n] = [], []
        return IssueCreateResult(str(n), f"KAN-{n}", f"{conn.get('base_url')}/browse/KAN-{n}")

    def check_webhook(self, headers, body, secret):
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, headers.get("x-fake-signature", ""))

    def read_webhook(self, body):
        data = json.loads(body) if isinstance(body, (bytes, str)) else body
        return TicketEvent(self.provider_key, data.get("kind", "other"),
                           TicketRef(self.provider_key, key=data.get("key")), data.get("actor"))
