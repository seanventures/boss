"""BOSS-469: a preview is a read, and its binding never substitutes for admission.

Only this test run's confined, disposable Postgres schema is used. No worker runs.
"""
from __future__ import annotations

import json
import time
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import authz
from boss_engine.services import work_agreement, workflow_custom_steps, workflows

DIRECTIVE = "Add a health endpoint with a focused test. Stop after opening a reviewable PR."
SPEC = {"entry_step": "decide", "steps": [{
    "id": "decide", "kind": "capability", "next": [],
    "binding": {"typed_decision": {"question": "Go?", "options": {"yes": "Go", "no": "Stop"}},
                "inputs": {"state": {"from_input": "directive"}}}}]}


@pytest.fixture
def agreement_world(engine):
    world = IdentityWorld.create(engine)
    owner = world.person("agreement-owner")
    binding = world.bind(owner, "owner", project=world.project_a)
    with engine.begin() as connection:
        definition = workflows.create_definition(connection, project_id=world.project_a,
            workflow_key="agreement_" + uuid4().hex[:8], name="Review this bounded change",
            description=None, actor=owner.id, namespace=workflows.PRODUCT_NAMESPACE)
        published = workflows.publish_version(connection, project_id=world.project_a,
            definition_id=definition["id"], actor=owner.id, spec=SPEC)
        mapping = workflows.create_mapping(connection, project_id=world.project_a,
            workflow_version_id=published["id"], selector_kind="work_class", selector_value="project.new",
            scope_type="project", scope_id=world.project_a, priority=0, actor=owner.id)
    return world, owner, binding, definition, published, mapping


def preview(client, world, owner, **changes):
    with world.acting_as(world.ctx(owner)):
        response = client.post("/v1/delivery-workflows/agreement", json={
            "project_id": world.project_a, "directive": DIRECTIVE, **changes})
    assert response.status_code == 200, response.text
    return response.json()


def start(client, world, owner, agreement, **changes):
    body = {"project_id": world.project_a, "work_class": "project.new", "input": agreement["input"],
            "agreement_content_hash": agreement["content_hash"],
            "agreement_binding_token": agreement["binding_token"], "start_anyway": agreement["start_anyway"], **changes}
    with world.acting_as(world.ctx(owner)):
        return client.post("/v1/delivery-workflows/start", json=body)


def effects(engine):
    with engine.connect() as connection:
        return {table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                for table in ("workflow_runs", "jobs", "events", "budget_reservations", "receipts", "api_idempotency_keys")}


def test_preview_is_typed_read_only_and_unknown_is_not_zero(client, engine, agreement_world):
    world, owner, _, _, published, _ = agreement_world
    before = effects(engine)
    agreement = preview(client, world, owner)
    assert effects(engine) == before
    assert agreement["input"] == work_agreement.first_input(DIRECTIVE)
    assert agreement["workflow_version_id"] == published["id"]
    assert agreement["entry_step"] == "decide"
    assert agreement["steps"][0]["next"] == []
    assert agreement["approved_spec"] is None
    assert agreement["selected_account"] is None
    assert agreement["selected_provider"] is None
    assert agreement["acceptance_criteria"] == []
    assert agreement["test_commands"] == []
    assert all(agreement["financial"][key] is None for key in
               ("estimate_usd_micros", "reservation_usd_micros", "authorized_limit_usd_micros", "history"))
    assert "subscription" in agreement["financial"]["history_basis"]
    assert "spec" not in agreement  # stored bindings are hashed, not leaked


def test_valid_confirmation_is_exact_version_and_one_intent(client, engine, agreement_world):
    world, owner, _, _, published, _ = agreement_world
    agreement = preview(client, world, owner)
    first = start(client, world, owner, agreement)
    assert first.status_code == 200, first.text
    second = start(client, world, owner, agreement)
    assert second.status_code == 200, second.text
    assert second.json()["run"]["id"] == first.json()["run"]["id"]
    assert second.json()["run"]["duplicate"] is True
    with engine.connect() as connection:
        row = connection.execute(text("SELECT workflow_version_id,input FROM workflow_runs WHERE id=:r"),
                                 {"r": first.json()["run"]["id"]}).mappings().one()
        assert row["workflow_version_id"] == published["id"]
        assert row["input"] == agreement["input"]
    assert effects(engine)["jobs"] == 0  # the preview and start do not call a provider
    with world.acting_as(world.ctx(owner)):
        recorded = client.get("/v1/delivery-workflows/runs", params={"project_id": world.project_a})
    assert recorded.status_code == 200, recorded.text
    observed = next(run for run in recorded.json()["runs"] if run["id"] == first.json()["run"]["id"])
    assert observed["project_id"] == world.project_a
    assert observed["dedupe_key"] == agreement["start_intent_key"]



