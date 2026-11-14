"""A step says what is wanted. The agent works out how.

WHAT THIS REPLACES, AND WHY IT HAD TO BE REPLACED
--------------------------------------------------
Every other step kind binds ONE skill. That is right for a pipeline whose shape is known and
wrong for work described in a sentence: "keep 0:28 to 1:48, make it portrait, put the ending
slide on the end" is three operations this time and nine the next.

The consequence was a product growing a special case per customer. An operator wanting an ending
slide could not have one until the edit-plan vocabulary grew a word for it — a commit against the
product for one customer's sentence. The next asks for a fade. That does not end.

So the step carries the INSTRUCTION and the agent carries the CAPABILITY. Nothing in
`agent_steps` mentions video, or any other domain.

WHAT MAKES IT SHIPPABLE RATHER THAN A SHELL
-------------------------------------------
The bounds, which are the things these tests are mostly about:
  * the tools are exactly `role_skills` — the grant, not the role spec's prose
  * no native tools at all: no shell, no database, no repository
  * the risk floor still suspends for a person
  * a step cap, and a run that hits it says so
"""
import os

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import agent_steps

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "boss-dogfood"


@pytest.fixture()
def db():
    """One transaction, rolled back: a test that empties an agent or rewrites its spec leaves
    the shared project as it found it for the tests after it."""
    engine = create_engine(DATABASE_URL)
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            yield connection
        finally:
            outer.rollback()
    engine.dispose()


def _holds_nothing(db, role_key: str) -> None:
    """Take everything an agent holds: its skills AND its MCP servers (an MCP grant is a
    holding — agent_steps.holdings — and the seed gives agents servers)."""
    for table in ("role_skills", "role_mcp_servers"):
        db.execute(text(f"DELETE FROM {table} WHERE project_id=:p AND role_key=:r"),
                   {"p": PROJECT, "r": role_key})


# --------------------------------------------------------------------------- #
# The bound: what an agent may reach
# --------------------------------------------------------------------------- #
@needs_db
def test_the_tools_are_what_the_agent_HOLDS(db):
    """The grant, not the spec.

    `skill_registry.resolve_for_task` filters by the role spec's `allowed_skills` and unions in
    system skills and module defaults besides. For a step handing an instruction to a named agent
    that is the wrong answer: the bound an operator set by choosing skills has to be the bound
    that runs, or choosing skills was theatre.
    """
    db.execute(text("DELETE FROM role_skills WHERE project_id=:p AND role_key='qa'"),
               {"p": PROJECT})
    assert agent_steps.held_skills(db, Settings(), PROJECT, "qa") == []

    from boss_engine.services.skill_definitions import (
        assignable_catalogue, assign_skill_to_role)

    # FROM THE CATALOGUE, not from what some other agent happens to hold. A test that borrows
    # another agent's grant fails when an unrelated test changes that agent, which is how this
    # one first failed.
    available = assignable_catalogue(db, PROJECT)
    assert available, "this project has no skills at all; the fixture changed"
    one = available[0]["skill_id"]
    assign_skill_to_role(db, project_id=PROJECT, actor="test", role_key="qa", skill_id=one)

    held = agent_steps.held_skills(db, Settings(), PROJECT, "qa")
    assert [s["id"] for s in held] == [one]
    # THE CALLER IS THE AGENT. That is what makes the grant the thing being cashed in, rather
    # than an anonymous runner borrowing somebody's permission.
    assert held[0]["caller"] == "qa"


@needs_db
def test_an_agent_holding_nothing_is_refused_before_a_model_is_called(db):
    """Refused HERE, not at the first tool call.

    A step given to an empty agent would otherwise spend a model call to discover it has no
    tools, then report success having done nothing.
    """
    _holds_nothing(db, "qa")
    with pytest.raises(agent_steps.AgentStepError, match="holds no skills"):
        agent_steps.run(db, Settings(), PROJECT, instructions="do something",
                        role_key="qa")


@needs_db
def test_an_agent_holding_nothing_is_not_offered_as_a_candidate(db):
    """It is not a candidate for anything: every call it made would be refused."""
    _holds_nothing(db, "qa")
    assert "qa" not in {c["role_key"] for c in agent_steps.candidates(db, Settings(), PROJECT)}


