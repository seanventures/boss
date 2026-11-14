"""Agent versions: save, publish, roll back and diff `boss.agent/v1` documents (AGENT_PLATFORM D2).

DB VERSIONS ARE THE SOURCE OF TRUTH; THE ROLES ROW IS A PROJECTION
------------------------------------------------------------------
`agent_spec_versions` holds every version of every agent, immutable and content-hashed. The
ACTIVE version of an agent is projected, in the caller's transaction, onto:

* `roles` — display name, reporting line and the owned `roles.spec` keys (spec.ROLE_SPEC_KEYS);
  every other key of the row is left alone;
* `role_skills` / `role_mcp_servers` — the grant, through the same functions every other grant
  goes through (skill policy, seed exclusions, events), as a DIFF against what the previous
  version granted: a grant made elsewhere (a kit, the Skills page) is not taken away by a publish
  that never mentioned it, and the baseline every agent holds is never revoked;
* the identity seal — one `agent.sealed` per publish; the fingerprint covers roles.spec, which
  now carries `agent_spec.content_hash`, so the seal is keyed to the version;
* the Passport — a preset ceiling grows to its preset (never past it; a ceiling a person set is
  never touched) and the publish is written to the Passport's lineage with the hash and the
  permissions the spec needs.

A publish that would give an agent a tool its Passport ceiling does not allow is REFUSED
(`ceiling`), unless the agent already had that reach in its previous version (a boot never takes
away what exists; it reports it in the version's `report.ceiling_gaps`).

THE ROW CAN STILL BE WRITTEN THE OLD WAY (PATCH /v1/agents, the Skills page, a kit). `snapshot`
reads the row back into a document (spec.doc_from_role) and, when it differs from the active
version, stores it as the next version without projecting anything: history stays truthful and
nothing is lost. Boot runs it for every role (migration.sync_project), so it is idempotent:
an unchanged row hashes to its active version and nothing is written.
"""
from __future__ import annotations

import difflib
import json
from typing import Any, Iterable, Optional

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..services.core import emit_event, make_id
from . import spec as S

KINDS = ("agent", "profile")
#: Sources a version may record. Only `operator`/`import` are held to the strict ceiling rule.
SOURCES = ("operator", "import", "shipped", "migration", "legacy_edit")
_STRICT_SOURCES = frozenset({"operator", "import"})


class VersionError(ValueError):
    """Something the operator can fix, phrased for them. `status` is the HTTP answer."""

    def __init__(self, message: str, *, status: int = 422, errors: Optional[list] = None):
        super().__init__(message)
        self.status = status
        self.errors = errors or []


# ── Reading ────────────────────────────────────────────────────────────────────────────────────

_COLS = ("id, project_id, agent_key, kind, version, api_version, spec, content_hash, compiled_prompt, "
         "compiled, report, source, provenance, active, created_by, created_at, published_at")


def _out(row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    for k in ("created_at", "published_at"):
        if out.get(k) is not None and hasattr(out[k], "isoformat"):
            out[k] = out[k].isoformat()
    return out


def active(connection: Connection, project_id: str, key: str, *, kind: str = "agent") -> Optional[dict]:
    return _out(connection.execute(
        text(f"SELECT {_COLS} FROM agent_spec_versions WHERE project_id = :p AND kind = :k "
             "AND agent_key = :a AND active"), {"p": project_id, "k": kind, "a": key}).mappings().first())


def get_version(connection: Connection, project_id: str, key: str, version: int, *,
                kind: str = "agent") -> dict[str, Any]:
    row = _out(connection.execute(
        text(f"SELECT {_COLS} FROM agent_spec_versions WHERE project_id = :p AND kind = :k "
             "AND agent_key = :a AND version = :v"),
        {"p": project_id, "k": kind, "a": key, "v": int(version)}).mappings().first())
    if row is None:
        raise VersionError(f"{key} has no version {version}.", status=404)
    return row


def list_versions(connection: Connection, project_id: str, key: str, *, kind: str = "agent") -> list[dict]:
    rows = connection.execute(
        text("SELECT id, agent_key, kind, version, content_hash, source, active, created_by, created_at, "
             "published_at, report FROM agent_spec_versions WHERE project_id = :p AND kind = :k "
             "AND agent_key = :a ORDER BY version DESC"),
        {"p": project_id, "k": kind, "a": key}).mappings().all()
    return [_out(r) for r in rows]


def _by_hash(connection: Connection, project_id: str, key: str, digest: str, kind: str) -> Optional[dict]:
    return _out(connection.execute(
        text(f"SELECT {_COLS} FROM agent_spec_versions WHERE project_id = :p AND kind = :k "
             "AND agent_key = :a AND content_hash = :h ORDER BY version DESC LIMIT 1"),
        {"p": project_id, "k": kind, "a": key, "h": digest}).mappings().first())


def _role_row(connection: Connection, project_id: str, key: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT id, role_key, display_name, spec, reports_to, agent_id, spec_version FROM roles "
             "WHERE project_id = :p AND role_key = :r"), {"p": project_id, "r": key}).mappings().first()
    return dict(row) if row else None