def test_direct_preview_press_key_is_not_stored_or_replayed(client, engine, agreement_world):
    world, owner, *_ = agreement_world
    before = effects(engine)
    with world.acting_as(world.ctx(owner)):
        first = client.post("/v1/delivery-workflows/agreement", json={"project_id": world.project_a,
            "directive": DIRECTIVE}, headers={"Idempotency-Key": "same-read"})
        second = client.post("/v1/delivery-workflows/agreement", json={"project_id": world.project_a,
            "directive": DIRECTIVE + " Changed brief."}, headers={"Idempotency-Key": "same-read"})
    assert first.status_code == second.status_code == 200, second.text
    assert first.json()["content_hash"] != second.json()["content_hash"]
    assert "Idempotency-Replayed" not in second.headers
    assert effects(engine) == before


@pytest.mark.parametrize("change", [
    {"work_class": "project.fix"}, {"workflow_key": "some_other_path"},
    {"input": {"title": DIRECTIVE, "directive": DIRECTIVE + " Deploy it."}},
    {"input": {**work_agreement.first_input(DIRECTIVE), "repository": "hidden/target"}},
    {"input": {**work_agreement.first_input(DIRECTIVE), "adopt_spec_id": "hidden"}},
    {"input": {**work_agreement.first_input(DIRECTIVE), "preview_id": "hidden"}},
    {"start_anyway": ["github"]}, {"from_run": "old"}, {"from_discovery": "old"},
    {"replaces_run": "old"}, {"rerun_of": "old"}, {"sample": True},
])
def test_changed_intent_refuses_before_effects(client, engine, agreement_world, change):
    world, owner, *_ = agreement_world
    agreement = preview(client, world, owner)
    before = effects(engine)
    response = start(client, world, owner, agreement, **change)
    assert response.status_code in (409, 404), response.text
    assert effects(engine) == before, (response.status_code, response.json().get("detail"), effects(engine), before)


def test_workflow_mapping_change_invalidates_preview(client, engine, agreement_world):
    world, owner, _, definition, _, mapping = agreement_world
    agreement = preview(client, world, owner)
    with engine.begin() as connection:
        changed = json.loads(json.dumps(SPEC))
        changed["steps"][0]["binding"]["typed_decision"]["question"] = "Changed decision?"
        published = workflows.publish_version(connection, project_id=world.project_a,
            definition_id=definition["id"], actor=owner.id, spec=changed)
        connection.execute(text("UPDATE workflow_mappings SET workflow_version_id=:v WHERE id=:m"),
                           {"v": published["id"], "m": mapping["id"]})
    before = effects(engine)
    response = start(client, world, owner, agreement)
    assert response.status_code == 409, response.text
    assert effects(engine) == before


def test_current_repository_change_invalidates_preview(client, engine, agreement_world):
    world, owner, *_ = agreement_world
    connection_id, binding_id = "ghc_" + uuid4().hex, "ghb_" + uuid4().hex
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO github_connections (id,project_id,connection_label,auth_method,credential_ref,created_by) "
                                "VALUES (:id,:p,'agreement-test','personal_access_token','env:TEST_DO_NOT_RETURN',:a)"),
                           {"id": connection_id, "p": world.project_a, "a": owner.id})
        connection.execute(text("INSERT INTO github_repository_bindings (id,project_id,connection_id,owner,name,full_name,default_branch,created_by) "
                                "VALUES (:id,:p,:c,'owner','app','owner/app','main',:a)"),
                           {"id": binding_id, "p": world.project_a, "c": connection_id, "a": owner.id})
    agreement = preview(client, world, owner)
    assert agreement["repository"]["full_name"] == "owner/app"
    assert agreement["repository"]["base_branch"] == "main"
    assert agreement["repository"]["base_revision"] is None
    assert "TEST_DO_NOT_RETURN" not in json.dumps(agreement)
    assert "credential_ref" not in json.dumps(agreement)
    with engine.begin() as connection:
        connection.execute(text("UPDATE github_repository_bindings SET default_branch='release' WHERE id=:b"),
                           {"b": binding_id})
    before = effects(engine)
    response = start(client, world, owner, agreement)
    assert response.status_code == 409, response.text
    assert effects(engine) == before


@pytest.mark.parametrize("kind", ["expired", "signature", "content", "incomplete"])
def test_invalid_binding_refuses_before_effects(client, engine, agreement_world, monkeypatch, kind):
    world, owner, *_ = agreement_world
    agreement = preview(client, world, owner)
    if kind == "expired":
        monkeypatch.setattr(work_agreement.time, "time", lambda: agreement["expires_at_unix"])
    elif kind == "signature":
        parts = agreement["binding_token"].split(".")
        parts[3] = "0" * 64
        agreement["binding_token"] = ".".join(parts)
    elif kind == "content":
        agreement["content_hash"] = "0" * 64
    else:
        agreement["binding_token"] = None
    before = effects(engine)
    response = start(client, world, owner, agreement)
    assert response.status_code == 409, response.text
    assert effects(engine) == before


