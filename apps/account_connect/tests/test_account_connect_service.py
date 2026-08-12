"""Focused Account Connect service tests (device + browser Easy Connect, seeds)."""

from __future__ import annotations

import re
import uuid
from urllib.parse import parse_qs, urlparse


def test_health_endpoints_require_no_auth(ac_client):
    health = ac_client.get("/v1/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "healthy"
    assert body["service"] == "boss-account-connect"

    org_health = ac_client.get("/v1/orgs/org_boss_cloud/health")
    assert org_health.status_code == 200
    assert org_health.json()["status"] == "healthy"


def test_device_flow_form_authorize_then_poll(
    ac_client, auth_headers, ac_default_org_id
):
    user_code = f"T{uuid.uuid4().hex[:3].upper()}-{uuid.uuid4().hex[:4].upper()}"

    page = ac_client.get("/device", params={"user_code": user_code})
    assert page.status_code == 200
    assert "Device code" in page.text or "device code" in page.text.lower()

    # Operator-facing form (no service token): short code is the secret.
    submit = ac_client.post(
        "/device",
        data={"user_code": user_code.lower().replace("-", " ")},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert submit.status_code == 200
    assert "Connected" in submit.text or "authorized" in submit.text.lower()

    # Canonical uppercase+hyphen form is stored and returned by poll.
    expected = user_code.upper()
    # Normalization may re-insert a single hyphen after collapsing spaces.
    # Accept either the original mint form or fully hyphen-normalized equivalent.
    polled = ac_client.get(
        f"/v1/device/authorizations/{expected}",
        headers=auth_headers,
    )
    assert polled.status_code == 200, polled.text
    payload = polled.json()
    assert payload["status"] == "authorized"
    assert payload["hosted_org_id"] == ac_default_org_id
    assert payload["user_code"].replace("-", "") == expected.replace("-", "")


def test_device_flow_api_authorize_normalizes_hyphens(
    ac_client, auth_headers, ac_default_org_id
):
    raw_code = "ab12–cd34"  # en-dash + lowercase
    authorize = ac_client.post(
        "/v1/device/authorize",
        headers=auth_headers,
        json={"user_code": raw_code, "hosted_org_id": ac_default_org_id},
    )
    assert authorize.status_code == 200, authorize.text
    body = authorize.json()
    assert body["status"] == "authorized"
    assert body["hosted_org_id"] == ac_default_org_id
    assert body["user_code"] == "AB12-CD34"

    # Re-authorize same code upserts (unique on user_code).
    again = ac_client.post(
        "/v1/device/authorize",
        headers=auth_headers,
        json={"user_code": "AB12-CD34", "hosted_org_id": ac_default_org_id},
    )
    assert again.status_code == 200
    assert again.json()["user_code"] == "AB12-CD34"

    polled = ac_client.get(
        "/v1/device/authorizations/AB12-CD34",
        headers=auth_headers,
    )
    assert polled.status_code == 200
    assert polled.json()["status"] == "authorized"

    missing = ac_client.get(
        "/v1/device/authorizations/ZZZZ-YYYY",
        headers=auth_headers,
    )
    assert missing.status_code == 404


def test_device_authorize_requires_service_token_when_configured(ac_client):
    response = ac_client.post(
        "/v1/device/authorize",
        json={"user_code": "NOPE-CODE"},
    )
    assert response.status_code == 401


def test_browser_flow_create_authorize_complete(
    ac_client, auth_headers, ac_default_org_id
):
    project_id = "boss-dogfood"
    state = f"state_{uuid.uuid4().hex[:12]}"

    created = ac_client.post(
        f"/v1/orgs/{ac_default_org_id}/auth-sessions",
        headers=auth_headers,
        json={
            "project_id": project_id,
            "hosted_org_id": ac_default_org_id,
            "return_url": "http://localhost:3000/cloud/callback",
            "state": state,
            "metadata": {"flow": "browser"},
        },
    )
    assert created.status_code == 201, created.text
    session = created.json()
    session_id = session["id"]
    assert session["status"] == "pending"
    assert session["hosted_org_id"] == ac_default_org_id
    assert "session_id=" in session["authorization_url"]
    assert f"state={state}" in session["authorization_url"]

    page = ac_client.get(
        "/v1/demo/authorize",
        params={"session_id": session_id, "state": state},
    )
    assert page.status_code == 200
    assert "Approve" in page.text or "connect" in page.text.lower()

    # Browser approve stores auth_code and redirects to return_url.
    approve = ac_client.post(
        "/v1/demo/authorize",
        data={"session_id": session_id, "state": state},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert approve.status_code in (302, 303), approve.text
    location = approve.headers.get("location") or ""
    assert "code=" in location
    assert f"state={state}" in location or "state=" in location
    query = parse_qs(urlparse(location).query)
    auth_code = query["code"][0]
    assert auth_code

    # Wrong code is rejected once auth_code was stored.
    bad_complete = ac_client.post(
        f"/v1/orgs/{ac_default_org_id}/auth-sessions/{session_id}/complete",
        headers=auth_headers,
        json={
            "project_id": project_id,
            "code": "wrong-code",
            "state": state,
        },
    )
    assert bad_complete.status_code == 409

    complete = ac_client.post(
        f"/v1/orgs/{ac_default_org_id}/auth-sessions/{session_id}/complete",
        headers=auth_headers,
        json={
            "project_id": project_id,
            "code": auth_code,
            "state": state,
        },
    )
    assert complete.status_code == 200, complete.text
    completed = complete.json()
    assert completed["status"] == "completed"
    assert completed["hosted_org_id"] == ac_default_org_id
    assert completed["project_id"] == project_id

    # Idempotent re-complete.
    again = ac_client.post(
        f"/v1/auth-sessions/{session_id}/complete",
        headers=auth_headers,
        json={
            "project_id": project_id,
            "code": auth_code,
            "state": state,
        },
    )
    assert again.status_code == 200
    assert again.json()["hosted_org_id"] == ac_default_org_id


def test_entitlements_seed_includes_cloud_control_plane(
    ac_client, auth_headers, ac_default_org_id
):
    response = ac_client.get(
        f"/v1/orgs/{ac_default_org_id}/entitlements",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    keys = [item["entitlement_key"] for item in response.json()["entitlements"]]
    assert "cloud_control_plane" in keys
    cloud = next(
        item
        for item in response.json()["entitlements"]
        if item["entitlement_key"] == "cloud_control_plane"
    )
    assert cloud["status"] == "active"
    assert cloud["edition"] == "cloud"


def test_marketplace_packs_seed_includes_core(
    ac_client, auth_headers, ac_default_org_id
):
    response = ac_client.get(
        f"/v1/orgs/{ac_default_org_id}/marketplace/packs",
        headers=auth_headers,
        params={"project_id": "boss-dogfood"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    pack_ids = [item["pack_id"] for item in body["items"]]
    assert "core_v1" in pack_ids
    core = next(item for item in body["items"] if item["pack_id"] == "core_v1")
    assert core["status"] == "active"
    assert core["required_entitlement"] == "cloud_control_plane"


def test_team_invite_create_and_revoke(ac_client, auth_headers, ac_default_org_id):
    email = f"operator+{uuid.uuid4().hex[:8]}@example.test"
    created = ac_client.post(
        f"/v1/orgs/{ac_default_org_id}/team-invites",
        headers=auth_headers,
        json={
            "project_id": "boss-dogfood",
            "email": email,
            "display_name": "Test Operator",
            "role_keys": ["operator"],
            "permissions": ["cloud.read"],
        },
    )
    assert created.status_code == 201, created.text
    invite = created.json()
    assert invite["status"] == "pending"
    assert invite["email"] == email
    assert invite["id"]
    assert invite["invite_id"] == invite["id"]

    revoked = ac_client.post(
        f"/v1/orgs/{ac_default_org_id}/team-invites/{invite['id']}/revoke",
        headers=auth_headers,
        json={"project_id": "boss-dogfood", "reason": "test cleanup"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"


def test_normalize_user_code_helper():
    from apps.account_connect.app.main import _normalize_user_code

    assert _normalize_user_code("ab12-cd34") == "AB12-CD34"
    assert _normalize_user_code(" ab12 cd34 ") == "AB12-CD34"
    assert _normalize_user_code("ab12–cd34") == "AB12-CD34"
    assert _normalize_user_code("ab12_cd34") == "AB12-CD34"
    assert re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)*", _normalize_user_code("x y z"))
