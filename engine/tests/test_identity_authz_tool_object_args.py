"""Tool arguments that name objects (IDENTITY.md §6.3/§6.4, identity int-b), without a database.

  * every `*_id` argument of every built-in tool is either CONFINED to the call's project or listed
    UNCONFINED with a reason (tools/object_args.py) — a new tool that takes an id fails here;
  * the identity gate checks the tool's permission at the project the call ACTS in (the
    `project_id` argument when given), refuses a confined id of another project as not found
    BEFORE the handler runs, and lets the same id in the call's project through;
  * deploy_app / redeploy also need deployment:production when their target is production.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from boss_engine.config import get_settings
from boss_engine.identity import authz
from boss_engine.identity import object_rules
from boss_engine.identity import scope as scope_module
from boss_engine.identity.authz import Decision, DenyReason
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.tools import build_default_registry, build_discuss_read_registry
from boss_engine.tools.object_args import CONFINED, PRODUCTION, UNCONFINED
from boss_engine.tools.registry import ToolRegistry

ORG = "org_x"
ALICE = AuthContext(Principal("usr_a", PrincipalKind.USER, ORG, PrincipalStatus.ACTIVE, "A"),
                    AuthMethod.SESSION)


def _registries():
    return [build_default_registry(), build_discuss_read_registry()]


def _id_args(spec) -> list[str]:
    props = (spec.parameters or {}).get("properties", {})
    return [k for k in props if (k.endswith("_id") or k.endswith("_ids")) and k != "project_id"]


def test_every_tool_id_argument_is_confined_or_explained():
    missing = []
    for reg in _registries():
        for spec in reg.specs():
            for arg in _id_args(spec):
                if arg not in CONFINED.get(spec.name, {}) and arg not in UNCONFINED.get(spec.name, {}):
                    missing.append(f"{spec.name}.{arg}")
    assert not missing, ("tool id arguments with no confinement rule (tools/object_args.py): "
                         + ", ".join(sorted(set(missing))))


def test_the_table_names_real_tools_arguments_and_kinds():
    specs = {s.name: s for reg in _registries() for s in reg.specs()}
    kinds = scope_module.registered_object_kinds()
    wrong = []
    for tool, args in {**CONFINED, **UNCONFINED}.items():
        if tool not in specs:
            wrong.append(f"no tool {tool}")
            continue
        props = (specs[tool].parameters or {}).get("properties", {})
        wrong += [f"{tool}.{a}: no such argument" for a in args if a not in props]
    for tool, args in CONFINED.items():
        for arg, kind in args.items():
            for k in ((kind,) if isinstance(kind, str) else kind):
                if k not in kinds:
                    wrong.append(f"{tool}.{arg}: unknown object kind {k}")
    assert not wrong, wrong


def test_the_registered_specs_carry_their_rules():
    reg = build_default_registry()
    for tool, args in CONFINED.items():
        spec = reg.get(tool)
        assert {c.name for c in spec.confine} == set(args), tool
        assert all(c.where == "args" for c in spec.confine)
    for tool in ("deploy_app", "redeploy"):
        assert reg.get(tool).production is not None, tool
    # the four the review named (identity int-b item 2)
    for tool in ("extend_code_task", "redeploy", "preflight_deployment_target", "investigate"):
        assert "run_id" in {c.name for c in reg.get(tool).confine}, tool


# ---- the gate -------------------------------------------------------------------------------------

@pytest.fixture
def gate(monkeypatch):
    """A registry of the real specs with stub handlers; objects named a_* live in project pa,
    b_* in project pb; authz allows everything except what a test removes."""
    calls: list = []
    real = build_default_registry()
    reg = ToolRegistry()
    for name in ("extend_code_task", "redeploy", "preflight_deployment_target", "investigate",
                 "deploy_app", "decide", "get_code_task", "list_code_tasks"):
        spec = real.get(name)
        reg.register(replace(spec, handler=lambda ctx, n=name, **kw: calls.append((n, kw)) or {"ran": n},
                             reversibility="safe"))
    for kind in scope_module.registered_object_kinds():
        monkeypatch.setitem(scope_module._OBJECT_LOADERS, kind,  # noqa: SLF001
                            lambda db, oid: (ORG, "pb") if oid.startswith("b_") else
                            (ORG, "pa") if oid.startswith("a_") else None)
    held: set[str] = set()
    seen: list = []

    def check(ctx, permission, scope, **_):
        seen.append((permission, scope.label()))
        if permission in held or permission != "deployment:production":
            return Decision.allow(permission, scope)
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION)

    monkeypatch.setattr(authz, "check", check)
    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "authz_router_modes", "")
    return reg, calls, held, seen


@pytest.mark.parametrize("tool", ["extend_code_task", "redeploy", "preflight_deployment_target",
                                  "investigate", "get_code_task"])
def test_another_projects_run_is_not_found_before_the_handler(gate, monkeypatch, tool):
    reg, calls, _held, _seen = gate
    monkeypatch.setattr(object_rules, "production_projects", lambda *a, **k: [])
    args = {"run_id": "b_crun", "instructions": "x", "question": "q"}
    out = reg.invoke(tool, None, project_id="pa", actor="x", args=args, auth=ALICE,
                     operator_confirmed=True)
    assert out["ok"] is False and out["error"] == "not_found", out
    assert calls == []
    ok = reg.invoke(tool, None, project_id="pa", actor="x", args={**args, "run_id": "a_crun"},
                    auth=ALICE, operator_confirmed=True)
    assert ok.get("ran") == tool, ok


def test_the_permission_is_checked_at_the_project_argument(gate):
    reg, calls, _held, seen = gate
    reg.invoke("list_code_tasks", None, project_id="pa", actor="x", args={"project_id": "pb"},
               auth=ALICE)
    assert ("code_task:read", "project:pb") in seen and ("code_task:read", "project:pa") not in seen


def test_a_confined_id_must_match_the_project_argument_too(gate):
    reg, calls, _held, _seen = gate
    out = reg.invoke("decide", None, project_id="pb", actor="x",
                     args={"decision_id": "a_dec", "project_id": "pb", "action": "approve"},
                     auth=ALICE, operator_confirmed=True)
    assert out.get("error") == "not_found" and calls == []


def test_deploy_app_to_a_production_environment_needs_deployment_production(gate):
    reg, calls, held, _seen = gate
    # no target_id: the environment argument decides (deploy_build picks the target by it)
    refused = reg.invoke("deploy_app", None, project_id="pa", actor="x",
                         args={"run_id": "a_crun", "environment": "production"}, auth=ALICE)
    assert refused["ok"] is False and refused["permission"] == "deployment:production", refused
    staging = reg.invoke("deploy_app", None, project_id="pa", actor="x",
                         args={"run_id": "a_crun", "environment": "staging"}, auth=ALICE)
    assert staging.get("ran") == "deploy_app"
    held.add("deployment:production")
    allowed = reg.invoke("deploy_app", None, project_id="pa", actor="x",
                         args={"run_id": "a_crun", "environment": "prod"}, auth=ALICE)
    assert allowed.get("ran") == "deploy_app"


@pytest.mark.parametrize("env,prod", [("production", True), ("Prod", True), ("live", True),
                                      ("prod-eu", True), ("staging", False), ("", False),
                                      ("preview", False), ("productionish", True)])
def test_what_counts_as_production(env, prod):
    assert object_rules.is_production_environment(env) is prod