def _baseline() -> frozenset[str]:
    from ..services.agent_baseline import BASELINE_SKILLS

    return frozenset(BASELINE_SKILLS)


def holdings(connection: Connection, project_id: str, key: str) -> tuple[list[str], list[dict]]:
    """(skills, mcp) one agent holds now: role_skills less the baseline every agent holds, and
    role_mcp_servers as [{server (name), server_id, tools}]."""
    skills = sorted(set(connection.execute(
        text("SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": key}).scalars().all()) - _baseline())
    mcp = [{"server": str(r["name"]), "server_id": str(r["id"]), "tools": sorted(r["tools"] or [])}
           for r in connection.execute(
               text("SELECT s.id, s.name, g.tools FROM role_mcp_servers g JOIN mcp_servers s "
                    "ON s.id = g.mcp_server_id WHERE g.project_id = :p AND g.role_key = :r ORDER BY s.name"),
               {"p": project_id, "r": key}).mappings().all()]
    return skills, mcp


def _expand(connection: Connection, project_id: str, skills: Iterable[str]) -> list[str]:
    """Wildcards (`coding_pipeline.*`) to the project's skill ids, the way Studio reads them."""
    plain = [s for s in skills if "*" not in s]
    wild = [s for s in skills if "*" in s]
    if not wild:
        return sorted(set(plain))
    from ..services.skill_catalogue import entries
    from ..services.skill_scope import matching_skill_ids

    catalog = set(entries(connection, project_id).keys())
    out = set(plain)
    for ref in wild:
        out |= matching_skill_ids(ref, catalog)
    return sorted(out)


