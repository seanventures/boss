"""MCP may remove project-local planning metadata only after human review."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.mcp.api_surface import operation_alias
from boss_engine.services import org_assignments
from boss_engine.services.meeting_service import upsert_meeting_role


PROJECT = "boss-dogfood"


@pytest.mark.parametrize(
    ("template", "permission", "event_type", "success_status"),
    [
        ("/v1/meeting-roles/{role_key}", "workflow_manage", "meeting_role.deleted", 204),
        ("/v1/console/v2/org/{agent_id}", "role_review", "org.assignment_cleared", 200),
    ],
)
def test_project_cleanup_requires_exact_human_confirmation_and_keeps_actor(
    client, engine, monkeypatch, template, permission, event_type, success_status,
):
    suffix = uuid4().hex[:10]
    mcp_key, human_key = f"mcp-{suffix}", f"human-{suffix}"
    mcp_member = f"mcp_{suffix}"
    item_id = f"reviewed_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{mcp_key},{human_key}")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{mcp_key}:{mcp_member},{human_key}:ceo",
    )
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=mcp_member,
            display_name="Dedicated MCP caller", member_type="agent",
            role_keys=[], permissions=[permission], actor="ceo",
        )
        if permission == "workflow_manage":
            upsert_meeting_role(
                connection, PROJECT, key=item_id, title="Reviewed role", lens="Inspect work",
            )
            exists_sql = "SELECT count(*) FROM meeting_roles WHERE project_id = :project_id AND role_key = :item_id"
        else:
            org_assignments.set_one(connection, PROJECT, item_id, actor="ceo")
            exists_sql = "SELECT count(*) FROM org_assignments WHERE project_id = :project_id AND agent_id = :item_id"

    path = template.replace("{role_key}", item_id).replace("{agent_id}", item_id)
    alias = operation_alias("DELETE", template)
    query = {"project_id": PROJECT, "actor": mcp_member}
    mcp_headers = {"Authorization": f"Bearer {mcp_key}"}
    human_headers = {"Authorization": f"Bearer {human_key}"}
    direct = client.delete(path, headers=mcp_headers, params=query)
    assert direct.status_code == 409, direct.text
    assert direct.json()["detail"] == "mcp_confirmation_required"

    request = {
        "project_id": PROJECT, "operation": alias, "method": "DELETE",
        "path": path, "query": query, "body": None,
    }
    proposal = client.post("/v1/mcp-confirmations", headers=mcp_headers, json=request)
    assert proposal.status_code == 201, proposal.text
    confirmation_id = proposal.json()["id"]
    approval = client.post(
        f"/v1/mcp-confirmations/{confirmation_id}/approve", headers=human_headers,
        json={
            "project_id": PROJECT,
            "reviewed_digest": proposal.json()["request_digest"],
            "reviewed_request": request,
        },
    )
    assert approval.status_code == 200, approval.text
    effect_headers = {
        **mcp_headers, "X-BOSS-MCP-Operation": alias,
        "X-BOSS-MCP-Confirmation": confirmation_id,
        "X-BOSS-MCP-Project": PROJECT,
    }
    # The human approved the exact principal and project, not an interchangeable URL.
    changed = client.delete(
        path, headers=effect_headers, params={**query, "actor": "ceo"},
    )
    assert changed.status_code == 409, changed.text
    with engine.begin() as connection:
        assert connection.execute(
            text(exists_sql), {"project_id": PROJECT, "item_id": item_id},
        ).scalar_one() == 1

    effected = client.delete(path, headers=effect_headers, params=query)
    assert effected.status_code == success_status, effected.text
    replay = client.delete(path, headers=effect_headers, params=query)
    assert replay.status_code == 409, replay.text
    with engine.begin() as connection:
        assert connection.execute(
            text(exists_sql), {"project_id": PROJECT, "item_id": item_id},
        ).scalar_one() == 0
        event_actor = connection.execute(text("""
            SELECT actor FROM events
            WHERE project_id = :project_id AND entity_id = :item_id
              AND event_type = :event_type
            ORDER BY created_at DESC LIMIT 1
        """), {
            "project_id": PROJECT, "item_id": item_id, "event_type": event_type,
        }).scalar_one()
        assert event_actor == mcp_member
