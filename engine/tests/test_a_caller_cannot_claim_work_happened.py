"""A caller names a source of research; only the server decides a run builds on it.

THE INCIDENT (boss-1, 2026-10-02): a delivery run built on research that had failed. The fix
made "Build on this research" one rule (`workflow_run_actions`) and the HTTP start endpoint
strips caller-supplied `adopt_*` keys. The review of 0fd6c45 found the other doors still open:

* The agent's `start_workflow` copied its `inputs` into the run whole, so a model could pass
  `adopt_discovery_run_id` — "this research already happened" — past both rules.
* Its `from_discovery`, and the endpoint's, accepted a COMPLETED dossier with NO findings, which
  the run rule refuses.
* A request naming both `from_run` and `from_discovery` skipped the `from_run` rule entirely.

Now `start_run` itself drops every `adopt_*` key in its input, whoever sent it, and the one way
to set one is its `adopted` argument — which only callers holding a CHECKED source fill.
"""
from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.services import workflow_run_actions as actions_mod
from boss_engine.services import workflows

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    Ctx,
    _publish,
    _start,
    eng,
    project,
)

ROOT = Path(__file__).resolve().parents[2]


def _stored_input(c, run_id):
    return c.execute(text("SELECT input FROM workflow_runs WHERE id=:r"), {"r": run_id}).scalar()


def _version(c, project_id, key=None):
    key = key or f"one_{uuid.uuid4().hex[:6]}"
    _, version = _publish(c, project_id, key, [{"id": "a", "kind": "task"}], "a")
    return key, version


def _dossier(c, project_id, status="completed", findings=True):
    did = f"drun_{uuid.uuid4().hex[:8]}"
    state = {"signal_findings": ["teams lose leads in a shared inbox"]} if findings else {}
    c.execute(text("INSERT INTO discovery_runs (id, project_id, status, phase, title, state) "
                   "VALUES (:i, :p, :s, 'done', 'x', CAST(:st AS JSONB))"),
              {"i": did, "p": project_id, "s": status, "st": json.dumps(state)})
    return did


def test_start_run_drops_an_adoption_a_caller_put_in_its_input(eng, project):
    """THE CENTRAL RULE: every entry point reaches `start_run`, so it is enforced there."""
    with eng.begin() as c:
        _, version = _version(c, project)
        run = workflows.start_run(
            c, project_id=project, actor="test",
            input_data={"directive": "x", "adopt_discovery_run_id": "drun_dead",
                        "adopt_code_task_run_id": "crun_old"},
            dedupe_key=f"dk_{uuid.uuid4().hex[:8]}", workflow_version_id=version["id"])
        stored = _stored_input(c, run["id"])
    assert not [k for k in stored if k.startswith("adopt_")], stored


def test_only_the_adopted_argument_sets_one(eng, project):
    with eng.begin() as c:
        _, version = _version(c, project)
        run = workflows.start_run(
            c, project_id=project, actor="test", input_data={"directive": "x"},
            dedupe_key=f"dk_{uuid.uuid4().hex[:8]}", workflow_version_id=version["id"],
            adopted={"adopt_discovery_run_id": "drun_checked"})
        stored = _stored_input(c, run["id"])
        with pytest.raises(workflows.WorkflowValidationError):
            workflows.start_run(
                c, project_id=project, actor="test", input_data={"directive": "x"},
                dedupe_key=f"dk_{uuid.uuid4().hex[:8]}", workflow_version_id=version["id"],
                adopted={"directive": "smuggled"})
    assert stored["adopt_discovery_run_id"] == "drun_checked"


def _agent_start(eng, project, args):
    from boss_engine.tools.builtin import build_default_registry

    tool = {spec.name: spec for spec in build_default_registry().specs()}["start_workflow"]
    return tool.handler(Ctx(eng), project_id=project, actor="test", args=args)


def test_the_agent_cannot_claim_research_happened(eng, project):
    """The review's probe: `start_workflow` with `inputs: {adopt_discovery_run_id: …}`."""
    with eng.begin() as c:
        key, _ = _version(c, project)
        dead = _dossier(c, project, status="failed")
    out = _agent_start(eng, project, {"directive": "build it", "workflow_key": key,
                                      "inputs": {"adopt_discovery_run_id": dead,
                                                 "video_url": "https://example.com/v"}})
    assert out.get("run_id"), out
    assert out.get("ignored_input") == ["adopt_discovery_run_id"]
    with eng.begin() as c:
        stored = _stored_input(c, out["run_id"])
        c.execute(text("DELETE FROM discovery_runs WHERE id=:i"), {"i": dead})
    assert "adopt_discovery_run_id" not in stored
    assert stored["video_url"] == "https://example.com/v", "ordinary inputs still travel"


@pytest.mark.parametrize("status,findings,ok", [
    ("completed", True, True),
    ("completed", False, False),     # researched nothing — the copy here used to accept it
    ("failed", True, False),
    ("running", False, False),
])
def test_the_agent_builds_on_research_by_the_same_rule_as_the_console(
        eng, project, status, findings, ok):
    with eng.begin() as c:
        key, _ = _version(c, project)
        did = _dossier(c, project, status=status, findings=findings)
    out = _agent_start(eng, project, {"directive": "build it", "workflow_key": key,
                                      "from_discovery": did})
    with eng.begin() as c:
        stored = _stored_input(c, out["run_id"]) if out.get("run_id") else None
        c.execute(text("DELETE FROM discovery_runs WHERE id=:i"), {"i": did})
    if ok:
        assert stored["adopt_discovery_run_id"] == did
    else:
        assert out.get("ok") is False and stored is None, out
        assert "nothing" in out["error"] or "not completed" in out["error"], out


def test_naming_both_sources_is_refused(eng, project):
    with eng.begin() as c:
        did = _dossier(c, project)
        with pytest.raises(workflows.WorkflowValidationError, match="not both"):
            actions_mod.research_to_adopt(c, project, from_run="wfr_live", from_discovery=did)
        c.execute(text("DELETE FROM discovery_runs WHERE id=:i"), {"i": did})


def test_only_checked_sources_set_an_adoption():
    """ENUMERATED: every call that passes `adopted=` to start_run, anywhere in the engine or the
    API, is one of the three that hold a checked source — the HTTP start (via
    research_to_adopt), the agent's start_workflow (via research_to_adopt) and
    start_project_run (true by construction: the code task IS the build). A fourth must be
    added here deliberately, with its reason."""
    allowed = {
        ("apps/api/app/main.py", "start_delivery_workflow"),
        ("engine/boss_engine/tools/builtin.py", "start_workflow"),
        ("engine/boss_engine/services/workflows.py", "start_project_run"),
    }
    found = set()
    for path in [*(ROOT / "engine" / "boss_engine").rglob("*.py"),
                 *(ROOT / "apps" / "api" / "app").rglob("*.py")]:
        def visit(node, enclosing):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                enclosing = node.name
            if isinstance(node, ast.Call) and any(k.arg == "adopted" for k in node.keywords):
                found.add((str(path.relative_to(ROOT)), enclosing))
            for child in ast.iter_child_nodes(node):
                visit(child, enclosing)

        visit(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    assert found == allowed
