"""BOSSAPP-1160: the connection's health says whether it can CREATE repositories, and the
new-repo routes say which credential will create, per owner.

  * Test all (setup_actions.check_repository) reads the token's scopes from GitHub's own header
    and records the flow permissions (contents, pull_requests, administration) beside the
    repository ones; Setup's account row says "Can create repos: no (missing:
    administration)" instead of green.
  * GET /v1/projects/{id}/github-repository/owners lists each owner with its creating credential
    or its exact fix; /check says the resolver's precise words when none can.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import repo_creation, setup_accounts, setup_actions, setup_view
from boss_engine.services.setup_checks import HttpAnswer, now_utc


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("rch-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def quiet_app(monkeypatch):
    """No Setup App and no environment App in this test's world: the connections answer alone."""
    monkeypatch.setattr(repo_creation, "_identity_app_candidates", lambda db, owner: [])
    monkeypatch.setattr(repo_creation, "_env_app_candidates", lambda settings: [])
    monkeypatch.setattr(repo_creation, "_install_candidates", lambda settings: [])


def _connection(engine, world, login: str, *, env_name: str, label: str, role: str = "source") -> tuple[str, str]:
    """A personal-access-token connection for `login` with a usable env secret, and one binding."""
    cid = f"ghconn_{uuid.uuid4().hex[:10]}"
    bid = f"ghb_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key, secret_kind, "
            "storage_backend, status, created_by) VALUES (:id, :p, 'project', :p, 'env', :k, 'api_token', "
            "'environment', 'active', 'test')"), {"id": f"sref_{uuid.uuid4().hex[:10]}", "p": world.project_a, "k": env_name})
        db.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, credential_ref, status, "
            "account_login, created_by) VALUES (:i, :p, :l, 'personal_access_token', :r, 'active', :a, 'test')"),
            {"i": cid, "p": world.project_a, "l": label, "r": f"env:{env_name}", "a": login})
        db.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, full_name, role, "
            "status, host, created_by) VALUES (:i, :p, :c, :o, 'shop', :f, :r, 'active', 'github', 'test')"),
            {"i": bid, "p": world.project_a, "c": cid, "o": login, "f": f"{login}/shop", "r": role})
    return cid, bid


def _github(scopes_by_token: dict[str, str]):
    """A fake GitHub for the three repository probes and GET /user (with the token's scopes)."""
    def http(method, url, *, headers, json_body=None, timeout=15.0):
        token = str(headers.get("Authorization", "")).split(" ", 1)[-1]
        if url.endswith("/user"):
            if token not in scopes_by_token:
                return HttpAnswer(401, {}, {"message": "Bad credentials"})
            return HttpAnswer(200, {"x-oauth-scopes": scopes_by_token[token]}, {"login": "someone"})
        if method == "GET" and "/repos/" in url:
            return HttpAnswer(200, {}, {"full_name": url.split("/repos/", 1)[1], "default_branch": "main", "private": True})
        if url.endswith("/git/blobs"):
            return HttpAnswer(201, {}, {"sha": "abc"})
        if url.endswith("/pulls"):
            return HttpAnswer(422, {}, {"message": "Validation Failed"})
        return HttpAnswer(404, {}, {})
    return http


def test_test_all_records_whether_the_token_can_create_repos(engine, world, monkeypatch, quiet_app):
    monkeypatch.setenv("BOSS_TEST_GH_WEAK", "ghp_weak_token_1160")
    monkeypatch.setenv("BOSS_TEST_GH_FULL", "ghp_full_token_1160")
    weak_cid, weak_bid = _connection(engine, world, "example-org-b", env_name="BOSS_TEST_GH_WEAK", label="weak")
    full_cid, full_bid = _connection(engine, world, "paramg", env_name="BOSS_TEST_GH_FULL", label="full")
    http = _github({"ghp_weak_token_1160": "read:user, workflow", "ghp_full_token_1160": "repo, read:org"})
    settings = get_settings()
    with engine.begin() as db:
        weak = setup_actions.check_repository(db, settings, world.project_a, weak_bid, http=http)
        full = setup_actions.check_repository(db, settings, world.project_a, full_bid, http=http)
    assert weak["state"] == "working" and full["state"] == "working", "the repository probes are unchanged"
    assert weak["flows"]["can_create"] is False and full["flows"]["can_create"] is True
    with engine.connect() as db:
        rows = {r["id"]: dict(r) for r in db.execute(text(
            "SELECT id, status, granted_permissions, missing_permissions FROM github_connections WHERE id IN (:a, :b)"),
            {"a": weak_cid, "b": full_cid}).mappings()}
    assert rows[weak_cid]["status"] == "active", "builds still work: a missing create permission never fails the test"
    assert "administration" in rows[weak_cid]["missing_permissions"]
    assert set(rows[full_cid]["granted_permissions"]) >= {"administration", "contents", "pull_requests"}
    assert rows[full_cid]["missing_permissions"] == []

    # Setup's account rows say it (setup_accounts), and the connection API carries it.
    with engine.begin() as db:
        now = now_utc()
        repository = setup_view._repository(db, settings, world.project_a, now)  # noqa: SLF001 — the view's own section
        view = setup_accounts.build(db, settings, world.project_a, repository, now)
        from boss_engine.services.github_connections import GitHubConnectionService

        listed = {c["id"]: c for c in GitHubConnectionService(settings).list_connections(db, world.project_a)}
    by = {a["connection_id"]: a for a in view["accounts"]}
    assert by[weak_cid]["can_create_repos"]["ok"] is False
    assert "Can create repos: no (missing: administration)" in by[weak_cid]["can_create_repos"]["words"]
    assert by[full_cid]["can_create_repos"]["ok"] is True and "yes" in by[full_cid]["can_create_repos"]["words"]
    assert listed[weak_cid]["can_create_repos"] == {"ok": False, "missing": ["administration"]}
    assert listed[full_cid]["can_create_repos"] == {"ok": True, "missing": []}

    # The owner picker: paramg can (via its token), example-org-b cannot (with its fix).
    with world.acting_as(world.ctx(world.owner)):
        r = client_get(world, f"/v1/projects/{world.project_a}/github-repository/owners?owners=paramg,someone-else")
    assert r.status_code == 200, r.text
    owners = {o["login"]: o for o in r.json()["owners"]}
    assert owners["paramg"]["can_create"] and owners["paramg"]["verified"]
    # this project's own token, said as such (BOSSAPP-1338)
    assert owners["paramg"]["via"] == "paramg (access token, this project)"
    assert not owners["example-org-b"]["can_create"]
    assert owners["example-org-b"]["fixes"] == ["Replace the token for example-org-b with one holding the repo scope (Setup)"]
    assert not owners["someone-else"]["can_create"] and "no GitHub credential" in owners["someone-else"]["words"]

    # The check says, in the resolver's words, why example-org-b cannot — never the generic line.
    with world.acting_as(world.ctx(world.owner)):
        r = client_get(world, f"/v1/projects/{world.project_a}/github-repository/check?owner=example-org-b&repo=shop")
    assert r.status_code == 200, r.text
    said = r.json()
    assert said["reason"] == "no_github_credential" and "missing: administration" in said["words"]
    assert "example-org-b (access token" in said["words"] and "Administration" in said["words"]


