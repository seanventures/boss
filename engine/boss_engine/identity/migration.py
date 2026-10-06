"""Migration from today's identity to BOSS Identity (IDENTITY.md §12, M0-M5). Owned by stream S5.

Two guarantees, each pinned by the migration rehearsal (engine/tests/test_identity_migration_
rehearsal.py):
  1. no step locks out an existing install;
  2. every step can be reversed with one setting until the step that removes the legacy path.

What `seed` (M0, idempotent, run lazily by the bridge and by `python -m boss_engine.identity.cli
migrate`) maps, and to what:

  today                                   becomes
  -------------------------------------   ----------------------------------------------------------
  human member (ceo, cto, cao, ...)       a PENDING placeholder person (legacy_member_ids=[id]),
                                          bound to a `legacy_<hash>` access role holding exactly the
                                          permissions its capabilities map to (inert until a real
                                          person is invited into it or claims it)
  system member (executive_console,       a system principal sys_legacy_<id> with the fixed
    metatron, coo, system)                Owner-equivalent set (+ the worker's system set for
                                          BOSS_WORKER_PRINCIPAL), legacy credentials only
  member capabilities (21 strings)        catalogue permissions (CAPABILITY_PERMISSIONS)
  break-glass grant (active)              a time-boxed binding (expires_at) of the grant's role
  approver role in default_safety.yaml    an approval seat row for the placeholder
  BOSS_API_KEY (the console's key)        sys_legacy_console: the Owner set until the claim, then
                                          the Owner set minus identity writes until the cutoff
                                          (D25, M5; operator decision 2026-10-06)
  BOSS_API_KEYS + _PRINCIPALS             identity_api_credentials(kind=legacy_env), hash only,
                                          bound to the member's principal (system) or to a legacy
                                          service account for that member (human)
  BOSS_CONSOLE_PASSWORD                   the claim credential, then usr_legacy_shared (Operator,
                                          30 days after the claim, D25)
  BOSS_CONSOLE_AUTH=network               usr_network_operator: Owner minus identity writes
                                          before the claim,
                                          Operator for <=30 days after, then refused (D27)
  SCIM users / groups / bearer tokens     pending people with their email, `scim` groups with
                                          members and bindings, and the raw bearer tokens hashed
                                          into kind=scim credentials of sys_scim_legacy
  agent roles with an agent_id            agent principal rows (Passports are S3's)
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from . import audit
from .audit import Outcome, SecurityEvent
from .ids import new_id
from .legacy import (
    LEGACY_CONSOLE_PRINCIPAL,
    LEGACY_SHARED_PRINCIPAL,
    LEGACY_SYSTEM_PREFIX,
    LEGACY_WORKER_PRINCIPAL,
    NETWORK_OPERATOR_PRINCIPAL,
    SCIM_LEGACY_PRINCIPAL,
    LegacyKind,
)
from .permissions_gen import PERMISSION_META, ROLE_PERMISSIONS, SYSTEM_PRINCIPALS
from .policy import get_policy

# ---- today's 21 capabilities -> catalogue permissions --------------------------------------------

#: Each of today's capability strings (members.py seeds, mcp/api_surface.py) mapped to the
#: catalogue permissions that do the same job. Pinned by a test: all 21 present, every target in
#: the catalogue, none system-only.
CAPABILITY_PERMISSIONS: dict[str, frozenset[str]] = {
    "agent_chat": frozenset({"assistant:chat", "agent:read", "meeting:read"}),
    "command_submit": frozenset({"assistant:chat", "code_task:create", "work_item:manage",
                                 "autopilot:read", "autopilot:operate", "meeting:convene",
                                 "discovery:create"}),
    "task_create": frozenset({"code_task:create", "code_task:steer", "code_task:retry",
                              "code_task:cancel", "work_item:manage", "job:read"}),
    "decision_resolve": frozenset({"decision:read", "decision:approve", "decision:execute"}),
    "pack_manage": frozenset({"pack:read", "pack:install"}),
    "module_manage": frozenset({"skill:read", "skill:manage", "mcp_server:read",
                                "mcp_server:register", "agent_ability:assign"}),
    "role_review": frozenset({"agent:read", "agent:update", "agent:publish", "agent:lifecycle",
                              "agent:test", "autonomy:read"}),
    "self_improvement_manage": frozenset({"improvement:read", "improvement:manage"}),
    "audit_export": frozenset({"activity:read", "activity:export"}),
    "health_refresh": frozenset({"system:read", "reliability:read", "reliability:respond",
                                 "observability:read"}),
    "comms_manage": frozenset({"comms:read", "comms:send", "comms:manage"}),
    "member_manage": frozenset({"directory:read", "user:read", "user:invite", "user:update",
                                "user:disable", "role_binding:read", "role_binding:assign",
                                "group:read", "group:manage_members"}),
    "budget_manage": frozenset({"budget:read", "budget:manage"}),
    "entitlement_manage": frozenset({"entitlement:read", "entitlement:manage"}),
    "enterprise_manage": frozenset({"identity_provider:read", "identity_provider:manage"}),
    "infra_provision": frozenset({"infra:read", "infra:provision", "host:read",
                                  "deployment:read", "deployment:create",
                                  "deployment_target:manage"}),
    "providerops_manage": frozenset({"model_provider:read", "model_provider:manage"}),
    "github_manage": frozenset({"github:read", "github:connect", "github:manage", "github:sync",
                                "repository:read", "repository:register", "repository:manage"}),
    "integration_manage": frozenset({"integration:read", "integration:manage"}),
    "workflow_manage": frozenset({"workflow:read", "workflow:create", "workflow:publish",
                                  "workflow:install", "workflow:delete",
                                  "workflow_schedule:manage"}),
    "workflow_execute": frozenset({"workflow_run:read", "workflow_run:start",
                                   "workflow_run:control"}),
}
ALL_CAPABILITIES = frozenset(CAPABILITY_PERMISSIONS)

#: Every active member could read its project today (require_project_reader): the Viewer set.
MEMBER_BASELINE = frozenset(ROLE_PERMISSIONS["viewer"])

#: The fixed set of the legacy SYSTEM principals (the console key, metatron, coo, system): "a
#: fixed Owner-equivalent set, used only by legacy credentials" (§12 M0, M2). Today those
#: credentials can do everything; until the cutoff they still can.
LEGACY_OWNER_SET: frozenset[str] = frozenset(ROLE_PERMISSIONS["owner"])
#: BOSS_WORKER_PRINCIPAL's key keeps today's reach plus the worker's own system set until S3's
#: run tokens replace the shared worker key (M4).
WORKER_LEGACY_SET: frozenset[str] = LEGACY_OWNER_SET | frozenset(ROLE_PERMISSIONS["system_worker"])
#: The network operator BEFORE the claim (§4.12): Owner-equivalent, except that "it may claim the
#: first Owner, and it may not touch any other identity setting" — so no identity writes. The
#: claim itself is S1's public route and needs nothing from here.
IDENTITY_CATEGORY = "Identity and access"
BRIDGE_SET: frozenset[str] = frozenset(
    p for p in ROLE_PERMISSIONS["owner"]
    if PERMISSION_META[p].category != IDENTITY_CATEGORY or p.endswith(":read")
    or p in ("directory:read", "api_key:manage_own", "connection:use_own",
             "delegation:read_all", "connection:read_all"))

#: sys_legacy_console FROM THE CLAIM ON (operator decision, 2026-10-06): real Owners manage
#: identity from then, so the shared key loses every identity write at once — people, groups,
#: roles and bindings, owners, service accounts and keys, sessions, agent passports and
#: delegations, connections, providers, the sign-in policy and the audit export — and keeps
#: everything else (and every identity read) until the cutoff.
CONSOLE_AFTER_CLAIM_SET: frozenset[str] = frozenset(
    p for p in LEGACY_OWNER_SET
    if PERMISSION_META[p].category != IDENTITY_CATEGORY
    or p.endswith(":read") or p.endswith(":read_all"))

LEGACY_BRIDGE_ROLE_KEY = "legacy_bridge"
ROLE_ORDER = ("viewer", "operator", "admin", "owner")
LIVE_SESSION_GRACE = timedelta(hours=24)

#: system members of today and the principal each becomes
SYSTEM_MEMBER_PRINCIPALS = {
    "executive_console": LEGACY_CONSOLE_PRINCIPAL,
    "metatron": LEGACY_SYSTEM_PREFIX + "metatron",
    "coo": LEGACY_SYSTEM_PREFIX + "coo",
    "system": LEGACY_WORKER_PRINCIPAL,
}


def capability_permissions(capabilities: Iterable[str]) -> frozenset[str]:
    """Catalogue permissions for a list of today's capability strings ('*' = all of them).
    Unknown strings map to nothing (they granted nothing today either)."""
    caps = set(capabilities or ())
    if "*" in caps:
        caps = set(ALL_CAPABILITIES)
    out: set[str] = set()
    for cap in caps:
        out |= CAPABILITY_PERMISSIONS.get(str(cap), frozenset())
    return frozenset(out)


def member_permissions(capabilities: Iterable[str]) -> frozenset[str]:
    """What a legacy member could do: its capabilities' permissions plus reading its project."""
    return MEMBER_BASELINE | capability_permissions(capabilities)


