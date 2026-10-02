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
