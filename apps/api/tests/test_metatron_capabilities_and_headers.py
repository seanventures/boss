"""Wave B tests: capabilities, X-Boss-Version, 426 client_too_old, and legacy session parity."""
from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld


def test_version_headers_and_426_enforcement(client: TestClient, engine, monkeypatch):
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "enforce")
    world = IdentityWorld.create(engine)
    viewer = world.person("cap-viewer")
    world.bind(viewer, "viewer", project=world.project_a)

    # Absent headers pass through cleanly and receive X-Boss-Version
    with world.acting_as(world.ctx(viewer)):
        ok = client.get("/v1/metatron/capabilities")
        assert ok.status_code == 200
        assert ok.headers.get("x-boss-version") == app.version
        body = ok.json()
        assert body["contract"] == 1
        assert body["min_client"] == "0.1.0"
        assert set(body["features"]) >= {
            "turns.stream",
            "tools.client",
            "policy",
            "audit",
            "device_login",
            "credential_lease",
        }
        assert body["limits"]["tool_result_inline_bytes"] == 65536
        assert body["limits"]["turn_deadline_s"] == 900

        # Valid Metatron headers pass
        ok_hdrs = client.get(
            "/v1/metatron/capabilities",
            headers={
                "X-Metatron-Client": "metatron/0.1.0 (macos; arm64)",
                "X-Boss-Contract": "1",
            },
        )
        assert ok_hdrs.status_code == 200
        assert ok_hdrs.headers.get("x-boss-version") == app.version

        # Out-of-date X-Metatron-Client -> 426 Upgrade Required
        old_client = client.get(
            "/v1/metatron/capabilities",
            headers={"X-Metatron-Client": "metatron/0.0.9 (linux; x86_64)"},
        )
        assert old_client.status_code == 426
        assert old_client.headers.get("x-boss-version") == app.version
        err = old_client.json()["error"]
        assert err["code"] == "client_too_old"
        assert err["min_client"] == "0.1.0"
        assert err["retryable"] is False

        # Out-of-date X-Boss-Contract -> 426 Upgrade Required
        old_contract = client.get(
            "/v1/metatron/capabilities",
            headers={"X-Boss-Contract": "0"},
        )
        assert old_contract.status_code == 426
        assert old_contract.json()["error"]["code"] == "client_too_old"

    # Unauthenticated request fails 401 on /v1/metatron/capabilities under enforce
    with world.acting_as(None, mode="enforce"):
        unauth = client.get("/v1/metatron/capabilities")
        assert unauth.status_code == 401
        assert unauth.headers.get("x-boss-version") == app.version


def test_legacy_metatron_session_route_still_works_without_client_headers(
    client: TestClient, engine, monkeypatch
):
    from apps.api.app import main

    world = IdentityWorld.create(engine)
    owner = world.person("legacy-tui-owner")
    world.bind(owner, "owner", project=world.project_a)

    monkeypatch.setattr(
        main,
        "agent_turn",
        lambda payload, request, endpoint=None: main.AgentChatResponse(
            thread_id="athr_legacy_1",
            reply="legacy session ok",
            act_id="",
            actions=[],
            proposed_actions=[],
            next_steps=[],
            tool_calls=[],
            messages=[],
        ),
    )

    with world.acting_as(world.ctx(owner)):
        resp = client.post(
            "/v1/metatron/session",
            json={"project_id": world.project_a, "message": "status?", "actor": owner.id},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-boss-version") == app.version
    assert resp.json()["reply"] == "legacy session ok"
