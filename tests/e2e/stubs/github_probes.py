"""What GitHub answers Repo and models' token and repository probes, per token profile.

The fake GitHub (github_fake.py), the API tests and the engine tests all answer the probes from
here, so they cannot disagree about what a read-only token or an expired one looks like. The
responses live in engine/tests/fixtures/github_probe_<profile>.json (derived from GitHub's REST
docs; spec §7.0 asks for them to be re-recorded against real tokens).

The PROFILE comes from the token: `ghp_e2e_<profile>` (e.g. `ghp_e2e_contents_read_only`).
`ghp_e2e_expiring` is the full profile with an expiry two days out. Any other token returns None
here and the caller answers as it always did.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

FIXTURES = Path(__file__).resolve().parents[3] / "engine" / "tests" / "fixtures"
PREFIX = "ghp_e2e_"

#: Every profile there is a fixture for.
PROFILES = tuple(sorted(p.stem[len("github_probe_"):] for p in FIXTURES.glob("github_probe_*.json")))


def profile_of(token: str) -> Optional[str]:
    token = (token or "").strip()
    if not token.startswith(PREFIX):
        return None
    name = token[len(PREFIX):]
    if name == "expiring":
        return name
    return name if name in PROFILES else None


def _load(profile: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"github_probe_{profile}.json").read_text())


#: The head branch Repo and models' pull-request probe names (setup_checks.probe_repository).
PROBE_HEAD = "boss-setup-check-"


def answer(token: str, method: str, path: str, *, body: Any = None,
           now: Optional[datetime] = None) -> Optional[tuple[int, dict[str, str], Any]]:
    """(status, headers, body) for a probe, or None when the git-backed fake should answer.

    A profile that ALLOWS a write lets real writes through: a build on `ghp_e2e_full` makes real
    blobs and opens a real pull request in the fake. Only the probe's own pull request (a head
    named boss-setup-check-…, which does not exist) gets GitHub's 422, and a profile that DENIES
    a write denies every one, build included."""
    profile = profile_of(token)
    if profile is None:
        return None
    data = _load("full" if profile == "expiring" else profile)
    path = path.split("?", 1)[0]
    if method == "GET" and path == "/user":
        key = "user"
    elif method == "GET" and path == "/user/repos":
        key = "repos"
    elif method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+", path):
        key = "repo"
    elif method == "POST" and re.fullmatch(r"/repos/[^/]+/[^/]+/git/blobs", path):
        key = "blobs"
    elif method == "POST" and re.fullmatch(r"/repos/[^/]+/[^/]+/pulls", path):
        key = "pulls"
    else:
        return None
    entry = data[key]
    allowed = int(entry["status"]) in (201, 422) and key in ("blobs", "pulls")
    if allowed and key == "blobs":
        return None                                   # a real blob in the fake's repository
    if allowed and key == "pulls":
        head = str((body or {}).get("head") or "") if isinstance(body, dict) else ""
        if not head.split(":")[-1].startswith(PROBE_HEAD):
            return None                               # a real pull request: the fake opens it
    headers = {k.lower(): v for k, v in (entry.get("headers") or {}).items()}
    if profile == "expiring" and "github-authentication-token-expiration" in headers:
        when = (now or datetime.now(timezone.utc)) + timedelta(days=2, hours=1)
        headers["github-authentication-token-expiration"] = when.strftime(
            "%Y-%m-%d %H:%M:%S UTC")
    body = entry.get("body")
    if key == "repo" and isinstance(body, dict) and entry["status"] == 200:
        full = path[len("/repos/"):]
        body = {**body, "full_name": full}
    return int(entry["status"]), headers, body


def http_for_tests(calls: Optional[list] = None):
    """A `setup_checks.Http` that answers from the profiles: the API and engine tests' GitHub."""
    import sys

    engine_root = Path(__file__).resolve().parents[3] / "engine"
    if str(engine_root) not in sys.path:
        sys.path.insert(0, str(engine_root))
    from boss_engine.services.setup_checks import HttpAnswer

    def http(method: str, url: str, *, headers: dict[str, str], json_body: Any = None,
             timeout: float = 15.0) -> HttpAnswer:
        path = url.split("api.github.com", 1)[-1]
        token = (headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if calls is not None:
            calls.append((method, path))
        assert "/git/refs" not in path, "a setup check must never create a branch"
        found = answer(token, method, path, body=json_body)
        if found is None and method == "POST" and path.endswith("/git/blobs"):
            return HttpAnswer(201, {}, {"sha": "0" * 40})          # the fake would store it
        if found is None:
            return HttpAnswer(401, {}, {"message": "Bad credentials"})
        status, out_headers, body = found
        return HttpAnswer(status, out_headers, body)

    return http