def row_document(connection: Connection, project_id: str, key: str,
                 base: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """The document the roles row and its holdings describe now (None: no such role)."""
    row = _role_row(connection, project_id, key)
    if row is None:
        return None
    skills, mcp = holdings(connection, project_id, key)
    if base:
        # A version that names a wildcard keeps naming it while it still describes the holding.
        bskills = list(((base.get("spec") or {}).get("tools") or {}).get("skills") or [])
        if any("*" in s for s in bskills) and set(_expand(connection, project_id, bskills)) - _baseline() == set(skills):
            skills = bskills
    doc = S.doc_from_role(row, skills=skills, mcp=[{"server": m["server"], "tools": m["tools"]} for m in mcp],
                          base=base)
    persona = (doc.get("spec") or {}).get("persona")
    if persona:
        # A persona written before the lint (or by hand) that the lint refuses is described by
        # what a prompt may use of it (agent_persona.trusted_persona): deterministic, so a boot
        # does not mint a version every time it reads the same row.
        from ..services.agent_persona import lint_persona, trusted_persona

        if lint_persona(persona)[0]:
            doc["spec"]["persona"] = trusted_persona(persona)
            try:
                doc = S.dump_spec(S.load_spec(doc))
            except S.SpecError:
                pass
    return doc


# ── The Passport ceiling ───────────────────────────────────────────────────────────────────────

def _native_permission(name: str) -> Optional[str]:
    """The catalogue permission a native loop tool needs (agent_loop.AGENT_TOOL_PERMISSIONS)."""
    try:
        from ..services.agent_loop import AGENT_TOOL_PERMISSIONS
    except Exception:  # noqa: BLE001 — the map moves to the kernel in P1b
        AGENT_TOOL_PERMISSIONS = {}
    return AGENT_TOOL_PERMISSIONS.get(name)


def required_permissions(compiled: S.CompiledAgent) -> tuple[set[str], list[str]]:
    """(catalogue permissions the spec's tools need, native tools nobody knows).

    Skills and MCP tools are governed per call (the skill runtime, skill policy, the run token's
    `skill:invoke`), exactly as today. What a SPEC can add on top is native loop tools, and each
    of those needs its catalogue permission inside the agent's ceiling."""
    need: set[str] = set()
    unknown: list[str] = []
    for tool in compiled.native_tools:
        perm = _native_permission(tool)
        if perm is None:
            unknown.append(tool)
        else:
            need.add(perm)
    return need, unknown


def _ceiling(connection: Connection, row: dict[str, Any]) -> tuple[Optional[Any], frozenset[str]]:
    from ..identity import passports

    passport = None
    if row.get("agent_id"):
        try:
            with connection.begin_nested():
                passport = passports.get(str(row["agent_id"]), db=connection)
        except Exception:  # noqa: BLE001 — no identity tables: the preset is the ceiling
            passport = None
    if passport is not None:
        return passport, frozenset(passport.ceiling)
    from ..identity.permissions_gen import PASSPORT_PRESETS

    spec = row.get("spec") if isinstance(row.get("spec"), dict) else {}
    callsign = str(((spec.get("persona") or {}) if isinstance(spec.get("persona"), dict) else {}).get("callsign") or "")
    preset = (passports.preset_for_role(callsign) or passports.preset_for_role(str(row.get("role_key") or ""))
              or passports.BASELINE_PRESET)
    p = PASSPORT_PRESETS.get(preset)
    return None, frozenset(p.permissions if p else ())


def check_ceiling(connection: Connection, row: dict[str, Any], compiled: S.CompiledAgent, *,
                  previous: Optional[S.CompiledAgent], strict: bool) -> dict[str, Any]:
    """Grants never exceed the Passport ceiling. Returns the report; raises when `strict` and the
    spec would ADD reach the ceiling does not hold (or names a native tool nobody knows)."""
    from ..identity import catalogue

    need, unknown = required_permissions(compiled)
    _, ceiling = _ceiling(connection, row)
    gaps = sorted(need - ceiling)
    had = set()
    if previous is not None:
        prev_need, _ = required_permissions(previous)
        had = prev_need - ceiling
    new_gaps = [g for g in gaps if g not in had]
    not_delegable = sorted(p for p in need if catalogue.is_known(p) and not catalogue.meta(p).delegable)
    report = {"required_permissions": sorted(need), "ceiling_gaps": gaps, "unknown_native_tools": unknown,
              "not_delegable": not_delegable}
    if strict and (new_gaps or unknown or not_delegable):
        problems = []
        if unknown:
            problems.append(f"no native tool called {', '.join(map(repr, unknown))}")
        if not_delegable:
            problems.append(f"{', '.join(not_delegable)} can never be given to an agent")
        if new_gaps:
            problems.append(f"its Passport ceiling does not allow {', '.join(new_gaps)} — a person raises the "
                            "ceiling on the agent's Passport first")
        raise VersionError("This version would give the agent more than its Passport allows: "
                           + "; ".join(problems) + ".", status=403, errors=[report])
    return report


def _sync_passport(connection: Connection, row: dict[str, Any], version: dict[str, Any], actor: str,
                   report: dict[str, Any]) -> None:
    """Keep the Passport in step with the published version: a preset ceiling grows to its
    preset (never past it; a person's `custom` ceiling is untouched) and the publish is written
    to the Passport's lineage."""
    from ..identity import lineage, passports

    passport, _ = _ceiling(connection, row)
    if passport is None:
        return
    if passport.ceiling_preset and passport.ceiling_preset != "custom":
        passports._grow_to_preset(connection, passport)  # noqa: SLF001 — additive, audited
        passport = passports.get(passport.agent_id, db=connection) or passport
    lineage.passport_event(connection, passport, "agent.passport.spec_published", actor_id=actor or "system",
                           details={"version": version["version"], "content_hash": version["content_hash"],
                                    "required_permissions": report.get("required_permissions") or [],
                                    "ceiling_gaps": report.get("ceiling_gaps") or []})


# ── Saving and publishing ──────────────────────────────────────────────────────────────────────

def _load(doc: Any) -> S.AgentSpec:
    if isinstance(doc, str):
        try:
            doc = yaml.safe_load(doc)
        except yaml.YAMLError as exc:
            raise VersionError(f"not YAML: {exc}") from exc
    try:
        return S.load_spec(doc)
    except S.SpecError as exc:
        raise VersionError(str(exc), errors=exc.errors) from exc


def _canonical(spec: S.AgentSpec) -> S.AgentSpec:
    """Baseline skills are held by every agent, not described by one: strip them so a version
    never differs from its own projection read back."""
    base = _baseline()
    if any(s in base for s in spec.tools.skills):
        doc = S.dump_spec(spec)
        doc["spec"]["tools"]["skills"] = [s for s in spec.tools.skills if s not in base]
        spec = S.load_spec(S._strip(doc))  # noqa: SLF001
    return spec


def save(connection: Connection, project_id: str, doc: Any, *, actor: str, source: str = "operator",
         provenance: Optional[dict[str, Any]] = None, key: Optional[str] = None) -> dict[str, Any]:
    """Store a document as the agent's next version (NOT published). A document identical to an
    existing version returns that version (`duplicate: true`) instead of minting a copy."""
    if source not in SOURCES:
        raise VersionError(f"unknown source {source!r}.")
    spec = _canonical(_load(doc))
    if key is not None and spec.key != key:
        raise VersionError(f"this document is for {spec.key!r}, not {key!r}; metadata.key cannot change.")
    kind = "profile" if spec.extends else "agent"
    compiled = S.compile_spec(spec)
    same = _by_hash(connection, project_id, spec.key, compiled.content_hash, kind)
    if same is not None:
        return {**same, "duplicate": True}
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                       {"k": f"agent_spec:{project_id}:{kind}:{spec.key}"})
    number = int(connection.execute(
        text("SELECT COALESCE(MAX(version), 0) + 1 FROM agent_spec_versions WHERE project_id = :p "
             "AND kind = :k AND agent_key = :a"), {"p": project_id, "k": kind, "a": spec.key}).scalar())
    vid = make_id("asv")
    connection.execute(
        text("INSERT INTO agent_spec_versions (id, project_id, agent_key, kind, version, api_version, spec, "
             "content_hash, compiled_prompt, compiled, source, provenance, created_by) VALUES (:id, :p, :a, :k, "
             ":v, :api, CAST(:spec AS JSONB), :h, :prompt, CAST(:compiled AS JSONB), :src, CAST(:prov AS JSONB), "
             ":by)"),
        {"id": vid, "p": project_id, "a": spec.key, "k": kind, "v": number, "api": S.API_VERSION,
         "spec": json.dumps(S.dump_spec(spec)), "h": compiled.content_hash, "prompt": compiled.prompt,
         "compiled": json.dumps(S.compiled_as_dict(compiled)), "src": source,
         "prov": json.dumps(provenance or {}), "by": actor or "system"})
    emit_event(connection, project_id, "agent.spec.version_saved", actor or "system", "agent", spec.key,
               {"kind": kind, "version": number, "content_hash": compiled.content_hash, "source": source})
    return {**get_version(connection, project_id, spec.key, number, kind=kind), "duplicate": False}