def nearest_builtin_role(permissions: Iterable[str]) -> str:
    """The smallest built-in role holding every one of `permissions` (for invitations: the
    suggested role for the person who takes over a placeholder)."""
    wanted = set(permissions)
    for role in ROLE_ORDER:
        if wanted <= set(ROLE_PERMISSIONS[role]):
            return role
    return "owner"


def system_member_principal(member_id: str, *, worker_member: str = "system") -> str:
    if member_id == worker_member:
        return LEGACY_WORKER_PRINCIPAL
    if member_id in SYSTEM_MEMBER_PRINCIPALS:
        return SYSTEM_MEMBER_PRINCIPALS[member_id]
    return LEGACY_SYSTEM_PREFIX + _slug(member_id)


def legacy_system_set(principal_id: str) -> Optional[frozenset[str]]:
    """The fixed permission set of a legacy system principal (None for any other id)."""
    if principal_id == LEGACY_WORKER_PRINCIPAL:
        return WORKER_LEGACY_SET
    if principal_id == SCIM_LEGACY_PRINCIPAL:
        return frozenset(ROLE_PERMISSIONS["system_scim"])
    if principal_id == LEGACY_CONSOLE_PRINCIPAL or principal_id.startswith(LEGACY_SYSTEM_PREFIX):
        return LEGACY_OWNER_SET
    return None


# ---- small SQL helpers --------------------------------------------------------------------------

def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")[:40] or "member"


