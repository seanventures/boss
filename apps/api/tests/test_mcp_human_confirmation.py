"""A model's MCP key cannot manufacture approval for a sensitive effect."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.mcp.api_surface import operation_alias


PROJECT = "boss-dogfood"


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_exact_human_confirmation_is_scoped_single_use_and_audited(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    mcp_key, human_key, system_key = (f"mcp-{suffix}", f"human-{suffix}", f"system-{suffix}")
    mcp_member = f"mcp_member_{suffix}"
    new_member = f"new_member_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{mcp_key},{human_key},{system_key}")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{mcp_key}:{mcp_member},{human_key}:ceo,{system_key}:system",
    )
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=mcp_member,
            display_name="Dedicated MCP caller", member_type="agent",
            role_keys=[], permissions=["member_manage"], actor="ceo",
        )

    alias = operation_alias("POST", "/v1/members")
    path = "/v1/members"
    body = {
        "project_id": PROJECT, "actor": mcp_member,
        "member_id": new_member, "display_name": "Human-reviewed test member",
        "member_type": "agent", "role_keys": ["operator"], "permissions": ["task_create"],
    }
    requested = {
        "project_id": PROJECT, "operation": alias, "method": "POST",
        "path": path, "query": {}, "body": body,
    }
    proposal = client.post("/v1/mcp-confirmations", headers=_headers(mcp_key), json=requested)
    assert proposal.status_code == 201, proposal.text
    request = proposal.json()
    confirmation_id = request["id"]
    digest = request["request_digest"]
    assert request["status"] == "pending"
    assert request["requested_by"] == mcp_member
    assert digest and len(digest) == 64

    # The model's own key and a system member cannot be the human reviewer.
    approval = {"project_id": PROJECT, "reviewed_digest": digest,
                "reviewed_request": requested}
    assert client.post(f"/v1/mcp-confirmations/{confirmation_id}/approve",
                       headers=_headers(mcp_key), json=approval).status_code == 403
    assert client.post(f"/v1/mcp-confirmations/{confirmation_id}/approve",
                       headers=_headers(system_key), json=approval).status_code == 403
    assert client.post(f"/v1/mcp-confirmations/{confirmation_id}/approve",
                       headers=_headers(human_key), json={
                           **approval, "reviewed_digest": "0" * 64,
                       }).status_code == 409
    assert client.post(f"/v1/mcp-confirmations/{confirmation_id}/approve",
                       headers=_headers(human_key), json={
                           **approval, "reviewed_request": {
                               **requested, "body": {**body, "permissions": ["member_manage"]},
                           },
                       }).status_code == 409
    preview = client.get("/v1/mcp-confirmations", headers=_headers(human_key),
                         params={"project_id": PROJECT})
    assert preview.status_code == 200, preview.text
    assert confirmation_id in {item["id"] for item in preview.json()["items"]}
    assert client.post(f"/v1/mcp-confirmations/{confirmation_id}/approve",
                       headers=_headers(human_key), json=approval).status_code == 200

    effect_headers = {
        **_headers(mcp_key), "X-BOSS-MCP-Operation": alias,
        "X-BOSS-MCP-Confirmation": confirmation_id,
        "X-BOSS-MCP-Project": PROJECT,
    }
    assert client.post(path, headers={**_headers(mcp_key),
                                      "X-BOSS-MCP-Operation": alias},
                       json=body).status_code == 409
    # A changed request cannot consume the approval or create the member.
    assert client.post(path, headers=effect_headers,
                       json={**body, "permissions": ["member_manage"]}).status_code == 409
    with engine.begin() as connection:
        assert connection.execute(text("""
            SELECT count(*) FROM project_members WHERE member_id = :id
        """), {"id": new_member}).scalar_one() == 0
    effected = client.post(path, headers=effect_headers, json=body)
    assert effected.status_code == 201, effected.text
    assert effected.json()["member_id"] == new_member
    assert client.post(path, headers=effect_headers, json=body).status_code == 409
    with engine.begin() as connection:
        events = connection.execute(text("""
            SELECT event_type, actor, data FROM events
            WHERE entity_id = :id ORDER BY created_at
        """), {"id": confirmation_id}).mappings().all()
    assert {event["event_type"] for event in events} == {
        "mcp.confirmation_requested", "mcp.confirmation_approved",
        "mcp.confirmation_consumed", "mcp.confirmation_api_result",
    }
    assert next(event for event in events if event["event_type"] ==
                "mcp.confirmation_api_result")["data"]["status_code"] == 201
    assert {event["actor"] for event in events} == {mcp_member, "ceo"}


def test_confirmation_never_stores_secret_body_and_expires_closed(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    mcp_key, human_key = f"mcp-{suffix}", f"human-{suffix}"
    mcp_member = f"mcp_member_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{mcp_key},{human_key}")
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        f"{mcp_key}:{mcp_member},{human_key}:ceo")
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=mcp_member,
            display_name="Dedicated MCP caller", member_type="agent",
            role_keys=[], permissions=["member_manage"], actor="ceo",
        )
    alias = operation_alias("POST", "/v1/members")
    secret = "not-a-real-secret-98765"
    body = {
        "project_id": PROJECT, "actor": mcp_member,
        "member_id": f"new_member_{suffix}", "display_name": "Secret test member",
        "member_type": "agent", "role_keys": [], "permissions": ["task_create"],
        "note": secret, "auth": {"api_key": secret},
    }
    requested = {
        "project_id": PROJECT, "operation": alias, "method": "POST",
        "path": "/v1/members", "query": {}, "body": body,
    }
    proposal = client.post("/v1/mcp-confirmations", headers=_headers(mcp_key), json=requested)
    assert proposal.status_code == 201, proposal.text
    assert secret not in proposal.text
    confirmation_id = proposal.json()["id"]
    with engine.begin() as connection:
        stored = connection.execute(text("""
            SELECT preview::text, request_digest FROM mcp_confirmations WHERE id = :id
        """), {"id": confirmation_id}).mappings().one()
        connection.execute(text("""
            UPDATE mcp_confirmations SET expires_at = clock_timestamp() - INTERVAL '1 second'
            WHERE id = :id
        """), {"id": confirmation_id})
    assert secret not in stored["preview"]
    assert stored["request_digest"] == proposal.json()["request_digest"]
    response = client.post(f"/v1/mcp-confirmations/{confirmation_id}/approve",
                           headers=_headers(human_key), json={
                               "project_id": PROJECT,
                               "reviewed_digest": stored["request_digest"],
                               "reviewed_request": requested,
                           })
    assert response.status_code == 409
    assert response.json()["detail"] == "confirmation_expired"


def test_multi_project_mcp_key_cannot_request_cross_project_confirmation(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    mcp_key = f"mcp-{suffix}"
    mcp_member = f"mcp_member_{suffix}"
    other_project = f"other-{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", mcp_key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{mcp_key}:{mcp_member}")
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=mcp_member,
            display_name="Multi-project MCP caller", member_type="agent",
            role_keys=[], permissions=["decision_resolve"], actor="ceo",
        )
        workspace_id = connection.execute(text("""
            SELECT workspace_id FROM projects WHERE id = :id
        """), {"id": PROJECT}).scalar_one()
        connection.execute(text("""
            INSERT INTO projects (id, workspace_id, name, slug)
            VALUES (:id, :workspace_id, 'Other test project', :slug)
        """), {"id": other_project, "workspace_id": workspace_id, "slug": other_project})
        api_main.members.upsert_member(
            connection, project_id=other_project, member_id=mcp_member,
            display_name="Multi-project MCP caller", member_type="agent",
            role_keys=[], permissions=["decision_resolve"], actor="ceo",
        )
    alias = operation_alias("POST", "/v1/members")
    response = client.post("/v1/mcp-confirmations", headers=_headers(mcp_key), json={
        "project_id": PROJECT, "operation": alias, "method": "POST",
        "path": "/v1/members", "query": {},
        "body": {"actor": mcp_member, "project_id": other_project},
    })
    assert response.status_code == 403
    assert response.json()["detail"] == "exclusive_mcp_project_required"
    with engine.begin() as connection:
        assert connection.execute(text("""
            SELECT count(*) FROM mcp_confirmations WHERE requested_by = :member
        """), {"member": mcp_member}).scalar_one() == 0


def test_mcp_confirmation_headers_fail_when_transport_auth_is_disabled(
    client, engine, monkeypatch,
):
    monkeypatch.setattr(api_main.settings, "require_api_auth", False)
    monkeypatch.setattr(api_main.settings, "oidc_enabled", False)
    monkeypatch.setattr(api_main.settings, "enterprise_saml_enabled", False)
    alias = operation_alias("POST", "/v1/members")
    response = client.post("/v1/members", headers={
        "X-BOSS-MCP-Operation": alias,
        "X-BOSS-MCP-Confirmation": "mcpconf_" + "a" * 20,
        "X-BOSS-MCP-Project": PROJECT,
    }, json={"project_id": PROJECT, "actor": "ceo", "member_id": "nope",
             "display_name": "No auth"})
    assert response.status_code == 403
    assert response.json()["detail"] == "mcp_auth_required"
    with engine.begin() as connection:
        assert connection.execute(text("""
            SELECT count(*) FROM project_members WHERE project_id = :project_id
              AND member_id = 'nope'
        """), {"project_id": PROJECT}).scalar_one() == 0
