"""The questionable declarations of the completeness audit, settled as classes (FX-B; part 3 §2).

  * A route that writes or removes CREDENTIALS declares a critical permission (kit connect and
    disconnect were skill:manage, the only credential writer that was not).
  * Minting another person's password-reset link is user:reset_mfa (critical), not user:update.
  * Changing a group's members is granting (§6.8): group:manage_members is critical.
  * A tool that picks between operations by an argument needs the chosen operation's permission,
    the same one the HTTP route for that operation declares (decide: delegate, execute).
  * Raw SQL (write_db) never writes an access table.
  * /v1/identity/introspect is not a sign-in endpoint: it has its own public reason.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app import identity_guard
from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity.declarations import Permit, Public, PublicReason
from boss_engine.identity.permissions_gen import PERMISSION_META

ROOT = Path(__file__).resolve().parents[3]

#: functions that store or remove a credential; a route calling one directly is a credential writer
CREDENTIAL_WRITERS = ("put_secret", "kit_connections.connect", "kit_connections.disconnect")


def _declaration(method: str, path: str):
    for entry in identity_guard.route_inventory(app):
        if f"{method} {path}" in entry.labels():
            return entry.access
    raise AssertionError(f"no route {method} {path}")


def test_every_route_that_writes_credentials_declares_a_critical_permission():
    found, wrong = [], []
    for path in (ROOT / "apps/api/app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for fn in ast.walk(ast.parse(source)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            permits = [d for d in fn.decorator_list if isinstance(d, ast.Call)
                       and ast.unparse(d.func).endswith("permit") and d.args]
            if not permits:
                continue
            calls = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
            if not any(c == w or c.endswith("." + w) for c in calls for w in CREDENTIAL_WRITERS):
                continue
            declared = ast.unparse(permits[0].args[0])
            key = next((k for k in PERMISSION_META if "P." + k.upper().replace(":", "_") == declared), None)
            found.append(f"{path.name}:{fn.name}")
            if key is None or not PERMISSION_META[key].critical:
                wrong.append(f"{path.name}:{fn.name} writes credentials under {declared} (not critical)")
    assert {"kit.py:connect", "kit.py:disconnect"} <= set(found), found
    assert not wrong, "\n".join(wrong)


def test_the_settled_declarations():
    assert _declaration("POST", "/v1/kit/{ability}/connect").permission == "integration:manage"
    assert _declaration("DELETE", "/v1/kit/{ability}/connection").permission == "integration:manage"
    assert _declaration("POST", "/v1/identity/users/{user_id}/reset-link").permission == "user:reset_mfa"
    assert PERMISSION_META["user:reset_mfa"].critical
    assert PERMISSION_META["group:manage_members"].critical and PERMISSION_META["group:manage_members"].step_up
    introspect = _declaration("POST", "/v1/identity/introspect")
    assert isinstance(introspect, Public) and introspect.reason == PublicReason.EGRESS_SERVICE_CREDENTIAL


def test_the_decide_tool_needs_what_the_route_for_its_action_needs():
    from boss_engine.tools import build_default_registry

    decide = build_default_registry().get("decide")
    wrong = []
    for action in ("approve", "reject", "revise", "delegate", "execute"):
        route = _declaration("POST", f"/v1/decisions/{{decision_id}}/{action}")
        assert isinstance(route, Permit)
        if decide.permission_for({"action": action}) != route.permission:
            wrong.append(f"{action}: tool {decide.permission_for({'action': action})} vs route {route.permission}")
    assert not wrong, wrong


@pytest.mark.parametrize("sql", [
    "UPDATE identity_role_bindings SET role_id = 'arl_owner' WHERE id = 'x'",
    "INSERT INTO identity_approval_seats (org_id, seat_key, principal_id, assigned_by) VALUES ('o','ceo','p','x')",
    'DELETE FROM public."identity_security_events" WHERE id = 1',
    "UPDATE project_members SET permissions = '[\"enterprise_manage\"]' WHERE member_id = 'eve'",
    "INSERT INTO break_glass_grants (id) VALUES ('b')",
])
def test_raw_sql_never_writes_an_access_table(sql):
    from boss_engine.tools.builtin import _writes_access_tables

    assert _writes_access_tables(sql)


def test_raw_sql_on_other_tables_is_not_mistaken_for_access():
    from boss_engine.tools.builtin import _writes_access_tables

    assert _writes_access_tables("UPDATE jobs SET status = 'cancelled' WHERE id = 'j'") is None
    assert _writes_access_tables("UPDATE my_identity_notes SET x = 1 WHERE id = 1") is None


# ---- over HTTP / through the gate -------------------------------------------------------------------

@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def test_a_skill_manager_without_integration_manage_cannot_store_kit_credentials(world):
    person = world.person("skills-only")
    world.bind(person, world.custom_role("skills-only", ("project:read", "skill:read", "skill:manage")),
               project=world.project_a)
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(person)):
        connect = client.post("/v1/kit/web_search/connect",
                              json={"project_id": world.project_a, "provider": "x", "fields": {}})
        disconnect = client.delete(f"/v1/kit/web_search/connection?project_id={world.project_a}")
    for out in (connect, disconnect):
        assert out.status_code == 403 and "integration:manage" in out.text, out.text


def test_an_operator_cannot_delegate_a_decision_through_the_tool(world):
    from boss_engine.tools import build_default_registry

    operator = world.person("decide-operator")
    world.bind(operator, "operator", project=world.project_a)
    ctx = world.ctx(operator)
    with world.acting_as(ctx):
        out = build_default_registry().invoke(
            "decide", None, project_id=world.project_a, actor="x", auth=ctx,
            args={"decision_id": "dec_none", "action": "delegate", "note": "n", "delegate_to": "y"},
            operator_confirmed=True)
    assert out.get("ok") is False and out.get("permission") == "decision:delegate", out
