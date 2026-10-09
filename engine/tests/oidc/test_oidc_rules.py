"""BOSS as an OIDC provider: the rules that need no database (F6 §6.8 unit tier, stream S6)."""
from __future__ import annotations

import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from boss_engine.oidc_provider import clients, pages
from boss_engine.oidc_provider.util import s256

BASE = {"project_id": "p", "app_key": "bank", "pool_id": "opl_x", "name": "Bank",
        "redirect_uris": ["https://app.example.test/cb"]}


def test_s256_is_rfc_7636_appendix_b():
    assert s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


@pytest.mark.parametrize("uri", [
    "https://app.example.test/cb", "https://app.example.test:8443/cb?x=1", "http://localhost:3000/cb",
    "http://127.0.0.1/cb", "http://[::1]:9/cb",
])
def test_good_redirect_uris(uri):
    assert clients.check_redirect_uri(uri) == uri


@pytest.mark.parametrize("uri", [
    "", "https://*.example.test/cb", "https://app.example.test/cb#f", "https://app.example.test/cb#",
    "http://app.example.test/cb", "http://10.0.0.1/cb", "https://u@app.example.test/cb",
    "https://app.example.test/x/../cb", "https://app.example.test/x/%2E%2E/cb", "https://app.example.test/a%2Fb",
    "https://app.example.test/a%5cb", "ftp://app.example.test/cb", "javascript:alert(1)", "//app.example.test/cb",
    "https://app.example.test/a b", "https://app.example.test/a\\b", "https://app.example.test:99999/cb",
    "https:///cb", "https://app.example.test/" + "a" * 2100, 42, None,
])
def test_bad_redirect_uris(uri):
    with pytest.raises(ValueError):
        clients.check_redirect_uri(uri)


def test_validate_fills_safe_defaults():
    c = clients.validate(BASE)
    assert c["type"] == "confidential" and c["token_endpoint_auth_method"] == "client_secret_basic"
    assert c["id_token_alg"] == "RS256" and c["subject_type"] == "pairwise" and c["allowed_scopes"] == ["openid"]
    assert c["first_party"] is False and c["require_mfa"] is False


@pytest.mark.parametrize("over", [
    {"type": "public", "token_endpoint_auth_method": "client_secret_basic"},   # public uses none only
    {"token_endpoint_auth_method": "none"},                                     # confidential cannot use none
    {"token_endpoint_auth_method": "client_secret_post"},
    {"allowed_scopes": ["email"]}, {"allowed_scopes": ["openid", "admin"]},
    {"id_token_alg": "HS256"}, {"id_token_alg": "none"}, {"subject_type": "global"},
    {"jwks": {"keys": []}}, {"redirect_uris": []}, {"redirect_uris": ["https://a.test/cb"] * 21},
    {"app_key": "Bank App"}, {"name": ""},
])
def test_validate_refuses(over):
    with pytest.raises(ValueError):
        clients.validate({**BASE, **over})


def test_check_jwks_keeps_public_members_and_refuses_private_or_symmetric():
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(priv.public_key()))
    assert clients.check_jwks({"keys": [pub]})["keys"][0]["kty"] == "EC"
    for bad in ({"keys": [json.loads(jwt.algorithms.ECAlgorithm.to_jwk(priv))]},
                {"keys": [{"kty": "oct", "k": "c2VjcmV0"}]}, {"keys": [{**pub, "jku": "https://x.test"}]},
                {"keys": [{**pub, "kid": "a"}, {**pub, "kid": "a"}]}, {"keys": [{"kty": "RSA", "n": "x", "e": "AQAB"}]},
                {"jwks_uri": "https://x.test/jwks"}, []):
        with pytest.raises(ValueError):
            clients.check_jwks(bad)


def test_the_consent_page_escapes_everything_and_forbids_framing_and_scripts():
    body, headers = pages.consent_page(app_name='<script>alert(1)</script>', user_label='"><img src=x>',
                                       scopes=["openid", "<b>x</b>"], action="/oidc/o/consent", interaction='"x',
                                       csrf="c", redirect_uri="https://app.example.test/cb")
    assert "<script>" not in body and "<img" not in body and "<b>" not in body
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "script-src" not in csp
    assert "form-action 'self' https://app.example.test" in csp
    assert headers["X-Frame-Options"] == "DENY" and headers["Cache-Control"] == "no-store"


def test_error_pages_escape_and_are_never_cached():
    body, headers = pages.error_page("<x>", "<y>")
    assert "<x>" not in body and "&lt;y&gt;" in body and headers["Cache-Control"] == "no-store"
