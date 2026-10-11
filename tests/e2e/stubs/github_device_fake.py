"""GitHub's OAuth DEVICE FLOW and a GitHub App's user-to-server reach, faked.

What "Sign in with GitHub" (engine identity/github_device.py) talks to:

  github.com      POST /login/device/code            a device code and the user code to type
                  POST /login/oauth/access_token     the poll: authorization_pending | slow_down |
                                                     expired_token | access_denied | a token
  api.github.com  GET  /user                         who a user token belongs to
                  GET  /user/installations           the App's installations this person can see
                  GET  /user/installations/{id}/repositories   what they reach through each
                  GET  /user/orgs                    the organisations they belong to

The person's side (typing the code at github.com/login/device and pressing Authorize) is played by
`approve(user_code)` / `deny(user_code)` — or, in the e2e stack, POST /github/_device on the fakes'
control port. `slow_down_next` makes the next poll answer slow_down (GitHub's "poll less often");
`expire(user_code)` makes the code lapse. One instance serves the e2e stack (fakes.py routes the
two hosts here) and the engine's unit tests (as `github_app.TRANSPORT`).

THE REACH IS GITHUB'S RULE, not a list: a user token sees an installation only when the person IS
its account or is a member of that organisation, and sees a repository through it only when the
person has access to that repository (`member_repos`). An organisation the person belongs to that
has no installation is what "Install BOSS on <org>" is for.
"""
from __future__ import annotations

