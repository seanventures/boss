"""The agent reads and acts in ANY project its caller may, through one generic argument.

Exam, 2026-10-24 (Metatron, project boss-dogfood): "tell me the current state of projects
northpeak-bank-a, -ap and -b — do they exist, which repositories are registered in each, and what
is each one's knowledge_graph setting?". Every chat tool was pinned to the conversation's project
with no way to name another, so the agent "checked" by calling create_project, and earlier said it
"has no tool that lets one conversation act on a different project's settings or repositories" and
guessed the setting `knowledge`. The class: project scope and capability self-knowledge.

No database: the project lookup and the authz decision are faked, so what is pinned is WHERE each
call is decided and acts, and what the model is told.
"""
from __future__ import annotations

import types

import pytest

from boss_engine.config import get_settings
from boss_engine.identity import authz
from boss_engine.identity.authz import Decision, DenyReason
from boss_engine.identity.enforcement import LEDGER
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.services import project_settings
from boss_engine.tools import builtin, registry as registry_module
from boss_engine.tools.registry import PROJECT_ARG, ToolRegistry, ToolSpec

ALICE = AuthContext(Principal("usr_a", PrincipalKind.USER, "org_x", PrincipalStatus.ACTIVE, "A"),
                    AuthMethod.SESSION)
PROJECTS = {"proj_A": "proj_A", "bank-b": "proj_B", "proj_B": "proj_B"}


@pytest.fixture
def mode(monkeypatch):
    settings = get_settings()

    def set_mode(value: str):
        monkeypatch.setattr(settings, "authz_mode", value)
        monkeypatch.setattr(settings, "authz_router_modes", "")
    set_mode("enforce")
    LEDGER.clear()
    yield set_mode
    LEDGER.clear()


@pytest.fixture(autouse=True)
def lookup(monkeypatch):
    def resolve(ctx, ref, auth):
        if ref in PROJECTS:
            return PROJECTS[ref], None
        return None, {"ok": False, "error": "project_not_found", "project": ref,
                      "projects_in_this_organization": sorted(set(PROJECTS.values()))}
    monkeypatch.setattr(registry_module, "resolve_named_project", resolve)


def _holds_only_at(project: str, seen: list):
    def check(ctx, permission, scope, **_):
        seen.append((permission, scope.label()))
        if scope.label() == f"project:{project}":
            return Decision.allow(permission, scope)
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION)
    return check


def _pinned(acted: list, *, kind="write", permission="settings:update") -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolSpec("t", "t", {"type": "object"},
                          lambda ctx, *, project_id, actor, args:
                          acted.append((project_id, dict(args))) or {},
                          kind=kind, permission=permission, pinned_project=True))
    return reg


def test_every_project_tool_the_agent_holds_says_it_can_name_another_project():
    """Self-knowledge from the code: the argument is on every tool that acts in one project (a
    tool added tomorrow too), and only on those — an install-wide tool takes none."""
    reg = builtin.build_discuss_read_registry(edition="enterprise")
    for spec in reg.specs():
        props = (spec.parameters or {}).get("properties") or {}
        if spec.org_scope is True:
            assert PROJECT_ARG not in props, spec.name
        else:
            assert props.get(PROJECT_ARG, {}).get("type") == "string", spec.name
        assert "project_id" not in props, spec.name


def test_a_named_project_is_where_the_call_is_decided_and_acts(mode, monkeypatch):
    seen: list = []
    acted: list = []
    monkeypatch.setattr(authz, "check", _holds_only_at("proj_B", seen))
    out = _pinned(acted).invoke("t", types.SimpleNamespace(), project_id="proj_A", actor="u",
                                args={PROJECT_ARG: "bank-b", "key": "k"}, auth=ALICE)
    assert out["ok"] is True, out
    assert acted == [("proj_B", {"key": "k"})], "acts in the named project, argument consumed"
    assert seen and all(label == "project:proj_B" for _p, label in seen), seen