def client_get(world, path: str):
    return TestClient(main.app, raise_server_exceptions=False).get(path)


def test_the_connection_test_records_the_flow_permissions_too(engine, world, monkeypatch, quiet_app):
    """The other health path (POST /v1/github/connections/{id}/test → test_connection) records
    the same flow permissions, from the connection's OWN token."""
    from boss_engine.services import setup_checks
    from boss_engine.services.github_connections import GitHubConnectionService

    monkeypatch.setenv("BOSS_TEST_GH_CT", "ghp_ct_token_1160")
    cid, bid = _connection(engine, world, "paramg-ct", env_name="BOSS_TEST_GH_CT", label="ct")
    monkeypatch.setattr(setup_checks, "default_http", _github({"ghp_ct_token_1160": "read:user"}))

    class Adapter:
        def __init__(self, settings, *, owner, repo, token=None, api_base_url=None):
            self.full = f"{owner}/{repo}"

        def repo_metadata(self):
            return {"full_name": self.full, "id": 7, "default_branch": "main", "visibility": "private",
                    "permissions": {"pull": True, "push": True, "admin": False}, "owner": {"login": "paramg-ct"}}

    service = GitHubConnectionService(get_settings(), adapter_factory=Adapter)
    with engine.begin() as db:
        out = service.test_connection(db, project_id=world.project_a, connection_id=cid, binding_id=bid,
                                      dedupe_key=f"t-{uuid.uuid4().hex[:8]}", actor="test")
    assert out["status"] == "passed", out
    assert out["detail"]["can_create_repos"] is False
    assert "administration" in out["detail"]["missing_permissions"] and "push" in out["detail"]["granted_permissions"]
    assert out["connection"]["can_create_repos"] == {"ok": False, "missing": ["administration"]}


def test_a_ghes_connection_never_creates_or_checks_a_github_com_repository(engine, world, monkeypatch,
                                                                          quiet_app):
    """BOSSAPP-1286 (fourth review, S2): a GHES connection whose account has the same login as
    the owner is NOT a creating credential for github.com, and the check route never sends its
    token anywhere — it says no credential for this host."""
    import httpx

    monkeypatch.setenv("BOSS_TEST_GH_GHES", "ghes_token_never_to_github_com")
    cid, _bid = _connection(engine, world, "ghes-only-owner", env_name="BOSS_TEST_GH_GHES",
                            label="ghes")
    with engine.begin() as db:
        db.execute(text("UPDATE github_connections SET api_base_url = :b WHERE id = :i"),
                   {"b": "https://ghe.northgate.example/api/v3", "i": cid})
    sent: list[tuple[str, str]] = []

    def handle(request):
        sent.append((str(request.url), request.headers.get("Authorization", "")))
        return httpx.Response(404, json={})

    from boss_engine.services import local_publish

    real_check = local_publish.check_repository
    monkeypatch.setattr(local_publish, "check_repository", lambda **k: real_check(
        **k, client=httpx.Client(transport=httpx.MockTransport(handle))))
    with world.acting_as(world.ctx(world.owner)):
        r = client_get(world, f"/v1/projects/{world.project_a}/github-repository/check"
                              "?owner=ghes-only-owner&repo=new-app")
        owners = client_get(world, f"/v1/projects/{world.project_a}/github-repository/owners"
                                   "?owners=ghes-only-owner")
    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "no_github_credential", r.json()
    assert not any("ghes_token_never_to_github_com" in auth for _u, auth in sent), sent
    assert owners.status_code == 200, owners.text
    row = {o["login"]: o for o in owners.json()["owners"]}["ghes-only-owner"]
    assert not row["can_create"]
