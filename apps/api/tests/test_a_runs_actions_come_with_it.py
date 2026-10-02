"""Every run the console reads carries the actions the engine decided for it.

The incident: a cancelled Standard Product Delivery run whose Research step had FAILED still
offered "Build on this research →" — the console decided it with its own rule, and an old
research step that had failed (`succeeded`, output `failed_continued`, a dead discovery's id)
satisfied that rule. The product owner: "Does it make sense to have Build on this research?"

The engine now decides (workflow_run_actions) and the API ships the result with the run, on the
console's list (`GET /v1/delivery-workflows/runs`), on the run itself
(`GET /v1/workflow-runs/{id}`) and on the agent's list (`GET /v1/workflow-runs`).

And it ENFORCES what it decides. The first version only moved the buttons: Try again was hidden
on a cancelled run while POST retry-step, the agent's `retry_workflow_step` and the engine all
still accepted it, and `from_run` built on a run still going. Every operation now asks the same
decision, so the second half of this file presses, over HTTP and through the agent's tools,
every button the list leaves out — and each is refused in the words the list would have shown.
"""
import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import workflow_run_actions, workflows

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
    assert run["finished"] is True
    keys = _keys(run)
    assert "build_on_research" not in keys
    assert keys["rerun"] is True and keys["remove"] is True
    assert "stop" not in keys
    # REWRITTEN: this used to pin a greyed Try again here. A stopped run is never resumed, so a
    # disabled button under the ✗ could never become active — it is left out, like Build on
    # this research, and pressing it anyway is refused (below).
    assert not any(a["key"] in ("retry_step", "skip_step") for a in run["actions"])


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
    listed = _listed(client, run_id)
    assert _keys(listed) == {"stop": True, "stop_and_rerun": True}
    assert listed["finished"] is False


def test_the_run_detail_carries_the_same_actions(client, engine, make_run):
    run_id = make_run(run_status="failed", research_status="failed",
                      research_output={"status": "failed"}, panel_status="pending")
    response = client.get(f"/v1/workflow-runs/{run_id}", params={"project_id": PROJECT})
    assert response.status_code == 200, response.text
    detail = response.json()
    assert _keys(detail) == _keys(_listed(client, run_id))
    assert _keys(detail)["skip_step:research"] is True


def test_failed_continued_alone_is_enough_to_withhold_build_on_this_research(
        client, engine, make_run):
    """The step's own report, not the dossier, says the research failed: here the dossier it
    names went on to COMPLETE with findings. Every other incident case also had a dead
    dossier, so dropping `failed_continued` from the rule left them all green."""
    good = _discovery(engine, "completed", {"signal_findings": ["buyers want X"]})
    run_id = make_run(run_status="cancelled", research_status="succeeded",
                      research_output={"discovery_id": good, "status": "failed_continued"})
    assert "build_on_research" not in _keys(_listed(client, run_id))
    response = client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": "standard_product_delivery",
        "from_run": run_id, "input": {}})
    assert response.status_code == 409, response.text


def test_the_agent_list_carries_the_actions_too(client, engine, make_run):
    """GET /v1/workflow-runs is what the agent and the MCP list surface read."""
    run_id = make_run(run_status="failed", research_status="failed",
                      research_output={"status": "failed"}, panel_status="pending")
    response = client.get("/v1/workflow-runs", params={"project_id": PROJECT, "limit": 200})
    assert response.status_code == 200, response.text
    item = next(i for i in response.json()["items"] if i["id"] == run_id)
    assert _keys(item) == _keys(_listed(client, run_id))


# ── Pressing what is not offered ─────────────────────────────────────────────────────────────

def _why(engine, run_id, key, step_id=None):
    """The sentence the action rule gives for `key` on this run — what a refusal must say."""
    with engine.begin() as c:
        run, steps = workflow_run_actions.load_runs(c, PROJECT, [run_id])[run_id]
        verdict = workflow_run_actions.decide(run, steps, {}, key, step_id)
    assert verdict["state"] != "enabled", f"{key} is offered; this case presses what is not"
    return verdict["reason"]


def _state(engine, run_id, step_id):
    with engine.begin() as c:
        return (c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"),
                          {"r": run_id}).scalar(),
                c.execute(text("SELECT status FROM workflow_step_runs "
                               "WHERE workflow_run_id=:r AND step_id=:s"),
                          {"r": run_id, "s": step_id}).scalar())


