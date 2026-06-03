import json
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.services.digitalocean_adapter import (
    DropletProvisionResult,
    DropletTerminationResult,
)
from apps.api.app.services.github_adapter import (
    DeploymentResult,
    DeploymentStatusResult,
    PullRequestMergeResult,
    PullRequestResult,
    PullRequestValidationCheck,
    PullRequestValidationResult,
)


def create_local_validation_repo(tmp_path, branch_name: str):
    repo = tmp_path / "sandbox-validation"
    repo.mkdir()
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "boss@example.com"],
        ["git", "config", "user.name", "BOSS Test"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("# Sandbox validation\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial sandbox validation fixture"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return repo


def configure_pr_proposal_integrations(monkeypatch):
    api_main.software_factory.settings.openai_api_key = "test-openai-key"
    monkeypatch.setattr(
        api_main.software_factory.openai,
        "create_text_response",
        lambda prompt, system_prompt=None, model=None: (
            "resp_review_packet",
            "## Implementation summary\n- Review packet generated for test coverage.",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "ensure_branch",
        lambda branch_name: "sha-base",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "upsert_file",
        lambda branch_name, path, content, message: f"commit-{path}",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "create_pull_request",
        lambda branch_name, title, body: PullRequestResult(
            url="https://github.com/seanheiney/boss-sandbox/pull/77",
            number=77,
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "wait_for_commit_checks",
        lambda head_sha, timeout_seconds, poll_interval_seconds: PullRequestValidationResult(
            status="passed",
            summary="GitHub validation passed: CI.",
            head_sha=head_sha,
            completed=True,
            checks=[
                PullRequestValidationCheck(
                    name="CI",
                    status="completed",
                    conclusion="success",
                    html_url="https://github.com/seanheiney/boss-sandbox/actions/runs/77",
                    started_at="2026-06-02T12:00:00Z",
                    completed_at="2026-06-02T12:01:00Z",
                )
            ],
        ),
    )


def reserve_for_pr_proposal(client) -> str:
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 100_000,
            "reason": "Run sandbox PR proposal validation.",
        },
    )
    assert reserve.status_code == 201
    return reserve.json()["reservation_id"]


def reserve_for_infra_provision(client, reason: str = "Reserve infra budget.") -> str:
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "infra",
            "amount_usd_micros": 250_000,
            "reason": reason,
        },
    )
    assert reserve.status_code == 201
    return reserve.json()["reservation_id"]


