"""A model's MCP key cannot manufacture approval for a sensitive effect."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
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


@pytest.mark.parametrize(
    ("action", "initial_status", "extra", "expected_status", "event_type"),
    [
        ("reject", "pending", {"note": "Keep this on hold"}, "rejected", "decision.rejected"),
        ("revise", "pending", {"note": "Add evidence"}, "revision_requested",
         "decision.revision_requested"),
        ("delegate", "pending", {"delegate_to": "release_manager"}, "delegated",
         "decision.delegated"),
        ("execute", "approved", {"note": "Recorded manually"}, "executed",
         "decision.executed"),
    ],
)
def test_confirmed_mcp_decision_records_human_reviewer_as_actor(
    client, engine, monkeypatch, action, initial_status, extra, expected_status, event_type,
):
    suffix = uuid4().hex[:10]
    mcp_key, human_key = f"mcp-{suffix}", f"human-{suffix}"
    mcp_member = f"mcp_member_{suffix}"
    decision_id = f"dec_mcp_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{mcp_key},{human_key}")
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        f"{mcp_key}:{mcp_member},{human_key}:ceo")
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=mcp_member,
            display_name="Dedicated MCP caller", member_type="agent",
            role_keys=[], permissions=["decision_resolve"], actor="ceo",
        )
        connection.execute(text("""
            INSERT INTO decisions (
                id, project_id, question, recommendation, options,
                risk_tier, default_action, status, approved_by
            ) VALUES (
                :id, :project_id, 'Review MCP attribution',
                '{"action":"manual_test"}'::jsonb,
                '[{"label":"Review","action":"review"}]'::jsonb,
                1, 'hold', :status, :approved_by
            )
        """), {"id": decision_id, "project_id": PROJECT,
               "status": initial_status,
               "approved_by": "ceo" if initial_status == "approved" else None})

    template = f"/v1/decisions/{{decision_id}}/{action}"
    path = f"/v1/decisions/{decision_id}/{action}"
    alias = operation_alias("POST", template)
    body = {"actor": mcp_member, **extra}
    requested = {"project_id": PROJECT, "operation": alias, "method": "POST",
                 "path": path, "query": {}, "body": body}
    # The key is still an API key. Omitting MCP headers must not turn it into a human
    # approval credential for the same route.
    direct = client.post(path, headers=_headers(mcp_key), json=body)
    assert direct.status_code == 409, direct.text
    assert direct.json()["detail"] == "mcp_confirmation_required"
    with engine.begin() as connection:
        assert connection.execute(text("""
            SELECT status FROM decisions WHERE id = :id
        """), {"id": decision_id}).scalar_one() == initial_status
    proposed = client.post("/v1/mcp-confirmations", headers=_headers(mcp_key),
                           json=requested)
    assert proposed.status_code == 201, proposed.text
    confirmation_id = proposed.json()["id"]
    assert proposed.json()["status"] == "pending"
    assert proposed.json()["preview"]["effect_actor"] == "approving_human"
    assert client.post(path, headers={**_headers(mcp_key),
                                      "X-BOSS-MCP-Operation": alias},
                       json=body).status_code == 409
    approval = client.post(f"/v1/mcp-confirmations/{confirmation_id}/approve",
                           headers=_headers(human_key), json={
                               "project_id": PROJECT,
                               "reviewed_digest": proposed.json()["request_digest"],
                               "reviewed_request": requested,
                           })
    assert approval.status_code == 200, approval.text
    assert approval.json()["approved_by"] == "ceo"
    headers = {**_headers(mcp_key), "X-BOSS-MCP-Operation": alias,
               "X-BOSS-MCP-Confirmation": confirmation_id,
               "X-BOSS-MCP-Project": PROJECT}
    assert client.post(path, headers=headers,
                       json={**body, "actor": "ceo"}).status_code == 409
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected_status
    assert client.post(path, headers=headers, json=body).status_code == 409
    with engine.begin() as connection:
        event = connection.execute(text("""
            SELECT actor FROM events WHERE project_id = :project_id
              AND entity_id = :decision_id AND event_type = :event_type
            ORDER BY created_at DESC LIMIT 1
        """), {"project_id": PROJECT, "decision_id": decision_id,
               "event_type": event_type}).mappings().first()
        assert event is not None
        assert event["actor"] == "ceo"
        confirmation = connection.execute(text("""
            SELECT status, requested_by, approved_by FROM mcp_confirmations WHERE id = :id
        """), {"id": confirmation_id}).mappings().one()
        assert dict(confirmation) == {"status": "consumed",
                                      "requested_by": mcp_member,
                                      "approved_by": "ceo"}


def test_revoked_human_reviewer_cannot_authorize_a_confirmed_mcp_effect(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    mcp_key, human_key = f"mcp-{suffix}", f"human-{suffix}"
    mcp_member = f"mcp_member_{suffix}"
    decision_id = f"dec_mcp_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{mcp_key},{human_key}")
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        f"{mcp_key}:{mcp_member},{human_key}:ceo")
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=mcp_member,
            display_name="Dedicated MCP caller", member_type="agent",
            role_keys=[], permissions=["decision_resolve"], actor="ceo",
        )
        connection.execute(text("""
            INSERT INTO decisions (
                id, project_id, question, recommendation, options,
                risk_tier, default_action, status
            ) VALUES (
                :id, :project_id, 'Review revoked MCP attribution',
                '{"action":"manual_test"}'::jsonb,
                '[{"label":"Review","action":"review"}]'::jsonb,
                1, 'hold', 'pending'
            )
        """), {"id": decision_id, "project_id": PROJECT})
    template = "/v1/decisions/{decision_id}/reject"
    path = f"/v1/decisions/{decision_id}/reject"
    alias = operation_alias("POST", template)
    body = {"actor": mcp_member, "note": "Decline"}
    requested = {"project_id": PROJECT, "operation": alias, "method": "POST",
                 "path": path, "query": {}, "body": body}
    proposed = client.post("/v1/mcp-confirmations", headers=_headers(mcp_key),
                           json=requested)
    assert proposed.status_code == 201, proposed.text
    confirmation_id = proposed.json()["id"]
    approved = client.post(f"/v1/mcp-confirmations/{confirmation_id}/approve",
                           headers=_headers(human_key), json={
                               "project_id": PROJECT,
                               "reviewed_digest": proposed.json()["request_digest"],
                               "reviewed_request": requested,
                           })
    assert approved.status_code == 200, approved.text
    with engine.begin() as connection:
        connection.execute(text("""
            UPDATE project_members SET status = 'inactive'
            WHERE project_id = :project_id AND member_id = 'ceo'
        """), {"project_id": PROJECT})
        # The seeded CEO also has org membership. Revoke that authority too;
        # an active org member is intentionally valid for a project.
        connection.execute(text("""
            UPDATE org_members SET status = 'inactive'
            WHERE member_id = 'ceo' AND org_id = (
                SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
            )
        """), {"project_id": PROJECT})
    response = client.post(path, headers={
        **_headers(mcp_key), "X-BOSS-MCP-Operation": alias,
        "X-BOSS-MCP-Confirmation": confirmation_id,
        "X-BOSS-MCP-Project": PROJECT,
    }, json=body)
    assert response.status_code == 403, response.text
    with engine.begin() as connection:
        assert connection.execute(text("""
            SELECT status FROM decisions WHERE id = :id
        """), {"id": decision_id}).scalar_one() == "pending"
        assert connection.execute(text("""
            SELECT status FROM mcp_confirmations WHERE id = :id
        """), {"id": confirmation_id}).scalar_one() == "approved"


def test_human_confirmation_does_not_grant_the_mcp_principal_missing_permission(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    mcp_key = f"mcp-{suffix}"
    mcp_member = f"mcp_member_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", mcp_key)
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        f"{mcp_key}:{mcp_member}")
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=mcp_member,
            display_name="Dedicated MCP caller", member_type="agent",
            role_keys=[], permissions=[], actor="ceo",
        )
    path = "/v1/decisions/dec_unreachable/reject"
    response = client.post("/v1/mcp-confirmations", headers=_headers(mcp_key),
                           json={
                               "project_id": PROJECT,
                               "operation": operation_alias(
                                   "POST", "/v1/decisions/{decision_id}/reject"
                               ),
                               "method": "POST", "path": path, "query": {},
                               "body": {"actor": mcp_member, "note": "Decline"},
                           })
    assert response.status_code == 403, response.text
    with engine.begin() as connection:
        assert connection.execute(text("""
            SELECT count(*) FROM mcp_confirmations WHERE requested_by = :member
        """), {"member": mcp_member}).scalar_one() == 0


def test_mcp_cannot_propose_a_human_attributed_effect_with_a_spoofed_actor(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    mcp_key = f"mcp-{suffix}"
    mcp_member = f"mcp_member_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", mcp_key)
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        f"{mcp_key}:{mcp_member}")
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=mcp_member,
            display_name="Dedicated MCP caller", member_type="agent",
            role_keys=[], permissions=["decision_resolve"], actor="ceo",
        )
    response = client.post("/v1/mcp-confirmations", headers=_headers(mcp_key),
                           json={
                               "project_id": PROJECT,
                               "operation": operation_alias(
                                   "POST", "/v1/decisions/{decision_id}/reject"
                               ),
                               "method": "POST",
                               "path": "/v1/decisions/dec_unreachable/reject",
                               "query": {},
                               "body": {"actor": "ceo", "note": "Decline"},
                           })
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "mcp_actor_mismatch"
    with engine.begin() as connection:
        assert connection.execute(text("""
            SELECT count(*) FROM mcp_confirmations WHERE requested_by = :member
        """), {"member": mcp_member}).scalar_one() == 0


def test_confirmed_mcp_changeset_approval_persists_the_human_actor(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    mcp_key, human_key = f"mcp-{suffix}", f"human-{suffix}"
    mcp_member = f"mcp_member_{suffix}"
    changeset_id = f"chg_mcp_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{mcp_key},{human_key}")
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        f"{mcp_key}:{mcp_member},{human_key}:ceo")
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=mcp_member,
            display_name="Dedicated MCP caller", member_type="agent",
            role_keys=[], permissions=["providerops_manage"], actor="ceo",
        )
        connection.execute(text("""
            INSERT INTO changesets (
                id, project_id, title, summary, status, risk_tier, created_by
            ) VALUES (
                :id, :project_id, 'MCP approval attribution',
                'Local approval only; no provider action',
                'pending_approval', 1, 'ceo'
            )
        """), {"id": changeset_id, "project_id": PROJECT})
    template = "/v1/changesets/{changeset_id}/approve"
    path = f"/v1/changesets/{changeset_id}/approve"
    alias = operation_alias("POST", template)
    body = {"actor": mcp_member}
    requested = {"project_id": PROJECT, "operation": alias, "method": "POST",
                 "path": path, "query": {}, "body": body}
    direct = client.post(path, headers=_headers(mcp_key), json=body)
    assert direct.status_code == 409, direct.text
    assert direct.json()["detail"] == "mcp_confirmation_required"
    with engine.begin() as connection:
        assert connection.execute(text("""
            SELECT status FROM changesets WHERE id = :id
        """), {"id": changeset_id}).scalar_one() == "pending_approval"
    proposal = client.post("/v1/mcp-confirmations", headers=_headers(mcp_key),
                           json=requested)
    assert proposal.status_code == 201, proposal.text
    confirmation_id = proposal.json()["id"]
    review = client.post(f"/v1/mcp-confirmations/{confirmation_id}/approve",
                         headers=_headers(human_key), json={
                             "project_id": PROJECT,
                             "reviewed_digest": proposal.json()["request_digest"],
                             "reviewed_request": requested,
                         })
    assert review.status_code == 200, review.text
    effect_headers = {**_headers(mcp_key), "X-BOSS-MCP-Operation": alias,
                      "X-BOSS-MCP-Confirmation": confirmation_id,
                      "X-BOSS-MCP-Project": PROJECT}
    effected = client.post(path, headers=effect_headers, json=body)
    assert effected.status_code == 200, effected.text
    assert effected.json()["status"] == "approved"
    assert effected.json()["approved_by"] == "ceo"
    assert client.post(path, headers=effect_headers, json=body).status_code == 409
    with engine.begin() as connection:
        persisted = connection.execute(text("""
            SELECT status, approved_by FROM changesets WHERE id = :id
        """), {"id": changeset_id}).mappings().one()
        assert dict(persisted) == {"status": "approved", "approved_by": "ceo"}
        event_actor = connection.execute(text("""
            SELECT actor FROM events WHERE project_id = :project_id
              AND entity_id = :id AND event_type = 'changeset.approved'
            ORDER BY created_at DESC LIMIT 1
        """), {"project_id": PROJECT, "id": changeset_id}).scalar_one()
        assert event_actor == "ceo"


@pytest.mark.parametrize("principal", ["system", "executive_console"])
def test_stock_nonhuman_api_keys_cannot_bypass_decision_review(
    client, engine, monkeypatch, principal,
):
    key = f"stock-{uuid4().hex[:10]}"
    decision_id = f"dec_stock_{uuid4().hex[:10]}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{principal}")
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO decisions (
                id, project_id, question, recommendation, options,
                risk_tier, default_action, status
            ) VALUES (
                :id, :project_id, 'Stock nonhuman approval test',
                '{"action":"manual_test"}'::jsonb,
                '[{"label":"Review","action":"review"}]'::jsonb,
                1, 'hold', 'pending'
            )
        """), {"id": decision_id, "project_id": PROJECT})
    path = f"/v1/decisions/{decision_id}/reject"
    direct = client.post(path, headers=_headers(key), json={"actor": principal})
    assert direct.status_code == 409, direct.text
    assert direct.json()["detail"] == "mcp_confirmation_required"
    held = client.post(f"/v1/decisions/{decision_id}/approve",
                       headers=_headers(key), json={"actor": principal})
    assert held.status_code == 409, held.text
    assert held.json()["detail"] == "mcp_operation_held"
    with engine.begin() as connection:
        assert connection.execute(text("""
            SELECT status FROM decisions WHERE id = :id
        """), {"id": decision_id}).scalar_one() == "pending"


