"""R2 #10: network mode on a compose install, where BOSS_REQUIRE_API_AUTH is always true.

The legacy transport middleware refused every credential-less request (401) before identity ran,
so the network bridge (BOSS_CONSOLE_AUTH=network: a private-network visitor IS usr_network_operator)
was dead on the one install shape that ships it. Now such a request goes through identity, which
maps it; a request from a public address, or carrying any credential, is not the bridge.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import main

PRIVATE = ("10.20.30.40", 41000)
PUBLIC = ("8.8.8.8", 41000)   # (TEST-NET ranges count as private to ipaddress)


@pytest.fixture
def network_mode(monkeypatch):
    from boss_engine.config import get_settings
    from boss_engine.identity import legacy, migration

    for settings in {id(s): s for s in (main.settings, get_settings())}.values():
        monkeypatch.setattr(settings, "require_api_auth", True)
        monkeypatch.setattr(settings, "legacy_console_auth", "network")
        monkeypatch.setattr(settings, "api_keys", "r2-network-mode-some-real-key-0000000000")
        monkeypatch.setattr(settings, "api_key_principals", "r2-network-mode-some-real-key-0000000000:ceo")
        monkeypatch.setattr(settings, "authz_mode", "enforce")
        monkeypatch.setattr(settings, "authz_router_modes", "")
    legacy.invalidate_cache()
    migration.forget_seeded()
    yield
    legacy.invalidate_cache()
    migration.forget_seeded()


def test_a_private_visitor_is_the_network_operator_under_require_api_auth(engine, network_mode):
    from boss_engine.identity import legacy

    got = TestClient(main.app, client=PRIVATE).get("/v1/auth/session")
    assert got.status_code == 200, got.text
    assert (got.json().get("principal") or {}).get("id") == legacy.NETWORK_OPERATOR_PRINCIPAL, got.json()


def test_a_public_visitor_is_not(engine, network_mode):
    got = TestClient(main.app, client=PUBLIC).get("/v1/auth/session")
    assert got.status_code == 401, got.text


def test_a_wrong_credential_is_never_upgraded_to_the_bridge(engine, network_mode):
    got = TestClient(main.app, client=PRIVATE).get(
        "/v1/auth/session", headers={"Authorization": "Bearer not-a-real-key"})
    assert got.status_code == 401, got.text


def test_a_setup_link_is_still_offered_where_a_shared_password_is_set(engine, monkeypatch):
    """Compose always sets BOSS_CONSOLE_PASSWORD, so the claim page took `legacy_password` and
    ignored the setup link it was opened with. /v1/auth/options lists every path (and the live
    link's expiry), so a page holding `#t=` claims with it."""
    from boss_engine.config import get_settings

    from apps.api.tests.identity_s1_world import fresh_client

    for settings in {id(s): s for s in (main.settings, get_settings())}.values():
        monkeypatch.setattr(settings, "legacy_console_password", "the-compose-shared-password")
    if not main.announce_claim_link_once():
        pytest.skip("this database's org is already claimed")
    claim = fresh_client().get("/v1/auth/options").json()["claim"]
    assert claim["path"] == "legacy_password"
    assert {"setup_token", "legacy_password"} <= set(claim["paths"])
    assert claim["expires_at"]


def test_the_claim_origin_is_the_browsers_never_the_host_a_server_fetch_used(engine, network_mode):
    from boss_engine.identity import claim as claim_module

    with engine.connect() as db:
        if claim_module.is_claimed(db):
            pytest.skip("this database's org is already claimed")
    api = TestClient(main.app, client=PRIVATE, base_url="http://api:8000")
    served = api.get("/v1/auth/options").json()["claim"]          # a server-side fetch: no Origin
    assert served["path"] == "network_bridge" and served["origin"] is None
    browser = api.get("/v1/auth/options", headers={"Origin": "https://boss.example.test"}).json()["claim"]
    assert browser["origin"] == "https://boss.example.test"