def token_hash(token: str) -> str:
    """SHA-256 hex, the same definition as authn.hash_token (IDENTITY.md §5.1)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


def default_org(db: Any, settings: Any = None) -> Optional[str]:
    if settings is None:
        from ..config import get_settings
        settings = get_settings()
    wanted = getattr(settings, "default_org_id", None) or "org_boss"
    if db.execute(_sql("SELECT 1 FROM orgs WHERE id = :o"), {"o": wanted}).first():
        return wanted
    row = db.execute(_sql("SELECT id FROM orgs ORDER BY created_at, id LIMIT 1")).first()
    return row[0] if row else None


def ensure_principal(db: Any, pid: str, org_id: str, kind: str, status: str, display: str, *,
                     network_bound: bool = False, legacy_member_ids: Iterable[str] = (),
                     created_by: str = "migration") -> bool:
    row = db.execute(_sql(
        "INSERT INTO identity_principals (id, org_id, kind, status, display_name, network_bound, "
        "legacy_member_ids, created_by) VALUES (:id, :o, :k, :s, :d, :nb, :lm, :by) "
        "ON CONFLICT (id) DO NOTHING RETURNING id"),
        {"id": pid, "o": org_id, "k": kind, "s": status, "d": display[:200], "nb": network_bound,
         "lm": list(legacy_member_ids), "by": created_by}).first()
    return row is not None


def mapping(db: Any, org_id: str, kind: LegacyKind, ref: str) -> Optional[dict[str, Any]]:
    row = db.execute(_sql(
        "SELECT principal_id, access_role_key, retire_after, first_used_at, last_used_at, "
        "use_count, retired_at FROM identity_legacy_mappings WHERE org_id = :o "
        "AND legacy_kind = :k AND legacy_ref = :r"),
        {"o": org_id, "k": kind.value, "r": ref}).mappings().first()
    return dict(row) if row else None


def put_mapping(db: Any, org_id: str, kind: LegacyKind, ref: str, principal_id: str,
                role_key: Optional[str] = None) -> None:
    db.execute(_sql(
        "INSERT INTO identity_legacy_mappings (org_id, legacy_kind, legacy_ref, principal_id, "
        "access_role_key) VALUES (:o, :k, :r, :p, :role) ON CONFLICT (org_id, legacy_kind, "
        "legacy_ref) DO UPDATE SET principal_id = EXCLUDED.principal_id, access_role_key = "
        "COALESCE(EXCLUDED.access_role_key, identity_legacy_mappings.access_role_key)"),
        {"o": org_id, "k": kind.value, "r": ref, "p": principal_id, "role": role_key})


def ensure_custom_role(db: Any, org_id: str, key: str, name: str, permissions: Iterable[str], *,
                       scopes: tuple[str, ...] = ("org", "project"), system: bool = True) -> str:
    """An org-scoped access role holding exactly `permissions` (legacy mappings only)."""
    perms = sorted(set(permissions))
    row = db.execute(_sql("SELECT id FROM identity_access_roles WHERE org_id = :o AND key = :k"),
                     {"o": org_id, "k": key}).first()
    if row:
        return row[0]
    role_id = new_id("arl")
    db.execute(_sql(
        "INSERT INTO identity_access_roles (id, org_id, key, name, description, builtin, system, "
        "bindable_scopes, version, created_by) VALUES (:id, :o, :k, :n, :d, false, :sys, :sc, 1, "
        "'migration')"),
        {"id": role_id, "o": org_id, "k": key, "n": name[:120], "sys": system, "sc": list(scopes),
         "d": "Created by the identity migration: exactly what this legacy access allowed."})
    for perm in perms:
        db.execute(_sql("INSERT INTO identity_access_role_permissions (role_id, permission_key) "
                        "VALUES (:r, :p) ON CONFLICT DO NOTHING"), {"r": role_id, "p": perm})
    db.execute(_sql("INSERT INTO identity_access_role_versions (role_id, version, permissions, "
                    "changed_by) VALUES (:r, 1, :p, 'migration') ON CONFLICT DO NOTHING"),
               {"r": role_id, "p": perms})
    return role_id


def legacy_role_for(db: Any, org_id: str, permissions: frozenset[str], label: str) -> str:
    digest = hashlib.sha256(",".join(sorted(permissions)).encode()).hexdigest()[:10]
    return ensure_custom_role(db, org_id, f"legacy_{digest}", f"Legacy access ({label})",
                              permissions)


def bind(db: Any, org_id: str, subject_id: str, role_id: str, scope_type: str, scope_id: str, *,
         expires_at: Optional[datetime] = None, reason: str = "migration",
         subject_type: str = "principal", granted_by: str = "migration") -> None:
    db.execute(_sql(
        "INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
        "scope_type, scope_id, granted_by, expires_at, reason) VALUES (:id, :o, :st, :s, :r, "
        ":sct, :sci, :by, :exp, :reason) ON CONFLICT (subject_type, subject_id, role_id, "
        "scope_type, scope_id) DO UPDATE SET expires_at = EXCLUDED.expires_at, "
        "reason = EXCLUDED.reason"),
        {"id": new_id("rbd"), "o": org_id, "st": subject_type, "s": subject_id, "r": role_id,
         "sct": scope_type, "sci": scope_id, "by": granted_by, "exp": expires_at,
         "reason": reason})


def unbind(db: Any, subject_id: str, role_id: str) -> None:
    db.execute(_sql("DELETE FROM identity_role_bindings WHERE subject_type = 'principal' AND "
                    "subject_id = :s AND role_id = :r"), {"s": subject_id, "r": role_id})


def bump_epoch(db: Any, org_id: str, reason: str = "identity migration") -> None:
    """S2's authz.bump_epoch (which also NOTIFYs) once it exists; until then a local bump."""
    try:
        from . import authz
        authz.bump_epoch(org_id, db=db, reason=reason)
        return
    except NotImplementedError:
        pass
    db.execute(_sql("INSERT INTO identity_settings (org_id, epoch) VALUES (:o, 1) ON CONFLICT "
                    "(org_id) DO UPDATE SET epoch = identity_settings.epoch + 1, "
                    "updated_at = now()"), {"o": org_id})


def claimed_at(db: Any, org_id: str) -> Optional[datetime]:
    row = db.execute(_sql(
        "SELECT ts FROM identity_security_events WHERE org_id = :o AND event_type = "
        "'bootstrap.claimed' ORDER BY seq LIMIT 1"), {"o": org_id}).first()
    return row[0] if row else None


# ---- M0: seed ------------------------------------------------------------------------------------

@dataclass
class SeedReport:
    org_id: Optional[str]
    principals_created: int = 0
    placeholders: dict[str, str] = field(default_factory=dict)       # member_id -> usr_
    system_members: dict[str, str] = field(default_factory=dict)     # member_id -> sys_
    env_keys: int = 0
    env_keys_revoked: int = 0
    break_glass: int = 0
    seats: int = 0
    scim_users: int = 0
    scim_groups: int = 0
    scim_tokens: int = 0
    agents: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


_seeded: set[str] = set()
_seed_lock = threading.Lock()


def ensure_seeded(db: Any, *, settings: Any = None, now: Optional[datetime] = None
                  ) -> Optional[str]:
    """Run `seed` once per process per org (the bridge calls this before its first lookup, so an
    upgraded install is mapped without anyone running anything)."""
    org_id = default_org(db, settings)
    if org_id is None:
        return None
    if org_id in _seeded and db.execute(_sql(
            "SELECT 1 FROM identity_principals WHERE id = :p AND org_id = :o"),
            {"p": LEGACY_CONSOLE_PRINCIPAL, "o": org_id}).first():
        return org_id      # (a seed in a transaction that rolled back is not trusted)
    with _seed_lock:
        seed(db, settings=settings, org_id=org_id, now=now)
        _seeded.add(org_id)
    return org_id


def forget_seeded() -> None:
    """Tests and the CLI: make the next ensure_seeded re-run the (idempotent) seed."""
    _seeded.clear()


def _settings(settings: Any) -> Any:
    if settings is None:
        from ..config import get_settings
        return get_settings()
    return settings


def network_mode_configured(settings: Any) -> bool:
    return str(getattr(settings, "legacy_console_auth", "") or "").strip().lower() == "network"


