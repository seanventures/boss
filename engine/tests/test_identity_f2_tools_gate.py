"""F2 authz fixes in the tool layer (security test id/integration @9070c34c): H1, H2 (tool side),
M6, M7, M10 and the LOW items for `repo` and `generate_image`. No database: the decision is faked
at authz.check and the handlers' queries at ctx.begin, so what is pinned is WHERE the gate decides
and WHAT the handler refuses.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import types

import pytest

from boss_engine.config import get_settings
from boss_engine.identity import authz
from boss_engine.identity.authz import Decision, DenyReason
from boss_engine.identity.enforcement import LEDGER
from boss_engine.identity.permissions_gen import PERMISSION_META
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.tools import builtin
from boss_engine.tools.registry import ToolRegistry, ToolSpec

ALICE = AuthContext(Principal("usr_a", PrincipalKind.USER, "org_x", PrincipalStatus.ACTIVE, "A"),
                    AuthMethod.SESSION)


@pytest.fixture
def enforce(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "authz_router_modes", "")
    LEDGER.clear()
    yield settings
    LEDGER.clear()


def _holds_only_at(project: str | None, seen: list, permissions: set[str] | None = None):
    """A caller who holds everything (or `permissions`) only at `project` (None: at org)."""
    def check(ctx, permission, scope, **_):
        seen.append((permission, scope.label()))
        at = "org" if project is None else f"project:{project}"
        if scope.label() == at and (permissions is None or permission in permissions):
            return Decision.allow(permission, scope)
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION)
    return check


# ---- H1: the gate decides the project the handler acts in ------------------------------------

def test_h1_every_pinned_discuss_tool_is_decided_at_the_session_project(enforce, monkeypatch):
    """The tester's poc_project_mismatch.py, over the WHOLE pinned registry: a caller who holds
    everything in project B only, in a session pinned to project A, names B in the arguments. The
    gate used to check B (allowed) while the pinned handler acted in A. Now no check ever lands
    on B, and nothing runs."""
    seen: list = []
    monkeypatch.setattr(authz, "check", _holds_only_at("proj_B", seen))
    reg = builtin.build_discuss_read_registry(edition="enterprise")
    ctx = types.SimpleNamespace(durable_workspace_configured=True)
    assert len(reg.names()) > 20
    for name in reg.names():
        spec = reg.get(name)
        if spec.inherits_route_permission:
            continue
        assert spec.pinned_project, name
        out = reg.invoke(name, ctx, project_id="proj_A", actor="u",
                         args={"project_id": "proj_B", "host": "h", "command": "id"},
                         operator_confirmed=True, auth=ALICE)
        assert out.get("ok") is False and out.get("error") in ("forbidden", "not_found"), (name, out)
    assert seen and not [s for s in seen if s[1] == "project:proj_B"], seen


def test_h1_the_agent_action_route_registry_is_the_pinned_one():
    """POST /v1/engine/agent/action passes free-form `args` to build_discuss_read_registry: the
    pin (not the route's body check alone) is what keeps args.project_id out of the decision."""
    import pathlib

    main = pathlib.Path(__file__).resolve().parents[2] / "apps/api/app/main.py"
    body = main.read_text(encoding="utf-8")
    route = body[body.index('"/v1/engine/agent/action"'):]
    route = route[route.index("\ndef "):]
    route = route[: route.index("\n@", 10)]
    assert "build_discuss_read_registry(" in route and "registry.invoke(" in route


def test_h1_an_unpinned_tool_acts_exactly_where_the_gate_decided(enforce, monkeypatch):
    """A handler that ignores args.project_id used to act in the session project after the gate
    checked the argument's project. The registry now hands the handler the decided project."""
    seen: list = []
    acted: list = []
    monkeypatch.setattr(authz, "check", _holds_only_at("p2", seen))
    reg = ToolRegistry()
    reg.register(ToolSpec("t", "t", {"type": "object"},
                          lambda ctx, *, project_id, actor, args: acted.append(project_id) or {},
                          kind="write", permission="deployment:create"))
    assert reg.invoke("t", None, project_id="p1", actor="x", args={"project_id": "p2"},
                      auth=ALICE)["ok"] is True
    assert seen == [("deployment:create", "project:p2")] and acted == ["p2"]
    # pinned: the argument is discarded before the gate
    pinned = ToolRegistry()
    pinned.register(ToolSpec("t", "t", {"type": "object"},
                             lambda ctx, *, project_id, actor, args: acted.append(project_id) or {},
                             kind="write", permission="deployment:create", pinned_project=True))
    out = pinned.invoke("t", None, project_id="p1", actor="x", args={"project_id": "p2"}, auth=ALICE)
    assert out["ok"] is False and seen[-1] == ("deployment:create", "project:p1") and acted == ["p2"]


# ---- H2 (tool side): no identity, no call -----------------------------------------------------

def test_h2_no_identity_is_refused_for_every_kind_of_tool_under_enforce(enforce):
    reg = ToolRegistry()
    ran: list = []
    reg.register(ToolSpec("native", "n", {"type": "object"}, lambda ctx, **k: ran.append(1) or {},
                          permission="system:read"))
    reg.register(ToolSpec("api_x", "a", {"type": "object"}, lambda ctx, **k: ran.append(2) or {},
                          inherits_route_permission=True))
    for name in ("native", "api_x"):
        out = reg.invoke(name, None, project_id="p1", actor="x", args={})
        assert out["ok"] is False and out["error"] == "identity_unavailable", name
    assert ran == []


# ---- M6 / M7: install-wide actions are decided at org scope -----------------------------------

def test_m6_system_doctor_act_needs_org_scope(enforce, monkeypatch):
    seen: list = []
    monkeypatch.setattr(authz, "check", _holds_only_at("p1", seen))
    reg = builtin.build_default_registry()
    assert reg.get("system_doctor_act").org_scope is True
    out = reg.invoke("system_doctor_act", None, project_id="p1", actor="x",
                     args={"finding_id": "f", "option": "o"}, operator_confirmed=True, auth=ALICE)
    assert out["ok"] is False and seen == [("system:operate", "org")]
    # and the pinned chat registry keeps it
    discuss = builtin.build_discuss_read_registry(edition="enterprise")
    assert discuss.get("system_doctor_act").org_scope is True


def test_m7_mandate_standard_all_projects_needs_org_scope(enforce, monkeypatch):
    seen: list = []
    monkeypatch.setattr(authz, "check", _holds_only_at("p1", seen))
    reg = builtin.build_default_registry()
    args = {"key": "k", "title": "t", "body": "b", "rationale": "r", "all_projects": True}
    out = reg.invoke("mandate_standard", None, project_id="p1", actor="x", args=args, auth=ALICE)
    assert out["ok"] is False and seen == [("policy:manage", "org")]
    # without all_projects it is the project's own rule: decided at the project
    seen.clear()
    monkeypatch.setattr(authz, "check", _holds_only_at("p1", seen, {"x"}))
    reg.invoke("mandate_standard", None, project_id="p1", actor="x",
               args={**args, "all_projects": False}, auth=ALICE)
    assert seen == [("policy:manage", "project:p1")]


# ---- M10: port probes ------------------------------------------------------------------------

class _NoRows:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.sql: list[str] = []

    def execute(self, statement, params=None):
        self.sql.append(str(statement))
        rows = self.rows if "deployment_hosts" in str(statement) else []

        class R:
            def mappings(self):
                return self

            def first(self):
                return rows[0] if rows else None

            def all(self):
                return rows

            def scalar(self):
                return None

        return R()


def _ctx(edition: str = "enterprise", rows=()):
    conn = _NoRows(rows)

    @contextlib.contextmanager
    def begin():
        yield conn

    return types.SimpleNamespace(settings=types.SimpleNamespace(
        boss_edition=edition, target_github_owner="acme", target_github_repo="web",
        github_token="", openai_base_url="", image_model=""),
        begin=begin, durable_workspace_configured=True), conn


def test_m10_port_tools_need_a_non_delegable_host_permission():
    reg = builtin.build_default_registry()
    for name in ("check_port_exposure", "find_free_port"):
        meta = PERMISSION_META[reg.get(name).permission]
        assert meta.delegable is False, name


def test_m10_port_probes_only_reach_the_projects_registered_hosts(monkeypatch):
    import socket

    connected: list = []

    class Sock:
        def __init__(self, *a, **k):
            pass

        def settimeout(self, *_):
            pass

        def connect(self, addr):
            connected.append(addr)
            raise OSError("closed")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", Sock)
    reg = builtin.build_default_registry()
    ctx, conn = _ctx()
    for name, args in (("check_port_exposure", {"host": "10.0.0.9", "ports": [22]}),
                       ("find_free_port", {"host": "10.0.0.9"})):
        out = reg.get(name).handler(ctx, project_id="p1", actor="x", args=args)
        assert out["ok"] is False and "registered" in out["error"], name
    assert connected == []
    assert any("deployment_hosts" in q and "project_id" in q for q in conn.sql)
    # a registered host of the project: probed at its recorded address, within its port budget
    ctx, _ = _ctx(rows=[{"id": "h1", "name": "web-1", "address": "10.0.0.9",
                         "port_min": 8000, "port_max": 8002}])
    out = reg.get("find_free_port").handler(ctx, project_id="p1", actor="x",
                                            args={"host": "web-1", "start": 3000, "count": 10})
    assert out["free_ports"] == [8000, 8001, 8002]
    assert {a for a, _p in connected} == {"10.0.0.9"}


# ---- LOW: `repo` is project-bound in every edition --------------------------------------------

@pytest.mark.parametrize("edition", ["enterprise", "community"])
def test_repo_argument_is_project_bound_in_every_edition(edition):
    ctx, conn = _ctx(edition)
    for repo in ("other-org/private", "private"):
        _repo, _ref, err = builtin._resolve_source(ctx, {"repo": repo}, project_id="p1")
        assert err == "repository unavailable in this project", (edition, repo)
    assert any("github_repository_bindings" in q for q in conn.sql)
    # BOSS's own (open) source stays readable
    assert builtin._resolve_source(ctx, {}, project_id="p1")[2] is None


# ---- LOW: generate_image is namespaced per project --------------------------------------------

def test_generate_image_writes_only_inside_the_projects_own_directory(monkeypatch, tmp_path):
    import urllib.request

    from boss_engine.services import model_routing

    monkeypatch.setenv("BOSS_IMAGE_DIR", str(tmp_path))
    monkeypatch.setattr(model_routing, "provider_credential", lambda *a, **k: "sk-test")

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": [{"b64_json": base64.b64encode(b"PNG").decode()}]}).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Resp())
    reg = builtin.build_default_registry()
    handler = reg.get("generate_image").handler
    ctx, _ = _ctx()
    a = handler(ctx, project_id="proj_a", actor="x", args={"prompt": "p", "path": "hero.png"})
    b = handler(ctx, project_id="proj_b", actor="x", args={"prompt": "p", "path": "hero.png"})
    assert (tmp_path / "proj_a" / "hero.png").exists() and (tmp_path / "proj_b" / "hero.png").exists()
    assert json.dumps(a).count("proj_a") and not json.dumps(a).count("proj_b")
    assert json.dumps(b).count("proj_b")
    escape = handler(ctx, project_id="proj_a", actor="x",
                     args={"prompt": "p", "path": "../proj_b/hero.png"})
    assert escape["ok"] is False
    assert handler(ctx, project_id="", actor="x", args={"prompt": "p", "path": "x.png"})["ok"] is False
    assert reg.get("generate_image").permission == "artifact:rebuild"
    assert os.path.getsize(tmp_path / "proj_b" / "hero.png") == 3
