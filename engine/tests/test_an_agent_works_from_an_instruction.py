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
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        yield connection


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
    db.execute(text("DELETE FROM role_skills WHERE project_id=:p AND role_key='qa'"),
               {"p": PROJECT})
    with pytest.raises(agent_steps.AgentStepError, match="holds no skills"):
        agent_steps.run(db, Settings(), PROJECT, instructions="do something",
                        role_key="qa")


@needs_db
def test_an_agent_holding_nothing_is_not_offered_as_a_candidate(db):
    """It is not a candidate for anything: every call it made would be refused."""
    db.execute(text("DELETE FROM role_skills WHERE project_id=:p AND role_key='qa'"),
               {"p": PROJECT})
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


# --------------------------------------------------------------------------- #
# Choosing the agent
# --------------------------------------------------------------------------- #
@needs_db
def test_one_candidate_needs_no_judgement(db):
    picked = agent_steps.choose_agent(db, Settings(), PROJECT,
                                     instructions="anything at all", only=["qa"])
    assert picked["role_key"] == "qa"
    assert picked["how"] == "only_candidate"


@needs_db
def test_choosing_falls_back_to_something_rather_than_refusing(db):
    """A project with no model configured must still be able to run a workflow.

    Worse than a judgement, and honest about being worse: `how` says which happened, so an
    operator reading a surprising choice can see it was word overlap and not reasoning.
    """
    picked = agent_steps.choose_agent(
        db, Settings(), PROJECT,
        instructions="publish the finished video to the channel")
    assert picked["role_key"]
    assert picked["how"] in {"model", "word_overlap", "only_candidate"}
    assert picked["why"]


@needs_db
def test_no_agent_holds_anything_says_what_to_do_about_it(db):
    """Narrowed to one empty agent rather than emptying the project.

    The first version of this test did `DELETE FROM role_skills WHERE project_id=:p`, which
    passed and then broke two unrelated tests that ran after it. A test that wrecks the state
    other tests read is a worse defect than the one it was checking for.
    """
    db.execute(text("DELETE FROM role_skills WHERE project_id=:p AND role_key='qa'"),
               {"p": PROJECT})
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

    source = inspect.getsource(agent_loop._drive_run)
    assert 'bounded = isinstance(finalize_ctx.get("agent_step"), dict)' in source
    assert "schemas = [] if bounded else" in source
    # And no MCP server either: a connector is authority over somebody's account, granted by
    # giving the agent a skill that runs through it, not by being registered.
    assert "[] if bounded" in source.split("list_mcp_servers")[0][-200:]


def test_a_bounded_run_resolves_its_skills_from_the_grant():
    import inspect

    from boss_engine.services import agent_loop

    source = inspect.getsource(agent_loop._skill_resolution_for_run)
    assert "held_skills(" in source, "it must read role_skills, not the role spec"
    assert '"on_demand": []' in source, "a granted skill is not something to discover"


def test_the_step_cap_is_bounded():
    """An unbounded loop on a paid model is an invoice, not a feature."""
    assert 5 <= agent_steps.DEFAULT_MAX_STEPS <= 100


# --------------------------------------------------------------------------- #
# What the step produces, which is what makes it chainable
# --------------------------------------------------------------------------- #
def test_the_results_of_the_skills_become_the_steps_output():
    """LATER CALLS WIN, and that is the point.

    An agent that fetches, then renders, then verifies has produced three `media_artifact_id`s,
    and the one a publish step wants is the LAST. Without this an agent step is a dead end: it
    does the work and the next step cannot read the result — which would make the whole idea
    unusable, however good the loop is.
    """
    import inspect

    source = inspect.getsource(agent_steps._what_the_skills_returned)
    assert "merged[key] = value" in source
    # The runtime's bookkeeping must not leak into a step's output — an `invocation_id` in there
    # is something somebody will wire.
    assert "invocation_id" in source
    assert '"denied"' in source, "a refused call must not contribute a result"
