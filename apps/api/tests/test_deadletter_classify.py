"""API + DB coverage for POST /v1/deadletters/classify (P0-2)."""

from __future__ import annotations

from sqlalchemy import text

from apps.api.app.services.deadletters import (
    classify_deadletter_error,
    is_model_capacity_exhaustion,
)


PROJECT_ID = "boss-dogfood"


def _insert_deadletter(engine, *, dlq_id: str, reason: str, payload: dict | None = None):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (:id, :project_id, 'job', :source_id, :reason, CAST(:payload AS JSONB))
                """
            ),
            {
                "id": dlq_id,
                "project_id": PROJECT_ID,
                "source_id": f"job_for_{dlq_id}",
                "reason": reason,
                "payload": __import__("json").dumps(payload or {}),
            },
        )


def test_classify_endpoint_buckets_open_deadletters(client, engine):
    _insert_deadletter(
        engine, dlq_id="dlq_cls_git", reason="No such file or directory: 'git'"
    )
    _insert_deadletter(
        engine,
        dlq_id="dlq_cls_model",
        reason="rate limit: TPM exceeded for groq",
        payload={"message": "429 too many requests"},
    )
    _insert_deadletter(
        engine, dlq_id="dlq_cls_other", reason="unexpected terminal failure"
    )

    resp = client.post(
        "/v1/deadletters/classify",
        json={
            "project_id": PROJECT_ID,
            "limit": 50,
            "escalate_model": False,
            "actor": "ceo",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project_id"] == PROJECT_ID
    assert body["total_open"] >= 3
    assert set(body["error_classes"]) == {"git", "sandbox", "model", "github", "other"}
    assert body["by_class"]["git"] >= 1
    assert body["by_class"]["model"] >= 1
    assert body["by_class"]["other"] >= 1

    by_id = {item["id"]: item for item in body["items"]}
    assert by_id["dlq_cls_git"]["error_class"] == "git"
    assert by_id["dlq_cls_model"]["error_class"] == "model"
    assert by_id["dlq_cls_other"]["error_class"] == "other"
    assert body["escalations"] == []


def test_classify_endpoint_escalates_model_capacity_to_decision(client, engine):
    _insert_deadletter(
        engine,
        dlq_id="dlq_cls_tpm",
        reason="model TPM rate limit exceeded",
        payload={"type": "RateLimitError"},
    )

    resp = client.post(
        "/v1/deadletters/classify",
        json={
            "project_id": PROJECT_ID,
            "limit": 50,
            "escalate_model": True,
            "actor": "ceo",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    item = next(i for i in body["items"] if i["id"] == "dlq_cls_tpm")
    assert item["error_class"] == "model"
    assert item["escalated_to_decision"] is True
    assert item["remediation_decision_id"]
    assert any(e["deadletter_id"] == "dlq_cls_tpm" for e in body["escalations"])

    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT remediation_decision_id, status
                FROM deadletters WHERE id = :id
                """
            ),
            {"id": "dlq_cls_tpm"},
        ).mappings().one()
        assert row["remediation_decision_id"] == item["remediation_decision_id"]
        decision = connection.execute(
            text("SELECT id, question, status FROM decisions WHERE id = :id"),
            {"id": row["remediation_decision_id"]},
        ).mappings().first()
        assert decision is not None
        assert decision["status"] == "pending"
        assert "capacity" in (decision.get("question") or "").lower()

    # Idempotent: second classify does not create another decision.
    again = client.post(
        "/v1/deadletters/classify",
        json={
            "project_id": PROJECT_ID,
            "limit": 50,
            "escalate_model": True,
            "actor": "ceo",
        },
    )
    assert again.status_code == 200
    item2 = next(i for i in again.json()["items"] if i["id"] == "dlq_cls_tpm")
    assert item2["remediation_decision_id"] == item["remediation_decision_id"]
    assert item2["escalated_to_decision"] is False


def test_pure_helpers_reexported_via_api_services():
    assert classify_deadletter_error("github pull request 403") == "github"
    assert is_model_capacity_exhaustion("credits exhausted")
    # credits ≠ credentials (already-fixed classifier)
    assert classify_deadletter_error("bad credentials") == "github"
    assert not is_model_capacity_exhaustion("bad credentials")
    assert (
        classify_deadletter_error(
            "Missing credential for SecretRef env:BOSS_DIGITALOCEAN_TOKEN."
        )
        == "other"
    )
    # existing fail-closed sandbox classes
    assert classify_deadletter_error("microvm_claims_frozen") == "sandbox"
    assert classify_deadletter_error("sandbox_task_missing") == "sandbox"
