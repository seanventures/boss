"""A step names an agent, and the agent runs it.

BOSS's own pipelines read as agents doing the work because a capability step's `captain`
becomes the actor. A CUSTOM step — the only kind a customer can build — ignored it and ran as
`workflow_runner` every time, so a customer's workflow had no agent in it anywhere. That is
the asymmetry: what BOSS does for itself, and what it lets a customer do.

REWRITTEN. These were assertions about source text ("captain or RUNNER_CALLER" appears in
execute), and they pinned the runner borrowing a skill by widening its allowed_callers. The rule
now lives in `step_agents` and is asked, not read: the named agent, else the agent that holds
the skill, else the workflow only where the skill's own policy names it, else a refusal. The
end-to-end runs are in test_workflow_custom_steps.py and apps/api/tests/test_who_runs_a_step.py.
"""
import inspect

import pytest

from boss_engine.services import step_agents
from boss_engine.services import workflow_custom_steps as steps
from boss_engine.services import workflows
from boss_engine.services.workflow_capabilities import validate_captain


def test_a_named_agent_runs_the_step_however_it_was_typed():
    """validate_captain lowercases and strips, so the resolver must too — or a step could pass
    validation and then act as a role that does not exist."""
    ran_as = step_agents.resolve(
        None, None, "p",
        {"id": "publish", "kind": "capability", "captain": " Media_Publisher ",
         "binding": {"custom_step": "publish"}},
        actor="ceo")
    assert ran_as == {"agent": "media_publisher", "how": "named", "why": "named on the step",
                      "considered": ["media_publisher"]}


def _nobody_may_run(monkeypatch):
    definition = {"binding_kind": "skill", "skill_id": "tracker.file_issue", "label": "File"}
    monkeypatch.setattr(steps, "get", lambda *a: definition)
    monkeypatch.setattr(step_agents, "project_agents", lambda *a, **k: [])
    monkeypatch.setattr(step_agents, "_allowed_callers", lambda *a: set())
    return {"id": "file", "kind": "capability", "binding": {"custom_step": "file"}}


def test_naming_an_agent_is_not_a_way_to_grant_it_something(monkeypatch):
    """REWRITTEN. This asserted the widening helper was deleted. It is back, for one case only —
    a version published under the old rules (see the next test) — so what is pinned now is the
    behaviour: a step under the current rules that nobody may run is REFUSED, with what to do,
    instead of running as the workflow on a widened grant."""
    step = _nobody_may_run(monkeypatch)
    with pytest.raises(step_agents.StepAgentError, match="Give tracker.file_issue to an agent"):
        step_agents.resolve(None, None, "p", step, actor="ceo")


def test_a_version_published_under_the_old_rules_still_runs_as_the_workflow(monkeypatch):
    """THE INCIDENT: the builder's default for a custom step was "no agent — runs as the
    workflow". The new rule refused those steps everywhere, including in runs already going, so
    an upgrade killed working runs mid-way. A step `_run_spec` tagged as published under the old
    rules keeps that behaviour, and says on the run how to move it to an agent."""
    step = _nobody_may_run(monkeypatch)
    step[step_agents.AGENT_RULES_KEY] = step_agents.LEGACY_AGENT_RULES
    ran_as = step_agents.resolve(None, None, "p", step, actor="ceo")
    assert (ran_as["agent"], ran_as["how"]) == ("workflow_runner", "legacy_workflow_runner")
    assert "Give tracker.file_issue to an agent" in ran_as["why"]
    assert "publish the workflow again" in ran_as["why"]

    # Tagged as current — what every version published from now on carries — it is refused.
    step[step_agents.AGENT_RULES_KEY] = step_agents.CURRENT_AGENT_RULES
    with pytest.raises(step_agents.StepAgentError):
        step_agents.resolve(None, None, "p", step, actor="ceo")


def test_the_runner_is_used_only_where_the_skills_own_policy_names_it(monkeypatch):
    """REWRITTEN from "the anonymous runner still works exactly as before". It does — for a
    binding whose policy lists `workflow_runner` (every synthesised MCP tool) — and nowhere else."""
    agents = [{"role_key": "clerk", "holds": [], "name": "clerk", "mission": ""}]
    definition = {"binding_kind": "skill", "skill_id": "tracker.file_issue", "label": "File"}

    monkeypatch.setattr(step_agents, "_allowed_callers", lambda *a: {"workflow_runner"})
    pool, basis, fallback = step_agents.who_may_run(None, "p", definition, agents)
    assert (pool, fallback) == ([], "workflow_runner")

    monkeypatch.setattr(step_agents, "_allowed_callers", lambda *a: {"agent_loop"})
    pool, basis, fallback = step_agents.who_may_run(None, "p", definition, agents)
    assert (pool, fallback) == ([], ""), "nobody may run it, so it must be refused"