def test_binding_cannot_be_shared_between_principals_or_projects(client, engine, agreement_world):
    world, owner, *_ = agreement_world
    second = world.person("another-owner")
    world.bind(second, "owner", project=world.project_a)
    world.bind(owner, "owner", project=world.project_b)
    agreement = preview(client, world, owner)
    before = effects(engine)
    assert start(client, world, second, agreement).status_code == 409
    response = start(client, world, owner, agreement, project_id=world.project_b)
    assert response.status_code == 409, response.text
    assert effects(engine) == before


def test_authority_revoked_after_preview_still_refuses(client, engine, agreement_world):
    world, owner, binding, *_ = agreement_world
    agreement = preview(client, world, owner)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM identity_role_bindings WHERE id=:b"), {"b": binding})
        authz.bump_epoch(world.org, db=connection, reason="test.revoke")
    before = effects(engine)
    response = start(client, world, owner, agreement)
    assert response.status_code == 403, response.text
    assert effects(engine) == before


def test_viewer_can_read_but_binding_does_not_grant_start(client, engine, agreement_world):
    world, _, *_ = agreement_world
    viewer = world.person("agreement-viewer")
    world.bind(viewer, "viewer", project=world.project_a)
    agreement = preview(client, world, viewer)
    before = effects(engine)
    response = start(client, world, viewer, agreement)
    assert response.status_code == 403, response.text
    assert effects(engine) == before


def test_actor_spoof_and_cross_project_preview_are_denied(client, engine, agreement_world):
    world, owner, *_ = agreement_world
    before = effects(engine)
    with world.acting_as(world.ctx(owner)):
        assert client.post("/v1/delivery-workflows/agreement", json={"project_id": world.project_a,
            "directive": DIRECTIVE, "actor": "ceo"}).status_code == 403
        assert client.post("/v1/delivery-workflows/agreement", json={"project_id": world.project_b,
            "directive": DIRECTIVE}).status_code == 403
    assert effects(engine) == before


def test_stable_material_excludes_clock_and_statistical_history(client, engine, agreement_world, monkeypatch):
    world, owner, *_ = agreement_world
    now = int(time.time())
    monkeypatch.setattr(work_agreement.time, "time", lambda: now)
    first = preview(client, world, owner)
    monkeypatch.setattr(work_agreement.time, "time", lambda: now + 1)
    monkeypatch.setattr(work_agreement, "workflow_costs", lambda *a: {"costs": {first["workflow_key"]:
        {"runs": 2, "low_usd": 1.25, "high_usd": 2.25, "metered": False}}})
    second = preview(client, world, owner)
    assert first["content_hash"] == second["content_hash"]
    assert first["binding_token"] != second["binding_token"]
    assert second["financial"]["history"]["metered"] is False
    assert second["financial"]["estimate_usd_micros"] is None
    assert start(client, world, owner, first).status_code == 200


def test_malformed_unicode_mac_is_a_conflict_not_a_crash(engine, agreement_world):
    world, owner, *_ = agreement_world
    with engine.begin() as connection:
        with pytest.raises(work_agreement.AgreementConflict):
            work_agreement.validate(connection, main.settings, project_id=world.project_a, principal=owner.id,
                run_input=work_agreement.first_input(DIRECTIVE), work_class="project.new", workflow_key="",
                start_anyway=[], content_hash="0" * 64,
                binding_token=f"{int(time.time()) + 60}.{'0' * 64}.{'1' * 32}.☃")


def test_preview_refuses_unexpected_fields_and_missing_selection(client, engine, agreement_world):
    world, owner, *_ = agreement_world
    before = effects(engine)
    with world.acting_as(world.ctx(owner)):
        assert client.post("/v1/delivery-workflows/agreement", json={"project_id": world.project_a,
            "directive": DIRECTIVE, "grant_budget": 100}).status_code == 422
    world.bind(owner, "owner", project=world.project_b)
    with world.acting_as(world.ctx(owner)):
        response = client.post("/v1/delivery-workflows/agreement", json={"project_id": world.project_b,
            "directive": DIRECTIVE})
    assert response.status_code == 409, response.text
    assert effects(engine) == before


