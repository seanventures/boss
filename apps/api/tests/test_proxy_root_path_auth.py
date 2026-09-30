"""A reverse-proxy root path must not bypass API transport or MCP approval gates."""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.mcp.api_surface import operation_alias


PROJECT = "boss-dogfood"


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_proxy_prefixed_v1_path_still_requires_transport_auth(client, monkeypatch):
    key = f"human-{uuid4().hex[:10]}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:ceo")
    proxy = TestClient(api_main.app, root_path="/proxy")

    ordinary = client.get("/v1/auth/session")
    prefixed = proxy.get("/proxy/v1/auth/session")
    assert ordinary.status_code == 401, ordinary.text
    assert prefixed.status_code == 401, prefixed.text

    authenticated = proxy.get("/proxy/v1/auth/session", headers=_headers(key))
    assert authenticated.status_code == 200, authenticated.text
    assert authenticated.json()["transport_authenticated"] is True
    assert authenticated.json()["principal_member_id"] == "ceo"
    # A callback still reaches its own signature check behind the prefix.
    assert proxy.get("/proxy/v1/webhooks/github").status_code == 405


def test_proxy_prefixed_approval_uses_route_relative_confirmation_digest(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    mcp_key, human_key = f"mcp-{suffix}", f"human-{suffix}"
    agent = f"mcp_proxy_{suffix}"
    decision_id = f"dec_proxy_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{mcp_key},{human_key}")
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        f"{mcp_key}:{agent},{human_key}:ceo")
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=agent,
            display_name="Proxy MCP caller", member_type="agent",
            role_keys=[], permissions=["decision_resolve"], actor="ceo",
        )
        connection.execute(text("""
            INSERT INTO decisions (
                id, project_id, question, recommendation, options,
                risk_tier, default_action, status
            ) VALUES (
                :id, :project_id, 'Proxy approval review',
                '{"action":"manual_test"}'::jsonb,
                '[{"label":"Review","action":"review"}]'::jsonb,
                1, 'hold', 'pending'
            )
        """), {"id": decision_id, "project_id": PROJECT})

    proxy = TestClient(api_main.app, root_path="/proxy")
    path = f"/v1/decisions/{decision_id}/reject"
    prefixed_path = f"/proxy{path}"
    body = {"actor": agent, "note": "Human reviewed"}
    assert proxy.post(prefixed_path, json=body).status_code == 401
    direct = proxy.post(prefixed_path, headers=_headers(mcp_key), json=body)
    assert direct.status_code == 409, direct.text
    assert direct.json()["detail"] == "mcp_confirmation_required"
    assert client.post(path, headers=_headers(mcp_key), json=body).status_code == 409

    alias = operation_alias("POST", "/v1/decisions/{decision_id}/reject")
    requested = {
        "project_id": PROJECT, "operation": alias, "method": "POST",
        "path": path, "query": {}, "body": body,
    }
    proposal = client.post("/v1/mcp-confirmations", headers=_headers(mcp_key),
                           json=requested)
    assert proposal.status_code == 201, proposal.text
    confirmation = proposal.json()
    approved = proxy.post(
        f"/proxy/v1/mcp-confirmations/{confirmation['id']}/approve",
        headers=_headers(human_key), json={
            "project_id": PROJECT, "reviewed_digest": confirmation["request_digest"],
            "reviewed_request": requested,
        },
    )
    assert approved.status_code == 200, approved.text
    reviewed_headers = {
        **_headers(mcp_key), "X-BOSS-MCP-Operation": alias,
        "X-BOSS-MCP-Confirmation": confirmation["id"],
        "X-BOSS-MCP-Project": PROJECT,
    }
    effect = proxy.post(prefixed_path, headers=reviewed_headers, json=body)
    assert effect.status_code == 200, effect.text
    assert effect.json()["status"] == "rejected"
    assert client.post(path, headers=reviewed_headers, json=body).status_code == 409
    with engine.begin() as connection:
        event_actor = connection.execute(text("""
            SELECT actor FROM events WHERE project_id = :project_id
              AND entity_id = :id AND event_type = 'decision.rejected'
            ORDER BY created_at DESC LIMIT 1
        """), {"project_id": PROJECT, "id": decision_id}).scalar_one()
        assert event_actor == "ceo"