def test_no_permission_in_the_named_project_is_refused_in_every_authz_mode(mode, monkeypatch):
    """A cross-project call is never passed through on trust: shadow and off modes refuse it too."""
    for setting in ("enforce", "shadow", "off"):
        mode(setting)
        seen: list = []
        acted: list = []
        monkeypatch.setattr(authz, "check", _holds_only_at("proj_A", seen))
        out = _pinned(acted).invoke("t", types.SimpleNamespace(), project_id="proj_A", actor="u",
                                    args={PROJECT_ARG: "proj_B"}, auth=ALICE)
        assert out["ok"] is False and out["error"] == "forbidden", (setting, out)
        assert out["project"] == "proj_B" and "proj_B" in out["message"]
        assert acted == [], setting
        # ...and with no verified caller at all
        out = _pinned(acted).invoke("t", types.SimpleNamespace(), project_id="proj_A", actor="u",
                                    args={PROJECT_ARG: "proj_B"})
        assert out["ok"] is False and out["error"] == "identity_unavailable", (setting, out)
        assert acted == []


def test_naming_the_session_project_or_none_acts_in_the_session_project(mode, monkeypatch):
    seen: list = []
    acted: list = []
    monkeypatch.setattr(authz, "check", _holds_only_at("proj_A", seen))
    reg = _pinned(acted)
    for args in ({}, {PROJECT_ARG: "proj_A"}, {"project_id": "proj_B"}):
        assert reg.invoke("t", types.SimpleNamespace(), project_id="proj_A", actor="u",
                          args=dict(args), auth=ALICE)["ok"] is True, args
    assert [p for p, _a in acted] == ["proj_A"] * 3, "project_id is still discarded (H1)"


def test_a_project_that_does_not_exist_is_a_fact_not_an_error_to_retry(mode, monkeypatch):
    acted: list = []
    monkeypatch.setattr(authz, "check", _holds_only_at("proj_A", []))
    out = _pinned(acted, kind="read", permission="project:read").invoke(
        "t", types.SimpleNamespace(), project_id="proj_A", actor="u",
        args={PROJECT_ARG: "northpeak-bank-z"}, auth=ALICE)
    assert out["ok"] is False and out["error"] == "project_not_found", out
    assert out["projects_in_this_organization"] and acted == []


def test_an_offer_keeps_the_project_it_was_made_for():
    """A write the model may not press comes back with its arguments verbatim, so the button
    acts where the agent said it would."""
    reg = ToolRegistry()
    reg.register(ToolSpec("w", "w", {"type": "object"}, lambda ctx, **k: {}, kind="write",
                          reversibility="destroys_work", permission="settings:update",
                          pinned_project=True))
    out = reg.invoke("w", None, project_id="proj_A", actor="u", args={PROJECT_ARG: "bank-b"})
    assert out["status"] == "confirmation_required"
    assert out["proposed_args"] == {PROJECT_ARG: "bank-b"}


def test_the_settings_tool_lists_every_real_key_and_its_values_from_the_settings_table():
    """Generated from KNOBS, so the agent never guesses a key (`knowledge`) or a value."""
    reg = builtin.build_discuss_read_registry(edition="community")
    said = reg.get("set_project_setting").description
    for key, spec in project_settings.KNOBS.items():
        assert f"{key}={project_settings.allowed_values_said(spec)}" in said, key
    assert "knowledge_graph=on|keyword|off" in said


def test_the_agent_can_read_which_projects_exist_and_each_ones_state():
    reg = builtin.build_discuss_read_registry(edition="community")
    for name in ("list_projects", "describe_project"):
        spec = reg.get(name)
        assert spec.kind == "read" and spec.permission == "project:read", name
    assert "never create a project to find out" in reg.get("list_projects").description.lower()
    assert "list_projects" in reg.get("create_project").description


def test_a_held_write_tells_the_model_a_write_is_not_a_way_to_check_state():
    from boss_engine.agent.engine_agent import EngineAgent

    reg = ToolRegistry()
    reg.register(ToolSpec("make", "m", {"type": "object"}, lambda ctx, **k: {"ok": True},
                          kind="write", permission="project:create"))
    agent = EngineAgent(types.SimpleNamespace(settings=get_settings()), registry=reg, adapter=object())
    pending: list = []
    out = agent._run_tool("make", {}, project_id="p", actor="u", allow_writes=False,
                          pending=pending)
    assert out["status"] == "confirmation_required" and pending
    assert "never finds anything out" in out["note"]