def insert_approved_infra_decision(
    engine,
    *,
    decision_id: str,
    reservation_id: str,
    task_id: Optional[str] = None,
    title: str = "Provision preview droplet",
    region: str = "nyc3",
    size: str = "s-1vcpu-1gb",
    ttl_minutes: int = 60,
    data_boundary: str = "internal",
):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, deadline_at, default_action, status, approved_by, approved_at
                )
                VALUES (
                    :decision_id,
                    'boss-dogfood',
                    :task_id,
                    :question,
                    CAST(:recommendation AS JSONB),
                    '[{"label":"Provision","action":"approve"}]'::jsonb,
                    3,
                    CAST(:cost_summary AS JSONB),
                    now() + interval '1 day',
                    'hold',
                    'approved',
                    'ceo',
                    now()
                )
                """
            ),
            {
                "decision_id": decision_id,
                "task_id": task_id,
                "question": f"Approve infrastructure provisioning: {title}",
                "recommendation": json.dumps(
                    {
                        "action": "review_infra_provisioning",
                        "reservation_id": reservation_id,
                        "title": title,
                        "region": region,
                        "size": size,
                        "ttl_minutes": ttl_minutes,
                        "tags": ["dogfood", "preview"],
                        "data_boundary": data_boundary,
                    }
                ),
                "cost_summary": json.dumps(
                    {
                        "reservation_id": reservation_id,
                        "region": region,
                        "size": size,
                        "ttl_minutes": ttl_minutes,
                    }
                ),
            },
        )


def assert_complete_rolespec(spec: dict) -> None:
    for key in (
        "role_id",
        "display_name",
        "mission",
        "primary_outcomes",
        "boundaries",
        "decision_rights",
        "cadence",
        "permissions",
        "comms_contract",
        "output_contracts",
        "eval_rubric",
        "autonomy",
    ):
        assert key in spec
    assert spec["primary_outcomes"]
    assert spec["output_contracts"]
    assert spec["boundaries"]["must"]
    assert spec["boundaries"]["must_not"]
    assert spec["decision_rights"]["can_decide"]
    assert spec["decision_rights"]["must_escalate"]
    assert spec["permissions"]["allowed_skills"]
    assert spec["permissions"]["forbidden_skills"]
    assert isinstance(spec["autonomy"]["level"], int)


def assert_complete_skill_spec(skill: dict) -> None:
    for key in (
        "id",
        "kind",
        "risk_tier",
        "description",
        "mock_mode",
        "input_schema",
        "output_schema",
        "cost",
        "retry",
        "permissions",
        "data_boundary",
        "policy",
    ):
        assert key in skill
    assert skill["mock_mode"] is False
    assert skill["input_schema"]
    assert skill["output_schema"]
    assert skill["cost"]["estimator"]
    assert skill["cost"]["category"]
    assert skill["retry"]["max_attempts"] >= 1
    assert skill["retry"]["backoff"]
    assert skill["permissions"]["allowed_callers"]
    assert skill["permissions"]["required_permissions"]


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_returns_service_status(client):
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database_connected"] is True


def test_v1_routes_can_require_api_key_without_hiding_health(client):
    previous_required = api_main.settings.require_api_auth
    previous_keys = api_main.settings.api_keys
    api_main.settings.require_api_auth = True
    api_main.settings.api_keys = "secret-one,secret-two"
    try:
        health = client.get("/health")
        assert health.status_code == 200

        missing = client.get("/v1/projects")
        assert missing.status_code == 401

        wrong = client.get("/v1/projects", headers={"Authorization": "Bearer wrong"})
        assert wrong.status_code == 401

        bearer = client.get(
            "/v1/projects", headers={"Authorization": "Bearer secret-one"}
        )
        assert bearer.status_code == 200

        explicit_header = client.get(
            "/v1/projects", headers={"X-BOSS-API-Key": "secret-two"}
        )
        assert explicit_header.status_code == 200
    finally:
        api_main.settings.require_api_auth = previous_required
        api_main.settings.api_keys = previous_keys


def test_v1_routes_fail_closed_when_auth_required_without_configured_keys(client):
    previous_required = api_main.settings.require_api_auth
    previous_keys = api_main.settings.api_keys
    api_main.settings.require_api_auth = True
    api_main.settings.api_keys = ""
    try:
        response = client.get("/v1/projects", headers={"Authorization": "Bearer any"})
        assert response.status_code == 503
        assert "no BOSS_API_KEYS" in response.json()["detail"]
    finally:
        api_main.settings.require_api_auth = previous_required
        api_main.settings.api_keys = previous_keys


def test_api_key_principal_binding_reports_session_and_blocks_impersonation(client):
    previous_required = api_main.settings.require_api_auth
    previous_keys = api_main.settings.api_keys
    previous_principals = api_main.settings.api_key_principals
    api_main.settings.require_api_auth = True
    api_main.settings.api_keys = "ceo-key,release-key"
    api_main.settings.api_key_principals = "ceo-key:ceo,release-key:release_manager"
    try:
        session = client.get(
            "/v1/auth/session",
            headers=auth_headers("ceo-key"),
        )
        assert session.status_code == 200
        assert session.json()["principal_member_id"] == "ceo"
        assert session.json()["actor_binding_required"] is True

        denied = client.post(
            "/v1/commands/propose",
            headers=auth_headers("release-key"),
            json={
                "project_id": "boss-dogfood",
                "command": "Attempt to act as the CEO with the release key.",
                "actor": "ceo",
            },
        )
        assert denied.status_code == 403
        assert "release_manager" in denied.json()["detail"]

        allowed = client.post(
            "/v1/commands/propose",
            headers=auth_headers("ceo-key"),
            json={
                "project_id": "boss-dogfood",
                "command": "Create a principal-bound command intake record.",
                "actor": "ceo",
            },
        )
        assert allowed.status_code == 201
    finally:
        api_main.settings.require_api_auth = previous_required
        api_main.settings.api_keys = previous_keys
        api_main.settings.api_key_principals = previous_principals


def test_unbound_api_key_fails_closed_when_principal_binding_is_configured(client):
    previous_required = api_main.settings.require_api_auth
    previous_keys = api_main.settings.api_keys
    previous_principals = api_main.settings.api_key_principals
    api_main.settings.require_api_auth = True
    api_main.settings.api_keys = "ceo-key,unbound-key"
    api_main.settings.api_key_principals = "ceo-key:ceo"
    try:
        response = client.get(
            "/v1/projects",
            headers=auth_headers("unbound-key"),
        )

        assert response.status_code == 401
        assert "not bound to a project member principal" in response.json()["detail"]
    finally:
        api_main.settings.require_api_auth = previous_required
        api_main.settings.api_keys = previous_keys
        api_main.settings.api_key_principals = previous_principals


def test_api_key_principal_binding_applies_to_decision_transitions(client, engine):
    previous_required = api_main.settings.require_api_auth
    previous_keys = api_main.settings.api_keys
    previous_principals = api_main.settings.api_key_principals
    api_main.settings.require_api_auth = True
    api_main.settings.api_keys = "ceo-key,release-key"
    api_main.settings.api_key_principals = "ceo-key:ceo,release-key:release_manager"
    decision_id = "dec_auth_principal_binding"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, question, recommendation, options,
                    risk_tier, default_action, status
                )
                VALUES (
                    :decision_id,
                    'boss-dogfood',
                    'Approve a principal-bound decision transition?',
                    '{"action":"approve"}'::jsonb,
                    '[{"label":"Approve","action":"approve"}]'::jsonb,
                    1,
                    'hold',
                    'pending'
                )
                """
            ),
            {"decision_id": decision_id},
        )
    try:
        denied = client.post(
            f"/v1/decisions/{decision_id}/approve",
            headers=auth_headers("release-key"),
            json={"actor": "ceo"},
        )
        assert denied.status_code == 403
        assert "release_manager" in denied.json()["detail"]

        allowed = client.post(
            f"/v1/decisions/{decision_id}/approve",
            headers=auth_headers("ceo-key"),
            json={"actor": "ceo"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["status"] == "approved"
    finally:
        api_main.settings.require_api_auth = previous_required
        api_main.settings.api_keys = previous_keys
        api_main.settings.api_key_principals = previous_principals


def test_status_returns_seeded_project(client):
    response = client.get("/v1/status", params={"project_id": "boss-dogfood"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == "boss-dogfood"
    assert payload["project_name"] == "BOSS dogfood bootstrap"
    assert "deadletters" in payload
    assert "active_jobs" in payload
    assert "top_risks" in payload


def test_list_projects_returns_seeded_project_scope(client):
    response = client.get("/v1/projects")

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"]
    projects = {item["id"]: item for item in payload["items"]}
    assert projects["boss-dogfood"]["workspace_name"] == "Dogfood"
    assert projects["boss-dogfood"]["org_name"] == "BOSS"


def test_list_project_members_returns_seeded_actor_directory(client):
    response = client.get("/v1/members", params={"project_id": "boss-dogfood"})

    assert response.status_code == 200
    payload = response.json()
    member_ids = {item["member_id"] for item in payload["items"]}
    assert {"ceo", "executive_console", "release_manager", "vp_agents", "system"}.issubset(
        member_ids
    )
    executive_console = next(
        item for item in payload["items"] if item["member_id"] == "executive_console"
    )
    assert "command_submit" in executive_console["permissions"]
    assert "ceo" in executive_console["role_keys"]


def test_project_member_upsert_requires_member_manage_permission(client):
    denied = client.post(
        "/v1/members",
        json={
            "project_id": "boss-dogfood",
            "member_id": "operator",
            "display_name": "Operator",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["health_refresh"],
            "actor": "release_manager",
        },
    )
    assert denied.status_code == 403

    created = client.post(
        "/v1/members",
        json={
            "project_id": "boss-dogfood",
            "member_id": "operator",
            "display_name": "Operator",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["health_refresh"],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["member_id"] == "operator"
    assert payload["created_by"] == "ceo"


def test_break_glass_grant_enables_temporary_permission_and_postmortem(client):
    grant = client.post(
        "/v1/break-glass/grants",
        json={
            "project_id": "boss-dogfood",
            "target_member_id": "release_manager",
            "reason": "Emergency member directory repair for an incident.",
            "scope": {"incident_id": "inc-break-glass-test"},
            "granted_permissions": ["member_manage"],
            "ttl_minutes": 30,
            "actor": "ceo",
        },
    )
    assert grant.status_code == 201
    grant_payload = grant.json()
    assert grant_payload["id"].startswith("bg_")
    assert grant_payload["status"] == "active"
    assert grant_payload["postmortem_task_id"].startswith("tsk_")

    created = client.post(
        "/v1/members",
        json={
            "project_id": "boss-dogfood",
            "member_id": "emergency_operator",
            "display_name": "Emergency Operator",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["health_refresh"],
            "actor": "release_manager",
        },
    )
    assert created.status_code == 201

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "break_glass.granted" in event_types
    assert "break_glass.postmortem_task_created" in event_types
    assert "break_glass.used" in event_types


def test_break_glass_revoke_removes_temporary_permission(client):
    grant = client.post(
        "/v1/break-glass/grants",
        json={
            "project_id": "boss-dogfood",
            "target_member_id": "release_manager",
            "reason": "Temporary escalation for revoke test.",
            "granted_permissions": ["member_manage"],
            "ttl_minutes": 30,
            "actor": "ceo",
        },
    )
    assert grant.status_code == 201
    grant_id = grant.json()["id"]

    revoked = client.post(
        f"/v1/break-glass/grants/{grant_id}/revoke",
        json={"actor": "ceo", "reason": "Emergency ended."},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    denied = client.post(
        "/v1/members",
        json={
            "project_id": "boss-dogfood",
            "member_id": "blocked_operator",
            "display_name": "Blocked Operator",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["health_refresh"],
            "actor": "release_manager",
        },
    )
    assert denied.status_code == 403


def test_expired_break_glass_grant_is_visible_and_not_usable(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO break_glass_grants (
                    id, project_id, target_member_id, requested_by, reason,
                    scope, granted_permissions, status, expires_at
                )
                VALUES (
                    'bg_expired_test',
                    'boss-dogfood',
                    'release_manager',
                    'ceo',
                    'Expired test grant.',
                    '{}'::jsonb,
                    '["member_manage"]'::jsonb,
                    'active',
                    now() - interval '1 minute'
                )
                """
            )
        )

    denied = client.post(
        "/v1/members",
        json={
            "project_id": "boss-dogfood",
            "member_id": "expired_operator",
            "display_name": "Expired Operator",
            "member_type": "human",
            "role_keys": ["operator"],
            "permissions": ["health_refresh"],
            "actor": "release_manager",
        },
    )
    assert denied.status_code == 403

    grants = client.get(
        "/v1/break-glass/grants",
        params={"project_id": "boss-dogfood", "include_expired": True},
    )
    expired = next(item for item in grants.json()["items"] if item["id"] == "bg_expired_test")
    assert expired["status"] == "expired"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "break_glass.expired" in event_types


def test_compliance_evidence_bundle_captures_controls_and_access_evidence(client):
    grant = client.post(
        "/v1/break-glass/grants",
        json={
            "project_id": "boss-dogfood",
            "target_member_id": "release_manager",
            "reason": "Seed compliance evidence with emergency access.",
            "scope": {"incident_id": "inc-compliance-test"},
            "granted_permissions": ["member_manage"],
            "ttl_minutes": 30,
            "actor": "ceo",
        },
    )
    assert grant.status_code == 201

    response = client.post(
        "/v1/compliance/evidence-bundles",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["artifact_type"] == "compliance_evidence_bundle"
    assert payload["counts"]["project_members"] >= 1
    assert payload["counts"]["break_glass_grants"] == 1

    artifact = client.get(f"/v1/artifacts/{payload['artifact_id']}")
    assert artifact.status_code == 200
    content = artifact.json()["content"]
    assert content["bundle_type"] == "compliance_evidence"
    assert "break_glass" in content["controls"]
    assert content["break_glass_grants"][0]["id"] == grant.json()["id"]

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "compliance.evidence_bundle_created" in event_types


def test_command_proposal_requires_known_project_actor(client):
    response = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Create a plan for member-gated command intake.",
            "actor": "not_a_member",
        },
    )

    assert response.status_code == 403
    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "policy.denied" in event_types


def test_metatron_status_query_returns_fresh_answer_and_references(client):
    response = client.post(
        "/v1/metatron/query",
        json={
            "project_id": "boss-dogfood",
            "question": "What is the current status?",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["category"] == "status"
    assert "budget state" in payload["answer"]
    assert payload["freshness_at"]
    assert payload["references"]
    assert payload["references"][0]["object_type"] in {"project", "task", "decision"}


def test_metatron_budget_query_returns_reservation_references(client):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 125_000,
            "reason": "Seed a reservation for Metatron budget status.",
        },
    )
    assert reserve.status_code == 201

    response = client.post(
        "/v1/metatron/query",
        json={
            "project_id": "boss-dogfood",
            "question": "How is the budget doing right now?",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["category"] == "budget"
    assert "Available:" in payload["answer"]
    assert any(
        reference["object_type"] == "budget_reservation"
        for reference in payload["references"]
    )


def test_metatron_activity_query_returns_event_references(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data)
                VALUES (
                    'evt_metatron_activity',
                    'boss-dogfood',
                    'artifact.created',
                    'tester',
                    'artifact',
                    'art_metatron_activity',
                    '{"message":"Activity query should see this event."}'::jsonb
                )
                """
            )
        )

    response = client.post(
        "/v1/metatron/query",
        json={
            "project_id": "boss-dogfood",
            "question": "What changed recently?",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["category"] == "activity"
    assert payload["references"]
    assert payload["references"][0]["object_type"] == "artifact"
    assert "artifact.created" in payload["answer"]


def test_metatron_task_scope_returns_task_lineage(client):
    created = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Create a scoped task answer for Metatron.",
            "constraints": {"deadline": "today"},
        },
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    response = client.post(
        "/v1/metatron/query",
        json={
            "project_id": "boss-dogfood",
            "question": "What is happening with this task?",
            "task_id": task_id,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["category"] == "task"
    assert task_id in {reference["object_id"] for reference in payload["references"]}
    assert "artifact" in {reference["object_type"] for reference in payload["references"]}


def test_bootstrap_seed_keeps_roles_unique(client):
    reseed = client.post("/v1/bootstrap/seed")
    assert reseed.status_code == 200
    response = client.get("/v1/roles", params={"project_id": "boss-dogfood"})

    assert response.status_code == 200
    payload = response.json()
    role_keys = [role["role_key"] for role in payload["items"]]
    assert len(role_keys) == len(set(role_keys))
    assert "metatron" in role_keys


def test_health_snapshot_refresh_persists_summary_and_risks(client, engine):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force health-risk decision",
        },
    )
    assert denial.status_code == 201

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (
                    'dlq_health_1', 'boss-dogfood', 'job', 'job_health_1',
                    'health test deadletter', '{}'::jsonb
                )
                """
            )
        )

    refreshed = client.post(
        "/v1/health-snapshots/refresh",
        json={"project_id": "boss-dogfood", "actor": "metatron"},
    )
    assert refreshed.status_code == 201
    payload = refreshed.json()
    assert payload["project_id"] == "boss-dogfood"
    assert payload["generated_by"] == "metatron"
    assert payload["summary"]["deadletters"] == 1
    assert payload["workload_state"]["pending_decisions"] >= 1
    assert payload["top_risks"]
    assert any(
        risk["category"] == "incident" and "Deadletters" in risk["title"]
        for risk in payload["top_risks"]
    )

    snapshots = client.get(
        "/v1/health-snapshots",
        params={"project_id": "boss-dogfood", "limit": 10},
    )
    assert snapshots.status_code == 200
    snapshot_payload = snapshots.json()
    assert snapshot_payload["items"]
    assert snapshot_payload["items"][0]["id"] == payload["id"]


def test_status_surfaces_latest_health_snapshot_metadata(client):
    refreshed = client.post(
        "/v1/health-snapshots/refresh",
        json={"project_id": "boss-dogfood", "actor": "executive_console"},
    )
    assert refreshed.status_code == 201
    snapshot_id = refreshed.json()["id"]

    status = client.get("/v1/status", params={"project_id": "boss-dogfood"})
    assert status.status_code == 200
    payload = status.json()
    assert payload["latest_snapshot_id"] == snapshot_id
    assert payload["latest_snapshot_at"]
    assert isinstance(payload["top_risks"], list)


def test_seed_installs_core_pack_and_roles(client):
    packs = client.get("/v1/packs", params={"project_id": "boss-dogfood"})
    assert packs.status_code == 200
    pack_payload = packs.json()
    core_pack = next(item for item in pack_payload["items"] if item["pack_id"] == "core_v1")
    assert core_pack["installed"] is True
    assert core_pack["installed_version"] == "1.0.0"
    assert "metatron" in core_pack["roles"]

    roles = client.get("/v1/roles", params={"project_id": "boss-dogfood"})
    assert roles.status_code == 200
    role_payload = roles.json()
    assert len(role_payload["items"]) >= 9
    role_keys = [item["role_key"] for item in role_payload["items"]]
    assert "metatron" in role_keys
    assert "cto" in role_keys


def test_role_detail_returns_loaded_spec(client):
    response = client.get(
        "/v1/roles/metatron",
        params={"project_id": "boss-dogfood"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["role_key"] == "metatron"
    assert payload["display_name"] == "Metatron"
    assert payload["autonomy_level"] == 1
    assert payload["trust_score"] == 50
    assert payload["source_module_id"] == "metatron_operator_v1"
    assert payload["source_module_version"] == "1.0.0"
    assert payload["spec"]["mission"].startswith("Watch the system")
    assert "StatusAnswer" in payload["spec"]["output_contracts"]


def test_installed_roles_have_complete_rolespec_contract(client):
    response = client.get("/v1/roles", params={"project_id": "boss-dogfood"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"]
    for role in payload["items"]:
        detail = client.get(
            f"/v1/roles/{role['role_key']}",
            params={"project_id": "boss-dogfood"},
        )
        assert detail.status_code == 200
        assert_complete_rolespec(detail.json()["spec"])


def test_rolespec_validator_rejects_incomplete_specs():
    with pytest.raises(ValueError, match="decision_rights"):
        api_main.specpack.validate_role_spec(
            {
                "role_id": "unsafe_role",
                "display_name": "Unsafe Role",
                "mission": "Do unsafe work.",
                "primary_outcomes": ["Output"],
                "boundaries": {
                    "must": ["Follow policy."],
                    "must_not": ["Bypass approvals."],
                },
                "permissions": {
                    "allowed_skills": ["artifact.create"],
                    "forbidden_skills": ["github.merge_pr_real"],
                },
                "output_contracts": ["Report"],
                "autonomy": {"level": 1},
            },
            "test role",
        )


def test_seed_installs_default_agent_modules(client):
    response = client.get("/v1/agent-modules", params={"project_id": "boss-dogfood"})

    assert response.status_code == 200
    payload = response.json()
    modules = {item["module_id"]: item for item in payload["items"]}
    assert modules["metatron_operator_v1"]["installed"] is True
    assert modules["codex_software_engineer_v1"]["installed"] is True
    assert "plan.command" in modules["metatron_operator_v1"]["job_kinds"]
    assert "software_factory.propose_pr" in modules["codex_software_engineer_v1"][
        "job_kinds"
    ]


def test_role_review_creates_upgrade_recommendation_when_role_is_eligible(client):
    response = client.post(
        "/v1/roles/metatron/reviews",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "summary": "Metatron completed enough clean runs to qualify for the next autonomy tier.",
            "metrics": {
                "successful_runs": 30,
                "policy_violations": 0,
                "duplicate_effect_attempts": 0,
                "hidden_failures": 0,
                "low_quality_outputs": 0,
                "scope_violations": 0,
                "rollback_rate": 0.0,
            },
            "evidence_refs": ["artifact:metatron-eval-001"],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["role"]["role_key"] == "metatron"
    assert payload["role"]["trust_score"] > 50
    assert payload["pending_autonomy_change"] is not None
    assert payload["pending_autonomy_change"]["change_type"] == "upgrade"
    assert payload["pending_autonomy_change"]["from_level"] == 1
    assert payload["pending_autonomy_change"]["to_level"] == 2
    assert payload["pending_autonomy_change"]["decision_id"]
    assert payload["recent_reviews"][0]["governance_outcome"] == "upgrade_recommended"


def test_role_review_with_violation_auto_downgrades_and_records_drift(client):
    response = client.post(
        "/v1/roles/cto/reviews",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "summary": "CTO drifted outside its charter and triggered a policy violation.",
            "metrics": {
                "successful_runs": 2,
                "policy_violations": 1,
                "duplicate_effect_attempts": 0,
                "hidden_failures": 0,
                "low_quality_outputs": 0,
                "scope_violations": 1,
                "rollback_rate": 0.0,
            },
            "evidence_refs": ["event:policy-violation-001"],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["role"]["role_key"] == "cto"
    assert payload["role"]["autonomy_level"] == 0
    assert payload["role"]["policy_violation_count"] == 1
    assert payload["role"]["scope_violation_count"] == 1
    assert payload["pending_autonomy_change"] is None
    assert payload["recent_reviews"][0]["governance_outcome"] == "auto_downgraded"
    assert payload["recent_autonomy_changes"][0]["status"] == "applied"
    assert payload["recent_autonomy_changes"][0]["change_type"] == "downgrade"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "role.drift_detected" in event_types


def test_approved_role_autonomy_change_updates_role_level(client):
    review = client.post(
        "/v1/roles/metatron/reviews",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "summary": "Metatron met the next autonomy threshold.",
            "metrics": {
                "successful_runs": 30,
                "policy_violations": 0,
                "duplicate_effect_attempts": 0,
                "hidden_failures": 0,
                "low_quality_outputs": 0,
                "scope_violations": 0,
                "rollback_rate": 0.0,
            },
            "evidence_refs": ["artifact:metatron-eval-approve"],
        },
    )
    assert review.status_code == 201
    decision_id = review.json()["pending_autonomy_change"]["decision_id"]

    approved = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo", "note": "Upgrade approved."},
    )
    assert approved.status_code == 200

    role = client.get("/v1/roles/metatron", params={"project_id": "boss-dogfood"})
    assert role.status_code == 200
    assert role.json()["autonomy_level"] == 2

    governance = client.get(
        "/v1/roles/metatron/governance",
        params={"project_id": "boss-dogfood"},
    )
    assert governance.status_code == 200
    payload = governance.json()
    assert payload["pending_autonomy_change"] is None
    assert payload["recent_autonomy_changes"][0]["status"] == "applied"
    assert payload["recent_autonomy_changes"][0]["to_level"] == 2


def test_role_autonomy_check_denies_unearned_real_execution(client):
    response = client.post(
        "/v1/roles/software_engineer/autonomy/check",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "action": "execute",
            "risk_tier": 1,
            "sandboxed": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is False
    assert payload["current_level"] == 1
    assert payload["required_capability"] == "execute_tier1_real"
    assert payload["required_level"] == 3
    assert payload["reason"] == "autonomy_level_too_low"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "role.autonomy_denied" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_role_autonomy_check_allows_sandbox_execution_at_level_two(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE roles
                SET autonomy_level = 2
                WHERE project_id = 'boss-dogfood'
                  AND role_key = 'software_engineer'
                """
            )
        )

    response = client.post(
        "/v1/roles/software_engineer/autonomy/check",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "action": "execute",
            "risk_tier": 1,
            "sandboxed": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is True
    assert payload["current_level"] == 2
    assert payload["required_capability"] == "execute_tier1_sandbox"
    assert payload["required_level"] == 2
    assert "execute_tier1_sandbox" in payload["allowed_capabilities"]


def test_role_scope_check_allows_rolespec_skill_without_trust_change(client):
    response = client.post(
        "/v1/roles/software_engineer/scope/check",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "action": "github.create_pr_sandbox",
            "evidence_ref": "job:software-factory-allowed",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is True
    assert payload["reason"] == "allowed_by_rolespec"
    assert payload["trust_score_before"] == 50
    assert payload["trust_score_after"] == 50
    assert payload["autonomy_level_before"] == payload["autonomy_level_after"]
    assert payload["scope_violation_count"] == 0
    assert payload["artifact_id"] is None

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "role.scope_allowed" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_role_scope_check_blocks_forbidden_skill_and_records_drift(client):
    response = client.post(
        "/v1/roles/software_engineer/scope/check",
        json={
            "project_id": "boss-dogfood",
            "actor": "vp_agents",
            "action": "infra.create_node_real",
            "skill_id": "infra.create_node_real",
            "evidence_ref": "event:out-of-scope-infra",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is False
    assert payload["reason"] == "forbidden_by_rolespec"
    assert payload["trust_score_before"] == 50
    assert payload["trust_score_after"] == 40
    assert payload["autonomy_level_before"] == 1
    assert payload["autonomy_level_after"] == 0
    assert payload["scope_violation_count"] == 1
    assert payload["artifact_id"].startswith("art_")
    assert payload["autonomy_change_id"].startswith("rac_")

    role = client.get(
        "/v1/roles/software_engineer",
        params={"project_id": "boss-dogfood"},
    )
    assert role.status_code == 200
    assert role.json()["trust_score"] == 40
    assert role.json()["autonomy_level"] == 0
    assert role.json()["scope_violation_count"] == 1

    artifact = client.get(f"/v1/artifacts/{payload['artifact_id']}")
    assert artifact.status_code == 200
    assert artifact.json()["artifact_type"] == "role_scope_violation"
    assert artifact.json()["content"]["skill_id"] == "infra.create_node_real"
    assert artifact.json()["content"]["matched_forbidden_pattern"] == "infra.*"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "role.scope_violation" in event_types
    assert "role.trust_changed" in event_types
    assert "role.autonomy_changed" in event_types


def test_pack_install_is_idempotent_for_new_project(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM events WHERE project_id = 'boss-lab'"))
        connection.execute(
            text("DELETE FROM agent_module_installs WHERE project_id = 'boss-lab'")
        )
        connection.execute(text("DELETE FROM roles WHERE project_id = 'boss-lab'"))
        connection.execute(
            text("DELETE FROM pack_installs WHERE project_id = 'boss-lab'")
        )
        connection.execute(text("DELETE FROM projects WHERE id = 'boss-lab'"))
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('boss-lab', 'ws_boss', 'BOSS Lab', 'boss-lab')
                """
            )
        )
        api_main.members.seed_default_members(connection, "boss-lab", "test")

    first = client.post(
        "/v1/packs/install",
        json={"project_id": "boss-lab", "pack_id": "core_v1", "actor": "ceo"},
    )
    assert first.status_code == 201
    first_payload = first.json()
    assert first_payload["pack_id"] == "core_v1"
    assert first_payload["install_status"] == "installed"
    assert first_payload["role_count"] == 9

    second = client.post(
        "/v1/packs/install",
        json={"project_id": "boss-lab", "pack_id": "core_v1", "actor": "ceo"},
    )
    assert second.status_code == 201
    second_payload = second.json()
    assert second_payload["install_status"] == "already_installed"

    roles = client.get("/v1/roles", params={"project_id": "boss-lab"})
    assert roles.status_code == 200
    role_keys = [item["role_key"] for item in roles.json()["items"]]
    assert role_keys == [
        "cao",
        "coo",
        "cto",
        "metatron",
        "pm",
        "program_manager",
        "qa",
        "release_manager",
        "vp_agents",
    ]

    pack = client.get("/v1/packs/core_v1", params={"project_id": "boss-lab"})
    assert pack.status_code == 200
    assert pack.json()["installed"] is True


def test_agent_module_install_is_idempotent_and_updates_role_lineage(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM events WHERE project_id = 'boss-lab'"))
        connection.execute(
            text("DELETE FROM agent_module_installs WHERE project_id = 'boss-lab'")
        )
        connection.execute(text("DELETE FROM roles WHERE project_id = 'boss-lab'"))
        connection.execute(
            text("DELETE FROM pack_installs WHERE project_id = 'boss-lab'")
        )
        connection.execute(text("DELETE FROM projects WHERE id = 'boss-lab'"))
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('boss-lab', 'ws_boss', 'BOSS Lab', 'boss-lab')
                """
            )
        )
        api_main.members.seed_default_members(connection, "boss-lab", "test")

    first = client.post(
        "/v1/agent-modules/install",
        json={
            "project_id": "boss-lab",
            "module_id": "codex_software_engineer_v1",
            "actor": "ceo",
            "config": {"branch_prefix": "catalog/"},
        },
    )
    assert first.status_code == 201
    first_payload = first.json()
    assert first_payload["module_id"] == "codex_software_engineer_v1"
    assert first_payload["install_status"] == "installed"
    assert first_payload["role_result"]["role_key"] == "software_engineer"

    second = client.post(
        "/v1/agent-modules/install",
        json={
            "project_id": "boss-lab",
            "module_id": "codex_software_engineer_v1",
            "actor": "ceo",
            "config": {"branch_prefix": "catalog/"},
        },
    )
    assert second.status_code == 201
    assert second.json()["install_status"] == "already_installed"

    role = client.get(
        "/v1/roles/software_engineer", params={"project_id": "boss-lab"}
    )
    assert role.status_code == 200
    role_payload = role.json()
    assert role_payload["source_module_id"] == "codex_software_engineer_v1"
    assert role_payload["source_module_version"] == "1.0.0"

    module = client.get(
        "/v1/agent-modules/codex_software_engineer_v1",
        params={"project_id": "boss-lab"},
    )
    assert module.status_code == 200
    assert module.json()["config"]["branch_prefix"] == "catalog/"

    task = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-lab",
            "title": "Catalog proposal task",
            "dedupe_key": "catalog-proposal-task",
        },
    )
    assert task.status_code == 201
    task_id = task.json()["id"]

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, project_id, task_id, artifact_type, title, uri, content, content_hash
                )
                VALUES (
                    'art_catalog_plan', 'boss-lab', :task_id, 'plan_draft', 'Catalog plan',
                    'file://artifacts/catalog-plan.json',
                    CAST(:content AS JSONB), 'hash-catalog-plan'
                )
                """
            ),
            {"task_id": task_id, "content": json.dumps({"summary": "Catalog plan"})},
        )
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, default_action, status
                )
                VALUES (
                    'dec_catalog_plan', 'boss-lab', :task_id, 'Review plan draft for catalog task',
                    CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'pending'
                )
                """
            ),
            {
                "task_id": task_id,
                "recommendation": json.dumps(
                    {
                        "action": "review_plan_draft",
                        "artifact_id": "art_catalog_plan",
                    }
                ),
            },
        )

    approve = client.post(
        "/v1/decisions/dec_catalog_plan/approve",
        json={"actor": "ceo", "note": "Queue the catalog-backed proposal."},
    )
    assert approve.status_code == 200

    with engine.begin() as connection:
        queued_job = connection.execute(
            text(
                """
                SELECT payload
                FROM jobs
                WHERE project_id = 'boss-lab'
                  AND kind = 'software_factory.propose_pr'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()

    payload = queued_job["payload"]
    assert payload["agent_module_id"] == "codex_software_engineer_v1"
    assert payload["branch_name"].startswith("catalog/proposal-")


def test_skill_registry_and_seeded_entitlement_sources_are_visible(client):
    skills = client.get("/v1/skills")
    assert skills.status_code == 200
    skill_payload = skills.json()
    skill_ids = [item["id"] for item in skill_payload["items"]]
    assert "model.route_via_entitlement_broker" in skill_ids
    assert "model.generate_text" in skill_ids
    assert "software_factory.openhands_run" not in skill_ids
    for skill in skill_payload["items"]:
        assert_complete_skill_spec(skill)

    sources = client.get(
        "/v1/entitlements/sources",
        params={"project_id": "boss-dogfood"},
    )
    assert sources.status_code == 200
    payload = sources.json()
    openai_api = next(item for item in payload["items"] if item["source_id"] == "openai_api")
    assert openai_api["provider"] == "openai"
    assert openai_api["runtime_supported"] is True
    assert openai_api["effective_status"] in {"active", "missing_credentials"}


def test_skill_spec_validator_rejects_mock_or_incomplete_specs():
    with pytest.raises(ValueError, match="mock_mode"):
        api_main.entitlements.validate_skill_spec(
            {
                "id": "model.fake",
                "kind": "model",
                "risk_tier": 1,
                "mock_mode": True,
                "description": "Invalid mock skill.",
                "input_schema": {"prompt": "string"},
                "output_schema": {"text": "string"},
                "data_boundary": "internal",
                "cost": {"estimator": "fixed", "category": "model"},
                "retry": {"max_attempts": 1, "backoff": "none"},
                "permissions": {
                    "allowed_callers": ["worker"],
                    "required_permissions": ["model_invoke"],
                },
                "policy": {"requires_budget_reservation": True},
            },
            "test skill",
        )


def test_router_preview_selects_openai_api_and_writes_log(client):
    previous = api_main.entitlements.settings.openai_api_key
    api_main.entitlements.settings.openai_api_key = "test-openai-key"
    try:
        preview = client.post(
            "/v1/router/preview",
            json={
                "project_id": "boss-dogfood",
                "skill_id": "model.generate_text",
                "task_kind": "planning",
                "workload_class": "owner_sandbox",
                "risk_tier": 1,
                "data_boundary": "internal",
                "preferred_capabilities": ["planning", "summarization"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.entitlements.settings.openai_api_key = previous

    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] == "openai_api"
    assert payload["provider"] == "openai"
    assert payload["model_id"].startswith("openai:")
    assert payload["data_boundary"] == "internal"
    assert payload["denial_reason"] is None

    logs = client.get("/v1/router/logs", params={"project_id": "boss-dogfood"})
    assert logs.status_code == 200
    log = next(
        item
        for item in logs.json()["items"]
        if item["id"] == payload["route_decision_log_id"]
    )
    assert log["data_boundary"] == "internal"


def test_router_preview_denies_restricted_boundary_for_unapproved_external_sources(client):
    previous = api_main.entitlements.settings.openai_api_key
    api_main.entitlements.settings.openai_api_key = "test-openai-key"
    try:
        preview = client.post(
            "/v1/router/preview",
            json={
                "project_id": "boss-dogfood",
                "skill_id": "model.generate_text",
                "task_kind": "planning",
                "workload_class": "owner_sandbox",
                "risk_tier": 1,
                "data_boundary": "restricted",
                "preferred_capabilities": ["planning"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.entitlements.settings.openai_api_key = previous

    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] is None
    assert payload["data_boundary"] == "restricted"
    assert payload["denial_reason"] == "no_eligible_source"
    assert any(
        item["source_id"] == "openai_api"
        and item["reason"] == "data_boundary_not_allowed"
        for item in payload["rejected_sources"]
    )
    assert payload["decision"]["data_boundary"] == "restricted"


def test_router_preview_allows_local_only_data_on_approved_local_model_source(client):
    previous_enabled = api_main.entitlements.settings.local_model_enabled
    previous_model_id = api_main.entitlements.settings.local_model_id
    api_main.entitlements.settings.local_model_enabled = True
    api_main.entitlements.settings.local_model_id = "approved-local-test"
    try:
        preview = client.post(
            "/v1/router/preview",
            json={
                "project_id": "boss-dogfood",
                "skill_id": "model.generate_text",
                "task_kind": "privacy_sensitive",
                "workload_class": "internal_batch",
                "risk_tier": 1,
                "data_boundary": "local-only",
                "preferred_capabilities": ["privacy_sensitive"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.entitlements.settings.local_model_enabled = previous_enabled
        api_main.entitlements.settings.local_model_id = previous_model_id

    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] == "approved_local_model_host"
    assert payload["provider"] == "local"
    assert payload["model_id"] is None
    assert payload["data_boundary"] == "local-only"
    assert payload["denial_reason"] is None


def test_router_preview_denies_multi_tenant_cli_source(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM events WHERE project_id = 'boss-router'"))
        connection.execute(text("DELETE FROM routing_logs WHERE project_id = 'boss-router'"))
        connection.execute(
            text("DELETE FROM entitlement_sources WHERE project_id = 'boss-router'")
        )
        connection.execute(text("DELETE FROM projects WHERE id = 'boss-router'"))
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES ('boss-router', 'ws_boss', 'BOSS Router', 'boss-router')
                """
            )
        )
        api_main.members.seed_default_members(connection, "boss-router", "test")

    source = client.post(
        "/v1/entitlements/sources",
        json={
            "project_id": "boss-router",
            "provider": "openai",
            "source_id": "owner_cli_test",
            "source_kind": "official_cli",
            "entitlement_kind": "subscription",
            "owner_type": "workspace_operator",
            "owner_id": "founder",
            "workload_classes_allowed": [
                "owner_sandbox",
                "single_tenant_customer",
                "multi_tenant_customer",
            ],
            "multi_tenant_allowed": False,
            "supported_models": ["gpt-5.2-codex"],
            "task_strengths": ["planning", "coding"],
            "risk_tier_max": 1,
            "status": "active",
            "observed_capacity": {"credential_present": True},
            "runtime_supported": True,
            "actor": "ceo",
        },
    )
    assert source.status_code == 201

    preview = client.post(
        "/v1/router/preview",
        json={
            "project_id": "boss-router",
            "skill_id": "model.generate_text",
            "task_kind": "planning",
            "workload_class": "multi_tenant_customer",
            "risk_tier": 1,
            "preferred_capabilities": ["planning"],
            "actor": "ceo",
        },
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] is None
    assert payload["denial_reason"] == "no_eligible_source"
    assert any(
        item["reason"] in {"provider_policy_disallows_workload", "multi_tenant_not_allowed"}
        for item in payload["rejected_sources"]
    )


def test_command_proposal_creates_task_job_artifact_and_task_lookup(client, engine):
    response = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Plan the next sandbox verification pass.",
            "constraints": {"deadline": "soon"},
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["task_id"]
    assert payload["job_id"]
    assert payload["artifact_id"]

    task_response = client.get(f"/v1/tasks/{payload['task_id']}")
    assert task_response.status_code == 200
    task_payload = task_response.json()
    assert task_payload["id"] == payload["task_id"]
    assert task_payload["status"] == "open"
    assert task_payload["priority"] == "p1"

    list_response = client.get("/v1/tasks", params={"project_id": "boss-dogfood"})
    assert list_response.status_code == 200
    listed_ids = [item["id"] for item in list_response.json()["items"]]
    assert payload["task_id"] in listed_ids

    with engine.begin() as connection:
        job_kind = connection.execute(
            text("SELECT kind FROM jobs WHERE id = :job_id"),
            {"job_id": payload["job_id"]},
        ).scalar_one()
        artifact_type = connection.execute(
            text("SELECT artifact_type FROM artifacts WHERE id = :artifact_id"),
            {"artifact_id": payload["artifact_id"]},
        ).scalar_one()
    assert job_kind == "plan.command"
    assert artifact_type == "command_brief"


def test_task_create_supports_hierarchy_dependencies_owner_and_due_date(client):
    parent = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Parent initiative",
            "dedupe_key": "task-lineage-parent",
            "owner_role_id": "pm",
        },
    )
    assert parent.status_code == 201
    parent_id = parent.json()["id"]

    dependency = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Dependency task",
            "dedupe_key": "task-lineage-dependency",
            "owner_role_id": "cto",
        },
    )
    assert dependency.status_code == 201
    dependency_id = dependency.json()["id"]

    child = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Child implementation task",
            "description": "Depends on a prerequisite and belongs to the parent.",
            "dedupe_key": "task-lineage-child",
            "owner_role_id": "software_engineer",
            "parent_task_id": parent_id,
            "dependency_task_ids": [dependency_id],
            "due_at": "2026-06-30T12:00:00Z",
        },
    )
    assert child.status_code == 201
    payload = child.json()
    assert payload["owner_role_id"] == "software_engineer"
    assert payload["parent_task_id"] == parent_id
    assert payload["dependency_task_ids"] == [dependency_id]
    assert payload["due_at"].startswith("2026-06-30T12:00:00")

    fetched = client.get(f"/v1/tasks/{payload['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["parent_task_id"] == parent_id

    listed = client.get("/v1/tasks", params={"project_id": "boss-dogfood"})
    assert listed.status_code == 200
    child_from_list = next(
        item for item in listed.json()["items"] if item["id"] == payload["id"]
    )
    assert child_from_list["dependency_task_ids"] == [dependency_id]

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    child_created = next(
        item
        for item in events.json()["items"]
        if item["event_type"] == "task.created" and item["entity_id"] == payload["id"]
    )
    assert child_created["data"]["parent_task_id"] == parent_id
    assert child_created["data"]["dependency_task_ids"] == [dependency_id]


def test_task_create_rejects_unknown_parent_dependency_and_owner(client):
    bad_parent = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Bad parent",
            "dedupe_key": "task-lineage-bad-parent",
            "parent_task_id": "tsk_missing_parent",
        },
    )
    assert bad_parent.status_code == 400
    assert "parent_task_id" in bad_parent.json()["detail"]

    bad_dependency = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Bad dependency",
            "dedupe_key": "task-lineage-bad-dependency",
            "dependency_task_ids": ["tsk_missing_dependency"],
        },
    )
    assert bad_dependency.status_code == 400
    assert "dependency_task_ids" in bad_dependency.json()["detail"]

    bad_owner = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Bad owner",
            "dedupe_key": "task-lineage-bad-owner",
            "owner_role_id": "unknown_role",
        },
    )
    assert bad_owner.status_code == 400
    assert "owner_role_id" in bad_owner.json()["detail"]