def seed(db: Any, *, settings: Any = None, org_id: Optional[str] = None,
         now: Optional[datetime] = None, apply_claimed: bool = True) -> SeedReport:
    """M0, idempotent: re-running it changes nothing that is already mapped, maps what is new
    (members, grants, keys added since), and revokes env-key credentials removed from the env."""
    settings = _settings(settings)
    org_id = org_id or default_org(db, settings)
    report = SeedReport(org_id)
    if org_id is None:
        report.notes.append("no org yet: nothing to map")
        return report
    now = _now(now)
    # one seeder at a time per org: two API workers booting together must not both create
    # placeholders and roles (the second waits, then finds everything mapped)
    lock = int.from_bytes(hashlib.sha256(f"identity_seed:{org_id}".encode()).digest()[:8], "big",
                          signed=True)
    db.execute(_sql("SELECT pg_advisory_xact_lock(:k)"), {"k": lock})
    from . import change_feed
    change_feed.install(db)        # the epoch moves on any identity change from here on
    db.execute(_sql("INSERT INTO identity_settings (org_id) VALUES (:o) ON CONFLICT DO NOTHING"),
               {"o": org_id})
    is_claimed = claimed_at(db, org_id) is not None
    worker_member = str(getattr(settings, "worker_principal", "system") or "system")

    # -- built-in system principals of the catalogue (sys_scim is per provider) --
    for pid in SYSTEM_PRINCIPALS:
        if pid == "sys_scim":
            continue
        report.principals_created += ensure_principal(
            db, pid, org_id, "system", "active", pid.replace("sys_", "BOSS ").replace("_", " "))

    # -- the bridge principals --
    bridge_role = ensure_custom_role(db, org_id, LEGACY_BRIDGE_ROLE_KEY,
                                     "Legacy bridge (today's operator)", BRIDGE_SET,
                                     scopes=("org",))
    report.principals_created += ensure_principal(
        db, LEGACY_CONSOLE_PRINCIPAL, org_id, "system", "active", "Legacy console key",
        legacy_member_ids=["executive_console"])
    report.principals_created += ensure_principal(
        db, LEGACY_SHARED_PRINCIPAL, org_id, "user", "pending", "Shared console password")
    network = network_mode_configured(settings)
    report.principals_created += ensure_principal(
        db, NETWORK_OPERATOR_PRINCIPAL, org_id, "user", "active" if network else "pending",
        "Network operator", network_bound=True)
    if not is_claimed:
        bind(db, org_id, NETWORK_OPERATOR_PRINCIPAL, bridge_role, "org", org_id,
             reason="legacy: network bridge before the first claim (D27)")
    if network:
        put_mapping(db, org_id, LegacyKind.NETWORK_MODE, "BOSS_CONSOLE_AUTH=network",
                    NETWORK_OPERATOR_PRINCIPAL, LEGACY_BRIDGE_ROLE_KEY)
        activate_network_operator(db, org_id)
    if getattr(settings, "legacy_console_api_key", ""):
        put_mapping(db, org_id, LegacyKind.CONSOLE_API_KEY, "BOSS_API_KEY",
                    LEGACY_CONSOLE_PRINCIPAL, LEGACY_BRIDGE_ROLE_KEY)
    if getattr(settings, "legacy_console_password", ""):
        put_mapping(db, org_id, LegacyKind.SHARED_PASSWORD, "BOSS_CONSOLE_PASSWORD",
                    LEGACY_SHARED_PRINCIPAL, "operator")

    # -- members --
    members = _legacy_members(db, org_id)
    for member_id, info in members.items():
        if info["member_type"] == "system" or member_id == worker_member:
            pid = system_member_principal(member_id, worker_member=worker_member)
            report.principals_created += ensure_principal(
                db, pid, org_id, "system", "active", f"Legacy {info['display_name']}",
                legacy_member_ids=[member_id])
            put_mapping(db, org_id, LegacyKind.MEMBER_ID, member_id, pid,
                        LEGACY_BRIDGE_ROLE_KEY)
            report.system_members[member_id] = pid
            continue
        pid = _ensure_placeholder(db, org_id, member_id, info, report)
        perms = member_permissions(info["capabilities"])
        role_id = legacy_role_for(db, org_id, perms, member_id)
        for project_id in info["projects"]:
            bind(db, org_id, pid, role_id, "project", project_id,
                 reason=f"legacy: member {member_id} of project {project_id}")
        if info["org_member"]:
            bind(db, org_id, pid, role_id, "org", org_id, reason=f"legacy: org member {member_id}")
        report.placeholders[member_id] = pid

    _seed_seats(db, org_id, members, report)
    _seed_break_glass(db, org_id, members, report, now, worker_member)
    _seed_env_keys(db, org_id, settings, members, report, now, worker_member)
    _seed_scim(db, org_id, settings, report)
    _seed_agents(db, org_id, report)
    if is_claimed and apply_claimed:
        _apply_claimed_state(db, org_id, settings, now, started=claimed_at(db, org_id))
    bump_epoch(db, org_id)
    return report


def activate_network_operator(db: Any, org_id: str) -> None:
    """The network bridge is live (mapped and not retired): its principal must be active, or
    every tailnet visitor would be refused before the cutoff. Never revives a retired bridge."""
    db.execute(_sql(
        "UPDATE identity_principals SET status = 'active', updated_at = now() WHERE id = :p AND "
        "status = 'pending' AND NOT EXISTS (SELECT 1 FROM identity_legacy_mappings WHERE org_id = "
        ":o AND legacy_kind = 'network_mode' AND retired_at IS NOT NULL)"),
        {"p": NETWORK_OPERATOR_PRINCIPAL, "o": org_id})


def _legacy_members(db: Any, org_id: str) -> dict[str, dict[str, Any]]:
    """Every member id of today, merged across project_members and org_members."""
    out: dict[str, dict[str, Any]] = {}
    rows = db.execute(_sql(
        "SELECT 'project' AS src, pm.project_id AS scope_id, pm.member_id, pm.display_name, "
        "pm.member_type, pm.role_keys, pm.permissions, pm.status FROM project_members pm "
        "UNION ALL SELECT 'org', om.org_id, om.member_id, om.display_name, om.member_type, "
        "om.role_keys, om.permissions, om.status FROM org_members om WHERE om.org_id = :o "
        "ORDER BY 3, 1"), {"o": org_id}).mappings().all()
    for r in rows:
        m = out.setdefault(r["member_id"], {
            "display_name": r["display_name"], "member_type": r["member_type"] or "human",
            "capabilities": set(), "role_keys": set(), "projects": set(), "org_member": False,
            "active": False})
        if r["status"] != "active":
            continue
        m["active"] = True
        m["capabilities"] |= set(_json_list(r["permissions"]))
        m["role_keys"] |= set(_json_list(r["role_keys"]))
        if r["src"] == "project":
            m["projects"].add(r["scope_id"])
        else:
            m["org_member"] = True
        if r["member_type"] == "system":
            m["member_type"] = "system"
    return out


def _ensure_placeholder(db: Any, org_id: str, member_id: str, info: dict[str, Any],
                        report: SeedReport) -> str:
    existing = mapping(db, org_id, LegacyKind.MEMBER_ID, member_id)
    if existing:
        return existing["principal_id"]
    pid = new_id("usr")
    status = "pending" if info["active"] else "disabled"
    report.principals_created += ensure_principal(
        db, pid, org_id, "user", status, info["display_name"] or member_id,
        legacy_member_ids=[member_id])
    put_mapping(db, org_id, LegacyKind.MEMBER_ID, member_id, pid,
                nearest_builtin_role(member_permissions(info["capabilities"])))
    return pid


