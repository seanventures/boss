"""Agent Passports as principals: a ceiling, a required human sponsor, spawn rules and a depth cap.
Owned by S3. IDENTITY.md §7.1, IDENTITY_PERMISSIONS.md §4.

A Passport is the TEMPLATE (`agt_…`, an `identity_principals` row of kind `agent` plus
`identity_agent_passports` and `identity_agent_ceilings`). A running agent is an instance
(`agt_…#run_…`) that exists only in tokens and lineage.

Rules this module keeps:
  - a sponsor is REQUIRED and must be an active person (`usr_…`);
  - the ceiling holds delegable permissions only (checked here, and again by the DB trigger);
  - `revoked` is terminal: there is no resume or reactivate from it (a common flaw: `reactivate`
    undid revocation). Re-issuing creates a NEW Passport with `reissued_from`;
  - only a person in a human session with `agent_passport:*` changes a Passport; an agent token
    never can (the permissions are not delegable, and `_require_human` refuses any run token);
  - suspend and revoke run the revocation cascade in the same transaction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from . import catalogue
from .delegation import DEFAULT_MAX_DEPTH, HARD_MAX_DEPTH
from .principal import AuthContext, PrincipalKind


class PassportError(Exception):
    """`code` is stable; `status` the HTTP status a route maps it to."""

    def __init__(self, code: str, status: int = 400, **detail: Any):
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Passport:
    agent_id: str
    org_id: str
    role_key: str
    status: str
    sponsor_principal_id: str
    ceiling: frozenset[str]
    external_ceiling: Mapping[str, Any] = field(default_factory=dict)
    can_spawn: bool = False
    allowed_children: tuple[str, ...] = ()
    max_depth: int = DEFAULT_MAX_DEPTH
    autonomous: bool = False
    ceiling_preset: Optional[str] = None
    version: int = 1
    reissued_from: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.status == "active"

    def may_spawn(self, child: "Passport") -> bool:
        """`allowed_children` names role keys (the spec) or agent ids; both are accepted."""
        return self.can_spawn and (child.role_key in self.allowed_children
                                   or child.agent_id in self.allowed_children)


EXTERNAL_PROVIDERS = {"github"}
GITHUB_KEYS = frozenset({"contents", "pull_requests", "issues", "metadata", "workflows", "checks", "statuses",
                         "actions"})
_LEVEL = {"none": 0, "read": 1, "write": 2}


def validate_external_ceiling(value: Any) -> dict[str, dict[str, str]]:
    """{"github": {"contents": "read|write|none", ..., "push_refs": "boss/*"}}. Closed keys only."""
    if not isinstance(value, Mapping):
        raise PassportError("bad_external_ceiling", 422)
    out: dict[str, dict[str, str]] = {}
    for provider, perms in value.items():
        if provider not in EXTERNAL_PROVIDERS or not isinstance(perms, Mapping):
            raise PassportError("bad_external_ceiling", 422, provider=str(provider))
        clean: dict[str, str] = {}
        for key, level in perms.items():
            if key == "push_refs":
                if not isinstance(level, str) or not level.startswith("boss/") or ".." in level:
                    raise PassportError("bad_external_ceiling", 422, key=key)
                clean[key] = level
            elif key in GITHUB_KEYS and level in _LEVEL:
                if level != "none":
                    clean[key] = level
            else:
                raise PassportError("bad_external_ceiling", 422, key=str(key))
        out[provider] = clean
    return out


def above_connection(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Keys a change RAISES above what the BOSS GitHub App asks GitHub for (github_setup.APP_PERMISSIONS):
    a limit no token can ever reach is a promise BOSS cannot keep, so it is refused rather than
    stored. Only raises are checked: a preset written before the App's set changed still loads."""
    from .github_setup import APP_PERMISSIONS

    over = []
    old = (before or {}).get("github") or {}
    for key, level in ((after or {}).get("github") or {}).items():
        if key == "push_refs":
            continue
        raised = _LEVEL.get(level, 0) > _LEVEL.get(old.get(key, "none"), 0)
        if raised and _LEVEL.get(level, 0) > _LEVEL.get(APP_PERMISSIONS.get(key, "none"), 0):
            over.append(key)
    return sorted(over)