import itertools
import json
import threading
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceFlowFake:
    def __init__(self, *, client_id: str = "Iv23-boss-published", interval: int = 5, expires_in: int = 900):
        self.client_id = client_id
        self.interval = interval
        self.expires_in = expires_in
        self.device_flow_enabled = True
        self.slow_down_next = False
        #: Approve every code on its Nth poll (the e2e walk sets it; unit tests approve by hand).
        self.auto_approve_after: Optional[int] = None
        self.auto_login = "octo-dev"
        #: Issue this exact token (e.g. a github_probes profile such as ghp_e2e_full, so Repo and
        #: models' Read/Write/Pull-request probes pass on it); None = a fresh ghu_ token each time.
        self.fixed_token: Optional[str] = None
        self.codes: dict[str, dict[str, Any]] = {}          # device_code -> state
        self.tokens: dict[str, str] = {}                     # user token -> login
        self.people: dict[str, dict[str, Any]] = {"octo-dev": {"id": 501, "orgs": ["acme", "globex"]}}
        self.orgs: dict[str, int] = {"acme": 9001, "globex": 9002}
        #: The App's installations: id -> account (login, type) and the repositories it was given.
        self.installations: dict[int, dict[str, Any]] = {
            71: {"account": "acme", "type": "Organization", "selection": "selected",
                 "repos": ["acme/web", "acme/api", "acme/secret-infra"]},
            72: {"account": "someone-else", "type": "User", "selection": "all", "repos": ["someone-else/dotfiles"]},
        }
        #: What each person may do on each repository (GitHub's `permissions` on the repo object).
        self.member_repos: dict[str, dict[str, dict[str, bool]]] = {
            "octo-dev": {"acme/web": {"pull": True, "push": True, "admin": False},
                         "acme/api": {"pull": True, "push": False, "admin": False}},
        }
        self.calls: list[tuple[str, str]] = []
        self._n = itertools.count(1)
        self._lock = threading.RLock()

    # ---- the person's side -----------------------------------------------------------------------------
    def _by_user_code(self, user_code: str) -> Optional[dict[str, Any]]:
        return next((c for c in self.codes.values() if c["user_code"] == user_code), None)

    def approve(self, user_code: Optional[str] = None, login: str = "octo-dev") -> bool:
        with self._lock:
            targets = [self._by_user_code(user_code)] if user_code else [
                c for c in self.codes.values() if c["status"] == "pending"]
            for c in targets:
                if c:
                    c["status"], c["login"] = "approved", login
            return any(targets)

    def deny(self, user_code: str) -> None:
        with self._lock:
            c = self._by_user_code(user_code)
            if c:
                c["status"] = "denied"

    def expire(self, user_code: str) -> None:
        with self._lock:
            c = self._by_user_code(user_code)
            if c:
                c["expires_at"] = time.time() - 1

    # ---- github.com ----------------------------------------------------------------------------------
    def device_code(self, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if form.get("client_id") != self.client_id:
            return 200, {"error": "incorrect_client_credentials"}
        if not self.device_flow_enabled:
            return 200, {"error": "device_flow_disabled"}
        with self._lock:
            n = next(self._n)
            device = f"dc_{n:04d}_{int(time.time())}"
            user = f"BOSS-{n:04d}"
            self.codes[device] = {"user_code": user, "status": "pending", "polls": 0, "login": None,
                                  "expires_at": time.time() + self.expires_in}
        return 200, {"device_code": device, "user_code": user, "verification_uri": "https://github.com/login/device",
                     "expires_in": self.expires_in, "interval": self.interval}

    def access_token(self, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if form.get("grant_type") != DEVICE_GRANT:
            return 200, {"error": "unsupported_grant_type"}
        if form.get("client_id") != self.client_id:
            return 200, {"error": "incorrect_client_credentials"}
        with self._lock:
            c = self.codes.get(form.get("device_code") or "")
            if c is None:
                return 200, {"error": "incorrect_device_code"}
            c["polls"] += 1
            if c["status"] == "used":
                return 200, {"error": "incorrect_device_code"}
            if time.time() > c["expires_at"]:
                return 200, {"error": "expired_token"}
            if self.slow_down_next:
                self.slow_down_next = False
                return 200, {"error": "slow_down", "interval": self.interval + 5}
            if (c["status"] == "pending" and self.auto_approve_after is not None
                    and c["polls"] >= self.auto_approve_after):
                c["status"], c["login"] = "approved", self.auto_login
            if c["status"] == "denied":
                return 200, {"error": "access_denied"}
            if c["status"] != "approved":
                return 200, {"error": "authorization_pending"}
            c["status"] = "used"
            token = self.fixed_token or f"ghu_fake{next(self._n):04d}{'x' * 20}"
            self.tokens[token] = c["login"]
            return 200, {"access_token": token, "token_type": "bearer", "scope": "",
                         "expires_in": 28800, "refresh_token": f"ghr_fake{next(self._n):04d}",
                         "refresh_token_expires_in": 15897600}

    # ---- api.github.com, for a user token ---------------------------------------------------------------
    def _visible(self, login: str) -> list[tuple[int, dict[str, Any]]]:
        orgs = set(self.people.get(login, {}).get("orgs", []))
        return [(i, inst) for i, inst in self.installations.items()
                if inst["account"] == login or inst["account"] in orgs]

    def user_api(self, method: str, path: str, token: str) -> Optional[tuple[int, Any]]:
        login = self.tokens.get(token)
        if login is None:
            return None
        if method != "GET" or not path.startswith("/user"):
            return None                     # the repo API: the git-backed fake answers it
        person = self.people.get(login, {"id": 1, "orgs": []})
        if path == "/user":
            return 200, {"login": login, "id": person["id"], "type": "User"}
        if path == "/user/orgs":
            return 200, [{"login": o, "id": self.orgs.get(o, 0)} for o in person["orgs"]]
        if path == "/user/installations":
            visible = self._visible(login)
            return 200, {"total_count": len(visible), "installations": [
                {"id": i, "account": {"login": inst["account"], "type": inst["type"]},
                 "repository_selection": inst["selection"]} for i, inst in visible]}
        if path.startswith("/user/installations/") and path.endswith("/repositories"):
            iid = int(path.split("/")[3])
            inst = dict(self._visible(login)).get(iid)
            if inst is None:
                return 404, {"message": "Not Found"}
            mine = self.member_repos.get(login, {})
            repos = [{"full_name": r, "private": True, "default_branch": "main",
                      "owner": {"login": r.split("/")[0], "type": inst["type"]}, "permissions": mine[r]}
                     for r in inst["repos"] if r in mine]
            return 200, {"total_count": len(repos), "repositories": repos}
        return None

    # ---- one entry point for both hosts -------------------------------------------------------------------
    def handle(self, method: str, url: str, headers: dict[str, str], body: bytes) -> Optional[tuple[int, Any]]:
        """(status, json) for a request this fake answers, else None (someone else's route)."""
        parts = urlsplit(url)
        path = parts.path.rstrip("/") or "/"
        if method == "POST" and path in ("/login/device/code", "/login/oauth/access_token"):
            form = {k: v[0] for k, v in parse_qs((body or b"").decode()).items()}
            if not form and body:
                try:
                    form = {k: str(v) for k, v in json.loads(body).items()}
                except ValueError:
                    form = {}
            self.calls.append((method, path))
            if path == "/login/device/code":
                return self.device_code(form)
            if form.get("grant_type") == DEVICE_GRANT:
                return self.access_token(form)
            return None
        auth = {k.lower(): v for k, v in (headers or {}).items()}.get("authorization", "")
        token = auth.split(" ", 1)[-1] if auth else ""
        answer = self.user_api(method, path, token)
        if answer is not None:
            self.calls.append((method, path))
        return answer

    # ---- the e2e control port: POST /github/_device {"action": "approve"|"deny"|..., "user_code"} ---------
    def control(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or "approve")
        code = body.get("user_code")
        if action == "approve":
            return {"ok": self.approve(code, str(body.get("login") or "octo-dev"))}
        if action == "deny" and code:
            self.deny(code)
        elif action == "expire" and code:
            self.expire(code)
        elif action == "slow_down":
            self.slow_down_next = True
        elif action == "auto":
            self.auto_approve_after = int(body.get("after") or 2)
        return {"ok": True, "codes": [{"user_code": c["user_code"], "status": c["status"]}
                                      for c in self.codes.values()]}