def _activate(connection: Connection, project_id: str, version: dict[str, Any]) -> None:
    connection.execute(
        text("UPDATE agent_spec_versions SET active = false WHERE project_id = :p AND kind = :k "
             "AND agent_key = :a AND active AND id <> :id"),
        {"p": project_id, "k": version["kind"], "a": version["agent_key"], "id": version["id"]})
    connection.execute(
        text("UPDATE agent_spec_versions SET active = true, published_at = COALESCE(published_at, now()) "
             "WHERE id = :id"), {"id": version["id"]})


def _merged_role_spec(current: Any, projection: dict[str, Any], version: int) -> dict[str, Any]:
    out = {k: v for k, v in (current if isinstance(current, dict) else {}).items() if k not in S.ROLE_SPEC_KEYS}
    perms = dict(out.get("permissions") or {}) if isinstance(out.get("permissions"), dict) else {}
    perms = {k: v for k, v in perms.items() if k not in S.ROLE_PERMISSION_KEYS}
    perms.update(projection.get("permissions") or {})
    out.update({k: v for k, v in projection.items() if k != "permissions"})
    out["permissions"] = perms
    out["agent_spec"] = {**dict(projection.get("agent_spec") or {}), "version": version}
    return out


def _project(connection: Connection, project_id: str, version: dict[str, Any], *, actor: str,
             previous: Optional[dict[str, Any]], creating: bool) -> dict[str, Any]:
    """Write the version onto roles / role_skills / role_mcp_servers. Returns what changed."""
    from ..services import agents as agent_svc
    from ..services.skill_definitions import SkillDefinitionError, assign_skill_to_role, unassign_skill_from_role

    spec = S.load_spec(version["spec"])
    compiled = S.compile_spec(spec)
    key = spec.key
    row = _role_row(connection, project_id, key)
    parent = agent_svc._check_reports_to(connection, project_id, key, spec.reports_to)  # noqa: SLF001
    new_spec = _merged_role_spec(row["spec"] if row else {}, dict(compiled.role_spec), int(version["version"]))
    # A PERSON'S DECISION (publish, roll back, import): the seed must never revert it.
    from ..services import seed_exclusions

    seed_exclusions.exclude(connection, project_id, "role_spec", key, actor=actor,
                            reason=f"agent spec v{int(version['version'])} published")
    connection.execute(
        text("UPDATE roles SET display_name = :n, reports_to = :rt, spec = CAST(:s AS JSONB), spec_version = :v "
             "WHERE project_id = :p AND role_key = :r"),
        {"n": spec.display_name, "rt": parent, "s": json.dumps(new_spec), "v": int(version["version"]),
         "p": project_id, "r": key})

    # THE GRANT, AS A DIFF against what the previous version granted (or, for a first version,
    # against nothing): out-of-band grants and the baseline are never taken away here.
    held, held_mcp = holdings(connection, project_id, key)
    want = set(_expand(connection, project_id, compiled.skills)) - _baseline()
    agent_svc._validate_skills(connection, project_id, sorted(want), spec.tools.forbidden)  # noqa: SLF001
    before: set[str] = set()
    before_mcp: set[str] = set()
    if previous is not None:
        pspec = S.load_spec(previous["spec"])
        before = set(_expand(connection, project_id, pspec.tools.skills)) - _baseline()
        before_mcp = {g.server.lower() for g in pspec.tools.mcp}
    added, removed = sorted(want - set(held)), sorted((before - want) & set(held))
    if creating:
        from ..services.agent_baseline import BASELINE_SKILLS

        added += [s for s in BASELINE_SKILLS if s not in added
                  and agent_svc._baseline_available(connection, project_id, s)]  # noqa: SLF001
    for skill in added:
        try:
            assign_skill_to_role(connection, project_id=project_id, actor=actor, role_key=key, skill_id=skill)
        except SkillDefinitionError as exc:
            raise VersionError(str(exc)) from exc
    for skill in removed:
        unassign_skill_from_role(connection, project_id=project_id, actor=actor, role_key=key, skill_id=skill)
    have = {m["server"].lower(): m for m in held_mcp}
    for grant in compiled.mcp:
        current = have.get(grant.server.lower())
        if current is None or sorted(current["tools"]) != sorted(grant.tools):
            agent_svc.grant_mcp(connection, project_id, actor=actor, role_key=key,
                                mcp_server_id=current["server_id"] if current else grant.server,
                                tools=list(grant.tools))
    wanted_mcp = {g.server.lower() for g in compiled.mcp}
    for name in sorted((before_mcp - wanted_mcp) & set(have)):
        agent_svc.revoke_mcp(connection, project_id, actor=actor, role_key=key,
                             mcp_server_id=have[name]["server_id"])
    return {"skills_added": added, "skills_removed": removed,
            "mcp": sorted(g.server for g in compiled.mcp)}