def _external_widening(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    widened = []
    for provider, perms in after.items():
        old = (before or {}).get(provider) or {}
        for key, level in perms.items():
            if key != "push_refs" and _LEVEL.get(level, 0) > _LEVEL.get(old.get(key, "none"), 0):
                widened.append(f"{provider}.{key}")
    return widened


def preset_for_role(role_key: str) -> Optional[str]:
    from .permissions_gen import PASSPORT_PRESETS

    key = (role_key or "").lower()
    for preset in PASSPORT_PRESETS.values():
        if key in preset.agents:
            return preset.key
    return None


def _preset(preset_key: str):
    from .permissions_gen import PASSPORT_PRESETS

    try:
        return PASSPORT_PRESETS[preset_key]
    except KeyError as exc:
        raise PassportError("unknown_preset", 422) from exc


def _require_delegable(perms: frozenset[str]) -> None:
    for key in perms:
        if not catalogue.is_known(key):
            raise PassportError("unknown_permission", 422)
        if not catalogue.meta(key).delegable:
            raise PassportError("not_delegable", 422)


def _principal_status(db: Any, principal_id: str) -> Optional[tuple[str, str]]:
    from sqlalchemy import text

    row = db.execute(text("SELECT kind, status FROM identity_principals WHERE id = :id"),
                     {"id": principal_id}).first()
    return (row[0], row[1]) if row else None


def ensure_passport(*, org_id: str, agent_id: str, role_key: str, display_name: str,
                    sponsor_principal_id: str, db: Any, preset: Optional[str] = None,
                    can_spawn: Optional[bool] = None, allowed_children: Optional[tuple[str, ...]] = None,
                    max_depth: Optional[int] = None, autonomous: bool = False,
                    created_by: Optional[str] = None) -> Passport:
    """Create (or return) the Passport for an existing agent (`roles.agent_id`). Idempotent: an
    existing Passport is returned unchanged; changes go through `set_ceiling` / `set_rules`."""
    from sqlalchemy import text

    from . import lineage

    if not agent_id.startswith("agt_"):
        raise PassportError("bad_agent_id", 422)
    existing = get(agent_id, db=db)
    if existing is not None:
        return existing
    sponsor = _principal_status(db, sponsor_principal_id) if sponsor_principal_id else None
    if sponsor is None or sponsor[0] != PrincipalKind.USER.value:
        raise PassportError("sponsor_required", 422)
    if sponsor[1] != "active":
        raise PassportError("sponsor_inactive", 422)
    preset_key = preset or preset_for_role(role_key)
    ceiling: frozenset[str] = frozenset()
    external: Mapping[str, Any] = {}
    if preset_key:
        p = _preset(preset_key)
        ceiling, external = frozenset(p.permissions), dict(p.external_ceiling)
    _require_delegable(ceiling)
    depth = DEFAULT_MAX_DEPTH if max_depth is None else max_depth
    if not 0 <= depth <= HARD_MAX_DEPTH:
        raise PassportError("bad_max_depth", 422)
    spawn = bool(can_spawn) if can_spawn is not None else preset_key == "planner"
    children = tuple(allowed_children or ())
    db.execute(text(
        "INSERT INTO identity_principals (id, org_id, kind, status, display_name, created_by) "
        "VALUES (:id, :org, 'agent', 'active', :name, :by) ON CONFLICT (id) DO NOTHING"),
        {"id": agent_id, "org": org_id, "name": display_name or role_key, "by": created_by})
    db.execute(text(
        "INSERT INTO identity_agent_passports (agent_id, role_key, sponsor_principal_id, ceiling_preset, "
        " can_spawn, allowed_children, max_depth, autonomous, external_ceiling) VALUES "
        "(:id, :rk, :sp, :preset, :spawn, :children, :depth, :auto, CAST(:ext AS JSONB))"),
        {"id": agent_id, "rk": role_key, "sp": sponsor_principal_id, "preset": preset_key,
         "spawn": spawn, "children": list(children), "depth": depth, "auto": autonomous,
         "ext": json.dumps(external)})
    for key in sorted(ceiling):
        db.execute(text("INSERT INTO identity_agent_ceilings (agent_id, permission_key) VALUES (:a, :k)"),
                   {"a": agent_id, "k": key})
    passport = get(agent_id, db=db)
    lineage.passport_event(db, passport, "agent.passport.born", actor_id=created_by,
                           details={"preset": preset_key, "ceiling": sorted(ceiling)})
    return passport


def get(agent_id: str, *, db: Any) -> Optional[Passport]:
    from sqlalchemy import text

    row = db.execute(text(
        "SELECT p.id AS agent_id, p.org_id, p.status, a.role_key, a.sponsor_principal_id, a.ceiling_preset, "
        " a.version, a.can_spawn, a.allowed_children, a.max_depth, a.autonomous, a.external_ceiling, "
        " a.reissued_from FROM identity_principals p JOIN identity_agent_passports a ON a.agent_id = p.id "
        "WHERE p.id = :id AND p.kind = 'agent'"), {"id": agent_id}).mappings().first()
    if row is None:
        return None
    ceiling = frozenset(db.execute(text("SELECT permission_key FROM identity_agent_ceilings WHERE agent_id = :id"),
                                   {"id": agent_id}).scalars().all())
    return Passport(agent_id=row["agent_id"], org_id=row["org_id"], role_key=row["role_key"],
                    status=row["status"], sponsor_principal_id=row["sponsor_principal_id"],
                    ceiling=ceiling, external_ceiling=row["external_ceiling"] or {},
                    can_spawn=row["can_spawn"], allowed_children=tuple(row["allowed_children"] or ()),
                    max_depth=row["max_depth"], autonomous=row["autonomous"],
                    ceiling_preset=row["ceiling_preset"], version=row["version"],
                    reissued_from=row["reissued_from"])


def require(agent_id: str, *, db: Any) -> Passport:
    p = get(agent_id, db=db)
    if p is None:
        raise PassportError("not_found", 404)
    return p


# ---- changes (people only) ---------------------------------------------------------------------------

def _require_human(ctx: AuthContext, permission: str, org_id: str, db: Any) -> None:
    from . import authz
    from .scope import ResolvedScope

    if ctx is None or ctx.run_token is not None or ctx.principal.kind != PrincipalKind.USER:
        raise PassportError("people_only", 403)
    if ctx.principal.org_id != org_id:
        raise PassportError("not_found", 404)
    decision = authz.check(ctx, permission, ResolvedScope.org_level(org_id), db=db)
    if not decision.allowed:
        raise PassportError(decision.error_body().get("error", "forbidden"), decision.status)


def set_ceiling(ctx: AuthContext, agent_id: str, permissions: frozenset[str], *, db: Any) -> Passport:
    """Replace the ceiling (`agent_passport:manage`, step-up via the PDP). Lowering it narrows every
    live run at its next call (J10): the ceiling is read live at every hop."""
    from sqlalchemy import text

    from . import lineage

    before = require(agent_id, db=db)
    _require_human(ctx, "agent_passport:manage", before.org_id, db)
    if before.status == "revoked":
        raise PassportError("revoked", 409)
    perms = frozenset(permissions)
    _require_delegable(perms)
    added = perms - before.ceiling
    if added:
        # No escalation by proxy: you cannot raise an agent's ceiling above what you hold yourself.
        from . import grants as _grants

        mine = _grants.person_live(db, ctx.principal, None)
        over = sorted(added - mine)
        if over:
            raise PassportError("forbidden", 403, permission=over[0])
    db.execute(text("DELETE FROM identity_agent_ceilings WHERE agent_id = :a"), {"a": agent_id})
    for key in sorted(perms):
        db.execute(text("INSERT INTO identity_agent_ceilings (agent_id, permission_key) VALUES (:a, :k)"),
                   {"a": agent_id, "k": key})
    # 'custom': a person set this ceiling, so no upgrade or restart ever "repairs" it — not even
    # an empty one a human chose (#LOW: a restart used to restore the preset).
    db.execute(text("UPDATE identity_agent_passports SET version = version + 1, ceiling_preset = 'custom', "
                    "updated_at = now() WHERE agent_id = :a"), {"a": agent_id})
    after = require(agent_id, db=db)
    lineage.passport_event(db, after, "agent.passport.ceiling_changed", ctx=ctx,
                           details={"removed": sorted(before.ceiling - perms),
                                    "added": sorted(perms - before.ceiling)})
    return after


def set_rules(ctx: AuthContext, agent_id: str, *, db: Any, can_spawn: Optional[bool] = None,
              allowed_children: Optional[tuple[str, ...]] = None, max_depth: Optional[int] = None,
              sponsor_principal_id: Optional[str] = None,
              external_ceiling: Optional[Mapping[str, Any]] = None) -> Passport:
    from sqlalchemy import text

    from . import lineage

    before = require(agent_id, db=db)
    _require_human(ctx, "agent_passport:manage", before.org_id, db)
    if before.status == "revoked":
        raise PassportError("revoked", 409)
    if max_depth is not None and not 0 <= max_depth <= HARD_MAX_DEPTH:
        raise PassportError("bad_max_depth", 422)
    if external_ceiling is not None:
        # A ceiling change: `agent_passport:manage` (checked above, step-up via the PDP), audited
        # below with the before/after and the widened keys named.
        external_ceiling = validate_external_ceiling(external_ceiling)
        over = above_connection(before.external_ceiling, external_ceiling)
        if over:
            raise PassportError("above_github_connection", 422, key=over[0])
    if sponsor_principal_id is not None:
        sponsor = _principal_status(db, sponsor_principal_id)
        if sponsor is None or sponsor[0] != "user" or sponsor[1] != "active":
            raise PassportError("sponsor_required", 422)
    db.execute(text(
        "UPDATE identity_agent_passports SET can_spawn = COALESCE(:spawn, can_spawn), "
        " allowed_children = COALESCE(:children, allowed_children), max_depth = COALESCE(:depth, max_depth), "
        " sponsor_principal_id = COALESCE(:sponsor, sponsor_principal_id), "
        " external_ceiling = COALESCE(CAST(:ext AS JSONB), external_ceiling), version = version + 1, "
        " updated_at = now() WHERE agent_id = :a"),
        {"a": agent_id, "spawn": can_spawn, "children": list(allowed_children) if allowed_children is not None else None,
         "depth": max_depth, "sponsor": sponsor_principal_id,
         "ext": json.dumps(external_ceiling) if external_ceiling is not None else None})
    after = require(agent_id, db=db)
    if external_ceiling is not None and dict(external_ceiling) != dict(before.external_ceiling):
        lineage.passport_event(db, after, "agent.passport.ceiling_changed", ctx=ctx,
                               details={"external_before": dict(before.external_ceiling),
                                        "external_after": dict(external_ceiling),
                                        "widened": _external_widening(before.external_ceiling, external_ceiling)})
    if sponsor_principal_id is not None and sponsor_principal_id != before.sponsor_principal_id:
        lineage.passport_event(db, after, "agent.passport.sponsor_changed", ctx=ctx,
                               details={"from": before.sponsor_principal_id, "to": sponsor_principal_id})
    rules_before = {"can_spawn": before.can_spawn, "allowed_children": sorted(before.allowed_children),
                    "max_depth": before.max_depth}
    rules_after = {"can_spawn": after.can_spawn, "allowed_children": sorted(after.allowed_children),
                   "max_depth": after.max_depth}
    if rules_before != rules_after:
        # Who may spawn whom, how deep: a change of authority like a ceiling's, so it is lineage.
        lineage.passport_event(db, after, "agent.passport.rules_changed", ctx=ctx,
                               details={"before": rules_before, "after": rules_after})
    return after


def _set_status(db: Any, agent_id: str, status: str, actor_id: Optional[str], reason: str) -> None:
    from sqlalchemy import text

    db.execute(text("UPDATE identity_principals SET status = :s, status_changed_at = now(), "
                    "status_changed_by = :by, status_reason = :r, updated_at = now() WHERE id = :id"),
               {"s": status, "by": actor_id, "r": reason[:500], "id": agent_id})


def suspend(ctx: AuthContext, agent_id: str, *, reason: str, db: Any):
    """Halt every run whose chain holds the agent and pause the grants naming it (cascade)."""
    from . import cascade

    p = require(agent_id, db=db)
    _require_human(ctx, "agent_passport:suspend", p.org_id, db)
    if p.status == "revoked":
        raise PassportError("revoked", 409)
    _set_status(db, agent_id, "suspended", ctx.principal.id, reason)
    return cascade.revoke_cascade(cascade.CascadeTrigger.PASSPORT_SUSPENDED, agent_id, actor=ctx,
                                  reason=reason, db=db)


def resume(ctx: AuthContext, agent_id: str, *, reason: str, db: Any) -> Passport:
    """Suspended -> active. Never from revoked. Tokens revoked by the suspension stay revoked, and
    grants it paused stay paused until their person resumes them."""
    from . import lineage

    p = require(agent_id, db=db)
    _require_human(ctx, "agent_passport:suspend", p.org_id, db)
    if p.status != "suspended":
        raise PassportError("revoked" if p.status == "revoked" else "not_suspended", 409)
    _set_status(db, agent_id, "active", ctx.principal.id, reason)
    after = require(agent_id, db=db)
    lineage.passport_event(db, after, "agent.passport.resumed", ctx=ctx, details={"reason": reason[:200]})
    return after


def revoke(ctx: AuthContext, agent_id: str, *, reason: str, db: Any):
    """Terminal. Grants naming it are revoked; Passports it bore are suspended pending review."""
    from . import cascade

    p = require(agent_id, db=db)
    _require_human(ctx, "agent_passport:revoke", p.org_id, db)
    if p.status == "revoked":
        raise PassportError("revoked", 409)
    _set_status(db, agent_id, "revoked", ctx.principal.id, reason)
    return cascade.revoke_cascade(cascade.CascadeTrigger.PASSPORT_REVOKED, agent_id, actor=ctx,
                                  reason=reason, db=db)


def reissue(ctx: AuthContext, old_agent_id: str, new_agent_id: str, *, db: Any) -> Passport:
    """A revoked Passport comes back only as a new one (new id, lineage `reissued_from`)."""
    from sqlalchemy import text

    from . import lineage

    old = require(old_agent_id, db=db)
    _require_human(ctx, "agent_passport:manage", old.org_id, db)
    if old.status != "revoked":
        raise PassportError("not_revoked", 409)
    new = ensure_passport(org_id=old.org_id, agent_id=new_agent_id, role_key=old.role_key,
                          display_name=old.role_key, sponsor_principal_id=ctx.principal.id, db=db,
                          preset=old.ceiling_preset, can_spawn=old.can_spawn,
                          allowed_children=old.allowed_children, max_depth=old.max_depth,
                          created_by=ctx.principal.id)
    db.execute(text("UPDATE identity_agent_passports SET reissued_from = :o WHERE agent_id = :n"),
               {"o": old_agent_id, "n": new_agent_id})
    new = require(new_agent_id, db=db)
    # §6.11: a re-issued Passport is a ceiling granted now, so the caller must hold it (the
    # caller's transaction rolls the new Passport back on a refusal).
    from . import grants as _grants

    over = sorted(new.ceiling - _grants.person_live(db, ctx.principal, None))
    if over:
        raise PassportError("forbidden", 403, permission=over[0])
    lineage.passport_event(db, new, "agent.passport.reissued", ctx=ctx, details={"from": old_agent_id})
    return new


def born_by_subtree(agent_id: str, *, db: Any) -> list[str]:
    """Every Passport born (transitively) by `agent_id` (`roles.born_by = 'agent:<id>'`)."""
    from sqlalchemy import text

    if not db.execute(text("SELECT to_regclass('roles') IS NOT NULL")).scalar():
        return []
    out: list[str] = []
    frontier = [agent_id]
    seen = {agent_id}
    while frontier:
        rows = db.execute(text("SELECT DISTINCT agent_id FROM roles WHERE born_by = ANY(:b) AND agent_id IS NOT NULL"),
                          {"b": [f"agent:{a}" for a in frontier]}).scalars().all()
        frontier = [r for r in rows if r not in seen]
        seen.update(frontier)
        out.extend(frontier)
    return out


def _repair_empty_ceiling(db: Any, passport: "Passport", preset_key: str) -> None:
    """A Passport an earlier upgrade made with NO ceiling (the preset was looked up by role key and
    missed): give it its preset, once. A ceiling someone set by hand is never touched."""
    import json

    from sqlalchemy import text

    p = _preset(preset_key)
    ceiling = frozenset(p.permissions)
    _require_delegable(ceiling)
    for key in sorted(ceiling):
        db.execute(text("INSERT INTO identity_agent_ceilings (agent_id, permission_key) VALUES (:a, :k) "
                        "ON CONFLICT DO NOTHING"), {"a": passport.agent_id, "k": key})
    db.execute(text("UPDATE identity_agent_passports SET ceiling_preset = :p, external_ceiling = CAST(:e AS JSONB), "
                    "version = version + 1, updated_at = now() WHERE agent_id = :a"),
               {"p": preset_key, "e": json.dumps(dict(p.external_ceiling)), "a": passport.agent_id})
    from . import lineage
    lineage.passport_event(db, get(passport.agent_id, db=db), "agent.passport.ceiling_changed",
                           actor_id="sys_migration", details={"preset": preset_key, "repaired": True})


def upgrade_from_roles(*, org_id: str, db: Any, default_sponsor: Optional[str] = None) -> dict[str, str]:
    """The Passport upgrade (§7.1, migration M0 calls this): every `roles` row with an `agent_id`
    gets an `agent` principal and a Passport with its preset ceiling. The sponsor is the role's
    sponsor mapped to a person through identity_legacy_mappings (member ids such as `ceo`), else
    `default_sponsor` (the claimed Owner). A role whose sponsor maps to nobody is skipped and
    reported: a Passport is never created without an accountable human. Returns agent_id -> outcome."""
    from sqlalchemy import text

    out: dict[str, str] = {}
    if not db.execute(text("SELECT to_regclass('roles') IS NOT NULL")).scalar():
        return out
    rows = db.execute(text("SELECT r.agent_id, r.role_key, r.display_name, r.sponsor, w.org_id, "
                           "r.spec->'persona'->>'callsign' AS callsign FROM roles r "
                           "JOIN projects p ON p.id = r.project_id JOIN workspaces w ON w.id = p.workspace_id "
                           "WHERE r.agent_id IS NOT NULL AND w.org_id = :o"), {"o": org_id}).mappings().all()
    for r in rows:
        # The presets name agents by their persona callsign (Forge, Helm, Juno...), not by the
        # role key (software_engineer, engineering_captain): match either, callsign first.
        preset = preset_for_role(r["callsign"] or "") or preset_for_role(r["role_key"])
        existing = get(r["agent_id"], db=db)
        if existing is not None:
            if not existing.ceiling and not existing.ceiling_preset and preset:
                _repair_empty_ceiling(db, existing, preset)
                out[r["agent_id"]] = f"ceiling repaired: {preset}"
            else:
                out[r["agent_id"]] = "exists"
            continue
        sponsor = None
        if r["sponsor"]:
            sponsor = r["sponsor"] if str(r["sponsor"]).startswith("usr_") else db.execute(text(
                "SELECT principal_id FROM identity_legacy_mappings WHERE org_id = :o AND legacy_kind = 'member_id' "
                "AND legacy_ref = :m"), {"o": org_id, "m": r["sponsor"]}).scalar()
        sponsor = sponsor or default_sponsor
        if not sponsor:
            out[r["agent_id"]] = "skipped: no accountable person"
            continue
        try:
            ensure_passport(org_id=org_id, agent_id=r["agent_id"], role_key=r["role_key"],
                            display_name=r["display_name"] or r["role_key"], sponsor_principal_id=sponsor, db=db,
                            created_by="sys_migration", preset=preset)
            out[r["agent_id"]] = "created"
        except PassportError as exc:
            out[r["agent_id"]] = f"skipped: {exc.code}"
    return out