def _approver_seats() -> set[str]:
    try:
        from ..config import get_settings
        import yaml
        path = get_settings().specpack_root / "policies" / "default_safety.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {"ceo", "cao"}
    approvals = (payload.get("rules") or {}).get("approvals") or {}
    tiers = approvals.get("risk_tier_requirements") or {}
    seats: set[str] = set()
    for tier in tiers.values():
        seats |= {str(s) for s in (tier or {}).get("approvals") or []}
    return seats


def _seed_seats(db: Any, org_id: str, members: dict[str, dict[str, Any]],
                report: SeedReport) -> None:
    seats = _approver_seats()
    for member_id, info in members.items():
        if info["member_type"] == "system" or not info["active"]:
            continue
        held = seats & (info["role_keys"] | {member_id})
        target = mapping(db, org_id, LegacyKind.MEMBER_ID, member_id)
        if not target:
            continue
        for seat in sorted(held):
            row = db.execute(_sql(
                "INSERT INTO identity_approval_seats (org_id, seat_key, principal_id, assigned_by) "
                "VALUES (:o, :s, :p, 'migration') ON CONFLICT DO NOTHING RETURNING seat_key"),
                {"o": org_id, "s": seat, "p": target["principal_id"]}).first()
            report.seats += row is not None


def _seed_break_glass(db: Any, org_id: str, members: dict[str, dict[str, Any]],
                      report: SeedReport, now: datetime, worker_member: str) -> None:
    rows = db.execute(_sql(
        "SELECT id, project_id, target_member_id, granted_permissions, expires_at FROM "
        "break_glass_grants WHERE status = 'active' AND revoked_at IS NULL AND expires_at > :now"),
        {"now": now}).mappings().all()
    for r in rows:
        target = mapping(db, org_id, LegacyKind.MEMBER_ID, r["target_member_id"])
        if not target or target["principal_id"].startswith("sys_"):
            continue    # system principals already hold the bridge set
        perms = member_permissions(_json_list(r["granted_permissions"]))
        role_id = legacy_role_for(db, org_id, perms, f"break-glass {r['id']}")
        subjects = [target["principal_id"]]
        sva = mapping(db, org_id, LegacyKind.ENV_API_KEY, f"member:{r['target_member_id']}")
        if sva:
            subjects.append(sva["principal_id"])
        for subject in subjects:
            bind(db, org_id, subject, role_id, "project", r["project_id"],
                 expires_at=r["expires_at"], reason=f"break_glass:{r['id']}")
        report.break_glass += 1


def member_service_account(db: Any, org_id: str, member_id: str) -> str:
    """The legacy service account machine credentials bound to a HUMAN member act as (env keys,
    OIDC/SAML bearers): active, holding exactly the member's legacy access, owned by the
    member's placeholder. A pending placeholder is not a lockout for those credentials."""
    ref = f"member:{member_id}"
    existing = mapping(db, org_id, LegacyKind.ENV_API_KEY, ref)
    if existing:
        return existing["principal_id"]
    owner = mapping(db, org_id, LegacyKind.MEMBER_ID, member_id)
    sva = new_id("sva")
    ensure_principal(db, sva, org_id, "service_account", "active",
                     f"Legacy credentials of {member_id}", legacy_member_ids=[member_id])
    db.execute(_sql("INSERT INTO identity_service_accounts (principal_id, description, "
                    "owner_principal_ids) VALUES (:p, :d, :o) ON CONFLICT DO NOTHING"),
               {"p": sva, "o": [owner["principal_id"] if owner else "sys_host_cli"],
                "d": f"Created by the identity migration for keys bound to member {member_id}. "
                     "Rotate each key into a boss_sak_ key, then disable this account."})
    put_mapping(db, org_id, LegacyKind.ENV_API_KEY, ref, sva)
    if owner:
        rows = db.execute(_sql(
            "SELECT role_id, scope_type, scope_id, expires_at, reason FROM identity_role_bindings "
            "WHERE subject_type = 'principal' AND subject_id = :p"),
            {"p": owner["principal_id"]}).mappings().all()
        for b in rows:
            bind(db, org_id, sva, b["role_id"], b["scope_type"], b["scope_id"],
                 expires_at=b["expires_at"], reason=b["reason"] or "migration")
    return sva


def _seed_env_keys(db: Any, org_id: str, settings: Any, members: dict[str, dict[str, Any]],
                   report: SeedReport, now: datetime, worker_member: str) -> None:
    console_key = str(getattr(settings, "legacy_console_api_key", "") or "")
    principal_map = dict(getattr(settings, "api_key_principal_map", {}) or {})
    present: set[str] = set()
    for token, member_id in principal_map.items():
        if not token or (console_key and token == console_key):
            continue
        digest = token_hash(token)
        present.add(digest)
        info = members.get(member_id, {"member_type": "human"})
        if info.get("member_type") == "system" or member_id == worker_member \
                or member_id in SYSTEM_MEMBER_PRINCIPALS:
            pid = system_member_principal(member_id, worker_member=worker_member)
            ensure_principal(db, pid, org_id, "system", "active", f"Legacy {member_id}",
                             legacy_member_ids=[member_id])
        else:
            if not mapping(db, org_id, LegacyKind.MEMBER_ID, member_id):
                _ensure_placeholder(db, org_id, member_id,
                                    {"display_name": member_id, "active": True,
                                     "capabilities": set()}, report)
            pid = member_service_account(db, org_id, member_id)
        existing = db.execute(_sql("SELECT id FROM identity_api_credentials WHERE token_hash = :h"),
                              {"h": digest}).first()
        if existing:
            key_id = existing[0]
        else:
            key_id = new_id("key")
            # A legacy env key was never narrower than its member: its scopes are the whole
            # catalogue, so its reach is exactly its principal's, live (a break-glass grant
            # mapped later widens it, a removed binding narrows it, with no stale copy here).
            db.execute(_sql(
                "INSERT INTO identity_api_credentials (id, principal_id, kind, name, token_hash, "
                "scopes, created_by) VALUES (:id, :p, 'legacy_env', :n, :h, :sc, 'migration')"),
                {"id": key_id, "p": pid, "n": f"Legacy env key ({member_id})", "h": digest,
                 "sc": sorted(PERMISSION_META)})
            report.env_keys += 1
        put_mapping(db, org_id, LegacyKind.ENV_API_KEY, key_id, pid)
    # a key removed from the env no longer works today; its credential row follows
    rows = db.execute(_sql("SELECT id, token_hash FROM identity_api_credentials WHERE kind = "
                           "'legacy_env' AND revoked_at IS NULL")).all()
    for key_id, digest in rows:
        if digest not in present:
            db.execute(_sql("UPDATE identity_api_credentials SET revoked_at = :now, revoked_by = "
                            "'migration: removed from BOSS_API_KEYS' WHERE id = :id"),
                       {"now": now, "id": key_id})
            report.env_keys_revoked += 1


