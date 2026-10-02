"""Community Discovery cannot start unleased model work or replay uncertain spend."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.config import get_settings
from apps.api.app.services import discovery_service
from boss_engine.context import EngineContext
from boss_engine.tools import build_default_registry


PROJECT = "boss-dogfood"


def test_service_rejects_community_discovery_before_engine_or_model(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "boss_edition", "community")

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("Community Discovery reached its engine or model")

    monkeypatch.setattr(discovery_service, "get_engine", must_not_run)
    monkeypatch.setattr(discovery_service, "make_code_agent", must_not_run)
    with pytest.raises(discovery_service.DiscoveryExecutionBlocked):
        discovery_service.start_discovery(
            project_id=PROJECT, title="Blocked", seed="research this",
            context="", settings=settings,
        )
    tool_result = build_default_registry().invoke(
        "create_discovery",
        EngineContext(settings=settings, engine=None),
        project_id=PROJECT, actor="ceo", args={"seed": "research this"},
        operator_confirmed=True,
    )
    assert tool_result["ok"] is False
    assert "DiscoveryExecutionBlocked" in tool_result["error"]


def test_authorized_community_start_and_deepen_make_no_run_or_model_call(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    actor = f"discovery_gate_{suffix}"
    key = f"discovery-gate-key-{suffix}"
    parent_id = f"drun_parent_{suffix}"
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{actor}")
    headers = {"Authorization": f"Bearer {key}"}

    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=actor,
            display_name="Discovery gate test", member_type="human", role_keys=[],
            permissions=["workflow_execute"], actor="ceo",
        )
        connection.execute(
            text("INSERT INTO discovery_runs "
                 "(id, project_id, status, phase, title, seed) "
                 "VALUES (:id, :project_id, 'completed', 'done', 'Existing', 'research this')"),
            {"id": parent_id, "project_id": PROJECT},
        )

    calls = []
    monkeypatch.setattr(discovery_service, "start_discovery",
                        lambda **kwargs: calls.append(kwargs) or "unexpected")
    for path, body in (
        ("/v1/discoveries", {"project_id": PROJECT, "seed": "new idea"}),
        (f"/v1/discoveries/{parent_id}/deepen", {}),
    ):
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 503, (path, response.text)
        assert "durable" in response.json()["detail"]
    assert calls == []
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM discovery_runs WHERE project_id = :project_id"),
            {"project_id": PROJECT},
        ).scalar_one() == 1


def test_community_startup_blocks_interrupted_replay_and_records_reconciliation(
    engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    run_id = f"drun_interrupted_{suffix}"
    settings = get_settings()
    monkeypatch.setattr(settings, "boss_edition", "community")
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO discovery_runs "
                 "(id, project_id, status, phase, title, seed, state, usage) "
                 "VALUES (:id, :project_id, 'running', 'synthesize', 'Interrupted', "
                 "'research this', CAST(:state AS jsonb), CAST(:usage AS jsonb))"),
            {"id": run_id, "project_id": PROJECT,
             "state": '{"governance":{"reservation_id":"res_unknown",'
                      '"receipt_id":"rcpt_unknown"}}',
             "usage": '{"cost_usd_micros":1000}'},
        )

    def no_model(*_args, **_kwargs):
        raise AssertionError("Community startup retried uncertain model spend")

    monkeypatch.setattr(discovery_service, "make_code_agent", no_model)
    result = discovery_service.resume_interrupted(settings)
    assert result["resumed"] == 0
    assert result["blocked"] == 1
    assert result["blocked_run_ids"] == [run_id]
    assert discovery_service.resume_interrupted(settings)["blocked"] == 0
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT status, error FROM discovery_runs WHERE id = :id"),
            {"id": run_id},
        ).mappings().one()
        assert row["status"] == "failed"
        assert "durable" in row["error"]
        assert connection.execute(
            text("SELECT count(*) FROM events WHERE entity_id = :id "
                 "AND event_type = 'discovery.blocked'"),
            {"id": run_id},
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT count(*) FROM decisions WHERE project_id = :project_id "
                 "AND question = :question AND status = 'pending'"),
            {"project_id": PROJECT,
             "question": f"Reconcile interrupted Discovery research {run_id}"},
        ).scalar_one() == 1
