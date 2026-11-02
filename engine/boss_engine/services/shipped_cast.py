"""The shipped cast reaches every project, on every boot, not only on a fresh install.

WHY THIS EXISTS
---------------
Roles arrived only through `bootstrap.seed`, which runs when BOSS is installed. An upgrade runs
the API's startup, which synced the schema and the default project's workflow catalogue and
nothing else. So on boss-1 (boss-dogfood, created before the Agent World) the new cast — Atlas,
Helm, Strata, Prism, Relay, Beacon and the four archetypes — was in the YAMLs and on the
catalogue's steps but not in the project: the Passport said "No registered role", every
identity field was "—", and Ask answered "this project has no agent chief_architect".

`sync_project` is the upgrade's half of the install seed, for ONE project:

- every role of the shipped cast packs (the core pack, the archetypes, and any other baked pack
  the project already has) that the project lacks is installed with its persona, born from the
  pack (`agent.born`, born_by spec-pack, born_from {pack, version}) and sealed v1;
- every role those packs' workflows or the meeting seat aliases name (software_engineer, which
  core_v1's coding workflows hand steps to and no core pack installs) is installed the same way,
  so work done under that name is a registered agent's;
- a role that exists keeps what it has, whichever pack (or none) it came from. Only MISSING persona fields are filled — the whole
  persona when it has none; its absent fields too unless an operator edited the role
  (seed_exclusions 'role_spec'), whose persona is theirs — and an MCP holding its spec lacks;
- skills (role_skills) and MCP servers (role_mcp_servers) are granted from the role specs by
  the same additive syncs the install seed runs, so what an operator took away stays away;
- every role without an identity gets one (agent_identity.backfill: quiet, no birth dated today).

Idempotent: a second run finds nothing to do and writes nothing. `sync_all` runs it for every
project on the install, one transaction each, so one bad project cannot stop the others.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

_log = logging.getLogger(__name__)

#: Rides with the core pack on every project: the four "thinks like" agents are part of the
#: roster the console shows, so a project without them showed four unregistered agents.
ARCHETYPES_PACK = "archetypes_pack"
#: Persona fields filled when absent. The rest of a role's spec is the install seed's to write.
_PERSONA_FIELDS = ("version", "archetype", "callsign", "tagline", "about_me", "voice",
                   "phrases", "never", "avatar")


def cast_packs(connection: Connection, settings: Any, specpack: Any,
               project_id: str) -> list[str]:
    """The baked packs whose roles this project should hold, core first."""
    baked = {p["pack_id"] for p in specpack.list_available_packs()}
    wanted = [settings.core_pack_id, ARCHETYPES_PACK]
    try:
        with connection.begin_nested():
            wanted += [str(r) for r in connection.execute(
                text("SELECT pack_id FROM pack_installs WHERE project_id = :p "
                     "AND COALESCE(source, 'baked') = 'baked' ORDER BY pack_id"),
                {"p": project_id}).scalars()]
    except Exception:  # noqa: BLE001 — an older schema without `source` still gets the core cast
        pass
    out: list[str] = []
    for pack_id in wanted:
        if pack_id in baked and pack_id not in out:
            out.append(pack_id)
    return out


def fill_missing(current: dict[str, Any], shipped: dict[str, Any], *,
                 operator_edited: bool) -> dict[str, Any]:
    """`current` with the shipped persona/MCP fields it LACKS. Never changes a present value."""
    out = dict(current or {})
    shipped_persona = shipped.get("persona")
    if isinstance(shipped_persona, dict) and shipped_persona:
        have = out.get("persona")
        if not isinstance(have, dict) or not have:
            out["persona"] = dict(shipped_persona)
        elif not operator_edited:
            merged = dict(have)
            for field in _PERSONA_FIELDS:
                if field not in merged and field in shipped_persona:
                    merged[field] = shipped_persona[field]
            out["persona"] = merged
    # WHICH TEAM, when the role has none — even an operator-edited one: a team is where the
    # agent sits on the org map, not a remit or a voice, and an edited Forge with no team would
    # otherwise sit in "Yours" for ever. A team somebody chose is never changed.
    shipped_team = shipped.get("team")
    if isinstance(shipped_team, str) and shipped_team.strip() and not str(
            out.get("team") or "").strip():
        out["team"] = shipped_team.strip()
    # THE KIT'S ABILITIES, when a role predates them: an upgraded install gets the loadout its
    # role now names, the same way a fresh one does (kit.sync_role_abilities). A role that has
    # its own list keeps it.
    for field in ("abilities", "recommended_abilities"):
        shipped_list = (shipped.get("permissions") or {}).get(field)
        if shipped_list:
            permissions = out.get("permissions")
            permissions = dict(permissions) if isinstance(permissions, dict) else {}
            if field not in permissions:
                permissions[field] = list(shipped_list)
                out["permissions"] = permissions
    shipped_mcp = (shipped.get("permissions") or {}).get("mcp_servers")
    if shipped_mcp:
        permissions = out.get("permissions")
        permissions = dict(permissions) if isinstance(permissions, dict) else {}
        if "mcp_servers" not in permissions:
            permissions["mcp_servers"] = shipped_mcp
            out["permissions"] = permissions
    return out


def _record_pack(connection: Connection, project_id: str, pack_id: str,
                 pack_spec: dict[str, Any], actor: str) -> None:
    """The Packs page lists what a project has: add the pack row if it is not there yet."""
    import json

    connection.execute(
        text("""INSERT INTO pack_installs (id, project_id, pack_id, pack_version, description,
                                           spec, installed_by, status, source)
                VALUES (:id, :p, :pack, :v, :d, CAST(:spec AS JSONB), :by, :st, 'baked')
                ON CONFLICT (project_id, pack_id) DO NOTHING"""),
        {"id": f"pack_{project_id}_{pack_id}", "p": project_id, "pack": pack_id,
         "v": pack_spec.get("version", "0.0.0"), "d": pack_spec.get("description", ""),
         "spec": json.dumps(pack_spec), "by": actor, "st": pack_spec.get("status", "active")},
    )


def sync_project(connection: Connection, settings: Any, project_id: str, *,
                 specpack: Any = None, actor: str = "boss",
                 specs: Optional[dict] = None) -> dict[str, Any]:
    """Install the shipped cast into one project and give every agent an identity.

    Returns {installed: [role_key], filled: [role_key], identities: n}. See the module doc.
    `specs` memoises the parsed pack and role YAMLs across projects (sync_all passes one).
    """
    import json

    from . import agent_identity, seed_exclusions
    from .specpack import RoleNotFoundError, SpecPackService

    specpack = specpack or SpecPackService(settings)
    specs = {} if specs is None else specs

    def load(kind: str, key: str) -> Any:
        if (kind, key) not in specs:
            try:
                specs[(kind, key)] = (specpack.load_pack_spec(key) if kind == "pack"
                                      else specpack.load_role_spec(key))
            except (RoleNotFoundError, ValueError) as error:
                specs[(kind, key)] = error
        found = specs[(kind, key)]
        if isinstance(found, Exception):
            raise found
        return copy.deepcopy(found)

    installed: list[str] = []
    filled: list[str] = []
    seen: set[str] = set()

    def shipped_role(role_key: str) -> Optional[dict[str, Any]]:
        try:
            return load("role", role_key)
        except (RoleNotFoundError, ValueError):
            return None

    def install_or_fill(role_key: str, shipped: dict[str, Any], born_from: dict) -> bool:
        """Install a missing role; fill an existing one's absent persona. True if it changed."""
        row = connection.execute(
            text("SELECT spec FROM roles WHERE project_id = :p AND role_key = :k "
                 "ORDER BY created_at DESC LIMIT 1"),
            {"p": project_id, "k": role_key},
        ).mappings().first()
        if row is None:
            # An agent the operator deleted stays deleted (_upsert_role_spec asks too).
            result = specpack._upsert_role_spec(connection, project_id, role_key, shipped,
                                                actor, born_from=born_from)
            if result.get("status") == "installed":
                installed.append(role_key)
                return True
            return False
        current = row["spec"] if isinstance(row["spec"], dict) else {}
        edited = seed_exclusions.is_excluded(connection, project_id, "role_spec", role_key)
        merged = fill_missing(current, shipped, operator_edited=edited)
        if merged == current:
            return False
        connection.execute(
            text("UPDATE roles SET spec = CAST(:s AS JSONB) "
                 "WHERE project_id = :p AND role_key = :k"),
            {"s": json.dumps(merged), "p": project_id, "k": role_key},
        )
        agent_identity.quietly(connection, agent_identity.seal_identity,
                               project_id, role_key, "persona", actor)
        filled.append(role_key)
        return True

    with agent_identity.sealing_once():
        packs = cast_packs(connection, settings, specpack, project_id)
        for pack_id in packs:
            pack_spec = load("pack", pack_id)
            version = str(pack_spec.get("version") or "0.0.0")
            roles = (pack_spec.get("installs") or {}).get("roles") or []
            changed = False
            for role_key in roles:
                if not isinstance(role_key, str) or not role_key or role_key in seen:
                    continue
                seen.add(role_key)
                shipped = shipped_role(role_key)
                if shipped is None:
                    _log.warning("shipped cast: %s names role %s, which does not load",
                                 pack_id, role_key)
                    continue
                changed |= install_or_fill(role_key, shipped, {
                    "kind": "pack", "pack_id": pack_id, "module_version": version})
            if changed:
                _record_pack(connection, project_id, pack_id, pack_spec, actor)
        # A ROLE THE PROJECT'S WORK NAMES IS A ROLE IT HAS. core_v1's ceo_command_to_pr and
        # governed_coding_pipeline hand steps to software_engineer and the meeting seat
        # "engineer" resolves to it, but no core pack installs it: the roster then showed
        # work done by a shipped agent as "unregistered". Every role a cast pack's workflows
        # or a seat alias name, that BOSS ships a definition for, is installed with the cast.
        for role_key in referenced_roles(settings, specpack, packs, load):
            if role_key in seen:
                continue
            seen.add(role_key)
            shipped = shipped_role(role_key)
            if shipped is None:
                continue
            pack_id, version = home_pack(specpack, role_key, settings)
            install_or_fill(role_key, shipped, {
                "kind": "pack", "pack_id": pack_id, "module_version": version})
        # EVERY SHIPPED ROLE IS SOMEBODY, however it got here. A video_editor from a media
        # install with no pack row, or from an old seed, is not in a cast pack; it still gets
        # the persona its YAML ships (absent fields only; an operator's edit holds).
        # An agent made in the console under a shipped key (spec source 'operator') is the
        # operator's, not the shipped one: it is left as made.
        for role_key in [str(k) for k in connection.execute(
                text("SELECT DISTINCT role_key FROM roles WHERE project_id = :p "
                     "AND COALESCE(spec->>'source', '') <> 'operator' ORDER BY role_key"),
                {"p": project_id}).scalars()]:
            if role_key in seen:
                continue
            seen.add(role_key)
            shipped = shipped_role(role_key)
            if shipped is not None:
                install_or_fill(role_key, shipped, {"kind": "pack"})
        _grant(connection, project_id, actor)
        identities = agent_identity.quietly(connection, agent_identity.backfill, project_id) or 0
        # ONE HOME FOR A REPORTING LINE: the plan (org_assignments) is copied onto
        # roles.reports_to for history; nothing reads the latter for a declared line any more.
        from .agent_acts import copy_declared_lines

        agent_identity.quietly(connection, copy_declared_lines, project_id)
    # THE CATALOGUE WORKFLOWS WHOSE STEPS ARE ROLE AGENTS' WORK (the KG case studies) are
    # re-seeded here, so a project that installed them before they named those agents gets the
    # agents and the named steps on the next boot, not only the default project.
    try:
        with connection.begin_nested():
            from .workflow_catalogue import restaff_role_agent_entries

            restaff_role_agent_entries(connection, project_id, actor=actor)
    except Exception:  # noqa: BLE001 — a boot must still come up; the next seed retries
        _log.warning("shipped cast: role-agent workflows not re-seeded for %s", project_id,
                     exc_info=True)
    if installed or filled:
        _log.info("shipped cast for %s: installed %s, filled %s", project_id, installed, filled)
    return {"project_id": project_id, "installed": installed, "filled": filled,
            "identities": int(identities)}


