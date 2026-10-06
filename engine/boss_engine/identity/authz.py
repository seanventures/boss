"""Authorization: the one decision function (the PDP). Owned by stream S2.

FROZEN IN PHASE 0: Decision, Why, DenyReason, EffectivePermissions and the signatures. The bodies
marked NotImplementedError are S2's (IDENTITY_BUILD.md §3). IDENTITY.md §6.

`check` is used by API routes (through the enforcement point), MCP tools, server-rendered console
data, the token service and the egress proxy. Nothing else decides. The algorithm is IDENTITY.md
§6.6, in order: catalogue -> liveness of everything in the context -> system fixed sets -> base set
(person / OBO intersection / autonomous / key scopes) -> step-up -> decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Optional

from .principal import AuthContext, Principal
from .scope import ResolvedScope


class DenyReason(StrEnum):
    UNAUTHENTICATED = "unauthenticated"          # 401
    STEP_UP_REQUIRED = "step_up_required"        # 401 {"error": "step_up_required"}
    MISSING_PERMISSION = "missing_permission"    # 403 {"error": "forbidden", "permission": ...}
    NOT_DELEGABLE = "not_delegable"              # 403: an agent token, a non-delegable permission
    UNKNOWN_PERMISSION = "unknown_permission"    # 403 (and 500 in dev/test)
    PRINCIPAL_INACTIVE = "principal_inactive"    # 401
    CREDENTIAL_INVALID = "credential_invalid"    # 401: key revoked/expired/CIDR, jti revoked
    GRANT_INACTIVE = "grant_inactive"            # 403: grant revoked/expired/suspended
    CHAIN_INACTIVE = "chain_inactive"            # 403: an agent in the chain suspended/revoked
    OUT_OF_SCOPE = "out_of_scope"                # 403: key/grant/task project limits
    ORG_ONLY_AT_PROJECT = "org_only_at_project"  # 403: an org-only permission at a project scope
    NOT_FOUND = "not_found"                      # 404: invisible object (no existence oracle)
    UNDECLARED_ROUTE = "undeclared_route"        # 403: deny by default
    NOT_EVALUATED = "not_evaluated"              # shadow only: a stream's piece is not built yet


DENY_STATUS: dict[DenyReason, int] = {
    DenyReason.UNAUTHENTICATED: 401, DenyReason.STEP_UP_REQUIRED: 401,
    DenyReason.PRINCIPAL_INACTIVE: 401, DenyReason.CREDENTIAL_INVALID: 401,
    DenyReason.NOT_FOUND: 404, DenyReason.NOT_EVALUATED: 503,
}


@dataclass(frozen=True)
class Why:
    """One link of an allow's explanation: `role operator <- group Builders <- binding at
    project:acme-web`. A direct binding has no group."""
    role_key: str
    binding_id: str
    binding_scope: str                 # "org" | "project:<id>"
    via_group: Optional[str] = None    # group name
    group_source: Optional[str] = None  # manual | scim | oidc

    def __str__(self) -> str:
        parts = [f"role {self.role_key}"]
        if self.via_group:
            src = f" (from {self.group_source})" if self.group_source and self.group_source != "manual" else ""
            parts.append(f"group {self.via_group}{src}")
        parts.append(f"binding at {self.binding_scope}")
        return " ← ".join(parts)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    permission: str
    scope: Optional[ResolvedScope]
    reason: Optional[DenyReason] = None           # None when allowed
    via: tuple[Why, ...] = ()                     # allow: the why-chain(s)
    narrowed_by: tuple[str, ...] = ()             # deny after narrowing: "key_scopes", "ceiling:agt_forge", "grant:dlg_x", "task"

    @property
    def status(self) -> int:
        if self.allowed:
            return 200
        return DENY_STATUS.get(self.reason or DenyReason.MISSING_PERMISSION, 403)

    def error_body(self) -> dict[str, Any]:
        """The API error body (IDENTITY.md §10). Never names more than the missing permission."""
        if self.allowed:
            return {}
        reason = self.reason or DenyReason.MISSING_PERMISSION
        if reason in (DenyReason.UNAUTHENTICATED, DenyReason.PRINCIPAL_INACTIVE,
                      DenyReason.CREDENTIAL_INVALID):
            return {"error": "unauthenticated"}
        if reason == DenyReason.STEP_UP_REQUIRED:
            return {"error": "step_up_required"}
        if reason == DenyReason.NOT_FOUND:
            return {"error": "not_found"}
        if reason == DenyReason.NOT_EVALUATED:
            return {"error": "identity_unavailable"}
        return {"error": "forbidden", "permission": self.permission}

    @classmethod
    def allow(cls, permission: str, scope: Optional[ResolvedScope], via: tuple[Why, ...] = ()) -> "Decision":
        return cls(True, permission, scope, None, via)

    @classmethod
    def deny(cls, permission: str, scope: Optional[ResolvedScope], reason: DenyReason,
             narrowed_by: tuple[str, ...] = ()) -> "Decision":
        return cls(False, permission, scope, reason, (), narrowed_by)


@dataclass(frozen=True)
class EffectivePermissions:
    """What a principal (or a narrowed credential/run token) holds at one scope, right now.
    This is exactly what GET /v1/identity/me/permissions returns (contracts.MePermissions)."""
    permissions: frozenset[str]
    scope: ResolvedScope
    epoch: int
    step_up_fresh_until: Optional[datetime] = None
    why: dict[str, tuple[Why, ...]] = field(default_factory=dict)  # filled only when asked


def check(ctx: AuthContext, permission: str, scope: ResolvedScope, *, db: Any = None) -> Decision:
    """THE decision. Deny by default; never raises for a deny (only for a broken database).
    Denials are audited as `authz.denied` (S5's `audit.record`), critical allows as
    `authz.allowed_critical`."""
    raise NotImplementedError("identity.authz.check is stream S2's")


#: The spec's name for the same function (IDENTITY.md §6.6).
authorize = check


def effective_permissions(subject: AuthContext | Principal, scope: ResolvedScope, *,
                          db: Any = None, with_why: bool = False) -> EffectivePermissions:
    """The union of role permissions bound to the principal or its groups at org scope or at
    `scope`'s project — narrowed by key scopes or the run token's intersection when `subject` is
    an AuthContext. Cached per (principal, scope, identity epoch)."""
    raise NotImplementedError("identity.authz.effective_permissions is stream S2's")


def explain(principal_id: str, permission: str, scope: ResolvedScope, *,
            db: Any = None) -> tuple[Why, ...]:
    """Every binding path that grants `permission` at `scope` (empty: not held). Feeds the
    console's "Effective access" view and every audited decision."""
    raise NotImplementedError("identity.authz.explain is stream S2's")


def bump_epoch(org_id: str, *, db: Any, reason: str) -> int:
    """Increment identity_settings.epoch in the caller's transaction and NOTIFY identity_epoch.
    Every identity mutation calls this (§6.6)."""
    raise NotImplementedError("identity.authz.bump_epoch is stream S2's")
