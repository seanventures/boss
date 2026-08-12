"""BOSS-345: plan.dispatched → agent.orchestrator.next_move is claimable and advances phases."""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.services.event_subscription_service import (
    seed_default_event_subscriptions,
)
from apps.api.app.services.intake import CommandIntakeService
from apps.api.app.services.orchestrator_next_move import (
    OrchestratorNextMoveService,
    classify_phase,
)
from apps.worker.boss_worker.runner import capabilities


PROJECT_ID = "boss-dogfood"


def test_classify_phase_keywords():
    assert classify_phase("Design the API", "scope the product brief") == "scope"
    assert classify_phase("Architecture review", "") == "architecture"
    assert classify_phase("Implement the checkout flow", "") == "implement"
    assert classify_phase("QA validation", "verify regressions") == "qa"
    assert classify_phase("Ship the release", "deploy staging") == "release"


def test_worker_capabilities_include_orchestrator_next_move():
    assert "agent.orchestrator.next_move" in capabilities()


def test_plan_dispatched_enqueues_claimable_next_move_job(engine, client):
    settings = get_settings()
    intake = CommandIntakeService(settings)
    with engine.begin() as connection:
        seed_default_event_subscriptions(connection, PROJECT_ID)
        proposed = intake.propose_command(
            connection,
            PROJECT_ID,
            "Build a hello world web app",
            constraints={"phases": [{"title": "Implement hello world app"}]},
        )
        plan_id = proposed["plan_id"]
        dispatched = intake.dispatch_plan(
            connection,
            plan_id,
            project_id=PROJECT_ID,
            actor="ceo",
        )
        assert len(dispatched["phase_task_ids"]) == 1

        job = connection.execute(
            text(
                """
                SELECT id, kind, status, task_id, payload
                FROM jobs
                WHERE project_id = :project_id
                  AND kind = 'agent.orchestrator.next_move'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": PROJECT_ID},
        ).mappings().first()
        assert job is not None
        assert job["kind"] == "agent.orchestrator.next_move"
        assert job["status"] == "queued"
        # Workers advertise this capability so the job is leaseable.
        assert job["kind"] in capabilities()
        payload = job["payload"]
        if isinstance(payload, str):
            import json

            payload = json.loads(payload)
        assert payload["entity_id"] == plan_id
        assert payload["event_type"] == "plan.dispatched"
        assert payload["event_data"]["phase_task_ids"] == dispatched["phase_task_ids"]
        assert payload["event_data"]["task_id"] == proposed["task_id"]


def test_next_move_starts_ready_implement_phase_as_code_task(engine, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "target_github_owner", "seanheiney")
    monkeypatch.setattr(settings, "target_github_repo", "boss-sandbox")
    intake = CommandIntakeService(settings)
    service = OrchestratorNextMoveService(settings)

    with engine.begin() as connection:
        proposed = intake.propose_command(
            connection,
            PROJECT_ID,
            "Implement a hello world web app",
            constraints={"phases": [{"title": "Implement hello world web app"}]},
        )
        plan_id = proposed["plan_id"]
        dispatched = intake.dispatch_plan(
            connection, plan_id, project_id=PROJECT_ID, actor="ceo"
        )
        phase_id = dispatched["phase_task_ids"][0]

        result = service.execute(
            connection,
            project_id=PROJECT_ID,
            event_id="evt_test_next",
            event_type="plan.dispatched",
            entity_type="plan",
            entity_id=plan_id,
            event_data={
                "phase_task_ids": dispatched["phase_task_ids"],
                "task_id": proposed["task_id"],
            },
            created_by_job_id=None,
            worker_id="worker-test",
        )
        assert result["status"] == "succeeded"
        assert result["plan_id"] == plan_id
        assert result["receipt_id"]
        assert phase_id in result["ready_task_ids"]
        assert result["actions"]
        action = result["actions"][0]
        assert action["task_id"] == phase_id
        assert action["status"] in {"started", "reused"}
        assert action["job_kind"] == "code.task"
        assert action.get("run_id")

        task_status = connection.execute(
            text("SELECT status FROM tasks WHERE id = :id"),
            {"id": phase_id},
        ).scalar_one()
        assert task_status == "in_progress"

        code_job = connection.execute(
            text(
                """
                SELECT kind, status FROM jobs
                WHERE project_id = :project_id
                  AND task_id = :task_id
                  AND kind = 'code.task'
                """
            ),
            {"project_id": PROJECT_ID, "task_id": phase_id},
        ).mappings().first()
        assert code_job is not None
        assert code_job["status"] == "queued"

        applied = connection.execute(
            text(
                """
                SELECT event_type FROM events
                WHERE project_id = :project_id
                  AND event_type = 'orchestrator.next_move.applied'
                  AND entity_id = :plan_id
                """
            ),
            {"project_id": PROJECT_ID, "plan_id": plan_id},
        ).first()
        assert applied is not None


def test_next_move_fail_closed_without_target_repo(engine, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "target_github_owner", "")
    monkeypatch.setattr(settings, "target_github_repo", "")
    intake = CommandIntakeService(settings)
    service = OrchestratorNextMoveService(settings)

    with engine.begin() as connection:
        proposed = intake.propose_command(
            connection,
            PROJECT_ID,
            "Implement feature X",
            constraints={"phases": [{"title": "Implement feature X"}]},
        )
        plan_id = proposed["plan_id"]
        dispatched = intake.dispatch_plan(
            connection, plan_id, project_id=PROJECT_ID, actor="ceo"
        )
        result = service.execute(
            connection,
            project_id=PROJECT_ID,
            event_type="plan.dispatched",
            entity_type="plan",
            entity_id=plan_id,
            event_data={"phase_task_ids": dispatched["phase_task_ids"]},
            created_by_job_id=None,
        )
        assert result["status"] == "succeeded"
        assert result["actions"][0]["status"] == "needs_decision"
        assert result["actions"][0]["decision_id"]
        assert result["required_decisions"]


def test_next_move_respects_phase_dependencies(engine, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "target_github_owner", "seanheiney")
    monkeypatch.setattr(settings, "target_github_repo", "boss-sandbox")
    intake = CommandIntakeService(settings)
    service = OrchestratorNextMoveService(settings)

    with engine.begin() as connection:
        proposed = intake.propose_command(
            connection,
            PROJECT_ID,
            "multi",
            constraints={
                "phases": [
                    {"title": "Implement phase one"},
                    {"title": "Implement phase two", "depends_on": [0]},
                ]
            },
        )
        plan_id = proposed["plan_id"]
        dispatched = intake.dispatch_plan(
            connection, plan_id, project_id=PROJECT_ID, actor="ceo"
        )
        phase_ids = dispatched["phase_task_ids"]
        assert len(phase_ids) == 2

        result = service.execute(
            connection,
            project_id=PROJECT_ID,
            event_type="plan.dispatched",
            entity_type="plan",
            entity_id=plan_id,
            event_data={"phase_task_ids": phase_ids},
            created_by_job_id=None,
        )
        assert phase_ids[0] in result["ready_task_ids"]
        assert phase_ids[1] in result["blocked_task_ids"]
        assert len(result["actions"]) == 1
        assert result["actions"][0]["task_id"] == phase_ids[0]


def test_next_move_enqueues_scope_agent_job(engine):
    settings = get_settings()
    intake = CommandIntakeService(settings)
    service = OrchestratorNextMoveService(settings)

    with engine.begin() as connection:
        proposed = intake.propose_command(
            connection,
            PROJECT_ID,
            "scope work",
            constraints={"phases": [{"title": "Product scope brief for the feature"}]},
        )
        plan_id = proposed["plan_id"]
        dispatched = intake.dispatch_plan(
            connection, plan_id, project_id=PROJECT_ID, actor="ceo"
        )
        result = service.execute(
            connection,
            project_id=PROJECT_ID,
            event_type="plan.dispatched",
            entity_type="plan",
            entity_id=plan_id,
            event_data={"phase_task_ids": dispatched["phase_task_ids"]},
            created_by_job_id=None,
        )
        assert result["actions"][0]["job_kind"] == "agent.pm.scope_brief"
        assert result["actions"][0]["status"] in {"enqueued", "reused"}

        job = connection.execute(
            text(
                """
                SELECT kind, status FROM jobs
                WHERE project_id = :project_id
                  AND kind = 'agent.pm.scope_brief'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": PROJECT_ID},
        ).mappings().first()
        assert job is not None
        assert job["status"] == "queued"


def test_execute_endpoint_wires_worker_boundary(client, engine, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "target_github_owner", "seanheiney")
    monkeypatch.setattr(settings, "target_github_repo", "boss-sandbox")
    # Rebind the app singleton settings used by the route.
    from apps.api.app import main as api_main

    monkeypatch.setattr(
        api_main.orchestrator_next_move, "settings", settings, raising=False
    )
    api_main.orchestrator_next_move.settings = settings
    if hasattr(api_main.orchestrator_next_move, "jobs"):
        api_main.orchestrator_next_move.jobs.settings = settings

    intake = CommandIntakeService(settings)
    with engine.begin() as connection:
        proposed = intake.propose_command(
            connection,
            PROJECT_ID,
            "Implement endpoint wiring check",
            constraints={"phases": [{"title": "Implement endpoint wiring check"}]},
        )
        plan_id = proposed["plan_id"]
        dispatched = intake.dispatch_plan(
            connection, plan_id, project_id=PROJECT_ID, actor="ceo"
        )
        phase_ids = dispatched["phase_task_ids"]
        parent_task = proposed["task_id"]

    response = client.post(
        "/v1/orchestration/next-move/execute",
        json={
            "project_id": PROJECT_ID,
            "event_id": "evt_http",
            "event_type": "plan.dispatched",
            "entity_type": "plan",
            "entity_id": plan_id,
            "event_data": {
                "phase_task_ids": phase_ids,
                "task_id": parent_task,
            },
            "task_id": parent_task,
            "worker_id": "worker-http",
            "lease_token": "lease_http",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["plan_id"] == plan_id
    assert body["receipt_id"]
