"""Round 3 (boss-1 at https://100.64.0.20): a passkey needs a DOMAIN on a secure context.

WebAuthn forbids an IP literal as the RP ID: every browser refuses before the authenticator runs,
so the claim's "Create your passkey" failed with a bare "That didn't work". BOSS now says so
(`passkey: false`, `passkey_reason: passkey_needs_domain`), offers only the authenticator app at
enrolment, and never hands out WebAuthn options for such an origin.

And the TOTP QR is a standalone SVG (with its namespace): the console draws it through
<img src="data:image/svg+xml…">, where an SVG without xmlns is a broken image.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    fresh_client,
    make_user,
    sign_in,
    world,
)
from boss_engine.config import get_settings
from boss_engine.identity import webauthn

IP_ORIGIN = "https://100.64.0.20"


@pytest.mark.parametrize("origin, reason", [
    ("https://100.64.0.20", "needs_domain"),
    ("https://100.64.0.20:8443", "needs_domain"),
    ("http://127.0.0.1:3000", "needs_domain"),
    ("https://[fd7a:115c:a1e0::1]", "needs_domain"),
    ("http://[::1]:3000", "needs_domain"),
    ("http://boss.lan", "needs_https"),
    ("http://shared-server.tail4b2c.ts.net", "needs_https"),
    (None, "unavailable"),
    ("", "unavailable"),
    ("https://boss.example.test", None),
    ("https://shared-server.tail4b2c.ts.net", None),
    ("http://localhost:3000", None),
    ("http://boss.localhost:3000", None),
])
def test_the_blocker_judges_the_origin(origin, reason):
    assert webauthn.passkey_blocker(origin) == reason


@pytest.mark.parametrize("origin", [IP_ORIGIN, "https://[fd7a:115c:a1e0::1]", "http://boss.lan"])
def test_no_webauthn_options_for_an_origin_a_browser_refuses(origin):
    with pytest.raises(webauthn.PasskeyError) as exc:
        webauthn.rp_for(origin)
    assert exc.value.code in ("needs_domain", "needs_https")
    # …and both option builders go through rp_for before anything else.
    with pytest.raises(webauthn.PasskeyError):
        webauthn.registration_options(None, principal_id="usr_x", email="x@example.test", display_name="X",
                                      origin=origin, binding_secret="s")
    with pytest.raises(webauthn.PasskeyError):
        webauthn.authentication_options(None, purpose="login", origin=origin, binding_secret="s")


def test_a_domain_is_an_rp_id():
    assert webauthn.rp_for("https://boss.example.test") == ("boss.example.test", "https://boss.example.test")


def _on_ip(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_url", IP_ORIGIN)
    return {"Origin": IP_ORIGIN, "Sec-Fetch-Site": "same-origin"}


def test_options_say_passkeys_need_a_domain(world, engine, monkeypatch):
    _on_ip(monkeypatch)
    opts = fresh_client().get("/v1/auth/options").json()
    assert opts["passkey"] is False and opts["passkey_reason"] == "passkey_needs_domain"


def test_options_on_a_domain_offer_passkeys(world, engine):
    opts = fresh_client().get("/v1/auth/options").json()
    assert opts["passkey"] is True and opts["passkey_reason"] is None


def test_enrolment_offers_only_the_authenticator_app_on_an_ip(world, engine, monkeypatch):
    headers = _on_ip(monkeypatch)
    make_user(engine, "own@example.test", role="owner")
    client = fresh_client()
    body = sign_in(client, "own@example.test", headers=headers).json()
    assert body["next"] == "enrol" and body["methods"] == ["totp"]
    assert body["passkey_reason"] == "passkey_needs_domain"
    me = client.get("/v1/identity/me")
    assert me.status_code == 200, me.text
    csrf = {**headers, "X-Boss-CSRF": me.json()["csrf_token"]}
    reg = client.post("/v1/identity/me/passkeys/options", json={}, headers=csrf)
    assert reg.status_code == 409 and reg.json()["error"] == "passkey_needs_domain"
    assert "publicKey" not in reg.json()


def test_no_sign_in_options_on_an_ip(world, engine, monkeypatch):
    headers = _on_ip(monkeypatch)
    res = fresh_client().post("/v1/auth/webauthn/options", json={"purpose": "login"}, headers=headers)
    assert res.status_code == 409 and res.json()["error"] == "passkey_needs_domain"


def test_the_totp_qr_is_a_standalone_svg():
    from apps.api.app.routers.identity_me import _qr_svg

    svg = _qr_svg("otpauth://totp/BOSS:own%40example.test?secret=JBSWY3DPEHPK3PXP&issuer=BOSS")
    assert svg and not svg.startswith("<?xml")
    root = ET.fromstring(svg)                       # well-formed XML…
    assert root.tag == "{http://www.w3.org/2000/svg}svg"     # …whose root is in the SVG namespace
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg
