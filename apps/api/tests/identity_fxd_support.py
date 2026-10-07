"""Shared helpers for the FX-D authorization suites (test_identity_fxd_*.py).

The built-in matrix (identity_matrix.expected) models ONE shape of caller: a built-in role bound at
org scope, MFA-fresh. These suites need more shapes (project-bound roles, custom roles, keys,
stale sessions), so `expect` below models the PDP's decision order (authz.check) for a caller who
holds `org_held` through org bindings and `project_held[p]` through bindings at project p:

    org_only at a project scope -> 403      (A3: before anything else)
    system_only                 -> 403      (no person ever holds one)
    held                        -> 200
    object scope, no <res>:read -> 404      (no existence oracle)
    otherwise                   -> 403

It is deliberately written from the spec's rules, not by calling the PDP: a test that asked the PDP
what to expect would agree with any PDP bug.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld, _id
from boss_engine.identity.declarations import Authenticated, Permit, Public
from boss_engine.identity.permissions_gen import PERMISSION_META, ROLE_PERMISSIONS
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, SessionRef
from boss_engine.identity.scope import ScopeSource

OPS = identity_matrix.operations(app)
#: Every HTTP operation the matrix runs (WS and the docs/framework routes are covered elsewhere).
HTTP_OPS = [o for o in OPS if o[3] == "http" and o[0] != "WS"]
PERMIT_OPS = [o for o in HTTP_OPS if isinstance(o[2], Permit)]
ORG_OPS = [o for o in PERMIT_OPS if o[2].scope.source == ScopeSource.ORG]
PROJECT_OPS = [o for o in PERMIT_OPS if o[2].scope.source == ScopeSource.PROJECT]
OBJECT_OPS = [o for o in PERMIT_OPS if o[2].scope.source == ScopeSource.OBJECT]
STEP_UP_OPS = [o for o in PERMIT_OPS if PERMISSION_META[o[2].permission].step_up
               and not PERMISSION_META[o[2].permission].system_only]

#: The step-up window (IDENTITY.md §4.6; authz.DEFAULT_STEP_UP_SECONDS with no org policy row).
STEP_UP_MINUTES = 10


def label(op: tuple) -> str:
    return f"{op[0]} {op[1]}"


def without_org_only(perms: Iterable[str]) -> set[str]:
    """What a PROJECT binding of a role carrying `perms` grants (amendment A3)."""
    return {p for p in perms if not PERMISSION_META[p].org_only and not PERMISSION_META[p].system_only}


def role_perms(role: str) -> set[str]:
    return set(ROLE_PERMISSIONS[role])


def expect(access: Any, *, org_held: Iterable[str] = (),
           project_held: Optional[Mapping[str, Iterable[str]]] = None,
           project: Optional[str] = None) -> int:
    """The status the guard must answer for `access` (see the module docstring). `project` is
    the project the request names (PROJECT scope) or the object lives in (OBJECT scope)."""
    if isinstance(access, (Public, Authenticated)):
        return 200
    assert isinstance(access, Permit), access
    meta = PERMISSION_META[access.permission]
    org = {p for p in org_held if not PERMISSION_META[p].system_only}
    source = access.scope.source
    if source == ScopeSource.ORG:
        return 200 if (access.permission in org and not meta.system_only) else 403
    if meta.org_only:
        return 403
    if meta.system_only:
        return 403
    held = without_org_only(org) | without_org_only((project_held or {}).get(project or "", ()))
    if access.permission in held:
        return 200
    if source == ScopeSource.OBJECT:
        read_key = access.permission.split(":", 1)[0] + ":read"
        return 403 if read_key in held else 404
    return 403


def session_ctx(principal: Principal, *, mfa_age: Optional[timedelta]) -> AuthContext:
    """A session whose second factor is `mfa_age` old (None: a password-only session)."""
    now = datetime.now(timezone.utc)
    session = SessionRef(id=_id("ses"), mfa_at=(now - mfa_age) if mfa_age is not None else None,
                         auth_methods=("password", "totp") if mfa_age is not None else ("password",),
                         idle_expires_at=now + timedelta(hours=1),
                         absolute_expires_at=now + timedelta(days=1))
    return AuthContext(principal, AuthMethod.SESSION, session=session)


def place_objects(monkeypatch, org: str, project: str) -> None:
    """Point every object kind at one project (as test_identity_authz_matrix does): the real
    loaders are checked against the real schema in test_identity_authz_object_kinds.py."""
    from boss_engine.identity import scope as scope_module

    for kind in scope_module.registered_object_kinds():
        monkeypatch.setitem(scope_module._OBJECT_LOADERS, kind,  # noqa: SLF001
                            lambda db, oid, o=org, p=project: (o, p))


def sweep(client: Any, world: IdentityWorld, ops: Iterable[tuple], want: Any, *,
          project: str, ok_body: Any = None) -> list[str]:
    """Send one request per op naming `project`; return a line per op whose status differs from
    want(access) (and, for refusals, whose body is not the guard's refusal)."""
    wrong: list[str] = []
    for method, path, access, _kind in ops:
        expected = want(access)
        got = client.request(**identity_matrix.request_for(method, path, access, project))
        if expected == 200:
            ok = got.status_code == 200 if ok_body is None else ok_body(got)
        else:
            ok = got.status_code == expected
        if not ok:
            wrong.append(f"{method} {path} [{getattr(access, 'permission', access)}]: "
                         f"want {expected} got {got.status_code} {got.text[:100]}")
    return wrong


def refusal_body_ok(got: Any, status: int, permission: Optional[str] = None) -> bool:
    """The guard's refusal bodies (authz.Decision.error_body)."""
    if got.status_code != status:
        return False
    try:
        body = got.json()
    except ValueError:
        return False
    if status == 401:
        return body == {"error": "step_up_required"} if permission == "step_up" else "error" in body
    if status == 404:
        return body == {"error": "not_found"}
    if status == 403 and permission:
        return body.get("error") == "forbidden" and body.get("permission") == permission
    return True


def report(wrong: list[str], what: str, limit: int = 40) -> str:
    return f"{len(wrong)} wrong ({what}):\n" + "\n".join(wrong[:limit])