def test_canonical_work_objects_have_stable_ids_lifecycles_and_events(client):
    department = client.post(
        "/v1/departments",
        json={
            "project_id": "boss-dogfood",
            "department_key": "engineering",
            "name": "Engineering",
            "mission": "Ship tested BOSS software changes.",
            "actor": "ceo",
        },
    )
    assert department.status_code == 201
    assert department.json()["id"].startswith("dept_")
    assert department.json()["status"] == "active"

    policy = client.post(
        "/v1/policies",
        json={
            "project_id": "boss-dogfood",
            "policy_key": "default_safety",
            "version": 1,
            "title": "Default Safety Policy",
            "body": {"no_external_effect_without_receipt": True},
            "actor": "ceo",
        },
    )
    assert policy.status_code == 201
    assert policy.json()["id"].startswith("pol_")
    assert policy.json()["status"] == "active"

    plan = client.post(
        "/v1/plans",
        json={
            "project_id": "boss-dogfood",
            "title": "Canonical object plan",
            "objective": "Represent plans as first-class canonical objects.",
            "phases": ["schema", "api", "tests"],
            "success_criteria": ["objects are listable"],
            "risks": ["schema drift"],
            "actor": "ceo",
        },
    )
    assert plan.status_code == 201
    assert plan.json()["id"].startswith("plan_")
    assert plan.json()["status"] == "draft"

    meeting = client.post(
        "/v1/meetings",
        json={
            "project_id": "boss-dogfood",
            "title": "Canonical object review",
            "purpose": "Review stable object coverage.",
            "participants": ["ceo", "cao"],
            "agenda": ["coverage", "risks"],
            "actor": "ceo",
        },
    )
    assert meeting.status_code == 201
    assert meeting.json()["id"].startswith("mtg_")
    assert meeting.json()["status"] == "scheduled"

    for path in ("/v1/departments", "/v1/policies", "/v1/plans", "/v1/meetings"):
        response = client.get(path, params={"project_id": "boss-dogfood"})
        assert response.status_code == 200
        assert response.json()["items"]

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "department.upserted" in event_types
    assert "policy.upserted" in event_types
    assert "plan.created" in event_types
    assert "meeting.created" in event_types


def test_artifact_list_and_detail_return_created_artifacts(client):
    response = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Capture artifact visibility in the console.",
            "constraints": {"deadline": "today"},
        },
    )
    assert response.status_code == 201
    artifact_id = response.json()["artifact_id"]

    artifact_list = client.get(
        "/v1/artifacts",
        params={"project_id": "boss-dogfood", "limit": 10},
    )
    assert artifact_list.status_code == 200
    payload = artifact_list.json()
    assert payload["project_id"] == "boss-dogfood"
    assert artifact_id in [item["id"] for item in payload["items"]]

    artifact_detail = client.get(f"/v1/artifacts/{artifact_id}")
    assert artifact_detail.status_code == 200
    detail = artifact_detail.json()
    assert detail["artifact_type"] == "command_brief"
    assert detail["content"]["command"] == "Capture artifact visibility in the console."
    assert detail["content"]["constraints"]["deadline"] == "today"


def test_change_journal_lists_and_details_change_artifacts(client, engine):
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Change journal task",
            "dedupe_key": "change-journal-task",
        },
    )
    assert task.status_code == 201
    task_id = task.json()["id"]
    content = {
        "summary": "Opened a sandbox PR with a tested UI change.",
        "diff_summary": "Adds a decision inbox default action panel.",
        "files_changed": ["apps/web/app/page.tsx", "apps/api/app/main.py"],
        "rollback_plan": "Close the sandbox PR branch.",
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, project_id, task_id, artifact_type, title, uri, content,
                    content_hash, data_boundary
                )
                VALUES (
                    'art_change_journal_diff', 'boss-dogfood', :task_id,
                    'diff_manifest', 'Diff manifest for change journal',
                    'file://artifacts/change-journal.json',
                    CAST(:content AS JSONB), 'hash-change-journal', 'internal'
                )
                """
            ),
            {"task_id": task_id, "content": json.dumps(content)},
        )

    journal = client.get(
        "/v1/change-journal",
        params={"project_id": "boss-dogfood", "limit": 5},
    )
    assert journal.status_code == 200
    payload = journal.json()
    item = next(
        item for item in payload["items"] if item["id"] == "art_change_journal_diff"
    )
    assert item["change_kind"] == "software_factory"
    assert item["diff_summary"] == "Adds a decision inbox default action panel."
    assert item["files_changed"] == [
        "apps/web/app/page.tsx",
        "apps/api/app/main.py",
    ]
    assert item["rollback_plan"] == "Close the sandbox PR branch."

    detail = client.get(
        "/v1/change-journal/art_change_journal_diff",
        params={"project_id": "boss-dogfood"},
    )
    assert detail.status_code == 200
    assert detail.json()["content"]["summary"] == content["summary"]

    wrong_project = client.get(
        "/v1/change-journal/art_change_journal_diff",
        params={"project_id": "boss-lab"},
    )
    assert wrong_project.status_code == 404


def test_data_boundary_propagates_to_work_objects_receipts_and_audit(client, engine):
    response = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Handle restricted customer support export.",
            "constraints": {"data_boundary": "restricted"},
        },
    )
    assert response.status_code == 201
    created = response.json()

    task = client.get(f"/v1/tasks/{created['task_id']}")
    assert task.status_code == 200
    assert task.json()["data_boundary"] == "restricted"

    job = client.get(f"/v1/jobs/{created['job_id']}")
    assert job.status_code == 200
    assert job.json()["data_boundary"] == "restricted"

    artifact = client.get(f"/v1/artifacts/{created['artifact_id']}")
    assert artifact.status_code == 200
    assert artifact.json()["data_boundary"] == "restricted"

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-boundary",
            "capabilities": ["plan.command"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()["job"]
    assert leased["id"] == created["job_id"]
    assert leased["data_boundary"] == "restricted"

    with engine.begin() as connection:
        api_main.receipts.create_or_reuse(
            connection,
            api_main.settings.default_org_id,
            api_main.settings.default_workspace_id,
            "boss-dogfood",
            "boundary-receipt-1",
            "github.pr.create",
            {
                "target_repo": "boss-sandbox",
                "task_id": created["task_id"],
                "data_boundary": "restricted",
            },
            {"pull_request": {"title": "Boundary proof"}},
            created_by_job_id=created["job_id"],
        )

    receipts = client.get("/v1/receipts", params={"project_id": "boss-dogfood"})
    assert receipts.status_code == 200
    receipt = next(
        item
        for item in receipts.json()["items"]
        if item["dedupe_key"] == "boundary-receipt-1"
    )
    assert receipt["data_boundary"] == "restricted"

    audit = client.get(
        "/v1/audit/export",
        params={"project_id": "boss-dogfood", "task_id": created["task_id"]},
    )
    assert audit.status_code == 200
    payload = audit.json()
    assert payload["tasks"][0]["data_boundary"] == "restricted"
    assert payload["jobs"][0]["data_boundary"] == "restricted"
    assert payload["artifacts"][0]["data_boundary"] == "restricted"
    assert payload["receipts"][0]["data_boundary"] == "restricted"


def test_invalid_data_boundary_is_rejected_without_creating_work(client):
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Invalid boundary task",
            "dedupe_key": "invalid-boundary-task",
            "data_boundary": "secret",
        },
    )
    assert task.status_code == 422

    command = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Use an invalid data boundary.",
            "constraints": {"data_boundary": "secret"},
        },
    )
    assert command.status_code == 400
    assert "data_boundary" in command.json()["detail"]

    job = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.command",
            "payload": {"data_boundary": "secret"},
        },
    )
    assert job.status_code == 400
    assert "data_boundary" in job.json()["detail"]


def test_comms_outbox_and_inbox_are_visible(client):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "subject": "BOSS digest",
            "body": "System health summary.",
            "dedupe_key": "outbox-digest-1",
        },
    )
    assert outbox.status_code == 201
    outbox_payload = outbox.json()
    assert outbox_payload["status"] == "draft"

    inbox = client.post(
        "/v1/comms/inbox/ingest",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder",
            "subject": "Need status",
            "body": "What changed today?",
            "raw_ref": "mailbox-123",
        },
    )
    assert inbox.status_code == 201
    inbox_payload = inbox.json()
    assert inbox_payload["status"] == "received"

    outbox_list = client.get(
        "/v1/comms/outbox", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert outbox_list.status_code == 200
    assert outbox_payload["id"] in [item["id"] for item in outbox_list.json()["items"]]

    inbox_list = client.get(
        "/v1/comms/inbox", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert inbox_list.status_code == 200
    assert inbox_payload["id"] in [item["id"] for item in inbox_list.json()["items"]]


def test_decision_digest_creates_artifact_and_optional_outbox_draft(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, question, recommendation, options,
                    risk_tier, cost_summary, deadline_at, default_action, status
                )
                VALUES
                    (
                        'dec_digest_urgent',
                        'boss-dogfood',
                        'Approve a high-risk sandbox release?',
                        '{"action":"approve_release"}'::jsonb,
                        '[{"label":"Approve","action":"approve"}]'::jsonb,
                        3,
                        '{"estimated_usd_micros": 50000}'::jsonb,
                        now() + interval '2 hours',
                        'hold',
                        'pending'
                    ),
                    (
                        'dec_digest_batch',
                        'boss-dogfood',
                        'Approve low-risk copy update?',
                        '{"action":"approve_copy"}'::jsonb,
                        '[{"label":"Approve","action":"approve"}]'::jsonb,
                        1,
                        '{"estimated_usd_micros": 1000}'::jsonb,
                        now() + interval '1 day',
                        'hold',
                        'pending'
                    )
                """
            )
        )

    digest = client.post(
        "/v1/decisions/digests",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "channel": "email:founder@example.com",
            "dedupe_key": "decision-digest-outbox-1",
        },
    )
    assert digest.status_code == 201
    payload = digest.json()
    assert payload["status"] == "created"
    assert payload["decision_count"] >= 2
    assert payload["urgent_count"] >= 1
    assert payload["batched_count"] >= 1
    assert "dec_digest_urgent" in payload["body"]
    assert "dec_digest_batch" in payload["body"]
    assert payload["outbox_message_id"]

    with engine.begin() as connection:
        artifact = connection.execute(
            text(
                """
                SELECT artifact_type, content
                FROM artifacts
                WHERE id = :artifact_id
                """
            ),
            {"artifact_id": payload["artifact_id"]},
        ).mappings().one()
        outbox = connection.execute(
            text(
                """
                SELECT status, subject, body
                FROM outbox_messages
                WHERE id = :message_id
                """
            ),
            {"message_id": payload["outbox_message_id"]},
        ).mappings().one()
        event_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM events
                WHERE event_type = 'decision.digest_created'
                  AND entity_id = :artifact_id
                """
            ),
            {"artifact_id": payload["artifact_id"]},
        ).scalar_one()

    assert artifact["artifact_type"] == "decision_digest"
    assert artifact["content"]["urgent"]
    assert artifact["content"]["batched_low_risk"]
    assert outbox["status"] == "draft"
    assert outbox["subject"] == "BOSS decision digest"
    assert "dec_digest_urgent" in outbox["body"]
    assert event_count == 1


def test_inbox_decision_reply_approves_decision(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, question, recommendation, options,
                    risk_tier, cost_summary, deadline_at, default_action, status
                )
                VALUES (
                    'dec_inbox_reply_approve',
                    'boss-dogfood',
                    'Approve from inbound email?',
                    '{"action":"approve"}'::jsonb,
                    '[{"label":"Approve","action":"approve"}]'::jsonb,
                    1,
                    '{}'::jsonb,
                    now() + interval '1 day',
                    'hold',
                    'pending'
                )
                """
            )
        )

    inbox = client.post(
        "/v1/comms/inbox/ingest",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder@example.com",
            "subject": "Approve decision dec_inbox_reply_approve",
            "body": "Approved. Ship the safe change.",
            "raw_ref": "email-thread-approve-1",
            "actor": "ceo",
        },
    )
    assert inbox.status_code == 201
    payload = inbox.json()
    assert payload["status"] == "actioned"
    assert payload["parsed"]["action_result"]["decision_id"] == "dec_inbox_reply_approve"
    assert payload["parsed"]["action_result"]["status"] == "approved"

    with engine.begin() as connection:
        decision_status = connection.execute(
            text(
                """
                SELECT status
                FROM decisions
                WHERE id = 'dec_inbox_reply_approve'
                """
            )
        ).scalar_one()
        actioned_event = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM events
                WHERE event_type = 'inbox.actioned'
                  AND entity_id = :message_id
                """
            ),
            {"message_id": payload["id"]},
        ).scalar_one()

    assert decision_status == "approved"
    assert actioned_event == 1


def test_inbox_unparseable_decision_reply_deadletters(client, engine):
    inbox = client.post(
        "/v1/comms/inbox/ingest",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder@example.com",
            "subject": "Decision approval",
            "body": "Approve this decision, but no ID is included.",
            "raw_ref": "email-thread-missing-id-1",
            "actor": "ceo",
        },
    )
    assert inbox.status_code == 201
    payload = inbox.json()
    assert payload["status"] == "deadlettered"
    assert payload["parsed"]["deadletter"]["reason"] == (
        "decision_reply_missing_id_or_action"
    )

    with engine.begin() as connection:
        deadletter = connection.execute(
            text(
                """
                SELECT source_kind, source_id, reason
                FROM deadletters
                WHERE source_id = :message_id
                """
            ),
            {"message_id": payload["id"]},
        ).mappings().one()

    assert deadletter["source_kind"] == "inbox_message"
    assert deadletter["reason"] == "decision_reply_missing_id_or_action"


def test_comms_messages_carry_data_boundaries_and_delivery_jobs_inherit_them(client):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "Confidential delivery job.",
            "data_boundary": "confidential",
            "dedupe_key": "outbox-boundary-1",
        },
    )
    assert outbox.status_code == 201
    outbox_payload = outbox.json()
    assert outbox_payload["data_boundary"] == "confidential"

    inbox = client.post(
        "/v1/comms/inbox/ingest",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder",
            "body": "Restricted inbound request.",
            "raw_ref": "restricted-mail-1",
            "data_boundary": "restricted",
        },
    )
    assert inbox.status_code == 201
    assert inbox.json()["data_boundary"] == "restricted"

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Queue boundary-aware outbound delivery.",
        },
    )
    assert reserve.status_code == 201
    queued = client.post(
        f"/v1/comms/outbox/{outbox_payload['id']}/queue",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reserve.json()["reservation_id"],
            "dedupe_key": "outbox-boundary-job-1",
        },
    )
    assert queued.status_code == 201
    assert queued.json()["data_boundary"] == "confidential"

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-comms-boundary",
            "capabilities": ["comms.outbox.deliver"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    assert poll.json()["job"]["data_boundary"] == "confidential"

    outbox_list = client.get(
        "/v1/comms/outbox", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert outbox_list.status_code == 200
    listed_outbox = next(
        item for item in outbox_list.json()["items"] if item["id"] == outbox_payload["id"]
    )
    assert listed_outbox["data_boundary"] == "confidential"

    inbox_list = client.get(
        "/v1/comms/inbox", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert inbox_list.status_code == 200
    listed_inbox = next(
        item for item in inbox_list.json()["items"] if item["id"] == inbox.json()["id"]
    )
    assert listed_inbox["data_boundary"] == "restricted"


def test_slack_delivery_blocks_restricted_boundary_before_receipt(client, engine):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "Restricted outbound message.",
            "data_boundary": "restricted",
            "dedupe_key": "outbox-restricted-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Restricted Slack send should be blocked before receipt.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    delivery = client.post(
        f"/v1/comms/outbox/{message_id}/deliver",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "dedupe_key": "restricted-slack-delivery",
        },
    )
    assert delivery.status_code == 200
    payload = delivery.json()
    assert payload["status"] == "blocked"
    assert payload["data_boundary"] == "restricted"
    assert "Slack" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text(
                """
                SELECT status
                FROM budget_reservations
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        ).scalar_one()
        receipt_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM receipts
                WHERE dedupe_key = 'restricted-slack-delivery'
                """
            )
        ).scalar_one()
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()

    assert reservation_status == "released"
    assert receipt_count == 0
    assert outbox_status == "blocked"


def test_email_delivery_blocks_restricted_boundary_before_receipt(client, engine):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder@example.com",
            "body": "Restricted outbound email.",
            "data_boundary": "restricted",
            "dedupe_key": "outbox-email-restricted-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Restricted email send should be blocked before receipt.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    delivery = client.post(
        f"/v1/comms/outbox/{message_id}/deliver",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "dedupe_key": "restricted-email-delivery",
        },
    )
    assert delivery.status_code == 200
    payload = delivery.json()
    assert payload["status"] == "blocked"
    assert payload["data_boundary"] == "restricted"
    assert "Email" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text(
                """
                SELECT status
                FROM budget_reservations
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        ).scalar_one()
        receipt_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM receipts
                WHERE dedupe_key = 'restricted-email-delivery'
                """
            )
        ).scalar_one()
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()

    assert reservation_status == "released"
    assert receipt_count == 0
    assert outbox_status == "blocked"


def test_comms_invalid_data_boundary_is_rejected(client):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "Invalid boundary.",
            "dedupe_key": "outbox-invalid-boundary",
            "data_boundary": "secret",
        },
    )
    assert outbox.status_code == 422

    inbox = client.post(
        "/v1/comms/inbox/ingest",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder",
            "body": "Invalid boundary.",
            "data_boundary": "secret",
        },
    )
    assert inbox.status_code == 422


def test_queue_outbox_delivery_creates_job_and_marks_message_queued(client, engine):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "Queue this message.",
            "dedupe_key": "outbox-queue-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Queue outbound Slack delivery.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    queued = client.post(
        f"/v1/comms/outbox/{message_id}/queue",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "dedupe_key": "outbox-queue-job-1",
        },
    )
    assert queued.status_code == 201
    payload = queued.json()
    assert payload["kind"] == "comms.outbox.deliver"

    with engine.begin() as connection:
        message_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
        attached_job_id = connection.execute(
            text(
                "SELECT job_id FROM budget_reservations WHERE id = :reservation_id"
            ),
            {"reservation_id": reservation_id},
        ).scalar_one()

    assert message_status == "queued"
    assert attached_job_id == payload["id"]


def test_outbox_queue_defers_during_quiet_hours_and_urgent_bypasses(
    client, engine
):
    previous_enabled = api_main.comms.settings.comms_quiet_hours_enabled
    previous_start = api_main.comms.settings.comms_quiet_hours_start_utc
    previous_end = api_main.comms.settings.comms_quiet_hours_end_utc
    api_main.comms.settings.comms_quiet_hours_enabled = True
    api_main.comms.settings.comms_quiet_hours_start_utc = "00:00"
    api_main.comms.settings.comms_quiet_hours_end_utc = "23:59"
    try:
        normal_outbox = client.post(
            "/v1/comms/outbox",
            json={
                "project_id": "boss-dogfood",
                "channel": "slack:#ops",
                "body": "Normal delivery should wait for quiet hours.",
                "dedupe_key": "outbox-quiet-normal-1",
            },
        )
        assert normal_outbox.status_code == 201
        normal_reserve = client.post(
            "/v1/budget/reserve",
            json={
                "project_id": "boss-dogfood",
                "category": "comms",
                "amount_usd_micros": 50_000,
                "reason": "Quiet-hours normal delivery.",
            },
        )
        assert normal_reserve.status_code == 201
        normal_queued = client.post(
            f"/v1/comms/outbox/{normal_outbox.json()['id']}/queue",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": normal_reserve.json()["reservation_id"],
                "dedupe_key": "outbox-quiet-normal-job-1",
                "urgency": "normal",
            },
        )
        assert normal_queued.status_code == 201

        urgent_outbox = client.post(
            "/v1/comms/outbox",
            json={
                "project_id": "boss-dogfood",
                "channel": "slack:#ops",
                "body": "Urgent delivery can bypass quiet hours.",
                "dedupe_key": "outbox-quiet-urgent-1",
            },
        )
        assert urgent_outbox.status_code == 201
        urgent_reserve = client.post(
            "/v1/budget/reserve",
            json={
                "project_id": "boss-dogfood",
                "category": "comms",
                "amount_usd_micros": 50_000,
                "reason": "Quiet-hours urgent delivery.",
            },
        )
        assert urgent_reserve.status_code == 201
        urgent_queued = client.post(
            f"/v1/comms/outbox/{urgent_outbox.json()['id']}/queue",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": urgent_reserve.json()["reservation_id"],
                "dedupe_key": "outbox-quiet-urgent-job-1",
                "urgency": "urgent",
            },
        )
        assert urgent_queued.status_code == 201

        with engine.begin() as connection:
            normal_job = connection.execute(
                text(
                    """
                    SELECT run_after, created_at, payload
                    FROM jobs
                    WHERE id = :job_id
                    """
                ),
                {"job_id": normal_queued.json()["id"]},
            ).mappings().one()
            urgent_job = connection.execute(
                text(
                    """
                    SELECT run_after, created_at, payload
                    FROM jobs
                    WHERE id = :job_id
                    """
                ),
                {"job_id": urgent_queued.json()["id"]},
            ).mappings().one()
            deferred_event_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM events
                    WHERE event_type = 'outbox.deferred'
                      AND entity_id = :job_id
                      AND data->>'reason' = 'quiet_hours'
                    """
                ),
                {"job_id": normal_queued.json()["id"]},
            ).scalar_one()
            urgent_deferred_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM events
                    WHERE event_type = 'outbox.deferred'
                      AND entity_id = :job_id
                    """
                ),
                {"job_id": urgent_queued.json()["id"]},
            ).scalar_one()

        assert normal_job["run_after"] > normal_job["created_at"]
        assert normal_job["payload"]["urgency"] == "normal"
        assert urgent_job["run_after"] <= urgent_job["created_at"] + timedelta(seconds=1)
        assert urgent_job["payload"]["urgency"] == "urgent"
        assert deferred_event_count == 1
        assert urgent_deferred_count == 0
    finally:
        api_main.comms.settings.comms_quiet_hours_enabled = previous_enabled
        api_main.comms.settings.comms_quiet_hours_start_utc = previous_start
        api_main.comms.settings.comms_quiet_hours_end_utc = previous_end


def test_outbox_queue_throttles_nonurgent_delivery(client, engine):
    previous_limit = api_main.comms.settings.comms_throttle_max_queued_per_hour
    previous_defer = api_main.comms.settings.comms_throttle_defer_minutes
    previous_quiet = api_main.comms.settings.comms_quiet_hours_enabled
    api_main.comms.settings.comms_quiet_hours_enabled = False
    api_main.comms.settings.comms_throttle_max_queued_per_hour = 1
    api_main.comms.settings.comms_throttle_defer_minutes = 10
    try:
        first_outbox = client.post(
            "/v1/comms/outbox",
            json={
                "project_id": "boss-dogfood",
                "channel": "email:founder@example.com",
                "body": "First throttled-channel message.",
                "dedupe_key": "outbox-throttle-first-1",
            },
        )
        second_outbox = client.post(
            "/v1/comms/outbox",
            json={
                "project_id": "boss-dogfood",
                "channel": "email:founder@example.com",
                "body": "Second throttled-channel message.",
                "dedupe_key": "outbox-throttle-second-1",
            },
        )
        assert first_outbox.status_code == 201
        assert second_outbox.status_code == 201

        first_reserve = client.post(
            "/v1/budget/reserve",
            json={
                "project_id": "boss-dogfood",
                "category": "comms",
                "amount_usd_micros": 50_000,
                "reason": "First throttle delivery.",
            },
        )
        second_reserve = client.post(
            "/v1/budget/reserve",
            json={
                "project_id": "boss-dogfood",
                "category": "comms",
                "amount_usd_micros": 50_000,
                "reason": "Second throttle delivery.",
            },
        )
        assert first_reserve.status_code == 201
        assert second_reserve.status_code == 201

        first_queued = client.post(
            f"/v1/comms/outbox/{first_outbox.json()['id']}/queue",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": first_reserve.json()["reservation_id"],
                "dedupe_key": "outbox-throttle-first-job-1",
            },
        )
        assert first_queued.status_code == 201
        second_queued = client.post(
            f"/v1/comms/outbox/{second_outbox.json()['id']}/queue",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": second_reserve.json()["reservation_id"],
                "dedupe_key": "outbox-throttle-second-job-1",
            },
        )
        assert second_queued.status_code == 201

        with engine.begin() as connection:
            first_job = connection.execute(
                text(
                    """
                    SELECT run_after, created_at
                    FROM jobs
                    WHERE id = :job_id
                    """
                ),
                {"job_id": first_queued.json()["id"]},
            ).mappings().one()
            second_job = connection.execute(
                text(
                    """
                    SELECT run_after, created_at
                    FROM jobs
                    WHERE id = :job_id
                    """
                ),
                {"job_id": second_queued.json()["id"]},
            ).mappings().one()
            throttle_event_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM events
                    WHERE event_type = 'outbox.deferred'
                      AND entity_id = :job_id
                      AND data->>'reason' = 'throttled'
                    """
                ),
                {"job_id": second_queued.json()["id"]},
            ).scalar_one()

        assert first_job["run_after"] <= first_job["created_at"] + timedelta(seconds=1)
        assert second_job["run_after"] >= second_job["created_at"] + timedelta(
            minutes=9
        )
        assert throttle_event_count == 1
    finally:
        api_main.comms.settings.comms_throttle_max_queued_per_hour = previous_limit
        api_main.comms.settings.comms_throttle_defer_minutes = previous_defer
        api_main.comms.settings.comms_quiet_hours_enabled = previous_quiet


