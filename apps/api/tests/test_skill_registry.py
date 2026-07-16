from __future__ import annotations

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.services.entitlements import EntitlementBrokerService
from apps.api.app.services.jobs import JobLeaseService
from apps.api.app.services.receipts import ReceiptService
from apps.api.app.services.skill_registry import (
    disable_skill,
    enable_skill,
    ensure_skill,
    list_project_skills,
    register_skill,
    seed_system_skills,
)


def test_project_skill_registry_is_scoped_idempotent_and_audited(engine):
    settings = get_settings()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('project_skill_other', :workspace_id, 'Other skills', 'other-skills')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"workspace_id": settings.default_workspace_id},
        )
        item = register_skill(
            connection,
            settings.default_project_id,
            "github.create_pr_sandbox",
            source="tenant",
            on_demand=True,
            config={"target": "sandbox"},
            publisher={"id": "acme"},
            actor="operator",
        )
        assert item["source"] == "tenant"
        assert item["on_demand"] is True
        assert item["config"] == {"target": "sandbox"}
        assert item["publisher"] == {"id": "acme"}

        assert disable_skill(
            connection,
            "project_skill_other",
            "github.create_pr_sandbox",
            "other-operator",
        ) is None
        disabled = disable_skill(
            connection,
            settings.default_project_id,
            "github.create_pr_sandbox",
            "operator",
        )
        assert disabled is not None and disabled["enabled"] is False
        enabled = enable_skill(
            connection,
            settings.default_project_id,
            "github.create_pr_sandbox",
            "operator",
        )
        assert enabled is not None and enabled["enabled"] is True

        ensured = ensure_skill(
            connection,
            settings.default_project_id,
            "github.create_pr_sandbox",
            source="tenant",
            on_demand=True,
            config={"target": "sandbox"},
            publisher={"id": "acme"},
        )
        assert ensured["id"] == item["id"]
        listed = list_project_skills(connection, settings.default_project_id)
        assert [row["skill_id"] for row in listed].count(
            "github.create_pr_sandbox"
        ) == 1

        event_types = connection.execute(
            text(
                """
                SELECT event_type
                FROM events
                WHERE project_id = :project_id
                  AND entity_id = :entity_id
                ORDER BY created_at
                """
            ),
            {"project_id": settings.default_project_id, "entity_id": item["id"]},
        ).scalars().all()
        assert event_types == [
            "skill.registered",
            "skill.disabled",
            "skill.enabled",
        ]


def test_skill_spec_system_and_load_metadata_seed_fresh_projects(engine):
    settings = get_settings()
    entitlements = EntitlementBrokerService(settings)
    skills = entitlements.list_skills()
    system_skill = next(skill for skill in skills if skill["system"])
    on_demand_skill = next(
        skill for skill in skills if skill["id"] == "github.create_pr_sandbox"
    )
    assert system_skill["id"] == "deployment.health.check"
    assert on_demand_skill["id"] == "github.create_pr_sandbox"

    invalid = {**system_skill, "load": "sometimes"}
    try:
        entitlements.validate_skill_spec(invalid, "test")
    except ValueError as error:
        assert "load must be either eager or on_demand" in str(error)
    else:
        raise AssertionError("invalid SkillSpec load value was accepted")

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (
                    'project_skill_seed_test', :workspace_id,
                    'Fresh skill seed', 'fresh-skill-seed'
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"workspace_id": settings.default_workspace_id},
        )
        connection.execute(
            text(
                "DELETE FROM project_skills WHERE project_id = 'project_skill_seed_test'"
            )
        )
        seeded = seed_system_skills(
            connection,
            "project_skill_seed_test",
            skills,
            "test",
        )
        matching = [
            item
            for item in seeded
            if item["skill_id"] == "deployment.health.check"
        ]
        assert len(matching) == 1
        assert matching[0]["source"] == "system"
        assert matching[0]["enabled"] is True
        assert matching[0]["on_demand"] is False


