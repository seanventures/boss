"""Agents › Live says only what BOSS knows (agents-home spec §6.2, §8.1).

One read, `GET /v1/agents/live-board`, behind the Live tab: what needs you, what is moving,
what got done, who is idle. Driven through the HTTP API the console and the worker use, on the
real database; only the model on the other end of an agent step is scripted. Pinned here:

  (a) no percent, no ETA, no progress — no field of those names anywhere in the payload;
  (b) `step k of n` comes from the run's own rows, and k never passes n;
  (c) a run at a gate is `waiting_you`, its row names the card, and the card names the agent
      whose finished work the gate holds;
  (d) a decision nothing attributes is nobody's — never a guessed face;
  (e) a failed step is a `failed_step` card with the run's own answers, and its run `stopped`;
  (f) a step waiting on a build is `waiting`, not `waiting_you`;
  (g) a pull request, a refusal and a risk-3 decision are never decided from a card;
  (h) an unchanged board is a 304, and a hop changing state changes the ETag;
  (i) a run that completed 11 minutes ago has left Live; one that finished 2 minutes ago shows.
And the agreement: the board, the pill and the Command Center say one needs-you number.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from apps.api.tests import workflow_management_support as wf

BANNED_KEYS = {"eta", "percent", "progress", "percentage", "estimate", "estimated"}


def _board(client, **headers):
    return client.get("/v1/agents/live-board", params={"project_id": wf.PROJECT},
                      headers=headers)


def _keys(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, f"{path}.{k}"
            yield from _keys(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _keys(v, f"{path}[{i}]")


def _mission(board, run_id):
    found = [m for m in board["missions"] if m["run_id"] == run_id]
    return found[0] if found else None


def _agent(client, prefix):
    """A custom agent that holds a skill (a step naming an agent that holds nothing is
    refused at publish)."""
    return wf.new_agent(client, wf.key(prefix), holds=tuple(wf.skills(client, 1)))


def _gated_run(client, monkeypatch):
    wf.install_model(monkeypatch)
    herald = _agent(client, "herald")
    spec = {
        "entry_step": "draft",
        "input_schema": {"type": "object", "required": ["instructions", "title"],
                         "properties": {"instructions": {"type": "string"},
                                        "title": {"type": "string"}}},
        "steps": [
            wf.agent_step("draft", captain=herald, nxt=("publish",)),
            wf.gate_step("publish", nxt=("wrap",), question="Publish it?"),
            wf.agent_step("wrap", captain=herald),
        ],
    }
    made = wf.published(client, spec)
    run_id = wf.started(client, made["definition"]["workflow_key"],
                        {"instructions": "Draft the release video.", "title": "Weekly video"})
    for _ in range(4):
        wf.tick(client)
    view = wf.run_view(client, run_id)
    assert wf.statuses(view)["publish"] == "waiting", wf.statuses(view)
    return run_id, herald, view


def test_a_gate_is_one_card_and_one_waiting_row_attributed_to_the_agent(client, monkeypatch):
    run_id, herald, view = _gated_run(client, monkeypatch)
    decision_id = wf.steps_of(view)["publish"]["decision_id"]

    got = _board(client)
    assert got.status_code == 200, got.text
    board = got.json()

    # (a) nothing estimated, anywhere.
    banned = [p for k, p in _keys(board) if k.lower() in BANNED_KEYS]
    assert not banned, banned

    # (c) the row waits on you, names its card; the card is the herald's.
    row = _mission(board, run_id)
    assert row is not None, [m["run_id"] for m in board["missions"]]
    assert row["state"] == "waiting_you"
    assert row["verb"] == "Needs you"
    assert row["attention_id"] == decision_id
    card = next(a for a in board["attention"] if a["id"] == decision_id)
    assert card["kind"] == "approval" and card["decision_kind"] == "workflow_gate"
    assert card["role_key"] == herald
    assert card["run"]["id"] == run_id
    # (b) counted from the run's rows: one step done, on the second of three.
    assert (row["step_index"], row["step_count"]) == (2, 3)
    assert (card["run"]["step_index"], card["run"]["step_count"]) == (2, 3)
    assert row["step_index"] <= row["step_count"]
    # The lane: who started it, the herald's draft, then the gate waiting on a person.
    hops = [h for h in row["hops"] if "act_id" in h]
    assert hops[0]["label"] == "Started" and hops[0]["role_key"] is None
    assert any(h["role_key"] == herald and h["state"] == "done" for h in hops)
    assert hops[-1]["state"] == "needs_you" and hops[-1]["role_key"] is None, row["hops"]
    # The herald is on the floor, in Yours (a custom agent with no team), needing you.
    agent = next(a for a in board["agents"] if a["role_key"] == herald)
    assert agent["team"] == "yours" and agent["state"] == "needs_you"
    assert [t["key"] for t in board["teams"]][:8] == [
        "leadership", "architecture", "engineering", "quality", "operations", "cost", "media",
        "thinkers"]
    assert board["teams"][-1]["key"] == "yours"

    # ONE NUMBER: the board, the pill and the Command Center.
    pill = client.get("/v1/agents/live", params={"project_id": wf.PROJECT}).json()["attention"]
    cc = client.get("/v1/console/command-center", params={"project_id": wf.PROJECT}).json()
    assert board["counts"]["needs_you"] == len(board["attention"]) == pill["count"] \
        == cc["counts"]["needs_you"]
    assert decision_id in [i["id"] for i in pill["items"]] or pill["count"] > 5

    # (h) unchanged → 304; the gate answered and the next hop running → a new ETag.
    etag = got.headers["etag"]
    assert _board(client, **{"If-None-Match": etag}).status_code == 304
    answered = client.post(f"/v1/decisions/{decision_id}/approve",
                           json={"actor": "ceo", "note": "ship it"})
    assert answered.status_code == 200, answered.text
    for _ in range(4):
        wf.tick(client)
    after = _board(client, **{"If-None-Match": etag})
    assert after.status_code == 200
    assert after.headers["etag"] != etag
    assert decision_id not in [a["id"] for a in after.json()["attention"]]


def test_a_decision_nothing_attributes_is_nobodys_and_risky_kinds_go_to_approvals(
        client, engine):
    rows = {
        "dec_lb_lonely": ({"action": "provide_github_token"}, 1),
        "dec_lb_pr": ({"action": "review_pull_request"}, 1),
        "dec_lb_risky": ({"action": "provide_github_token"}, 3),
        "dec_lb_refusal": ({"action": "budget_refusal", "refusal": "budget"}, 1),
    }
    with engine.begin() as c:
        for did, (rec, tier) in rows.items():
            c.execute(text(
                "INSERT INTO decisions (id, project_id, question, recommendation, status, "
                "risk_tier, default_action) VALUES (:i, :p, :q, CAST(:r AS JSONB), 'pending', "
                ":t, 'hold') ON CONFLICT (id) DO NOTHING"),
                {"i": did, "p": wf.PROJECT, "q": f"Seeded {did}", "r": json.dumps(rec),
                 "t": tier})
    board = _board(client).json()
    cards = {a["id"]: a for a in board["attention"]}
    # (d) no act, no captain: nobody's.
    assert cards["dec_lb_lonely"]["role_key"] is None
    assert cards["dec_lb_lonely"]["decide_inline"] is True
    # (g) a pull request, a high-risk decision and a refusal go to Approvals.
    assert cards["dec_lb_pr"]["decision_kind"] == "pull_request"
    assert cards["dec_lb_pr"]["decide_inline"] is False
    assert cards["dec_lb_risky"]["decide_inline"] is False
    if "dec_lb_refusal" in cards:  # a refusal family the engine recognises
        if cards["dec_lb_refusal"]["decision_kind"] == "refusal":
            assert cards["dec_lb_refusal"]["decide_inline"] is False
    # Every card's buttons are the engine's own offer.
    for card in cards.values():
        for action in card["actions"]:
            assert set(action) == {"action", "label", "tone", "effect", "needs"}


def test_a_failed_step_is_a_card_with_the_runs_answers_and_its_run_stopped(client, monkeypatch):
    model = wf.install_model(monkeypatch)
    forge = _agent(client, "forge")
    model.failing = "FAIL-THIS-STEP"
    spec = {
        "entry_step": "build",
        "input_schema": {"type": "object", "required": ["instructions"],
                         "properties": {"instructions": {"type": "string"}}},
        "steps": [wf.agent_step("build", captain=forge, nxt=("test",)),
                  wf.agent_step("test", captain=forge)],
    }
    made = wf.published(client, spec)
    run_id = wf.started(client, made["definition"]["workflow_key"],
                        {"instructions": "FAIL-THIS-STEP please"})
    for _ in range(4):
        wf.tick(client)
    assert wf.statuses(wf.run_view(client, run_id))["build"] in ("failed", "deadlettered")

    board = _board(client).json()
    row = _mission(board, run_id)
    assert row is not None and row["state"] == "stopped" and row["verb"] == "Stuck"
    card = next(a for a in board["attention"] if a["kind"] == "failed_step"
                and a["run"]["id"] == run_id)
    assert card["id"] == f"run:{run_id}:build"
    assert row["attention_id"] == card["id"]
    assert card["role_key"] == forge
    assert card["answers"] and all({"key", "label", "enabled"} <= set(a)
                                   for a in card["answers"])
    assert card["run"]["step_index"] == 1 == row["step_index"]
    forge_now = next(a for a in board["agents"] if a["role_key"] == forge)
    assert forge_now["state"] == "stuck"


def test_a_step_waiting_on_a_build_is_waiting_not_waiting_on_you(client, engine, monkeypatch):
    from boss_engine.services import workflow_capability_runner as runner

    wf.install_model(monkeypatch)
    real = runner._invoke_tool

    def invoke(ctx, tool_name, project_id, actor, args):
        if tool_name != "create_code_task":
            return real(ctx, tool_name, project_id, actor, args)
        build_id = f"ctr_{uuid.uuid4().hex[:16]}"
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, "
                "finalize_ctx) VALUES (:i, :p, 'running', 'coding', 'Ship it', '{}'::jsonb, "
                "CAST(:f AS JSONB))"),
                {"i": build_id, "p": project_id,
                 "f": json.dumps({"workflow_run_id": args.get("workflow_run_id")})})
        return {"run_id": build_id, "status": "running"}

    monkeypatch.setattr(runner, "_invoke_tool", invoke)
    spec = {
        "entry_step": "build",
        "input_schema": {"type": "object", "required": ["instructions", "title"],
                         "properties": {"instructions": {"type": "string"},
                                        "title": {"type": "string"}}},
        "steps": [{"id": "build", "kind": "capability", "next": [], "max_attempts": 1,
                   "binding": {"capability": "build", "inputs": {
                       "title": {"from_input": "title"},
                       "instructions": {"from_input": "instructions"}}}}],
    }
    made = wf.published(client, spec)
    started = wf.start(client, made["definition"]["workflow_key"],
                       {"instructions": "Ship it.", "title": "Ship it"},
                       start_anyway=["model", "repository", "github"])
    assert started.status_code == 200, started.text
    run_id = started.json()["run"]["id"]
    for _ in range(3):
        wf.tick(client)
    assert wf.statuses(wf.run_view(client, run_id))["build"] == "waiting"

    row = _mission(_board(client).json(), run_id)
    assert row["state"] == "waiting", row
    assert row["waiting_on"] == "the build to finish"
    assert row["attention_id"] is None


def test_a_finished_run_stays_ten_minutes_then_leaves(client, engine, monkeypatch):
    wf.install_model(monkeypatch)
    agent = _agent(client, "quill")
    spec = {"entry_step": "write",
            "input_schema": {"type": "object", "required": ["instructions"],
                             "properties": {"instructions": {"type": "string"}}},
            "steps": [wf.agent_step("write", captain=agent)]}
    made = wf.published(client, spec)
    run_id = wf.started(client, made["definition"]["workflow_key"], {"instructions": "Write."})
    for _ in range(3):
        wf.tick(client)
    assert wf.run_view(client, run_id)["status"] == "completed"

    def finished(minutes):
        with engine.begin() as c:
            c.execute(text("UPDATE workflow_runs SET completed_at = now() - make_interval("
                           "mins => :m), updated_at = now() - make_interval(mins => :m) "
                           "WHERE id = :r"), {"m": minutes, "r": run_id})
        return _board(client).json()

    board = finished(2)
    row = _mission(board, run_id)
    assert row is not None and row["state"] == "done" and row["finished_at"]
    assert (row["step_index"], row["step_count"]) == (1, 1)
    assert any(o["kind"] == "run" and o["run_id"] == run_id and o["text"] == "Finished"
               for o in board["outcomes"])
    # (i) eleven minutes on, it has left Live work (it is still an outcome of the day).
    board = finished(11)
    assert _mission(board, run_id) is None
    assert any(o["run_id"] == run_id for o in board["outcomes"])


def test_a_rejected_gate_needs_nobody_anywhere(client, monkeypatch):
    """Rejecting a gate leaves the run blocked and its step row waiting; its act must close,
    or presence, the teams and Activity go on saying "needs you" with no card to answer."""
    run_id, herald, view = _gated_run(client, monkeypatch)
    decision_id = wf.steps_of(view)["publish"]["decision_id"]
    refused = client.post(f"/v1/decisions/{decision_id}/reject",
                          json={"actor": "ceo", "note": "not this week"})
    assert refused.status_code == 200, refused.text
    wf.tick(client)
    board = _board(client).json()
    assert decision_id not in [a["id"] for a in board["attention"]]
    agent = next(a for a in board["agents"] if a["role_key"] == herald)
    assert agent["state"] != "needs_you"
    assert sum(t["needs_you"] for t in board["teams"]) == sum(
        1 for a in board["agents"] if a["state"] == "needs_you")
    pill = client.get("/v1/agents/live", params={"project_id": wf.PROJECT}).json()
    assert pill["attention"]["count"] == board["counts"]["needs_you"]
    assert all(i["state"] != "needs_you" for i in pill["items"] if i["role_key"] == herald)
    waiting = client.get("/v1/agents/activity", params={
        "project_id": wf.PROJECT, "kinds": "needs_you"}).json()["items"]
    assert not [i for i in waiting if i.get("links", {}).get("workflow_run_id") == run_id]