@needs_db
def test_a_skill_the_project_no_longer_has_is_not_offered_as_a_tool(db):
    """A grant left behind by a deleted skill is a tool nothing can execute.

    Offered, it costs the agent a whole turn to find out. Left out, the agent works with what is
    real — and the AGENT PAGE still shows the dead grant, which is where it belongs.
    """
    from boss_engine.services.core import make_id

    db.execute(
        text("INSERT INTO role_skills (id, project_id, role_key, skill_id, source, assigned_by) "
             "VALUES (:i, :p, 'qa', 'skill_that_left', 'builtin', 'test') "
             "ON CONFLICT DO NOTHING"),
        {"i": make_id("rsk"), "p": PROJECT})
    offered = {s["id"] for s in agent_steps.held_skills(db, Settings(), PROJECT, "qa")}
    assert "skill_that_left" not in offered


# --------------------------------------------------------------------------- #
# The brief: what the agent is told about itself
# --------------------------------------------------------------------------- #
@needs_db
def test_the_agents_own_boundaries_reach_it_as_rules(db):
    """A spec's `must_not` list is written in every role YAML and reached only the chat persona.

    An agent EXECUTING work is exactly where "must never publish without a person" belongs, and
    it is phrased as an instruction rather than described as data — a model reading
    "boundaries: [...]" treats it as a fact about itself, not as a rule.
    """
    spec = {"mission": "Edit video to a house standard.",
            "boundaries": {"must": ["verify a render plays"],
                           "must_not": ["publish anything"]},
            "output_contracts": ["RenderedVideo"]}
    import json

    db.execute(
        text("UPDATE roles SET spec = CAST(:s AS JSONB) WHERE project_id=:p AND role_key='qa'"),
        {"s": json.dumps(spec), "p": PROJECT})

    brief = agent_steps.agent_brief(db, PROJECT, "qa")
    assert "Edit video to a house standard." in brief
    assert "You MUST NEVER publish anything" in brief
    assert "You MUST verify a render plays" in brief
    assert "RenderedVideo" in brief
    # AND IT IS TOLD THAT ITS SKILLS ARE THE WHOLE OF WHAT IT CAN DO, so it reports a gap rather
    # than approximating with a tool meant for something else.
    assert "the whole of" in brief
    assert "say so plainly and stop" in brief
    # WHO IT IS comes from the persona compiler, disclosure line and all.
    from boss_engine.services.agent_world_types import AI_DISCLOSURE
    assert AI_DISCLOSURE in brief


# --------------------------------------------------------------------------- #
# Choosing the agent
# --------------------------------------------------------------------------- #
@needs_db
def test_one_candidate_needs_no_judgement(db):
    picked = agent_steps.choose_agent(db, Settings(), PROJECT,
                                     instructions="anything at all", only=["qa"])
    assert picked["role_key"] == "qa"
    assert picked["how"] == "only_candidate"


def test_choosing_without_a_model_says_what_to_do_rather_than_guessing(monkeypatch):
    """REWRITTEN FOR BOSSAPP-759. This pinned a word-overlap fallback ("a project with no model
    must still run a workflow"). The pick is now a turn through the one agent pipeline, and the
    operator's mandate is that a decision is a model's — "it cannot / should not work just on
    keywords". With no model, nobody is picked by shared words: the step says to name the agent
    on it (which needs no model) or to connect one."""
    from boss_engine.services import model_routing

    def no_model(*_a, **_k):
        raise RuntimeError("no model is connected")

    monkeypatch.setattr(model_routing, "project_chat_adapter", no_model)
    pool = [{"role_key": "video_editor", "mission": "cuts video", "holds": ["video.trim"]},
            {"role_key": "media_publisher", "mission": "publishes", "holds": ["publish_youtube"]}]
    with pytest.raises(agent_steps.AgentStepError) as failed:
        agent_steps.choose_agent(None, Settings(), PROJECT, pool=pool,
                                 instructions="publish the finished video to the channel")
    assert "Name the agent on the step" in str(failed.value)


@needs_db
def test_no_agent_holds_anything_says_what_to_do_about_it(db):
    """Narrowed to one empty agent rather than emptying the project.

    The first version of this test did `DELETE FROM role_skills WHERE project_id=:p`, which
    passed and then broke two unrelated tests that ran after it. A test that wrecks the state
    other tests read is a worse defect than the one it was checking for.
    """
    _holds_nothing(db, "qa")
    with pytest.raises(agent_steps.AgentStepError, match="give it the skills"):
        agent_steps.choose_agent(db, Settings(), PROJECT, instructions="do a thing",
                                 only=["qa"])


