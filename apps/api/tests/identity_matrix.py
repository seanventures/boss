"""The role x operation matrix (IDENTITY.md §15.5): its generator and the stub app it runs on.

    PYTHONPATH=.:engine python -m apps.api.tests.identity_matrix      # rewrite tests/identity/matrix.yaml

`tests/identity/matrix.yaml` is GENERATED from the catalogue (who holds what) and the route
declarations (what each operation needs), and committed: a role change or a re-declared route is
a reviewable diff. The test (test_identity_authz_matrix.py) checks the file is current and then
sends one request per built-in role per operation through the REAL enforcement point and the REAL
PDP against real bindings — on a mirror of the app whose handlers are stubs, so the matrix tests
the access decision, not 1,188 handlers' side effects.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute

from boss_engine.identity.catalogue import HUMAN_ROLES
from boss_engine.identity.declarations import ACCESS_ATTR, Authenticated, Permit, Public, access_of
from boss_engine.identity.permissions_gen import PERMISSION_META, ROLE_PERMISSIONS
from boss_engine.identity.scope import ScopeSource

MATRIX_PATH = Path(__file__).resolve().parents[3] / "tests" / "identity" / "matrix.yaml"
LETTER = {"owner": "O", "admin": "A", "operator": "P", "viewer": "V", "auditor": "U"}


def operations(app: FastAPI) -> list[tuple[str, str, Any, str]]:
    """(method, path, declaration, kind) for every operation the app serves, sorted."""
    from apps.api.app import identity_guard

    ops: list[tuple[str, str, Any, str]] = []
    for entry in identity_guard.route_inventory(app):
        for label in entry.labels():
            method, _, path = label.partition(" ")
            ops.append((method, path, entry.access, entry.kind))
    return sorted(ops, key=lambda o: (o[1], o[0]))


def expected(access: Any, role: str) -> int:
    """200 (allowed), 403 or 404 (denied) for one built-in role bound at org scope."""
    if isinstance(access, (Public, Authenticated)):
        return 200
    assert isinstance(access, Permit), access
    held = ROLE_PERMISSIONS[role]
    if access.permission in held and not PERMISSION_META[access.permission].system_only:
        return 200
    if access.scope.source == ScopeSource.OBJECT:
        read_key = access.permission.split(":", 1)[0] + ":read"
        return 403 if read_key in held else 404
    return 403


def build(app: FastAPI) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for method, path, access, _kind in operations(app):
        allowed = "".join(LETTER[r] for r in HUMAN_ROLES if expected(access, r) == 200)
        out[f"{method} {path}"] = (describe(access), allowed or "-")
    return out


def describe(access: Any) -> str:
    """The declaration, plus its object rules (object_rules.py) so a change to them is a diff."""
    if access is None:
        return "UNDECLARED"
    text = access.describe()
    if isinstance(access, Permit):
        if access.writes_on_get:
            text += " +writes_on_get"
        if access.confine:
            text += " +confine(" + ",".join(c.describe() for c in access.confine) + ")"
        if access.production is not None:
            text += " +" + access.production.describe()
    return text


def render(matrix: dict[str, tuple[str, str]]) -> str:
    lines = [
        "# THE ROLE x OPERATION MATRIX (IDENTITY.md §15.5). GENERATED: do not edit by hand.",
        "#   PYTHONPATH=.:engine python -m apps.api.tests.identity_matrix",
        "# One line per operation: its declaration, then the built-in roles allowed",
        "# (O owner, A admin, P operator, V viewer, U auditor; '-' none). Every other role is denied",
        "# 403, or 404 when the operation names an object the role cannot even read.",
        f"# {len(matrix)} operations.",
    ]
    for label, (decl, allowed) in matrix.items():
        lines.append(f'"{label}": ["{decl}", "{allowed}"]')
    return "\n".join(lines) + "\n"


# ---- the stub mirror ----------------------------------------------------------------------------

def stub_app(app: FastAPI) -> FastAPI:
    """Every HTTP route of `app`, same path, methods and declaration, with a handler that returns
    {"ok": true}, behind the same enforcement point."""
    from apps.api.app import identity_guard

    mirror = FastAPI(dependencies=[Depends(identity_guard.enforce_access)], openapi_url=None,
                     docs_url=None, redoc_url=None)
    identity_guard.install(mirror)
    for route in app.router.routes:
        if isinstance(route, APIRoute):
            access = access_of(route.endpoint)

            async def stub() -> dict:
                return {"ok": True}

            stub.__module__ = route.endpoint.__module__
            setattr(stub, ACCESS_ATTR, access)
            mirror.add_api_route(route.path, stub, methods=sorted(route.methods or ()))
    return mirror


def request_for(method: str, path: str, access: Any, project_id: str,
                object_id: str = "obj_1") -> dict[str, Any]:
    """The minimal request that names `project_id` (and an object) wherever the declaration
    looks: path, query and body."""
    url = path.replace("{path:path}", "x")
    params: dict[str, str] = {"project_id": project_id}
    body: dict[str, Any] = {"project_id": project_id}
    obj_param: Optional[tuple[str, str]] = None
    if isinstance(access, Permit) and access.scope.source == ScopeSource.OBJECT:
        obj_param = access.scope.location
    import re

    for name in re.findall(r"{(\w+)(?::\w+)?}", path):
        value = project_id if name == "project_id" else object_id
        url = re.sub(r"{" + name + r"(?::\w+)?}", value, url)
    if isinstance(access, Permit) and access.scope.source == ScopeSource.PROJECT:
        where, name = access.scope.location or ("", "")
        if where == "query":
            params[name] = project_id
        elif where == "body":
            body[name] = project_id
    if obj_param and obj_param[0] == "query":
        params[obj_param[1]] = object_id
    if obj_param and obj_param[0] == "body":
        body[obj_param[1]] = object_id
    # The console's own fetches say Sec-Fetch-Site: same-origin; a GET that writes needs it
    # (identity_guard._writing_get_csrf), every other route ignores it.
    request: dict[str, Any] = {"method": method, "url": url, "params": params,
                               "headers": {"sec-fetch-site": "same-origin"}}
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        request["json"] = body
    return request


if __name__ == "__main__":
    from apps.api.app.main import app as _app

    MATRIX_PATH.write_text(render(build(_app)), encoding="utf-8")
    print(f"wrote {MATRIX_PATH}")
