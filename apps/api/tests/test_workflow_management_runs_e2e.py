"""A workflow somebody built, started and driven to the end through the API.

    "test APIs when invoking the workflows ... past experience was very poor quality for
     workflow working e2e (especially the management one)"

Nothing here calls a service function to move a run. A workflow is built with `/publish`, started
with `/start` (and its readiness check), advanced by `/run-capabilities` — the endpoint the worker
calls on its loop — answered through the decisions inbox, and recovered with `retry-step` and
`skip-step`. Every assertion about where a run stands is read from `/runs`, the view the console's
Runs tab draws. Only the model on the far side of an agent step is scripted (support.ScriptedModel).

FOUND BY THIS FILE: an Approval gate placed from the editor's palette (a `decision` step) was
never released. A person approved it, and the run sat `waiting` at that gate for ever, because
the worker's tick released timers only and nothing else called `advance_run` for a product run.
"Run another workflow" had the same hole. See `workflows.a_wait_is_over`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.tests import workflow_management_support as wf


def _build(client, engine, *, finish_approval: dict | None = None) -> dict:
    """Draft (an agent, scoped to one skill and one MCP server) → a person looks → Finish (the
    same agent, a fixed task, its own approval setting)."""
    skill = wf.skills(client, 1)[0]
    agent = wf.new_agent(client, wf.key("writer"), holds=(skill,))
    server = wf.mcp_server(engine)
    wf.grant_mcp(client, agent, server)
    spec = {
        "entry_step": "draft",
        "input_schema": wf.START_FORM,
        "steps": [
            wf.agent_step("draft", captain=agent, nxt=("look",),
                          skills=[skill], mcp=[server["name"]]),
            wf.gate_step("look", nxt=("finish",), question="Ship the draft?",
                         guidance="Read it through once."),
            {**wf.agent_step("finish", captain=agent,
                             instructions={"value": "Write the release note."}),
             "approval": finish_approval or {"mode": "auto"}},
        ],
    }
    made = wf.published(client, spec)
    return {"agent": agent, "skill": skill, "server": server, "spec": spec,
            "definition_id": made["definition"]["id"],
            "workflow_key": made["definition"]["workflow_key"]}


def _decide(client, decision_id: str, verb: str) -> None:
    answered = client.post(f"/v1/decisions/{decision_id}/{verb}",
                           json={"actor": "ceo", "note": f"{verb}d in the test"})
    assert answered.status_code == 200, answered.text


def _drive_to_the_gate(client, built: dict, instructions: str) -> tuple[str, dict]:
    run_id = wf.started(client, built["workflow_key"], {"instructions": instructions})
    wf.tick(client)
    view = wf.run_view(client, run_id)
    assert wf.statuses(view) == {"draft": "succeeded", "look": "waiting", "finish": "pending"}
    return run_id, view


# ── The happy path, every step a different kind of thing ──────────────────────────────────────


def test_a_built_workflow_runs_to_the_end_through_its_agent_and_its_gates(
        client, engine, monkeypatch):
    model = wf.install_model(monkeypatch)
    built = _build(client, engine, finish_approval={
        "mode": "manual", "sla_hours": 4, "on_timeout": "block"})
    key, agent = built["workflow_key"], built["agent"]

    # READINESS FIRST. Nothing is spent, and nothing is started, until the project can run it:
    # this one has no model connected, and Start says so with the checklist the composer draws.
    ready = client.get(f"/v1/delivery-workflows/{key}/readiness",
                       params={"project_id": wf.PROJECT,
                               "input": json.dumps({"instructions": "x"})})
    assert ready.status_code == 200, ready.text
    assert "model" in {m["key"] for m in ready.json()["missing"]}
    refused = wf.start(client, key, {"instructions": "Draft the notes."})
    assert refused.status_code == 409, refused.text
    assert "model" in {m["key"] for m in refused.json()["detail"]["readiness"]["missing"]}
    # The start form's required field is required.
    unfilled = wf.start(client, key, {"audience": "the team"}, start_anyway=["model"])
    assert unfilled.status_code == 422, unfilled.text
    assert unfilled.json()["detail"].endswith("not given: instructions")
    assert wf.tick(client) == [], "a refused start left a run behind"

    run_id = wf.started(client, key, {"instructions": "Draft the notes.", "audience": "team"})
    view = wf.run_view(client, run_id)
    assert (view["status"], view["version"], view["current_step"]) == ("running", 1, "draft")
    assert wf.statuses(view) == {"draft": "ready", "look": "pending", "finish": "pending"}
    assert {s: v["captain"] for s, v in wf.steps_of(view).items()} == {
        "draft": agent, "look": "human", "finish": agent}

    # The agent step runs as the agent the workflow names, limited to what the step allows.
    performed = wf.tick(client)
    assert [(p["step_id"], p["outcome"]) for p in performed] == [("draft", "succeeded")]
    view = wf.run_view(client, run_id)
    draft = wf.steps_of(view)["draft"]
    assert draft["status"] == "succeeded"
    ran_as = draft["output"]["ran_as"]
    assert (ran_as["agent"], ran_as["how"]) == (agent, "named")
    assert ran_as["skills_offered"] == [built["skill"]]
    assert draft["output"]["mcp_servers_offered"] == [built["server"]["name"]]
    assert "Draft the notes." in model.calls[0]["prompt"]
    assert any(t.startswith("mcp_") and t.endswith("_post") for t in model.calls[0]["tools"])

    # The gate waits for a person, with the question the editor wrote, and the tick does not
    # walk past it.
    gate = wf.steps_of(view)["look"]
    assert (view["status"], view["current_step"], gate["status"]) == ("waiting", "look", "waiting")
    assert gate["decision_status"] == "pending"
    card = client.get(f"/v1/decisions/{gate['decision_id']}").json()
    assert card["question"] == "Ship the draft?"
    assert "Read it through once." in card["reason"]
    assert wf.tick(client) == []
    assert wf.statuses(wf.run_view(client, run_id))["finish"] == "pending"

    # Approved in the inbox: the next tick releases the gate and reaches `finish`, whose own
    # approval setting ("Needs approval") opens a second gate before the agent does anything.
    _decide(client, gate["decision_id"], "approve")
    performed = wf.tick(client)
    assert ("finish", "gate_opened") in [(p["step_id"], p["outcome"]) for p in performed]
    view = wf.run_view(client, run_id)
    finish = wf.steps_of(view)["finish"]
    assert wf.statuses(view) == {"draft": "succeeded", "look": "succeeded", "finish": "waiting"}
    assert finish["decision_id"] and finish["decision_id"] != gate["decision_id"]
    # The approval settings the editor wrote are the ones the gate runs with: answer within
    # four hours, and if nobody does, stay blocked rather than approve itself.
    assert finish["approval"] == {"mode": "manual", "sla_hours": 4, "on_timeout": "block"}
    due_in = datetime.fromisoformat(finish["due_at"]) - datetime.now(timezone.utc)
    assert timedelta(hours=3, minutes=55) < due_in <= timedelta(hours=4)
    assert finish["timeout_applied"] is None
    assert len(model.calls) == 1, "the agent ran before its approval"

    _decide(client, finish["decision_id"], "approve")
    wf.tick(client)
    if wf.run_view(client, run_id)["status"] != "completed":
        wf.tick(client)        # released in one pass, executed in the next
    view = wf.run_view(client, run_id)
    assert view["status"] == "completed", view
    assert wf.statuses(view) == {"draft": "succeeded", "look": "succeeded",
                                 "finish": "succeeded"}
    assert (view["steps_done"], view["step_count"], view["finished"]) == (3, 3, True)
    finish = wf.steps_of(view)["finish"]
    assert finish["output"]["ran_as"]["agent"] == agent
    assert "Write the release note." in model.calls[-1]["prompt"]
    assert len(model.calls) == 2


# ── When a step fails ─────────────────────────────────────────────────────────────────────────


def _failed_at_finish(client, engine, monkeypatch) -> tuple[wf.ScriptedModel, dict, str]:
    model = wf.install_model(monkeypatch)
    built = _build(client, engine)
    run_id, view = _drive_to_the_gate(client, built, "Draft the notes.")
    model.failing = "Write the release note."
    _decide(client, wf.steps_of(view)["look"]["decision_id"], "approve")
    for _ in range(3):
        wf.tick(client)
    return model, built, run_id


def test_a_failed_step_stops_the_run_and_try_again_runs_only_that_step(
        client, engine, monkeypatch):
    model, built, run_id = _failed_at_finish(client, engine, monkeypatch)
    view = wf.run_view(client, run_id)
    finish = wf.steps_of(view)["finish"]
    assert (view["status"], view["standing"]) == ("deadlettered", "stopped"), view
    assert wf.statuses(view) == {"draft": "succeeded", "look": "succeeded",
                                 "finish": "deadlettered"}
    assert "the model provider is down" in finish["error"]
    assert view["blocked_reason"].startswith("Stopped at finish:")
    # The console's two ways forward, for that step.
    offered = {a["key"]: a for a in view["actions"]}
    assert offered["retry_step"]["step_id"] == offered["skip_step"]["step_id"] == "finish"
    calls_before = len(model.calls)

    # A stopped run stays stopped: more ticks do nothing to it.
    assert [p for p in wf.tick(client) if p.get("run_id") == run_id] == []

    model.failing = None
    retried = wf.post(client, f"runs/{run_id}/retry-step", step_id="finish")
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "ready"
    wf.tick(client)
    view = wf.run_view(client, run_id)
    assert view["status"] == "completed", view
    assert wf.statuses(view) == {"draft": "succeeded", "look": "succeeded",
                                 "finish": "succeeded"}
    # Only the failed step ran again; the draft and the person's approval were kept.
    assert len(model.calls) == calls_before + 1
    assert "Write the release note." in model.calls[-1]["prompt"]

    # Retrying a step that did not fail is refused, in words.
    again = wf.post(client, f"runs/{run_id}/retry-step", step_id="draft")
    assert again.status_code == 409, again.text


def test_skip_goes_on_without_the_failed_step_and_a_refused_gate_stops_the_run(
        client, engine, monkeypatch):
    model, built, run_id = _failed_at_finish(client, engine, monkeypatch)

    # A skip needs a reason, which the run keeps with who gave it.
    no_reason = wf.post(client, f"runs/{run_id}/skip-step", step_id="finish", reason=" ")
    assert no_reason.status_code == 422, no_reason.text
    skipped = wf.post(client, f"runs/{run_id}/skip-step", step_id="finish",
                      reason="The note can wait.")
    assert skipped.status_code == 200, skipped.text
    wf.tick(client)
    view = wf.run_view(client, run_id)
    assert view["status"] == "completed", view
    finish = wf.steps_of(view)["finish"]
    assert finish["status"] == "skipped"
    assert finish["output"]["skipped_by_operator"] is True
    assert (finish["output"]["skip_reason"], finish["output"]["skipped_by"]) == (
        "The note can wait.", "ceo")
    assert "the model provider is down" in finish["output"]["failure"]

    # A person's "no" at a gate stops the run there, and cannot be skipped into a yes.
    second, view = _drive_to_the_gate(client, built, "Draft another.")
    gate = wf.steps_of(view)["look"]
    _decide(client, gate["decision_id"], "reject")
    wf.tick(client)
    view = wf.run_view(client, second)
    assert view["status"] == "blocked", view
    assert wf.statuses(view) == {"draft": "succeeded", "look": "failed", "finish": "pending"}, \
        "the run walked past a rejected gate"
    assert "rejected" in view["blocked_reason"]
    # What the console offers is reopening the question, not skipping it.
    offered = {a["key"]: a for a in view["actions"]}
    assert offered["retry_step"]["step_id"] == "look" and "skip_step" not in offered
    refused = wf.post(client, f"runs/{second}/skip-step", step_id="look",
                      reason="Ship it anyway.")
    assert refused.status_code == 409, refused.text
    assert "approval" in refused.json()["detail"]
    wf.tick(client)
    assert wf.statuses(wf.run_view(client, second))["finish"] == "pending"


# ── Run another workflow ──────────────────────────────────────────────────────────────────────


def test_a_run_another_workflow_step_waits_for_the_child_and_goes_on_when_it_ends(
        client, engine, monkeypatch):
    model = wf.install_model(monkeypatch)
    skill = wf.skills(client, 1)[0]
    agent = wf.new_agent(client, wf.key("helper"), holds=(skill,))
    child = wf.published(client, {"entry_step": "help", "steps": [
        wf.agent_step("help", captain=agent, instructions={"value": "Help out."})]})
    # "Run another workflow" is pinned to the child's version, as the editor's picker pins it.
    with engine.begin() as c:
        version_id = c.execute(text(
            "SELECT active_version_id FROM workflow_definitions WHERE id = :d"),
            {"d": child["definition"]["id"]}).scalar()
    parent = wf.published(client, {"entry_step": "delegate", "steps": [
        {"id": "delegate", "kind": "subworkflow", "next": ["wrap"],
         "binding": {"workflow_version_id": version_id}},
        wf.agent_step("wrap", captain=agent, instructions={"value": "Wrap it up."})]})

    run_id = wf.started(client, parent["definition"]["workflow_key"], {})
    for _ in range(3):
        wf.tick(client)
    view = wf.run_view(client, run_id)
    assert view["status"] == "completed", view
    assert wf.statuses(view) == {"delegate": "succeeded", "wrap": "succeeded"}
    # The child ran as its own run, to the end, and only then did the parent go on.
    child_runs = [r for r in client.get("/v1/delivery-workflows/runs", params={
        "project_id": wf.PROJECT}).json()["runs"]
        if r["workflow_key"] == child["definition"]["workflow_key"]]
    assert [r["status"] for r in child_runs] == ["completed"]
    prompts = [c["prompt"] for c in model.calls]
    assert len(prompts) == 2
    assert "Help out." in prompts[0] and "Wrap it up." in prompts[1]


# ── The agent's door ──────────────────────────────────────────────────────────────────────────


def test_the_agents_start_passes_start_form_values_and_cannot_rename_the_work(
        client, engine, monkeypatch):
    """The agent's start_workflow passes `inputs` into the run, where a step's `from_input`
    reads them — and cannot overwrite the directive and title BOSS sets, which govern the run.
    (Rewritten from a test that grepped builtin.py for the update call.)"""
    from apps.api.app.config import get_settings
    from boss_engine.context import EngineContext
    from boss_engine.tools import build_default_registry

    model = wf.install_model(monkeypatch)
    built = _build(client, engine)
    registry = build_default_registry()
    ctx = EngineContext(settings=get_settings(), engine=engine)

    def start(**args):
        return registry.invoke("start_workflow", ctx, project_id=wf.PROJECT, actor="ceo",
                               args={"workflow_key": built["workflow_key"], **args},
                               operator_confirmed=True)

    refused = start(directive="Notes for the team", inputs={"instructions": "Draft them."})
    assert refused["ok"] is False and "model" in {m["key"] for m in refused["missing"]}

    began = start(directive="Notes for the team", title="Team notes",
                  inputs={"instructions": "Draft them.", "audience": "team",
                          "directive": "something else entirely", "title": "Renamed"},
                  start_anyway=["model"])
    assert began["status"] == "running", began
    view = wf.run_view(client, began["run_id"])
    assert view["input"] == {"instructions": "Draft them.", "audience": "team",
                             "directive": "Notes for the team", "title": "Team notes"}
    wf.tick(client)
    assert wf.statuses(wf.run_view(client, began["run_id"]))["draft"] == "succeeded"
    assert "Draft them." in model.calls[-1]["prompt"]