def _scim_email(row: dict[str, Any]) -> Optional[str]:
    profile = row.get("raw_profile") or {}
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except ValueError:
            profile = {}
    for entry in profile.get("emails") or []:
        value = (entry or {}).get("value") if isinstance(entry, dict) else entry
        if isinstance(value, str) and "@" in value:
            return value.strip()
    name = str(row.get("user_name") or "")
    return name if "@" in name else None


def _seed_scim(db: Any, org_id: str, settings: Any, report: SeedReport) -> None:
    users = db.execute(_sql(
        "SELECT id, project_id, user_name, display_name, member_id, groups, raw_profile, status "
        "FROM enterprise_scim_users WHERE org_id = :o ORDER BY created_at, id"),
        {"o": org_id}).mappings().all()
    for u in users:
        m = mapping(db, org_id, LegacyKind.MEMBER_ID, u["member_id"])
        if not m:
            pid = new_id("usr")
            ensure_principal(db, pid, org_id, "user",
                             "pending" if u["status"] == "active" else "disabled",
                             u["display_name"], legacy_member_ids=[u["member_id"]])
            put_mapping(db, org_id, LegacyKind.MEMBER_ID, u["member_id"], pid)
        else:
            pid = m["principal_id"]
        email = _scim_email(dict(u))
        if email:
            taken = db.execute(_sql("SELECT principal_id FROM identity_users WHERE lower(email) = "
                                    "lower(:e)"), {"e": email}).first()
            if taken is None:
                db.execute(_sql("INSERT INTO identity_users (principal_id, email) VALUES (:p, :e) "
                                "ON CONFLICT DO NOTHING"), {"p": pid, "e": email})
                report.scim_users += 1
            elif taken[0] != pid:
                report.notes.append(f"SCIM user {u['id']}: email already belongs to another person")
        else:
            report.notes.append(f"SCIM user {u['id']}: no email; invite them to finish")
    groups = db.execute(_sql(
        "SELECT id, project_id, external_id, display_name, role_keys, permissions, status FROM "
        "enterprise_scim_groups WHERE org_id = :o ORDER BY created_at, id"), {"o": org_id}
    ).mappings().all()
    for g in groups:
        key = "scim_" + _slug(g["id"])
        row = db.execute(_sql("SELECT id FROM identity_groups WHERE org_id = :o AND key = :k"),
                         {"o": org_id, "k": key}).first()
        if row:
            gid = row[0]
        else:
            gid = new_id("grp")
            db.execute(_sql(
                "INSERT INTO identity_groups (id, org_id, key, name, description, source, "
                "external_id) VALUES (:id, :o, :k, :n, :d, 'scim', :ext)"),
                {"id": gid, "o": org_id, "k": key, "n": g["display_name"][:120],
                 "ext": g["external_id"], "d": "Imported from SCIM by the identity migration."})
            report.scim_groups += 1
        if g["status"] == "active":
            perms = capability_permissions(_json_list(g["permissions"]))
            if perms:
                role_id = legacy_role_for(db, org_id, perms | MEMBER_BASELINE,
                                          f"SCIM group {g['display_name']}")
                bind(db, org_id, gid, role_id, "project", g["project_id"], subject_type="group",
                     reason=f"legacy: SCIM group {g['id']}")
        tokens = {g["id"], g["display_name"], g["external_id"]} - {None}
        for u in users:
            if u["project_id"] != g["project_id"] or not tokens & set(_json_list(u["groups"])):
                continue
            m = mapping(db, org_id, LegacyKind.MEMBER_ID, u["member_id"])
            if m:
                db.execute(_sql("INSERT INTO identity_group_members (group_id, principal_id, "
                                "source, added_by) VALUES (:g, :p, 'scim', 'migration') "
                                "ON CONFLICT DO NOTHING"), {"g": gid, "p": m["principal_id"]})
    tokens = tuple(getattr(settings, "enterprise_scim_bearer_token_values", ()) or ())
    if tokens:
        ensure_principal(db, SCIM_LEGACY_PRINCIPAL, org_id, "system", "active",
                         "SCIM (legacy bearer tokens)")
        for token in tokens:
            digest = token_hash(token)
            existing = db.execute(_sql("SELECT id FROM identity_api_credentials WHERE "
                                       "token_hash = :h"), {"h": digest}).first()
            if existing:
                key_id = existing[0]
            else:
                key_id = new_id("key")
                db.execute(_sql(
                    "INSERT INTO identity_api_credentials (id, principal_id, kind, name, "
                    "token_hash, scopes, created_by) VALUES (:id, :p, 'scim', :n, :h, "
                    "ARRAY['scim:provision'], 'migration')"),
                    {"id": key_id, "p": SCIM_LEGACY_PRINCIPAL, "h": digest,
                     "n": "Legacy SCIM bearer token (rotate into a boss_scim_ key)"})
                report.scim_tokens += 1
            put_mapping(db, org_id, LegacyKind.SCIM_BEARER, key_id, SCIM_LEGACY_PRINCIPAL)


def _seed_agents(db: Any, org_id: str, report: SeedReport) -> None:
    rows = db.execute(_sql(
        "SELECT DISTINCT ON (agent_id) agent_id, display_name, lifecycle FROM roles WHERE "
        "agent_id LIKE 'agt\\_%' ORDER BY agent_id")).mappings().all()
    for r in rows:
        status = {"active": "active", "paused": "suspended", "retired": "revoked"}.get(
            r["lifecycle"] or "active", "active")
        report.agents += ensure_principal(db, r["agent_id"], org_id, "agent", status,
                                          r["display_name"] or r["agent_id"])


# ---- M1: the claim (S1 calls this in the claim's transaction) ------------------------------------

def on_claimed(org_id: str, owner_principal_id: str, *, db: Any, now: Optional[datetime] = None,
               via: Optional[str] = None, assume_member: Optional[str] = None,
               settings: Any = None) -> datetime:
    """Start the legacy countdown at the first Owner's claim (D25, D27). S1's claim service calls
    this after it has created the Owner, bound `owner` and recorded `bootstrap.claimed`, in the
    same transaction. Returns the cutoff."""
    settings = _settings(settings)
    now = _now(now)
    seed(db, settings=settings, org_id=org_id, now=now, apply_claimed=False)
    if assume_member:
        assume_legacy_member(org_id, owner_principal_id, assume_member, db=db)
    # Every agent gets its Passport now, sponsored by its role's sponsor or this Owner (S3 §7.1):
    # without one, delegation never applies and work would run outside it.
    from . import passports
    passports.upgrade_from_roles(org_id=org_id, db=db, default_sponsor=owner_principal_id)
    return _apply_claimed_state(db, org_id, settings, now, started=now)


