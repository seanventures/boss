"""A recorded-HTTP stand-in for Confluence Cloud: the transport the ticket-connector HTTP helper
sends through is replaced by this object (``ticket_http.TRANSPORT``), which answers from the JSON
fixtures in ./fixtures (shaped like Confluence's v2 and CQL-search responses). No socket is opened.

It checks the Authorization header (a wrong credential answers 401), pages the CQL search, and
keeps each page's version so a stale PUT answers 409 like the real service.
"""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from boss_engine.connectors.tickets.http import Response

FIXTURES = Path(__file__).parent / "fixtures"
SITE = "https://acme.atlassian.net/wiki"
EMAIL = "bot@acme.test"
TOKEN = "ATATT3xFfGF0-fixture-token-do-not-use-0123456789"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _json(status: int, body) -> Response:
    return Response(status, {"content-type": "application/json"}, json.dumps(body).encode())


class FakeConfluence:
    def __init__(self, *, email: str = EMAIL, token: str = TOKEN):
        self.expect = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        self.requests: list[dict] = []
        self.versions = {"101": 4}
        self.force_conflict = False
        self.pages_created: list[dict] = []
        self.pages_updated: list[dict] = []

    # the TRANSPORT signature: (method, url, headers, data, timeout, context)
    def __call__(self, method, url, headers, data, timeout, context) -> Response:
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        body = json.loads(data) if data else None
        self.requests.append({"method": method, "url": url, "path": parts.path, "query": query,
                              "headers": dict(headers), "body": body, "host": parts.hostname})
        if headers.get("Authorization") != self.expect:
            return _json(401, fixture("unauthorized"))
        return self.route(method, parts.path, query, body)

    def page(self, page_id: str, name: str = "page_101") -> dict:
        page = copy.deepcopy(fixture(name))
        page["id"] = page_id
        page["version"]["number"] = self.versions.get(page_id, page["version"]["number"])
        return page

    def route(self, method, path, query, body) -> Response:
        p = path.removeprefix("/wiki")
        if method == "GET" and p == "/api/v2/spaces":
            spaces = fixture("spaces")
            if query.get("keys"):
                spaces["results"] = [s for s in spaces["results"] if s["key"] == query["keys"]]
            elif query.get("limit") == "1":
                spaces["results"] = spaces["results"][:1]
            return _json(200, spaces)
        if method == "GET" and p == "/api/v2/spaces/98":
            return _json(200, fixture("space_eng"))
        if method == "GET" and p == "/api/v2/spaces/98/pages":
            return _json(200, fixture("space_pages"))
        if method == "GET" and p == "/rest/api/user/current":
            return _json(200, fixture("user_current"))
        if method == "GET" and p == "/rest/api/search":
            return _json(200, fixture("search_p2" if query.get("start") == "2" else "search_p1"))
        if method == "GET" and p == "/api/v2/pages":
            if query.get("title") == "Deploy Runbook":
                found = fixture("page_by_title")
                found["results"][0]["version"]["number"] = self.versions["101"]
                return _json(200, found)
            return _json(200, {"results": [], "_links": {}})
        if method == "GET" and p.startswith("/api/v2/pages/"):
            page_id, _, tail = p.removeprefix("/api/v2/pages/").partition("/")
            if tail == "labels":
                return _json(200, fixture("labels_101"))
            if tail == "attachments":
                return _json(200, fixture("attachments_101"))
            if page_id != "101":
                return _json(404, {"message": "not found"})
            return _json(200, self.page(page_id))
        if method == "POST" and p == "/api/v2/pages":
            self.pages_created.append(body)
            made = fixture("page_new")
            made["title"] = body["title"]
            return _json(200, made)
        if method == "PUT" and p == "/api/v2/pages/101":
            self.pages_updated.append(body)
            if self.force_conflict or body["version"]["number"] != self.versions["101"] + 1:
                return _json(409, fixture("conflict"))
            self.versions["101"] += 1
            return _json(200, self.page("101", "page_new_updated"))
        return _json(404, {"message": f"no fixture for {method} {p}"})
