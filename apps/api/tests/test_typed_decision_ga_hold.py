"""BOSS-451: every typed-decision model call — workflow step or key proof — is reserved,
leased and receipted before it is made, and settled after."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import typed_decisions, workflow_capability_runner, workflows


PROJECT = "boss-dogfood"


def _spec() -> dict:
    return {
        "entry_step": "route",
        "steps": [{
            "id": "route", "kind": "capability",
            "binding": {
                "typed_decision": {
                    "question": "Which team handles this ticket?",
                    "options": {"billing": "Payments", "technical": "Bugs"},
                },
                "inputs": {"state": {"from_input": "ticket"}},
            },
        }],
    }


def _definition(connection) -> dict:
    key = f"typed_hold_{uuid4().hex[:12]}"
    return workflows.create_definition(
        connection, project_id=PROJECT, workflow_key=key,
        name="Typed decision hold", description=None, actor="ceo",
        namespace=workflows.PRODUCT_NAMESPACE,
    )


def test_new_typed_decision_versions_publish_onto_the_governed_path(engine):
    with engine.begin() as connection:
        definition = _definition(connection)
        version = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition["id"],
            spec=_spec(), actor="ceo",
        )
        count = connection.execute(text(
            "SELECT count(*) FROM workflow_versions WHERE workflow_definition_id=:id"
        ), {"id": definition["id"]}).scalar_one()
    assert version["id"]
    assert count == 1


def _capacity(client, suffix: str) -> None:
    """Give the gateway a source to lease, as a real install has once a model is connected."""
    created = client.post("/v1/secret-refs", json={
        "project_id": PROJECT, "ref_kind": "env", "ref_key": f"BOSS_TEST_DECIDE_{suffix}",
        "provider": "openai", "secret_kind": "api_token", "status": "active", "actor": "ceo",
    })
    assert created.status_code == 201, created.text
    bound = client.post("/v1/entitlements/sources/openai_api/credential-ref", json={
        "project_id": PROJECT, "secret_ref_id": created.json()["id"], "actor": "ceo",
    })
    assert bound.status_code == 200, bound.text


def _committed_spend(engine, project: str) -> dict:
    """What another connection can see: only what the spend path has COMMITTED."""
    with engine.connect() as connection:
        receipts = connection.execute(text(
            "SELECT status FROM receipts WHERE dedupe_key LIKE :prefix "
            "ORDER BY created_at DESC LIMIT 1"),
            {"prefix": f"model-gateway:{project}:typed_decision:{project}:setup:typesafe-key:%"},
        ).scalar_one_or_none()
        reservations = connection.execute(text(
            "SELECT count(*) FROM budget_reservations WHERE project_id = :p"), {"p": project},
        ).scalar_one()
    return {"receipt": receipts, "reservations": reservations}


def test_typesafe_key_proof_is_reserved_and_receipted_before_the_call(client, engine, monkeypatch):
    """BOSS-451 step 5: the proof is a billed call, so the reservation and an in-progress
    receipt must be committed — visible to another connection — when TypeSafe is called."""
    _capacity(client, "PROOF")
    seen = {}
    before = _committed_spend(engine, PROJECT)["reservations"]

    def decide(self, context, questions):
        seen["at_call"] = _committed_spend(engine, PROJECT)
        return typed_decisions.DecisionBatch(
            decisions={"decision": typed_decisions.Decision("yes", 0.99)},
            backend="typesafe", model="jev-test", latency_ms=42, cost_usd_micros=7)

    stored = {}
    monkeypatch.setattr(typed_decisions.TypeSafeDecider, "decide", decide)
    monkeypatch.setattr("boss_engine.services.secrets_service.put_secret",
                        lambda *a, **k: stored.update(k) or {
                            "secret_ref_id": "sref_test", "backend": k.get("backend"),
                            "fingerprint": "fp"})
    response = client.post("/v1/setup/decision-model/key", json={
        "project_id": PROJECT, "actor": "ceo", "api_key": "test-only-key",
        "accept_database_fallback": True,
    })
    assert response.status_code == 201, response.text
    assert seen["at_call"]["receipt"] == "in_progress", seen
    assert seen["at_call"]["reservations"] == before + 1, seen
    assert _committed_spend(engine, PROJECT)["receipt"] == "succeeded"
    body = response.json()
    assert body["receipt_id"] and body["cost_usd_micros"] == 7
    assert stored["value"] == "test-only-key" and "test-only-key" not in response.text
    status = client.get("/v1/setup/decision-model", params={"project_id": PROJECT}).json()
    assert status["execution_available"] is True and status["hold_reason"] == ""


def test_a_failed_key_write_after_the_proof_keeps_the_receipt(client, engine, monkeypatch):
    """The original gap: the proof ran inside the request transaction, so a failed key write
    rolled back every trace of a call TypeSafe had already billed."""
    _capacity(client, "BROKEN")
    monkeypatch.setattr(typed_decisions.TypeSafeDecider, "decide", lambda self, c, q:
                        typed_decisions.DecisionBatch(
                            decisions={"decision": typed_decisions.Decision("yes", 0.99)},
                            backend="typesafe", model="jev-test", latency_ms=5,
                            cost_usd_micros=3))

    def broken_store(*a, **k):
        raise RuntimeError("secret store unavailable")

    monkeypatch.setattr("boss_engine.services.secrets_service.put_secret", broken_store)
    with pytest.raises(RuntimeError, match="secret store unavailable"):
        client.post("/v1/setup/decision-model/key", json={
            "project_id": PROJECT, "actor": "ceo", "api_key": "test-only-key",
            "accept_database_fallback": True,
        })
    assert _committed_spend(engine, PROJECT)["receipt"] == "succeeded", (
        "the billed proof must stay on record even though the key was not kept")


def test_existing_running_step_deadletters_once_with_event_before_model_call(
    engine, monkeypatch,
):
    monkeypatch.setattr(typed_decisions, "make_decider", lambda settings: pytest.fail(
        "Model backend was constructed while BOSS-451 is held"))
    # Simulate a version and run created before this release hold. The publish
    # and start gates now reject new work, but in-flight runs still need a
    # durable, visible terminal outcome when the worker ticks them.
    with monkeypatch.context() as old_version:
        old_version.setattr(workflows, "_hold_new_typed_decision_work", lambda spec: None)
        with engine.begin() as connection:
            definition = _definition(connection)
            version = workflows.publish_version(
                connection, project_id=PROJECT, definition_id=definition["id"],
                spec=_spec(), actor="ceo",
            )
            run = workflows.start_run(
                connection, project_id=PROJECT, actor="ceo",
                input_data={"ticket": "My card was charged twice"},
                dedupe_key=f"typed-hold:{uuid4().hex}",
                workflow_version_id=version["id"],
            )

    with engine.begin() as connection:
        outcome = workflow_capability_runner.tick(
            connection, SimpleNamespace(settings=None), project_id=PROJECT,
            worker_id="typed-hold-test",
        )
    assert any(item.get("outcome") == "failed" and "BOSS-451" in item.get("error", "")
               for item in outcome["performed"])
    with engine.begin() as connection:
        step = connection.execute(text(
            "SELECT status, attempt, last_error FROM workflow_step_runs "
            "WHERE workflow_run_id=:run_id AND step_id='route'"
        ), {"run_id": run["id"]}).mappings().one()
        state = connection.execute(text(
            "SELECT status, error FROM workflow_runs WHERE id=:run_id"
        ), {"run_id": run["id"]}).mappings().one()
        events = connection.execute(text(
            "SELECT event_type, data FROM events "
            "WHERE entity_id=:run_id AND event_type='workflow.step_deadlettered'"
        ), {"run_id": run["id"]}).mappings().all()
    assert step["status"] == "deadlettered" and step["attempt"] == 1
    assert "BOSS-451" in step["last_error"]
    assert state["status"] == "deadlettered" and "BOSS-451" in state["error"]
    assert len(events) == 1 and "BOSS-451" in events[0]["data"]["error"]

    with engine.begin() as connection:
        replay = workflow_capability_runner.tick(
            connection, SimpleNamespace(settings=None), project_id=PROJECT,
            worker_id="typed-hold-test",
        )
    assert not any(item.get("run_id") == run["id"] for item in replay["performed"])