def test_try_again_on_a_stopped_run_is_refused_by_the_endpoint(client, engine, make_run):
    """The console no longer shows it; the endpoint used to revive the run anyway."""
    run_id = make_run(run_status="cancelled", research_status="failed",
                      research_output={"status": "failed"})
    response = client.post(f"/v1/delivery-workflows/runs/{run_id}/retry-step", json={
        "project_id": PROJECT, "actor": "ceo", "step_id": "research"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _why(engine, run_id, "retry_step", "research")
    assert _state(engine, run_id, "research") == ("cancelled", "failed")


def test_and_by_the_agents_tool(engine, make_run):
    from apps.api.app.config import get_settings
    from boss_engine.context import EngineContext
    from boss_engine.tools import build_default_registry

    run_id = make_run(run_status="cancelled", research_status="failed",
                      research_output={"status": "failed"})
    result = build_default_registry().invoke(
        "retry_workflow_step", EngineContext(settings=get_settings(), engine=engine),
        project_id=PROJECT, actor="ceo",
        args={"run_id": run_id, "step_id": "research", "project_id": PROJECT})
    assert result["ok"] is False
    assert result["error"] == _why(engine, run_id, "retry_step", "research")
    assert _state(engine, run_id, "research") == ("cancelled", "failed")


def test_skip_on_a_stopped_run_is_refused(client, engine, make_run):
    run_id = make_run(run_status="cancelled", research_status="failed",
                      research_output={"status": "failed"})
    response = client.post(f"/v1/delivery-workflows/runs/{run_id}/skip-step", json={
        "project_id": PROJECT, "actor": "ceo", "step_id": "research", "reason": "go on"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _why(engine, run_id, "skip_step", "research")
    assert _state(engine, run_id, "research") == ("cancelled", "failed")


def test_building_on_a_run_that_is_still_going_is_refused(client, engine, make_run):
    """The button is only offered once the run has finished — a second workflow on the same
    directive while this one runs is the duplicate Stop and run again exists to prevent. The
    request used to accept it anyway."""
    good = _discovery(engine, "completed", {"signal_findings": ["buyers want X"]})
    run_id = make_run(run_status="running", research_status="succeeded",
                      research_output={"discovery_id": good}, panel_status="running")
    response = client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": "standard_product_delivery",
        "from_run": run_id, "input": {}})
    assert response.status_code == 409, response.text
    assert _why(engine, run_id, "build_on_research") in response.json()["detail"]


def test_removing_a_run_that_is_still_going_is_refused_with_what_to_do(
        client, engine, make_run):
    run_id = make_run(run_status="running", research_status="failed",
                      research_output={"status": "failed"}, panel_status="pending")
    response = client.delete(f"/v1/delivery-workflows/runs/{run_id}",
                             params={"project_id": PROJECT, "actor": "ceo"})
    assert response.status_code == 409, response.text
    assert "Cancel it first" in response.json()["detail"]
    assert response.json()["detail"] == _why(engine, run_id, "remove")


def test_stopping_a_run_that_is_undoing_itself_is_refused(client, engine, make_run):
    """Stop is shown DISABLED on a compensating run — the one temporary state — and pressing it
    is refused with the same sentence rather than racing the undo."""
    run_id = make_run(run_status="compensating", research_status="failed",
                      research_output={"status": "failed"}, panel_status="pending")
    stop = next(a for a in _listed(client, run_id)["actions"] if a["key"] == "stop")
    assert stop["enabled"] is False
    response = client.post(f"/v1/workflow-runs/{run_id}/cancel", json={
        "project_id": PROJECT, "actor": "ceo", "reason": "stop it", "compensate": False})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == stop["reason"]
    assert _state(engine, run_id, "research")[0] == "compensating"


def test_what_is_offered_still_works(client, engine, make_run):
    """The other half — the gate must not refuse what the list offers. A failed run's Try again
    re-queues the step, and its Remove removes it."""
    run_id = make_run(run_status="failed", research_status="failed",
                      research_output={"status": "failed"}, panel_status="pending")
    assert _keys(_listed(client, run_id))["retry_step:research"] is True
    response = client.post(f"/v1/delivery-workflows/runs/{run_id}/retry-step", json={
        "project_id": PROJECT, "actor": "ceo", "step_id": "research"})
    assert response.status_code == 200, response.text
    assert _state(engine, run_id, "research")[1] == "ready"

    done = make_run(run_status="cancelled", research_status="failed",
                    research_output={"status": "failed"})
    assert _keys(_listed(client, done))["remove"] is True
    response = client.delete(f"/v1/delivery-workflows/runs/{done}",
                             params={"project_id": PROJECT, "actor": "ceo"})
    assert response.status_code == 200, response.text
