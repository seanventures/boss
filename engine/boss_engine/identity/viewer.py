"""THE "may the person reading this page do X here?" question, for every `can_*` field a read
returns so the console can show or grey a control (R2 #5).

Under an enforced request the answer is the PDP's — the permission the control's write route
declares, held by the request's verified principal at that project — never the legacy members
table (which names none of the identity people) and never a claimed `actor=`/`viewer=` query
parameter. Outside an enforced request (shadow, off, a background job) `may` answers None and the
caller asks its legacy question, exactly as before.

It is a question, not a gate: nothing is audited or recorded as a denial (authz.check audits
every deny; a page that asks a dozen can_* questions per poll must not fill the audit log).
"""
from __future__ import annotations

from typing import Any, Optional

from . import lineage
from .permissions_gen import PERMISSION_META
from .scope import ResolvedScope


def enforced() -> bool:
    """Did the API's guard decide (and enforce) the request this code runs under?"""
    return bool(lineage.request_enforced())


def may(project_id: Optional[str], permission: str, *, db: Any = None) -> Optional[bool]:
    """True/False under an enforced request: does the request's principal hold the catalogue
    `permission` at `project_id` (or at the org when None)? None when no guard enforced this
    request: the caller's legacy rule decides."""
    if not enforced():
        return None
    if PERMISSION_META.get(permission) is None:
        raise KeyError(f"unknown catalogue permission {permission!r}")
    ctx = lineage.current_context()
    if ctx is None:
        return False
    from . import authz

    org = ctx.principal.org_id
    scope = (ResolvedScope.in_project(org, str(project_id)) if project_id
             else ResolvedScope.org_level(org))
    try:
        held = authz.effective_permissions(ctx, scope, db=db).permissions
    except Exception:  # noqa: BLE001 — cannot tell: the control is shown as locked, never open
        return False
    return permission in held


def decide(project_id: Optional[str], permission: str) -> Optional[bool]:
    """A GATE (audited like every PDP decision): under an enforced request, may the request's
    principal do `permission` at `project_id`? None when no guard enforced this request."""
    if not enforced():
        return None
    ctx = lineage.current_context()
    if ctx is None:
        return False
    from . import authz

    org = ctx.principal.org_id
    scope = (ResolvedScope.in_project(org, str(project_id)) if project_id
             else ResolvedScope.org_level(org))
    return authz.check(ctx, permission, scope).allowed