def test_outbox_delivery_missing_reservation_creates_visible_blocker(client, engine):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "Missing reservation should block visibly.",
            "dedupe_key": "outbox-missing-reservation-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    response = client.post(
        f"/v1/comms/outbox/{message_id}/deliver",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": "budget_reservation_missing",
            "dedupe_key": "outbox-missing-reservation-delivery-1",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]

    with engine.begin() as connection:
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
    assert outbox_status == "blocked"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "outbox.blocked" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_outbox_queue_rejects_cross_project_message_reference(client, engine):
    lab_project_id = "boss-comms-lab"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (
                    :project_id, 'ws_boss', 'BOSS Comms Lab', 'boss-comms-lab'
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"project_id": lab_project_id},
        )
        api_main.members.seed_default_members(connection, lab_project_id, "test")

    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "This belongs to the dogfood project.",
            "dedupe_key": "outbox-cross-project-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    response = client.post(
        f"/v1/comms/outbox/{message_id}/queue",
        json={
            "project_id": lab_project_id,
            "reservation_id": "reservation_not_reached",
            "dedupe_key": "outbox-cross-project-queue-1",
        },
    )

    assert response.status_code == 404
    assert "not found for project" in response.json()["detail"]


def test_outbox_delivery_blocks_when_slack_delivery_is_disabled(client, engine):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "Blocked message.",
            "dedupe_key": "outbox-blocked-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Attempt blocked Slack delivery.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous_allowed = api_main.comms.settings.slack_real_send_allowed
    previous_webhook = api_main.comms.settings.slack_webhook_url
    api_main.comms.settings.slack_real_send_allowed = False
    api_main.comms.settings.slack_webhook_url = None
    try:
        response = client.post(
            f"/v1/comms/outbox/{message_id}/deliver",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "outbox-blocked-delivery-1",
            },
        )
    finally:
        api_main.comms.settings.slack_real_send_allowed = previous_allowed
        api_main.comms.settings.slack_webhook_url = previous_webhook

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert payload["receipt_id"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :reservation_id"),
            {"reservation_id": reservation_id},
        ).scalar_one()
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()

    assert reservation_status == "released"
    assert outbox_status == "blocked"


def test_outbox_delivery_succeeds_and_creates_receipt(client, engine, monkeypatch):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "subject": "Deployment",
            "body": "The sandbox change is ready for review.",
            "dedupe_key": "outbox-success-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Send real Slack message.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous_allowed = api_main.comms.settings.slack_real_send_allowed
    previous_webhook = api_main.comms.settings.slack_webhook_url
    api_main.comms.settings.slack_real_send_allowed = True
    api_main.comms.settings.slack_webhook_url = "https://hooks.slack.com/services/test"
    monkeypatch.setattr(
        api_main.comms.slack,
        "post_message",
        lambda channel, subject, body: f"slack-webhook:{channel}",
    )
    try:
        response = client.post(
            f"/v1/comms/outbox/{message_id}/deliver",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "outbox-success-delivery-1",
            },
        )
    finally:
        api_main.comms.settings.slack_real_send_allowed = previous_allowed
        api_main.comms.settings.slack_webhook_url = previous_webhook

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "created"
    assert payload["receipt_id"]
    assert payload["provider_ref"] == "slack-webhook:slack:#ops"

    receipts = client.get(
        "/v1/receipts", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert receipts.status_code == 200
    receipt = receipts.json()["items"][0]
    assert receipt["effect_kind"] == "slack.message.post"
    assert receipt["created_by_job_id"] is None

    with engine.begin() as connection:
        outbox_row = connection.execute(
            text(
                """
                SELECT status, receipt_id
                FROM outbox_messages
                WHERE id = :message_id
                """
            ),
            {"message_id": message_id},
        ).mappings().one()

    assert outbox_row["status"] == "acked"
    assert outbox_row["receipt_id"] == payload["receipt_id"]


def test_outbox_email_delivery_blocks_when_email_delivery_is_disabled(client, engine):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder@example.com",
            "subject": "Founder update",
            "body": "Email delivery should be blocked without SMTP config.",
            "dedupe_key": "outbox-email-blocked-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Attempt blocked email delivery.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous_allowed = api_main.comms.settings.email_real_send_allowed
    previous_host = api_main.comms.settings.email_smtp_host
    previous_from = api_main.comms.settings.email_from_address
    api_main.comms.settings.email_real_send_allowed = False
    api_main.comms.settings.email_smtp_host = None
    api_main.comms.settings.email_from_address = None
    try:
        response = client.post(
            f"/v1/comms/outbox/{message_id}/deliver",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "outbox-email-blocked-delivery-1",
            },
        )
    finally:
        api_main.comms.settings.email_real_send_allowed = previous_allowed
        api_main.comms.settings.email_smtp_host = previous_host
        api_main.comms.settings.email_from_address = previous_from

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert payload["receipt_id"]
    assert "BOSS_EMAIL_REAL_SEND_ALLOWED is false" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :reservation_id"),
            {"reservation_id": reservation_id},
        ).scalar_one()
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
        receipt = connection.execute(
            text(
                """
                SELECT effect_kind, status
                FROM receipts
                WHERE dedupe_key = 'outbox-email-blocked-delivery-1'
                """
            )
        ).mappings().one()

    assert reservation_status == "released"
    assert outbox_status == "blocked"
    assert receipt["effect_kind"] == "email.message.send"
    assert receipt["status"] == "failed"


def test_outbox_email_delivery_succeeds_and_creates_receipt(
    client, engine, monkeypatch
):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "email:founder@example.com",
            "subject": "Deployment",
            "body": "The sandbox change is ready for email review.",
            "dedupe_key": "outbox-email-success-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Send real email message.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous_allowed = api_main.comms.settings.email_real_send_allowed
    previous_host = api_main.comms.settings.email_smtp_host
    previous_from = api_main.comms.settings.email_from_address
    previous_tls = api_main.comms.settings.email_smtp_use_tls
    api_main.comms.settings.email_real_send_allowed = True
    api_main.comms.settings.email_smtp_host = "smtp.example.com"
    api_main.comms.settings.email_from_address = "boss@example.com"
    api_main.comms.settings.email_smtp_use_tls = True
    monkeypatch.setattr(
        api_main.comms.email,
        "send_message",
        lambda channel, subject, body: f"smtp:{channel.split(':', 1)[1]}",
    )
    try:
        response = client.post(
            f"/v1/comms/outbox/{message_id}/deliver",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "outbox-email-success-delivery-1",
            },
        )
    finally:
        api_main.comms.settings.email_real_send_allowed = previous_allowed
        api_main.comms.settings.email_smtp_host = previous_host
        api_main.comms.settings.email_from_address = previous_from
        api_main.comms.settings.email_smtp_use_tls = previous_tls

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "created"
    assert payload["receipt_id"]
    assert payload["provider_ref"] == "smtp:founder@example.com"

    receipts = client.get(
        "/v1/receipts", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert receipts.status_code == 200
    receipt = receipts.json()["items"][0]
    assert receipt["effect_kind"] == "email.message.send"
    assert receipt["created_by_job_id"] is None

    with engine.begin() as connection:
        outbox_row = connection.execute(
            text(
                """
                SELECT status, receipt_id
                FROM outbox_messages
                WHERE id = :message_id
                """
            ),
            {"message_id": message_id},
        ).mappings().one()

    assert outbox_row["status"] == "acked"
    assert outbox_row["receipt_id"] == payload["receipt_id"]


def test_outbox_delivery_rejects_unknown_created_by_job_id(client):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "This request should fail validation.",
            "dedupe_key": "outbox-invalid-job-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Validate created_by_job_id handling.",
        },
    )
    assert reserve.status_code == 201

    response = client.post(
        f"/v1/comms/outbox/{message_id}/deliver",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reserve.json()["reservation_id"],
            "dedupe_key": "outbox-invalid-job-delivery-1",
            "created_by_job_id": "job_missing",
        },
    )

    assert response.status_code == 400
    assert "created_by_job_id" in response.json()["detail"]


def test_comms_job_failure_deadletters_outbox_message(client, engine):
    outbox = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": "boss-dogfood",
            "channel": "slack:#ops",
            "body": "This message will deadletter.",
            "dedupe_key": "outbox-deadletter-1",
        },
    )
    assert outbox.status_code == 201
    message_id = outbox.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Queue a comms job that fails.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    queued = client.post(
        f"/v1/comms/outbox/{message_id}/queue",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "dedupe_key": "outbox-deadletter-job-1",
        },
    )
    assert queued.status_code == 201
    job_id = queued.json()["id"]

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-comms-deadletter",
            "capabilities": ["comms.outbox.deliver"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()["job"]
    assert leased["id"] == job_id

    failed = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-comms-deadletter",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "error": {"message": "Slack webhook rejected the payload."},
            "retryable": False,
            "retry_delay_seconds": 0,
        },
    )
    assert failed.status_code == 200
    payload = failed.json()
    assert payload["status"] == "deadlettered"
    assert payload["deadletter_id"]

    with engine.begin() as connection:
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
    assert outbox_status == "deadlettered"


def test_budget_denial_creates_decision(client):
    response = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "denied"
    assert payload["decision_id"]


def test_budget_status_returns_project_snapshot(client):
    response = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope_type"] == "project"
    assert payload["scope_id"] == "boss-dogfood"
    assert payload["budget_state"] == "green"
    assert payload["cap_usd_micros"] > 0
    assert payload["available_usd_micros"] == payload["cap_usd_micros"]
    assert payload["committed_usd_micros"] == 0
    assert payload["burn_window_hours"] == 24
    assert payload["burn_rate_usd_micros_per_hour"] == 0
    assert payload["forecasted_24h_usd_micros"] == 0
    assert payload["time_to_cap_seconds"] is None
    assert payload["active_reservations_count"] == 0
    assert payload["top_cost_drivers"] == []


def test_budget_status_reports_forecast_and_time_to_cap(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = 1_000_000
                WHERE scope_type = 'project' AND scope_id = 'boss-dogfood'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, category, skill_or_model_id,
                    estimated_usd_micros, actual_usd_micros, status, created_at
                )
                VALUES (
                    'cost_budget_forecast', 'boss-dogfood', 'model',
                    'gpt-5.2-codex', 240000, 240000, 'actual', :created_at
                )
                """
            ),
            {"created_at": datetime.now(timezone.utc)},
        )

    response = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["actual_usd_micros"] == 240_000
    assert payload["committed_usd_micros"] == 240_000
    assert payload["burn_window_hours"] == 24
    assert payload["burn_rate_usd_micros_per_hour"] == 10_000
    assert payload["forecasted_24h_usd_micros"] == 240_000
    assert payload["time_to_cap_seconds"] == 273_600
    assert payload["top_cost_drivers"] == [
        {"skill_or_model_id": "gpt-5.2-codex", "amount_usd_micros": 240_000}
    ]


def test_budget_quote_returns_allowed_projection(client):
    response = client.post(
        "/v1/budget/quote",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 250_000,
            "reason": "quote a model call",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is True
    assert payload["requested_usd_micros"] == 250_000
    assert payload["budget_state"] == "green"
    assert payload["post_reservation_available_usd_micros"] < payload["available_usd_micros"]


def test_budget_allocations_enforce_parent_limits_and_reservation_capacity(client):
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=1)

    root = client.post(
        "/v1/budget/allocations",
        json={
            "project_id": "boss-dogfood",
            "scope_type": "project",
            "scope_id": "boss-dogfood",
            "amount_usd_micros": 1_000_000,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "actor": "ceo",
        },
    )
    assert root.status_code == 201
    root_payload = root.json()
    assert root_payload["available_usd_micros"] == 1_000_000

    child = client.post(
        "/v1/budget/allocations",
        json={
            "project_id": "boss-dogfood",
            "parent_allocation_id": root_payload["id"],
            "scope_type": "department",
            "scope_id": "software_factory",
            "amount_usd_micros": 700_000,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "actor": "ceo",
        },
    )
    assert child.status_code == 201
    child_payload = child.json()

    over_child = client.post(
        "/v1/budget/allocations",
        json={
            "project_id": "boss-dogfood",
            "parent_allocation_id": root_payload["id"],
            "scope_type": "department",
            "scope_id": "growth",
            "amount_usd_micros": 400_000,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "actor": "ceo",
        },
    )
    assert over_child.status_code == 409

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "allocation_id": child_payload["id"],
            "category": "tool",
            "amount_usd_micros": 650_000,
            "reason": "reserve against software factory allocation",
        },
    )
    assert reserve.status_code == 201
    assert reserve.json()["status"] == "reserved"

    over_reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "allocation_id": child_payload["id"],
            "category": "tool",
            "amount_usd_micros": 100_000,
            "reason": "force allocation denial",
        },
    )
    assert over_reserve.status_code == 201
    assert over_reserve.json()["status"] == "denied"
    assert over_reserve.json()["decision_id"]

    allocations = client.get(
        "/v1/budget/allocations", params={"project_id": "boss-dogfood"}
    )
    assert allocations.status_code == 200
    items = {item["id"]: item for item in allocations.json()["items"]}
    assert items[root_payload["id"]]["child_allocated_usd_micros"] == 700_000
    assert items[child_payload["id"]]["reserved_usd_micros"] == 650_000
    assert items[child_payload["id"]]["available_usd_micros"] == 50_000

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "budget.allocation_denied" in event_types
    assert "budget.allocation_reservation_denied" in event_types


def test_budget_release_frees_active_reservation(client):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 250_000,
            "reason": "reserve then release",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    release = client.post(
        "/v1/budget/release",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "reason": "No external effect was executed.",
        },
    )
    assert release.status_code == 200
    payload = release.json()
    assert payload["status"] == "released"
    assert payload["reservation_id"] == reservation_id

    status = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )
    assert status.status_code == 200
    status_payload = status.json()
    assert status_payload["reserved_usd_micros"] == 0
    assert status_payload["active_reservations_count"] == 0


def test_budget_denial_is_visible_in_decision_inbox_and_event_feed(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    inbox = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert inbox.status_code == 200
    inbox_payload = inbox.json()
    assert inbox_payload["items"]
    assert inbox_payload["items"][0]["id"] == decision_id
    assert inbox_payload["items"][0]["status"] == "pending"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.created" in event_types
    assert "budget.reservation_denied" in event_types


def test_decision_can_be_approved_and_emits_audit_event(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo", "note": "Budget increase is acceptable."},
    )
    assert approve.status_code == 200
    payload = approve.json()
    assert payload["id"] == decision_id
    assert payload["status"] == "approved"
    assert payload["approved_by"] == "ceo"
    assert payload["approved_at"]

    status = client.get("/v1/status", params={"project_id": "boss-dogfood"})
    assert status.status_code == 200
    assert status.json()["pending_decisions"] == 0

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.approved" in event_types


def test_decision_can_be_rejected_and_cannot_be_reapproved(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    reject = client.post(
        f"/v1/decisions/{decision_id}/reject",
        json={"actor": "ceo", "note": "Keep the guardrail in place."},
    )
    assert reject.status_code == 200
    payload = reject.json()
    assert payload["status"] == "rejected"
    assert payload["approved_by"] is None
    assert payload["approved_at"] is None

    duplicate_approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo"},
    )
    assert duplicate_approve.status_code == 409

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.rejected" in event_types


def test_tier_two_decision_approval_requires_configured_approver_role(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={
            "actor": "release_manager",
            "note": "Trying to approve without delegation.",
        },
    )

    assert approve.status_code == 403
    assert "not allowed to approve" in approve.json()["detail"]
    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    policy_events = [
        item for item in events.json()["items"] if item["event_type"] == "policy.denied"
    ]
    assert policy_events
    assert policy_events[0]["entity_id"] == decision_id


def test_decision_can_be_delegated_and_then_approved(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    delegate = client.post(
        f"/v1/decisions/{decision_id}/delegate",
        json={
            "actor": "ceo",
            "delegate_to": "release_manager",
            "note": "Release manager should own this final call.",
        },
    )
    assert delegate.status_code == 200
    delegated_payload = delegate.json()
    assert delegated_payload["status"] == "delegated"
    assert delegated_payload["delegated_to"] == "release_manager"
    assert delegated_payload["delegated_by"] == "ceo"
    assert delegated_payload["delegated_at"]
    assert delegated_payload["resolution_note"] == "Release manager should own this final call."

    approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "release_manager", "note": "Approved after delegated review."},
    )
    assert approve.status_code == 200
    approved_payload = approve.json()
    assert approved_payload["status"] == "approved"
    assert approved_payload["approved_by"] == "release_manager"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.delegated" in event_types
    assert "decision.approved" in event_types


def test_decision_can_request_revision_and_then_blocks_approval(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    revise = client.post(
        f"/v1/decisions/{decision_id}/revise",
        json={
            "actor": "ceo",
            "note": "Revise the estimate and include lower-cost options.",
        },
    )
    assert revise.status_code == 200
    payload = revise.json()
    assert payload["status"] == "revision_requested"
    assert payload["revision_requested_by"] == "ceo"
    assert payload["revision_requested_at"]
    assert payload["resolution_note"] == "Revise the estimate and include lower-cost options."

    approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo"},
    )
    assert approve.status_code == 409

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.revision_requested" in event_types


def test_self_improvement_proposal_runs_through_canary_rollout_and_rollback(
    client, engine
):
    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Tighten decision inbox defaults",
            "summary": "Improve default-action handling and observability for expired decisions.",
            "change_ref": "https://github.com/seanheiney/boss/pull/42",
            "diff_summary": "Adjust decision expiry handling and add stronger audit markers.",
            "expected_impact": ["Lower decision latency", "Fewer unsafe expirations"],
            "test_plan": ["Add API transition tests", "Run smoke suite"],
            "canary_scope": ["dogfood workspace only", "decision inbox traffic"],
            "rollback_plan": ["Revert PR", "Disable rollout flag"],
            "success_metrics": ["No expired high-risk decision executes", "No silent drops"],
            "actor": "vp_agents",
            "risk_tier": 2,
        },
    )
    assert created.status_code == 201
    created_payload = created.json()
    proposal_id = created_payload["id"]
    task_id = created_payload["task_id"]
    canary_decision_id = created_payload["current_decision_id"]
    assert created_payload["status"] == "proposed"
    assert created_payload["proposal_artifact_id"]

    approve_canary = client.post(
        f"/v1/decisions/{canary_decision_id}/approve",
        json={"actor": "ceo", "note": "Run the dogfood canary."},
    )
    assert approve_canary.status_code == 200

    proposal_after_canary_approval = client.get(
        f"/v1/self-improvement/proposals/{proposal_id}"
    )
    assert proposal_after_canary_approval.status_code == 200
    assert proposal_after_canary_approval.json()["status"] == "canary_approved"

    canary = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/canary",
        json={
            "actor": "release_manager",
            "outcome": "succeeded",
            "summary": "Dogfood canary stayed within expected error and cost bounds.",
            "metrics": {"decision_latency_delta_pct": -12},
            "evidence_refs": ["evt_canary_1"],
        },
    )
    assert canary.status_code == 200
    canary_payload = canary.json()
    rollout_decision_id = canary_payload["current_decision_id"]
    assert canary_payload["status"] == "canary_succeeded"
    assert canary_payload["canary_artifact_id"]
    assert rollout_decision_id

    approve_rollout = client.post(
        f"/v1/decisions/{rollout_decision_id}/approve",
        json={"actor": "ceo", "note": "Promote to full dogfood rollout."},
    )
    assert approve_rollout.status_code == 200

    rollout_ready = client.get(f"/v1/self-improvement/proposals/{proposal_id}")
    assert rollout_ready.status_code == 200
    assert rollout_ready.json()["status"] == "rollout_approved"

    rollout = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollout",
        json={
            "actor": "release_manager",
            "summary": "Rolled out to the dogfood control plane.",
            "external_ref": "deploy://dogfood/2026-06-01/self-improvement-42",
            "metrics": {"deadletter_rate": 0},
            "evidence_refs": ["art_rollout_1"],
        },
    )
    assert rollout.status_code == 200
    rollout_payload = rollout.json()
    assert rollout_payload["status"] == "rolled_out"
    assert rollout_payload["rollout_artifact_id"]

    rollback = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollback",
        json={
            "actor": "release_manager",
            "reason": "Rollback after regression review.",
            "external_ref": "rollback://dogfood/2026-06-01/self-improvement-42",
            "metrics": {"policy_violation_count": 1},
            "evidence_refs": ["art_rollback_1"],
        },
    )
    assert rollback.status_code == 200
    rollback_payload = rollback.json()
    assert rollback_payload["status"] == "rolled_back"
    assert rollback_payload["rollback_artifact_id"]

    with engine.begin() as connection:
        task_status = connection.execute(
            text("SELECT status FROM tasks WHERE id = :task_id"),
            {"task_id": task_id},
        ).scalar_one()
        artifact_types = [
            row[0]
            for row in connection.execute(
                text(
                    """
                    SELECT artifact_type
                    FROM artifacts
                    WHERE task_id = :task_id
                    ORDER BY created_at ASC
                    """
                ),
                {"task_id": task_id},
            ).all()
        ]

    assert task_status == "rolled_back"
    assert "self_improvement_proposal" in artifact_types
    assert "self_improvement_canary_report" in artifact_types
    assert "self_improvement_rollout_record" in artifact_types
    assert "self_improvement_rollback_record" in artifact_types


def test_self_improvement_proposal_requires_non_empty_change_control_evidence(client):
    response = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "   ",
            "summary": "Harden proposal input validation.",
            "change_ref": "https://github.com/seanheiney/boss/pull/99",
            "diff_summary": "Require explicit test, canary, rollback, and metric evidence.",
            "expected_impact": [],
            "test_plan": [" "],
            "canary_scope": [],
            "rollback_plan": [],
            "success_metrics": [],
        },
    )

    assert response.status_code == 422
    payload = response.json()
    assert "must" in json.dumps(payload)
    assert any(
        term in json.dumps(payload)
        for term in [
            "title must not be blank",
            "expected_impact must contain at least one non-empty item",
            "test_plan must contain at least one non-empty item",
            "canary_scope must contain at least one non-empty item",
            "rollback_plan must contain at least one non-empty item",
            "success_metrics must contain at least one non-empty item",
        ]
    )


def test_self_improvement_canary_requires_metrics_and_evidence(client):
    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Tighten decision inbox defaults",
            "summary": "Improve default-action handling and observability for expired decisions.",
            "change_ref": "https://github.com/seanheiney/boss/pull/142",
            "diff_summary": "Adjust decision expiry handling and add stronger audit markers.",
            "expected_impact": ["Lower decision latency"],
            "test_plan": ["Add API transition tests"],
            "canary_scope": ["dogfood workspace only"],
            "rollback_plan": ["Revert PR"],
            "success_metrics": ["No expired high-risk decision executes"],
        },
    )
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    decision_id = created.json()["current_decision_id"]

    approve_canary = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo", "note": "Run the dogfood canary."},
    )
    assert approve_canary.status_code == 200

    canary = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/canary",
        json={
            "actor": "release_manager",
            "outcome": "succeeded",
            "summary": "Canary looked healthy.",
            "metrics": {},
            "evidence_refs": [],
        },
    )
    assert canary.status_code == 422
    assert "metrics" in json.dumps(canary.json())
    assert "evidence_refs" in json.dumps(canary.json())


def test_self_improvement_rollout_and_rollback_require_metrics_and_evidence(client):
    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Refine budget governor events",
            "summary": "Improve budget denial event detail and audit clarity.",
            "change_ref": "https://github.com/seanheiney/boss/pull/177",
            "diff_summary": "Expand denial event payloads and review packet notes.",
            "expected_impact": ["Better operator clarity"],
            "test_plan": ["Decision API coverage"],
            "canary_scope": ["dogfood only"],
            "rollback_plan": ["Revert PR"],
            "success_metrics": ["Clearer denial event feed"],
        },
    )
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    canary_decision_id = created.json()["current_decision_id"]

    assert (
        client.post(
            f"/v1/decisions/{canary_decision_id}/approve",
            json={"actor": "ceo", "note": "Approve canary."},
        ).status_code
        == 200
    )
    successful_canary = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/canary",
        json={
            "actor": "release_manager",
            "outcome": "succeeded",
            "summary": "Canary stayed within expected bounds.",
            "metrics": {"decision_latency_delta_pct": -4},
            "evidence_refs": ["evt_canary_budget_1"],
        },
    )
    assert successful_canary.status_code == 200
    rollout_decision_id = successful_canary.json()["current_decision_id"]

    assert (
        client.post(
            f"/v1/decisions/{rollout_decision_id}/approve",
            json={"actor": "ceo", "note": "Approve rollout."},
        ).status_code
        == 200
    )

    rollout = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollout",
        json={
            "actor": "release_manager",
            "summary": "Rolled out broadly.",
            "metrics": {},
            "evidence_refs": [],
        },
    )
    assert rollout.status_code == 422
    assert "metrics" in json.dumps(rollout.json())
    assert "evidence_refs" in json.dumps(rollout.json())

    valid_rollout = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollout",
        json={
            "actor": "release_manager",
            "summary": "Rolled out broadly.",
            "external_ref": "deploy://dogfood/2026-06-02/self-improvement-177",
            "metrics": {"deadletter_rate": 0},
            "evidence_refs": ["art_rollout_budget_1"],
        },
    )
    assert valid_rollout.status_code == 200

    rollback = client.post(
        f"/v1/self-improvement/proposals/{proposal_id}/rollback",
        json={
            "actor": "release_manager",
            "reason": "Rollback after operator review.",
            "metrics": {},
            "evidence_refs": [],
        },
    )
    assert rollback.status_code == 422
    assert "metrics" in json.dumps(rollback.json())
    assert "evidence_refs" in json.dumps(rollback.json())


def test_self_improvement_revision_request_updates_proposal_status(client):
    created = client.post(
        "/v1/self-improvement/proposals",
        json={
            "project_id": "boss-dogfood",
            "title": "Refine budget governor events",
            "summary": "Improve event detail for budget denials.",
            "change_ref": "https://github.com/seanheiney/boss/pull/77",
            "diff_summary": "Expand denial event payloads.",
            "expected_impact": ["Better operator clarity"],
            "test_plan": ["Decision API coverage"],
            "canary_scope": ["dogfood only"],
            "rollback_plan": ["Revert PR"],
            "success_metrics": ["Clearer denial event feed"],
        },
    )
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    decision_id = created.json()["current_decision_id"]

    revise = client.post(
        f"/v1/decisions/{decision_id}/revise",
        json={
            "actor": "ceo",
            "note": "Add a stronger rollback story before canarying this.",
        },
    )
    assert revise.status_code == 200

    proposal = client.get(f"/v1/self-improvement/proposals/{proposal_id}")
    assert proposal.status_code == 200
    payload = proposal.json()
    assert payload["status"] == "revision_requested"
    assert payload["status_note"] == "Add a stronger rollback story before canarying this."
    assert payload["current_decision_id"] is None


def test_approved_decision_can_be_manually_executed_with_evidence(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, priority, risk_tier, dedupe_key
                )
                VALUES (
                    'task_manual_execute', 'boss-dogfood', 'Manual merge follow-on',
                    'Track a manually executed follow-on action.', 'open', 'p1', 2,
                    'manual-execute-task'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, deadline_at, default_action, status, approved_by, approved_at
                )
                VALUES (
                    'dec_manual_execute', 'boss-dogfood', 'task_manual_execute',
                    'Record the manual sandbox merge.', '{}'::jsonb, '[]'::jsonb, 2,
                    '{}'::jsonb, now() + interval '1 day', 'hold', 'approved',
                    'ceo', now()
                )
                """
            )
        )

    execute = client.post(
        "/v1/decisions/dec_manual_execute/execute",
        json={
            "actor": "release_manager",
            "note": "Merged manually after review in GitHub.",
            "external_ref": "https://github.com/seanheiney/boss-sandbox/pull/42",
        },
    )
    assert execute.status_code == 200
    payload = execute.json()
    assert payload["status"] == "executed"
    assert payload["executed_by"] == "release_manager"
    assert payload["executed_at"]
    assert payload["external_ref"] == "https://github.com/seanheiney/boss-sandbox/pull/42"
    assert payload["resolution_note"] == "Merged manually after review in GitHub."

    with engine.begin() as connection:
        task_status = connection.execute(
            text("SELECT status FROM tasks WHERE id = 'task_manual_execute'")
        ).scalar_one()
        artifact_type = connection.execute(
            text(
                """
                SELECT artifact_type
                FROM artifacts
                WHERE task_id = 'task_manual_execute'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).scalar_one()

    assert task_status == "completed"
    assert artifact_type == "manual_execution_record"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.executed" in event_types
    assert "task.completed" in event_types


def test_overdue_pending_decisions_expire_to_safe_default(client, engine):
    deadline_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, deadline_at, default_action, status
                )
                VALUES (
                    'dec_expired_for_inbox', 'boss-dogfood', NULL, 'Expired decision',
                    '{}'::jsonb, '[]'::jsonb, 2, '{}'::jsonb, :deadline_at, 'hold', 'pending'
                )
                """
            ),
            {"deadline_at": deadline_at},
        )

    inbox = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert inbox.status_code == 200
    payload = inbox.json()
    assert payload["items"][0]["id"] == "dec_expired_for_inbox"
    assert payload["items"][0]["status"] == "expired"
    assert payload["items"][0]["default_action"] == "hold"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "decision.expired" in [item["event_type"] for item in events.json()["items"]]


