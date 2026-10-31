"""KG P6b: assigning a connected external agent to a workflow step in the console.

P6b's HTTP API over registration (test_kg_p6b_external_agents_api.py) and P6a's engine-level
step execution (engine/tests/test_kg_p6a_external_agent_step.py) are both covered. What is not:
the console-facing path in between — Check/Publish on ``/v1/delivery-workflows`` when a step's
captain names a connected external agent. These tests close that gap: a registered, active
agent assigned to a stage checks clean; an unregistered or suspended one is refused with the
same wording either way (Risk #4 — never tell an outsider which case it is).
"""
from __future__ import annotations

import secrets
from typing import Any

from boss_engine.config import get_settings as get_engine_settings
from boss_engine.db import begin, get_engine
from boss_engine.services.secrets_service import put_secret

PROJECT = "boss-dogfood"
ACTOR = "ceo"

CONTRACT = {
    "reads": [{"kind": "rule"}],
    "writes": [{"kind": "agent_decision", "required": True}],
}


def _make_secret_ref() -> str:
    settings = get_engine_settings()
    with begin(get_engine()) as connection:
        stored = put_secret(
            connection,
            settings,
            project_id=PROJECT,
            name=f"outside-agent-{secrets.token_hex(4)}",
            value=secrets.token_urlsafe(24),
            secret_kind="api_token",
            backend="database",
            actor=ACTOR,
        )
    return stored["secret_ref_id"]


def _register(client, *, name: str = "sample-bank-agent") -> str:
    created = client.post(
        "/v1/external-agents",
        json={
            "project_id": PROJECT,
            "display_name": name,
            "endpoint_url": f"http://localhost:8765/{secrets.token_hex(4)}",
            "secret_ref_id": _make_secret_ref(),
            "sponsor_principal_id": ACTOR,
            "actor": ACTOR,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _spec(agent_id: str) -> dict:
    return {"entry_step": "bank_review", "steps": [{
        "id": "bank_review",
        "kind": "external_agent",
        "captain": {"external_agent": agent_id},
        "contract": CONTRACT,
        "next": [],
    }]}


def _check(client, spec: dict) -> dict:
    response = client.post(
        "/v1/delivery-workflows/check",
        json={"project_id": PROJECT, "actor": ACTOR, "spec": spec},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_connected_active_agent_assigned_to_a_stage_checks_clean(client):
    agent_id = _register(client)

    result = _check(client, _spec(agent_id))

    assert result["problems"] == [], result["problems"]


def test_an_unregistered_agent_id_is_refused_naming_the_stage(client):
    result = _check(client, _spec("ext_does_not_exist"))

    assert result["problems"], "expected a binding problem naming the unknown agent"
    step_ids = {p.get("step_id") for p in result["problems"]}
    message = " ".join(p.get("message", "") for p in result["problems"])
    assert "bank_review" in step_ids
    assert "does not hold" in message


def test_a_suspended_agent_is_refused_the_same_way_as_unregistered(client):
    agent_id = _register(client, name="suspended-bank-agent")
    suspended = client.post(
        f"/v1/external-agents/{agent_id}/suspend",
        json={"project_id": PROJECT, "actor": ACTOR},
    )
    assert suspended.status_code == 200, suspended.text

    unregistered = _check(client, _spec("ext_does_not_exist"))
    suspended_result = _check(client, _spec(agent_id))

    unregistered_message = next(p["message"] for p in unregistered["problems"] if p.get("field") == "captain")
    suspended_message = next(p["message"] for p in suspended_result["problems"] if p.get("field") == "captain")
    assert unregistered_message.replace("ext_does_not_exist", agent_id) == suspended_message


def test_an_external_agent_step_without_a_contract_is_refused_before_binding_is_checked(client):
    agent_id = _register(client, name="shape-check-agent")
    spec = _spec(agent_id)
    del spec["steps"][0]["contract"]

    result = _check(client, spec)

    assert result["problems"], "expected a shape problem for the missing contract"
    message = " ".join(p.get("message", "") for p in result["problems"])
    assert "contract" in message


def _publish(client, spec: dict, *, name: str) -> Any:
    return client.post(
        "/v1/delivery-workflows/publish",
        json={"project_id": PROJECT, "actor": ACTOR, "name": name, "spec": spec},
    )


def test_a_connected_active_agent_assigned_to_a_stage_publishes_clean(client):
    agent_id = _register(client, name="publish-active-agent")

    response = _publish(client, _spec(agent_id), name=f"bank-review-{secrets.token_hex(4)}")

    assert response.status_code == 200, response.text


def test_publish_refuses_an_unregistered_agent_naming_the_stage(client):
    response = _publish(
        client, _spec("ext_does_not_exist"), name=f"bank-review-{secrets.token_hex(4)}")

    assert response.status_code != 200, response.text
    problems = response.json()["problems"]
    step_ids = {p.get("step_id") for p in problems}
    message = " ".join(p.get("message", "") for p in problems)
    assert "bank_review" in step_ids
    assert "does not hold" in message


def test_publish_refuses_a_suspended_agent_the_same_way_as_unregistered(client):
    agent_id = _register(client, name="publish-suspended-agent")
    suspended = client.post(
        f"/v1/external-agents/{agent_id}/suspend",
        json={"project_id": PROJECT, "actor": ACTOR},
    )
    assert suspended.status_code == 200, suspended.text

    unregistered = _publish(
        client, _spec("ext_does_not_exist"), name=f"bank-review-{secrets.token_hex(4)}")
    suspended_result = _publish(
        client, _spec(agent_id), name=f"bank-review-{secrets.token_hex(4)}")

    assert unregistered.status_code != 200, unregistered.text
    assert suspended_result.status_code != 200, suspended_result.text
    unregistered_message = next(
        p["message"] for p in unregistered.json()["problems"] if p.get("field") == "captain")
    suspended_message = next(
        p["message"] for p in suspended_result.json()["problems"] if p.get("field") == "captain")
    assert unregistered_message.replace("ext_does_not_exist", agent_id) == suspended_message
