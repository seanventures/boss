"""Pass 2 (More tests), PLRR and an `extend` build start their work UNDER THE RUN'S AUTHORITY,
for a permission the consent asked for — and a pass 2 that cannot start never stops the run.

wfr_9575c86ab4a74defa0d2 (2026-10-13): `more_tests` returned from the runner's `tool is None`
branch and called extend_code_task bare, as the worker. Under enforce it was refused "needs
code_task:retry" (the consent derived from the workflow never asked for it: the capability's
tool is None), the step showed "returned no id to wait on", was retried three times, and the
`all` join before the done step stopped the whole run on a pass that only adds tests.

Enforced as a class:
  * every tool a capability calls in-process is DECLARED (Capability.calls) and the consent of
    any workflow holding it asks for that tool's permission;
  * `_invoke_tool` refuses a call made outside `_step_authority`;
  * under enforce, a consented run's More tests starts (awaits the new task); refused, it
    completes advisory instead of failing the step.
"""
from __future__ import annotations

import ast
import json
import types
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import consent, runs
from boss_engine.services import workflow_capabilities
from boss_engine.services import workflow_capability_runner as runner

from .test_identity_agents_workflow_consent import env  # noqa: F401  (full-schema fixture)
from .test_no_step_is_blocked_by_consent import (  # noqa: F401
    RUN_INPUT, _every_capability_spec, _grant, _publish, _start_consented, staffed)

RUNNER = Path(runner.__file__)


def _in_process_tool_calls() -> dict[str, set[str]]:
    """{tool: {functions}} for every literal tool name the runner calls in-process: an
    `_invoke_tool(ctx, "<tool>", ...)` or a `tool = "<tool>"` handed to `_invoke_tool`."""
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_invoke_tool"
                    and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)):
                found.setdefault(node.args[1].value, set()).add(fn.name)
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and getattr(node.targets[0], "id", "") == "tool"
                    and isinstance(node.value, ast.Constant)):
                found.setdefault(node.value.value, set()).add(fn.name)
    return found


def test_every_in_process_tool_is_declared_by_the_capabilities_that_call_it():
    calls = _in_process_tool_calls()
    assert set(calls) == set(runner._BRANCH_TOOLS), calls
    for key in (*runner._ON_THE_BRANCH, "build"):
        cap = workflow_capabilities.CAPABILITIES[key]
        assert set(runner._BRANCH_TOOLS) <= set(consent.capability_tools(cap)), key


def test_the_consent_asks_for_every_tool_a_capability_calls(env):  # noqa: F811
    """The class rule, for every capability: each tool its step calls (its own and in-process)
    whose permission is delegable is in the consent of a workflow holding it."""
    from boss_engine.identity import catalogue
    from boss_engine.tools.builtin import build_default_registry

    specs = {t.name: t for t in build_default_registry().specs()}
    for key, cap in workflow_capabilities.CAPABILITIES.items():
        spec = {"entry_step": key, "steps": [{"id": key, "kind": "capability", "next": [],
                                              "binding": {"capability": key, "inputs": {}}}]}
        with env.engine.begin() as c:
            _, perms, _ = consent.workflow_needs(c, env.project, spec, {})
        for tool in consent.capability_tools(cap):
            perm = str(getattr(specs.get(tool), "permission", "") or "")
            if perm and catalogue.is_known(perm) and catalogue.meta(perm).delegable:
                assert perm in perms, (key, tool, perm)
    with env.engine.begin() as c:
        assert "code_task:retry" in consent.workflow_needs(
            c, env.project, {"steps": [{"id": "m", "binding": {"capability": "more_tests"}}]}, {})[1]


def test_a_tool_call_outside_the_runs_authority_is_refused():
    with pytest.raises(runner.SettledCapabilityError, match="outside this run's authority"):
        runner._invoke_tool(types.SimpleNamespace(), "extend_code_task", "p", "ceo",
                            {"run_id": "crun_x", "instructions": "x"})


# ---- under enforce, end to end through _perform_capability ------------------------------------

class _Spec:
    def __init__(self, real, handler):
        self.name, self.permission, self.handler = real.name, real.permission, handler
        for attr in ("permission_for", "inherits_route_permission", "confine", "production",
                     "also_requires", "acts_org_wide"):
            if hasattr(real, attr):
                setattr(self, attr, getattr(real, attr))


def _enforce(env, monkeypatch):  # noqa: F811
    """authz_mode enforce; the fixture's authz fake decides a run token with no connection."""
    from boss_engine.identity import authz

    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")
    fake = authz.check

    def check(ctx, permission, scope, *, db=None):
        if db is None and getattr(ctx, "run_token", None) is not None:
            with env.engine.begin() as own:
                return fake(ctx, permission, scope, db=own)
        return fake(ctx, permission, scope, db=db)

    monkeypatch.setattr(authz, "check", check)