def _apply_claimed_state(db: Any, org_id: str, settings: Any, now: datetime,
                         started: Optional[datetime] = None) -> datetime:
    policy = get_policy(org_id, db=db)
    started = started or claimed_at(db, org_id) or now
    cutoff = started + timedelta(days=policy.legacy_cutoff_days)
    operator_role = "arl_operator"
    bridge_role = db.execute(_sql("SELECT id FROM identity_access_roles WHERE org_id = :o AND "
                                  "key = :k"), {"o": org_id, "k": LEGACY_BRIDGE_ROLE_KEY}).first()
    keep_console = bool((policy.extra or {}).get("keep_legacy_console_key"))
    for kind, ref in ((LegacyKind.SHARED_PASSWORD, "BOSS_CONSOLE_PASSWORD"),
                      (LegacyKind.CONSOLE_API_KEY, "BOSS_API_KEY"),
                      (LegacyKind.NETWORK_MODE, "BOSS_CONSOLE_AUTH=network")):
        if kind == LegacyKind.CONSOLE_API_KEY and keep_console:
            db.execute(_sql("UPDATE identity_legacy_mappings SET retire_after = NULL WHERE "
                            "org_id = :o AND legacy_kind = :k AND legacy_ref = :r"),
                       {"o": org_id, "k": kind.value, "r": ref})
            continue
        until = cutoff
        if kind == LegacyKind.NETWORK_MODE and not policy.network_bridge_enabled:
            until = min(cutoff, now)
        db.execute(_sql(
            "UPDATE identity_legacy_mappings SET retire_after = COALESCE(retire_after, :c) "
            "WHERE org_id = :o AND legacy_kind = :k AND legacy_ref = :r AND retired_at IS NULL"),
            {"o": org_id, "k": kind.value, "r": ref, "c": until})
    shared = mapping(db, org_id, LegacyKind.SHARED_PASSWORD, "BOSS_CONSOLE_PASSWORD")
    if shared and shared["retired_at"] is None:
        db.execute(_sql("UPDATE identity_principals SET status = 'active', updated_at = now() "
                        "WHERE id = :p AND status = 'pending'"), {"p": LEGACY_SHARED_PRINCIPAL})
        bind(db, org_id, LEGACY_SHARED_PRINCIPAL, operator_role, "org", org_id,
             expires_at=shared["retire_after"] or cutoff,
             reason="legacy: shared console password as Operator for 30 days (D25)")
    network = mapping(db, org_id, LegacyKind.NETWORK_MODE, "BOSS_CONSOLE_AUTH=network")
    if bridge_role:
        unbind(db, NETWORK_OPERATOR_PRINCIPAL, bridge_role[0])
    if network and network["retired_at"] is None:
        activate_network_operator(db, org_id)
        bind(db, org_id, NETWORK_OPERATOR_PRINCIPAL, operator_role, "org", org_id,
             expires_at=network["retire_after"] or cutoff,
             reason="legacy: network bridge as Operator for at most 30 days (D27)")
    bump_epoch(db, org_id)
    from . import legacy as _legacy
    _legacy.invalidate_cache()
    return cutoff


def assume_legacy_member(org_id: str, principal_id: str, member_id: str, *, db: Any) -> None:
    """A real person takes over a legacy member id (the claimant "is" ceo): history renders as
    "Param (was: ceo)", approval seats move to them, the placeholder is retired."""
    current = mapping(db, org_id, LegacyKind.MEMBER_ID, member_id)
    db.execute(_sql(
        "UPDATE identity_principals SET legacy_member_ids = array_append(legacy_member_ids, :m), "
        "updated_at = now() WHERE id = :p AND NOT (:m = ANY(legacy_member_ids))"),
        {"p": principal_id, "m": member_id})
    if current and current["principal_id"] != principal_id:
        old = current["principal_id"]
        db.execute(_sql(
            "INSERT INTO identity_approval_seats (org_id, seat_key, principal_id, assigned_by) "
            "SELECT org_id, seat_key, :new, 'migration: assumed' FROM identity_approval_seats "
            "WHERE org_id = :o AND principal_id = :old ON CONFLICT DO NOTHING"),
            {"o": org_id, "new": principal_id, "old": old})
        db.execute(_sql("DELETE FROM identity_approval_seats WHERE org_id = :o AND "
                        "principal_id = :old"), {"o": org_id, "old": old})
        if old.startswith("usr_"):
            db.execute(_sql(
                "UPDATE identity_principals SET status = 'revoked', status_changed_at = now(), "
                "status_changed_by = :p, status_reason = :r, updated_at = now() WHERE id = :old "
                "AND status = 'pending'"),
                {"p": principal_id, "old": old, "r": f"assumed by {principal_id}"})
    put_mapping(db, org_id, LegacyKind.MEMBER_ID, member_id, principal_id)


# ---- M5: retirement ------------------------------------------------------------------------------

BRIDGE_PATHS = ((LegacyKind.SHARED_PASSWORD, "BOSS_CONSOLE_PASSWORD", LEGACY_SHARED_PRINCIPAL),
                (LegacyKind.CONSOLE_API_KEY, "BOSS_API_KEY", LEGACY_CONSOLE_PRINCIPAL),
                (LegacyKind.NETWORK_MODE, "BOSS_CONSOLE_AUTH=network", NETWORK_OPERATOR_PRINCIPAL))


@dataclass(frozen=True)
class Retirement:
    kind: str
    retired: bool
    reason: str


def people_sessions_exist(db: Any, org_id: str, now: datetime) -> bool:
    """Has anyone signed in with a real session recently? (the console has moved off the key)"""
    row = db.execute(_sql(
        "SELECT 1 FROM identity_sessions s JOIN identity_principals p ON p.id = s.principal_id "
        "WHERE p.org_id = :o AND p.kind = 'user' AND NOT p.network_bound AND p.id <> :shared "
        "AND s.last_seen_at > :since LIMIT 1"),
        {"o": org_id, "shared": LEGACY_SHARED_PRINCIPAL, "since": now - LIVE_SESSION_GRACE}
    ).first()
    return row is not None


