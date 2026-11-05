"""BOSSAPP-813: a fresh install is claimed on its own site when the browser is on a private network.

Operator, 2026-11-05: "I'll use this link only - https://boss-1.tail18604a.ts.net/ ... where is the
claim link after I go to this website?" Like Gitea, n8n or Portainer, the first visitor sets up the
Owner on the site — but only a visitor whose REAL client address is private. The address is
resolved from the socket peer and the X-Forwarded-For of BOSS_TRUSTED_PROXIES only; the docker
bridge gateway, the console container or a loopback peer that reports nobody is never the client,
and a hostname is never trusted (a *.ts.net name can be published by Tailscale Funnel).

The compose chain this models: browser (tailnet 100.x) → Caddy on the host (appends the client) →
docker-proxy (peer 172.18.0.1, the gateway) → web 172.18.0.3 (forwards ONE hop,
BOSS_CONSOLE_PROXY_HOPS=1) → api (trusts loopback and `web`).
"""
from __future__ import annotations

import ipaddress

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.tests.identity_s1_world import ORIGIN, STRONG, world  # noqa: F401  (world is a fixture)
from boss_engine.config import get_settings
from boss_engine.identity import claim

WEB = "172.18.0.3"            # the console container: a trusted proxy
GATEWAY = "172.18.0.1"        # the docker bridge gateway: any process on the host
TRUSTED = f"127.0.0.0/8,::1/128,{WEB}/32"
COMPOSE_NET = [ipaddress.ip_network("172.18.0.0/16")]
TAILNET_BROWSER = "100.101.102.103"
ADDRESSED = {"X-Forwarded-Host": ORIGIN.split("://", 1)[1], "X-Forwarded-Proto": "https"}


@pytest.fixture
def compose_chain(monkeypatch):
    from apps.api.app import main

    for settings in {id(s): s for s in (main.settings, get_settings())}.values():
        monkeypatch.setattr(settings, "trusted_proxies", TRUSTED)
        monkeypatch.setattr(settings, "public_url", "")
    monkeypatch.setattr(claim, "_container_networks", lambda: list(COMPOSE_NET))


def client_from(peer: str) -> TestClient:
    from apps.api.app.main import app

    return TestClient(app, base_url=ORIGIN, client=(peer, 40000))


def post_claim(peer: str, xff: str | None = None, **extra):
    # what the console's /v1 proxy passes on from Caddy (header_up X-Forwarded-Host/Proto)
    headers = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin", **ADDRESSED}
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    body = {"name": "First Owner", "email": "first@example.test", "password": STRONG,
            "confirm_origin": ORIGIN, **extra}
    return client_from(peer).post("/v1/auth/setup/claim", json=body, headers=headers)


# ---- the rule itself ----------------------------------------------------------------------------

@pytest.mark.parametrize("peer, xff, expected", [
    (WEB, TAILNET_BROWSER, TAILNET_BROWSER),                 # the trusted console reports a tailnet browser
    (WEB, "192.168.1.20", "192.168.1.20"),                   # a LAN browser
    (WEB, "fd00::20", "fd00::20"),                           # ULA
    (WEB, f"8.8.8.8, {TAILNET_BROWSER}", TAILNET_BROWSER),   # right-most untrusted hop wins
    (WEB, f"{TAILNET_BROWSER}, 8.8.8.8", None),              # ...so a client cannot prepend a private one
    (WEB, "8.8.8.8", None),                                  # a public browser through the proxy
    (WEB, None, None),                                       # the console reported nobody (hops=0)
    (WEB, GATEWAY, None),                                    # the gateway is never the client
    (WEB, WEB, None),                                        # nor the console itself
    ("8.8.4.4", "10.0.0.5", None),                           # spoofed XFF from an untrusted peer: ignored
    (GATEWAY, None, None),                                   # straight to the API from the host
    (GATEWAY, "10.0.0.9", None),                             # ...with a forged hop
    ("127.0.0.1", None, None),                               # a loopback peer reporting nobody (a tunnel?)
    ("127.0.0.1", "127.0.0.1", "127.0.0.1"),                 # a loopback browser a trusted proxy reported
    ("10.0.0.7", None, "10.0.0.7"),                          # a LAN peer reaching the API directly
    ("10.0.0.7", "8.8.8.8", "10.0.0.7"),                     # whose XFF is not believed either way
    ("100.64.0.1", None, "100.64.0.1"),
    ("8.8.8.8", None, None),
    ("testclient", None, None),
])
def test_who_may_claim_on_the_site(peer, xff, expected):
    assert claim.site_claim_client(peer, xff, trusted=TRUSTED, machinery=COMPOSE_NET) == expected