def test_expired_reservations_are_reclassified_before_budget_status(client, engine):
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO budget_reservations (
                    id, allocation_id, project_id, job_id, category, amount_usd_micros,
                    status, reason, expires_at
                )
                VALUES (
                    'res_expired_budget_status', NULL, 'boss-dogfood', NULL, 'model', 12345,
                    'active', 'expired for test', :expires_at
                )
                """
            ),
            {"expires_at": expired_at},
        )

    status_response = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )
    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["reserved_usd_micros"] == 0

    with engine.begin() as connection:
        status = connection.execute(
            text(
                """
                SELECT status FROM budget_reservations
                WHERE id = 'res_expired_budget_status'
                """
            )
        ).scalar_one()
    assert status == "expired"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "budget.reservation_expired" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_openai_plan_blocks_without_reservation(client):
    response = client.post(
        "/v1/openai/plans",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": "missing",
            "dedupe_key": "test-plan-no-res",
            "prompt": "Create a plan.",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]


def test_openai_plan_blocks_without_credentials_and_releases_reservation(client, engine):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 250_000,
            "reason": "OpenAI plan without credentials.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous = api_main.software_factory.settings.openai_api_key
    api_main.software_factory.settings.openai_api_key = None
    try:
        response = client.post(
            "/v1/openai/plans",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "missing-openai-creds",
                "prompt": "Create a plan.",
            },
        )
    finally:
        api_main.software_factory.settings.openai_api_key = previous

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert "OPENAI_API_KEY" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text(
                """
                SELECT status
                FROM budget_reservations
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        ).scalar_one()
        decision_question = connection.execute(
            text(
                """
                SELECT question
                FROM decisions
                WHERE id = :decision_id
                """
            ),
            {"decision_id": payload["decision_id"]},
        ).scalar_one()

    assert reservation_status == "released"
    assert decision_question == "OpenAI integration is not configured"


def test_two_workers_cannot_claim_same_job(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    worker_a = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert worker_a.status_code == 200
    leased = worker_a.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id

    worker_b = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-b",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert worker_b.status_code == 200
    assert worker_b.json()["status"] == "idle"
    assert worker_b.json()["job"] is None


def test_get_job_returns_created_job(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    fetched = client.get(f"/v1/jobs/{job_id}")
    assert fetched.status_code == 200
    payload = fetched.json()
    assert payload["id"] == job_id
    assert payload["kind"] == "plan.generate"
    assert payload["status"] == "queued"


def test_expired_lease_can_be_reclaimed(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    first = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert first.status_code == 200
    assert first.json()["job"]["attempt"] == 1

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE jobs
                SET lease_expires_at = :lease_expires_at
                WHERE id = :job_id
                """
            ),
            {
                "job_id": job_id,
                "lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=5),
            },
        )

    second = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-b",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert second.status_code == 200
    payload = second.json()
    assert payload["status"] == "leased"
    assert payload["job"]["id"] == job_id
    assert payload["job"]["attempt"] == 2

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "job.lease_reclaimed" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_failed_job_retries_then_deadletters(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
            "max_attempts": 2,
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    first_lease = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    heartbeat = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": first_lease["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    assert heartbeat.status_code == 200

    first_fail = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": first_lease["lease_token"],
            "error": {"message": "transient failure"},
            "retryable": True,
            "retry_delay_seconds": 0,
        },
    )
    assert first_fail.status_code == 200
    assert first_fail.json()["status"] == "failed_retryable"

    second_lease = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-b",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    second_heartbeat = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-b",
            "job_id": job_id,
            "lease_token": second_lease["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    assert second_heartbeat.status_code == 200

    second_fail = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-b",
            "job_id": job_id,
            "lease_token": second_lease["lease_token"],
            "error": {"message": "still failing"},
            "retryable": True,
            "retry_delay_seconds": 0,
        },
    )
    assert second_fail.status_code == 200
    payload = second_fail.json()
    assert payload["status"] == "deadlettered"
    assert payload["deadletter_id"]

    with engine.begin() as connection:
        status = connection.execute(
            text("SELECT status FROM jobs WHERE id = :job_id"),
            {"job_id": job_id},
        ).scalar_one()
        deadletters = connection.execute(
            text(
                "SELECT COUNT(*) FROM deadletters WHERE source_kind = 'job' AND source_id = :job_id"
            ),
            {"job_id": job_id},
        ).scalar_one()
    assert status == "deadlettered"
    assert deadletters == 1


def test_deadletters_are_visible_via_api(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
            "max_attempts": 1,
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-deadletter",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-deadletter",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    fail = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-deadletter",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "error": {"message": "terminal failure"},
            "retryable": False,
            "retry_delay_seconds": 0,
        },
    )
    assert fail.status_code == 200
    deadletter_id = fail.json()["deadletter_id"]

    deadletters = client.get(
        "/v1/deadletters",
        params={"project_id": "boss-dogfood", "limit": 10},
    )
    assert deadletters.status_code == 200
    payload = deadletters.json()
    assert payload["project_id"] == "boss-dogfood"
    item = next(entry for entry in payload["items"] if entry["id"] == deadletter_id)
    assert item["source_kind"] == "job"
    assert item["source_id"] == job_id
    assert item["reason"] == "terminal failure"


def test_worker_complete_creates_plan_artifact_and_decision(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "result": {
                "summary": "Create a draft plan.",
                "objectives": ["Create a draft plan."],
                "steps": ["Do the work."],
                "risks": ["None"],
                "done_when": ["Artifact exists."],
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["artifact_id"]
    assert payload["decision_id"]


def test_approving_plan_decision_queues_follow_on_pr_job(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Prepare the next sandbox proposal."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "result": {
                "summary": "Prepare the next sandbox proposal.",
                "objectives": ["Prepare the next sandbox proposal."],
                "steps": ["Draft the proposal markdown."],
                "risks": ["Budget could block execution."],
                "done_when": ["A sandbox PR proposal job is queued."],
            },
        },
    )
    assert complete.status_code == 200
    decision_id = complete.json()["decision_id"]

    approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo", "note": "Proceed to sandbox proposal."},
    )
    assert approve.status_code == 200

    with engine.begin() as connection:
        reservation = connection.execute(
            text(
                """
                SELECT id, status, category, amount_usd_micros
                FROM budget_reservations
                WHERE project_id = 'boss-dogfood'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()
        next_job = connection.execute(
            text(
                """
                SELECT kind, status, payload
                FROM jobs
                WHERE project_id = 'boss-dogfood'
                  AND kind = 'software_factory.propose_pr'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()

    assert reservation["status"] == "active"
    assert reservation["category"] == "tool"
    assert int(reservation["amount_usd_micros"]) == 500_000
    assert next_job["status"] == "queued"
    payload = next_job["payload"]
    assert payload["reservation_id"] == reservation["id"]
    assert payload["title"].startswith("BOSS proposal:")
    assert payload["branch_name"].startswith("boss/proposal-")
    assert payload["agent_module_id"] == "codex_software_engineer_v1"
    assert payload["files"][0]["path"].startswith("proposals/")

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.follow_on_job_queued" in event_types


def test_approving_pr_review_decision_creates_merge_gate_block_artifact(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, project_id, task_id, artifact_type, title, uri, content, content_hash, created_by_job_id
                )
                VALUES (
                    'art_review_packet', 'boss-dogfood', NULL, 'review_packet', 'Review packet',
                    NULL, '{}'::jsonb, 'hash', NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, project_id, task_id, artifact_type, title, uri, content, content_hash, created_by_job_id
                )
                VALUES (
                    'art_test_report_merge_gate', 'boss-dogfood', NULL, 'test_report', 'Test report',
                    NULL, CAST(:content AS JSONB), 'hash-test', NULL
                )
                """
            ),
            {
                "content": json.dumps(
                    {
                        "status": "passed",
                        "summary": "GitHub validation passed: CI.",
                        "tests_added_or_run": ["CI"],
                    }
                )
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, default_action, status
                )
                VALUES (
                    'dec_review_pr', 'boss-dogfood', NULL, 'Review and approve sandbox PR: Demo',
                    CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'pending'
                )
                """
            ),
            {
                "recommendation": json.dumps(
                    {
                        "action": "review_pull_request",
                        "artifact_id": "art_review_packet",
                        "test_report_artifact_id": "art_test_report_merge_gate",
                        "receipt_id": "rcpt_pr_demo",
                        "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                        "branch_name": "boss/demo-pr",
                        "title": "Demo",
                    }
                )
            },
        )

    approve = client.post(
        "/v1/decisions/dec_review_pr/approve",
        json={"actor": "ceo", "note": "Manual merge is acceptable."},
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"

    with engine.begin() as connection:
        merge_gate = connection.execute(
            text(
                """
                SELECT artifact_type, title, content
                FROM artifacts
                WHERE project_id = 'boss-dogfood' AND artifact_type = 'merge_gate'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()
        queued_jobs = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM jobs
                WHERE project_id = 'boss-dogfood' AND kind = 'github.pull_request.merge'
                """
            )
        ).scalar_one()

    assert merge_gate["artifact_type"] == "merge_gate"
    assert "Merge gate for Demo" == merge_gate["title"]
    assert merge_gate["content"]["status"] == "blocked_manual_merge_required"
    assert (
        merge_gate["content"]["pull_request_url"]
        == "https://github.com/seanheiney/boss-sandbox/pull/42"
    )
    assert merge_gate["content"]["receipt_id"] == "rcpt_pr_demo"
    assert queued_jobs == 0

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.follow_on_blocked" in event_types


def test_pr_proposal_runs_receipt_backed_local_sandbox_validation(
    client, engine, monkeypatch, tmp_path
):
    branch_name = "boss/local-validation-pass"
    repo = create_local_validation_repo(tmp_path, branch_name)
    settings = api_main.software_factory.settings
    previous = {
        "openai_api_key": settings.openai_api_key,
        "sandbox_local_validation_enabled": settings.sandbox_local_validation_enabled,
        "sandbox_validation_repo_path": settings.sandbox_validation_repo_path,
        "sandbox_validation_commands": settings.sandbox_validation_commands,
        "sandbox_validation_fetch_remote": settings.sandbox_validation_fetch_remote,
    }
    configure_pr_proposal_integrations(monkeypatch)
    settings.sandbox_local_validation_enabled = True
    settings.sandbox_validation_repo_path = repo
    settings.sandbox_validation_commands = "python3 -c \"print('local validation ok')\""
    settings.sandbox_validation_fetch_remote = False

    try:
        reservation_id = reserve_for_pr_proposal(client)
        proposal = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "local-validation-pass",
                "title": "Local validation pass",
                "branch_name": branch_name,
                "summary": "Validate the sandbox branch locally before merge.",
                "files": [
                    {
                        "path": "proposals/local-validation-pass.md",
                        "content": "# Local validation pass\n",
                    }
                ],
            },
        )
        assert proposal.status_code == 200
        payload = proposal.json()
        assert payload["status"] == "created"
        assert payload["validation_status"] == "passed"
        assert payload["validation_sources"] == [
            "github.check_runs",
            "local.sandbox_commands",
        ]
        assert payload["local_validation_receipt_id"]

        report = client.get(f"/v1/artifacts/{payload['test_report_artifact_id']}")
        assert report.status_code == 200
        content = report.json()["content"]
        assert content["status"] == "passed"
        assert content["local_validation"]["status"] == "passed"
        assert content["local_validation_receipt_status"] == "succeeded"
        assert "python3 -c \"print('local validation ok')\"" in content[
            "tests_added_or_run"
        ]

        with engine.begin() as connection:
            receipts = connection.execute(
                text(
                    """
                    SELECT effect_kind, status
                    FROM receipts
                    WHERE project_id = 'boss-dogfood'
                    ORDER BY created_at ASC
                    """
                )
            ).mappings().all()
        receipt_status_by_kind = {row["effect_kind"]: row["status"] for row in receipts}
        assert receipt_status_by_kind == {
            "github.pull_request.create": "succeeded",
            "sandbox.validation.run": "succeeded",
        }

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "software_factory.local_validation_passed" in event_types
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)


def test_pr_proposal_local_sandbox_validation_failure_blocks_merge_gate(
    client, engine, monkeypatch, tmp_path
):
    branch_name = "boss/local-validation-fail"
    repo = create_local_validation_repo(tmp_path, branch_name)
    settings = api_main.software_factory.settings
    previous_factory = {
        "openai_api_key": settings.openai_api_key,
        "sandbox_local_validation_enabled": settings.sandbox_local_validation_enabled,
        "sandbox_validation_repo_path": settings.sandbox_validation_repo_path,
        "sandbox_validation_commands": settings.sandbox_validation_commands,
        "sandbox_validation_fetch_remote": settings.sandbox_validation_fetch_remote,
    }
    previous_merge = api_main.decisions.settings.enable_sandbox_merge
    configure_pr_proposal_integrations(monkeypatch)
    settings.sandbox_local_validation_enabled = True
    settings.sandbox_validation_repo_path = repo
    settings.sandbox_validation_commands = "python3 -c \"raise SystemExit(3)\""
    settings.sandbox_validation_fetch_remote = False
    api_main.decisions.settings.enable_sandbox_merge = True

    try:
        reservation_id = reserve_for_pr_proposal(client)
        proposal = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "local-validation-fail",
                "title": "Local validation fail",
                "branch_name": branch_name,
                "summary": "Block merge when local sandbox validation fails.",
                "files": [
                    {
                        "path": "proposals/local-validation-fail.md",
                        "content": "# Local validation fail\n",
                    }
                ],
            },
        )
        assert proposal.status_code == 200
        payload = proposal.json()
        assert payload["status"] == "created"
        assert payload["validation_status"] == "failed"
        assert "exited 3" in payload["validation_summary"]

        report = client.get(f"/v1/artifacts/{payload['test_report_artifact_id']}")
        assert report.status_code == 200
        content = report.json()["content"]
        assert content["status"] == "failed"
        assert content["local_validation"]["status"] == "failed"
        assert content["local_validation_receipt_status"] == "failed"

        approve = client.post(
            f"/v1/decisions/{payload['decision_id']}/approve",
            json={"actor": "ceo", "note": "Try to merge despite local failure."},
        )
        assert approve.status_code == 200
        assert approve.json()["status"] == "approved"

        with engine.begin() as connection:
            merge_jobs = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM jobs
                    WHERE project_id = 'boss-dogfood'
                      AND kind = 'github.pull_request.merge'
                      AND payload->>'decision_id' = :decision_id
                    """
                ),
                {"decision_id": payload["decision_id"]},
            ).scalar_one()
            merge_gate = connection.execute(
                text(
                    """
                    SELECT content
                    FROM artifacts
                    WHERE project_id = 'boss-dogfood' AND artifact_type = 'merge_gate'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
            ).mappings().one()
            validation_receipt_status = connection.execute(
                text(
                    """
                    SELECT status
                    FROM receipts
                    WHERE id = :receipt_id
                    """
                ),
                {"receipt_id": payload["local_validation_receipt_id"]},
            ).scalar_one()

        assert merge_jobs == 0
        assert merge_gate["content"]["status"] == "blocked_ci_validation_failed"
        assert merge_gate["content"]["ci_validation_status"] == "failed"
        assert validation_receipt_status == "failed"

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "software_factory.local_validation_failed" in event_types
        assert "decision.follow_on_blocked" in event_types
    finally:
        for key, value in previous_factory.items():
            setattr(settings, key, value)
        api_main.decisions.settings.enable_sandbox_merge = previous_merge


def test_approving_pr_review_decision_queues_merge_job_when_policy_enabled(client, engine):
    previous = api_main.decisions.settings.enable_sandbox_merge
    api_main.decisions.settings.enable_sandbox_merge = True
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO artifacts (
                        id, project_id, task_id, artifact_type, title, uri, content, content_hash, created_by_job_id
                    )
                    VALUES (
                        'art_test_report_merge_allowed', 'boss-dogfood', NULL, 'test_report', 'Test report',
                        NULL, CAST(:content AS JSONB), 'hash-test', NULL
                    )
                    """
                ),
                {
                    "content": json.dumps(
                        {
                            "status": "passed",
                            "summary": "GitHub validation passed: CI.",
                            "tests_added_or_run": ["CI"],
                        }
                    )
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO decisions (
                        id, project_id, task_id, question, recommendation, options, risk_tier,
                        cost_summary, default_action, status
                    )
                    VALUES (
                        'dec_review_pr_merge', 'boss-dogfood', NULL, 'Review and approve sandbox PR: Merge',
                        CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'pending'
                    )
                    """
                ),
                {
                    "recommendation": json.dumps(
                        {
                            "action": "review_pull_request",
                            "test_report_artifact_id": "art_test_report_merge_allowed",
                            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                            "branch_name": "boss/demo-pr",
                            "title": "Merge",
                        }
                    )
                },
            )

        approve = client.post(
            "/v1/decisions/dec_review_pr_merge/approve",
            json={"actor": "ceo", "note": "Queue the merge."},
        )
        assert approve.status_code == 200
        assert approve.json()["status"] == "approved"

        with engine.begin() as connection:
            merge_jobs = connection.execute(
                text(
                    """
                    SELECT kind, status, payload
                    FROM jobs
                    WHERE project_id = 'boss-dogfood' AND kind = 'github.pull_request.merge'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
            ).mappings().all()
            merge_gates = connection.execute(
                text(
                    """
                    SELECT COUNT(*) FROM artifacts
                    WHERE project_id = 'boss-dogfood' AND artifact_type = 'merge_gate'
                    """
                )
            ).scalar_one()

        assert len(merge_jobs) == 1
        merge_payload = merge_jobs[0]["payload"]
        assert merge_jobs[0]["status"] == "queued"
        assert merge_payload["decision_id"] == "dec_review_pr_merge"
        assert merge_payload["pull_request_number"] == 42
        assert merge_payload["pull_request_url"].endswith("/pull/42")
        assert merge_gates == 0

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "decision.follow_on_job_queued" in event_types
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous


def test_approving_pr_review_decision_blocks_merge_when_ci_failed(client, engine):
    previous = api_main.decisions.settings.enable_sandbox_merge
    api_main.decisions.settings.enable_sandbox_merge = True
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO artifacts (
                        id, project_id, task_id, artifact_type, title, uri, content, content_hash, created_by_job_id
                    )
                    VALUES (
                        'art_test_report_merge_failed', 'boss-dogfood', NULL, 'test_report', 'Test report',
                        NULL, CAST(:content AS JSONB), 'hash-test', NULL
                    )
                    """
                ),
                {
                    "content": json.dumps(
                        {
                            "status": "failed",
                            "summary": "GitHub validation failed: CI.",
                            "tests_added_or_run": ["CI"],
                        }
                    )
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO decisions (
                        id, project_id, task_id, question, recommendation, options, risk_tier,
                        cost_summary, default_action, status
                    )
                    VALUES (
                        'dec_review_pr_merge_failed', 'boss-dogfood', NULL, 'Review and approve sandbox PR: Broken',
                        CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'pending'
                    )
                    """
                ),
                {
                    "recommendation": json.dumps(
                        {
                            "action": "review_pull_request",
                            "test_report_artifact_id": "art_test_report_merge_failed",
                            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/77",
                            "branch_name": "boss/broken-pr",
                            "title": "Broken",
                        }
                    )
                },
            )

        approve = client.post(
            "/v1/decisions/dec_review_pr_merge_failed/approve",
            json={"actor": "ceo", "note": "Try to merge anyway."},
        )
        assert approve.status_code == 200
        assert approve.json()["status"] == "approved"

        with engine.begin() as connection:
            merge_jobs = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM jobs
                    WHERE project_id = 'boss-dogfood'
                      AND kind = 'github.pull_request.merge'
                      AND payload->>'decision_id' = 'dec_review_pr_merge_failed'
                    """
                )
            ).scalar_one()
            merge_gate = connection.execute(
                text(
                    """
                    SELECT content
                    FROM artifacts
                    WHERE project_id = 'boss-dogfood' AND artifact_type = 'merge_gate'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
            ).mappings().one()

        assert merge_jobs == 0
        assert merge_gate["content"]["status"] == "blocked_ci_validation_failed"
        assert merge_gate["content"]["ci_validation_status"] == "failed"
        assert "Sandbox validation failed" in merge_gate["content"]["reason"]

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        blocked_events = [
            item
            for item in events.json()["items"]
            if item["event_type"] == "decision.follow_on_blocked"
        ]
        assert blocked_events
        assert any(
            item["data"]["reason"] == "ci_validation_failed" for item in blocked_events
        )
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous


def test_spend_gated_job_without_reservation_is_not_leased(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "openai.plan",
            "requires_reservation": True,
            "payload": {"prompt": "Plan a task."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-budget",
            "capabilities": ["openai.plan"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    assert poll.json()["status"] == "idle"

    with engine.begin() as connection:
        job_status = connection.execute(
            text("SELECT status FROM jobs WHERE id = :job_id"),
            {"job_id": job_id},
        ).scalar_one()
        decision_count = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM decisions
                WHERE project_id = 'boss-dogfood'
                  AND question = 'Budget blocked spend-gated job: openai.plan'
                """
            )
        ).scalar_one()
    assert job_status == "cancelled"
    assert decision_count == 1


def test_spend_gated_job_with_reservation_can_be_leased(client):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 250_000,
            "reason": "Lease an OpenAI-backed job.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "openai.plan",
            "requires_reservation": True,
            "reservation_id": reservation_id,
            "payload": {"prompt": "Plan a task."},
        },
    )
    assert created.status_code == 201

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-budget",
            "capabilities": ["openai.plan"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    payload = poll.json()
    assert payload["status"] == "leased"
    assert payload["job"]["kind"] == "openai.plan"


def test_orange_budget_defers_non_critical_job_and_creates_decision(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = 1_000_000,
                    soft_cap_pct = 0.70,
                    auto_lite_mode_pct = 0.85,
                    freeze_provisioning_pct = 0.95
                WHERE scope_type = 'project' AND scope_id = 'boss-dogfood'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, category, skill_or_model_id, estimated_usd_micros,
                    actual_usd_micros, status
                )
                VALUES (
                    'cost_orange_budget', 'boss-dogfood', 'model', 'gpt-5.2-codex',
                    860000, 860000, 'actual'
                )
                """
            )
        )

    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Defer this non-critical task."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-orange",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    assert poll.json()["status"] == "idle"

    with engine.begin() as connection:
        job = connection.execute(
            text(
                """
                SELECT status, run_after, last_error
                FROM jobs
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id},
        ).mappings().one()
        decision = connection.execute(
            text(
                """
                SELECT question, recommendation, cost_summary, status
                FROM decisions
                WHERE project_id = 'boss-dogfood'
                  AND recommendation->>'action' = 'review_budget_policy'
                  AND cost_summary->>'job_id' = :job_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"job_id": job_id},
        ).mappings().one()

    assert job["status"] == "queued"
    assert job["last_error"]["reason"] == "orange_defers_non_critical_work"
    assert decision["status"] == "pending"
    assert "Budget state orange deferred job" in decision["question"]
    assert decision["recommendation"]["budget_state"] == "orange"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "job.deferred_budget_policy" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_orange_budget_allows_high_priority_job(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = 1_000_000,
                    soft_cap_pct = 0.70,
                    auto_lite_mode_pct = 0.85,
                    freeze_provisioning_pct = 0.95
                WHERE scope_type = 'project' AND scope_id = 'boss-dogfood'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, category, skill_or_model_id, estimated_usd_micros,
                    actual_usd_micros, status
                )
                VALUES (
                    'cost_orange_allowed', 'boss-dogfood', 'model', 'gpt-5.2-codex',
                    860000, 860000, 'actual'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, owner_role_id, priority,
                    risk_tier, inputs, required_artifacts, budget_allocation_id, dedupe_key
                )
                VALUES (
                    'task_orange_p1', 'boss-dogfood', 'Orange P1 task', 'critical enough',
                    'open', NULL, 'p1', 1, '{}'::jsonb, '[]'::jsonb, NULL, 'task_orange_p1'
                )
                """
            )
        )

    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "task_id": "task_orange_p1",
            "kind": "plan.generate",
            "payload": {"command": "Allow this high-priority task."},
        },
    )
    assert created.status_code == 201

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-orange-allow",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    payload = poll.json()
    assert payload["status"] == "leased"
    assert payload["job"]["task_id"] == "task_orange_p1"


def test_red_budget_halts_non_p0_job_and_freezes_tier2_merge(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = 1_000_000,
                    soft_cap_pct = 0.70,
                    auto_lite_mode_pct = 0.80,
                    freeze_provisioning_pct = 0.90
                WHERE scope_type = 'project' AND scope_id = 'boss-dogfood'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, category, skill_or_model_id, estimated_usd_micros,
                    actual_usd_micros, status
                )
                VALUES (
                    'cost_red_budget', 'boss-dogfood', 'model', 'gpt-5.2-codex',
                    960000, 960000, 'actual'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, owner_role_id, priority,
                    risk_tier, inputs, required_artifacts, budget_allocation_id, dedupe_key
                )
                VALUES (
                    'task_red_p0', 'boss-dogfood', 'Red P0 task', 'keep running',
                    'open', NULL, 'p0', 2, '{}'::jsonb, '[]'::jsonb, NULL, 'task_red_p0'
                )
                """
            )
        )
    non_p0_job = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Halt this non-p0 task."},
        },
    )
    assert non_p0_job.status_code == 201
    merge_job = client.post(
        "/v1/software-factory/merges/queue",
        json={
            "project_id": "boss-dogfood",
            "task_id": "task_red_p0",
            "decision_id": "dec_red_merge",
            "dedupe_key": "red-merge-job",
            "pull_request_number": 7,
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/7",
            "title": "Red merge",
            "branch_name": "boss/red-merge",
        },
    )
    assert merge_job.status_code == 201
    p0_job = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "task_id": "task_red_p0",
            "kind": "plan.generate",
            "payload": {"command": "Allow this p0 task."},
        },
    )
    assert p0_job.status_code == 201

    poll_merge = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-red-merge",
            "capabilities": ["github.pull_request.merge"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll_merge.status_code == 200
    assert poll_merge.json()["status"] == "idle"

    poll_plan = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-red-plan",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll_plan.status_code == 200
    payload = poll_plan.json()
    assert payload["status"] == "leased"
    assert payload["job"]["task_id"] == "task_red_p0"

    with engine.begin() as connection:
        deferred_non_p0 = connection.execute(
            text(
                """
                SELECT status, last_error
                FROM jobs
                WHERE id = :job_id
                """
            ),
            {"job_id": non_p0_job.json()["id"]},
        ).mappings().one()
        deferred_merge = connection.execute(
            text(
                """
                SELECT status, last_error
                FROM jobs
                WHERE id = :job_id
                """
            ),
            {"job_id": merge_job.json()["id"]},
        ).mappings().one()

    assert deferred_non_p0["status"] == "queued"
    assert deferred_non_p0["last_error"]["reason"] == "red_halts_non_p0_work"
    assert deferred_merge["status"] == "queued"
    assert deferred_merge["last_error"]["reason"] == "red_freezes_tier2_effects"


def test_infra_provision_review_request_creates_decision_and_artifact(client):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for infrastructure provisioning review.",
    )

    response = client.post(
        "/v1/infra/droplets/requests",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "title": "Preview worker droplet",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 90,
            "tags": ["preview", "dogfood"],
            "actor": "ceo",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "pending_approval"
    assert payload["artifact_id"]
    assert payload["decision_id"]

    artifact = client.get(f"/v1/artifacts/{payload['artifact_id']}")
    assert artifact.status_code == 200
    assert artifact.json()["artifact_type"] == "infra_provision_request"

    inbox = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert inbox.status_code == 200
    decision = next(
        item for item in inbox.json()["items"] if item["id"] == payload["decision_id"]
    )
    assert decision["risk_tier"] == 3
    assert decision["recommendation"]["action"] == "review_infra_provisioning"


def test_approving_infra_review_decision_queues_job_when_enabled(client, engine):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for approved infra queue.",
    )
    request = client.post(
        "/v1/infra/droplets/requests",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "title": "Queued preview worker droplet",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 60,
            "tags": ["preview"],
            "actor": "ceo",
        },
    )
    assert request.status_code == 201
    decision_id = request.json()["decision_id"]

    previous_flag = api_main.decisions.settings.infra_real_provision_allowed
    api_main.decisions.settings.infra_real_provision_allowed = True
    try:
        approve = client.post(
            f"/v1/decisions/{decision_id}/approve",
            json={"actor": "ceo", "note": "Provision the preview worker."},
        )
    finally:
        api_main.decisions.settings.infra_real_provision_allowed = previous_flag

    assert approve.status_code == 200

    with engine.begin() as connection:
        job = connection.execute(
            text(
                """
                SELECT kind, payload
                FROM jobs
                WHERE project_id = 'boss-dogfood'
                  AND kind = 'infra.droplet.create'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()

    assert job["kind"] == "infra.droplet.create"
    assert job["payload"]["decision_id"] == decision_id
    assert job["payload"]["reservation_id"] == reservation_id


def test_approving_infra_review_decision_creates_gate_when_disabled(client, engine):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for blocked infra gate.",
    )
    request = client.post(
        "/v1/infra/droplets/requests",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "title": "Blocked preview worker droplet",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 60,
            "tags": ["preview"],
            "actor": "ceo",
        },
    )
    assert request.status_code == 201
    decision_id = request.json()["decision_id"]

    previous_flag = api_main.decisions.settings.infra_real_provision_allowed
    api_main.decisions.settings.infra_real_provision_allowed = False
    try:
        approve = client.post(
            f"/v1/decisions/{decision_id}/approve",
            json={"actor": "ceo", "note": "Keep infra gated."},
        )
    finally:
        api_main.decisions.settings.infra_real_provision_allowed = previous_flag

    assert approve.status_code == 200

    with engine.begin() as connection:
        gate = connection.execute(
            text(
                """
                SELECT artifact_type, content
                FROM artifacts
                WHERE project_id = 'boss-dogfood'
                  AND artifact_type = 'infra_provision_gate'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()

    assert gate["artifact_type"] == "infra_provision_gate"
    assert gate["content"]["status"] == "blocked_manual_provision_required"


def test_red_budget_freezes_infra_provisioning_job(client, engine):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for red infra freeze.",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = 1_000_000,
                    soft_cap_pct = 0.70,
                    auto_lite_mode_pct = 0.80,
                    freeze_provisioning_pct = 0.90
                WHERE scope_type = 'project' AND scope_id = 'boss-dogfood'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, category, skill_or_model_id, estimated_usd_micros,
                    actual_usd_micros, status
                )
                VALUES (
                    'cost_red_infra', 'boss-dogfood', 'infra', 'infra.do.create_droplet_real',
                    960000, 960000, 'actual'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, owner_role_id, priority,
                    risk_tier, inputs, required_artifacts, budget_allocation_id, data_boundary,
                    dedupe_key
                )
                VALUES (
                    'task_red_infra', 'boss-dogfood', 'Red infra task', 'freeze provisioning',
                    'open', NULL, 'p0', 1, '{}'::jsonb, '[]'::jsonb, NULL, 'internal',
                    'task_red_infra'
                )
                """
            )
        )
    insert_approved_infra_decision(
        engine,
        decision_id="dec_red_infra",
        reservation_id=reservation_id,
        task_id="task_red_infra",
        title="Red infra",
    )

    queued = client.post(
        "/v1/infra/droplets/queue",
        json={
            "project_id": "boss-dogfood",
            "task_id": "task_red_infra",
            "decision_id": "dec_red_infra",
            "reservation_id": reservation_id,
            "dedupe_key": "red-infra-job",
            "title": "Red infra",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 60,
            "tags": ["preview"],
            "actor": "ceo",
        },
    )
    assert queued.status_code == 201

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-red-infra",
            "capabilities": ["infra.droplet.create"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    assert poll.json()["status"] == "idle"

    with engine.begin() as connection:
        job = connection.execute(
            text(
                """
                SELECT status, last_error
                FROM jobs
                WHERE id = :job_id
                """
            ),
            {"job_id": queued.json()["id"]},
        ).mappings().one()

    assert job["status"] == "queued"
    assert job["last_error"]["reason"] == "red_freezes_infra_provisioning"


def test_queue_infra_job_is_leasable_and_completion_passes_through(client, engine):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for queued infra job.",
    )
    insert_approved_infra_decision(
        engine,
        decision_id="dec_queue_infra",
        reservation_id=reservation_id,
    )

    queued = client.post(
        "/v1/infra/droplets/queue",
        json={
            "project_id": "boss-dogfood",
            "decision_id": "dec_queue_infra",
            "reservation_id": reservation_id,
            "dedupe_key": "queued-infra-job",
            "title": "Queued infra",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 60,
            "tags": ["preview"],
            "actor": "ceo",
        },
    )
    assert queued.status_code == 201
    job_id = queued.json()["id"]

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-infra",
            "capabilities": ["infra.droplet.create"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id

    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-infra",
            "job_id": job_id,
            "lease_token": leased["job"]["lease_token"],
            "result": {
                "status": "created",
                "artifact_id": "art_infra",
                "receipt_id": "rcpt_infra",
                "decision_id": "dec_queue_infra",
                "resource_id": "infra_123",
                "provider_ref": "https://api.digitalocean.com/v2/droplets/123",
                "provider_resource_id": "123",
                "resource_name": "boss-preview",
                "expires_at": "2026-06-03T12:00:00+00:00",
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["resource_id"] == "infra_123"
    assert payload["provider_resource_id"] == "123"


def test_infra_provision_blocks_without_credentials_and_releases_reservation(
    client, engine
):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for blocked infra provisioning.",
    )
    insert_approved_infra_decision(
        engine,
        decision_id="dec_infra_missing_creds",
        reservation_id=reservation_id,
    )

    previous_flag = api_main.infrastructure.settings.infra_real_provision_allowed
    previous_token = api_main.infrastructure.settings.digitalocean_token
    api_main.infrastructure.settings.infra_real_provision_allowed = False
    api_main.infrastructure.settings.digitalocean_token = None
    try:
        response = client.post(
            "/v1/infra/droplets",
            json={
                "project_id": "boss-dogfood",
                "decision_id": "dec_infra_missing_creds",
                "reservation_id": reservation_id,
                "dedupe_key": "infra-missing-creds",
                "title": "Blocked infra",
                "region": "nyc3",
                "size": "s-1vcpu-1gb",
                "ttl_minutes": 60,
                "tags": ["preview"],
            },
        )
    finally:
        api_main.infrastructure.settings.infra_real_provision_allowed = previous_flag
        api_main.infrastructure.settings.digitalocean_token = previous_token

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert payload["receipt_id"]
    assert "BOSS_INFRA_REAL_PROVISION_ALLOWED is false" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :reservation_id"),
            {"reservation_id": reservation_id},
        ).scalar_one()
        receipt = connection.execute(
            text(
                """
                SELECT effect_kind, status
                FROM receipts
                WHERE dedupe_key = 'infra-missing-creds'
                """
            )
        ).mappings().one()

    assert reservation_status == "released"
    assert receipt["effect_kind"] == "infra.digitalocean.droplet.create"
    assert receipt["status"] == "failed"


def test_infra_provision_succeeds_and_records_resource(client, engine, monkeypatch):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for successful infra provisioning.",
    )
    insert_approved_infra_decision(
        engine,
        decision_id="dec_infra_success",
        reservation_id=reservation_id,
        title="Successful infra",
    )

    previous_flag = api_main.infrastructure.settings.infra_real_provision_allowed
    previous_token = api_main.infrastructure.settings.digitalocean_token
    api_main.infrastructure.settings.infra_real_provision_allowed = True
    api_main.infrastructure.settings.digitalocean_token = "do-test-token"
    monkeypatch.setattr(
        api_main.infrastructure.digitalocean,
        "create_droplet",
        lambda **kwargs: DropletProvisionResult(
            droplet_id="123456",
            name=kwargs["name"],
            region=kwargs["region"],
            size=kwargs["size"],
            status="new",
            api_url="https://api.digitalocean.com/v2/droplets/123456",
        ),
    )
    try:
        response = client.post(
            "/v1/infra/droplets",
            json={
                "project_id": "boss-dogfood",
                "decision_id": "dec_infra_success",
                "reservation_id": reservation_id,
                "dedupe_key": "infra-success",
                "title": "Successful infra",
                "region": "nyc3",
                "size": "s-1vcpu-1gb",
                "ttl_minutes": 60,
                "tags": ["preview"],
            },
        )
    finally:
        api_main.infrastructure.settings.infra_real_provision_allowed = previous_flag
        api_main.infrastructure.settings.digitalocean_token = previous_token

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "created"
    assert payload["resource_id"]
    assert payload["provider_resource_id"] == "123456"
    assert payload["provider_ref"].endswith("/123456")

    resources = client.get(
        "/v1/infra/resources", params={"project_id": "boss-dogfood", "limit": 10}
    )
    assert resources.status_code == 200
    resource = next(
        item for item in resources.json()["items"] if item["id"] == payload["resource_id"]
    )
    assert resource["status"] == "active"
    assert resource["external_resource_id"] == "123456"
    assert resource["provision_receipt_id"] == payload["receipt_id"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :reservation_id"),
            {"reservation_id": reservation_id},
        ).scalar_one()
        cost = connection.execute(
            text(
                """
                SELECT category, skill_or_model_id, status
                FROM cost_receipts
                WHERE receipt_id = :receipt_id
                """
            ),
            {"receipt_id": payload["receipt_id"]},
        ).mappings().one()

    assert reservation_status == "consumed"
    assert cost["category"] == "infra"
    assert cost["skill_or_model_id"] == "infra.do.create_droplet_real"
    assert cost["status"] == "estimated"


def test_infra_provision_blocks_restricted_data_boundary_before_receipt(client, engine):
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Restricted infra task",
            "dedupe_key": "restricted-infra-task",
            "data_boundary": "restricted",
        },
    )
    assert task.status_code == 201
    task_id = task.json()["id"]
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for restricted infra provisioning.",
    )
    insert_approved_infra_decision(
        engine,
        decision_id="dec_infra_restricted",
        reservation_id=reservation_id,
        task_id=task_id,
        title="Restricted infra",
        data_boundary="restricted",
    )

    response = client.post(
        "/v1/infra/droplets",
        json={
            "project_id": "boss-dogfood",
            "task_id": task_id,
            "decision_id": "dec_infra_restricted",
            "reservation_id": reservation_id,
            "dedupe_key": "infra-restricted",
            "title": "Restricted infra",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 60,
            "tags": ["preview"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["data_boundary"] == "restricted"
    assert "external infrastructure providers" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :reservation_id"),
            {"reservation_id": reservation_id},
        ).scalar_one()
        receipt_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM receipts
                WHERE dedupe_key = 'infra-restricted'
                """
            )
        ).scalar_one()
        denied_event_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM events
                WHERE event_type = 'policy.denied'
                  AND entity_type = 'infra_effect'
                  AND entity_id = 'infra.digitalocean.droplet.create'
                """
            )
        ).scalar_one()

    assert reservation_status == "released"
    assert receipt_count == 0
    assert denied_event_count == 1


def test_infra_termination_and_janitor_queue_are_visible(client, engine, monkeypatch):
    reservation_id = reserve_for_infra_provision(
        client,
        "Reserve budget for infra termination setup.",
    )
    insert_approved_infra_decision(
        engine,
        decision_id="dec_infra_terminate",
        reservation_id=reservation_id,
        title="Terminated infra",
    )

    previous_flag = api_main.infrastructure.settings.infra_real_provision_allowed
    previous_token = api_main.infrastructure.settings.digitalocean_token
    api_main.infrastructure.settings.infra_real_provision_allowed = True
    api_main.infrastructure.settings.digitalocean_token = "do-test-token"
    monkeypatch.setattr(
        api_main.infrastructure.digitalocean,
        "create_droplet",
        lambda **kwargs: DropletProvisionResult(
            droplet_id="654321",
            name=kwargs["name"],
            region=kwargs["region"],
            size=kwargs["size"],
            status="active",
            api_url="https://api.digitalocean.com/v2/droplets/654321",
        ),
    )
    create = client.post(
        "/v1/infra/droplets",
        json={
            "project_id": "boss-dogfood",
            "decision_id": "dec_infra_terminate",
            "reservation_id": reservation_id,
            "dedupe_key": "infra-terminate-setup",
            "title": "Terminated infra",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 10,
            "tags": ["preview"],
        },
    )
    assert create.status_code == 200
    resource_id = create.json()["resource_id"]
    monkeypatch.setattr(
        api_main.infrastructure.digitalocean,
        "delete_droplet",
        lambda droplet_id: DropletTerminationResult(
            droplet_id=droplet_id,
            api_url=f"https://api.digitalocean.com/v2/droplets/{droplet_id}",
        ),
    )
    try:
        terminate = client.post(
            f"/v1/infra/resources/{resource_id}/terminate",
            json={
                "project_id": "boss-dogfood",
                "dedupe_key": "infra-terminate-direct",
                "actor": "release_manager",
                "reason": "cleanup",
            },
        )
    finally:
        api_main.infrastructure.settings.infra_real_provision_allowed = previous_flag
        api_main.infrastructure.settings.digitalocean_token = previous_token

    assert terminate.status_code == 200
    assert terminate.json()["status"] == "terminated"

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE infra_resources
                SET status = 'active',
                    terminated_at = NULL,
                    termination_reason = NULL,
                    termination_receipt_id = NULL,
                    termination_artifact_id = NULL,
                    expires_at = now() - interval '5 minutes'
                WHERE id = :resource_id
                """
            ),
            {"resource_id": resource_id},
        )

    janitor = client.post(
        "/v1/infra/janitor/run",
        json={
            "project_id": "boss-dogfood",
            "actor": "system",
            "limit": 10,
        },
    )
    assert janitor.status_code == 200
    janitor_payload = janitor.json()
    assert janitor_payload["status"] == "queued"
    assert resource_id in janitor_payload["resource_ids"]
    assert janitor_payload["queued_job_ids"]

