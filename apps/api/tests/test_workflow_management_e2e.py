"""Managing a workflow of your own, through the endpoints the Workflow Studio calls.

    "create new workflow, editing the workflow (assign agents and tasks)"
    "past experience was very poor quality for workflow working e2e (especially the management
     one)"

One user-built workflow is taken through its whole life: created by `/publish`, edited and
published again, checked into trouble and back, given an agent, scoped skills and an MCP server,
renamed, versioned, copied from a standard, and finally deleted or retired. The editor's own
contract tests (test_the_workflow_editor_saves_once.py) pin each endpoint; these pin the
journey, on a workflow built here rather than a standard, and read every result back the way the
console reads it. Runs of these workflows are driven in test_workflow_management_runs_e2e.py.
"""
from __future__ import annotations

import copy

from sqlalchemy import text

from apps.api.tests import workflow_management_support as wf

STANDARD = "standard_product_delivery"


def _versions(engine, definition_id: str) -> int:
    with engine.begin() as c:
        return int(c.execute(text("SELECT count(*) FROM workflow_versions "
                                  "WHERE workflow_definition_id = :d"),
                             {"d": definition_id}).scalar() or 0)


def _definitions(engine) -> int:
    with engine.begin() as c:
        return int(c.execute(text(
            "SELECT count(*) FROM workflow_definitions WHERE project_id = :p "
            "AND NOT COALESCE(is_catalogue, false)"), {"p": wf.PROJECT}).scalar() or 0)


def _where(problems: list[dict]) -> set[tuple]:
    return {(p["step_id"], p["field"]) for p in problems}


def _three_steps(agent: str, skill: str) -> dict:
    """Draft (an agent task) → a person looks (an approval gate) → Build a preview (built-in)."""
    return {
        "entry_step": "draft",
        "input_schema": wf.START_FORM,
        "steps": [
            wf.agent_step("draft", captain=agent, nxt=("look",), skills=[skill]),
            wf.gate_step("look", nxt=("preview",), question="Good enough to preview?"),
            {"id": "preview", "kind": "capability", "next": [],
             "approval": {"mode": "manual", "sla_hours": 4, "on_timeout": "block"},
             "binding": {"capability": "preview",
                         "inputs": {"run_id": {"value": "wfr_example"}}}},
        ],
    }


# ── Create, edit, check, publish, rename ──────────────────────────────────────────────────────


