"""The MCP / engine tool dispatch check (IDENTITY.md §6.4, §15.4 R2), without a database.

`ToolRegistry.invoke` asks authz.check for the tool's permission at the session's project before
the handler runs; enforce refuses (and refuses a call with no verified identity), shadow records
and proceeds, off does nothing. API-surface tools pass: their route is checked when they call it.
"""
from __future__ import annotations

import pytest

from boss_engine.config import get_settings
from boss_engine.identity import authz
from boss_engine.identity.authz import Decision, DenyReason
from boss_engine.identity.enforcement import LEDGER
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.tools import registry as registry_module
from boss_engine.tools.registry import ToolRegistry, ToolSpec

ALICE = AuthContext(Principal("usr_a", PrincipalKind.USER, "org_x", PrincipalStatus.ACTIVE, "A"),
                    AuthMethod.SESSION)


@pytest.fixture
def reg(monkeypatch):
    calls: list = []
    r = ToolRegistry()
    r.register(ToolSpec("deploy", "d", {"type": "object"},
                        lambda ctx, **k: calls.append(k) or {"done": True}, kind="write",
                        permission="deployment:create"))
    r.register(ToolSpec("users", "u", {"type": "object"}, lambda ctx, **k: {"n": 1},
                        permission="user:read"))
    r.register(ToolSpec("api_x", "x", {"type": "object"}, lambda ctx, **k: {"api": True},
                        inherits_route_permission=True))
    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "authz_router_modes", "")
    LEDGER.clear()
    yield r, calls
    LEDGER.clear()


def _decide(allowed: set[str], seen: list):
    def check(ctx, permission, scope, **_):
        seen.append((permission, scope.label()))
        if permission in allowed:
            return Decision.allow(permission, scope)
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION)
    return check


def test_enforce_refuses_before_the_handler_runs(reg, monkeypatch):
    r, calls = reg
    seen: list = []
    monkeypatch.setattr(authz, "check", _decide(set(), seen))
    out = r.invoke("deploy", None, project_id="p1", actor="x", args={}, auth=ALICE)
    assert out["ok"] is False and out["permission"] == "deployment:create" and out["error"] == "forbidden"
    assert calls == [] and seen == [("deployment:create", "project:p1")]


def test_an_allowed_call_runs_and_org_only_permissions_are_checked_at_org(reg, monkeypatch):
    r, calls = reg
    seen: list = []
    monkeypatch.setattr(authz, "check", _decide({"deployment:create", "user:read"}, seen))
    assert r.invoke("deploy", None, project_id="p1", actor="x", args={}, auth=ALICE)["done"]
    assert r.invoke("users", None, project_id="p1", actor="x", args={}, auth=ALICE)["n"] == 1
    assert seen == [("deployment:create", "project:p1"), ("user:read", "org")] and len(calls) == 1


def test_no_verified_identity_is_refused_under_enforce(reg):
    r, calls = reg
    out = r.invoke("deploy", None, project_id="p1", actor="x", args={})
    assert out["ok"] is False and out["error"] == "identity_unavailable" and calls == []


def test_the_context_var_carries_the_transports_identity(reg, monkeypatch):
    r, calls = reg
    seen: list = []
    monkeypatch.setattr(authz, "check", _decide({"deployment:create"}, seen))
    token = registry_module.CURRENT_TOOL_AUTH.set(ALICE)
    try:
        assert r.invoke("deploy", None, project_id="p1", actor="x", args={})["done"]
    finally:
        registry_module.CURRENT_TOOL_AUTH.reset(token)


def test_shadow_records_and_proceeds_off_does_nothing(reg, monkeypatch):
    r, calls = reg
    settings = get_settings()
    monkeypatch.setattr(authz, "check", _decide(set(), []))
    monkeypatch.setattr(settings, "authz_mode", "shadow")
    assert r.invoke("deploy", None, project_id="p1", actor="x", args={}, auth=ALICE)["done"]
    assert any(k.path_template == "deploy" and k.reason == "missing_permission" for k in LEDGER.snapshot())
    monkeypatch.setattr(settings, "authz_mode", "off")
    LEDGER.clear()
    assert r.invoke("deploy", None, project_id="p1", actor="x", args={})["done"]
    assert not LEDGER.snapshot()


def test_api_surface_tools_defer_to_their_route(reg):
    r, _ = reg
    assert r.invoke("api_x", None, project_id="p1", actor="x", args={})["api"]


def test_the_workflow_runner_cannot_go_around_the_gate(monkeypatch):
    from boss_engine.services import workflow_capability_runner as runner

    source = open(runner.__file__, encoding="utf-8").read()
    body = source[source.index("def _invoke_tool"):]
    body = body[: body.index("\ndef ", 10)]
    assert body.index("identity_gate(") < body.index("spec.handler(")
