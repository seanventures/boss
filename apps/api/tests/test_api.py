import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.services.github_adapter import (
    DeploymentResult,
    DeploymentStatusResult,
    PullRequestMergeResult,
    PullRequestResult,
)


def test_health_returns_service_status(client):
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database_connected"] is True


def test_status_returns_seeded_project(client):
    response = client.get("/v1/status", params={"project_id": "boss-dogfood"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == "boss-dogfood"
    assert payload["project_name"] == "BOSS dogfood bootstrap"
    assert "deadletters" in payload
    assert "active_jobs" in payload
    assert "top_risks" in payload


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
    assert payload["spec"]["mission"].startswith("Omnipresent CEO interface")
    assert "WhatChangedDigest" in payload["spec"]["output_contracts"]


def test_pack_install_is_idempotent_for_new_project(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM events WHERE project_id = 'boss-lab'"))
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


def test_skill_registry_and_seeded_entitlement_sources_are_visible(client):
    skills = client.get("/v1/skills")
    assert skills.status_code == 200
    skill_ids = [item["id"] for item in skills.json()["items"]]
    assert "model.route_via_entitlement_broker" in skill_ids
    assert "model.generate_text" in skill_ids

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


def test_router_preview_selects_openai_api_and_writes_log(client):
    previous = api_main.software_factory.settings.openai_api_key
    api_main.software_factory.settings.openai_api_key = "test-openai-key"
    try:
        preview = client.post(
            "/v1/router/preview",
            json={
                "project_id": "boss-dogfood",
                "skill_id": "model.generate_text",
                "task_kind": "planning",
                "workload_class": "owner_sandbox",
                "risk_tier": 1,
                "preferred_capabilities": ["planning", "summarization"],
                "actor": "ceo",
            },
        )
    finally:
        api_main.software_factory.settings.openai_api_key = previous

    assert preview.status_code == 200
    payload = preview.json()
    assert payload["source_id"] == "openai_api"
    assert payload["provider"] == "openai"
    assert payload["model_id"].startswith("openai:")
    assert payload["denial_reason"] is None

    logs = client.get("/v1/router/logs", params={"project_id": "boss-dogfood"})
    assert logs.status_code == 200
    log_ids = [item["id"] for item in logs.json()["items"]]
    assert payload["route_decision_log_id"] in log_ids


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
    assert payload["active_reservations_count"] == 0
    assert payload["top_cost_drivers"] == []


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


def test_approving_pr_review_decision_queues_merge_job_when_policy_enabled(client, engine):
    previous = api_main.decisions.settings.enable_sandbox_merge
    api_main.decisions.settings.enable_sandbox_merge = True
    try:
        with engine.begin() as connection:
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
    api_main.decisions.settings.enable_sandbox_merge = True
    api_main.software_factory.settings.enable_sandbox_merge = True
    api_main.decisions.settings.enable_sandbox_deploy = True
    api_main.software_factory.settings.enable_sandbox_deploy = True
    api_main.software_factory.settings.openai_api_key = "test-openai-key"
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
            environment_url="https://example.com/staging/777",
        ),
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

        assert task_status == "completed"
        assert review_decision_status == "executed"
        assert deployment_decision_status == "executed"
        assert "deployment_result" in artifact_types
        assert receipt_kinds == [
            "github.pull_request.create",
            "github.pull_request.merge",
            "github.deployment.create",
        ]

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "software_factory.deployment_review_requested" in event_types
        assert "software_factory.deployment_created" in event_types
        assert "task.completed" in event_types
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous_merge
        api_main.software_factory.settings.enable_sandbox_merge = previous_merge_factory
        api_main.decisions.settings.enable_sandbox_deploy = previous_deploy
        api_main.software_factory.settings.enable_sandbox_deploy = previous_deploy_factory
        api_main.software_factory.settings.openai_api_key = previous_openai