def test_a_workflow_is_created_edited_checked_renamed_and_keeps_every_version(client, engine):
    skill = wf.skills(client, 1)[0]
    agent = wf.new_agent(client, wf.key("drafter"), holds=(skill,))
    spec = _three_steps(agent, skill)

    # Check before the first save: nothing wrong, and the summary the editor's chips show.
    before = _definitions(engine)
    checked = wf.check(client, spec)
    assert checked["ok"] is True, checked["problems"]
    assert checked["summary"]["steps"] == 3
    assert agent in checked["summary"]["agents"]
    assert checked["summary"]["approvals"], "the gate and the manual step need a person"
    assert _definitions(engine) == before, "a check wrote something"

    # Save, new: one definition, v1, in one call.
    name = wf.name()
    made = wf.published(client, spec, title=name)
    assert (made["created"], made["version"], made["definition"]["name"]) == (True, 1, name)
    definition_id = made["definition"]["id"]
    key = made["definition"]["workflow_key"]
    assert _definitions(engine) == before + 1

    # What the editor loads back is what it saved: the agent, its scope, the start form, the
    # gate's question and the approval's settings.
    loaded = wf.spec_of(client, key)
    assert (loaded["definition_id"], loaded["version"], loaded["is_catalogue"]) == (
        definition_id, 1, False)
    steps = {s["id"]: s for s in loaded["spec"]["steps"]}
    assert steps["draft"]["captain"] == agent
    assert steps["draft"]["binding"]["skills"] == [skill]
    assert steps["draft"]["binding"]["inputs"]["instructions"] == {"from_input": "instructions"}
    assert steps["look"]["question"] == "Good enough to preview?"
    assert steps["preview"]["approval"]["mode"] == "manual"
    assert steps["preview"]["approval"]["sla_hours"] == 4
    assert loaded["spec"]["input_schema"] == wf.START_FORM
    row = wf.library(client)[definition_id]
    assert (row["version"], row["step_count"], row["run_count"]) == (1, 3, 0)
    assert [s["id"] for s in row["steps"]] == ["draft", "look", "preview"]

    # An edit with three things wrong, on three steps: Check names all of them at once, each
    # on its step and field, and Save refuses with exactly the same list and writes nothing.
    broken = copy.deepcopy(loaded["spec"])
    by_id = {s["id"]: s for s in broken["steps"]}
    by_id["draft"]["captain"] = "nobody_by_this_name"
    by_id["look"].pop("question")
    by_id["preview"]["binding"]["inputs"] = {}
    problems = wf.check(client, broken, definition_id)["problems"]
    assert {("draft", "captain"), ("look", "question"), ("preview", "run_id")} <= _where(problems)
    refused = wf.publish(client, broken, definition_id=definition_id, title=name)
    assert refused.status_code == 422, refused.text
    assert refused.json()["problems"] == problems
    assert _versions(engine, definition_id) == 1
    assert wf.spec_of(client, key)["version"] == 1

    # Fixed and edited for real: a title and a new task on the agent step. v2.
    fixed = copy.deepcopy(loaded["spec"])
    by_id = {s["id"]: s for s in fixed["steps"]}
    by_id["draft"]["title"] = "Write the first draft"
    by_id["draft"]["binding"]["inputs"]["instructions"] = {"value": "Draft the release notes."}
    second = wf.published(client, fixed, definition_id=definition_id, title=name)
    assert (second["created"], second["version"]) == (False, 2)
    assert second["definition"]["id"] == definition_id
    # Saving what is already published publishes nothing.
    assert wf.published(client, fixed, definition_id=definition_id, title=name) == {
        "unchanged": True}
    assert _versions(engine, definition_id) == 2

    # Rename and describe: the definition changes, no version is made.
    renamed = client.patch(f"/v1/delivery-workflows/{definition_id}", json={
        "project_id": wf.PROJECT, "actor": "ceo", "name": f"{name} renamed",
        "description": "Draft, look, preview."})
    assert renamed.status_code == 200, renamed.text
    assert _versions(engine, definition_id) == 2
    row = wf.library(client)[definition_id]
    assert (row["name"], row["description"], row["version"]) == (
        f"{name} renamed", "Draft, look, preview.", 2)
    assert row["steps"][0]["title"] == "Write the first draft"

    # Every version stays readable; the latest is the one that runs.
    latest = wf.spec_of(client, key)
    assert [(v["version"], v["is_active"]) for v in latest["versions"]] == [
        (2, True), (1, False)]
    first = wf.spec_of(client, key, version=1)
    draft_v1 = next(s for s in first["spec"]["steps"] if s["id"] == "draft")
    assert "title" not in draft_v1
    assert draft_v1["binding"]["inputs"]["instructions"] == {"from_input": "instructions"}


# ── Who runs a step, and what it may use ──────────────────────────────────────────────────────


