"""First-class DLQ remediations + fail-closed retry (BOSS-342 residual / A5)."""

from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.services.deadletters import (
    classify_deadletter_error,
    is_model_capacity_exhaustion,
)


PROJECT_ID = "boss-dogfood"


def _insert_deadletter(engine, *, dlq_id: str, reason: str, source_kind: str = "job",
                       source_id: str | None = None, payload: dict | None = None):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (:id, :project_id, :source_kind, :source_id, :reason, CAST(:payload AS JSONB))
                """
            ),
            {
                "id": dlq_id,
                "project_id": PROJECT_ID,
                "source_kind": source_kind,
                "source_id": source_id or f"src_{dlq_id}",
                "reason": reason,
                "payload": json.dumps(payload or {}),
            },
        )


def _deadletter_a_job(client, *, kind: str = "plan.generate", worker: str = "worker-dlq-rem"):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": PROJECT_ID,
            "kind": kind,
            "payload": {"command": "Remediation retry fixture."},
            "max_attempts": 1,
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": worker,
            "capabilities": [kind],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": worker,
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    fail = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": worker,
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "error": {"message": "terminal failure for remediation e2e"},
            "retryable": False,
            "retry_delay_seconds": 0,
        },
    )
    assert fail.status_code == 200, fail.text
    return job_id, fail.json()["deadletter_id"]


def test_credit_vs_credential_classifier_api_export():
    assert classify_deadletter_error("OpenRouter credits exhausted") == "model"
    assert is_model_capacity_exhaustion("OpenRouter credits exhausted")
    assert classify_deadletter_error("bad credentials") == "github"
    assert not is_model_capacity_exhaustion("bad credentials")
    assert (
        classify_deadletter_error(
            "Missing credential for SecretRef env:BOSS_DIGITALOCEAN_TOKEN."
        )
        == "other"
    )


def test_sandbox_reason_codes_already_exist_as_sandbox_class():
    assert classify_deadletter_error("microvm_claims_frozen") == "sandbox"
    assert classify_deadletter_error("sandbox_task_missing") == "sandbox"


def test_list_remediations_empty_then_hold_is_visible(client, engine):
    empty = client.get(
        "/v1/deadletters/remediations",
        params={"project_id": PROJECT_ID, "limit": 20},
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["items"] == []
    assert set(empty.json()["actions"]) == {"retry", "requeue", "escalate", "hold"}

    _insert_deadletter(
        engine, dlq_id="dlq_hold_vis", reason="operator hold fixture"
    )
    created = client.post(
        "/v1/deadletters/dlq_hold_vis/remediations",
        json={"actor": "ceo", "action": "hold", "reason": "waiting_on_creds"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "applied"
    assert body["remediation"]["action"] == "hold"
    assert body["remediation"]["result"]["held"] is True

    listed = client.get(
        "/v1/deadletters/remediations",
        params={"project_id": PROJECT_ID, "deadletter_id": "dlq_hold_vis"},
    )
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == body["remediation"]["id"]
    assert items[0]["status"] == "applied"

    with engine.begin() as connection:
        dlq_status = connection.execute(
            text("SELECT status FROM deadletters WHERE id = 'dlq_hold_vis'")
        ).scalar_one()
        events = connection.execute(
            text(
                """
                SELECT event_type FROM events
                WHERE entity_id = :id OR data->>'deadletter_id' = 'dlq_hold_vis'
                """
            ),
            {"id": body["remediation"]["id"]},
        ).scalars().all()
    assert dlq_status == "open"
    assert "deadletter.remediation_created" in events
    assert "deadletter.remediation_applied" in events


def test_retry_requeues_job_and_sets_visible_state(client, engine):
    job_id, deadletter_id = _deadletter_a_job(client, worker="worker-dlq-retry")
    retry = client.post(
        f"/v1/deadletters/{deadletter_id}/retry",
        json={"actor": "ceo", "reason": "root_cause_fixed"},
    )
    assert retry.status_code == 200, retry.text
    payload = retry.json()
    assert payload["status"] == "applied"
    requeued_job_id = payload["requeued_job_id"]
    assert requeued_job_id
    assert payload["deadletter_status"] == "requeued"

    listed = client.get(
        "/v1/deadletters/remediations",
        params={"project_id": PROJECT_ID, "deadletter_id": deadletter_id},
    )
    assert listed.status_code == 200
    rem = listed.json()["items"][0]
    assert rem["status"] == "applied"
    assert rem["action"] == "retry"
    assert rem["requeued_job_id"] == requeued_job_id

    with engine.begin() as connection:
        job = connection.execute(
            text("SELECT status, kind, payload, attempt FROM jobs WHERE id = :id"),
            {"id": requeued_job_id},
        ).mappings().one()
        dlq = connection.execute(
            text("SELECT status, requeued_job_id FROM deadletters WHERE id = :id"),
            {"id": deadletter_id},
        ).mappings().one()
    assert job["status"] == "queued"
    assert job["kind"] == "plan.generate"
    assert job["attempt"] == 0
    assert job["payload"]["requeue"]["source_job_id"] == job_id
    assert job["payload"]["requeue"]["source_deadletter_id"] == deadletter_id
    assert dlq["status"] == "requeued"
    assert dlq["requeued_job_id"] == requeued_job_id

    again = client.post(
        f"/v1/deadletters/{deadletter_id}/retry",
        json={"actor": "ceo", "reason": "double_click"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["requeued_job_id"] == requeued_job_id
    assert again.json()["remediation"]["result"]["already_requeued"] is True


def test_retry_non_job_is_blocked_not_silent(client, engine):
    _insert_deadletter(
        engine,
        dlq_id="dlq_provider_sync",
        reason="provider credentials missing",
        source_kind="provider_sync",
        source_id="sync_do_1",
    )
    retry = client.post(
        "/v1/deadletters/dlq_provider_sync/retry",
        json={"actor": "ceo"},
    )
    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert body["status"] == "blocked"
    assert body["requeued_job_id"] is None
    assert "only job deadletters" in (body["remediation"]["error"] or "")

    listed = client.get(
        "/v1/deadletters/remediations",
        params={"project_id": PROJECT_ID, "deadletter_id": "dlq_provider_sync"},
    )
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "blocked"

    with engine.begin() as connection:
        dlq_status = connection.execute(
            text("SELECT status FROM deadletters WHERE id = 'dlq_provider_sync'")
        ).scalar_one()
        events = connection.execute(
            text(
                """
                SELECT event_type FROM events
                WHERE event_type IN (
                    'deadletter.remediation_created',
                    'deadletter.remediation_blocked'
                )
                AND (entity_id = :id OR data->>'deadletter_id' = 'dlq_provider_sync')
                """
            ),
            {"id": items[0]["id"]},
        ).scalars().all()
    assert dlq_status == "open"
    assert "deadletter.remediation_created" in events
    assert "deadletter.remediation_blocked" in events


def test_retry_missing_deadletter_is_404(client):
    response = client.post(
        "/v1/deadletters/dlq_does_not_exist/retry",
        json={"actor": "ceo"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "deadletter not found"


def test_unknown_remediation_action_is_400(client, engine):
    _insert_deadletter(engine, dlq_id="dlq_bad_action", reason="unknown action fixture")
    response = client.post(
        "/v1/deadletters/dlq_bad_action/remediations",
        json={"actor": "ceo", "action": "drop_silently"},
    )
    assert response.status_code == 400
    assert "action must be one of" in response.json()["detail"]
    listed = client.get(
        "/v1/deadletters/remediations",
        params={"project_id": PROJECT_ID, "deadletter_id": "dlq_bad_action"},
    )
    assert listed.json()["items"] == []