def publish(connection: Connection, project_id: str, key: str, version: int, *, actor: str,
            kind: str = "agent", reason: str = "publish") -> dict[str, Any]:
    """Make `version` the active one and project it — grants, identity seal and Passport in the
    caller's transaction (all or nothing)."""
    from ..services import agent_identity

    target = get_version(connection, project_id, key, version, kind=kind)
    prev = active(connection, project_id, key, kind=kind)
    if prev is not None and prev["id"] == target["id"] and reason != "repair":
        return {**target, "changed": {}, "already_active": True}
    compiled = S.compile_spec(S.load_spec(target["spec"]))
    previous = S.compile_spec(S.load_spec(prev["spec"])) if prev else None
    if kind == "profile":
        base = _role_row(connection, project_id, compiled.extends or "")
        if base is None:
            raise VersionError(f"profile {key!r} extends {compiled.extends!r}, which is not an agent here.")
        report = check_ceiling(connection, base, compiled, previous=previous,
                               strict=target["source"] in _STRICT_SOURCES)
        _activate(connection, project_id, target)
        changed: dict[str, Any] = {}
    else:
        row = _role_row(connection, project_id, key)
        if row is None:
            raise VersionError(f"no agent {key!r} on this project; create it from the spec first.", status=404)
        report = check_ceiling(connection, row, compiled, previous=previous,
                               strict=target["source"] in _STRICT_SOURCES)
        _activate(connection, project_id, target)
        with agent_identity.sealing_once():
            changed = _project(connection, project_id, target, actor=actor, previous=prev,
                               creating=row.get("spec_version") is None and reason == "create")
            agent_identity.seal_identity(connection, project_id, key, "spec", actor)
        row = _role_row(connection, project_id, key) or row
        _sync_passport(connection, row, target, actor, report)
    connection.execute(text("UPDATE agent_spec_versions SET report = CAST(:r AS JSONB) WHERE id = :id"),
                       {"r": json.dumps({**report, "changed": changed, "reason": reason}), "id": target["id"]})
    emit_event(connection, project_id, f"agent.spec.{'rolled_back' if reason == 'rollback' else 'published'}",
               actor or "system", "agent", key,
               {"kind": kind, "version": target["version"], "content_hash": target["content_hash"],
                "from_version": prev["version"] if prev else None, "changed": changed})
    return {**get_version(connection, project_id, key, version, kind=kind), "changed": changed}