def install_roles(connection: Connection, project_id: str, role_keys: list[str], *,
                  settings: Any = None, actor: str = "boss", refresh_present: bool = False) -> list[str]:
    """Install the shipped roles a workflow's steps need and the project lacks — the same path
    the cast takes (`_upsert_role_spec`, born from its home pack, skills/MCP by the additive
    syncs, an identity for each). Additive: a present role is left as it is, and one an operator
    deleted stays deleted (seed_exclusions). Returns the role keys installed.

    ``refresh_present``: a person loading a sample again asked for it as it ships now, so a
    present role is brought up to its shipped spec as well (a renamed agent shows its new name);
    _upsert_role_spec still keeps a role the operator edited exactly as they left it."""
    from . import agent_identity
    from .specpack import RoleNotFoundError, SpecPackService

    have = {str(k) for k in connection.execute(
        text("SELECT role_key FROM roles WHERE project_id = :p"), {"p": project_id}).scalars()}
    missing = [k for k in dict.fromkeys(str(r).strip().lower() for r in role_keys)
               if k and (refresh_present or k not in have)]
    if not missing:
        return []
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    specpack = SpecPackService(settings)
    installed: list[str] = []
    with agent_identity.sealing_once():
        for role_key in missing:
            try:
                shipped = specpack.load_role_spec(role_key)
            except (RoleNotFoundError, ValueError):
                _log.warning("install_roles: %s ships no role %s", project_id, role_key)
                continue
            pack_id, version = home_pack(specpack, role_key, settings)
            result = specpack._upsert_role_spec(  # noqa: SLF001 — the cast's own install path
                connection, project_id, role_key, shipped, actor,
                born_from={"kind": "pack", "pack_id": pack_id, "module_version": version})
            if result.get("status") == "installed":
                installed.append(role_key)
        if installed:
            _grant(connection, project_id, actor)
            agent_identity.quietly(connection, agent_identity.backfill, project_id)
    if installed:
        _log.info("roles for %s's workflows: installed %s", project_id, installed)
    return installed