def retire_due(org_id: str, *, db: Any, now: Optional[datetime] = None,
               actor_principal_id: str = "sys_host_cli") -> list[Retirement]:
    """Retire every bridge whose date has passed. The console key is the one exception that
    waits: while it was used in the last day and nobody has a real session yet, retiring it would
    lock the console out (it still sends the key) — it stays, and posture shows it red."""
    now = _now(now)
    from . import legacy as _legacy
    _legacy.flush_usage(db)
    out: list[Retirement] = []
    for kind, ref, pid in BRIDGE_PATHS:
        m = mapping(db, org_id, kind, ref)
        if not m or m["retired_at"] is not None or m["retire_after"] is None \
                or m["retire_after"] > now:
            continue
        if (kind == LegacyKind.CONSOLE_API_KEY and m["last_used_at"] is not None
                and m["last_used_at"] > now - LIVE_SESSION_GRACE
                and not people_sessions_exist(db, org_id, now)):
            out.append(Retirement(kind.value, False, "deferred: the console still uses the "
                                  "shared key and nobody has signed in with a session yet"))
            continue
        out.append(retire_path(org_id, kind, db=db, now=now, actor_principal_id=actor_principal_id,
                               reason="cutoff reached"))
    return out


def retire_path(org_id: str, kind: LegacyKind, *, db: Any, now: Optional[datetime] = None,
                actor_principal_id: str, actor_ctx: Any = None, reason: str) -> Retirement:
    """Retire one bridge now: the mapping stops working, its principal is disabled (history keeps
    its name), its sessions end. Writes `legacy.path_retired`."""
    now = _now(now)
    if claimed_at(db, org_id) is None:
        # before the claim a bridge is the only way in (network) or the claim credential
        # (password): ending it would leave nobody able to claim
        raise ValueError("claim the first Owner before ending a legacy path")
    ref, pid = {k: (r, p) for k, r, p in BRIDGE_PATHS}[kind]
    m = mapping(db, org_id, kind, ref)
    if not m:
        put_mapping(db, org_id, kind, ref, pid)
    db.execute(_sql("UPDATE identity_legacy_mappings SET retired_at = :now, retire_after = "
                    "LEAST(COALESCE(retire_after, :now), :now) WHERE org_id = :o AND "
                    "legacy_kind = :k AND legacy_ref = :r"),
               {"now": now, "o": org_id, "k": kind.value, "r": ref})
    if pid.startswith("usr_"):
        db.execute(_sql(
            "UPDATE identity_principals SET status = 'disabled', status_changed_at = :now, "
            "status_changed_by = :by, status_reason = :why, updated_at = now() WHERE id = :p "
            "AND status IN ('active', 'pending')"),
            {"now": now, "by": actor_principal_id, "why": f"legacy path retired: {reason}",
             "p": pid})
        db.execute(_sql("UPDATE identity_sessions SET revoked_at = :now, revoked_by = :by, "
                        "revoked_reason = 'legacy path retired' WHERE principal_id = :p AND "
                        "revoked_at IS NULL"), {"now": now, "by": actor_principal_id, "p": pid})
        db.execute(_sql("UPDATE identity_role_bindings SET expires_at = :now WHERE "
                        "subject_type = 'principal' AND subject_id = :p AND (expires_at IS NULL "
                        "OR expires_at > :now) AND role_id <> 'arl_owner'"),
                   {"now": now, "p": pid})
    details = {"path": kind.value, "reason": reason}
    if actor_ctx is not None:
        event = SecurityEvent.from_context(actor_ctx, "legacy.path_retired", Outcome.SUCCESS,
                                           target_type="principal", target_id=pid, details=details)
    else:
        event = SecurityEvent(org_id=org_id, event_type="legacy.path_retired",
                              outcome=Outcome.SUCCESS, actor_principal_id=actor_principal_id,
                              actor_kind="system" if actor_principal_id.startswith("sys_") else
                              None, target_type="principal", target_id=pid, details=details)
    audit.record(event, db=db)
    bump_epoch(db, org_id)
    from . import legacy as _legacy
    _legacy.invalidate_cache()
    return Retirement(kind.value, True, reason)


def extend_path(org_id: str, kind: LegacyKind, until: datetime, *, db: Any, actor_ctx: Any,
                now: Optional[datetime] = None) -> datetime:
    """An Owner moves a bridge's cutoff (D25: step-up, audited). At most 90 days from now; a
    retired path cannot be brought back (the decisions say so)."""
    now = _now(now)
    ref, pid = {k: (r, p) for k, r, p in BRIDGE_PATHS}[kind]
    m = mapping(db, org_id, kind, ref)
    if not m:
        raise ValueError(f"{kind.value} is not in use on this install")
    if m["retired_at"] is not None:
        raise ValueError(f"{kind.value} is already retired")
    until = min(until, now + timedelta(days=90))
    db.execute(_sql("UPDATE identity_legacy_mappings SET retire_after = :u WHERE org_id = :o AND "
                    "legacy_kind = :k AND legacy_ref = :r"),
               {"u": until, "o": org_id, "k": kind.value, "r": ref})
    db.execute(_sql("UPDATE identity_role_bindings SET expires_at = :u WHERE subject_type = "
                    "'principal' AND subject_id = :p AND role_id = 'arl_operator'"),
               {"u": until, "p": pid})
    audit.record(SecurityEvent.from_context(
        actor_ctx, "policy.changed", Outcome.SUCCESS, target_type="legacy_path",
        target_id=kind.value, details={"legacy_cutoff": {
            "from": m["retire_after"].isoformat() if m["retire_after"] else None,
            "to": until.isoformat()}}), db=db)
    bump_epoch(db, org_id)
    from . import legacy as _legacy
    _legacy.invalidate_cache()
    return until


def reschedule_cutoffs(org_id: str, *, db: Any) -> None:
    """After `legacy_cutoff_days` changes in policy: move every live bridge's date to
    claim + the new number of days (never past 90 days)."""
    started = claimed_at(db, org_id)
    if started is None:
        return
    policy = get_policy(org_id, db=db)
    until = started + timedelta(days=policy.legacy_cutoff_days)
    for kind, ref, pid in BRIDGE_PATHS:
        db.execute(_sql("UPDATE identity_legacy_mappings SET retire_after = :u WHERE org_id = :o "
                        "AND legacy_kind = :k AND legacy_ref = :r AND retired_at IS NULL AND "
                        "retire_after IS NOT NULL"),
                   {"u": until, "o": org_id, "k": kind.value, "r": ref})
        db.execute(_sql("UPDATE identity_role_bindings SET expires_at = :u WHERE subject_type = "
                        "'principal' AND subject_id = :p AND role_id = 'arl_operator'"),
                   {"u": until, "p": pid})
    from . import legacy as _legacy
    _legacy.invalidate_cache()