def test_the_holder_is_found_before_the_skills_listed_callers(monkeypatch):
    """The permission gate allows a role_skills grant OR a listed caller; both count."""
    agents = [{"role_key": "clerk", "holds": ["tracker.file_issue"], "name": "", "mission": ""},
              {"role_key": "auditor", "holds": [], "name": "", "mission": ""}]
    definition = {"binding_kind": "skill", "skill_id": "tracker.file_issue"}
    monkeypatch.setattr(step_agents, "_allowed_callers", lambda *a: {"auditor"})
    pool, basis, _ = step_agents.who_may_run(None, "p", definition, agents)
    assert [a["role_key"] for a in pool] == ["clerk"] and basis == "holds"

    agents[0]["holds"] = []
    pool, basis, _ = step_agents.who_may_run(None, "p", definition, agents)
    assert [a["role_key"] for a in pool] == ["auditor"] and basis == "listed"


def test_a_misspelt_agent_is_refused_against_the_projects_own_agents():
    """REWRITTEN: this checked a hard-coded set of 29 role keys, which is why an agent made on
    the Agents page could never be put on a step. The check is against the project's agents."""
    project = ["media_publisher", "video_editor", "docs_writer"]
    refused = validate_captain({"id": "publish", "captain": "vidoe_editor"}, roster=project)
    assert refused and "which this project does not have" in refused[0]
    assert "video_editor" in refused[0], "the refusal names the agents there are"
    # An agent the project made itself — refused by the old constant — is accepted.
    assert validate_captain({"id": "publish", "captain": "docs_writer"}, roster=project) == []
    # No captain is not an error: BOSS picks the agent that holds what the step needs.
    assert validate_captain({"id": "publish"}, roster=project) == []


def test_a_person_can_answer_a_gate_but_cannot_run_a_step():
    """`human` was accepted on every kind of step, and a build step then ran as actor 'human'."""
    assert validate_captain({"id": "gate", "kind": "decision", "captain": "human"}) == []
    refused = validate_captain(
        {"id": "build", "kind": "capability", "captain": "human",
         "binding": {"capability": "build"}})
    assert refused and "only an approval gate waits for a person" in refused[0]


def test_a_misspelt_agent_is_caught_when_the_workflow_is_published():
    """Only the built-in capabilities had their captain checked.

    So a typo on a customer's step published cleanly and was denied at RUN time — twenty
    minutes into a workflow — as "caller 'vidoe_editor' not in allowed_callers". Every step kind
    passes the project's roster (tests/test_a_created_agent_can_captain_a_step.py), so an agent
    the operator created is accepted and a typo is not. (Integration: the agents stream's
    roster check and the steps stream's shape check are one validate_captain; without a roster
    only the shape is checked, which the test above pins.)
    """
    source = inspect.getsource(workflows)
    assert "captain_problems = validate_captain(step, roster)" in source
    assert validate_captain({"id": "publish", "captain": "vidoe_editor"},
                            roster={"video_editor", "release_auditor"})


def test_the_builder_palette_endpoint_actually_runs():
    """CALLED, NOT PARSED.

    The roles list was added to this endpoint's payload and the import that made the name exist
    was not — an edit script raised between the two and wrote neither, then a second run wrote
    only the usage. `ast.parse` said "syntax OK" because a missing name is not a syntax error,
    the engine suite passed because it never imports this module's handlers, and the console
    tests are string comparisons. So a 500 on the endpoint that draws the entire builder
    palette shipped, and the first sign of it was an empty list on a live install.

    REWRITTEN for project agents: with no project there are no agents to offer, only the
    person a gate can name. The project's own list is checked against a database in
    apps/api/tests/test_who_runs_a_step.py.
    """
    import os

    os.environ.setdefault("BOSS_EDITION", "enterprise_self_hosted")
    from apps.api.app import main

    payload = main.list_delivery_capabilities(project_id=None)
    assert payload["capabilities"], "the palette must not be empty"
    assert payload["roles"] == ["human"]
    assert payload["agents"] == []
    assert isinstance(payload["manual_approval_risk_floor"], int)


@pytest.mark.parametrize("field", ["skills", "mcp"])
def test_a_scope_on_a_step_that_cannot_use_one_is_refused(field):
    """Silently accepted and ignored before: `binding.skills` validated on any step and nothing
    read it. Only an agent step can be given a set of skills or MCP servers."""
    from boss_engine.services.workflows import WorkflowValidationError, validate_workflow_spec

    with pytest.raises(WorkflowValidationError, match="only an agent step"):
        validate_workflow_spec({"entry_step": "s", "steps": [
            {"id": "s", "kind": "capability",
             "binding": {"custom_step": "x", field: ["a"]}}]})