def test_an_agent_step_is_given_only_the_skills_and_mcp_servers_its_agent_holds(
        client, engine):
    held, other = wf.skills(client, 2)
    agent = wf.new_agent(client, wf.key("scoped"), holds=(held,))
    server = wf.mcp_server(engine)

    def spec(**binding) -> dict:
        return {"entry_step": "work", "input_schema": wf.START_FORM,
                "steps": [wf.agent_step("work", captain=agent, **binding)]}

    # Scoped to what it holds: fine.
    assert wf.check(client, spec(skills=[held]))["ok"] is True

    # A skill it does not hold, an MCP server it does not hold: each refused on the step, by
    # Check and by Save alike, and nothing written. A step never grants anything.
    before = _definitions(engine)
    for field, binding in (("skills", {"skills": [held, other]}),
                           ("mcp", {"skills": [held], "mcp": [server["name"]]})):
        checked = wf.check(client, spec(**binding))
        assert checked["ok"] is False, field
        assert _where(checked["problems"]) == {("work", field)}, checked["problems"]
        assert f"which {agent} does not hold" in checked["problems"][0]["message"]
        saved = wf.publish(client, spec(**binding))
        assert saved.status_code == 422, (field, saved.text)
        assert saved.json()["problems"] == checked["problems"], field
    assert _definitions(engine) == before

    # Granted on the Agents page (the editor's Grant row calls the same endpoints): accepted.
    wf.grant_skill(client, agent, other)
    wf.grant_mcp(client, agent, server)
    full = spec(skills=[held, other], mcp=[server["name"]])
    assert wf.check(client, full)["ok"] is True
    made = wf.published(client, full)
    saved = wf.spec_of(client, made["definition"]["workflow_key"])["spec"]["steps"][0]
    assert saved["binding"]["skills"] == [held, other]
    assert saved["binding"]["mcp"] == [server["name"]]

    # Scope only means something on an agent task; on a built-in step it is refused.
    builtin = {"entry_step": "preview", "steps": [
        {"id": "preview", "kind": "capability", "next": [],
         "binding": {"capability": "preview", "skills": [held],
                     "inputs": {"run_id": {"value": "wfr_example"}}}}]}
    assert _where(wf.check(client, builtin)["problems"]) == {("preview", "skills")}

    # A person is offered only on a gate.
    person = spec(skills=[held])
    person["steps"][0]["captain"] = "human"
    assert ("work", "captain") in _where(wf.check(client, person)["problems"])


# ── Your copy of a standard ───────────────────────────────────────────────────────────────────


def test_a_refused_save_of_a_standard_or_of_your_copy_writes_nothing(client, engine):
    """The editor's contract test covers the copy made once and updated after; this covers the
    two refusals it does not — a broken Save as my copy, and a broken save onto the copy."""
    installed = client.post("/v1/delivery-workflows/catalogue/install",
                            json={"project_id": wf.PROJECT, "actor": "ceo"})
    assert installed.status_code == 200, installed.text
    standard = wf.spec_of(client, STANDARD)
    broken = copy.deepcopy(standard["spec"])
    broken["steps"][0]["captain"] = "nobody_by_this_name"

    before = _definitions(engine)
    refused = wf.publish(client, broken, definition_id=standard["definition_id"],
                         from_workflow_key=STANDARD)
    assert refused.status_code == 422, refused.text
    assert _definitions(engine) == before, "a refused Save as my copy made a copy"
    assert wf.spec_of(client, STANDARD).get("copy") is None

    mine = wf.published(client, standard["spec"], definition_id=standard["definition_id"],
                        from_workflow_key=STANDARD)
    copy_id = mine["definition"]["id"]
    assert _definitions(engine) == before + 1
    refused = wf.publish(client, broken, definition_id=standard["definition_id"],
                         from_workflow_key=STANDARD, update_copy_id=copy_id)
    assert refused.status_code == 422, refused.text
    assert _versions(engine, copy_id) == 1
    assert _definitions(engine) == before + 1
    # The standard itself is never written by any of it.
    assert wf.spec_of(client, STANDARD)["version"] == standard["version"]


# ── Versions and runs ─────────────────────────────────────────────────────────────────────────


