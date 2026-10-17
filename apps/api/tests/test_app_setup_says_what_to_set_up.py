"""App › Connectors (BOSSAPP-568): what the redesigned Setup reads, and the Apps row's chip.

1. THE PREVIEW'S ADDRESS IS BOSS'S PUBLIC ONE, never this request's Host. Behind the console the
   Host is BOSS's own container (api:3001), and the setup guide printed
   http://api:3001/v1/previews/… as the preview URL for a person to register at Google. Now the
   preview's address comes from BOSS's public origin (BOSS_PUBLIC_URL, else the claim's); with
   none, the guide says the preview is reachable inside BOSS only, and the response says so
   (`preview.reach`), with BOSS's public address (`boss_public_url`) beside it.
2. EACH CONNECTOR SAYS WHICH SECRETS ARE ITS OWN and which sign-in providers it wires, so the
   console draws one card per connector and a row per provider.
3. THE APPS ROW KNOWS WHAT NEEDS THE OPERATOR (`setup`): providers with no client id stored for
   preview or deploy, and required keys with no value — the same rule the console's card uses
   (apps/web/lib/app-connectors.ts).
"""
from __future__ import annotations

import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import authn_support, vault
from boss_engine.templates import apply as apply_service
from boss_engine.templates import secrets as app_secrets
from boss_engine.templates import setup_needs
from boss_engine.templates.service import get_row, manifest_of, sync_builtins


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def operator(world):
    p = world.person("setup-operator")
    world.bind(p, "operator")
    return p


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"a" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    yield
    vault.reset_master_key_cache()


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _key() -> str:
    return f"setupapp{uuid.uuid4().hex[:8]}"


def _apply(engine, world, app_key: str, template_key: str, status: str = "applied") -> dict:
    with engine.begin() as db:
        sync_builtins(db, world.org)
        row = get_row(db, world.org, template_key)
        db.execute(text("INSERT INTO app_template_applications (id, project_id, app_key, template_id, version, "
                        "stack, status) VALUES (:i, :p, :a, :t, :v, 'node', :s)"),
                   {"i": f"tapp_{uuid.uuid4().hex[:10]}", "p": world.project_a, "a": app_key, "t": row["id"],
                    "v": row["version"], "s": status})
        app_secrets.ensure_test_defaults(db, org_id=world.org, project_id=world.project_a, app_key=app_key,
                                         manifest=manifest_of(row), actor_id="usr_test")
    return dict(row)


