"""Confluence connection over HTTP (BOSSAPP-1384): connect (test first, the token to the vault and
never back out), re-test, change spaces, sync into knowledge, disconnect, and who may do which.

Recorded Confluence responses (engine/tests/docs_connectors/fixtures) replace the network; real
Postgres and the real permission guard. No live call, no model."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.connectors.tickets import http as ticket_http
from tests.docs_connectors.fake_confluence import EMAIL, SITE, TOKEN, FakeConfluence


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("dc-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def fake(monkeypatch):
    transport = FakeConfluence()
    monkeypatch.setattr(ticket_http, "TRANSPORT", transport)
    monkeypatch.setattr(ticket_http, "SLEEP", lambda s: None)
    return transport


@pytest.fixture(autouse=True)
def _clean(engine):
    yield
    with engine.begin() as db:
        db.execute(text("DELETE FROM docs_connections"))


def _connect(client, project_id, *, token=TOKEN, **extra):
    return client.post("/v1/docs-connections", json={
        "project_id": project_id, "base_url": SITE, "fields": {"email": EMAIL},
        "secrets": {"api_token": token}, **extra})


def test_catalog_lists_confluence_and_marks_data_center_unsupported(world, client):
    with world.acting_as(world.ctx(world.owner)):
        got = client.get("/v1/docs-connections/catalog")
    assert got.status_code == 200, got.text
    item = got.json()["items"][0]
    assert item["provider_key"] == "confluence" and item["deployments"]["datacenter"]["implemented"] is False
    assert [f["name"] for f in item["auth_fields"]] == ["base_url", "email", "api_token"]


def test_connect_tests_first_stores_by_reference_and_never_returns_the_token(world, client, fake, engine):
    with world.acting_as(world.ctx(world.owner)):
        made = _connect(client, world.project_a, default_spaces=["ENG"])
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["test"]["ok"] and body["test"]["account"] == "BOSS Bot"
    conn = body["connection"]
    assert conn["status"] == "connected" and conn["default_spaces"] == ["ENG"]
    assert conn["base_url"] == SITE and conn["last4"] == TOKEN[-4:] and conn["can_manage"] is True
    assert TOKEN not in made.text
    with engine.begin() as db:
        stored = db.execute(text("SELECT configuration FROM docs_connections")).scalar()
        refs = db.execute(text("SELECT count(*) FROM secret_refs WHERE project_id = :p"),
                          {"p": world.project_a}).scalar()
    assert stored["secret_ref_id"] and TOKEN not in json.dumps(stored) and refs >= 1
    # connect called the site with the right credential, and only that host
    assert {r["host"] for r in fake.requests} == {"acme.atlassian.net"}


def test_a_wrong_token_stores_nothing_and_says_how_to_fix_it(world, client, fake, engine):
    with world.acting_as(world.ctx(world.owner)):
        refused = _connect(client, world.project_a, token="a-wrong-token")
    assert refused.status_code == 424, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "auth_failed" and "Reconnect Confluence in Setup" in detail["message"]
    assert "a-wrong-token" not in refused.text
    with engine.begin() as db:
        assert db.execute(text("SELECT count(*) FROM docs_connections")).scalar() == 0


def test_bad_requests_are_refused_in_words(world, client, fake):
    with world.acting_as(world.ctx(world.owner)):
        dc = _connect(client, world.project_a, deployment="datacenter")
        host = client.post("/v1/docs-connections", json={
            "project_id": world.project_a, "base_url": "https://wiki.example.com",
            "fields": {"email": EMAIL}, "secrets": {"api_token": TOKEN}})
        extra = client.post("/v1/docs-connections", json={
            "project_id": world.project_a, "base_url": SITE, "fields": {"email": EMAIL, "x": "1"},
            "secrets": {"api_token": TOKEN}})
    assert dc.status_code == 422 and dc.json()["detail"]["error"] == "capability_not_supported"
    assert host.status_code == 422 and "Atlassian Cloud" in host.json()["detail"]["message"]
    assert extra.status_code == 422
    assert fake.requests == []


def test_the_connection_is_org_wide_test_settings_and_disconnect(world, client, fake, engine):
    with world.acting_as(world.ctx(world.owner)):
        assert client.get("/v1/docs-connections", params={"project_id": world.project_b}).json()["connections"] == []
        assert _connect(client, world.project_a).status_code == 201
        seen = client.get("/v1/docs-connections", params={"project_id": world.project_b}).json()["connections"]
        assert len(seen) == 1 and seen[0]["reach"] == "org" and seen[0]["home_project"]["id"] == world.project_a
        # re-test from the other project: manage at the home is required and the owner holds it
        tested = client.post("/v1/docs-connections/test", json={"project_id": world.project_b})
        assert tested.status_code == 200, tested.text
        assert tested.json()["test"]["ok"] and tested.json()["connection"]["last_test_ok"] is True
        spaces = client.get("/v1/docs-connections/spaces", params={"project_id": world.project_b})
        assert [s["key"] for s in spaces.json()["spaces"]] == ["ENG", "OPS"]
        changed = client.patch("/v1/docs-connections", json={
            "project_id": world.project_a, "default_spaces": ["ENG", "OPS"], "knowledge_spaces": ["ENG"]})
        assert changed.status_code == 200 and changed.json()["connection"]["knowledge_spaces"] == ["ENG"]
        assert client.patch("/v1/docs-connections", json={"project_id": world.project_a}).status_code == 422
        synced = client.post("/v1/docs-connections/sync", json={"project_id": world.project_b,
                                                                  "actor": "dc-owner"})
        assert synced.status_code == 200, synced.text
        assert synced.json()["spaces"][0]["indexed"] == 1
        gone = client.delete("/v1/docs-connections", params={"project_id": world.project_a})
        assert gone.status_code == 200 and gone.json()["connection"]["status"] == "disconnected"
        again = client.post("/v1/docs-connections/test", json={"project_id": world.project_a})
        assert again.status_code == 424 and "Connect Confluence in Setup" in again.json()["detail"]["message"]
        spaces = client.get("/v1/docs-connections/spaces", params={"project_id": world.project_a})
        assert spaces.status_code == 424
    with engine.begin() as db:
        cfg = db.execute(text("SELECT configuration FROM docs_connections")).scalar()
    assert "secret_ref_id" not in cfg and cfg["default_spaces"] == ["ENG", "OPS"]


def test_who_may_do_what(world, client, fake):
    with world.acting_as(world.ctx(world.owner)):
        assert _connect(client, world.project_a).status_code == 201
    viewer = world.person("dc-viewer")
    world.bind(viewer, "viewer", project=world.project_a)
    with world.acting_as(world.ctx(viewer)):
        read = client.get("/v1/docs-connections", params={"project_id": world.project_a})
        assert read.status_code == 200
        view = read.json()["connections"][0]
        assert view["can_manage"] is False and view["last4"] is None and view["fingerprint"] is None
        assert _connect(client, world.project_a).status_code == 403
        assert client.post("/v1/docs-connections/test", json={"project_id": world.project_a}).status_code == 403
        assert client.delete("/v1/docs-connections", params={"project_id": world.project_a}).status_code == 403
        assert client.post("/v1/docs-connections/sync", json={"project_id": world.project_a}).status_code == 403
    outsider = world.person("dc-nobody")
    with world.acting_as(world.ctx(outsider)):
        assert client.get("/v1/docs-connections", params={"project_id": world.project_a}).status_code in (403, 404)