def test_a_fresh_preview_is_a_new_intent_even_in_the_same_second(client, agreement_world, monkeypatch):
    world, owner, *_ = agreement_world
    now = int(time.time())
    monkeypatch.setattr(work_agreement.time, "time", lambda: now)
    first = preview(client, world, owner)
    second = preview(client, world, owner)
    assert first["content_hash"] == second["content_hash"]
    assert first["binding_token"] != second["binding_token"]
    assert start(client, world, owner, first).status_code == 200
    assert start(client, world, owner, second).status_code == 200


def test_reconciliation_read_is_project_scoped_and_current_authority(client, engine, agreement_world):
    world, owner, binding, *_ = agreement_world
    agreement = preview(client, world, owner)
    assert start(client, world, owner, agreement).status_code == 200
    with world.acting_as(world.ctx(owner)):
        assert client.get("/v1/delivery-workflows/runs", params={"project_id": world.project_b}).status_code == 403
    world.bind(owner, "owner", project=world.project_b)
    with world.acting_as(world.ctx(owner)):
        foreign = client.get("/v1/delivery-workflows/runs", params={"project_id": world.project_b})
    assert foreign.status_code == 200, foreign.text
    assert foreign.json()["runs"] == []
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM identity_role_bindings WHERE id=:b"), {"b": binding})
        authz.bump_epoch(world.org, db=connection, reason="test.revoke-read")
    with world.acting_as(world.ctx(owner)):
        assert client.get("/v1/delivery-workflows/runs", params={"project_id": world.project_a}).status_code == 403


@pytest.mark.parametrize("changed", ["knowledge", "tool", "risk", "target"])
def test_editable_custom_step_is_bound_without_leaking_configuration(client, engine, agreement_world, changed):
    world, owner, _, definition, _, mapping = agreement_world
    server_id = "mcp_" + uuid4().hex
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO mcp_servers (id,project_id,name,url,risk_tier,provenance) "
            "VALUES (:s,:p,'agreement custom tracker','http://private-test-target.invalid/mcp',0,CAST(:provenance AS JSONB))"),
            {"s": server_id, "p": world.project_a, "provenance": json.dumps({"trusted": False})})
        connection.execute(text("INSERT INTO roles (id,project_id,role_key,display_name,spec) "
            "VALUES (:id,:p,'agreement_clerk','Agreement clerk','{}'::jsonb)"),
            {"id": "role_" + uuid4().hex, "p": world.project_a})
        connection.execute(text("INSERT INTO role_mcp_servers (id,project_id,role_key,mcp_server_id,tools,assigned_by) "
            "VALUES (:id,:p,'agreement_clerk',:s,CAST(:tools AS JSONB),:a)"),
            {"id": "rms_" + uuid4().hex, "p": world.project_a, "s": server_id,
             "tools": json.dumps(["file_issue"]), "a": owner.id})
        custom = workflow_custom_steps.create(connection, project_id=world.project_a, actor=owner.id,
            label="File bounded issue", summary="File the issue with the supplied tracker tool.",
            binding_kind="mcp_tool", mcp_server_id=server_id, mcp_tool_name="file_issue",
            knowledge="PRIVATE_KNOWLEDGE_SENTINEL", produces=["issue_id"])
        spec = {"entry_step": "file", "steps": [{"id": "file", "kind": "capability", "captain": "agreement_clerk", "next": [],
            "approval": {"mode": "manual"}, "binding": {"custom_step": custom["step_key"]}}]}
        version = workflows.publish_version(connection, project_id=world.project_a,
            definition_id=definition["id"], actor=owner.id, spec=spec)
        connection.execute(text("UPDATE workflow_mappings SET workflow_version_id=:v WHERE id=:m"),
            {"v": version["id"], "m": mapping["id"]})
    agreement = preview(client, world, owner)
    step = agreement["steps"][0]
    assert step["runs"] == "file_issue"
    assert step["approval"] == "manual"
    assert "has not been reviewed" in step["approval_note"]
    assert "PRIVATE_KNOWLEDGE_SENTINEL" not in json.dumps(agreement)
    assert "private-test-target.invalid" not in json.dumps(agreement)
    with engine.begin() as connection:
        if changed == "knowledge":
            connection.execute(text("UPDATE workflow_custom_steps SET knowledge='Changed instruction' WHERE id=:s"), {"s": custom["id"]})
        elif changed == "tool":
            connection.execute(text("UPDATE workflow_custom_steps SET mcp_tool_name='another_tool' WHERE id=:s"), {"s": custom["id"]})
        elif changed == "risk":
            connection.execute(text("UPDATE mcp_servers SET risk_tier=3 WHERE id=:s"), {"s": server_id})
        else:
            connection.execute(text("UPDATE mcp_servers SET url='http://changed-target.invalid/mcp' WHERE id=:s"), {"s": server_id})
    before = effects(engine)
    response = start(client, world, owner, agreement)
    assert response.status_code == 409, response.text
    assert effects(engine) == before