def test_outside_a_container_no_machinery_is_assumed(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: False)
    assert claim._container_networks() == []


# ---- the API --------------------------------------------------------------------------------------

def test_options_offer_the_claim_form_to_a_private_browser_and_the_link_to_a_public_one(world, engine, compose_chain):
    world.network_bridge = False
    private = client_from(WEB).get("/v1/auth/options", headers={"X-Forwarded-For": TAILNET_BROWSER,
                                                               "Origin": ORIGIN}).json()["claim"]
    if private is None:
        pytest.skip("this database's org is already claimed")
    assert private["path"] == "network_bridge" and "network" in private["paths"]
    assert private["network"] == "private_network" and private["origin"] == ORIGIN
    public = client_from(WEB).get("/v1/auth/options", headers={"X-Forwarded-For": "8.8.8.8"}).json()["claim"]
    assert public["path"] in ("setup_token", "legacy_password") and "network" not in public["paths"]
    assert public["network"] is None
    status = client_from(WEB).get("/v1/auth/setup/status", headers={"X-Forwarded-For": TAILNET_BROWSER,
                                                                    "Origin": ORIGIN}).json()
    assert "network" in status["paths"] and status["origin"] == ORIGIN
    spoofed = client_from("8.8.4.4").get("/v1/auth/setup/status", headers={"X-Forwarded-For": "10.0.0.5"}).json()
    assert "network" not in spoofed["paths"]


def test_a_private_browser_through_the_trusted_proxy_claims_without_a_token(world, engine, compose_chain):
    world.network_bridge = False
    ok = post_claim(WEB, TAILNET_BROWSER)
    assert ok.status_code == 200, ok.text
    assert world.events_of("bootstrap.claimed")[0].details == {"path": "network"}
    with engine.begin() as db:
        bound = db.execute(text("SELECT policy->>'public_origin' FROM identity_settings")).scalar()
    assert bound == ORIGIN
    # one claim, for good
    assert post_claim(WEB, "10.0.0.8", email="second@example.test").status_code == 404


@pytest.mark.parametrize("peer, xff", [
    (WEB, "8.8.8.8"),            # a public browser
    ("8.8.4.4", "10.0.0.5"),     # a spoofed XFF from an untrusted peer
    (GATEWAY, None),             # the docker gateway, nobody reported
    (WEB, None),                 # the trusted console that reported nobody
])
def test_anyone_else_needs_a_credential(world, engine, compose_chain, peer, xff):
    world.network_bridge = False
    res = post_claim(peer, xff)
    if peer == WEB:
        assert res.status_code == 401 and res.json()["error"] == "credential_required", res.text
    else:   # an untrusted peer's X-Forwarded-Proto is not believed either: refused before the claim
        assert (res.status_code, res.json()["error"]) in ((401, "credential_required"), (403, "csrf_failed")), res.text


def test_the_private_path_still_binds_only_the_address_the_browser_is_on(world, engine, compose_chain):
    world.network_bridge = False
    res = post_claim(WEB, TAILNET_BROWSER, confirm_origin="https://evil.example")
    assert res.status_code == 400 and res.json()["error"] == "origin_mismatch"


def test_the_private_path_keeps_the_rate_limit(world, engine, compose_chain):
    world.network_bridge = False
    statuses = [post_claim(WEB, "10.9.9.9", confirm_origin="https://evil.example").status_code for _ in range(11)]
    assert statuses[:10] == [400] * 10 and statuses[10] == 429
