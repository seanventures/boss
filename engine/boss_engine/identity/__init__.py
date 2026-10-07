"""BOSS Identity: principals, the permission catalogue, the one decision function, delegation,
the vault and the security audit log. Spec: docs/product/IDENTITY.md. Build plan and stream
ownership: docs/maintainers/IDENTITY_BUILD.md.

Phase 0 froze the public surface re-exported here. The functions that raise NotImplementedError
are filled in by the streams named in their docstrings; nothing outside this package may decide
access or decrypt a vault item.
"""
from __future__ import annotations

from .authz import Decision, DenyReason, EffectivePermissions, Why
from .catalogue import CatalogueError, PermissionMeta
from .declarations import (
    Access,
    Authenticated,
    Permit,
    Public,
    PublicReason,
    access_of,
    authenticated,
    permit,
    public,
)
from .object_rules import Confine, Production
from .permissions_gen import CATALOGUE_FINGERPRINT, CATALOGUE_VERSION, P, Permission
from .principal import (
    ActorChain,
    AgentInstance,
    AuthContext,
    AuthMethod,
    CredentialRef,
    Principal,
    PrincipalKind,
    PrincipalStatus,
    RunMode,
    RunTokenClaims,
    SessionRef,
    TaskBinding,
)
from .scope import ResolvedScope, Scope, ScopeKind, ScopeNotFound, ScopeSpec

__all__ = [
    "Access", "ActorChain", "Confine", "Production", "AgentInstance", "AuthContext", "AuthMethod", "Authenticated",
    "CATALOGUE_FINGERPRINT", "CATALOGUE_VERSION", "CatalogueError", "CredentialRef", "Decision",
    "DenyReason", "EffectivePermissions", "P", "Permission", "PermissionMeta", "Permit",
    "Principal", "PrincipalKind", "PrincipalStatus", "Public", "PublicReason", "ResolvedScope",
    "RunMode", "RunTokenClaims", "Scope", "ScopeKind", "ScopeNotFound", "ScopeSpec", "SessionRef",
    "TaskBinding", "Why", "access_of", "authenticated", "permit", "public",
]
