"""Easy Connect E2E: Community control plane link + auto entitlement sync.

Mocks AccountConnectAdapter so tests run without a live hosted Account Connect
service while still exercising the full start → authorize → poll/complete →
sync → unlink loop through HTTP.
"""

from __future__ import annotations

from typing import Any, Optional

import apps.api.app.main as api_main


PROJECT_ID = "boss-dogfood"
HOSTED_ORG_ID = "org_boss_cloud_e2e"
ACTOR = "ceo"
MOCK_AC_URL = "https://account-connect.test.local"


class _FakeAccountConnectAdapter:
    """In-memory hosted Account Connect for Easy Connect E2E tests."""

    def __init__(self) -> None:
        self.device_authorized = False
        self.device_hosted_org_id = HOSTED_ORG_ID
        self.created_sessions: list[dict[str, Any]] = []
        self.completed_sessions: list[dict[str, Any]] = []
        self.fetch_calls = 0
        self.fail_entitlements = False
        self.entitlements: list[dict[str, Any]] = [
            {
                "id": "ent_cloud_control_plane",
                "entitlement_key": "cloud_control_plane",
                "capability_key": "cloud_control_plane",
                "capabilities": ["cloud_control_plane", "hosted_mission_control"],
                "status": "active",
                "source": "account_connect_hosted",
                "seats": 5,
            },
            {
                "id": "ent_team_collaboration",
                "entitlement_key": "team_collaboration",
                "capability_key": "team_collaboration",
                "capabilities": ["team_collaboration"],
                "status": "active",
                "source": "account_connect_hosted",
            },
        ]

    def create_auth_session(
        self,
        *,
        project_id: str,
        return_url: Optional[str],
        state: str,
        metadata: dict[str, Any],
        hosted_org_id: Optional[str] = None,
    ) -> dict[str, Any]:
        session_id = f"acsess_{len(self.created_sessions) + 1}"
        record = {
            "id": session_id,
            "project_id": project_id,
            "state": state,
            "return_url": return_url,
            "metadata": metadata,
            "hosted_org_id": hosted_org_id,
            "authorization_url": (
                f"{MOCK_AC_URL}/authorize?state={state}&session={session_id}"
            ),
        }
        self.created_sessions.append(record)
        return dict(record)

    def get_device_authorization(self, user_code: str) -> dict[str, Any]:
        code = user_code.strip().upper().replace(" ", "")
        if not self.device_authorized:
            return {"status": "pending", "user_code": code}
        return {
            "status": "authorized",
            "user_code": code,
            "hosted_org_id": self.device_hosted_org_id,
        }

    def complete_auth_session(
        self,
        *,
        project_id: str,
        provider_ref: str,
        code: str,
        state: str,
        metadata: dict[str, Any],
        hosted_org_id: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = {
            "hosted_org_id": HOSTED_ORG_ID,
            "connection_ref": f"conn_{provider_ref}",
            "marketplace_identity": {"org": HOSTED_ORG_ID},
            "billing_portal_ref": None,
            "connector_authorization_refs": [],
            "optional_telemetry_enabled": False,
            "metadata": {
                "project_id": project_id,
                "state": state,
                "code": code,
                **(metadata or {}),
            },
        }
        self.completed_sessions.append(payload)
        return payload

    def fetch_entitlements(
        self,
        *,
        hosted_org_id: str,
        project_id: str,
    ) -> list[dict[str, Any]]:
        self.fetch_calls += 1
        if self.fail_entitlements:
            from boss_engine.services.commercial import AccountConnectSyncError

            raise AccountConnectSyncError("Simulated hosted entitlement outage.")
        assert hosted_org_id == HOSTED_ORG_ID
        assert project_id == PROJECT_ID
        return [dict(item) for item in self.entitlements]


def _configure_mock_ac(monkeypatch, fake: _FakeAccountConnectAdapter) -> None:
    """Point control-plane settings + adapter at the fake hosted AC."""
    for target in (
        api_main.settings,
        api_main.commercial.settings,
        api_main.cloud_product.settings,
        api_main.commercial.account_connect_adapter.settings,
    ):
        monkeypatch.setattr(target, "account_connect_url", MOCK_AC_URL)
        monkeypatch.setattr(target, "account_connect_token", "test-token")

    monkeypatch.setattr(
        api_main.commercial, "account_connect_adapter", fake
    )
    # cloud_product uses commercial.account_connect_adapter for device poll
    # and commercial.sync / complete_auth_session — already wired via commercial.


def _assert_community_surfaces_ok(client) -> None:
    health = client.get("/health")
    assert health.status_code == 200, health.text
    providers = client.get("/v1/providers", params={"project_id": PROJECT_ID})
    assert providers.status_code == 200, providers.text
    catalog = client.get("/v1/cloud/catalog", params={"project_id": PROJECT_ID})
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["independence"]["community_runs_without_cloud"] is True


def test_easy_connect_device_link_sync_unlink(client, monkeypatch):
    fake = _FakeAccountConnectAdapter()
    _configure_mock_ac(monkeypatch, fake)
    _assert_community_surfaces_ok(client)

    status_before = client.get("/v1/cloud/status", params={"project_id": PROJECT_ID})
    assert status_before.status_code == 200
    assert status_before.json()["linked"] is False

    started = client.post(
        "/v1/cloud/easy-connect",
        json={
            "project_id": PROJECT_ID,
            "method": "device",
            "actor": ACTOR,
        },
    )
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["method"] == "device"
    assert body["status"] == "pending"
    assert body.get("user_code")
    assert body.get("session_id")
    assert fake.created_sessions, "hosted create_auth_session should be called"
    assert "user_code" in (fake.created_sessions[0].get("metadata") or {})

    session_id = body["session_id"]

    # Not yet authorized on hosted AC
    pending = client.get(
        f"/v1/cloud/easy-connect/{session_id}",
        params={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["status"] == "pending"

    # Hosted AC authorizes the user_code
    fake.device_authorized = True
    completed = client.get(
        f"/v1/cloud/easy-connect/{session_id}",
        params={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert completed.status_code == 200, completed.text
    done = completed.json()
    assert done["status"] == "completed"
    assert done["linked"] is True
    assert done["hosted_org_id"] == HOSTED_ORG_ID
    assert done["entitlements_synced"] is True
    assert done["synced_entitlement_count"] >= 2
    assert done.get("entitlements_sync_error") in (None, "")
    assert fake.fetch_calls >= 1

    status_linked = client.get("/v1/cloud/status", params={"project_id": PROJECT_ID})
    assert status_linked.status_code == 200
    assert status_linked.json()["linked"] is True

    entitlements = client.get("/v1/entitlements", params={"project_id": PROJECT_ID})
    assert entitlements.status_code == 200, entitlements.text
    hosted_rows = [
        item
        for item in entitlements.json()["items"]
        if item.get("source") == "account_connect_hosted"
    ]
    keys = {row["capability_key"] for row in hosted_rows}
    assert "cloud_control_plane" in keys
    assert "team_collaboration" in keys

    # Community providers/health still healthy while linked
    _assert_community_surfaces_ok(client)

    unlinked = client.post(
        "/v1/cloud/unlink",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "reason": "e2e_device_unlink",
        },
    )
    assert unlinked.status_code == 200, unlinked.text
    assert unlinked.json()["linked"] is False

    status_after = client.get("/v1/cloud/status", params={"project_id": PROJECT_ID})
    assert status_after.status_code == 200
    assert status_after.json()["linked"] is False
    assert status_after.json()["independent"] is True
    _assert_community_surfaces_ok(client)


def test_easy_connect_browser_complete_by_state_syncs(client, monkeypatch):
    fake = _FakeAccountConnectAdapter()
    _configure_mock_ac(monkeypatch, fake)

    started = client.post(
        "/v1/cloud/easy-connect",
        json={
            "project_id": PROJECT_ID,
            "method": "browser",
            "actor": ACTOR,
            "return_url": "https://community.local/cloud/callback",
        },
    )
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["method"] == "browser"
    assert body["status"] in {"pending", "ready", "authorized"}
    assert body.get("authorization_url")
    session_id = body["session_id"]

    # Recover state from the auth session row via commercial list (or poll metadata)
    sessions = client.get(
        "/v1/account-connect/auth-sessions",
        params={"project_id": PROJECT_ID},
    )
    assert sessions.status_code == 200, sessions.text
    session_row = next(
        item for item in sessions.json()["items"] if item["id"] == session_id
    )
    state = session_row["state"]
    assert state

    completed = client.post(
        "/v1/cloud/easy-connect/complete",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "code": "auth-code-browser-e2e",
            "state": state,
        },
    )
    assert completed.status_code == 200, completed.text
    done = completed.json()
    assert done["status"] == "completed"
    assert done["linked"] is True
    assert done["method"] == "browser"
    assert done["hosted_org_id"] == HOSTED_ORG_ID
    assert done["entitlements_synced"] is True
    assert done["synced_entitlement_count"] >= 2
    assert fake.completed_sessions
    assert fake.fetch_calls >= 1

    # Completing by session_id is idempotent for already-completed
    again = client.post(
        f"/v1/cloud/easy-connect/{session_id}/complete",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "code": "auth-code-browser-e2e",
            "state": state,
        },
    )
    assert again.status_code == 200, again.text
    assert again.json()["linked"] is True

    entitlements = client.get("/v1/entitlements", params={"project_id": PROJECT_ID})
    hosted_rows = [
        item
        for item in entitlements.json()["items"]
        if item.get("source") == "account_connect_hosted"
    ]
    assert any(row["capability_key"] == "cloud_control_plane" for row in hosted_rows)

    _assert_community_surfaces_ok(client)

    unlink = client.post(
        "/v1/cloud/unlink",
        json={"project_id": PROJECT_ID, "actor": ACTOR, "reason": "e2e_browser_unlink"},
    )
    assert unlink.status_code == 200
    assert unlink.json()["linked"] is False


def test_easy_connect_browser_complete_by_session_id(client, monkeypatch):
    fake = _FakeAccountConnectAdapter()
    _configure_mock_ac(monkeypatch, fake)

    started = client.post(
        "/v1/cloud/easy-connect",
        json={"project_id": PROJECT_ID, "method": "browser", "actor": ACTOR},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session_id"]

    sessions = client.get(
        "/v1/account-connect/auth-sessions",
        params={"project_id": PROJECT_ID},
    )
    state = next(
        item["state"] for item in sessions.json()["items"] if item["id"] == session_id
    )

    completed = client.post(
        f"/v1/cloud/easy-connect/{session_id}/complete",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "code": "auth-code-session-path",
            "state": state,
        },
    )
    assert completed.status_code == 200, completed.text
    done = completed.json()
    assert done["linked"] is True
    assert done["entitlements_synced"] is True
    assert done["synced_entitlement_count"] >= 1


def test_easy_connect_entitlement_sync_failure_keeps_link(client, monkeypatch):
    fake = _FakeAccountConnectAdapter()
    fake.fail_entitlements = True
    _configure_mock_ac(monkeypatch, fake)

    started = client.post(
        "/v1/cloud/easy-connect",
        json={"project_id": PROJECT_ID, "method": "device", "actor": ACTOR},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session_id"]
    fake.device_authorized = True

    completed = client.get(
        f"/v1/cloud/easy-connect/{session_id}",
        params={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert completed.status_code == 200, completed.text
    done = completed.json()
    assert done["linked"] is True, "sync failure must not unlink"
    assert done["entitlements_synced"] is False
    assert done.get("entitlements_sync_error")
    assert "Simulated" in done["entitlements_sync_error"] or "outage" in (
        done["entitlements_sync_error"] or ""
    ).lower()

    status = client.get("/v1/cloud/status", params={"project_id": PROJECT_ID})
    assert status.json()["linked"] is True
    _assert_community_surfaces_ok(client)


def test_community_providers_and_health_without_ac_url(client, monkeypatch):
    """Regression: Community works fully when Account Connect is unconfigured."""
    for target in (
        api_main.settings,
        api_main.commercial.settings,
        api_main.cloud_product.settings,
        api_main.commercial.account_connect_adapter.settings,
    ):
        monkeypatch.setattr(target, "account_connect_url", None)

    _assert_community_surfaces_ok(client)
    status = client.get("/v1/cloud/status", params={"project_id": PROJECT_ID})
    assert status.status_code == 200
    assert status.json()["linked"] is False
    assert status.json()["independent"] is True
    assert status.json()["easy_connect"]["ready"] is False

    device = client.post(
        "/v1/cloud/easy-connect",
        json={"project_id": PROJECT_ID, "method": "device", "actor": ACTOR},
    )
    assert device.status_code == 201
    assert device.json()["status"] == "blocked"
    _assert_community_surfaces_ok(client)