def rollback(connection: Connection, project_id: str, key: str, to_version: int, *, actor: str,
             kind: str = "agent") -> dict[str, Any]:
    """Re-activate an older version: the roles row, the grants and the compiled prompt become
    exactly what that version compiled to."""
    return publish(connection, project_id, key, to_version, actor=actor, kind=kind, reason="rollback")


def create_from_spec(connection: Connection, project_id: str, doc: Any, *, actor: str,
           source: str = "operator") -> dict[str, Any]:
    """A NEW agent from a document: the roles row, its identity (born, sponsored, sealed) and
    version 1, published, in one transaction. An existing key is refused (creating is never
    editing)."""
    from ..services import agent_identity, seed_exclusions

    spec = _canonical(_load(doc))
    if spec.extends:
        version = save(connection, project_id, doc, actor=actor, source=source)
        return publish(connection, project_id, spec.key, version["version"], actor=actor, kind="profile")
    if _role_row(connection, project_id, spec.key) is not None:
        raise VersionError(f"an agent {spec.key!r} already exists on this project. Save a new version of it "
                           "instead.", status=409)
    version = save(connection, project_id, S.dump_spec(spec), actor=actor, source=source)
    connection.execute(
        text("INSERT INTO roles (id, project_id, role_key, display_name, autonomy_level, spec) "
             "VALUES (:id, :p, :k, :n, 0, CAST(:s AS JSONB))"),
        {"id": make_id("role"), "p": project_id, "k": spec.key, "n": spec.display_name,
         "s": json.dumps({"source": "operator", "permissions": {"allowed_skills": []}})})
    seed_exclusions.lift(connection, project_id, "role", spec.key)
    seed_exclusions.exclude(connection, project_id, "role_spec", spec.key, actor=actor,
                            reason="created from an agent spec")
    with agent_identity.sealing_once():
        agent_identity.birth(connection, project_id, spec.key, born_by=actor,
                             born_from={"kind": "spec", "content_hash": version["content_hash"]},
                             sponsor=actor if agent_identity.is_person(actor) else None, actor=actor)
        out = publish(connection, project_id, spec.key, version["version"], actor=actor, reason="create")
    emit_event(connection, project_id, "agent.created", actor, "role", spec.key,
               {"from_spec": True, "version": version["version"], "content_hash": version["content_hash"]})
    return out


