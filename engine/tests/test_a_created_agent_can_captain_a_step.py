"""An agent an operator creates can be named as a step's captain.

THE INCIDENT (review of milestone/agents, round 1). Quick create told the operator a new agent
"can be put on a workflow step as soon as it exists". It could not: validate_captain accepted
only the bundled REGISTERED_ROLES, and the builder's captain list was that same fixed set. So
for every agent made with POST /v1/agents, publishing a workflow that named it failed with
"not a role this workforce has".

The fix is one rule in one place: a captain is valid if it is a bundled role OR one of the
project's agents (workflows.project_roster). Every validate_workflow_spec call that has a
project in hand passes the roster — enumerated here, so a new call site that forgets it fails
this test rather than an operator. The behaviour end to end, through POST /v1/agents and a real
publish, is in apps/api/tests/test_one_agent_model.py.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from boss_engine.services import workflows
from boss_engine.services.workflow_capabilities import validate_captain

ROOT = Path(__file__).resolve().parents[2]
SCANNED = [ROOT / "engine" / "boss_engine", ROOT / "apps" / "api" / "app"]

#: Calls with no project to read a roster from, and why that is right.
WITHOUT_A_PROJECT = {
    ("services/workflow_catalogue.py", "catalogue_summary"):
        "the bundled catalogue names only bundled roles",
}


def _agent_step(captain: str) -> dict:
    return {"id": "audit", "kind": "capability", "captain": captain,
            "binding": {"agent_work": True, "inputs": {"instructions": {"literal": "check"}}}}


def test_a_project_agent_is_a_valid_captain_when_the_roster_is_given():
    assert validate_captain({"id": "s", "captain": "release_auditor"},
                            roster={"release_auditor"}) == []


def test_without_the_roster_only_bundled_roles_pass_and_the_refusal_says_what_to_do():
    [problem] = validate_captain({"id": "s", "captain": "release_auditor"})
    assert "not a role this workforce has" in problem
    assert "Agents page" in problem and "leave the captain empty" in problem


def test_a_spec_naming_a_project_agent_validates_with_its_roster():
    spec = {"entry_step": "audit", "steps": [_agent_step("release_auditor")]}
    with pytest.raises(workflows.WorkflowValidationError):
        workflows.validate_workflow_spec(spec)
    normalized = workflows.validate_workflow_spec(spec, {"release_auditor"})
    assert normalized["steps"][0]["captain"] == "release_auditor"


def _calls_without_roster():
    for base in SCANNED:
        for path in base.rglob("*.py"):
            if "__pycache__" in str(path) or "/tests/" in str(path):
                continue
            source = path.read_text(encoding="utf-8")
            if "validate_workflow_spec(" not in source:
                continue
            for fn in ast.walk(ast.parse(source)):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.name == "validate_workflow_spec":
                    continue
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call)
                            and getattr(node.func, "attr", getattr(node.func, "id", ""))
                            == "validate_workflow_spec"
                            and len(node.args) + len(node.keywords) < 2):
                        yield (str(path.relative_to(base)), fn.name)


def test_every_call_with_a_project_passes_the_project_roster():
    missing = sorted(set(_calls_without_roster()) - set(WITHOUT_A_PROJECT))
    assert not missing, (
        f"validate_workflow_spec is called without the project's roster in {missing}, so a "
        "step naming an agent the operator created is refused there. Pass "
        "workflows.project_roster(connection, project_id), or list the call in "
        "WITHOUT_A_PROJECT with the reason.")


def test_with_a_roster_a_bundled_role_missing_from_it_is_refused():
    """Review of milestone/agents, round 2: validate_captain unioned REGISTERED_ROLES back into
    the roster, so a bundled agent the operator had DELETED still passed at publish and only
    failed at start or minutes into a run. With a roster, the roster is the whole answer (plus
    the human approver); project_roster is what keeps undeleted bundled roles in it."""
    [problem] = validate_captain({"id": "s", "captain": "qa"}, roster={"release_auditor"})
    assert "not a role this workforce has" in problem and "Agents page" in problem
    # The human approver, on a gate — `human` is only ever accepted where a person can answer
    # (the steps stream's shape rule; a bare step with no kind is not a gate).
    assert validate_captain({"id": "s", "kind": "decision", "captain": "human"},
                            roster={"release_auditor"}) == []
    assert validate_captain({"id": "s", "captain": "qa"}) == [], "no project: the bundled list"


class _Rows:
    """A connection answering the roster's two reads: the roles rows, then the tombstones."""

    def __init__(self, roles, deleted):
        self._answers = [roles, deleted]

    def execute(self, *_a, **_k):
        return self

    def scalars(self):
        return self

    def all(self):
        return list(self._answers.pop(0))


def test_the_project_roster_leaves_out_exactly_the_bundled_agents_an_operator_deleted(
        monkeypatch):
    from boss_engine.services import seed_exclusions
    from boss_engine.services.workflow_capabilities import REGISTERED_ROLES

    monkeypatch.setattr(seed_exclusions, "_table_ready", lambda _c: True)
    roster = workflows.project_roster(_Rows(["release_auditor", "cto"], ["qa"]), "p")
    assert "release_auditor" in roster, "an agent the operator made"
    assert "qa" not in roster, "a bundled agent the operator deleted"
    # Bundled roles merely not installed stay valid, as before: several captains run through
    # the agent process template with no pack installing them.
    assert (REGISTERED_ROLES - {"qa"}) <= roster
    # Re-made by hand after a delete, the roles row wins over the old tombstone.
    assert "qa" in workflows.project_roster(_Rows(["qa"], ["qa"]), "p")