def test_queue_pr_proposal_job_is_leasable_and_completion_passes_through(client):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 500_000,
            "reason": "Queue a PR proposal job.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    queued = client.post(
        "/v1/software-factory/proposals/queue",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "dedupe_key": "queued-pr-proposal",
            "title": "Sandbox PR",
            "branch_name": "boss/test-queued-pr",
            "summary": "Create a queued sandbox PR proposal.",
            "files": [{"path": "README.md", "content": "hello"}],
        },
    )
    assert queued.status_code == 201
    job_id = queued.json()["id"]

    fetched = client.get(f"/v1/jobs/{job_id}")
    assert fetched.status_code == 200
    assert fetched.json()["kind"] == "software_factory.propose_pr"

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-pr",
            "capabilities": ["software_factory.propose_pr"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id

    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-pr",
            "job_id": job_id,
            "lease_token": leased["job"]["lease_token"],
            "result": {
                "status": "created",
                "artifact_id": "art_review",
                "diff_artifact_id": "art_diff",
                "test_report_artifact_id": "art_test_report",
                "receipt_id": "rcpt_pr",
                "decision_id": "dec_pr",
                "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/1",
                "branch_name": "boss/test-queued-pr",
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["artifact_id"] == "art_review"
    assert payload["diff_artifact_id"] == "art_diff"
    assert payload["test_report_artifact_id"] == "art_test_report"
    assert payload["receipt_id"] == "rcpt_pr"
    assert payload["decision_id"] == "dec_pr"
    assert payload["pull_request_url"].endswith("/pull/1")


def test_pr_proposal_blocks_without_github_credentials_and_releases_reservation(
    client, engine, monkeypatch
):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 500_000,
            "reason": "PR proposal without GitHub credentials.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    monkeypatch.setattr(
        api_main.software_factory.openai,
        "create_text_response",
        lambda prompt, system_prompt=None, model=None: (
            "resp_review_packet",
            "Review packet",
        ),
    )
    previous = api_main.software_factory.settings.github_token
    previous_openai = api_main.software_factory.settings.openai_api_key
    api_main.software_factory.settings.github_token = None
    api_main.software_factory.settings.openai_api_key = "test-openai-key"
    try:
        response = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "missing-github-creds-pr",
                "title": "Sandbox PR",
                "branch_name": "boss/missing-github-creds",
                "summary": "Exercise fail-closed GitHub proposal behavior.",
                "files": [{"path": "README.md", "content": "hello"}],
            },
        )
    finally:
        api_main.software_factory.settings.github_token = previous
        api_main.software_factory.settings.openai_api_key = previous_openai

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert "BOSS_GITHUB_TOKEN" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text(
                """
                SELECT status
                FROM budget_reservations
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        ).scalar_one()
        decision_question = connection.execute(
            text(
                """
                SELECT question
                FROM decisions
                WHERE id = :decision_id
                """
            ),
            {"decision_id": payload["decision_id"]},
        ).scalar_one()

    assert reservation_status == "released"
    assert decision_question == "GitHub integration is not configured"


def test_pr_proposal_blocks_restricted_data_boundary_before_github_effect(
    client, engine
):
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Restricted PR proposal",
            "dedupe_key": "restricted-pr-task",
            "data_boundary": "restricted",
        },
    )
    assert task.status_code == 201
    task_id = task.json()["id"]

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 500_000,
            "reason": "Restricted PR proposal should be blocked before GitHub.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    response = client.post(
        "/v1/software-factory/proposals",
        json={
            "project_id": "boss-dogfood",
            "task_id": task_id,
            "reservation_id": reservation_id,
            "dedupe_key": "restricted-github-pr",
            "title": "Restricted Sandbox PR",
            "branch_name": "boss/restricted-pr",
            "summary": "This must not be sent to GitHub.",
            "files": [{"path": "README.md", "content": "restricted"}],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["data_boundary"] == "restricted"
    assert "GitHub" in payload["message"]

    with engine.begin() as connection:
        reservation_status = connection.execute(
            text(
                """
                SELECT status
                FROM budget_reservations
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        ).scalar_one()
        receipt_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM receipts
                WHERE dedupe_key = 'restricted-github-pr'
                """
            )
        ).scalar_one()
        decision_question = connection.execute(
            text("SELECT question FROM decisions WHERE id = :decision_id"),
            {"decision_id": payload["decision_id"]},
        ).scalar_one()
        denied_event_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM events
                WHERE event_type = 'policy.denied'
                  AND entity_type = 'github_effect'
                  AND entity_id = 'github.pull_request.create'
                """
            )
        ).scalar_one()

    assert reservation_status == "released"
    assert receipt_count == 0
    assert decision_question == "Data boundary blocks GitHub external effect"
    assert denied_event_count == 1


def test_pr_proposal_records_github_validation_evidence(client, monkeypatch):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 500_000,
            "reason": "PR proposal with validation evidence.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous_openai = api_main.software_factory.settings.openai_api_key
    api_main.software_factory.settings.openai_api_key = "test-openai-key"
    monkeypatch.setattr(
        api_main.software_factory.openai,
        "create_text_response",
        lambda prompt, system_prompt=None, model=None: (
            "resp_review_packet",
            "## Implementation summary\n- Review packet generated with CI evidence.",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "ensure_branch",
        lambda branch_name: "sha-base",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "upsert_file",
        lambda branch_name, path, content, message: f"commit-{path}",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "create_pull_request",
        lambda branch_name, title, body: PullRequestResult(
            url="https://github.com/seanheiney/boss-sandbox/pull/5",
            number=5,
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "wait_for_commit_checks",
        lambda head_sha, timeout_seconds, poll_interval_seconds: PullRequestValidationResult(
            status="passed",
            summary="GitHub validation passed: CI.",
            head_sha=head_sha,
            completed=True,
            checks=[
                PullRequestValidationCheck(
                    name="CI",
                    status="completed",
                    conclusion="success",
                    html_url="https://github.com/seanheiney/boss-sandbox/actions/runs/1",
                    started_at="2026-06-02T12:00:00Z",
                    completed_at="2026-06-02T12:01:00Z",
                )
            ],
        ),
    )

    try:
        response = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": "boss-dogfood",
                "reservation_id": reservation_id,
                "dedupe_key": "pr-proposal-validation-evidence",
                "title": "Sandbox PR with CI",
                "branch_name": "boss/pr-validation-evidence",
                "summary": "Collect GitHub validation evidence.",
                "files": [{"path": "README.md", "content": "hello validation"}],
            },
        )
    finally:
        api_main.software_factory.settings.openai_api_key = previous_openai

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "created"
    assert payload["validation_status"] == "passed"
    assert "GitHub validation passed" in payload["validation_summary"]

    test_report = client.get(f"/v1/artifacts/{payload['test_report_artifact_id']}")
    assert test_report.status_code == 200
    test_report_payload = test_report.json()
    assert test_report_payload["content"]["status"] == "passed"
    assert test_report_payload["content"]["tests_added_or_run"] == ["CI"]
    assert test_report_payload["content"]["checks"][0]["conclusion"] == "success"

    review_packet = client.get(f"/v1/artifacts/{payload['artifact_id']}")
    assert review_packet.status_code == 200
    review_payload = review_packet.json()
    assert review_payload["content"]["ci_validation_status"] == "passed"
    assert review_payload["content"]["confidence_score"] == "ci-passed-manual-review-required"


def test_queue_merge_job_is_leasable_and_completion_passes_through(client):
    queued = client.post(
        "/v1/software-factory/merges/queue",
        json={
            "project_id": "boss-dogfood",
            "decision_id": "dec_merge",
            "dedupe_key": "queued-pr-merge",
            "pull_request_number": 42,
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
            "title": "Merge Demo",
            "branch_name": "boss/demo-pr",
        },
    )
    assert queued.status_code == 201
    job_id = queued.json()["id"]

    fetched = client.get(f"/v1/jobs/{job_id}")
    assert fetched.status_code == 200
    assert fetched.json()["kind"] == "github.pull_request.merge"

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-merge",
            "capabilities": ["github.pull_request.merge"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id

    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-merge",
            "job_id": job_id,
            "lease_token": leased["job"]["lease_token"],
            "result": {
                "status": "created",
                "artifact_id": "art_merge",
                "receipt_id": "rcpt_merge",
                "decision_id": "dec_merge",
                "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                "branch_name": "boss/demo-pr",
                "merged_sha": "abc123",
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["artifact_id"] == "art_merge"
    assert payload["receipt_id"] == "rcpt_merge"
    assert payload["decision_id"] == "dec_merge"
    assert payload["pull_request_url"].endswith("/pull/42")
    assert payload["merged_sha"] == "abc123"


def test_approving_deployment_review_decision_creates_deploy_gate_block_artifact(
    client, engine
):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, default_action, status
                )
                VALUES (
                    'dec_review_deploy', 'boss-dogfood', NULL, 'Approve sandbox deployment to staging: Demo deploy',
                    CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'pending'
                )
                """
            ),
            {
                "recommendation": json.dumps(
                    {
                        "action": "review_deployment",
                        "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                        "branch_name": "boss/demo-pr",
                        "title": "Demo deploy",
                        "merged_sha": "merge-sha-42",
                        "environment": "staging",
                    }
                )
            },
        )

    approve = client.post(
        "/v1/decisions/dec_review_deploy/approve",
        json={"actor": "ceo", "note": "Manual deploy is acceptable."},
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"

    with engine.begin() as connection:
        deploy_gate = connection.execute(
            text(
                """
                SELECT artifact_type, title, content
                FROM artifacts
                WHERE project_id = 'boss-dogfood' AND artifact_type = 'deployment_gate'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()
        queued_jobs = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM jobs
                WHERE project_id = 'boss-dogfood' AND kind = 'github.deployment.create'
                """
            )
        ).scalar_one()

    assert deploy_gate["artifact_type"] == "deployment_gate"
    assert deploy_gate["title"] == "Deployment gate for Demo deploy"
    assert deploy_gate["content"]["status"] == "blocked_manual_deploy_required"
    assert deploy_gate["content"]["environment"] == "staging"
    assert deploy_gate["content"]["merged_sha"] == "merge-sha-42"
    assert queued_jobs == 0

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.follow_on_blocked" in event_types


def test_queue_deployment_job_is_leasable_and_completion_passes_through(client):
    queued = client.post(
        "/v1/software-factory/deployments/queue",
        json={
            "project_id": "boss-dogfood",
            "decision_id": "dec_deploy",
            "dedupe_key": "queued-deploy",
            "merged_sha": "merge-sha-42",
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
            "title": "Deploy Demo",
            "branch_name": "boss/demo-pr",
            "environment": "staging",
        },
    )
    assert queued.status_code == 201
    job_id = queued.json()["id"]

    fetched = client.get(f"/v1/jobs/{job_id}")
    assert fetched.status_code == 200
    assert fetched.json()["kind"] == "github.deployment.create"

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-deploy",
            "capabilities": ["github.deployment.create"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id

    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-deploy",
            "job_id": job_id,
            "lease_token": leased["job"]["lease_token"],
            "result": {
                "status": "created",
                "artifact_id": "art_deploy",
                "receipt_id": "rcpt_deploy",
                "decision_id": "dec_deploy",
                "environment": "staging",
                "deployment_id": 12345,
                "deployment_status": "success",
                "deployment_url": "https://api.github.com/repos/seanheiney/boss-sandbox/deployments/12345",
                "merged_sha": "merge-sha-42",
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["artifact_id"] == "art_deploy"
    assert payload["receipt_id"] == "rcpt_deploy"
    assert payload["decision_id"] == "dec_deploy"
    assert payload["environment"] == "staging"
    assert payload["deployment_id"] == 12345
    assert payload["deployment_status"] == "success"


def test_deployment_blocks_without_github_credentials(client, engine):
    previous = api_main.software_factory.settings.github_token
    api_main.software_factory.settings.github_token = None
    try:
        response = client.post(
            "/v1/software-factory/deployments",
            json={
                "project_id": "boss-dogfood",
                "decision_id": "dec_deploy_missing_github",
                "dedupe_key": "missing-github-creds-deploy",
                "merged_sha": "merge-sha-42",
                "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                "title": "Deploy Demo",
                "branch_name": "boss/demo-pr",
                "environment": "staging",
            },
        )
    finally:
        api_main.software_factory.settings.github_token = previous

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert "BOSS_GITHUB_TOKEN" in payload["message"]

    with engine.begin() as connection:
        decision_question = connection.execute(
            text(
                """
                SELECT question
                FROM decisions
                WHERE id = :decision_id
                """
            ),
            {"decision_id": payload["decision_id"]},
        ).scalar_one()
        deploy_artifact_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM artifacts
                WHERE project_id = 'boss-dogfood' AND artifact_type = 'deployment_result'
                """
            )
        ).scalar_one()

    assert decision_question == "GitHub integration is not configured"
    assert deploy_artifact_count == 0


def test_deployment_healthcheck_failure_creates_blocking_decision(client, engine, monkeypatch):
    previous = api_main.software_factory.settings.sandbox_deploy_healthcheck_url
    api_main.software_factory.settings.sandbox_deploy_healthcheck_url = (
        "https://sandbox.example.test/health"
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "create_deployment",
        lambda ref, environment, description: DeploymentResult(
            id=991,
            environment=environment,
            sha=ref,
            url="https://api.github.com/repos/seanheiney/boss-sandbox/deployments/991",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "mark_deployment_failure",
        lambda deployment_id, environment, environment_url=None, description=None: DeploymentStatusResult(
            state="failure",
            url="https://api.github.com/repos/seanheiney/boss-sandbox/deployments/991/statuses/1",
            environment_url=environment_url,
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory,
        "_verify_deployment_health",
        lambda healthcheck_url: {
            "status": "failed",
            "healthcheck_url": healthcheck_url,
            "summary": "Deployment health check returned HTTP 503.",
            "http_status": 503,
            "response_excerpt": "service unavailable",
        },
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, default_action, status, approved_by, approved_at
                )
                VALUES (
                    'dec_deploy_health_fail', 'boss-dogfood', NULL, 'Approve sandbox deployment to staging: Demo deploy',
                    CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold',
                    'approved', 'ceo', now()
                )
                """
            ),
            {
                "recommendation": json.dumps(
                    {
                        "action": "review_deployment",
                        "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                        "branch_name": "boss/demo-pr",
                        "title": "Deploy Demo",
                        "merged_sha": "merge-sha-42",
                        "environment": "staging",
                    }
                )
            },
        )

    try:
        response = client.post(
            "/v1/software-factory/deployments",
            json={
                "project_id": "boss-dogfood",
                "decision_id": "dec_deploy_health_fail",
                "dedupe_key": "deploy-health-fail",
                "merged_sha": "merge-sha-42",
                "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                "title": "Deploy Demo",
                "branch_name": "boss/demo-pr",
                "environment": "staging",
            },
        )
    finally:
        api_main.software_factory.settings.sandbox_deploy_healthcheck_url = previous

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["health_verification_status"] == "failed"
    assert payload["healthcheck_url"] == "https://sandbox.example.test/health"
    assert payload["health_decision_id"]
    assert payload["health_verification_artifact_id"]
    assert payload["deployment_status"] == "failure"
    assert payload["rollout_id"]
    assert payload["rollout_artifact_id"]
    assert payload["rollout_status"] == "blocked"
    assert payload["rollout_stage"] == "post_deploy_health"

    with engine.begin() as connection:
        original_decision_status = connection.execute(
            text("SELECT status FROM decisions WHERE id = 'dec_deploy_health_fail'")
        ).scalar_one()
        health_decision = connection.execute(
            text(
                """
                SELECT question, recommendation, status
                FROM decisions
                WHERE id = :decision_id
                """
            ),
            {"decision_id": payload["health_decision_id"]},
        ).mappings().one()
        health_artifact = connection.execute(
            text(
                """
                SELECT artifact_type, content
                FROM artifacts
                WHERE id = :artifact_id
                """
            ),
            {"artifact_id": payload["health_verification_artifact_id"]},
        ).mappings().one()
        rollout = connection.execute(
            text(
                """
                SELECT status, current_stage, rollback_decision_id, stages
                FROM deployment_rollouts
                WHERE id = :rollout_id
                """
            ),
            {"rollout_id": payload["rollout_id"]},
        ).mappings().one()
        rollout_artifact = connection.execute(
            text(
                """
                SELECT artifact_type, content
                FROM artifacts
                WHERE id = :artifact_id
                """
            ),
            {"artifact_id": payload["rollout_artifact_id"]},
        ).mappings().one()

    assert original_decision_status == "approved"
    assert "Review failed sandbox deployment health check" in health_decision["question"]
    assert health_decision["recommendation"]["action"] == "review_deployment_health"
    assert health_artifact["artifact_type"] == "deployment_health_check"
    assert health_artifact["content"]["verification"]["http_status"] == 503
    assert rollout["status"] == "blocked"
    assert rollout["current_stage"] == "post_deploy_health"
    assert rollout["rollback_decision_id"] == payload["health_decision_id"]
    assert rollout["stages"][1]["status"] == "failed"
    assert rollout_artifact["artifact_type"] == "deployment_rollout"
    assert rollout_artifact["content"]["status"] == "blocked"

    rollouts = client.get(
        "/v1/software-factory/rollouts",
        params={"project_id": "boss-dogfood"},
    )
    assert rollouts.status_code == 200
    rollout_items = rollouts.json()["items"]
    assert rollout_items[0]["id"] == payload["rollout_id"]
    assert rollout_items[0]["status"] == "blocked"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "software_factory.deployment_health_blocked" in event_types
    assert "software_factory.deployment_rollout_recorded" in event_types


def test_merge_blocks_without_github_credentials(client, engine):
    previous = api_main.software_factory.settings.github_token
    api_main.software_factory.settings.github_token = None
    try:
        response = client.post(
            "/v1/software-factory/merges",
            json={
                "project_id": "boss-dogfood",
                "decision_id": "dec_merge_missing_github",
                "dedupe_key": "missing-github-creds-merge",
                "pull_request_number": 42,
                "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                "title": "Merge Demo",
                "branch_name": "boss/demo-pr",
            },
        )
    finally:
        api_main.software_factory.settings.github_token = previous

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert "BOSS_GITHUB_TOKEN" in payload["message"]

    with engine.begin() as connection:
        decision_question = connection.execute(
            text(
                """
                SELECT question
                FROM decisions
                WHERE id = :decision_id
                """
            ),
            {"decision_id": payload["decision_id"]},
        ).scalar_one()
        merge_artifact_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM artifacts
                WHERE project_id = 'boss-dogfood' AND artifact_type = 'merge_result'
                """
            )
        ).scalar_one()

    assert decision_question == "GitHub integration is not configured"
    assert merge_artifact_count == 0


def test_merge_and_deployment_block_restricted_data_boundary_before_github_receipts(
    client, engine
):
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": "boss-dogfood",
            "title": "Restricted release",
            "dedupe_key": "restricted-release-task",
            "data_boundary": "restricted",
        },
    )
    assert task.status_code == 201
    task_id = task.json()["id"]
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options,
                    risk_tier, cost_summary, default_action, status
                )
                VALUES (
                    'dec_restricted_release', 'boss-dogfood', :task_id,
                    'Restricted release decision',
                    '{}'::jsonb, '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'approved'
                )
                """
            ),
            {"task_id": task_id},
        )

    merge = client.post(
        "/v1/software-factory/merges",
        json={
            "project_id": "boss-dogfood",
            "decision_id": "dec_restricted_release",
            "dedupe_key": "restricted-merge-effect",
            "pull_request_number": 42,
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
            "title": "Restricted Merge",
            "branch_name": "boss/restricted-release",
        },
    )
    assert merge.status_code == 200
    merge_payload = merge.json()
    assert merge_payload["status"] == "blocked"
    assert merge_payload["data_boundary"] == "restricted"

    deploy = client.post(
        "/v1/software-factory/deployments",
        json={
            "project_id": "boss-dogfood",
            "decision_id": "dec_restricted_release",
            "dedupe_key": "restricted-deploy-effect",
            "merged_sha": "abc123",
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
            "title": "Restricted Deploy",
            "branch_name": "boss/restricted-release",
            "environment": "staging",
        },
    )
    assert deploy.status_code == 200
    deploy_payload = deploy.json()
    assert deploy_payload["status"] == "blocked"
    assert deploy_payload["data_boundary"] == "restricted"

    with engine.begin() as connection:
        receipt_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM receipts
                WHERE dedupe_key IN ('restricted-merge-effect', 'restricted-deploy-effect')
                """
            )
        ).scalar_one()
        denied_effects = connection.execute(
            text(
                """
                SELECT entity_id
                FROM events
                WHERE event_type = 'policy.denied'
                  AND entity_type = 'github_effect'
                ORDER BY entity_id
                """
            )
        ).scalars().all()

    assert receipt_count == 0
    assert "github.pull_request.merge" in denied_effects
    assert "github.deployment.create" in denied_effects


