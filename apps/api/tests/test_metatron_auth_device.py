"""Wave B tests: RFC 8628 device-code login, bearer/API-key auth, refresh rotation & revocation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.app.config import get_settings as api_get_settings
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings as engine_get_settings
from boss_engine.identity import authn


def test_device_code_flow_end_to_end_under_require_api_auth_and_enforce(
    client: TestClient, engine, monkeypatch
):
    monkeypatch.setattr(main.settings, "require_api_auth", True)
    monkeypatch.setattr(main.settings, "authz_mode", "enforce")
    monkeypatch.setattr(api_get_settings(), "authz_mode", "enforce")
    monkeypatch.setattr(engine_get_settings(), "authz_mode", "enforce")
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "enforce")
    world = IdentityWorld.create(engine)
    operator = world.person("metatron-cli-user")
    world.bind(operator, "operator", project=world.project_a)

    # 1. Start device login with NO Origin header and NO existing API key.
    started = client.post(
        "/v1/auth/device/code",
        json={"client_id": "metatron", "scope": "metatron"},
    )
    assert started.status_code == 200, started.text
    assert started.headers.get("cache-control") == "no-store"
    data = started.json()
    assert data["device_code"] and data["user_code"]
    assert data["verification_uri"].endswith("/connected-apps/device")
    assert data["user_code"] in data["verification_uri_complete"]

    # 2. Polling before approval returns authorization_pending, then slow_down on immediate retry.
    pending = client.post(
        "/v1/auth/device/token",
        json={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": data["device_code"],
        },
    )
    assert pending.status_code == 400
    assert pending.json()["error"] == "authorization_pending"

    slow = client.post(
        "/v1/auth/device/token",
        json={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": data["device_code"],
        },
    )
    assert slow.status_code == 400
    assert slow.json()["error"] == "slow_down"

    # 3. Signed-in user approves the user_code in the console.
    with world.acting_as(world.ctx(operator)):
        client.cookies.set(authn.SESSION_COOKIE, "bss_test_console")
        try:
            approved = client.post(
                "/v1/mcp-oauth/device/approve",
                json={"user_code": data["user_code"], "approve": True},
                headers={"sec-fetch-site": "same-origin"},
            )
        finally:
            client.cookies.clear()
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    # 4. Poll exchanges for boss_pat_ Bearer + refresh token + principal summary.
    tok_resp = client.post(
        "/v1/auth/device/token",
        json={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": data["device_code"],
        },
    )
    assert tok_resp.status_code == 200, tok_resp.text
    assert tok_resp.headers.get("cache-control") == "no-store"
    tokens = tok_resp.json()
    assert tokens["access_token"].startswith("boss_pat_")
    assert tokens["token_type"] == "Bearer"
    assert tokens["refresh_token"]
    assert tokens["principal"]["id"] == operator.id
    assert "operator" in tokens["principal"]["roles"]

    # 5. Both Authorization: Bearer and X-Boss-Api-Key authenticate the minted token.
    r_bearer = client.get(
        "/v1/metatron/capabilities",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r_bearer.status_code == 200, r_bearer.text

    r_key = client.get(
        "/v1/metatron/capabilities",
        headers={"X-Boss-Api-Key": tokens["access_token"]},
    )
    assert r_key.status_code == 200, r_key.text

    # 6. Refresh rotates both tokens and kills the old access token.
    ref_resp = client.post(
        "/v1/auth/token",
        json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
    )
    assert ref_resp.status_code == 200, ref_resp.text
    assert ref_resp.headers.get("cache-control") == "no-store"
    rotated = ref_resp.json()
    assert rotated["access_token"] != tokens["access_token"]
    assert rotated["refresh_token"] != tokens["refresh_token"]

    old_after_rotate = client.get(
        "/v1/metatron/capabilities",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert old_after_rotate.status_code == 401

    new_ok = client.get(
        "/v1/metatron/capabilities",
        headers={"Authorization": f"Bearer {rotated['access_token']}"},
    )
    assert new_ok.status_code == 200

    # 7. Presenting the spent refresh token revokes the entire grant and the rotated access token.
    reused = client.post(
        "/v1/auth/token",
        json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
    )
    assert reused.status_code == 400
    assert reused.json()["error"] == "invalid_grant"

    after_reuse = client.get(
        "/v1/metatron/capabilities",
        headers={"Authorization": f"Bearer {rotated['access_token']}"},
    )
    assert after_reuse.status_code == 401


def test_device_denied_expired_scope_read_and_revoke(
    client: TestClient, engine, monkeypatch
):
    monkeypatch.setattr(main.settings, "require_api_auth", True)
    monkeypatch.setattr(main.settings, "authz_mode", "enforce")
    monkeypatch.setattr(api_get_settings(), "authz_mode", "enforce")
    monkeypatch.setattr(engine_get_settings(), "authz_mode", "enforce")
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "enforce")
    world = IdentityWorld.create(engine)
    operator = world.person("metatron-read-user")
    world.bind(operator, "operator", project=world.project_a)

    # Denied device code -> access_denied
    denied_start = client.post("/v1/auth/device/code", json={"scope": "metatron"}).json()
    with world.acting_as(world.ctx(operator)):
        client.cookies.set(authn.SESSION_COOKIE, "bss_test_console")
        try:
            app_denied = client.post(
                "/v1/mcp-oauth/device/approve",
                json={"user_code": denied_start["user_code"], "approve": False},
                headers={"sec-fetch-site": "same-origin"},
            )
            assert app_denied.status_code == 200, app_denied.text
        finally:
            client.cookies.clear()
    denied_tok = client.post(
        "/v1/auth/device/token",
        json={"device_code": denied_start["device_code"]},
    )
    assert denied_tok.status_code == 400
    assert denied_tok.json()["error"] == "access_denied"

    # Expired device code -> expired_token
    exp_start = client.post("/v1/auth/device/code", json={"scope": "metatron"}).json()
    with engine.begin() as c:
        c.execute(
            text("UPDATE mcp_oauth_device_codes SET expires_at = :past WHERE user_code = :uc"),
            {"past": datetime.now(timezone.utc) - timedelta(minutes=1), "uc": exp_start["user_code"]},
        )
    exp_tok = client.post("/v1/auth/device/token", json={"device_code": exp_start["device_code"]})
    assert exp_tok.status_code == 400
    assert exp_tok.json()["error"] == "expired_token"

    # metatron:read scope only grants :read permissions, then revoke kills key immediately.
    ro_start = client.post("/v1/auth/device/code", json={"scope": "metatron:read"}).json()
    with world.acting_as(world.ctx(operator)):
        client.cookies.set(authn.SESSION_COOKIE, "bss_test_console")
        try:
            app_ro = client.post(
                "/v1/mcp-oauth/device/approve",
                json={"user_code": ro_start["user_code"], "approve": True},
                headers={"sec-fetch-site": "same-origin"},
            )
            assert app_ro.status_code == 200, app_ro.text
        finally:
            client.cookies.clear()
    ro_tokens = client.post(
        "/v1/auth/device/token", json={"device_code": ro_start["device_code"]}
    ).json()

    can_read_policy = client.get(
        "/v1/metatron/policy",
        params={"project_id": world.project_a},
        headers={"Authorization": f"Bearer {ro_tokens['access_token']}"},
    )
    assert can_read_policy.status_code == 200

    cannot_create_grant = client.post(
        "/v1/metatron/grants",
        json={"project_id": world.project_a, "ttl_s": 120},
        headers={"Authorization": f"Bearer {ro_tokens['access_token']}"},
    )
    assert cannot_create_grant.status_code == 403

    rev = client.post("/v1/auth/revoke", json={"token": ro_tokens["access_token"]})
    assert rev.status_code == 200
    assert rev.headers.get("cache-control") == "no-store"

    after_revoke = client.get(
        "/v1/metatron/policy",
        params={"project_id": world.project_a},
        headers={"Authorization": f"Bearer {ro_tokens['access_token']}"},
    )
    assert after_revoke.status_code == 401
