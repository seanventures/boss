"""OIDC JWKS timeout and fail-closed authentication tests (no live IdP)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.error import URLError

import jwt
import pytest
from jwt.exceptions import PyJWKClientConnectionError

from boss_engine.config import Settings
from boss_engine.services import oidc as oidc_mod
from boss_engine.services.oidc import (
    JWKS_HTTP_TIMEOUT_SECONDS,
    OidcAuthenticationError,
    OidcAuthenticator,
    jwks_client_for_url,
)


def _oidc_settings() -> Settings:
    settings = Settings()
    settings.oidc_enabled = True
    settings.oidc_issuer = "https://issuer.example.com"
    settings.oidc_audience = "boss-api"
    settings.oidc_jwks_url = "https://issuer.example.com/.well-known/jwks.json"
    settings.oidc_principal_claim = "boss_member_id"
    settings.oidc_allowed_algorithms = "RS256,ES256"
    return settings


def _unsigned_bearer() -> str:
    return jwt.encode(
        {
            "iss": "https://issuer.example.com",
            "aud": "boss-api",
            "sub": "sub-ceo",
            "boss_member_id": "ceo",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        "unused-jwks-fails-first-32b-min!",
        algorithm="HS256",
        headers={"kid": "boss-test-key"},
    )


@pytest.fixture(autouse=True)
def _clear_jwks_cache():
    jwks_client_for_url.cache_clear()
    yield
    jwks_client_for_url.cache_clear()


def test_jwks_client_sets_explicit_http_timeout():
    client = jwks_client_for_url("https://issuer.example.com/.well-known/jwks.json")
    assert client.timeout == JWKS_HTTP_TIMEOUT_SECONDS
    assert JWKS_HTTP_TIMEOUT_SECONDS == 5.0


def test_jwks_fetch_passes_timeout_and_fails_closed_on_timeout(monkeypatch):
    import urllib.request as urllib_request

    seen: dict[str, float | None] = {"timeout": None}

    def fake_urlopen(*_args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise TimeoutError("slow identity provider")

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)

    client = jwks_client_for_url("https://issuer.example.com/.well-known/jwks.json")
    with pytest.raises(PyJWKClientConnectionError):
        client.fetch_data()
    assert seen["timeout"] == JWKS_HTTP_TIMEOUT_SECONDS

    auth = OidcAuthenticator(_oidc_settings())
    with pytest.raises(OidcAuthenticationError, match="OIDC"):
        auth.verify_bearer_token(_unsigned_bearer())


def test_jwks_connection_error_fails_closed(monkeypatch):
    class FailingClient:
        def get_signing_key_from_jwt(self, _token: str):
            raise PyJWKClientConnectionError('Fail to fetch data from the url, err: "timed out"')

    monkeypatch.setattr(oidc_mod, "jwks_client_for_url", lambda _url: FailingClient())
    auth = OidcAuthenticator(_oidc_settings())
    with pytest.raises(OidcAuthenticationError, match="Invalid OIDC bearer token"):
        auth.verify_bearer_token(_unsigned_bearer())


def test_jwks_raw_timeout_error_fails_closed(monkeypatch):
    class FailingClient:
        def get_signing_key_from_jwt(self, _token: str):
            raise TimeoutError("JWKS HTTP timed out")

    monkeypatch.setattr(oidc_mod, "jwks_client_for_url", lambda _url: FailingClient())
    auth = OidcAuthenticator(_oidc_settings())
    with pytest.raises(OidcAuthenticationError, match="OIDC JWKS fetch failed"):
        auth.verify_bearer_token(_unsigned_bearer())


def test_jwks_url_error_fails_closed(monkeypatch):
    class FailingClient:
        def get_signing_key_from_jwt(self, _token: str):
            raise URLError("Name or service not known")

    monkeypatch.setattr(oidc_mod, "jwks_client_for_url", lambda _url: FailingClient())
    auth = OidcAuthenticator(_oidc_settings())
    with pytest.raises(OidcAuthenticationError, match="OIDC JWKS fetch failed"):
        auth.verify_bearer_token(_unsigned_bearer())