def test_a_run_keeps_the_version_it_started_on_after_a_newer_one_is_published(
        client, engine, monkeypatch):
    model = wf.install_model(monkeypatch)
    skill = wf.skills(client, 1)[0]
    first_agent = wf.new_agent(client, wf.key("first"), holds=(skill,))
    second_agent = wf.new_agent(client, wf.key("second"), holds=(skill,))

    def spec(agent: str, task: str) -> dict:
        return {"entry_step": "work", "steps": [
            wf.agent_step("work", captain=agent, nxt=("look",), instructions={"value": task}),
            wf.gate_step("look", question="Done?")]}

    name = wf.name()
    made = wf.published(client, spec(first_agent, "Task as of v1."), title=name)
    key, definition_id = made["definition"]["workflow_key"], made["definition"]["id"]
    old_run = wf.started(client, key, {})

    newer = wf.published(client, spec(second_agent, "Task as of v2."),
                         definition_id=definition_id, title=name)
    assert newer["version"] == 2
    new_run = wf.started(client, key, {})

    wf.tick(client)
    old, new = wf.run_view(client, old_run), wf.run_view(client, new_run)
    assert (old["version"], new["version"]) == (1, 2)
    assert wf.steps_of(old)["work"]["captain"] == first_agent
    assert wf.steps_of(new)["work"]["captain"] == second_agent
    assert wf.steps_of(old)["work"]["output"]["ran_as"]["agent"] == first_agent
    assert wf.steps_of(new)["work"]["output"]["ran_as"]["agent"] == second_agent
    prompts = [c["prompt"] for c in model.calls]
    assert any("Task as of v1." in p for p in prompts)
    assert any("Task as of v2." in p for p in prompts)


def test_delete_removes_a_workflow_that_never_ran_and_retires_one_that_did(client, engine):
    skill = wf.skills(client, 1)[0]
    agent = wf.new_agent(client, wf.key("doer"), holds=(skill,))
    spec = {"entry_step": "work", "steps": [
        wf.agent_step("work", captain=agent, instructions={"value": "Do it."})]}

    def delete(definition_id: str):
        return client.delete(f"/v1/delivery-workflows/{definition_id}",
                             params={"project_id": wf.PROJECT, "actor": "ceo"})

    never = wf.published(client, spec)["definition"]["id"]
    ran = wf.published(client, spec)
    ran_id, ran_key = ran["definition"]["id"], ran["definition"]["workflow_key"]
    run_id = wf.started(client, ran_key, {})

    # A run still going: refused, with the count, and nothing changes.
    live = delete(ran_id)
    assert live.status_code == 409, live.text
    assert "1 run still going" in live.json()["detail"]
    assert ran_id in wf.library(client)

    # Never ran: gone, with its versions.
    gone = delete(never)
    assert gone.status_code == 200, gone.text
    assert gone.json()["outcome"] == "deleted", gone.json()
    assert never not in wf.library(client, include_retired=True)
    assert _versions(engine, never) == 0

    # Ran: once the run is stopped it is retired — out of the library, its run still readable.
    stopped = client.post(f"/v1/workflow-runs/{run_id}/cancel",
                          json={"project_id": wf.PROJECT, "actor": "ceo",
                                "reason": "Wrong workflow."})
    assert stopped.status_code == 200, stopped.text
    retired = delete(ran_id)
    assert retired.status_code == 200, retired.text
    assert retired.json()["outcome"] == "retired", retired.json()
    assert ran_id not in wf.library(client)
    assert wf.library(client, include_retired=True)[ran_id]["status"] == "retired"
    assert wf.run_view(client, run_id)["workflow_key"] == ran_key
    restart = wf.start(client, ran_key, {}, start_anyway=["model"])
    assert restart.status_code == 409, restart.text
    assert "not active" in restart.json()["detail"]

    # A standard is not yours to delete.
    client.post("/v1/delivery-workflows/catalogue/install",
                json={"project_id": wf.PROJECT, "actor": "ceo"})
    standard = wf.spec_of(client, STANDARD)
    assert delete(standard["definition_id"]).status_code == 403