def test_real_skill_invocation_fails_closed_for_unbacked_skill(client, engine):
    settings = get_settings()
    lease_service = JobLeaseService(settings)
    with engine.begin() as connection:
        job = lease_service.create_job(
            connection,
            settings.default_project_id,
            "skill.execute",
            {
                "skill_id": "model.generate_text",
                "caller": "worker",
                "inputs": {"prompt": "hello"},
            },
        )
        leased = lease_service.claim_job(
            connection, "worker-unbacked", ["skill.execute"], 60
        )
        assert leased is not None and leased["id"] == job["id"]

    response = client.post(
        "/v1/skills/model.generate_text/invoke",
        json={
            "project_id": settings.default_project_id,
            "caller": "worker",
            "inputs": {"prompt": "hello"},
            "mock": False,
            "created_by_job_id": leased["id"],
            "worker_id": "worker-unbacked",
            "lease_token": leased["lease_token"],
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["attempts"] == 0
    assert payload["reservation_id"] is None
    assert "No server-side executor is registered" in payload["reason"]


def test_real_skill_invocation_requires_matching_lease_and_reconciles_cost(
    client, engine, monkeypatch
):
    settings = get_settings()
    lease_service = JobLeaseService(settings)
    attempts = 0

    def fake_resolve(skill_id, *, context=None):
        assert skill_id == "github.create_pr_sandbox"
        assert context is not None

        def execute(inputs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient provider failure")
            org_id, workspace_id = context.tenant_scope()
            receipt, _ = ReceiptService().create_or_reuse(
                context.connection,
                org_id,
                workspace_id,
                context.project_id,
                "skill-runtime-test",
                "github.pull_request.create",
                {"repository": inputs["repo"]},
                {"title": inputs["title"]},
                created_by_job_id=context.created_by_job_id,
            )
            ReceiptService().mark_succeeded(
                context.connection,
                receipt["id"],
                "https://github.test/acme/widget/pull/1",
                {"pull_request_number": 1},
            )
            return {
                "receipt_id": receipt["id"],
                "actual_cost_usd_micros": 125,
                "pull_request_number": 1,
            }

        return execute

    monkeypatch.setattr(
        "apps.api.app.services.skill_runtime.resolve_executor", fake_resolve
    )
    inputs = {
        "repo": "acme/widget",
        "branch": "boss/test",
        "title": "Test real skill",
        "body": "Leased and receipt-backed",
    }
    with engine.begin() as connection:
        job = lease_service.create_job(
            connection,
            settings.default_project_id,
            "skill.execute",
            {
                "skill_id": "github.create_pr_sandbox",
                "caller": "worker",
                "inputs": inputs,
            },
        )
        leased = lease_service.claim_job(
            connection, "worker-skill", ["skill.execute"], 60
        )
        assert leased is not None and leased["id"] == job["id"]

    mismatched = client.post(
        "/v1/skills/github.create_pr_sandbox/invoke",
        json={
            "project_id": settings.default_project_id,
            "caller": "worker",
            "inputs": {**inputs, "title": "tampered"},
            "mock": False,
            "created_by_job_id": leased["id"],
            "worker_id": "worker-skill",
            "lease_token": leased["lease_token"],
        },
    )
    assert mismatched.status_code == 409

    response = client.post(
        "/v1/skills/github.create_pr_sandbox/invoke",
        json={
            "project_id": settings.default_project_id,
            "caller": "worker",
            "inputs": inputs,
            "mock": False,
            "created_by_job_id": leased["id"],
            "worker_id": "worker-skill",
            "lease_token": leased["lease_token"],
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "succeeded"
    assert payload["mode"] == "real"
    assert payload["attempts"] == 2
    assert payload["actual_cost_usd_micros"] == 125
    assert payload["cost_receipt_id"]
    assert payload["result"]["receipt_id"]

    replay = client.post(
        "/v1/skills/github.create_pr_sandbox/invoke",
        json={
            "project_id": settings.default_project_id,
            "caller": "worker",
            "inputs": inputs,
            "mock": False,
            "created_by_job_id": leased["id"],
            "worker_id": "worker-skill",
            "lease_token": leased["lease_token"],
        },
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == payload["id"]
    assert attempts == 2

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text(
                "SELECT status FROM budget_reservations WHERE id = :id"
            ),
            {"id": payload["reservation_id"]},
        ).scalar_one()
        assert reservation_status == "consumed"
        assert connection.execute(
            text(
                """
                SELECT count(*)
                FROM skill_invocations
                WHERE created_by_job_id = :job_id
                """
            ),
            {"job_id": leased["id"]},
        ).scalar_one() == 1
        event = connection.execute(
            text(
                """
                SELECT data
                FROM events
                WHERE event_type = 'skill.invoked' AND entity_id = :id
                """
            ),
            {"id": payload["id"]},
        ).scalar_one()
        assert event["mode"] == "real"
        assert event["status"] == "succeeded"