def test_human_api_key_still_uses_the_normal_decision_route(client, engine, monkeypatch):
    key = f"human-{uuid4().hex[:10]}"
    decision_id = f"dec_human_{uuid4().hex[:10]}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:ceo")
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO decisions (
                id, project_id, question, recommendation, options,
                risk_tier, default_action, status
            ) VALUES (
                :id, :project_id, 'Human API approval test',
                '{"action":"manual_test"}'::jsonb,
                '[{"label":"Review","action":"review"}]'::jsonb,
                1, 'hold', 'pending'
            )
        """), {"id": decision_id, "project_id": PROJECT})
    response = client.post(f"/v1/decisions/{decision_id}/reject",
                           headers=_headers(key), json={"actor": "ceo"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"


@pytest.mark.parametrize("auth_method", ["oidc", "saml"])
@pytest.mark.parametrize("principal,expected_status", [("system", 409), ("ceo", 200)])
def test_session_principal_type_controls_direct_decision_approval(
    client, engine, monkeypatch, auth_method, principal, expected_status,
):
    decision_id = f"dec_session_{uuid4().hex[:10]}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "")
    monkeypatch.setattr(api_main.settings, "api_key_principals", "")
    if auth_method == "oidc":
        monkeypatch.setattr(api_main.settings, "oidc_enabled", True)
        monkeypatch.setattr(
            api_main.oidc_auth, "verify_bearer_token",
            lambda _token: SimpleNamespace(member_id=principal),
        )
    else:
        monkeypatch.setattr(api_main.settings, "enterprise_saml_enabled", True)
        monkeypatch.setattr(
            api_main.saml_sessions, "authenticate_token",
            lambda _connection, _token: {"member_id": principal},
        )
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO decisions (
                id, project_id, question, recommendation, options,
                risk_tier, default_action, status
            ) VALUES (
                :id, :project_id, 'Session principal approval test',
                '{"action":"manual_test"}'::jsonb,
                '[{"label":"Review","action":"review"}]'::jsonb,
                1, 'hold', 'pending'
            )
        """), {"id": decision_id, "project_id": PROJECT})
    response = client.post(f"/v1/decisions/{decision_id}/reject",
                           headers=_headers("oidc-test-token"),
                           json={"actor": principal})
    assert response.status_code == expected_status, response.text
    if expected_status == 409:
        assert response.json()["detail"] == "mcp_confirmation_required"
    with engine.begin() as connection:
        assert connection.execute(text("""
            SELECT status FROM decisions WHERE id = :id
        """), {"id": decision_id}).scalar_one() == (
            "rejected" if expected_status == 200 else "pending"
        )