def test_command_to_merge_flow_completes_task_when_merge_policy_enabled(
    client, engine, monkeypatch
):
    previous = api_main.decisions.settings.enable_sandbox_merge
    previous_openai = api_main.software_factory.settings.openai_api_key
    api_main.decisions.settings.enable_sandbox_merge = True
    api_main.software_factory.settings.openai_api_key = "test-openai-key"
    monkeypatch.setattr(
        api_main.software_factory.openai,
        "create_text_response",
        lambda prompt, system_prompt=None, model=None: (
            "resp_review_packet",
            "## Implementation summary\n- Review packet generated for test coverage.",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "ensure_branch",
        lambda branch_name: "sha-base",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "upsert_file",
        lambda branch_name, path, content, message: f"commit-{path}",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "create_pull_request",
        lambda branch_name, title, body: PullRequestResult(
            url="https://github.com/seanheiney/boss-sandbox/pull/99",
            number=99,
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "wait_for_commit_checks",
        lambda head_sha, timeout_seconds, poll_interval_seconds: PullRequestValidationResult(
            status="passed",
            summary="GitHub validation passed: CI.",
            head_sha=head_sha,
            completed=True,
            checks=[
                PullRequestValidationCheck(
                    name="CI",
                    status="completed",
                    conclusion="success",
                    html_url="https://github.com/seanheiney/boss-sandbox/actions/runs/99",
                    started_at="2026-06-02T12:00:00Z",
                    completed_at="2026-06-02T12:01:00Z",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "merge_pull_request",
        lambda pull_request_number, commit_title: PullRequestMergeResult(
            sha="merge-sha-99",
            merged=True,
            message="Pull request successfully merged",
        ),
    )

    try:
        created = client.post(
            "/v1/commands/propose",
            json={
                "project_id": "boss-dogfood",
                "command": "Ship the next sandbox merge audit improvement.",
                "constraints": {"deadline": "this week"},
            },
        )
        assert created.status_code == 201
        payload = created.json()
        task_id = payload["task_id"]
        command_job_id = payload["job_id"]

        leased_plan = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-e2e-plan",
                "capabilities": ["plan.command"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_plan.status_code == 200
        leased_plan_job = leased_plan.json()["job"]
        assert leased_plan_job["id"] == command_job_id

        heartbeat_plan = client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-e2e-plan",
                "job_id": command_job_id,
                "lease_token": leased_plan_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        )
        assert heartbeat_plan.status_code == 200

        completed_plan = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": "worker-e2e-plan",
                "job_id": command_job_id,
                "lease_token": leased_plan_job["lease_token"],
                "result": {
                    "summary": "Ship the next sandbox merge audit improvement.",
                    "objectives": ["Ship the next sandbox merge audit improvement."],
                    "steps": [
                        "Prepare a reviewable proposal.",
                        "Review the sandbox PR.",
                        "Merge after approval.",
                    ],
                    "risks": ["Budget or provider credentials could block progress."],
                    "done_when": ["The sandbox PR is merged and the task is completed."],
                },
            },
        )
        assert completed_plan.status_code == 200
        plan_decision_id = completed_plan.json()["decision_id"]

        approve_plan = client.post(
            f"/v1/decisions/{plan_decision_id}/approve",
            json={"actor": "ceo", "note": "Proceed to PR proposal."},
        )
        assert approve_plan.status_code == 200
        assert approve_plan.json()["status"] == "approved"

        leased_pr = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-e2e-pr",
                "capabilities": ["software_factory.propose_pr"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_pr.status_code == 200
        leased_pr_job = leased_pr.json()["job"]
        assert leased_pr_job["kind"] == "software_factory.propose_pr"

        heartbeat_pr = client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-e2e-pr",
                "job_id": leased_pr_job["id"],
                "lease_token": leased_pr_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        )
        assert heartbeat_pr.status_code == 200

        proposal_run = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": leased_pr_job["project_id"],
                "task_id": leased_pr_job["task_id"],
                "reservation_id": leased_pr_job["payload"]["reservation_id"],
                "dedupe_key": leased_pr_job["payload"]["dedupe_key"],
                "title": leased_pr_job["payload"]["title"],
                "branch_name": leased_pr_job["payload"]["branch_name"],
                "summary": leased_pr_job["payload"]["summary"],
                "files": leased_pr_job["payload"]["files"],
                "created_by_job_id": leased_pr_job["id"],
            },
        )
        assert proposal_run.status_code == 200
        proposal_result = proposal_run.json()
        assert proposal_result["status"] == "created"
        assert proposal_result["validation_status"] == "passed"
        pr_decision_id = proposal_result["decision_id"]

        complete_pr = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": "worker-e2e-pr",
                "job_id": leased_pr_job["id"],
                "lease_token": leased_pr_job["lease_token"],
                "result": proposal_result,
            },
        )
        assert complete_pr.status_code == 200
        assert complete_pr.json()["status"] == "succeeded"

        approve_pr = client.post(
            f"/v1/decisions/{pr_decision_id}/approve",
            json={"actor": "ceo", "note": "Merge the approved sandbox PR."},
        )
        assert approve_pr.status_code == 200
        assert approve_pr.json()["status"] == "approved"

        leased_merge = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-e2e-merge",
                "capabilities": ["github.pull_request.merge"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_merge.status_code == 200
        leased_merge_job = leased_merge.json()["job"]
        assert leased_merge_job["kind"] == "github.pull_request.merge"

        heartbeat_merge = client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-e2e-merge",
                "job_id": leased_merge_job["id"],
                "lease_token": leased_merge_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        )
        assert heartbeat_merge.status_code == 200

        merge_run = client.post(
            "/v1/software-factory/merges",
            json={
                "project_id": leased_merge_job["project_id"],
                "decision_id": leased_merge_job["payload"]["decision_id"],
                "dedupe_key": leased_merge_job["payload"]["dedupe_key"],
                "pull_request_number": leased_merge_job["payload"]["pull_request_number"],
                "pull_request_url": leased_merge_job["payload"]["pull_request_url"],
                "title": leased_merge_job["payload"]["title"],
                "branch_name": leased_merge_job["payload"].get("branch_name"),
                "created_by_job_id": leased_merge_job["id"],
            },
        )
        assert merge_run.status_code == 200
        merge_result = merge_run.json()
        assert merge_result["status"] == "created"
        assert merge_result["merged_sha"] == "merge-sha-99"

        complete_merge = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": "worker-e2e-merge",
                "job_id": leased_merge_job["id"],
                "lease_token": leased_merge_job["lease_token"],
                "result": merge_result,
            },
        )
        assert complete_merge.status_code == 200
        assert complete_merge.json()["status"] == "succeeded"

        with engine.begin() as connection:
            task_status = connection.execute(
                text("SELECT status FROM tasks WHERE id = :task_id"),
                {"task_id": task_id},
            ).scalar_one()
            merge_decision_status = connection.execute(
                text("SELECT status FROM decisions WHERE id = :decision_id"),
                {"decision_id": pr_decision_id},
            ).scalar_one()
            artifact_types = [
                row["artifact_type"]
                for row in connection.execute(
                    text(
                        """
                        SELECT artifact_type
                        FROM artifacts
                        WHERE project_id = 'boss-dogfood'
                        ORDER BY created_at ASC
                        """
                    )
                ).mappings().all()
            ]
            receipt_count = connection.execute(
                text("SELECT COUNT(*) FROM receipts WHERE project_id = 'boss-dogfood'")
            ).scalar_one()

        assert task_status == "completed"
        assert merge_decision_status == "executed"
        assert "command_brief" in artifact_types
        assert "plan_draft" in artifact_types
        assert "review_packet" in artifact_types
        assert "diff_manifest" in artifact_types
        assert "test_report" in artifact_types
        assert "merge_result" in artifact_types
        assert receipt_count == 2

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "software_factory.pull_request_proposed" in event_types
        assert "software_factory.pull_request_merged" in event_types
        assert "task.completed" in event_types

        receipt_list = client.get(
            "/v1/receipts",
            params={"project_id": "boss-dogfood", "limit": 10},
        )
        assert receipt_list.status_code == 200
        receipt_payload = receipt_list.json()
        assert receipt_payload["project_id"] == "boss-dogfood"
        assert len(receipt_payload["items"]) == 2
        assert {item["effect_kind"] for item in receipt_payload["items"]} == {
            "github.pull_request.create",
            "github.pull_request.merge",
        }
        assert {
            item["created_by_job_id"] for item in receipt_payload["items"]
        } == {leased_pr_job["id"], leased_merge_job["id"]}

        audit_export = client.get(
            "/v1/audit/export",
            params={"project_id": "boss-dogfood", "task_id": task_id},
        )
        assert audit_export.status_code == 200
        export_payload = audit_export.json()
        assert export_payload["project_id"] == "boss-dogfood"
        assert export_payload["scope"]["task_id"] == task_id
        assert export_payload["counts"]["tasks"] == 1
        assert export_payload["counts"]["jobs"] == 3
        assert export_payload["counts"]["decisions"] == 2
        assert export_payload["counts"]["receipts"] == 2
        assert export_payload["counts"]["budget_reservations"] == 1
        assert export_payload["counts"]["routing_logs"] >= 1
        assert task_id in export_payload["chain"]["task_ids"]
        assert leased_pr_job["id"] in export_payload["chain"]["job_ids"]
        assert leased_merge_job["id"] in export_payload["chain"]["job_ids"]
        assert pr_decision_id in export_payload["chain"]["decision_ids"]

        replay_bundle = client.post(
            "/v1/audit/replay-bundles",
            json={
                "project_id": "boss-dogfood",
                "task_id": task_id,
                "actor": "ceo",
            },
        )
        assert replay_bundle.status_code == 201
        replay_payload = replay_bundle.json()
        assert replay_payload["status"] == "created"
        assert replay_payload["artifact_type"] == "replay_bundle"
        assert replay_payload["counts"]["receipts"] == 2

        replay_artifact = client.get(f"/v1/artifacts/{replay_payload['artifact_id']}")
        assert replay_artifact.status_code == 200
        replay_artifact_payload = replay_artifact.json()
        assert replay_artifact_payload["artifact_type"] == "replay_bundle"
        assert replay_artifact_payload["content"]["counts"]["jobs"] == 3
        assert leased_merge_job["id"] in replay_artifact_payload["content"]["chain"]["job_ids"]
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous
        api_main.software_factory.settings.openai_api_key = previous_openai


def test_command_to_deploy_flow_completes_task_when_deploy_policy_enabled(
    client, engine, monkeypatch
):
    previous_merge = api_main.decisions.settings.enable_sandbox_merge
    previous_merge_factory = api_main.software_factory.settings.enable_sandbox_merge
    previous_deploy = api_main.decisions.settings.enable_sandbox_deploy
    previous_deploy_factory = api_main.software_factory.settings.enable_sandbox_deploy
    previous_openai = api_main.software_factory.settings.openai_api_key
    previous_healthcheck_url = (
        api_main.software_factory.settings.sandbox_deploy_healthcheck_url
    )
    api_main.decisions.settings.enable_sandbox_merge = True
    api_main.software_factory.settings.enable_sandbox_merge = True
    api_main.decisions.settings.enable_sandbox_deploy = True
    api_main.software_factory.settings.enable_sandbox_deploy = True
    api_main.software_factory.settings.openai_api_key = "test-openai-key"
    api_main.software_factory.settings.sandbox_deploy_healthcheck_url = (
        "https://example.com/staging/777"
    )
    monkeypatch.setattr(
        api_main.software_factory.openai,
        "create_text_response",
        lambda prompt, system_prompt=None, model=None: (
            "resp_review_packet",
            "## Implementation summary\n- Review packet generated for deploy test.",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "ensure_branch",
        lambda branch_name: "sha-base",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "upsert_file",
        lambda branch_name, path, content, message: f"commit-{path}",
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "create_pull_request",
        lambda branch_name, title, body: PullRequestResult(
            url="https://github.com/seanheiney/boss-sandbox/pull/199",
            number=199,
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "wait_for_commit_checks",
        lambda head_sha, timeout_seconds, poll_interval_seconds: PullRequestValidationResult(
            status="passed",
            summary="GitHub validation passed: CI.",
            head_sha=head_sha,
            completed=True,
            checks=[
                PullRequestValidationCheck(
                    name="CI",
                    status="completed",
                    conclusion="success",
                    html_url="https://github.com/seanheiney/boss-sandbox/actions/runs/199",
                    started_at="2026-06-02T12:00:00Z",
                    completed_at="2026-06-02T12:01:00Z",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "merge_pull_request",
        lambda pull_request_number, commit_title: PullRequestMergeResult(
            sha="merge-sha-199",
            merged=True,
            message="Pull request successfully merged",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "create_deployment",
        lambda ref, environment, description: DeploymentResult(
            id=777,
            environment=environment,
            sha=ref,
            url="https://api.github.com/repos/seanheiney/boss-sandbox/deployments/777",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory.github,
        "mark_deployment_success",
        lambda deployment_id, environment, environment_url=None, description=None: DeploymentStatusResult(
            state="success",
            url="https://api.github.com/repos/seanheiney/boss-sandbox/deployments/777/statuses/1",
            environment_url=environment_url or "https://example.com/staging/777",
        ),
    )
    monkeypatch.setattr(
        api_main.software_factory,
        "_verify_deployment_health",
        lambda healthcheck_url: {
            "status": "passed",
            "healthcheck_url": healthcheck_url,
            "summary": "Deployment health check passed with HTTP 200.",
            "http_status": 200,
            "response_excerpt": "ok",
        },
    )

    try:
        created = client.post(
            "/v1/commands/propose",
            json={
                "project_id": "boss-dogfood",
                "command": "Ship the next sandbox deployment audit improvement.",
                "constraints": {"deadline": "this week"},
            },
        )
        assert created.status_code == 201
        payload = created.json()
        task_id = payload["task_id"]
        command_job_id = payload["job_id"]

        leased_plan = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-deploy-plan",
                "capabilities": ["plan.command"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_plan.status_code == 200
        leased_plan_job = leased_plan.json()["job"]
        assert leased_plan_job["id"] == command_job_id

        client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-deploy-plan",
                "job_id": command_job_id,
                "lease_token": leased_plan_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        ).raise_for_status()

        completed_plan = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": "worker-deploy-plan",
                "job_id": command_job_id,
                "lease_token": leased_plan_job["lease_token"],
                "result": {
                    "summary": "Ship the next sandbox deployment audit improvement.",
                    "objectives": ["Ship the next sandbox deployment audit improvement."],
                    "steps": [
                        "Prepare a reviewable proposal.",
                        "Review and merge the sandbox PR.",
                        "Approve and record the sandbox deployment.",
                    ],
                    "risks": ["Budget or provider credentials could block progress."],
                    "done_when": [
                        "The sandbox PR is merged, the sandbox deployment is recorded, and the task is completed."
                    ],
                },
            },
        )
        assert completed_plan.status_code == 200
        plan_decision_id = completed_plan.json()["decision_id"]

        assert (
            client.post(
                f"/v1/decisions/{plan_decision_id}/approve",
                json={"actor": "ceo", "note": "Proceed to PR proposal."},
            ).status_code
            == 200
        )

        leased_pr = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-deploy-pr",
                "capabilities": ["software_factory.propose_pr"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_pr.status_code == 200
        leased_pr_job = leased_pr.json()["job"]
        assert leased_pr_job["kind"] == "software_factory.propose_pr"

        client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-deploy-pr",
                "job_id": leased_pr_job["id"],
                "lease_token": leased_pr_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        ).raise_for_status()

        proposal_run = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": leased_pr_job["project_id"],
                "task_id": leased_pr_job["task_id"],
                "reservation_id": leased_pr_job["payload"]["reservation_id"],
                "dedupe_key": leased_pr_job["payload"]["dedupe_key"],
                "title": leased_pr_job["payload"]["title"],
                "branch_name": leased_pr_job["payload"]["branch_name"],
                "summary": leased_pr_job["payload"]["summary"],
                "files": leased_pr_job["payload"]["files"],
                "created_by_job_id": leased_pr_job["id"],
            },
        )
        assert proposal_run.status_code == 200
        proposal_result = proposal_run.json()
        assert proposal_result["status"] == "created"
        pr_decision_id = proposal_result["decision_id"]

        assert (
            client.post(
                "/v1/worker/complete",
                json={
                    "worker_id": "worker-deploy-pr",
                    "job_id": leased_pr_job["id"],
                    "lease_token": leased_pr_job["lease_token"],
                    "result": proposal_result,
                },
            ).status_code
            == 200
        )

        assert (
            client.post(
                f"/v1/decisions/{pr_decision_id}/approve",
                json={"actor": "ceo", "note": "Merge the approved sandbox PR."},
            ).status_code
            == 200
        )

        leased_merge = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-deploy-merge",
                "capabilities": ["github.pull_request.merge"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_merge.status_code == 200
        leased_merge_job = leased_merge.json()["job"]
        assert leased_merge_job["kind"] == "github.pull_request.merge"

        client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-deploy-merge",
                "job_id": leased_merge_job["id"],
                "lease_token": leased_merge_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        ).raise_for_status()

        merge_run = client.post(
            "/v1/software-factory/merges",
            json={
                "project_id": leased_merge_job["project_id"],
                "decision_id": leased_merge_job["payload"]["decision_id"],
                "dedupe_key": leased_merge_job["payload"]["dedupe_key"],
                "pull_request_number": leased_merge_job["payload"]["pull_request_number"],
                "pull_request_url": leased_merge_job["payload"]["pull_request_url"],
                "title": leased_merge_job["payload"]["title"],
                "branch_name": leased_merge_job["payload"].get("branch_name"),
                "created_by_job_id": leased_merge_job["id"],
            },
        )
        assert merge_run.status_code == 200
        merge_result = merge_run.json()
        assert merge_result["status"] == "created"
        assert merge_result["merged_sha"] == "merge-sha-199"
        deploy_decision_id = merge_result["deployment_decision_id"]

        complete_merge = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": "worker-deploy-merge",
                "job_id": leased_merge_job["id"],
                "lease_token": leased_merge_job["lease_token"],
                "result": merge_result,
            },
        )
        assert complete_merge.status_code == 200
        assert complete_merge.json()["status"] == "succeeded"
        assert complete_merge.json()["deployment_decision_id"] == deploy_decision_id

        with engine.begin() as connection:
            task_status_after_merge = connection.execute(
                text("SELECT status FROM tasks WHERE id = :task_id"),
                {"task_id": task_id},
            ).scalar_one()
            deploy_decision_status = connection.execute(
                text("SELECT status FROM decisions WHERE id = :decision_id"),
                {"decision_id": deploy_decision_id},
            ).scalar_one()

        assert task_status_after_merge == "open"
        assert deploy_decision_status == "pending"

        assert (
            client.post(
                f"/v1/decisions/{deploy_decision_id}/approve",
                json={"actor": "ceo", "note": "Record the sandbox deployment."},
            ).status_code
            == 200
        )

        leased_deploy = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": "worker-deploy-release",
                "capabilities": ["github.deployment.create"],
                "lease_ttl_seconds": 60,
            },
        )
        assert leased_deploy.status_code == 200
        leased_deploy_job = leased_deploy.json()["job"]
        assert leased_deploy_job["kind"] == "github.deployment.create"

        client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": "worker-deploy-release",
                "job_id": leased_deploy_job["id"],
                "lease_token": leased_deploy_job["lease_token"],
                "lease_ttl_seconds": 60,
            },
        ).raise_for_status()

        deploy_run = client.post(
            "/v1/software-factory/deployments",
            json={
                "project_id": leased_deploy_job["project_id"],
                "decision_id": leased_deploy_job["payload"]["decision_id"],
                "dedupe_key": leased_deploy_job["payload"]["dedupe_key"],
                "merged_sha": leased_deploy_job["payload"]["merged_sha"],
                "pull_request_url": leased_deploy_job["payload"]["pull_request_url"],
                "title": leased_deploy_job["payload"]["title"],
                "branch_name": leased_deploy_job["payload"].get("branch_name"),
                "environment": leased_deploy_job["payload"]["environment"],
                "created_by_job_id": leased_deploy_job["id"],
            },
        )
        assert deploy_run.status_code == 200
        deploy_result = deploy_run.json()
        assert deploy_result["status"] == "created"
        assert deploy_result["environment"] == "staging"
        assert deploy_result["deployment_status"] == "success"
        assert deploy_result["health_verification_status"] == "passed"
        assert deploy_result["healthcheck_url"] == "https://example.com/staging/777"
        assert deploy_result["rollout_status"] == "completed"
        assert deploy_result["rollout_stage"] == "release_readiness"

        complete_deploy = client.post(
            "/v1/worker/complete",
            json={
                "worker_id": "worker-deploy-release",
                "job_id": leased_deploy_job["id"],
                "lease_token": leased_deploy_job["lease_token"],
                "result": deploy_result,
            },
        )
        assert complete_deploy.status_code == 200
        assert complete_deploy.json()["status"] == "succeeded"
        assert complete_deploy.json()["rollout_id"] == deploy_result["rollout_id"]

        with engine.begin() as connection:
            task_status = connection.execute(
                text("SELECT status FROM tasks WHERE id = :task_id"),
                {"task_id": task_id},
            ).scalar_one()
            review_decision_status = connection.execute(
                text("SELECT status FROM decisions WHERE id = :decision_id"),
                {"decision_id": pr_decision_id},
            ).scalar_one()
            deployment_decision_status = connection.execute(
                text("SELECT status FROM decisions WHERE id = :decision_id"),
                {"decision_id": deploy_decision_id},
            ).scalar_one()
            artifact_types = [
                row["artifact_type"]
                for row in connection.execute(
                    text(
                        """
                        SELECT artifact_type
                        FROM artifacts
                        WHERE project_id = 'boss-dogfood'
                        ORDER BY created_at ASC
                        """
                    )
                ).mappings().all()
            ]
            receipt_kinds = [
                row["effect_kind"]
                for row in connection.execute(
                    text(
                        """
                        SELECT effect_kind
                        FROM receipts
                        WHERE project_id = 'boss-dogfood'
                        ORDER BY created_at ASC
                        """
                    )
                ).mappings().all()
            ]
            rollout = connection.execute(
                text(
                    """
                    SELECT status, current_stage, health_verification_status, stages
                    FROM deployment_rollouts
                    WHERE id = :rollout_id
                    """
                ),
                {"rollout_id": deploy_result["rollout_id"]},
            ).mappings().one()

        assert task_status == "completed"
        assert review_decision_status == "executed"
        assert deployment_decision_status == "executed"
        assert "deployment_result" in artifact_types
        assert "deployment_rollout" in artifact_types
        assert rollout["status"] == "completed"
        assert rollout["current_stage"] == "release_readiness"
        assert rollout["health_verification_status"] == "passed"
        assert rollout["stages"][2]["status"] == "passed"
        assert receipt_kinds == [
            "github.pull_request.create",
            "github.pull_request.merge",
            "github.deployment.create",
        ]

        rollouts = client.get(
            "/v1/software-factory/rollouts",
            params={"project_id": "boss-dogfood"},
        )
        assert rollouts.status_code == 200
        assert rollouts.json()["items"][0]["id"] == deploy_result["rollout_id"]

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "software_factory.deployment_review_requested" in event_types
        assert "software_factory.deployment_created" in event_types
        assert "software_factory.deployment_rollout_recorded" in event_types
        assert "task.completed" in event_types
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous_merge
        api_main.software_factory.settings.enable_sandbox_merge = previous_merge_factory
        api_main.decisions.settings.enable_sandbox_deploy = previous_deploy
        api_main.software_factory.settings.enable_sandbox_deploy = previous_deploy_factory
        api_main.software_factory.settings.sandbox_deploy_healthcheck_url = (
            previous_healthcheck_url
        )
        api_main.software_factory.settings.openai_api_key = previous_openai
