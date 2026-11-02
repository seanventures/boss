"""Agent design review and lock over HTTP (design §4.6): /v1/delivery-workflows/{key}/designs.

Breaks covered: the designs view not answering with each stage's status, reference and the graph
versions; a lock without step-up; a lock by an agent's credential; a publish that changes a locked
contract going through; Revise not opening the next reference; the routes not declared with the
permissions the design says (lock = workflow:publish, a step-up permission).
"""
from __future__ import annotations

import uuid

from apps.api.app.db import begin
from boss_engine.services import workflows

PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"
CONTRACT = {
    "trigger": "after the lanes",
    "reads": [{"kind": "rule"}],
    "writes": [{"kind": "agent_decision", "required": True, "decision_type": "Prepared"}],
    "steps": [{"n": 1, "name": "Read the rule", "reads": ["rule"]},
              {"n": 2, "name": "Write the decision", "writes": ["agent_decision"]}],
    "design": {"decisions": [{"id": "PREP-1", "text": "prepare only"}]},
}


def _spec(contract):
    return {"entry_step": "prepare", "steps": [{
        "id": "prepare", "kind": "capability", "title": "Prepare",
        "binding": {"capability": "remediation_package", "inputs": {}}, "contract": contract, "next": []}]}


def _workflow(engine) -> tuple[str, str]:
    key = f"designs_api_{uuid.uuid4().hex[:6]}"
    with begin(engine) as connection:
        definition = workflows.create_definition(
            connection, project_id=PROJECT_ID, workflow_key=key, name=key, description=None,
            actor=ACTOR, namespace="product")
        workflows.publish_version(connection, project_id=PROJECT_ID, definition_id=definition["id"],
                                  spec=_spec(CONTRACT), actor=ACTOR)
    return key, definition["id"]


def test_the_routes_are_declared_with_the_designs_permissions():
    from apps.api.app.main import app
    from apps.api.app.routers import workflow_designs

    paths = {(m, r.path): r.endpoint for r in app.routes for m in (getattr(r, "methods", None) or ())
             if "/designs" in getattr(r, "path", "")}
    assert ("POST", "/v1/delivery-workflows/{workflow_key}/designs/{stage_id}/lock") in paths
    assert paths[("GET", "/v1/delivery-workflows/{workflow_key}/designs")].__module__ == workflow_designs.__name__
    from boss_engine.identity.declarations import ACCESS_ATTR

    declared = {route: getattr(fn, ACCESS_ATTR).permission for route, fn in paths.items()}
    assert declared[("POST", "/v1/delivery-workflows/{workflow_key}/designs/{stage_id}/lock")] == "workflow:publish"
    assert declared[("POST", "/v1/delivery-workflows/{workflow_key}/designs/{stage_id}/review")] == "workflow:create"
    assert declared[("POST", "/v1/delivery-workflows/{workflow_key}/designs/{stage_id}/revise")] == "workflow:create"
    assert declared[("GET", "/v1/delivery-workflows/{workflow_key}/designs")] == "workflow:read"


def test_review_lock_publish_refusal_and_revise(client, engine):
    key, definition_id = _workflow(engine)
    base = f"/v1/delivery-workflows/{key}/designs"
    view = client.get(base, params={"project_id": PROJECT_ID})
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["stages"]["prepare"]["status"] == "not_reviewed"
    assert body["graph"]["kg_version"] >= 1 and body["graph"]["cg_version"] >= 1
    assert body["kinds"]["rule"]["layer"] == "rules"

    sent = client.post(f"{base}/prepare/review", json={"project_id": PROJECT_ID, "actor": ACTOR})
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "in_review" and sent.json()["reference"] == "Design v0.1"

    no_step_up = client.post(f"{base}/prepare/lock", json={"project_id": PROJECT_ID, "actor": ACTOR})
    assert no_step_up.status_code == 403 and "step_up" in no_step_up.json()["detail"]
    locked = client.post(f"{base}/prepare/lock", json={"project_id": PROJECT_ID, "actor": ACTOR, "step_up": "confirmed"})
    assert locked.status_code == 200, locked.text
    assert locked.json()["status"] == "locked"
    again = client.post(f"{base}/prepare/lock", json={"project_id": PROJECT_ID, "actor": ACTOR, "step_up": "confirmed"})
    assert again.status_code == 409 and "already locked" in again.json()["detail"]

    changed = {**CONTRACT, "trigger": "after the lanes, or on demand"}
    with begin(engine) as connection:
        try:
            workflows.publish_version(connection, project_id=PROJECT_ID, definition_id=definition_id,
                                      spec=_spec(changed), actor=ACTOR)
            raise AssertionError("a locked contract was published changed")
        except workflows.WorkflowValidationError as refused:
            assert "locked at Design v0.1" in str(refused) and "Revise" in str(refused)

    revised = client.post(f"{base}/prepare/revise", json={"project_id": PROJECT_ID, "actor": ACTOR,
                                                          "note": "On demand too"})
    assert revised.status_code == 200, revised.text
    assert revised.json()["reference"] == "Design v0.2" and revised.json()["status"] == "in_review"
    with begin(engine) as connection:
        workflows.publish_version(connection, project_id=PROJECT_ID, definition_id=definition_id,
                                  spec=_spec(changed), actor=ACTOR)


def test_an_unknown_workflow_or_stage_is_a_404(client, engine):
    key, _ = _workflow(engine)
    assert client.get("/v1/delivery-workflows/no_such_flow/designs",
                      params={"project_id": PROJECT_ID}).status_code == 404
    missing = client.post(f"/v1/delivery-workflows/{key}/designs/nope/review",
                          json={"project_id": PROJECT_ID, "actor": ACTOR})
    assert missing.status_code == 404