@pytest.mark.parametrize("path,service_name", [
    ("/v1/github/merge-authorizations/mga_missing/authorize", "authorize"),
    ("/v1/github/resolution-plans/grp_missing/approve", "approve_resolution"),
])
def test_github_approval_routes_reject_direct_agent_http_without_review(
    client, engine, monkeypatch, path, service_name,
):
    key = f"mcp-{uuid4().hex[:10]}"
    principal = f"mcp_github_{uuid4().hex[:10]}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{principal}")
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=principal,
            display_name="GitHub MCP caller", member_type="agent",
            role_keys=[], permissions=["github_manage"], actor="ceo",
        )
    service = (api_main.github_merge_authorizations if service_name == "authorize"
               else api_main.github_code_sync)
    called = []
    monkeypatch.setattr(service, service_name, lambda *args, **kwargs: called.append(True))
    response = client.post(path, headers=_headers(key), json={
        "project_id": PROJECT, "actor": principal,
        "review_evidence": {"verdict": "approved", "summary": "Review"},
    })
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "mcp_confirmation_required"
    assert called == []


@pytest.mark.parametrize("template,path,service_name,missing_error", [
    (
        "/v1/github/merge-authorizations/{authorization_id}/authorize",
        "/v1/github/merge-authorizations/mga_missing/authorize",
        "authorize", api_main.GitHubMergeAuthorizationNotFoundError,
    ),
    (
        "/v1/github/resolution-plans/{plan_id}/approve",
        "/v1/github/resolution-plans/grp_missing/approve",
        "approve_resolution", api_main.GitHubResolutionPlanNotFoundError,
    ),
])
def test_confirmed_github_approval_routes_pass_human_actor_to_domain_service(
    client, engine, monkeypatch, template, path, service_name, missing_error,
):
    suffix = uuid4().hex[:10]
    mcp_key, human_key = f"mcp-{suffix}", f"human-{suffix}"
    principal = f"mcp_github_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{mcp_key},{human_key}")
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        f"{mcp_key}:{principal},{human_key}:ceo")
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=principal,
            display_name="GitHub MCP caller", member_type="agent",
            role_keys=[], permissions=["github_manage"], actor="ceo",
        )
    service = (api_main.github_merge_authorizations if service_name == "authorize"
               else api_main.github_code_sync)
    observed_actors = []

    def observe_and_fail(_connection, **kwargs):
        observed_actors.append(kwargs["actor"])
        raise missing_error("Missing fixture target")

    monkeypatch.setattr(service, service_name, observe_and_fail)
    body = {
        "project_id": PROJECT, "actor": principal,
        "review_evidence": {"verdict": "approved", "summary": "Independent review"},
    }
    alias = operation_alias("POST", template)
    requested = {
        "project_id": PROJECT, "operation": alias, "method": "POST",
        "path": path, "query": {}, "body": body,
    }
    proposed = client.post("/v1/mcp-confirmations", headers=_headers(mcp_key),
                           json=requested)
    assert proposed.status_code == 201, proposed.text
    confirmation = proposed.json()
    approved = client.post(
        f"/v1/mcp-confirmations/{confirmation['id']}/approve",
        headers=_headers(human_key), json={
            "project_id": PROJECT, "reviewed_digest": confirmation["request_digest"],
            "reviewed_request": requested,
        },
    )
    assert approved.status_code == 200, approved.text
    effect = client.post(path, headers={
        **_headers(mcp_key), "X-BOSS-MCP-Operation": alias,
        "X-BOSS-MCP-Confirmation": confirmation["id"],
        "X-BOSS-MCP-Project": PROJECT,
    }, json=body)
    assert effect.status_code == 404, effect.text
    assert observed_actors == ["ceo"]
