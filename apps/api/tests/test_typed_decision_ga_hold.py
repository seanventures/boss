"""BOSS-451: model spend stays closed until workflow decisions are durable."""

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


def test_typesafe_setup_api_exposes_hold_without_provider_call_or_key_storage(client, monkeypatch):
    monkeypatch.setattr(typed_decisions.TypeSafeDecider, "decide", lambda *a, **k: pytest.fail(
        "TypeSafe was called before BOSS reserved the model spend"))
    monkeypatch.setattr("boss_engine.services.secrets_service.put_secret", lambda *a, **k:
                        pytest.fail("TypeSafe key was stored while live proof is held"))
    response = client.post("/v1/setup/decision-model/key", json={
        "project_id": PROJECT, "actor": "ceo", "api_key": "test-only-key",
    })
    assert response.status_code == 503, response.text
    assert "BOSS-451" in response.json()["detail"]
    status = client.get("/v1/setup/decision-model", params={"project_id": PROJECT})
    assert status.status_code == 200, status.text
    assert status.json()["effective_backend"] == "paused"
    assert status.json()["execution_available"] is False


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