# --------------------------------------------------------------------------- #
# The bound, stated in the loop rather than trusted
# --------------------------------------------------------------------------- #
def test_the_loop_gives_a_bounded_run_no_native_tools():
    """No shell, no database, no repository — and this is the test that matters most.

    The ordinary loop hands every run `run_shell`, the project's Postgres and the repository,
    because it was written for the software factory. An agent step is the opposite case: the
    skills an operator gave the agent ARE the claim about its reach, and handing it a shell
    besides makes that claim false. On 2026-09-02 an agent's shell reached a production
    DATABASE_URL and cleared 204 tables.
    """
    import inspect

    from boss_engine.services import agent_loop

    # The assembly is the agent step's toolset (K4), shared by both loops.
    source = inspect.getsource(agent_loop.AgentStepToolSet)
    assert 'bounded = isinstance(finalize_ctx.get("agent_step"), dict)' in source
    assert "schemas = [] if bounded else" in source
    # REWRITTEN. This asserted a bounded run got NO MCP server at all ("granted by giving the
    # agent a skill that runs through it"), which left no way to give an agent an MCP server.
    # The rule it protected still holds — being REGISTERED in the project grants nothing — and
    # is now stated positively: a bounded run is offered exactly the servers its agent holds
    # (role_mcp_servers), never the project's list.
    mcp_choice = source.split("list_mcp_servers")[0][-300:]
    assert "_granted_mcp_servers(" in mcp_choice and "if bounded" in mcp_choice
    # The step's own MCP list (steps stream) NARROWS that set and never widens it: agent_steps.run
    # pins held-and-named servers into `mcp_server_ids`, and only those pinned ids are offered.
    # (Integration: the steps stream let a step name any registered server; one ownership model,
    # the agents stream's role_mcp_servers grant, is kept.)
    assert 'step.get("mcp_server_ids")' in inspect.getsource(agent_loop._granted_mcp_servers)


def test_a_bounded_run_resolves_its_skills_from_the_grant():
    import inspect

    from boss_engine.services import agent_loop

    source = inspect.getsource(agent_loop._skill_resolution_for_run)
    assert "held_skills(" in source, "it must read role_skills, not the role spec"
    assert '"on_demand": []' in source, "a granted skill is not something to discover"


def test_an_approved_skill_replaces_the_deferral_the_loop_recorded():
    """The tool message is what a later step reads. Leaving it as the deferral drops the result.

    Resume tells the model the outcome in a user message. The workflow does not read that
    message. It reads tool payloads. The skill a person just approved has to be one of them.
    """
    from boss_engine.services.agent_loop import _replace_deferred_tool_result

    messages = [
        {"role": "assistant", "content": ""},
        {"role": "tool", "tool_call_id": "call_old",
         "content": '{"status": "succeeded", "skill_id": "media.fetch_link", '
                    '"result": {"media_artifact_id": "art_src"}}'},
        {"role": "tool", "tool_call_id": "call_new",
         "content": '{"status": "deferred_for_approval", "decision_id": "dec_1"}'},
    ]
    _replace_deferred_tool_result(messages, {
        "status": "succeeded",
        "skill_id": "video.trim",
        "result": {"media_artifact_id": "art_final"},
    })
    import json

    kept = json.loads(messages[1]["content"])
    replaced = json.loads(messages[2]["content"])
    assert kept["result"]["media_artifact_id"] == "art_src"
    assert replaced["result"]["media_artifact_id"] == "art_final"
    assert replaced["status"] == "succeeded"


def test_the_step_cap_is_bounded():
    """An unbounded loop on a paid model is an invoice, not a feature."""
    assert 5 <= agent_steps.DEFAULT_MAX_STEPS <= 100


