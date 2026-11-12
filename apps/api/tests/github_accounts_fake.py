"""A fake GitHub for Add a GitHub account (BOSSAPP-945): the API test suite's and the dev launcher's.

It answers exactly the reads BOSS makes, in GitHub's shapes, from state a test (or the dev seam)
changes as a person would on github.com:

  GET  /users/{login}                          the account (User / Organization), or 404
  GET  /app/installations[/{id}]               the App's installations (asked with the App's JWT)
  POST /app/installations/{id}/access_tokens   an installation token (repos limited to that account)
  GET  /installation/repositories              the installation's repositories (by its token)
  DELETE /installation/token                   revoke
  GET  /apps/{slug}                            the App via the API: 200 when public, 404 when private
  GET  {web}/apps/{slug}                       the App's HTML page: 200 either way, as real github.com does
  GET  /repos/{owner}/{repo}, POST …/git/blobs, POST …/pulls   the access check's three probes

Control (what a person does on GitHub): `make_public()`, `install(login)`, `uninstall(id)`.
Every call is recorded in `calls` ("METHOD path"); nothing leaves the process.
"""
from __future__ import annotations

import itertools
import json
import threading
from typing import Any, Optional
from urllib.parse import urlsplit


class FakeGitHub:
    def __init__(self, *, api_base: str = "https://api.github.com", web_base: str = "https://github.com",
                 slug: str = "boss-localhost-ab12", owner: str = "paramg") -> None:
        self.api_host = urlsplit(api_base).netloc
        self.web_host = urlsplit(web_base).netloc
        self.slug = slug
        self.owner = owner
        self.public = False
        self.users: dict[str, dict[str, Any]] = {}
        self.repos: dict[str, list[dict[str, Any]]] = {}      # owner login (lower) -> repos
        self.installations: dict[str, str] = {}                # installation id -> owner login
        self.read_only: set[str] = set()                       # repos the App may only read
        self.tokens: dict[str, str] = {}                       # token -> installation id
        self.calls: list[str] = []
        self._ids = itertools.count(91000001)
        self._lock = threading.Lock()

    # ---- the world -------------------------------------------------------------------------------
    def user(self, login: str, kind: str = "User", *, uid: int, repos: tuple[str, ...] = (),
             public_repos: Optional[int] = None, name: Optional[str] = None) -> "FakeGitHub":
        self.users[login.lower()] = {
            "login": login, "id": uid, "type": kind, "name": name or login,
            "avatar_url": f"https://avatars.githubusercontent.com/u/{uid}?v=4",
            "html_url": f"https://github.com/{login}",
            "public_repos": len(repos) if public_repos is None else public_repos}
        self.repos[login.lower()] = [{"full_name": f"{login}/{r}", "private": i % 2 == 0, "default_branch": "main",
                                      "owner": {"login": login, "type": kind}} for i, r in enumerate(repos)]
        return self

    def install(self, login: str) -> str:
        """The person pressed Install on GitHub for this account."""
        with self._lock:
            for iid, who in self.installations.items():
                if who.lower() == login.lower():
                    return iid
            iid = str(next(self._ids))
            self.installations[iid] = self.users[login.lower()]["login"]
            return iid

    def uninstall(self, iid: str) -> None:
        self.installations.pop(str(iid), None)

    def make_public(self, public: bool = True) -> None:
        self.public = public

    # ---- the wire --------------------------------------------------------------------------------
    def _installation(self, iid: str) -> dict[str, Any]:
        u = self.users[self.installations[iid].lower()]
        return {"id": int(iid), "account": {"login": u["login"], "id": u["id"], "type": u["type"]},
                "repository_selection": "all", "suspended_at": None,
                "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"}}

    def __call__(self, method: str, url: str, headers: Any, body: Optional[bytes]):
        from boss_engine.identity.github_app import Response

        parts = urlsplit(url)
        path = parts.path
        self.calls.append(f"{method} {path}")
        auth = str((headers or {}).get("Authorization") or "").split(" ", 1)[-1]

        def ok(data: Any, status: int = 200) -> Any:
            return Response(status, json.dumps(data).encode())

        if parts.netloc == self.web_host and method == "GET":
            if path.rstrip("/") == f"/apps/{self.slug}":
                # Real github.com serves this HTML page 200 whether the App is public or private
                # (measured live, BOSSAPP-1017) — only the REST API distinguishes them. The fake
                # used to answer 404 here when private, which let a page-probing app_is_public()
                # pass every test while calling a private App public in production.
                return Response(200, b"<html>BOSS app</html>")
            return Response(404, b"")
        if path.startswith("/api/"):                     # a local fake served under /api
            path = path[4:]
        if method == "GET" and path.rstrip("/") == f"/apps/{self.slug}":
            if self.public:
                return ok({"slug": self.slug, "name": "BOSS", "owner": {"login": self.owner}})
            return ok({"message": "Not Found"}, 404)
        if method == "GET" and path.startswith("/users/"):
            u = self.users.get(path.split("/")[2].lower())
            return ok(u) if u else ok({"message": "Not Found"}, 404)
        if method == "GET" and path == "/app/installations":
            if not auth.startswith("ey"):
                return ok({"message": "A JSON web token could not be decoded"}, 401)
            return ok([self._installation(i) for i in self.installations])
        if method == "GET" and path.startswith("/app/installations/"):
            iid = path.split("/")[3]
            return ok(self._installation(iid)) if iid in self.installations else ok({}, 404)
        if method == "POST" and path.startswith("/app/installations/") and path.endswith("/access_tokens"):
            iid = path.split("/")[3]
            if iid not in self.installations:
                return ok({"message": "Not Found"}, 404)
            want = json.loads(body or b"{}").get("repositories") or []
            have = {r["full_name"].split("/", 1)[1].lower() for r in self.repos.get(self.installations[iid].lower(), [])}
            if any(str(w).lower() not in have for w in want):
                return ok({"message": "There is at least one repository that does not exist or is not "
                                      "accessible to the parent installation."}, 422)
            token = f"ghs_fake{iid}x{next(self._ids)}"
            self.tokens[token] = iid
            return ok({"token": token, "expires_at": "2099-01-01T00:00:00Z"}, 201)
        if method == "DELETE" and path == "/installation/token":
            return Response(204, b"")
        if method == "GET" and path == "/installation/repositories":
            iid = self.tokens.get(auth)
            if iid is None or iid not in self.installations:
                return ok({"message": "Bad credentials"}, 401)
            repos = self.repos.get(self.installations[iid].lower(), [])
            return ok({"total_count": len(repos), "repositories": repos})
        if path.startswith("/repos/"):
            return self._probe(method, path, auth)
        return ok({"message": f"fake GitHub has no {method} {path}"}, 404)

    def _probe(self, method: str, path: str, auth: str):
        from boss_engine.identity.github_app import Response

        bits = path.split("/")
        full = f"{bits[2]}/{bits[3]}"
        iid = self.tokens.get(auth)
        owner = self.installations.get(iid or "", "")
        mine = owner.lower() == bits[2].lower() and any(
            r["full_name"].lower() == full.lower() for r in self.repos.get(owner.lower(), []))
        if not mine:
            return Response(404, b'{"message":"Not Found"}')
        if method == "GET" and len(bits) == 4:
            return Response(200, json.dumps({"full_name": full, "default_branch": "main", "private": True}).encode())
        if method == "POST" and path.endswith("/git/blobs"):
            if full.lower() in self.read_only:
                return Response(403, b'{"message":"Resource not accessible by integration"}')
            return Response(201, b'{"sha":"abc"}')
        if method == "POST" and path.endswith("/pulls"):
            return Response(422, b'{"message":"Validation Failed"}')
        return Response(404, b"{}")

    # ---- the access check's client (setup_checks.default_http's shape) ----------------------------
    def http(self, method: str, url: str, *, headers: dict[str, str], json_body: Any = None,
             timeout: float = 15.0):
        from boss_engine.services.setup_checks import HttpAnswer

        resp = self(method, url, headers, json.dumps(json_body).encode() if json_body is not None else None)
        try:
            data = json.loads(resp.body or b"null")
        except ValueError:
            data = None
        return HttpAnswer(resp.status, {}, data)


def demo_world(**kw: Any) -> FakeGitHub:
    """The world the e2e and the screenshots use: the operator's own account owns the App
    (installed there), and an organisation it is not installed on yet."""
    hub = FakeGitHub(**kw)
    hub.user("paramg", "User", uid=5101, repos=("helpdesk-ai-demo", "boss"), name="Param G")
    hub.user("kaappu", "Organization", uid=7202, repos=("helpdesk", "website", "infra-scripts"), public_repos=14,
             name="Kaappu")
    hub.user("octo-person", "User", uid=8303, repos=("dotfiles",))
    hub.install("paramg")
    return hub