def _stub_extend(monkeypatch, started: list):
    from boss_engine.tools.builtin import build_default_registry

    real = {s.name: s for s in build_default_registry().specs()}["extend_code_task"]

    def handler(ctx, *, project_id, actor, args):
        from boss_engine.identity import lineage

        here = lineage.current_context()
        started.append({"args": args, "agent": getattr(getattr(here, "principal", None), "id", None)})
        return {"status": "extending", "result": {"run_id": "crun_pass2"}}

    registry = types.SimpleNamespace(specs=lambda: [_Spec(real, handler)])
    monkeypatch.setattr(runner, "_tool_registry", lambda: registry)


def _more_tests_run(env, spec_steps):  # noqa: F811
    spec = {"entry_step": spec_steps[0]["id"], "steps": spec_steps}
    for i, s in enumerate(spec_steps):
        s["next"] = [spec_steps[i + 1]["id"]] if i + 1 < len(spec_steps) else []
    version = _publish(env, spec, "pass two")
    run = _start_consented(env, version, RUN_INPUT)
    with env.engine.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, finalize_ctx) "
                       "VALUES ('crun_pass1', :p, 'HelpDesk', 'completed', '{}'::jsonb, '{}'::jsonb) "
                       "ON CONFLICT (id) DO NOTHING"), {"p": env.project})
        c.execute(text("UPDATE workflow_step_runs SET status='succeeded', completed_at=now(), "
                       "output=CAST(:o AS JSONB), binding_snapshot=CAST(:b AS JSONB) "
                       "WHERE workflow_run_id=:r AND step_id='build'"),
                  {"r": run["id"], "o": json.dumps({"code_task_run_id": "crun_pass1"}),
                   "b": json.dumps({"capability": "build"})})
    return run


def _perform_more_tests(env, run):  # noqa: F811
    step = {"id": "more_tests", "kind": "capability",
            "binding": {"capability": "more_tests", "inputs": {}}}
    ctx = types.SimpleNamespace(engine=env.engine, settings=None)
    with env.engine.begin() as c:
        return runner._perform_capability(
            c, ctx, project_id=env.project, actor="ceo", step=step,
            context={"run_id": run["id"], "input": RUN_INPUT}, key="more_tests",
            capability=workflow_capabilities.CAPABILITIES["more_tests"], inputs={})


STEPS = [{"id": "build", "kind": "capability",
          "binding": {"capability": "build", "inputs": {"instructions": "go", "title": "t"}}},
         {"id": "more_tests", "kind": "capability",
          "binding": {"capability": "more_tests", "inputs": {"run_id": "crun_pass1"}}}]


def test_more_tests_starts_under_enforce_as_the_builder(env, staffed, monkeypatch):  # noqa: F811
    _enforce(env, monkeypatch)
    started: list = []
    _stub_extend(monkeypatch, started)
    run = _more_tests_run(env, json.loads(json.dumps(STEPS)))
    out = _perform_more_tests(env, run)
    assert out.get("status") == "awaiting", out
    assert out["await"] == {"kind": "code_task", "id": "crun_pass2"}, out
    assert started and started[0]["args"]["run_id"] == "crun_pass1"
    with env.engine.begin() as c:
        builder = runs.agent_for_role(c, env.project, runs.resolved_builder())
    assert started[0]["agent"] == builder, started


def test_a_more_tests_pass_that_cannot_start_never_stops_the_run(env, staffed, monkeypatch):  # noqa: F811
    """Refused at start (here: the grant does not hold code_task:retry): the step completes
    advisory, carrying pass 1's build, settled — no three retries, no failed step."""
    _enforce(env, monkeypatch)
    started: list = []
    _stub_extend(monkeypatch, started)
    real = consent._capability_needs
    monkeypatch.setattr(consent, "_capability_needs",
                        lambda k: (tuple(p for p in real(k)[0] if p != "code_task:retry"), real(k)[1]))
    run = _more_tests_run(env, json.loads(json.dumps(STEPS)))
    out = _perform_more_tests(env, run)
    assert not started
    assert out["status"] == "not_applicable" and out["advisory_failed"] is True, out
    assert out["code_task_run_id"] == "crun_pass1"
    assert "code_task:retry" in out["note"] or "Retry builds" in out["note"], out
    from boss_engine.services import workflow_outcomes

    assert workflow_outcomes.apply("more_tests", out, {})["status"] != "failed"