def _guide(client, world, operator, key: str, **headers) -> dict:
    with world.acting_as(world.ctx(operator)):
        r = client.get(f"/v1/apps/{key}/setup-guide", params={"project_id": world.project_a}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---- 1. the preview's address ---------------------------------------------------------------------

def test_with_no_public_address_the_preview_is_said_to_be_inside_boss_only(engine, world, operator, client, monkeypatch):
    key = _key()
    _apply(engine, world, key, "login-oauth")
    monkeypatch.setattr(apply_service, "preview_for", lambda *_a, **_k: "prv_inside")
    monkeypatch.setattr(authn_support, "public_origin", lambda *_a, **_k: None)
    monkeypatch.delenv("BOSS_PREVIEW_ORIGIN", raising=False)
    monkeypatch.delenv("BOSS_PREVIEW_PUBLIC_BASE", raising=False)
    body = _guide(client, world, operator, key, host="api:3001")
    assert body["boss_public_url"] is None
    assert body["preview"] == {"running": True, "url": None, "reach": "inside_only"}
    copy = body["guides"][0]["copy"]
    assert copy["preview_url"] == "(the preview is reachable inside BOSS only: set BOSS's public address)"
    assert copy["public_url"].startswith("(BOSS has no public address yet")
    assert "api:3001" not in json.dumps(body)


def test_the_preview_address_is_on_boss_public_origin_not_the_request_host(engine, world, operator, client, monkeypatch):
    key = _key()
    _apply(engine, world, key, "login-oauth")
    monkeypatch.setattr(apply_service, "preview_for", lambda *_a, **_k: "prv_pub")
    monkeypatch.setattr(authn_support, "public_origin", lambda *_a, **_k: "https://boss.example.test")
    monkeypatch.delenv("BOSS_PREVIEW_ORIGIN", raising=False)
    monkeypatch.setenv("BOSS_PREVIEW_PORT", "8443")
    body = _guide(client, world, operator, key, host="api:3001")
    assert body["boss_public_url"] == "https://boss.example.test"
    assert body["preview"] == {"running": True, "url": "https://boss.example.test:8443/v1/previews/prv_pub/web",
                               "reach": "public"}
    g = body["guides"][0]
    assert g["copy"]["redirect_uri"] == "https://boss.example.test:8443/v1/previews/prv_pub/web/auth/callback"
    assert "api:3001" not in json.dumps(body)


def test_no_preview_running_says_so(engine, world, operator, client, monkeypatch):
    key = _key()
    _apply(engine, world, key, "login-oauth")
    monkeypatch.setattr(apply_service, "preview_for", lambda *_a, **_k: None)
    body = _guide(client, world, operator, key)
    assert body["preview"] == {"running": False, "url": None, "reach": None}
    assert body["guides"][0]["copy"]["preview_url"] == "(start a preview to get its URL)"


# ---- 2. each connector's own secrets and providers --------------------------------------------------

def test_each_attempt_names_its_secrets_and_sign_in_providers(engine, world, operator, client):
    key = _key()
    _apply(engine, world, key, "login-oauth", status="failed")
    _apply(engine, world, key, "login-oauth", status="blocked")
    _apply(engine, world, key, "stripe-checkout", status="pr_open")
    with world.acting_as(world.ctx(operator)):
        rows = client.get(f"/v1/apps/{key}/templates", params={"project_id": world.project_a}).json()["templates"]
    login = [r for r in rows if r["template_key"] == "login-oauth"]
    assert len(login) == 2                       # every attempt is listed; the console folds them
    assert [p["provider"] for p in login[0]["sign_in_providers"]] == ["google", "github", "boss"]
    assert login[0]["sign_in_providers"][2] == {"provider": "boss", "id": "OIDC_BOSS_CLIENT_ID",
                                                "secret": "OIDC_BOSS_CLIENT_SECRET"}
    assert "SESSION_SECRET" in login[0]["secrets"]
    stripe = next(r for r in rows if r["template_key"] == "stripe-checkout")
    assert stripe["sign_in_providers"] == []    # a payment API's keys are not a sign-in provider
    assert "STRIPE_SECRET_KEY" in stripe["secrets"]
    assert "manifest" not in stripe
    with world.acting_as(world.ctx(operator)):
        declared = client.get(f"/v1/apps/{key}/secrets", params={"project_id": world.project_a}).json()["declared"]
    assert next(d for d in declared if d["name"] == "GOOGLE_CLIENT_ID")["required"] is False
    assert next(d for d in declared if d["name"] == "STRIPE_SECRET_KEY")["required"] is True


# ---- 3. what needs the operator ---------------------------------------------------------------------

def test_providers_and_keys_need_the_operator_until_a_value_is_stored(engine, world):
    key = _key()
    _apply(engine, world, key, "login-oauth", status="blocked")
    _apply(engine, world, key, "stripe-checkout", status="pr_open")
    with engine.begin() as db:
        need = setup_needs.needs(db, world.project_a)[key]
    assert need == {"providers": ["google", "github", "boss"], "keys": ["STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY"]}
    with engine.begin() as db:
        specs = {s.name: s for t in ("login-oauth", "stripe-checkout")
                 for s in manifest_of(get_row(db, world.org, t)).secrets}
        for name, value in (("GOOGLE_CLIENT_ID", "123-abc.apps.googleusercontent.com"),
                            ("STRIPE_SECRET_KEY", "sk_test_abc123"), ("STRIPE_PUBLISHABLE_KEY", "pk_test_abc123")):
            app_secrets.put(db, org_id=world.org, project_id=world.project_a, app_key=key, name=name, env="preview",
                            mode="test", value=value, actor_id="usr_test", spec=specs[name], may_live=False)
        need = setup_needs.needs(db, world.project_a)[key]
    assert need == {"providers": ["github", "boss"], "keys": []}


def test_a_connector_whose_every_attempt_failed_needs_nothing(engine, world):
    key = _key()
    _apply(engine, world, key, "login-oauth", status="failed")
    with engine.begin() as db:
        assert key not in setup_needs.needs(db, world.project_a)


def test_the_apps_row_carries_the_setup_chip(engine, world, operator, client):
    title = f"Setup Desk {uuid.uuid4().hex[:6]}"
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx) "
            "VALUES (:id, :p, 'completed', 'done', :t, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)"),
            {"id": run_id, "p": world.project_a, "t": title})
    with world.acting_as(world.ctx(operator)):
        row = next(a for a in client.get("/v1/factory/apps", params={"project_id": world.project_a}).json()["apps"]
                   if a["run_id"] == run_id)
    assert row["setup"] is None                  # nothing added: no chip
    _apply(engine, world, row["app_key"], "login-oauth", status="applied")
    with world.acting_as(world.ctx(operator)):
        row = next(a for a in client.get("/v1/factory/apps", params={"project_id": world.project_a}).json()["apps"]
                   if a["run_id"] == run_id)
    assert row["setup"] == {"providers": ["google", "github", "boss"], "keys": []}
