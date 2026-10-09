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
    # With a caller, the route decides (the bridge forwards the caller's own credential, M8).
    assert r.invoke("api_x", None, project_id="p1", actor="x", args={}, auth=ALICE)["api"]
    # With NO caller there is nobody for the route to decide about: refused under enforce (H2).
    out = r.invoke("api_x", None, project_id="p1", actor="x", args={})
    assert out["ok"] is False and out["error"] == "identity_unavailable"


def test_the_workflow_runner_cannot_go_around_the_gate(monkeypatch):
    from boss_engine.services import workflow_capability_runner as runner

    source = open(runner.__file__, encoding="utf-8").read()
    body = source[source.index("def _invoke_tool"):]
    body = body[: body.index("\ndef ", 10)]
    assert body.index("identity_gate(") < body.index("spec.handler(")


# ---- also_requires: a tool needing two keys, like the route it mirrors (apply_template) -----------

def _two_keys(monkeypatch):
    calls: list = []
    r = ToolRegistry()
    r.register(ToolSpec("apply", "a", {"type": "object"}, lambda ctx, **k: calls.append(k) or {"done": True},
                        kind="write", permission="workflow_run:start", also_requires=("template:read",)))
    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "authz_router_modes", "")
    return r, calls


def test_also_requires_refuses_without_the_second_key_and_checks_it_at_its_own_scope(monkeypatch):
    r, calls = _two_keys(monkeypatch)
    seen: list = []
    monkeypatch.setattr(authz, "check", _decide({"workflow_run:start"}, seen))
    out = r.invoke("apply", None, project_id="p1", actor="x", args={}, auth=ALICE)
    assert out["ok"] is False and out["permission"] == "template:read" and calls == []
    # the project key at the project, the org-only key at the org
    assert seen == [("workflow_run:start", "project:p1"), ("template:read", "org")]
    seen.clear()
    monkeypatch.setattr(authz, "check", _decide({"workflow_run:start", "template:read"}, seen))
    assert r.invoke("apply", None, project_id="p1", actor="x", args={}, auth=ALICE)["done"] and len(calls) == 1


def test_also_requires_is_not_asked_when_the_first_key_is_refused(monkeypatch):
    r, calls = _two_keys(monkeypatch)
    seen: list = []
    monkeypatch.setattr(authz, "check", _decide({"template:read"}, seen))
    out = r.invoke("apply", None, project_id="p1", actor="x", args={}, auth=ALICE)
    assert out["permission"] == "workflow_run:start" and seen == [("workflow_run:start", "project:p1")]
    assert calls == []


def test_also_requires_names_only_catalogue_keys_and_needs_a_permission():
    from boss_engine.identity.catalogue import CatalogueError

    with pytest.raises(CatalogueError):
        ToolSpec("x", "x", {"type": "object"}, lambda ctx, **k: {}, permission="project:read",
                 also_requires=("template:reed",))
    with pytest.raises(CatalogueError):
        ToolSpec("y", "y", {"type": "object"}, lambda ctx, **k: {}, inherits_route_permission=True,
                 also_requires=("template:read",))