# --------------------------------------------------------------------------- #
# What the step produces, which is what makes it chainable
# --------------------------------------------------------------------------- #
@needs_db
def test_the_results_of_the_skills_become_the_steps_output(db):
    """LATER CALLS WIN, and the operator's whole run depends on it.

    An agent that trims, then reframes, then watermarks has produced three `media_artifact_id`s
    and the one the PUBLISH step wants is the last — the finished video, not the raw upload.
    Get this wrong and the run succeeds, the gate opens, a person approves, and YouTube receives
    the untrimmed original.

    EXERCISED against a real agent run rather than read off the source, because "the code says
    merged[key] = value" is not the same claim as "the right artifact comes out".
    """
    import json

    from sqlalchemy import text as sql

    from boss_engine.services.core import make_id

    run_id = make_id("arun")
    # The shape the loop writes: one tool message per skill call, newest last.
    messages = [
        {"role": "system", "content": "…"},
        {"role": "user", "content": "Keep 3:42 to 4:50, portrait, badge on top right."},
        {"role": "tool", "content": json.dumps({
            "status": "succeeded", "skill_id": "video.trim", "invocation_id": "skinv_1",
            "result": {"media_artifact_id": "art_TRIM", "duration_seconds": 68.0,
                       "width": 1280, "height": 720}})},
        {"role": "tool", "content": json.dumps({
            "status": "succeeded", "skill_id": "video.reframe", "invocation_id": "skinv_2",
            "result": {"media_artifact_id": "art_REFRAME", "width": 404, "height": 720}})},
        # A refused call in the middle must contribute NOTHING, or a denied publish would
        # hand the next step whatever it echoed back.
        {"role": "tool", "content": json.dumps({
            "status": "denied", "skill_id": "publish_youtube",
            "result": {"media_artifact_id": "art_SHOULD_NOT_WIN"}})},
        {"role": "tool", "content": json.dumps({
            "status": "succeeded", "skill_id": "video.watermark", "invocation_id": "skinv_3",
            "result": {"media_artifact_id": "art_FINAL", "view_url": "https://drive/FINAL"}})},
    ]
    db.execute(
        sql("INSERT INTO agent_runs (id, project_id, status, messages, transcript, usage, "
            "finalize_ctx, step) VALUES (:i, :p, 'completed', CAST(:m AS JSONB), "
            "'[]'::jsonb, '{}'::jsonb, '{}'::jsonb, 4)"),
        {"i": run_id, "p": PROJECT, "m": json.dumps(messages)})

    produced = agent_steps._what_the_skills_returned(db, run_id)

    # THE LAST SUCCESSFUL ARTIFACT, which is the finished video.
    assert produced["media_artifact_id"] == "art_FINAL"
    # Earlier facts still come through where nothing later overwrote them.
    assert produced["duration_seconds"] == 68.0
    assert produced["width"] == 404 and produced["height"] == 720
    assert produced["view_url"] == "https://drive/FINAL"
    # The runtime's own bookkeeping does not leak into a step's output — an `invocation_id` in
    # there is something somebody will eventually wire a step to.
    assert "invocation_id" not in produced
    assert "skill_id" not in produced and "status" not in produced

    # And which skills it chose is answerable, which is what the operator reads before approving.
    assert agent_steps._used_skills(db, run_id) == [
        "video.trim", "video.reframe", "publish_youtube", "video.watermark"]


@needs_db
def test_a_run_that_produced_nothing_does_not_invent_an_artifact(db):
    """An agent that failed every call must not hand the next step a stale id."""
    import json

    from sqlalchemy import text as sql

    from boss_engine.services.core import make_id

    run_id = make_id("arun")
    db.execute(
        sql("INSERT INTO agent_runs (id, project_id, status, messages, transcript, usage, "
            "finalize_ctx, step) VALUES (:i, :p, 'completed', CAST(:m AS JSONB), "
            "'[]'::jsonb, '{}'::jsonb, '{}'::jsonb, 1)"),
        {"i": run_id, "p": PROJECT, "m": json.dumps([
            {"role": "tool", "content": json.dumps(
                {"status": "failed", "skill_id": "video.trim",
                 "reason": "no such artifact"})}])})
    assert agent_steps._what_the_skills_returned(db, run_id) == {}


