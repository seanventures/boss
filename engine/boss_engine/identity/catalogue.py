"""The permission catalogue's types, its loader and its validation rules.

The catalogue's SOURCE is `packages/identity/catalogue.yaml`. Nothing reads that YAML at run time:
`codegen.py` validates it and writes `permissions_gen.py`, which is what the PDP, the route
declarations and the MCP tool declarations import. This module holds what both sides share — the
frozen dataclasses for one permission, one access role and one Passport preset, and the rules a
catalogue must satisfy (IDENTITY.md §6.2, IDENTITY_BUILD.md §2).

Runtime lookups go through the helpers at the bottom (`meta`, `is_known`, `role_permissions`),
which read the generated tables.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

#: `resource:action`, lowercase snake_case. The DB CHECK on identity_permissions.key is the same.
KEY_PATTERN = re.compile(r"^[a-z][a-z_]*:[a-z][a-z_]*$")

#: The closed flag set. A flag absent from an entry is false.
FLAGS = frozenset({"step_up", "delegable", "critical", "system_only", "readonly_post"})

SCOPE_KINDS = frozenset({"org", "project"})

#: The five built-in human access roles, in display order. The YAML may not add a sixth.
HUMAN_ROLES = ("owner", "admin", "operator", "viewer", "auditor")
#: The fixed system roles. Never bindable by a person.
SYSTEM_ROLES = ("system_worker", "system_collector", "system_scim")

#: Permissions that must never be delegable, whatever a later catalogue edit says (§7.13, D12).
#: The generator refuses a catalogue that marks any of them delegable.
NEVER_DELEGABLE = frozenset({
    "decision:approve", "deployment:production", "code_task:override", "host:exec",
    "agent_ability:assign", "role_binding:assign", "access_role:manage", "delegation:grant",
    "agent_passport:manage", "owner:assign", "secret:manage", "security_policy:manage",
})


class CatalogueError(ValueError):
    """The catalogue YAML breaks a rule. The message names the entry and the rule."""


@dataclass(frozen=True)
class PermissionMeta:
    key: str
    title: str
    description: str
    category: str
    roles: tuple[str, ...]
    scope: str  # "org" | "project"
    step_up: bool = False
    delegable: bool = False
    critical: bool = False
    system_only: bool = False
    readonly_post: bool = False

    @property
    def resource(self) -> str:
        return self.key.split(":", 1)[0]

    @property
    def action(self) -> str:
        return self.key.split(":", 1)[1]

    @property
    def org_only(self) -> bool:
        return self.scope == "org"


@dataclass(frozen=True)
class AccessRoleMeta:
    key: str
    name: str
    description: str
    bindable_scopes: tuple[str, ...]
    mfa_required: bool = False
    system: bool = False


@dataclass(frozen=True)
class PassportPreset:
    key: str
    name: str
    agents: tuple[str, ...]
    permissions: tuple[str, ...]
    external_ceiling: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SystemPrincipalMeta:
    id: str
    access_role: str  # a system role key, or "none"
    replaces: str


@dataclass(frozen=True)
class Catalogue:
    version: int
    permissions: tuple[PermissionMeta, ...]
    roles: tuple[AccessRoleMeta, ...]
    presets: tuple[PassportPreset, ...]
    system_principals: tuple[SystemPrincipalMeta, ...]

    def role_permissions(self, role_key: str) -> tuple[str, ...]:
        return tuple(p.key for p in self.permissions if role_key in p.roles)


def repo_root() -> Path:
    """The source-tree root (engine/boss_engine/identity/catalogue.py -> parents[3])."""
    return Path(__file__).resolve().parents[3]


def source_path() -> Path:
    return repo_root() / "packages" / "identity" / "catalogue.yaml"


def load(path: Path | None = None) -> Catalogue:
    """Parse and validate the YAML. Raises CatalogueError on the first broken rule."""
    import yaml

    raw = yaml.safe_load((path or source_path()).read_text(encoding="utf-8"))
    return parse(raw)


def parse(raw: Mapping[str, Any]) -> Catalogue:
    if raw.get("version") != 1:
        raise CatalogueError("catalogue version must be 1")
    roles = tuple(
        AccessRoleMeta(
            key=r["key"], name=r["name"], description=str(r.get("description", "")),
            bindable_scopes=tuple(r["bindable_scopes"]),
            mfa_required=bool(r.get("mfa_required", False)), system=bool(r.get("system", False)),
        )
        for r in raw["access_roles"]
    )
    permissions: list[PermissionMeta] = []
    for category in raw["categories"]:
        for p in category["permissions"]:
            flags = list(p.get("flags") or [])
            unknown = set(flags) - FLAGS
            if unknown:
                raise CatalogueError(f"{p.get('key')}: unknown flags {sorted(unknown)}")
            permissions.append(PermissionMeta(
                key=p["key"], title=p["title"], description=p["description"],
                category=category["category"], roles=tuple(p.get("roles") or ()),
                scope=p["scope"],
                step_up="step_up" in flags, delegable="delegable" in flags,
                critical="critical" in flags, system_only="system_only" in flags,
                readonly_post="readonly_post" in flags,
            ))
    presets = tuple(
        PassportPreset(key=s["key"], name=s["name"], agents=tuple(s.get("agents") or ()),
                       permissions=tuple(s["permissions"]),
                       external_ceiling=dict(s.get("external_ceiling") or {}))
        for s in raw.get("passport_presets") or ()
    )
    principals = tuple(
        SystemPrincipalMeta(id=s["id"], access_role=s["access_role"], replaces=s["replaces"])
        for s in raw.get("system_principals") or ()
    )
    catalogue = Catalogue(version=1, permissions=tuple(permissions), roles=roles,
                          presets=presets, system_principals=principals)
    validate(catalogue)
    return catalogue


def validate(c: Catalogue) -> None:  # noqa: C901 — one rule per block, read top to bottom
    role_keys = [r.key for r in c.roles]
    if tuple(r.key for r in c.roles if not r.system) != HUMAN_ROLES:
        raise CatalogueError(f"human access roles must be exactly {HUMAN_ROLES}")
    if tuple(r.key for r in c.roles if r.system) != SYSTEM_ROLES:
        raise CatalogueError(f"system roles must be exactly {SYSTEM_ROLES}")
    for r in c.roles:
        if not r.bindable_scopes or set(r.bindable_scopes) - SCOPE_KINDS:
            raise CatalogueError(f"role {r.key}: bindable_scopes must be a subset of org, project")
    seen: set[str] = set()
    for p in c.permissions:
        if not KEY_PATTERN.match(p.key):
            raise CatalogueError(f"{p.key}: key must match {KEY_PATTERN.pattern}")
        if p.key in seen:
            raise CatalogueError(f"{p.key}: duplicate key")
        seen.add(p.key)
        if "*" in p.key:
            raise CatalogueError(f"{p.key}: wildcards are not allowed")
        if p.scope not in SCOPE_KINDS:
            raise CatalogueError(f"{p.key}: scope must be org or project")
        if not p.title.strip() or not p.description.strip():
            raise CatalogueError(f"{p.key}: title and description are required")
        bad = set(p.roles) - set(role_keys)
        if bad:
            raise CatalogueError(f"{p.key}: unknown roles {sorted(bad)}")
        if len(set(p.roles)) != len(p.roles):
            raise CatalogueError(f"{p.key}: a role is listed twice")
        human = [r for r in p.roles if r in HUMAN_ROLES]
        system = [r for r in p.roles if r in SYSTEM_ROLES]
        if p.system_only and (human or not system):
            raise CatalogueError(f"{p.key}: system_only means system roles only, at least one")
        if system and not p.system_only:
            raise CatalogueError(f"{p.key}: only system_only permissions go in system roles")
        if p.critical:
            if not p.step_up:
                raise CatalogueError(f"{p.key}: critical implies step_up")
            if p.delegable:
                raise CatalogueError(f"{p.key}: critical permissions are never delegable")
            if {"viewer", "auditor"} & set(p.roles):
                raise CatalogueError(f"{p.key}: critical is never held by viewer or auditor")
        if p.key in NEVER_DELEGABLE and p.delegable:
            raise CatalogueError(f"{p.key}: is on the never-delegable list (spec §7.13)")
        if human and "owner" not in human:
            # Owner is "everything a human can hold" (IDENTITY_PERMISSIONS.md §3).
            raise CatalogueError(f"{p.key}: a human-held permission must include owner")
    by_key = {p.key: p for p in c.permissions}
    for s in c.presets:
        for k in s.permissions:
            if k not in by_key:
                raise CatalogueError(f"preset {s.key}: unknown permission {k}")
            if not by_key[k].delegable:
                raise CatalogueError(f"preset {s.key}: {k} is not delegable")
    for sp in c.system_principals:
        if sp.access_role != "none" and sp.access_role not in SYSTEM_ROLES:
            raise CatalogueError(f"system principal {sp.id}: role must be a system role or none")


# ---- runtime helpers over the GENERATED tables -----------------------------------------------

def _gen():
    from . import permissions_gen

    return permissions_gen


def is_known(key: str) -> bool:
    return key in _gen().PERMISSION_META


def meta(key: str) -> PermissionMeta:
    """The metadata for one permission. KeyError for a key the catalogue does not hold."""
    return _gen().PERMISSION_META[key]


def require_known(key: str) -> str:
    """Return `key` unchanged, or raise CatalogueError naming it. Used at declaration time."""
    if not isinstance(key, str) or not is_known(key):
        raise CatalogueError(f"unknown permission {key!r}: not in packages/identity/catalogue.yaml")
    return key


def role_permissions(role_key: str) -> frozenset[str]:
    return _gen().ROLE_PERMISSIONS[role_key]


def delegable() -> frozenset[str]:
    return frozenset(k for k, m in _gen().PERMISSION_META.items() if m.delegable)


def all_keys() -> Iterable[str]:
    return _gen().PERMISSION_META.keys()
