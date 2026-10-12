"""A conversation is part of the work, not beside it (Agent World integration, spec §0.2 A, §2).

Three streams meet here, and each test crosses them through the real HTTP API:

    talk   a 1:1 talk with an agent (S-converse) is signed work (S-acts): it is in the agent's
           Work feed and its lineage, said in the §9 words, with the cost on that agent.
    room   a room of SMEs sums up, its start card names who runs each step, and one press
           starts a run that RUNS AS THOSE AGENTS (the runtime reads the card: S-converse +
           step_agents), so the run's chain shows the room's agents on its steps.
    ask    asking an agent about a step it ran opens a talk whose first reply hangs under that
           step's act, inside the run's own chain.

Only the models are stood in for: the conversation's (`agent_converse.make_adapter`) and the
agent step loop's (`agent_loop.make_chat_adapter`). Postgres, the start door, readiness, the
budget, the tick and the ledger are the real ones.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests import workflow_management_support as wf
from apps.api.tests.test_talking_with_agents import (  # noqa: F401 — fixtures used by name
    ACTOR, PROJECT, _events, _open, _say,
)
from apps.api.tests import test_talking_with_agents as conversation_fixtures

# Export the existing pytest fixtures without shadowing imported names.
model = conversation_fixtures.model
seated = conversation_fixtures.seated


def _feed(client, role_key: str) -> list[dict]:
    got = client.get(f"/v1/agents/{role_key}/acts",
                     params={"project_id": PROJECT, "viewer": ACTOR})
    assert got.status_code == 200, got.text
    return got.json()["items"]


def _lineage(client, role_key: str) -> dict:
    got = client.get(f"/v1/agents/{role_key}/lineage",
                     params={"project_id": PROJECT, "viewer": ACTOR})
    assert got.status_code == 200, got.text
    return got.json()


def _chain(client, run_id: str) -> list[dict]:
    # This lineage GET can repair rows, so a session read must prove same-origin.
    refused = client.get(f"/v1/workflow-runs/{run_id}/chain",
                         params={"project_id": PROJECT, "viewer": ACTOR})
    assert refused.status_code == 403 and refused.json()["error"] == "csrf_failed"
    got = client.get(f"/v1/workflow-runs/{run_id}/chain",
                     params={"project_id": PROJECT, "viewer": ACTOR},
                     headers={"sec-fetch-site": "same-origin"})
    assert got.status_code == 200, got.text
    return got.json()["acts"]


def _worker_ticks(client, engine, count: int, monkeypatch) -> None:
    # Start/Ask remain the real human's actions. Executing the worker protocol
    # requires its own fixed system identity; even an Owner must be refused.
    refused = client.post("/v1/delivery-workflows/run-capabilities", json={"project_id": PROJECT})
    assert refused.status_code == 403 and refused.json()["permission"] == "worker:lease"
    from apps.api.tests.identity_fixtures import IdentityWorld
    from boss_engine.identity.principal import (
        AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus,
    )
    with engine.connect() as c:
        row = c.execute(text("SELECT p.workspace_id,w.org_id FROM projects p JOIN workspaces w "
                             "ON w.id=p.workspace_id WHERE p.id=:p"), {"p": PROJECT}).mappings().one()
    world = IdentityWorld(engine, row["org_id"], row["workspace_id"], PROJECT, PROJECT)
    worker = AuthContext(Principal("sys_worker", PrincipalKind.SYSTEM, world.org,
                                   PrincipalStatus.ACTIVE, "Owned workflow worker"), AuthMethod.SYSTEM)
    # The old unauthenticated fixture never minted delegated run tokens. Give
    # this owned DB the genuine vault/signing bootstrap used by identity tests.
    import base64
    from boss_engine.identity import signing_keys, vault
    with monkeypatch.context() as boot_env:
        boot_env.delenv("BOSS_MASTER_KEY_FILE", raising=False)
        boot_env.setenv("BOSS_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
        vault.reset_master_key_cache()
        signing_keys.clear_cache()
        try:
            with engine.begin() as c:
                vault.check_master_key(db=c)
                signing_keys.provision(org_id=world.org, db=c)
            signing_keys.clear_cache()
            with engine.begin() as c:
                signing_keys.require_keys(org_id=world.org, db=c)
            with world.acting_as(worker):
                for _ in range(count):
                    wf.tick(client)
        finally:
            signing_keys.clear_cache()
            vault.reset_master_key_cache()


def test_talking_to_an_agent_is_in_its_work_and_its_lineage(client, engine, model, seated):
    conv = _open(client, "talk", ["qa"], context={"from": "passport"})
    said = _say(client, conv["conversation_id"], "What would you test first?")
    act_id = next(e for e in said if e["type"] == "turn_start")["act_id"]
    end = next(e for e in said if e["type"] == "turn_end")
    assert end["status"] == "complete"

    # In the agent's own Work feed, in the §9 words, read by the person who asked.
    mine = {a["act_id"]: a for a in _feed(client, "qa")}
    assert act_id in mine
    act = mine[act_id]
    assert (act["kind"], act["how"], act["edge_label"]) == ("chat", "asked_by_person",
                                                           "Asked by you")
    assert act["handed_by"] == {"kind": "person", "id": ACTOR}
    assert act["status"] == "complete"
    # Its cost is counted once: the reply's llm_calls row is the act's spend (the stand-in
    # charges 40 tokens out and 1500 micros per reply), never the caller's copy added on top.
    assert act["tokens_out"] == 40 and abs(act["cost_usd"] - 0.0015) < 1e-9

    # In its lineage: the person who asked hands it work, said the same way.
    graph = _lineage(client, "qa")
    into_qa = [e for e in graph["edges"] if e["to"] == "agent:qa"]
    people = {n["id"] for n in graph["nodes"] if n["kind"] == "person"}
    assert any(e["from"] in people and e["how_top"] == "asked_by_person" for e in into_qa), graph
    # Never "asked by an agent" from a conversation (§2.3).
    assert all(e["how_top"] != "asked_by_agent" for e in graph["edges"])


def test_a_room_starts_a_run_that_runs_as_the_room_and_asking_about_a_step_is_in_its_chain(
        client, engine, model, seated, monkeypatch):
    wf.install_model(monkeypatch)
    # Two agent steps that name nobody: Auto, until a start card names someone.
    spec = {
        "entry_step": "fix",
        "input_schema": {"type": "object", "required": ["instructions"],
                         "properties": {"instructions": {"type": "string",
                                                         "title": "What to do"}}},
        "steps": [wf.agent_step("fix", nxt=("verify",)), wf.agent_step("verify")],
    }
    workflow_key = wf.published(client, spec)["definition"]["workflow_key"]

    room = _open(client, "room", seated)
    cid = room["conversation_id"]
    round_one = _say(client, cid, "Should we ship the new login flow this week?")
    assert [e["role_key"] for e in round_one if e["type"] == "turn_start"] == seated

    model.summary = {
        "text": "Fix session fixation first, then verify it.",
        "decisions": [{"text": "Fix before shipping", "for": ["security_reviewer", "qa"],
                       "against": []}],
        "next_actions": [{
            "text": "Fix and verify session fixation", "owner_role_key": "security_reviewer",
            "kind": "start", "proposal": {
                "workflow_key": workflow_key, "title": "Session fix",
                "directive": "Rotate the session id on login.",
                "inputs": {"instructions": "Rotate the session id on login."},
                # The room names who runs each step, from its own seats.
                "agents": {"fix": "security_reviewer", "verify": "qa"},
                "why": "Two steps, two owners."}}]}
    summed = _events(client.post(f"/v1/conversations/{cid}/summary",
                                 json={"project_id": PROJECT, "actor": ACTOR}))
    card = next(e for e in summed if e["type"] == "summary")["summary"]["next_actions"][0][
        "proposal"]
    # The card promises exactly who the run will use.
    assert {a["step_id"]: a["role_key"] for a in card["agents"]} == {
        "fix": "security_reviewer", "verify": "qa"}

    started = client.post(f"/v1/conversations/{cid}/start", json={
        "project_id": PROJECT, "actor": ACTOR, "proposal_id": card["proposal_id"],
        "start_anyway": ["model"]})
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]
    assert run_id

    _worker_ticks(client, engine, 4, monkeypatch)
    view = wf.run_view(client, run_id)
    assert view["status"] == "completed", wf.statuses(view)

    # THE RUN RAN AS THE ROOM: each step's agent is the one the card named, and says why.
    steps = wf.steps_of(view)
    for step_id, role_key in (("fix", "security_reviewer"), ("verify", "qa")):
        ran_as = steps[step_id]["output"]["ran_as"]
        assert (ran_as["agent"], ran_as["how"]) == (role_key, "named"), ran_as
        assert ran_as["why"] == "named on the start card you pressed"

    acts = _chain(client, run_id)
    signed = {a["links"]["step_id"]: a for a in acts if a["kind"] == "step"}
    assert (signed["fix"]["role_key"], signed["verify"]["role_key"]) == ("security_reviewer",
                                                                        "qa")
    assert signed["fix"]["edge_label"] == "Named in workflow"

    # ASK THE AGENT ABOUT ITS STEP: the talk carries the step's act, and the first reply is
    # signed under it — inside the run's own chain, not beside it.
    step_act = signed["verify"]
    talk = _open(client, "talk", ["qa"], context={
        "from": "run_step", "act_id": step_act["act_id"], "workflow_run_id": run_id,
        "step_id": "verify"})
    assert talk["context"]["act_id"] == step_act["act_id"]
    asked = _say(client, talk["conversation_id"], "What did you do here, and why?")
    reply_act = next(e for e in asked if e["type"] == "turn_start")["act_id"]
    by_id = {a["act_id"]: a for a in _chain(client, run_id)}
    assert reply_act in by_id, "the talk about a step is not in the run's chain"
    assert by_id[reply_act]["parent_act_id"] == step_act["act_id"]
    assert (by_id[reply_act]["kind"], by_id[reply_act]["role_key"]) == ("chat", "qa")
    with engine.begin() as c:
        root = c.execute(text("SELECT root_act_id FROM agent_acts WHERE act_id = :a"),
                         {"a": step_act["act_id"]}).scalar()
    assert by_id[reply_act]["root_act_id"] == root


def test_a_start_card_never_replaces_an_agent_the_workflow_names(client, engine, model, seated,
                                                                 monkeypatch):
    """The card can fill an Auto step; it cannot overrule a choice made when the workflow was
    published — and the runtime holds that line even if a card's input says otherwise."""
    wf.install_model(monkeypatch)
    spec = {
        "entry_step": "fix",
        "input_schema": {"type": "object", "required": ["instructions"],
                         "properties": {"instructions": {"type": "string"}}},
        "steps": [wf.agent_step("fix", captain="security_reviewer")],
    }
    workflow_key = wf.published(client, spec)["definition"]["workflow_key"]
    run = wf.start(client, workflow_key, {"instructions": "Rotate the session id.",
                                          "step_agents": {"fix": "qa"}},
                   start_anyway=["model"])
    assert run.status_code == 200, run.text
    run_id = run.json()["run"]["id"]
    _worker_ticks(client, engine, 3, monkeypatch)
    ran_as = wf.steps_of(wf.run_view(client, run_id))["fix"]["output"]["ran_as"]
    assert (ran_as["agent"], ran_as["why"]) == ("security_reviewer", "named on the step")