# --------------------------------------------------------------------------- #
# The bound, EXERCISED. The two tests above read the source; these run the loop.
# --------------------------------------------------------------------------- #
class _Recorder:
    """A model that records the tools it was offered and then stops.

    Returning a plain message with no tool calls ends the loop after one turn, which is all this
    needs: the question is what was in `tools`, not what the model would have done with them.
    """

    def __init__(self) -> None:
        self.offered: list[list[str]] = []

    def create_chat_completion(self, messages, tools=None, **kwargs):
        self.offered.append(sorted(
            str((t.get("function") or {}).get("name") or "") for t in (tools or [])))
        self.seen_system = next(
            (m.get("content") for m in messages if m.get("role") == "system"), "")
        self.seen_user = next(
            (m.get("content") for m in messages if m.get("role") == "user"), "")
        # `raw` is what the loop appends back into the conversation — the adapter's own shape,
        # which every real adapter returns and which a stub has to honour too.
        return ({"role": "assistant", "content": "Nothing to do.",
                 "raw": {"role": "assistant", "content": "Nothing to do."},
                 "tool_calls": []},
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


@needs_db
def test_a_bounded_run_is_offered_the_granted_skills_AND_NOTHING_ELSE(db, monkeypatch):
    """THE TEST THAT MATTERS, and it runs the loop rather than reading it.

    The claim the whole design rests on is that an agent's reach IS the skills somebody gave it.
    An ordinary BOSS run is handed `run_shell`, the project's Postgres, the repository and the
    deploy tools, because that is the job in the software factory. If any of those reached a
    bounded run, "the skills you gave it" would stop being a true description — and on 2026-09-02
    an agent's shell reached a production DATABASE_URL and cleared 204 tables.

    So: give one agent exactly two skills, run a step, and look at what the model was offered.
    """
    from boss_engine.services import agent_loop
    from boss_engine.services.skill_definitions import assign_skill_to_role, assignable_catalogue

    db.execute(text("DELETE FROM role_skills WHERE project_id=:p AND role_key='qa'"),
               {"p": PROJECT})
    catalogue = [s["skill_id"] for s in assignable_catalogue(db, PROJECT)]
    two = catalogue[:2]
    assert len(two) == 2, "this project has fewer than two skills; the fixture changed"
    for skill in two:
        assign_skill_to_role(db, project_id=PROJECT, actor="test", role_key="qa",
                             skill_id=skill)

    recorder = _Recorder()
    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *_a, **_k: recorder)

    result = agent_steps.run(db, Settings(), PROJECT,
                             instructions="Do whatever you can with what you hold.",
                             role_key="qa", actor="test")

    assert recorder.offered, "the loop never called the model"
    offered = recorder.offered[0]

    # EXACTLY the granted skills, named as the loop names them, and nothing besides.
    assert offered == sorted("skill_" + s.replace(".", "_").replace("-", "_") for s in two), (
        f"offered {offered}, expected only the two granted skills")

    # Spelled out, because these are the specific tools whose absence is the point.
    for forbidden in ("run_shell", "query_db", "describe_schema", "read_repo_file",
                      "list_repo_files", "project_status", "load_on_demand_skill"):
        assert forbidden not in offered, f"a bounded run was handed {forbidden}"

    # And the agent was told who it is and what it was asked for.
    assert "an AI agent in BOSS" in recorder.seen_system
    from boss_engine.services.agent_world_types import AI_DISCLOSURE
    assert AI_DISCLOSURE in recorder.seen_system
    # Both reach the model: the persona in the system prompt, the outcome ask in the brief.
    assert agent_steps.OUTCOME_ASK in recorder.seen_user
    assert "Do whatever you can" in recorder.seen_user
    assert result["agent"] == "qa"
    assert sorted(result["skills_offered"]) == sorted(two)


@needs_db
def test_the_facts_from_earlier_steps_reach_the_agent_verbatim(db, monkeypatch):
    """An artifact id is a value to use exactly, not a thing to paraphrase.

    An agent that invents `art_1234` produces a run that fails three steps later with something
    that looks like a missing artifact rather than a hallucinated one.
    """
    from boss_engine.services import agent_loop
    from boss_engine.services.skill_definitions import assign_skill_to_role, assignable_catalogue

    one = assignable_catalogue(db, PROJECT)[0]["skill_id"]
    assign_skill_to_role(db, project_id=PROJECT, actor="test", role_key="qa", skill_id=one)

    recorder = _Recorder()
    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *_a, **_k: recorder)

    agent_steps.run(db, Settings(), PROJECT, instructions="Trim it.", role_key="qa",
                    actor="test",
                    context={"media_artifact_id": "art_9f3c", "slide_url": "",
                             "nothing": None})

    assert "media_artifact_id: art_9f3c" in recorder.seen_user
    assert "use these values exactly as written" in recorder.seen_user
    # Empty and absent values are left out rather than passed as blanks — an agent told
    # `slide_url: ` has been handed a fact that is not one.
    assert "slide_url" not in recorder.seen_user
    assert "nothing" not in recorder.seen_user