def save_and_publish(connection: Connection, project_id: str, doc: Any, *, actor: str, key: str,
                     source: str = "operator", publish_now: bool = True) -> dict[str, Any]:
    """An edit: version N+1 from the document, published unless `publish_now` is false."""
    version = save(connection, project_id, doc, actor=actor, source=source, key=key)
    if not publish_now:
        return version
    kind = version["kind"]
    out = publish(connection, project_id, key, version["version"], actor=actor, kind=kind)
    return {**out, "duplicate": version.get("duplicate", False)}


def snapshot(connection: Connection, project_id: str, key: str, *, actor: str, source: str = "legacy_edit",
             provenance: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Record the row as it is now as the agent's active version, WITHOUT projecting it (it is
    already the row). None when the row matches its active version (nothing to do)."""
    current = active(connection, project_id, key)
    doc = row_document(connection, project_id, key, base=current["spec"] if current else None)
    if doc is None:
        return None
    try:
        spec = _canonical(S.load_spec(doc))
    except S.SpecError as exc:
        raise VersionError(f"{key}: the row does not describe a valid agent: {exc}", errors=exc.errors) from exc
    digest = S.content_hash(spec)
    if current is not None and current["content_hash"] == digest:
        return None
    version = save(connection, project_id, S.dump_spec(spec), actor=actor, source=source, provenance=provenance)
    _activate(connection, project_id, version)
    connection.execute(text("UPDATE roles SET spec_version = :v WHERE project_id = :p AND role_key = :r"),
                       {"v": int(version["version"]), "p": project_id, "r": key})
    return active(connection, project_id, key)


# ── Diff, export, import ───────────────────────────────────────────────────────────────────────

def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out or {prefix: {}}
    return {prefix: value}


def diff(connection: Connection, project_id: str, key: str, a: int, b: int, *, kind: str = "agent") -> dict:
    """What changed from version `a` to `b`: each field path, and the compiled prompt as a
    unified diff."""
    va, vb = get_version(connection, project_id, key, a, kind=kind), get_version(connection, project_id, key, b, kind=kind)
    fa, fb = _flatten(va["spec"]), _flatten(vb["spec"])
    changes = [{"path": p, "from": fa.get(p), "to": fb.get(p),
                "change": "added" if p not in fa else "removed" if p not in fb else "changed"}
               for p in sorted(set(fa) | set(fb)) if fa.get(p) != fb.get(p)]
    prompt = "".join(difflib.unified_diff(va["compiled_prompt"].splitlines(keepends=True),
                                          vb["compiled_prompt"].splitlines(keepends=True),
                                          fromfile=f"{key}@{a}", tofile=f"{key}@{b}"))
    return {"agent_key": key, "from": a, "to": b, "from_hash": va["content_hash"], "to_hash": vb["content_hash"],
            "changes": changes, "prompt_diff": prompt}


def export_yaml(connection: Connection, project_id: str, key: str, version: Optional[int] = None, *,
                kind: str = "agent") -> str:
    row = get_version(connection, project_id, key, version, kind=kind) if version else active(
        connection, project_id, key, kind=kind)
    if row is None:
        raise VersionError(f"{key} has no published version yet.", status=404)
    doc = dict(row["spec"])
    doc["metadata"] = {**doc.get("metadata", {}), "version": row["version"]}
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def import_yaml(connection: Connection, project_id: str, document: str, *, actor: str,
                publish_now: bool = True) -> dict[str, Any]:
    """A YAML document in: a new agent when its key is new, else the next version of it."""
    spec = _load(document)
    exists = (_role_row(connection, project_id, spec.key) is not None) if not spec.extends else (
        active(connection, project_id, spec.key, kind="profile") is not None)
    doc = S.dump_spec(spec)
    if not exists and publish_now:
        return {**create_from_spec(connection, project_id, doc, actor=actor, source="import"), "created": True}
    return {**save_and_publish(connection, project_id, doc, actor=actor, key=spec.key, source="import",
                               publish_now=publish_now), "created": False}


__all__ = ["VersionError", "active", "check_ceiling", "create_from_spec", "diff", "export_yaml", "get_version", "holdings",
           "import_yaml", "list_versions", "publish", "required_permissions", "rollback", "row_document", "save",
           "save_and_publish", "snapshot"]
