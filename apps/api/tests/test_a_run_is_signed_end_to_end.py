"""A run is signed end to end: one connected chain, truthful edges, cost on the right agent.

Spec §12.4 test 4 (Agent World, S-acts). One workflow, every way work reaches an agent:

    review  an agent step NAMING qa                    → "Named in workflow"
    pick    an agent step left to BOSS, two could do it → "Picked by BOSS over 1"
    gate    an approval gate, a person answers          → "Approved by you"
    spend   a built-in step naming nobody               → "Ran as you"
    build   a build naming an engineer; its code task   → "Next stage", five stages, the
            runs five stages through the real telemetry   implement stage credited to the
            chokepoint, the way CodeAgent records them    named engineer, not STAGE_ROLES

Driven through the HTTP API the console and the worker use, on the real database. Only the
model (agent steps, the pick) and the code task's sandbox are stood in for: the build's model
calls go through `llm_telemetry.record_llm_call`, the one place every real call is recorded.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from apps.api.tests import workflow_management_support as wf

STAGES = ("plan", "write_tests", "implement", "review_code", "security_check")


def _unheld_skills(client, n: int) -> list[str]:
    held = {a["skill_id"] for a in client.get(
        "/v1/role-skills", params={"project_id": wf.PROJECT}).json()["assignments"]}
    free = [s["id"] for s in client.get(
        "/v1/skills", params={"project_id": wf.PROJECT}).json()["items"] if s["id"] not in held]
    assert len(free) >= n, "too few skills nobody holds; the fixture changed"
    return free[:n]


def _stand_in_for_the_build(monkeypatch, engine) -> dict:
    """create_code_task, minus the sandbox: a real code_task_runs row the step waits on."""
    from boss_engine.services import workflow_capability_runner as runner

    made: dict = {}
    real = runner._invoke_tool

    def invoke(ctx, tool_name, project_id, actor, args):
        if tool_name != "create_code_task":
            return real(ctx, tool_name, project_id, actor, args)
        run_id = f"ctr_{uuid.uuid4().hex[:16]}"
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, "
                "finalize_ctx) VALUES (:i, :p, 'running', 'coding', :t, '{}'::jsonb, "
                "CAST(:f AS JSONB))"),
                {"i": run_id, "p": project_id, "t": str(args.get("title") or "Ship it"),
                 "f": json.dumps({"workflow_run_id": args.get("workflow_run_id")})})
        made.update(run_id=run_id, actor=actor)
        return {"run_id": run_id, "status": "running"}

    monkeypatch.setattr(runner, "_invoke_tool", invoke)
    return made


def _the_build_runs_its_stages(engine, code_task_run_id: str) -> None:
    """What CodeAgent.run_role records per stage: one llm_calls row through the chokepoint."""
    from boss_engine.services.agent_acts import settle_code_task
    from boss_engine.services.llm_telemetry import (
        clear_llm_context, record_llm_call, set_llm_context)

    set_llm_context(db_engine=engine, project_id=wf.PROJECT, run_id=code_task_run_id)
    try:
        for stage in STAGES:
            set_llm_context(stage=stage)
            record_llm_call(engine_name="stub", usage={
                "model": "stub-model", "prompt_tokens": 100, "completion_tokens": 40,
                "total_tokens": 140}, cost_usd_micros=2_500, stage=stage)
    finally:
        clear_llm_context()
    with engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status='completed', phase='done', "
                       "pull_request_url='https://example.invalid/pr/7', pull_request_number=7 "
                       "WHERE id=:i"), {"i": code_task_run_id})
    settle_code_task(engine, code_task_run_id)


def _decide(client, decision_id: str) -> None:
    answered = client.post(f"/v1/decisions/{decision_id}/approve",
                           json={"actor": "ceo", "note": "approved in the test"})
    assert answered.status_code == 200, answered.text


def _live(client) -> dict[str, str]:
    got = client.get("/v1/agents/live", params={"project_id": wf.PROJECT})
    assert got.status_code == 200, got.text
    return {i["role_key"]: i["state"] for i in got.json()["items"]}


def test_a_run_is_one_connected_chain_with_truthful_edges_and_cost_on_the_right_agent(
        client, engine, monkeypatch):
    wf.install_model(monkeypatch)
    from boss_engine.services import agent_steps

    qa_skill, skill = _unheld_skills(client, 2)
    wf.grant_skill(client, "qa", qa_skill)
    pick_a = wf.new_agent(client, wf.key("pick_a"), holds=(skill,))
    pick_b = wf.new_agent(client, wf.key("pick_b"), holds=(skill,))
    engineer = wf.new_agent(client, wf.key("engineer"))
    monkeypatch.setattr(agent_steps, "_ask_the_model", lambda *_a, **_k: json.dumps(
        {"role_key": pick_b, "why": "it reads like pick_b's work"}))
    build = _stand_in_for_the_build(monkeypatch, engine)

    spec = {
        "entry_step": "review",
        # Single-pass (two_pass's documented opt-out): this test signs the chain of one build;
        # the More tests pass every build otherwise gets has its own tests.
        "more_tests": False,
        "input_schema": {"type": "object", "required": ["instructions", "title"],
                         "properties": {"instructions": {"type": "string"},
                                        "title": {"type": "string"}}},
        "steps": [
            wf.agent_step("review", captain="qa", nxt=("pick",), skills=[qa_skill]),
            wf.agent_step("pick", nxt=("gate",), skills=[skill]),
            wf.gate_step("gate", nxt=("spend",), question="Ship it?"),
            {"id": "spend", "kind": "capability", "next": ["build"], "max_attempts": 1,
             "binding": {"capability": "cost_check", "inputs": {}}},
            {"id": "build", "kind": "capability", "next": [], "max_attempts": 1,
             "captain": engineer,
             "binding": {"capability": "build", "inputs": {
                 "title": {"from_input": "title"},
                 "instructions": {"from_input": "instructions"}}}},
        ],
    }
    made = wf.published(client, spec)
    started = wf.start(client, made["definition"]["workflow_key"],
                       {"instructions": "Ship the release notes.", "title": "Ship it"},
                       start_anyway=["model", "repository", "github"])
    assert started.status_code == 200, started.text
    run_id = started.json()["run"]["id"]

    # Signed from the first moment: the person who started it is the root.
    chain = client.get(f"/v1/workflow-runs/{run_id}/chain", params={"project_id": wf.PROJECT})
    assert chain.status_code == 200, chain.text
    assert chain.json()["accountable"] == "ceo"
    assert [a["kind"] for a in chain.json()["acts"]] == ["run"]

    # review (named qa) and pick (BOSS's pick) run; the gate then waits on a person.
    for _ in range(4):
        wf.tick(client)
    view = wf.run_view(client, run_id)
    assert wf.statuses(view)["gate"] == "waiting", wf.statuses(view)
    # The gate waits on YOU, not on pick_b: the agent whose finished work it holds is never
    # drawn "needs you" for it (agents-home D4) — the wait is the operator's.
    assert _live(client).get(pick_b) != "needs_you"
    etag = client.get("/v1/agents/live", params={"project_id": wf.PROJECT}).headers["etag"]
    unchanged = client.get("/v1/agents/live", params={"project_id": wf.PROJECT},
                           headers={"If-None-Match": etag})
    assert unchanged.status_code == 304

    _decide(client, wf.steps_of(view)["gate"]["decision_id"])
    for _ in range(4):
        wf.tick(client)
    assert pick_b not in _live(client), "an answered gate still says it needs you"
    assert build.get("run_id"), wf.statuses(wf.run_view(client, run_id))
    assert wf.statuses(wf.run_view(client, run_id))["build"] == "waiting"

    _the_build_runs_its_stages(engine, build["run_id"])
    for _ in range(3):
        wf.tick(client)
    view = wf.run_view(client, run_id)
    assert view["status"] == "completed", wf.statuses(view)

    # Read by the person who ran it: their own edges say "you".
    chain = client.get(f"/v1/workflow-runs/{run_id}/chain",
                       params={"project_id": wf.PROJECT, "viewer": "ceo"}).json()
    acts = chain["acts"]
    by_id = {a["act_id"]: a for a in acts}

    # ONE CONNECTED TREE: one root, every other act's parent is in it.
    roots = [a for a in acts if not a["parent_act_id"]]
    assert len(roots) == 1 and roots[0]["kind"] == "run"
    assert roots[0]["handed_by"] == {"kind": "person", "id": "ceo"}
    for act in acts[1:]:
        assert act["parent_act_id"] in by_id, act
        assert act["root_act_id"] == roots[0]["act_id"]
    assert all(a["status"] in ("complete", "skipped") for a in acts), [
        (a["label"], a["status"]) for a in acts]

    steps = {a["links"]["step_id"]: a for a in acts if a["kind"] == "step"}
    assert set(steps) == {"review", "pick", "gate", "spend", "build"}
    # The parent is the step whose completion released this one, resolved from the edges.
    assert steps["review"]["parent_act_id"] == roots[0]["act_id"]
    assert steps["pick"]["parent_act_id"] == steps["review"]["act_id"]
    assert steps["gate"]["parent_act_id"] == steps["pick"]["act_id"]
    assert steps["spend"]["parent_act_id"] == steps["gate"]["act_id"]
    assert steps["build"]["parent_act_id"] == steps["spend"]["act_id"]

    # TRUTHFUL EDGES, in the §9 words, from the API's own label.
    assert (steps["review"]["role_key"], steps["review"]["edge_label"]) == (
        "qa", "Named in workflow")
    assert (steps["pick"]["role_key"], steps["pick"]["edge_label"]) == (
        pick_b, "Picked by BOSS over 1")
    assert sorted(steps["pick"]["considered"]) == sorted([pick_a, pick_b])
    assert (steps["spend"]["role_key"], steps["spend"]["edge_label"]) == (None, "Ran as you")
    assert (steps["build"]["role_key"], steps["build"]["edge_label"]) == (
        engineer, "Named in workflow")
    approvals = [a for a in acts if a["kind"] == "approval"]
    assert len(approvals) == 1
    assert approvals[0]["parent_act_id"] == steps["gate"]["act_id"]
    assert (approvals[0]["edge_label"], approvals[0]["handed_by"]) == (
        "Approved by you", {"kind": "person", "id": "ceo"})
    assert not any(a["how"] == "asked_by_agent" for a in acts)
    # A teammate reading the same chain reads whose they were — never their own "you".
    theirs = {a["act_id"]: a["edge_label"] for a in client.get(
        f"/v1/workflow-runs/{run_id}/chain",
        params={"project_id": wf.PROJECT, "viewer": "teammate"}).json()["acts"]}
    assert theirs[steps["spend"]["act_id"]] == "Ran as ceo"
    assert theirs[approvals[0]["act_id"]] == "Approved by ceo"
    assert theirs[roots[0]["act_id"]] == "Started by ceo"
    assert theirs[steps["review"]["act_id"]] == "Named in workflow"

    # FIVE STAGES under the build, the engineer credited with the one it builds.
    stages = [a for a in acts if a["kind"] == "stage"]
    assert [s["links"]["step_id"] for s in stages] == list(STAGES)
    assert all(s["parent_act_id"] == steps["build"]["act_id"] for s in stages)
    assert all(s["edge_label"] == "Next stage" for s in stages)
    credited = {s["links"]["step_id"]: s["role_key"] for s in stages}
    assert credited == {"plan": "chief_architect", "write_tests": "qa",
                        "implement": engineer, "review_code": "adversarial_reviewer",
                        "security_check": "security_reviewer"}

    # COST ON THE RIGHT AGENT: every model call in the run carries its act, and its role is
    # the act's role; each stage's cost is its own calls.
    with engine.begin() as c:
        calls = c.execute(text(
            "SELECT c.stage, c.role, c.act_id, a.role_key FROM llm_calls c "
            "LEFT JOIN agent_acts a ON a.act_id = c.act_id WHERE c.code_task_run_id = :r"),
            {"r": build["run_id"]}).mappings().all()
    assert len(calls) == len(STAGES)
    for call in calls:
        assert call["act_id"], call
        assert call["role"] == call["role_key"], call
    for stage in stages:
        assert (stage["tokens_in"], stage["tokens_out"], stage["cost_usd"]) == (100, 40, 0.0025)
    assert chain["totals"]["cost"] >= 5 * 0.0025
    assert chain["totals"]["steps"] == 5

    # Credits name every agent that did something, in chain order, and the person.
    credited_roles = [c["role_key"] for c in chain["credits"]]
    assert credited_roles[0] is None  # the person who started it
    for role in ("qa", pick_b, engineer, "chief_architect", "adversarial_reviewer"):
        assert role in credited_roles

    # The step output carries its signature next to ran_as.
    review_out = wf.steps_of(wf.run_view(client, run_id))["review"]["output"]
    assert review_out["act_id"] == steps["review"]["act_id"]
    assert review_out["ran_as"]["act_id"] == steps["review"]["act_id"]

    # The agent's own feed lists the step, with who handed it over.
    feed = client.get("/v1/agents/qa/acts", params={"project_id": wf.PROJECT})
    assert feed.status_code == 200, feed.text
    mine = {a["act_id"]: a for a in feed.json()["items"]}
    assert steps["review"]["act_id"] in mine
    assert mine[steps["review"]["act_id"]]["from"]["kind"] == "person"
    one = client.get(f"/v1/acts/{steps['build']['act_id']}", params={"project_id": wf.PROJECT})
    assert one.status_code == 200, one.text
    assert one.json()["children"]["by_kind"] == {"stage": 5}

    # Lineage: qa hands to pick_b, picked by BOSS; the engineer was handed work by BOSS's run.
    graph = client.get(f"/v1/agents/{pick_b}/lineage", params={"project_id": wf.PROJECT}).json()
    edges = {(e["from"], e["to"]): e for e in graph["edges"]}
    assert edges[("agent:qa", f"agent:{pick_b}")]["label"] == "Picked by BOSS over 1"

    # Nobody is working any more.
    assert _live(client) == {}

    # The PR body names who made it.
    with engine.begin() as c:
        from boss_engine.services.agent_acts import pr_trailers

        footer = pr_trailers(c, build["run_id"])
    assert f"BOSS-Agent: " in footer and f"({engineer}" in footer
    assert f"BOSS-Act: {steps['build']['act_id']}" in footer
    assert f"BOSS-Run: {run_id}" in footer
    assert "accountable: ceo" in footer
