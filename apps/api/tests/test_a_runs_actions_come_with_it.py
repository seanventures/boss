"""Every run the console reads carries the actions the engine decided for it.

The incident: a cancelled Standard Product Delivery run whose Research step had FAILED still
offered "Build on this research →" — the console decided it with its own rule, and an old
research step that had failed (`succeeded`, output `failed_continued`, a dead discovery's id)
satisfied that rule. The product owner: "Does it make sense to have Build on this research?"

The engine now decides (workflow_run_actions.run_actions) and the API ships the result with the
run, on the console's list (`GET /v1/delivery-workflows/runs`) and on the run itself
(`GET /v1/workflow-runs/{id}`). `from_run` on start reads the same rule, so the button and the
request it sends cannot disagree.
"""
import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import workflows

PROJECT = "boss-dogfood"


def _discovery(engine, status, state):
    discovery_id = f"drun_{uuid4().hex[:12]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO discovery_runs (id, project_id, title, status, phase, state) "
            "VALUES (:id, :p, 'research', :s, 'done', CAST(:state AS JSONB))"),
            {"id": discovery_id, "p": PROJECT, "s": status, "state": json.dumps(state)})
    return discovery_id


@pytest.fixture
def make_run(engine):
    """A real published product workflow — research → panel — started, then put in the state a
    case names."""
    def build(*, run_status, research_status, research_output, panel_status="cancelled"):
        key = f"acts_{uuid4().hex[:10]}"
        with engine.begin() as c:
            definition = workflows.create_definition(
                c, project_id=PROJECT, workflow_key=key, name="Actions", description=None,
                actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
            version = workflows.publish_version(
                c, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
                spec={"entry_step": "research", "steps": [
                    {"id": "research", "kind": "capability",
                     "binding": {"capability": "research", "inputs": {"seed": "x"}},
                     "next": ["panel"]},
                    {"id": "panel", "kind": "capability",
                     "binding": {"capability": "notify", "inputs": {"message": "on"}},
                     "next": []},
                ]})
            run = workflows.start_run(
                c, project_id=PROJECT, actor="ceo", input_data={"directive": "build a thing"},
                dedupe_key=f"dk_{uuid4().hex[:10]}", workflow_version_id=version["id"])
            c.execute(text(
                "UPDATE workflow_step_runs SET status=:s, output=CAST(:o AS JSONB), "
                "completed_at=now() WHERE workflow_run_id=:r AND step_id='research'"),
                {"r": run["id"], "s": research_status, "o": json.dumps(research_output)})
            c.execute(text(
                "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
                "step_kind, iteration, status, input, output, binding_snapshot, evidence, "
                "attempt, max_attempts, run_after, created_at, updated_at) "
                "SELECT :id, project_id, workflow_run_id, 'panel', step_kind, 0, :s, '{}', '{}', "
                "binding_snapshot, '[]', 0, max_attempts, now(), now(), now() "
                "FROM workflow_step_runs WHERE workflow_run_id=:r AND step_id='research' "
                "ON CONFLICT DO NOTHING"),
                {"id": f"wsr_{uuid4().hex[:12]}", "r": run["id"], "s": panel_status})
            c.execute(text("UPDATE workflow_step_runs SET status=:s "
                           "WHERE workflow_run_id=:r AND step_id='panel'"),
                      {"r": run["id"], "s": panel_status})
            c.execute(text("UPDATE workflow_runs SET status=:s WHERE id=:r"),
                      {"r": run["id"], "s": run_status})
        return run["id"]
    return build


def _listed(client, run_id):
    response = client.get("/v1/delivery-workflows/runs",
                          params={"project_id": PROJECT, "limit": 100})
    assert response.status_code == 200, response.text
    return next(r for r in response.json()["runs"] if r["id"] == run_id)


def _keys(run):
    return {a["key"] + (f":{a['step_id']}" if a.get("step_id") else ""): a["enabled"]
            for a in run["actions"]}


def test_the_incident_run_is_listed_without_build_on_this_research(client, engine, make_run):
    """Cancelled; research recorded `succeeded` but `failed_continued`; its discovery failed."""
    dead = _discovery(engine, "failed", {"signal_findings": ["half a finding"]})
    run_id = make_run(run_status="cancelled", research_status="succeeded",
                      research_output={"discovery_id": dead, "status": "failed_continued"})
    run = _listed(client, run_id)
    assert "actions" in run
    keys = _keys(run)
    assert "build_on_research" not in keys
    assert keys["rerun"] is True and keys["remove"] is True
    assert "stop" not in keys
    # A stopped run is not resumed by a step retry; the button says so rather than vanishing.
    retry = next(a for a in run["actions"] if a["key"] == "retry_step")
    assert retry["enabled"] is False and "stopped" in retry["reason"]


def test_and_starting_from_it_is_refused_by_the_same_rule(client, engine, make_run):
    dead = _discovery(engine, "failed", {})
    run_id = make_run(run_status="cancelled", research_status="succeeded",
                      research_output={"discovery_id": dead, "status": "failed_continued"})
    response = client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": "standard_product_delivery",
        "from_run": run_id, "input": {}})
    assert response.status_code == 409, response.text
    assert "did not succeed" in response.json()["detail"]


def test_research_that_finished_with_findings_can_be_built_on(client, engine, make_run):
    good = _discovery(engine, "completed", {"signal_findings": ["buyers want X"]})
    run_id = make_run(run_status="cancelled", research_status="succeeded",
                      research_output={"discovery_id": good})
    keys = _keys(_listed(client, run_id))
    assert keys["build_on_research"] is True


def test_a_live_run_offers_stop_and_not_remove(client, engine, make_run):
    good = _discovery(engine, "completed", {"signal_findings": ["buyers want X"]})
    run_id = make_run(run_status="running", research_status="succeeded",
                      research_output={"discovery_id": good}, panel_status="running")
    keys = _keys(_listed(client, run_id))
    assert keys == {"stop": True, "stop_and_rerun": True}


def test_the_run_detail_carries_the_same_actions(client, engine, make_run):
    run_id = make_run(run_status="failed", research_status="failed",
                      research_output={"status": "failed"}, panel_status="pending")
    response = client.get(f"/v1/workflow-runs/{run_id}", params={"project_id": PROJECT})
    assert response.status_code == 200, response.text
    detail = response.json()
    assert _keys(detail) == _keys(_listed(client, run_id))
    assert _keys(detail)["skip_step:research"] is True
