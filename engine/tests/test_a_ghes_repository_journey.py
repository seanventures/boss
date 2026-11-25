"""GitHub Enterprise Server, end to end against a local fake (BOSSAPP-1286).

A GHES instance serves REST under ``https://<host>/api/v3``, GraphQL at ``/api/graphql`` and git
at ``https://<host>/<owner>/<repo>.git``. This journey stands one up on 127.0.0.1 — the
git-backed GitHub fake the e2e stack uses (tests/e2e/stubs/github_fake.py, every write a real git
object) mounted under /api/v3, plus git's own http-backend for clone and push — and walks what a
preview team does, with the CONNECTION's API base as the only thing pointing BOSS at it:

  connect   the token and the repository are checked against the GHES API
  clone     the clone URL and the credential header are for the GHES host
  PR        branch, commit, pull request through the REST API on GHES
  checks    the CI reader reads the head's check runs from GHES
  webhook   a signed delivery from GHES is verified and parsed
  poller    an install GHES cannot reach polls GHES for the new pull request

The install's own default stays github.com (no BOSS_GITHUB_API_URL): a path that ignored the
connection would call api.github.com and fail the "every request reached GHES" assertion.

What this does NOT prove (the live check, filed with the ticket): a real GHES version's API
differences, its TLS chain under a custom CA, and an outbound proxy. That needs a GHES sandbox
(URL, version, a token) from the operator.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
STUBS = ROOT / "tests" / "e2e" / "stubs"
TOKEN = "ghes-token-for-the-journey"   # a placeholder, not a credential
OWNER, REPO = "platform", "gateway-onboarding"
FULL = f"{OWNER}/{REPO}"


class _Ghes:
    """A GHES on 127.0.0.1: /api/v3 -> the REST fake, /api/graphql -> its GraphQL, anything else
    -> git smart-HTTP. Every request is recorded (path, and whether it carried a credential)."""

    def __init__(self, root: Path):
        sys.path.insert(0, str(STUBS))
        try:
            from egress import GitRemotes
            from fakes import World
            from github_fake import GitHubFake
        finally:
            sys.path.remove(str(STUBS))
        self.remotes = GitRemotes(root / "git")
        self.world = World()
        self.rest = GitHubFake(self.remotes, self.world)
        self.requests: list[dict] = []
        ghes = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401 — quiet
                pass

            def _answer(self, method: str) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                path, _, query = self.path.partition("?")
                ghes.requests.append({"method": method, "path": path,
                                      "auth": bool(self.headers.get("authorization"))})
                if path.startswith("/api/v3/") or path == "/api/graphql":
                    rest_path = "/graphql" if path == "/api/graphql" else path[len("/api/v3"):]
                    try:
                        body = json.loads(raw) if raw else None
                    except ValueError:
                        body = None
                    status, payload = ghes.rest.handle(method, rest_path, query, body)
                    data = json.dumps(payload).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                headers = {k.lower(): v for k, v in self.headers.items()}
                status, out, payload = ghes.remotes.serve(method, path, query, headers, raw)
                self.send_response(status)
                for k, v in out.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):  # noqa: N802
                self._answer("GET")

            def do_POST(self):  # noqa: N802
                self._answer("POST")

            def do_PUT(self):  # noqa: N802
                self._answer("PUT")

            def do_PATCH(self):  # noqa: N802
                self._answer("PATCH")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.web = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.api = f"{self.web}/api/v3"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture()
def ghes(tmp_path, monkeypatch):
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git is not installed")
    for name in ("BOSS_GITHUB_API_URL", "BOSS_GITHUB_WEB_URL", "HTTPS_PROXY", "HTTP_PROXY",
                 "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    server = _Ghes(tmp_path)
    server.remotes.ensure(OWNER, REPO)          # a seeded repository with a main branch
    yield server
    server.close()


def _settings():
    return SimpleNamespace(target_github_owner="", target_github_repo="", github_token="",
                           github_app_id=None, github_app_private_key=None,
                           github_app_installation_id=None, open_pr_as_draft=False)


def test_a_ghes_connection_is_used_for_every_step_of_the_journey(ghes, tmp_path, monkeypatch):
    from boss_engine.connectors.intake.contract import Cursor, IntakeConnection, RawRequest, StoredRaw
    from boss_engine.connectors.intake.github import adapter as intake
    from boss_engine.identity import github_urls
    from boss_engine.services import setup_checks
    from boss_engine.services.ci_gate import HttpCiReader
    from boss_engine.services.github_adapter import GitHubAdapter
    from boss_engine.services.repo_host import git_access

    assert github_urls.api_base() == github_urls.PUBLIC_API     # the install default is github.com
    assert github_urls.web_base_for(ghes.api) == ghes.web
    assert github_urls.graphql_url(ghes.api) == f"{ghes.web}/api/graphql"

    # 1. CONNECT: the token and the repository, checked on GHES.
    probed = setup_checks.probe_repository(TOKEN, FULL, api_base=ghes.api)
    assert (probed.get("checks") or {}).get("read", {}).get("ok"), probed

    # 2. CLONE from the GHES host, the credential in git's environment (never the URL).
    access = git_access("github", OWNER, REPO, TOKEN, web_base=github_urls.web_base_for(ghes.api))
    assert access.url == f"{ghes.web}/{FULL}.git" and TOKEN not in access.url
    assert any(v.startswith(f"http.{ghes.web}/.") for v in access.env.values())
    work = tmp_path / "clone"
    done = subprocess.run(["git", "clone", "-q", access.url, str(work)], env=access.environ(),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert any(work.iterdir())

    # 3. PULL REQUEST through GHES's REST API.
    monkeypatch.setattr("boss_engine.services.run_secret_redaction.scrub", lambda text: text,
                        raising=False)
    gh = GitHubAdapter(_settings(), owner=OWNER, repo=REPO, token=TOKEN, api_base_url=ghes.api)
    assert gh.clone_url("") == f"{ghes.web}/{FULL}.git"
    gh.ensure_branch("boss/onboard-tenant", "main")
    gh.upsert_file("boss/onboard-tenant", "tenants/lumen-pay.yaml", "name: lumen-pay\n",
                   "Onboard the Lumen Pay tenant")
    pr = gh.create_pull_request("boss/onboard-tenant", "Onboard Lumen Pay", "From BOSS",
                                draft=False)
    number = int(getattr(pr, "number", 0) or 0)
    assert number > 0

    # 4. CHECKS: the head's check runs, read from GHES.
    head = gh.ensure_branch("boss/onboard-tenant")
    ghes.world.checks = {head: [{"name": "gateway-lint", "conclusion": "success"}]}
    reader = HttpCiReader(ghes.api, FULL, TOKEN)
    assert [r["name"] for r in reader.check_runs(head)] == ["gateway-lint"]

    # 5. WEBHOOK from GHES: signed, verified, parsed into an event for this repository.
    conn = IntakeConnection("icn_ghes", "p_1", "github", mode="push",
                            config={"repository": FULL, "api_base_url": ghes.api},
                            secret_resolver={"webhook_secret": "hook-secret",
                                             "api_token": TOKEN}.get)
    adapter = intake.GitHubIntakeAdapter()
    payload = {"action": "opened", "number": number,
               "pull_request": {"number": number, "title": "Onboard Lumen Pay",
                                "head": {"sha": head, "ref": "boss/onboard-tenant"},
                                "base": {"ref": "main"}, "user": {"login": "dev"},
                                "html_url": github_urls.pull_request(FULL, number, ghes.api)},
               "repository": {"full_name": FULL}, "sender": {"login": "dev"}}
    body = json.dumps(payload).encode()
    headers = {"x-github-event": "pull_request", "x-github-delivery": "ghes-1",
               "x-github-enterprise-host": "127.0.0.1",
               "x-hub-signature-256": intake.github_signature("hook-secret", body)}
    req = RawRequest(method="POST", headers=headers, body=body)
    adapter.verify(conn, req, 0.0)
    [event] = adapter.parse(conn, StoredRaw(body=body, headers=headers,
                                            dedupe_key=adapter.dedupe_key(conn, req)))
    assert FULL in json.dumps(getattr(event, "data", {}) or {}, default=str) or \
        FULL in str(event)

    # 6. POLLER (an install GHES cannot reach): the open PR, read from GHES.
    def transport(method, url, headers, body, purpose):
        import httpx

        assert url.startswith(ghes.api), url
        r = httpx.request(method, url, headers=dict(headers), content=body, timeout=20)
        return r.status_code, (r.json() if r.content else None), dict(r.headers)

    monkeypatch.setattr(intake, "TRANSPORT", transport)
    poll_conn = IntakeConnection("icn_ghes_poll", "p_1", "github", mode="poll",
                                 config={"repository": FULL, "api_base_url": ghes.api},
                                 secret_resolver={"api_token": TOKEN}.get)
    baseline = adapter.poll(poll_conn, Cursor(stream_key="s"), 50)
    gh.upsert_file("boss/onboard-tenant", "tenants/lumen-pay.yaml", "name: lumen-pay\nplan: gold\n",
                   "Lumen Pay: gold plan")
    moved = adapter.poll(poll_conn, baseline.next_cursor, 50)
    kinds = {(json.loads(d.body).get("action"), json.loads(d.body).get("number")) for d in moved.deliveries}
    assert ("synchronize", number) in kinds, kinds          # the PR's new head, seen by polling

    # NOTHING went anywhere but GHES, and every API call carried the credential.
    api_calls = [r for r in ghes.requests if r["path"].startswith("/api/")]
    assert api_calls and all(r["auth"] for r in api_calls)
    paths = {r["path"] for r in api_calls}
    assert f"/api/v3/repos/{FULL}/pulls" in paths
    assert f"/api/v3/repos/{FULL}/commits/{head}/check-runs" in paths