#: Keys in a workflow YAML whose string value names the role a step is handed to.
_ROLE_FIELDS = ("owner_role", "role", "role_key", "captain", "captain_role")


def referenced_roles(settings: Any, specpack: Any, packs: list[str], load: Any) -> list[str]:
    """Role keys the cast packs' workflows and the meeting seat aliases name, canonicalised
    through the one namespace (agent_identity.resolve). Not filtered: the caller keeps those
    with a shipped YAML."""
    import yaml

    from .agent_identity import resolve
    from .agent_world_types import ALIASES

    names: list[str] = list(ALIASES.values())

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _ROLE_FIELDS and isinstance(value, str):
                    names.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    root = getattr(settings, "specpack_root", None)
    for pack_id in packs:
        try:
            workflows = (load("pack", pack_id).get("installs") or {}).get("workflows") or []
        except Exception:  # noqa: BLE001
            continue
        for workflow_id in workflows:
            path = root / "workflows" / f"{workflow_id}.yaml" if root is not None else None
            if path is None or not path.is_file():
                continue
            try:
                walk(yaml.safe_load(path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 — a bad workflow file names nobody
                continue
    out: list[str] = []
    for name in names:
        key = resolve(name) or str(name).strip().lower()
        if key and key not in out:
            out.append(key)
    return out


def home_pack(specpack: Any, role_key: str, settings: Any) -> tuple[str, str]:
    """(pack_id, version) of the baked pack a role comes from — the core pack if it lists it,
    else the first other pack that does, else the core pack. The Passport's "From"."""
    packs = specpack.list_available_packs()
    core = settings.core_pack_id
    ordered = sorted(packs, key=lambda p: (p["pack_id"] != core, p["pack_id"]))
    for pack in ordered:
        if role_key in (pack.get("roles") or []):
            return str(pack["pack_id"]), str(pack.get("version") or "0.0.0")
    version = next((str(p.get("version") or "0.0.0") for p in packs if p["pack_id"] == core),
                   "0.0.0")
    return core, version

def _grant(connection: Connection, project_id: str, actor: str) -> None:
    """Skills and MCP servers from the role specs: the install seed's own additive syncs."""
    from .agents import sync_role_mcp
    from .skill_definitions import sync_role_skills

    _ensure_builtin_mcp_servers(connection, project_id)
    for sync in (sync_role_skills, sync_role_mcp):
        try:
            with connection.begin_nested():
                sync(connection, project_id=project_id, actor=actor)
        except Exception:  # noqa: BLE001 — a grant gap is reported by the sync; never fatal
            _log.warning("shipped cast: %s failed for %s", sync.__name__, project_id,
                         exc_info=True)


def _ensure_builtin_mcp_servers(connection: Connection, project_id: str) -> None:
    """The servers the cast's roles name, so a holding can be granted. Only a MISSING one is
    registered; one the operator removed stays removed (seed_exclusions)."""
    from ..bootstrap import BUILTIN_MCP_SERVERS, builtin_provenance, migrate_builtin_mcp_servers
    from .mcp_registry import ensure_mcp_server

    # AN UPGRADE MOVES THE SEEDED SERVERS TO mcp-tools (skills-mcp spec E3, J11): rows the
    # release seeded at 127.0.0.1 and no person has touched get the served address.
    try:
        with connection.begin_nested():
            migrate_builtin_mcp_servers(connection, project_id)
    except Exception:  # noqa: BLE001 — a boot must still come up; the Kit shows the dead server
        _log.warning("shipped cast: built-in MCP migration failed for %s", project_id,
                     exc_info=True)
    have = {str(n).lower() for n in connection.execute(
        text("SELECT name FROM mcp_servers WHERE project_id = :p"), {"p": project_id}).scalars()}
    for srv in BUILTIN_MCP_SERVERS:
        if srv["name"].lower() in have:
            continue  # present: the install seed's to refresh, not a boot's
        try:
            with connection.begin_nested():
                ensure_mcp_server(
                    connection, project_id, srv["name"], srv["url"],
                    risk_tier=srv["risk_tier"], data_boundary=srv["data_boundary"],
                    description=srv["description"],
                    publisher={"id": "boss", "name": "BOSS", "kind": "system"},
                    provenance=builtin_provenance(),
                    respect_operator=True,
                )
        except Exception:  # noqa: BLE001 — the MCP sync reports a server it cannot find
            _log.warning("shipped cast: MCP server %s not ensured for %s", srv["name"],
                         project_id, exc_info=True)


def sync_all(engine: Engine, settings: Any, *, actor: str = "boss",
             project_ids: Optional[list[str]] = None) -> dict[str, dict[str, Any]]:
    """`sync_project` for every project on the install, each in its own transaction.

    A project another process is syncing right now (two replicas booting) is skipped, not
    raced: the transaction-scoped advisory lock is per project.
    """
    from .specpack import SpecPackService

    specpack = SpecPackService(settings)
    specs: dict = {}
    if project_ids is None:
        with engine.connect() as connection:
            project_ids = [str(p) for p in connection.execute(
                text("SELECT id FROM projects ORDER BY id")).scalars()]
    results: dict[str, dict[str, Any]] = {}
    for project_id in project_ids:
        try:
            with engine.begin() as connection:
                got = connection.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
                    {"k": f"boss.shipped_cast:{project_id}"}).scalar()
                if not got:
                    continue
                results[project_id] = sync_project(connection, settings, project_id,
                                                   specpack=specpack, actor=actor, specs=specs)
        except Exception:  # noqa: BLE001 — one project's failure must not stop the rest
            _log.warning("shipped cast sync failed for %s", project_id, exc_info=True)
    return results
