"""BOSSAPP-1047 Phase B2 over HTTP, against the fake Jira: "Connect with Atlassian" (start,
the public state-bound callback, disconnect, /v1/me/ticket-identities with email_matches), the
public ticket webhook (verified by the connector), and the write-back timeline and retry with
their permissions. The services themselves are covered in
engine/tests/ticket_connectors/test_ticket_sync.py."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text

os.environ.setdefault("BOSS_MASTER_KEY",
                      base64.b64encode(b"p29a-test-master-key-32-bytes!!!").decode())

from apps.api.tests.identity_fixtures import IdentityWorld  # noqa: E402
from engine.tests.ticket_connectors import dataset as D  # noqa: E402
from engine.tests.ticket_connectors.fake_jira import (  # noqa: E402
    CLIENT_ID,
    CLIENT_SECRET,
    EMAIL,
    TOKEN,
    FakeJira,
)

SECRET = "ticket-webhook-secret-api-test"
STORY = f"KAN-{D.STORY}"


@pytest.fixture(autouse=True)
def _hook_off(monkeypatch):
    monkeypatch.setenv("BOSS_TICKET_SYNC", "off")


@pytest.fixture()
def fake(monkeypatch):
    from boss_engine.connectors.tickets import http as ticket_http
    from boss_engine.identity import vault

    vault.reset_master_key_cache()
    server = FakeJira()
    real = ticket_http._urllib_transport

    def route(method, url, headers, data, timeout, context):
        for host in ("https://auth.atlassian.com", "https://api.atlassian.com"):
            if url.startswith(host):
                url = server.base + url[len(host):]
        return real(method, url, headers, data, timeout, context)

    monkeypatch.setattr(ticket_http, "TRANSPORT", route)
    monkeypatch.setattr(ticket_http, "SLEEP", lambda s: None)
    monkeypatch.setenv("BOSS_PUBLIC_URL", "https://boss.example.test")
    monkeypatch.setenv("BOSS_JIRA_WEBHOOK_SECRET", SECRET)
    yield server
    server.close()


@pytest.fixture()
def world(engine, fake):
    world = IdentityWorld.create(engine)
    world.operator = world.person("ada")
    world.viewer = world.person("vic")
    world.bind(world.operator, "operator", project=world.project_a)
    world.bind(world.viewer, "viewer", project=world.project_a)
    world.conn_id = f"trk_{uuid.uuid4().hex[:12]}"
    config = {"base_url": fake.base, "email": EMAIL, "project_key": "KAN", "api_token": TOKEN,
              "oauth_client_id": CLIENT_ID, "oauth_client_secret": CLIENT_SECRET, "settings": {}}
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO work_tracker_providers (id, project_id, provider_key, display_name, "
            "status, is_default, configuration, created_by) VALUES (:id, :p, 'jira', 'Jira', "
            "'active', true, CAST(:c AS JSONB), :u)"),
            {"id": world.conn_id, "p": world.project_a, "c": json.dumps(config),
             "u": world.operator.id})
        c.execute(text("INSERT INTO identity_users (principal_id, email, email_verified_at) "
                       "VALUES (:p, :e, :v)"),
                  {"p": world.operator.id, "e": f"ada+{uuid.uuid4().hex[:8]}@example.com",
                   "v": datetime.now(timezone.utc)})
    return world


def test_connect_with_atlassian_start_callback_identities_and_disconnect(client, fake, world):
    start = f"/v1/ticket-systems/{world.conn_id}/oauth/start"
    with world.acting_as(world.ctx(world.viewer)):
        # connection:use_own (a denial on a named object answers 403 or 404)
        assert client.post(start + "/load").status_code in (403, 404)
    with world.acting_as(world.ctx(world.operator)):
        redirect = client.get(start, follow_redirects=False,
                              headers={"Sec-Fetch-Site": "same-origin"})
        assert redirect.status_code == 302, redirect.text
        assert redirect.headers["location"].startswith("https://auth.atlassian.com/authorize?")
        started = client.post(start + "/load")
        assert started.status_code == 200, started.text
    url = started.json()["authorize_url"]
    query = parse_qs(urlsplit(url).query)
    assert query["redirect_uri"] == ["https://boss.example.test/v1/ticket-systems/oauth/callback"]
    assert CLIENT_SECRET not in url
    code, state = fake.approve(url)
    # the provider's redirect: no BOSS credential, the signed state is what authenticates it
    with world.acting_as(None, mode="shadow"):
        done = client.get("/v1/ticket-systems/oauth/callback",
                          params={"state": state, "code": code})
        assert done.status_code == 200, done.text
        assert "please confirm" in done.text        # ada@example.com is not her BOSS email
        replay = client.get("/v1/ticket-systems/oauth/callback",
                            params={"state": state, "code": code})
        assert replay.status_code == 400 and "already used" in replay.text
        forged = client.get("/v1/ticket-systems/oauth/callback",
                            params={"state": "made-up", "code": "x"})
        assert forged.status_code == 400 and "not one BOSS started" in forged.text
    with world.acting_as(world.ctx(world.operator)):
        listed = client.get("/v1/me/ticket-identities")
        assert listed.status_code == 200, listed.text
        mine = listed.json()["identities"]
        assert [(i["provider"], i["connection_id"], i["account"], i["email_matches"])
                for i in mine] == [("jira", world.conn_id, fake.me_email, False)]
        assert not any(token in listed.text for token in fake.access)
        gone = client.delete(f"/v1/ticket-systems/{world.conn_id}/oauth/me")
        assert gone.status_code == 200 and gone.json()["disconnected"] is True
        assert client.get("/v1/me/ticket-identities").json()["identities"] == []


def test_the_callback_says_connected_when_the_email_is_yours(client, fake, world, engine):
    with engine.begin() as c:
        fake.me_email = c.execute(text("SELECT email FROM identity_users WHERE principal_id = :p"),
                                  {"p": world.operator.id}).scalar()
    with world.acting_as(world.ctx(world.operator)):
        url = client.post(f"/v1/ticket-systems/{world.conn_id}/oauth/start/load").json()[
            "authorize_url"]
    code, state = fake.approve(url)
    with world.acting_as(None, mode="shadow"):
        done = client.get("/v1/ticket-systems/oauth/callback",
                          params={"state": state, "code": code})
    assert done.status_code == 200 and "<h1>Connected</h1>" in done.text
    with world.acting_as(world.ctx(world.operator)):
        assert client.get("/v1/me/ticket-identities").json()["identities"][0]["email_matches"]


def _delivery(key=STORY):
    return json.dumps({"webhookEvent": "jira:issue_created",
                       "issue": {"id": str(10000 + D.STORY), "key": key}}).encode()


def test_the_ticket_webhook_is_public_and_verified_by_its_connector(client, fake, world):
    body = _delivery()
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    path = f"/v1/webhooks/tickets/{world.conn_id}"
    with world.acting_as(None, mode="shadow"):
        bad = client.post(path, content=body, headers={"X-Hub-Signature": "sha256=00"})
        assert bad.status_code == 401
        assert client.post("/v1/webhooks/tickets/trk_nope", content=body,
                           headers={"X-Hub-Signature": good}).status_code == 404
        # a Jira webhook registered without a secret: ?token= on its URL
        token = client.post(path + f"?token={SECRET}", content=body)
        assert token.status_code == 202 and token.json()["status"] == "waiting", token.text
        again = client.post(path, content=body, headers={"X-Hub-Signature": good})
        assert again.status_code == 200 and again.json()["status"] == "already_asked"


def test_the_timeline_reads_with_work_item_read_and_retry_needs_work_tracker_write(
        client, fake, world, engine):
    from boss_engine.services import ticket_sync

    class _S:
        boss_edition = "community"

    with engine.begin() as c:
        item = ticket_sync.ticket_import_items(
            c, _S(), project_id=world.project_a, items=[{"key": STORY}],
            actor=world.operator.id, connection_id=world.conn_id)["created"][0]["work_item_id"]
    path = f"/v1/work-items/{item}/ticket-sync"
    with world.acting_as(world.ctx(world.viewer)):
        seen = client.get(path)
        assert seen.status_code == 200, seen.text
        assert seen.json()["ticket"]["key"] == STORY and seen.json()["entries"] == []
        assert client.post(path + "/retry", json={}).status_code in (403, 404)
    with world.acting_as(world.ctx(world.operator)):
        retried = client.post(path + "/retry", json={})
        assert retried.status_code == 200, retried.text
        assert retried.json()["retried"] == [] and retried.json()["timeline"]["ticket"]
