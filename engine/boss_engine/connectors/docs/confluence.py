"""Confluence Cloud behind one small client (BOSSAPP-1384).

REST API v2 (``/wiki/api/v2``) for pages, spaces, labels, attachments and writes, and the v1
CQL search (``/wiki/rest/api/search``: v2 has no CQL search). Every request goes through the one
ticket-connector HTTP helper (connectors/tickets/http.py): a host other than the connection's own
site is refused, the credential is attached there and never returned, 429/5xx are retried, errors
are typed, and cursors are followed.

AUTH. ``basic`` = Atlassian account email + API token (Cloud). ``bearer`` = a bearer token: a
Data Center personal access token, or an OAuth 2.0 (3LO) access token. The structure is ready for
3LO (the connection would address ``https://api.atlassian.com/ex/confluence/<cloudId>`` as its
base and hold ``bearer``); only the sign-in flow is not built.

DATA CENTER / SERVER is a SEAM, not an implementation: ``DEPLOYMENTS`` names its API root
(``/rest/api``) and auth (PAT bearer) so the connection can be recorded, but every call refuses
with a plain message until a Data Center adapter is written.

The credential is passed as a resolver and never stored on the client or logged.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, quote, urlsplit

from ..tickets.contract import (
    AuthFailed,
    NotFound,
    TicketConnection,
    TicketSystemError,
)
from ..tickets.http import TicketHttp
from .storage_format import markdown_to_storage, storage_to_markdown

PROVIDER = "confluence"
NOT_CONNECTED = "Confluence is not connected. Connect Confluence in Setup › Connections, then try again."
REJECTED = ("Confluence rejected the credential. Reconnect Confluence in Setup › Connections "
            "(check the email, the API token and that the token's account can see the space).")

#: The deployments a connection may name. Only ``cloud`` is implemented.
DEPLOYMENTS: dict[str, dict[str, Any]] = {
    "cloud": {"implemented": True, "api_root": "/api/v2", "search": "/rest/api/search",
              "auth": ("basic", "bearer"), "base_path": "/wiki"},
    "datacenter": {"implemented": False, "api_root": "/rest/api", "search": "/rest/api/search",
                   "auth": ("bearer",), "base_path": ""},
}
AUTH_METHODS = ("basic", "bearer")

#: Size caps: what one read returns, how many rows a list returns.
MAX_BODY_CHARS = 60_000
MAX_SEARCH = 50
MAX_TREE = 500
PAGE_SIZE = 100


class ConfluenceError(TicketSystemError):
    """A Confluence call failed (message never contains a credential)."""


class ConfluenceNotConnected(ConfluenceError):
    """No Confluence connection (or no credential) for this org."""


class VersionConflict(ConfluenceError):
    """The page changed since it was read: ``current_version`` is what is there now."""

    def __init__(self, message: str, *, current_version: Optional[int] = None,
                 expected_version: Optional[int] = None):
        super().__init__(message, provider=PROVIDER, status=409)
        self.current_version = current_version
        self.expected_version = expected_version


@dataclass
class PageSummary:
    id: str
    title: str
    space_key: str = ""
    url: str = ""
    excerpt: str = ""
    last_modified: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "space": self.space_key, "url": self.url,
                "excerpt": self.excerpt, "last_modified": self.last_modified}


@dataclass
class Page:
    id: str
    title: str
    space_key: str
    space_id: str = ""
    version: int = 0
    url: str = ""
    parent_id: Optional[str] = None
    last_modified: Optional[str] = None
    author_id: Optional[str] = None
    labels: list[str] = field(default_factory=list)
    body_markdown: str = ""
    truncated: bool = False
    body_chars: int = 0
    status: str = "current"

    def as_dict(self, *, body: bool = True) -> dict[str, Any]:
        out = {"id": self.id, "title": self.title, "space": self.space_key, "version": self.version,
               "url": self.url, "parent_id": self.parent_id, "last_modified": self.last_modified,
               "labels": list(self.labels), "truncated": self.truncated}
        if body:
            out["body_markdown"] = self.body_markdown
            out["body_chars"] = self.body_chars
        return out


def cql_quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_cql(text: str = "", *, cql: str = "", spaces: tuple[str, ...] = (),
              labels: tuple[str, ...] = (), content_type: str = "page") -> str:
    """CQL from words and filters. A raw ``cql`` is wrapped, and its ``order by`` kept last."""
    order = "order by lastmodified desc"
    clauses: list[str] = []
    raw = (cql or "").strip()
    if raw:
        match = re.search(r"\border\s+by\b.*$", raw, re.IGNORECASE | re.DOTALL)
        if match:
            order = match.group(0).strip()
            raw = raw[:match.start()].strip()
        if raw:
            clauses.append(f"({raw})")
    if content_type:
        clauses.append(f"type = {cql_quote(content_type)}")
    spaces = tuple(s for s in (str(x).strip() for x in spaces) if s)
    if spaces:
        clauses.append("space in (" + ", ".join(cql_quote(s) for s in spaces) + ")")
    labels = tuple(s for s in (str(x).strip() for x in labels) if s)
    if labels:
        clauses.append("label in (" + ", ".join(cql_quote(s) for s in labels) + ")")
    if (text or "").strip():
        clauses.append(f"text ~ {cql_quote(text.strip())}")
    return " AND ".join(clauses) + f" {order}"


_PAGE_ID = re.compile(r"/pages/(\d+)")
_SPACE_IN_PATH = re.compile(r"/spaces/([^/]+)/")


def page_id_from_url(url: str) -> Optional[str]:
    """The page id in a Confluence URL (``/spaces/KEY/pages/123/Title``, ``?pageId=123``)."""
    parts = urlsplit(url or "")
    match = _PAGE_ID.search(parts.path)
    if match:
        return match.group(1)
    found = parse_qs(parts.query).get("pageId", [""])[0]
    return found if found.isdigit() else None


def auth_headers(config: dict[str, Any], token: str) -> dict[str, str]:
    """The Authorization header for this connection's method. Never logged, never returned."""
    if str(config.get("auth_method") or "basic") == "bearer":
        return {"Authorization": f"Bearer {token}"}
    email = str(config.get("email") or "").strip()
    return {"Authorization": "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()}


def validate_site(base_url: str, deployment: str = "cloud") -> str:
    """The normalised site URL, or a plain refusal. Cloud is ``https://<site>.atlassian.net`` with
    ``/wiki`` appended; the operator may allow another host (BOSS_CREDENTIAL_HOSTS=confluence=...)
    for a proxy or a Data Center seam."""
    from ...services import env_refs

    raw = (base_url or "").strip().rstrip("/")
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise ConfluenceError("The site address is an https URL, like "
                              "https://your-team.atlassian.net/wiki.", provider=PROVIDER)
    operator = env_refs._operator_hosts(PROVIDER)  # noqa: SLF001
    if not (host.endswith(".atlassian.net") or host in operator):
        raise ConfluenceError(
            f"{host} is not an Atlassian Cloud site (https://<site>.atlassian.net). Another host "
            "must be allowed by the operator (BOSS_CREDENTIAL_HOSTS=confluence=<host>).",
            provider=PROVIDER)
    path = parts.path.rstrip("/")
    if deployment == "cloud" and not path.endswith("/wiki"):
        path = (path + "/wiki") if not path else path
    return f"https://{parts.netloc}{path}"


class ConfluenceClient:
    """One Confluence connection's reads and writes. ``token`` is a resolver, called per request."""

    def __init__(self, config: dict[str, Any], token: Callable[[], Optional[str]], *,
                 conn: Optional[TicketConnection] = None, timeout: float = 20.0):
        self.config = dict(config or {})
        self.deployment = str(self.config.get("deployment") or "cloud")
        spec = DEPLOYMENTS.get(self.deployment)
        if spec is None:
            raise ConfluenceError(f"Unknown Confluence deployment {self.deployment!r}.",
                                  provider=PROVIDER)
        if not spec["implemented"]:
            raise ConfluenceError(
                "Confluence Data Center / Server is not supported yet (only Confluence Cloud). "
                "The connection records its base path and a personal access token for when it is.",
                provider=PROVIDER)
        self.base = str(self.config.get("base_url") or "").rstrip("/")
        if not self.base:
            raise ConfluenceNotConnected(NOT_CONNECTED, provider=PROVIDER)
        self.api = spec["api_root"]
        self.search_path = spec["search"]
        self._resolve = token
        self._tok: Optional[str] = None
        self.conn = conn or TicketConnection(PROVIDER, {"base_url": self.base},
                                             credential_resolver=self._token)
        self.conn.configuration.setdefault("base_url", self.base)
        self._http = TicketHttp(
            self.conn, base_url=self.base, auth=lambda cred: (auth_headers(self.config, cred.token), None),
            default_hosts=(), base_url_fields=("base_url",), guard_provider=PROVIDER,
            timeout=timeout, extra_headers={"User-Agent": "BOSS-confluence-connector/1"})
        self._space_keys: dict[str, str] = {}
        self._space_ids: dict[str, str] = {}
        self._origin = "{0.scheme}://{0.netloc}".format(urlsplit(self.base))

    # -- plumbing -----------------------------------------------------------------------------

    def _token(self) -> Optional[str]:
        """The credential, resolved once per client (each resolution is logged by the vault)."""
        if self._tok is None:
            self._tok = self._resolve() or ""
        return self._tok

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        if not self._token():
            raise ConfluenceNotConnected(NOT_CONNECTED, provider=PROVIDER)
        try:
            return self._http.request(method, path, **kw).json()
        except AuthFailed as error:
            if error.reason == "no_credential":
                raise ConfluenceNotConnected(NOT_CONNECTED, provider=PROVIDER) from None
            raise AuthFailed(REJECTED, status=error.status, provider=PROVIDER) from None
        except NotFound as error:
            raise NotFound(f"Confluence: not found ({urlsplit(path).path}). The page or space "
                           "may not exist, or the connected account cannot see it.",
                           status=404, provider=PROVIDER) from error

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        return self._call("GET", path, params=params)

    def _v2(self, tail: str) -> str:
        return f"{self.api}/{tail.lstrip('/')}"

    def _absolute(self, link: str) -> str:
        """A ``_links.next`` (``/wiki/api/v2/...``) on this site's origin."""
        return link if "://" in link else f"{self._origin}{link}"

    def _cursor_pages(self, path: str, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """Results across v2 cursor pages, at most ``limit`` rows."""
        rows: list[dict[str, Any]] = []
        data = self._get(path, params)
        for _ in range(50):
            rows.extend((data or {}).get("results") or [])
            nxt = ((data or {}).get("_links") or {}).get("next")
            if not nxt or len(rows) >= limit:
                break
            data = self._get(self._absolute(nxt))
        return rows[:limit]

    def _site_url(self, webui: str) -> str:
        return f"{self.base}{webui}" if webui.startswith("/") else webui

    # -- spaces -------------------------------------------------------------------------------

    def space_id(self, key: str) -> str:
        key = str(key).strip()
        if key not in self._space_ids:
            data = self._get(self._v2("spaces"), {"keys": key, "limit": 1})
            found = (data or {}).get("results") or []
            if not found:
                raise NotFound(f"Confluence: no space with key {key!r}, or the connected account "
                               "cannot see it.", status=404, provider=PROVIDER)
            self._space_ids[key] = str(found[0]["id"])
            self._space_keys[str(found[0]["id"])] = str(found[0].get("key") or key)
        return self._space_ids[key]

    def space_key(self, space_id: str, webui: str = "") -> str:
        match = _SPACE_IN_PATH.search(webui or "")
        if match:
            return match.group(1)
        if space_id and space_id not in self._space_keys:
            data = self._get(self._v2(f"spaces/{space_id}"))
            self._space_keys[space_id] = str((data or {}).get("key") or "")
        return self._space_keys.get(space_id, "")

    def list_spaces(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._cursor_pages(self._v2("spaces"), {"limit": min(limit, 250), "status": "current"},
                                  min(limit, 500))
        return [{"id": str(r["id"]), "key": r.get("key"), "name": r.get("name"),
                 "type": r.get("type")} for r in rows]

    # -- test ---------------------------------------------------------------------------------

    def test(self) -> dict[str, Any]:
        """A cheap check: one space listed (the account can use Confluence at all) and who the
        token is. Raises a typed error; never stores anything."""
        data = self._get(self._v2("spaces"), {"limit": 1})
        out: dict[str, Any] = {"ok": True, "site": self.base,
                               "sees_spaces": bool((data or {}).get("results"))}
        try:
            me = self._get("/rest/api/user/current")
            out["account"] = (me or {}).get("displayName") or (me or {}).get("email")
        except TicketSystemError:
            out["account"] = None
        return out

    # -- read ---------------------------------------------------------------------------------

    def search(self, text: str = "", *, cql: str = "", spaces: tuple[str, ...] = (),
               labels: tuple[str, ...] = (), limit: int = 20, start: int = 0
               ) -> list[PageSummary]:
        """CQL search for pages, newest change first. Pages through the results by ``start`` until
        ``limit`` rows (at most MAX_SEARCH)."""
        limit = max(1, min(int(limit or 20), MAX_SEARCH))
        query = build_cql(text, cql=cql, spaces=tuple(spaces), labels=tuple(labels))
        found: list[PageSummary] = []
        offset = max(0, int(start or 0))
        for _ in range(10):
            want = limit - len(found)
            data = self._get(self.search_path, {"cql": query, "limit": min(want, PAGE_SIZE),
                                                "start": offset, "excerpt": "highlight"})
            results = (data or {}).get("results") or []
            for r in results:
                content = r.get("content") or {}
                page_id = str(content.get("id") or "")
                if not page_id:
                    continue
                url = str(r.get("url") or "")
                match = _SPACE_IN_PATH.search(url)
                found.append(PageSummary(
                    id=page_id, title=str(content.get("title") or r.get("title") or ""),
                    space_key=match.group(1) if match else str(
                        (r.get("resultGlobalContainer") or {}).get("title") or ""),
                    url=self._site_url(url), excerpt=_plain(str(r.get("excerpt") or "")),
                    last_modified=r.get("lastModified")))
            offset += len(results)
            has_next = bool(((data or {}).get("_links") or {}).get("next"))
            if len(found) >= limit or not results or not has_next:
                break
        return found[:limit]

    def _page(self, data: dict[str, Any], *, max_chars: int, with_body: bool) -> Page:
        links = data.get("_links") or {}
        webui = str(links.get("webui") or "")
        space_id = str(data.get("spaceId") or "")
        version = data.get("version") or {}
        storage = (((data.get("body") or {}).get("storage") or {}).get("value")) or ""
        markdown = storage_to_markdown(storage) if with_body else ""
        total = len(markdown)
        cap = max(1, min(int(max_chars or MAX_BODY_CHARS), MAX_BODY_CHARS))
        return Page(
            id=str(data.get("id")), title=str(data.get("title") or ""),
            space_key=self.space_key(space_id, webui), space_id=space_id,
            version=int(version.get("number") or 0), url=self._site_url(webui),
            parent_id=str(data["parentId"]) if data.get("parentId") else None,
            last_modified=version.get("createdAt"), author_id=version.get("authorId"),
            body_markdown=markdown[:cap], truncated=total > cap, body_chars=total,
            status=str(data.get("status") or "current"))

    def get_page(self, page_id: str, *, max_chars: int = MAX_BODY_CHARS,
                 labels: bool = False, body: bool = True) -> Page:
        data = self._get(self._v2(f"pages/{quote(str(page_id), safe='')}"),
                         {"body-format": "storage"} if body else None)
        page = self._page(data or {}, max_chars=max_chars, with_body=body)
        if labels:
            page.labels = self.labels(page.id)
        return page

    def find_page(self, title: str, space: str, *, max_chars: int = MAX_BODY_CHARS) -> Page:
        data = self._get(self._v2("pages"), {"space-id": self.space_id(space), "title": title,
                                             "body-format": "storage", "limit": 1,
                                             "status": "current"})
        results = (data or {}).get("results") or []
        if not results:
            raise NotFound(f"Confluence: no page titled {title!r} in space {space}.",
                           status=404, provider=PROVIDER)
        return self._page(results[0], max_chars=max_chars, with_body=True)

    def read(self, *, page_id: str = "", title: str = "", space: str = "", url: str = "",
             max_chars: int = MAX_BODY_CHARS, labels: bool = True) -> Page:
        """One page by id, by URL, or by title + space."""
        page_id = str(page_id or "").strip()
        if not page_id and url:
            page_id = page_id_from_url(url) or ""
            if not page_id:
                raise NotFound("Confluence: that URL has no page id (use a /pages/<id>/ link, a "
                               "viewpage.action?pageId= link, or the page's title and space).",
                               status=404, provider=PROVIDER)
        if page_id:
            return self.get_page(page_id, max_chars=max_chars, labels=labels)
        if title and space:
            page = self.find_page(title, space, max_chars=max_chars)
            if labels:
                page.labels = self.labels(page.id)
            return page
        raise ConfluenceError("Name the page: its id, its URL, or its title and space.",
                              provider=PROVIDER)

    def labels(self, page_id: str) -> list[str]:
        rows = self._cursor_pages(self._v2(f"pages/{page_id}/labels"), {"limit": 100}, 200)
        return [str(r.get("name")) for r in rows if r.get("name")]

    def attachments(self, page_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._cursor_pages(self._v2(f"pages/{page_id}/attachments"), {"limit": 100}, limit)
        return [{"id": str(r.get("id")), "name": r.get("title"), "media_type": r.get("mediaType"),
                 "size": r.get("fileSize"), "download": self._site_url(str(r.get("downloadLink") or "")),
                 "version": (r.get("version") or {}).get("number"),
                 "modified": (r.get("version") or {}).get("createdAt")} for r in rows]

    def space_tree(self, space: str, *, root_id: str = "", limit: int = MAX_TREE
                   ) -> list[dict[str, Any]]:
        """A space's pages as a depth-ordered tree (parent before children), optionally only the
        subtree under ``root_id``. At most ``limit`` (MAX_TREE) pages."""
        limit = max(1, min(int(limit or MAX_TREE), MAX_TREE))
        sid = self.space_id(space)
        rows = self._cursor_pages(self._v2(f"spaces/{sid}/pages"),
                                  {"limit": 250, "status": "current", "sort": "title"}, MAX_TREE * 2)
        by_parent: dict[Optional[str], list[dict[str, Any]]] = {}
        known = {str(r["id"]) for r in rows}
        for r in rows:
            parent = str(r["parentId"]) if r.get("parentId") and str(r["parentId"]) in known else None
            by_parent.setdefault(parent, []).append(r)
        out: list[dict[str, Any]] = []

        def walk(parent: Optional[str], depth: int) -> None:
            for r in by_parent.get(parent, []):
                if len(out) >= limit:
                    return
                version = r.get("version") or {}
                out.append({"id": str(r["id"]), "title": r.get("title"), "depth": depth,
                            "parent_id": str(r["parentId"]) if r.get("parentId") else None,
                            "version": version.get("number"), "last_modified": version.get("createdAt"),
                            "url": self._site_url(str((r.get("_links") or {}).get("webui") or ""))})
                walk(str(r["id"]), depth + 1)

        if root_id:
            root = next((r for r in rows if str(r["id"]) == str(root_id)), None)
            if root is None:
                raise NotFound(f"Confluence: page {root_id} is not in space {space}.", status=404,
                               provider=PROVIDER)
            version = root.get("version") or {}
            out.append({"id": str(root["id"]), "title": root.get("title"), "depth": 0,
                        "parent_id": root.get("parentId"), "version": version.get("number"),
                        "last_modified": version.get("createdAt"),
                        "url": self._site_url(str((root.get("_links") or {}).get("webui") or ""))})
            walk(str(root["id"]), 1)
        else:
            walk(None, 0)
        return out

    def changed_since(self, space: str, since: Optional[str], limit: int = 200) -> list[PageSummary]:
        """Pages of a space modified after ``since`` (an ISO date), newest first."""
        clause = f'lastmodified >= "{since[:16].replace("T", " ")}"' if since else ""
        found: list[PageSummary] = []
        while len(found) < limit:
            size = min(MAX_SEARCH, limit - len(found))
            batch = self.search(cql=clause, spaces=(space,), limit=size, start=len(found))
            found.extend(batch)
            if len(batch) < size:
                break
        return found[:limit]

    # -- write --------------------------------------------------------------------------------

    def create_page(self, *, space: str, title: str, markdown: str,
                    parent_id: Optional[str] = None) -> Page:
        body = {"spaceId": self.space_id(space), "status": "current", "title": title,
                "body": {"representation": "storage", "value": markdown_to_storage(markdown)}}
        if parent_id:
            body["parentId"] = str(parent_id)
        data = self._call("POST", self._v2("pages"), json_body=body)
        return self._page(data or {}, max_chars=MAX_BODY_CHARS, with_body=False)

    def update_page(self, page_id: str, *, markdown: str, title: Optional[str] = None,
                    expected_version: Optional[int] = None, message: str = "Updated by BOSS"
                    ) -> Page:
        """A versioned update: ``expected_version`` is the version the author read. A newer page
        refuses with :class:`VersionConflict` (the new version in it); nothing is overwritten."""
        current = self.get_page(page_id, body=False)
        if expected_version is not None and current.version != int(expected_version):
            raise VersionConflict(
                f"Confluence page {page_id} is at version {current.version}, but this change was "
                f"written against version {expected_version}. Read the page again and redo the "
                "change.", current_version=current.version, expected_version=int(expected_version))
        body = {"id": str(page_id), "status": "current", "title": title or current.title,
                "body": {"representation": "storage", "value": markdown_to_storage(markdown)},
                "version": {"number": current.version + 1, "message": message[:200]}}
        try:
            data = self._call("PUT", self._v2(f"pages/{quote(str(page_id), safe='')}"),
                              json_body=body)
        except TicketSystemError as error:
            if getattr(error, "status", None) == 409:
                latest = None
                try:
                    latest = self.get_page(page_id, body=False).version
                except TicketSystemError:
                    pass
                raise VersionConflict(
                    f"Confluence page {page_id} changed while it was being updated"
                    + (f" (now version {latest})" if latest else "")
                    + ". Read it again and redo the change.", current_version=latest,
                    expected_version=current.version) from None
            raise
        return self._page(data or {}, max_chars=MAX_BODY_CHARS, with_body=False)


def _plain(excerpt: str) -> str:
    """A search excerpt without the highlight markers."""
    return re.sub(r"@@@(?:end)?hl@@@", "", re.sub(r"<[^>]+>", "", excerpt)).strip()[:400]
