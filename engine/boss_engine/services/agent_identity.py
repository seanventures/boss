"""Who an agent IS: an id that never changes, a birth, a sponsor, a lifecycle, a version.

WHY THIS EXISTS
---------------
An agent was a `roles` row keyed by `role_key`, a string anyone could reuse after a delete and
that said nothing about where the agent came from, who answers for it, or whether the agent
that did yesterday's work is the agent that exists today. So the run view could say "Ran as
qa" and nobody could say WHICH qa: its mission, voice and skills might all have changed since.

The identity facts are columns on `roles` (db/schema.sql, agent-world spec §4.1):

    agent_id          'agt_' + 10 base32 characters. Assigned once, never changed, never reused.
    born_by           the person who made it, 'agent:<agent_id>', or 'spec-pack'
    born_from         {kind: pack|template|archetype|blank|clone, …} — the blueprint
    sponsor           the accountable human (pause/retire are theirs, or an owner's)
    lifecycle         active | paused | retired — paused agents are never auto-picked
    identity_version  goes up by one whenever the definition really changes
    fingerprint       sha256 of the RFC 8785 canonical {agent, spec, grants, model_policy}

VERSION HISTORY IS EVENTS, NOT A TABLE. `seal_identity` recomputes the fingerprint and, only
when it differs, bumps the version and emits `agent.sealed` {version, fingerprint,
prev_fingerprint, change_kind, canonical}. An unchanged save seals nothing. Several changes in
one request — an agent created with eight skills, a PATCH that edits the voice and grants a
skill — seal ONCE: `sealing_once()` collects them and seals at the end, so a freshly installed
agent is v1, not v9.

Nothing here decides what an agent may DO. That is role_skills and role_mcp_servers; this
module only records them (as `grants` in the fingerprint) so a change to them is a new version.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import math
import re
import secrets
from typing import Any, Iterator, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .agent_world_types import ALIASES, CANONICAL_ROLE_KEYS, Passport
from .core import emit_event

#: Crockford base32, lower case: no i, l, o or u, so an id read aloud is an id typed right.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
AGENT_ID = re.compile(r"^agt_[0-9a-hjkmnp-tv-z]{10}$")

LIFECYCLES = ("active", "paused", "retired")
#: When several kinds of change land in one sealing batch, the version is labelled with the
#: most significant one.
_CHANGE_ORDER = ("born", "persona", "remit", "instructions", "grant", "revoke", "model",
                 "lifecycle")
#: Actors that are BOSS rather than a person: never a sponsor.
_SYSTEM_ACTORS = {"spec-pack", "system", "bootstrap", "boss", "onboarding", "seed", ""}
#: Columns a client may never set: they are facts about the agent, not settings.
IDENTITY_FIELDS = ("agent_id", "born_by", "born_from", "sponsor", "lifecycle",
                   "identity_version", "fingerprint")
#: Keys in a role spec that are computed rather than authored, so they never move a version.
#: `team` is where the agent sits on the org map, not who it is: moving it changes no version.
_VOLATILE_SPEC_KEYS = ("skill_reference_diagnostics", "team")

IDENTITY_FIXED = "Identity fields are fixed"

_log = logging.getLogger(__name__)


class IdentityError(ValueError):
    """Something the operator can act on. `status` is the HTTP answer."""

    def __init__(self, message: str, *, status: int = 422):
        super().__init__(message)
        self.status = status


# ── Ids and canonical form ───────────────────────────────────────────────────────────────────


def new_agent_id() -> str:
    """'agt_' + 10 random base32 characters (50 bits): never derived from the name."""
    return "agt_" + "".join(secrets.choice(_ALPHABET) for _ in range(10))


def _jcs(value: Any) -> str:
    """RFC 8785 JSON Canonicalization: sorted keys, no whitespace, UTF-8, ES6 numbers."""
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JCS cannot encode NaN or Infinity")
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs(v) for v in value) + "]"
    if isinstance(value, dict):
        # Sorted by UTF-16 code units, as the RFC says (differs from str order only above BMP).
        items = sorted(((str(k), v) for k, v in value.items()),
                       key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(f"{_jcs(k)}:{_jcs(v)}" for k, v in items) + "}"
    return _jcs(str(value))


def canonical(role_row: dict, grants: list, model_policy: dict,
              instructions: Optional[list] = None) -> dict:
    """The document an agent's fingerprint is the sha256 of. Plain JSON, deterministic.

    `instructions` ([{name, sha256, modes}], agent_instructions) is added ONLY when non-empty:
    an agent with none keeps the exact fingerprint (and version) it had before they existed.
    """
    spec = role_row.get("spec") if isinstance(role_row.get("spec"), dict) else {}
    spec = {k: v for k, v in spec.items() if k not in _VOLATILE_SPEC_KEYS}
    document = {
        "agent": {
            "role_key": role_row.get("role_key"),
            "display_name": role_row.get("display_name"),
            "reports_to": role_row.get("reports_to"),
            "autonomy_level": int(role_row.get("autonomy_level") or 0),
            "lifecycle": role_row.get("lifecycle") or "active",
        },
        "spec": json.loads(json.dumps(spec, default=str)),
        "grants": sorted(str(g) for g in grants or []),
        "model_policy": json.loads(json.dumps(model_policy or {}, default=str)),
    }
    if instructions:
        document["instructions"] = [
            {"name": str(i.get("name") or ""), "sha256": str(i.get("sha256") or ""),
             "modes": sorted(str(m) for m in i.get("modes") or [])}
            for i in instructions]
    return document


def fingerprint_of(document: dict) -> str:
    return hashlib.sha256(_jcs(document).encode("utf-8")).hexdigest()


def short_fingerprint(fingerprint: Optional[str]) -> str:
    """`9f3ac21e…` → `9f3a·c21e`, the way the Passport shows a version."""
    value = str(fingerprint or "")
    return f"{value[:4]}·{value[4:8]}" if len(value) >= 8 else value


# ── Reading ──────────────────────────────────────────────────────────────────────────────────

_ROLE_COLUMNS = ("id, role_key, display_name, spec, reports_to, autonomy_level, trust_score, "
                 "successful_runs, policy_violation_count, hidden_failure_count, "
                 "low_quality_output_count, scope_violation_count, source_module_id, "
                 "source_module_version, created_at, agent_id, born_by, born_from, sponsor, "
                 "lifecycle, identity_version, fingerprint")


def _row(connection: Connection, project_id: str, role_key: str, *,
         lock: bool = False) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text(f"SELECT {_ROLE_COLUMNS} FROM roles WHERE project_id = :p AND role_key = :r"
             + (" FOR UPDATE" if lock else "")),
        # As stored: keys are lower case by convention, but a hand-registered one may not be.
        {"p": project_id, "r": str(role_key or "").strip()},
    ).mappings().first()
    return dict(row) if row else None


def _require(connection: Connection, project_id: str, role_key: str, *,
             lock: bool = False) -> dict[str, Any]:
    row = _row(connection, project_id, role_key, lock=lock)
    if row is None:
        raise IdentityError(f"no agent {role_key!r} on this project.", status=404)
    return row


def _grants(connection: Connection, project_id: str, role_key: str) -> list[str]:
    skills = connection.execute(
        text("SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role_key},
    ).scalars().all()
    mcp = connection.execute(
        text("SELECT mcp_server_id, tools FROM role_mcp_servers "
             "WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role_key},
    ).mappings().all()
    return ([f"skill:{s}" for s in skills]
            + [f"mcp:{m['mcp_server_id']}:{','.join(sorted(m['tools'] or []))}" for m in mcp])


def _model_policy(connection: Connection, project_id: str, role_key: str,
                  spec: dict[str, Any]) -> dict[str, Any]:
    """The spec's own model policy plus the active Studio profile's, when there is one."""
    policy: dict[str, Any] = dict(spec.get("model_policy") or {}) if isinstance(
        spec.get("model_policy"), dict) else {}
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("SELECT d.agent_key, v.model_policy FROM agent_definitions d "
                     "JOIN agent_versions v ON v.id = d.active_version_id "
                     "WHERE d.project_id = :p AND v.role_key = :r ORDER BY d.agent_key"),
                {"p": project_id, "r": role_key},
            ).mappings().all()
    except Exception:  # noqa: BLE001 — no Studio tables, no Studio policy
        rows = []
    for r in rows:
        if r["model_policy"]:
            policy[f"studio:{r['agent_key']}"] = r["model_policy"]
    return policy


def _canonical_for(connection: Connection, project_id: str, row: dict[str, Any]) -> dict:
    spec = row["spec"] if isinstance(row.get("spec"), dict) else {}
    from .agent_instructions import fingerprint_entries

    return canonical(row, _grants(connection, project_id, row["role_key"]),
                     _model_policy(connection, project_id, row["role_key"], spec),
                     fingerprint_entries(connection, project_id, row["role_key"]))


# ── Sealing ──────────────────────────────────────────────────────────────────────────────────

#: The open sealing batch, if any: {(id(connection), project_id, role_key): {...}}.
_BATCH: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "agent_identity_batch", default=None)


@contextlib.contextmanager
def sealing_once() -> Iterator[None]:
    """Seal every agent touched inside the block once, at the end, with its strongest change.

    Nested blocks join the outer one. Nothing is sealed when the block raises: the transaction
    is going to roll back anyway, and a version for a change that never happened is a lie.
    """
    if _BATCH.get() is not None:
        yield
        return
    token = _BATCH.set({})
    try:
        yield
        pending = _BATCH.get() or {}
    finally:
        _BATCH.reset(token)
    for entry in pending.values():
        kinds = entry["kinds"]
        kind = next((k for k in _CHANGE_ORDER if k in kinds), next(iter(kinds)))
        _seal(entry["connection"], entry["project_id"], entry["role_key"], kind,
              entry["actor"], emit=entry["emit"])


def seal_identity(connection: Connection, project_id: str, role_key: str, change_kind: str,
                  actor: str) -> tuple[int, str]:
    """(version, fingerprint). Bumps the version and emits `agent.sealed` only on a real change.

    Inside `sealing_once()` the seal is deferred to the end of the block and this returns the
    stored values. An agent that does not exist (deleted in the same transaction) seals nothing.
    """
    key = str(role_key or "").strip()
    batch = _BATCH.get()
    if batch is not None:
        slot = batch.setdefault((id(connection), project_id, key), {
            "connection": connection, "project_id": project_id, "role_key": key,
            "kinds": [], "actor": actor, "emit": True})
        if change_kind not in slot["kinds"]:
            slot["kinds"].append(change_kind)
        row = _row(connection, project_id, key)
        if row is None:
            return (0, "")
        return int(row["identity_version"] or 1), str(row["fingerprint"] or "")
    return _seal(connection, project_id, key, change_kind, actor, emit=True)


def _seal(connection: Connection, project_id: str, role_key: str, change_kind: str,
          actor: str, *, emit: bool) -> tuple[int, str]:
    row = _row(connection, project_id, role_key, lock=True)
    if row is None:
        return (0, "")
    if not row.get("agent_id"):
        _assign_identity(connection, project_id, row, born_by=_born_by_guess(
            connection, project_id, row), born_from=_born_from_guess(row), sponsor=None)
        row = _row(connection, project_id, role_key, lock=True) or row
    document = _canonical_for(connection, project_id, row)
    fingerprint = fingerprint_of(document)
    previous = row.get("fingerprint")
    version = int(row.get("identity_version") or 1)
    if previous == fingerprint:
        return version, fingerprint
    if previous is not None:
        version += 1
    connection.execute(
        text("UPDATE roles SET fingerprint = :f, identity_version = :v "
             "WHERE project_id = :p AND role_key = :r"),
        {"f": fingerprint, "v": version, "p": project_id, "r": role_key},
    )
    if emit:
        emit_event(connection, project_id, "agent.sealed", actor or "system", "agent",
                   str(row["agent_id"] or role_key),
                   {"role_key": role_key, "version": version, "fingerprint": fingerprint,
                    "prev_fingerprint": previous,
                    "change_kind": change_kind if previous is not None else "born",
                    "canonical": document})
        if previous is not None and row.get("agent_id"):
            # The agent's Passport changed: it goes in the security log's lineage stream too
            # (`agent.passport.sealed`, IDENTITY.md §8.2), never failing the change it rides on.
            quietly(connection, _passport_sealed, str(row["agent_id"]), actor, version,
                    fingerprint, previous, change_kind)
    return version, fingerprint


def _passport_sealed(connection: Connection, agent_id: str, actor: Optional[str], version: int,
                     fingerprint: str, previous: Optional[str], change_kind: str) -> None:
    from ..identity import passports
    from ..identity.lineage import passport_event

    passport = passports.get(agent_id, db=connection)
    if passport is None:
        return
    passport_event(connection, passport, "agent.passport.sealed", actor_id=actor or "system",
                   details={"version": version, "fingerprint": fingerprint,
                            "prev_fingerprint": previous, "change_kind": change_kind})


def quietly(connection: Connection, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Identity bookkeeping riding on another write (a grant, a pack install) that must never
    fail that write. Inside a savepoint, so a failure cannot poison the transaction; logged,
    and repaired by the next `backfill`/seal. None when it failed."""
    try:
        with connection.begin_nested():
            return fn(connection, *args, **kwargs)
    except Exception:  # noqa: BLE001
        _log.warning("agent identity bookkeeping failed in %s", getattr(fn, "__name__", fn),
                     exc_info=True)
        return None


# ── Birth ────────────────────────────────────────────────────────────────────────────────────


#: The operator console's identity: the signed-in operator, a person by definition.
CONSOLE_OPERATOR = "executive_console"


def is_person(actor: Optional[str], connection: Optional[Connection] = None,
              project_id: Optional[str] = None) -> bool:
    """Is `actor` a person? Without a connection, only the name can be judged (not a system
    actor, not `agent:…`). WITH one, the project decides: the console operator is a person; a
    member is a person only when it is a human member AND no agent holds that role. `coo`,
    `metatron`, `cto` or any role agent is never a person, whatever its member row says — an
    agent's key acting under its role name must not count as somebody approving."""
    value = str(actor or "").strip()
    if not value or value in _SYSTEM_ACTORS or value.startswith("agent:"):
        return False
    if connection is None or not project_id:
        return True
    if value == CONSOLE_OPERATOR:
        return True
    if value.startswith("usr_"):
        # An identity principal (under enforce the actor IS the verified principal): a person is
        # an active user principal — the legacy members table names none of them (R2 #1).
        kind = connection.execute(
            text("SELECT kind FROM identity_principals WHERE id = :p AND status = 'active'"),
            {"p": value}).scalar()
        return kind == "user"
    member = connection.execute(
        text("SELECT member_type FROM project_members WHERE project_id = :p AND member_id = :m "
             "AND COALESCE(status, 'active') = 'active'"),
        {"p": project_id, "m": value}).mappings().first()
    if not member or str(member["member_type"] or "") != "human":
        return False
    role_agent = connection.execute(
        text("SELECT 1 FROM roles WHERE project_id = :p AND role_key = :r LIMIT 1"),
        {"p": project_id, "r": value}).first()
    return role_agent is None


def _assign_identity(connection: Connection, project_id: str, row: dict[str, Any], *,
                     born_by: str, born_from: dict[str, Any], sponsor: Optional[str]) -> str:
    agent_id = new_agent_id()
    sponsor = sponsor if sponsor is not None else (born_by if is_person(born_by) else None)
    for _ in range(5):  # a 50-bit collision is astronomically rare; never fail a create on one
        try:
            with connection.begin_nested():
                connection.execute(
                    text("UPDATE roles SET agent_id = :a, born_by = :by, "
                         "born_from = CAST(:from AS JSONB), sponsor = :sp "
                         "WHERE project_id = :p AND role_key = :r AND agent_id IS NULL"),
                    {"a": agent_id, "by": born_by, "from": json.dumps(born_from),
                     "sp": sponsor, "p": project_id, "r": row["role_key"]},
                )
            return agent_id
        except Exception:  # noqa: BLE001 — the unique index refused a duplicate id
            agent_id = new_agent_id()
    raise IdentityError("could not assign an agent id; try again.", status=503)


def birth(connection: Connection, project_id: str, role_key: str, *, born_by: str,
          born_from: Optional[dict[str, Any]] = None, sponsor: Optional[str] = None,
          actor: Optional[str] = None) -> str:
    """Give an agent its identity: id, birth, sponsor, `agent.born`, and the v1 seal.

    Idempotent: an agent that already has an id keeps it, and nothing is emitted.
    """
    row = _require(connection, project_id, role_key, lock=True)
    if row.get("agent_id"):
        return str(row["agent_id"])
    origin = dict(born_from or {"kind": "blank"})
    agent_id = _assign_identity(connection, project_id, row, born_by=born_by,
                                born_from=origin, sponsor=sponsor)
    emit_event(connection, project_id, "agent.born", actor or born_by, "agent", agent_id,
               {"role_key": row["role_key"], "born_by": born_by, "born_from": origin,
                "sponsor": sponsor if sponsor is not None else (
                    born_by if is_person(born_by) else None)})
    seal_identity(connection, project_id, row["role_key"], "born", actor or born_by)
    return agent_id


def _born_by_guess(connection: Connection, project_id: str, row: dict[str, Any]) -> str:
    """Who made a pre-identity agent: the earliest create/install event's actor, else the pack."""
    found = connection.execute(
        text("SELECT actor FROM events WHERE project_id = :p "
             "AND event_type IN ('agent.created', 'role.installed', 'role.registered') "
             "AND (entity_id = :k OR entity_id = :id OR data->>'role_key' = :k) "
             "ORDER BY created_at ASC LIMIT 1"),
        {"p": project_id, "k": row["role_key"], "id": row.get("id") or ""},
    ).scalar()
    actor = str(found or "").strip()
    return actor if is_person(actor) else "spec-pack"


def _born_from_guess(row: dict[str, Any]) -> dict[str, Any]:
    spec = row.get("spec") if isinstance(row.get("spec"), dict) else {}
    if row.get("source_module_id"):
        return {"kind": "pack", "module_id": row["source_module_id"],
                "module_version": row.get("source_module_version")}
    if spec.get("source") == "operator":
        return {"kind": "blank"}
    return {"kind": "pack"}


def backfill(connection: Connection, project_id: Optional[str] = None) -> int:
    """Give every pre-identity agent an id, a birth and a fingerprint. Idempotent; quiet.

    Quiet means no events: these agents were not born today, and a flood of `agent.born` rows
    dated at the upgrade would be a false history. Returns how many rows it changed.
    """
    params: dict[str, Any] = {}
    where = "(agent_id IS NULL OR fingerprint IS NULL)"
    if project_id:
        where += " AND project_id = :p"
        params["p"] = project_id
    rows = connection.execute(
        text(f"SELECT project_id, role_key FROM roles WHERE {where} ORDER BY project_id, role_key"),
        params,
    ).mappings().all()
    batch = _BATCH.get() or {}
    pending = {(project, key) for (_, project, key) in batch}
    changed = 0
    for r in rows:
        if (r["project_id"], r["role_key"]) in pending:
            continue  # born in this very batch: its seal (and `agent.sealed` v1) is coming
        row = _row(connection, r["project_id"], r["role_key"], lock=True)
        if row is None:
            continue
        changed += 1
        if not row.get("agent_id"):
            _assign_identity(connection, r["project_id"], row,
                             born_by=_born_by_guess(connection, r["project_id"], row),
                             born_from=_born_from_guess(row), sponsor=None)
        _seal(connection, r["project_id"], r["role_key"], "born", "system", emit=False)
    return changed


# ── Names ────────────────────────────────────────────────────────────────────────────────────


def resolve(key_or_title: str) -> Optional[str]:
    """One namespace: a role key, seat title, captain id or stage role → the role_key."""
    raw = str(key_or_title or "").strip().lower()
    if not raw:
        return None
    for candidate in (raw, re.sub(r"[^a-z0-9]+", "_", raw).strip("_")):
        if candidate in CANONICAL_ROLE_KEYS:
            return candidate
        if candidate in ALIASES:
            return ALIASES[candidate]
    return None


def resolve_in(connection: Connection, project_id: str, key_or_title: str) -> Optional[str]:
    """`resolve`, plus this project's own agents by key, display name or callsign."""
    raw = str(key_or_title or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    row = connection.execute(
        text("SELECT role_key FROM roles WHERE project_id = :p AND (role_key = :k "
             "OR lower(display_name) = :k OR lower(spec->'persona'->>'callsign') = :k) "
             "ORDER BY (role_key = :k) DESC, role_key LIMIT 1"),
        {"p": project_id, "k": lowered},
    ).scalar()
    return str(row) if row else resolve(raw)


def _persona(spec: Any) -> dict[str, Any]:
    persona = (spec or {}).get("persona") if isinstance(spec, dict) else None
    return persona if isinstance(persona, dict) else {}


def avatar_of(role_key: str, agent_id: Optional[str], persona: dict[str, Any]) -> dict[str, Any]:
    """The avatar spec a face is drawn from. Shipped roles seed by role_key (the kit's cast
    picks hand-designed parts for those); a custom agent seeds by its agent_id."""
    avatar = dict(persona.get("avatar") or {}) if isinstance(persona.get("avatar"), dict) else {}
    avatar.setdefault("kit", "boss-v1")
    avatar.setdefault("seed", agent_id or role_key)
    return avatar


def identities(connection: Connection, project_id: str) -> list[dict]:
    """The light list the web resolver loads once: one face and name per agent.

    BACKFILLS FIRST, as `passport` does. An agent with no agent_id here is seeded by its
    role_key, and the first Passport opened would give it an agt_ id and so a second face —
    the same agent looking different before and after somebody clicked it.
    """
    def read() -> list[Any]:
        return connection.execute(
            text("SELECT role_key, agent_id, fingerprint, display_name, spec, lifecycle "
                 "FROM roles WHERE project_id = :p AND role_key <> 'human' ORDER BY role_key"),
            {"p": project_id},
        ).mappings().all()

    rows = read()
    if any(not r["agent_id"] or not r["fingerprint"] for r in rows):
        if quietly(connection, backfill, project_id):
            rows = read()
    from .agent_persona import persona_of

    out = []
    for r in rows:
        persona = _persona(r["spec"])
        # Named by the one persona the prompt and every conversation read (persona_of).
        named = persona_of({"role_key": r["role_key"], "spec": r["spec"]})
        out.append({
            "role_key": r["role_key"], "agent_id": r["agent_id"],
            "display_name": r["display_name"],
            "callsign": named.get("callsign"), "tagline": named.get("tagline"),
            "archetype": persona.get("archetype"),
            "avatar": avatar_of(r["role_key"], r["agent_id"], persona),
            "lifecycle": r["lifecycle"] or "active",
            "team": team_of(r["spec"])["key"],
        })
    return out


# ── Teams (agents-home spec §4.6, §6.1) ──────────────────────────────────────────────────────

#: The shipped teams, in the order every surface draws them, with their label and tooltip.
#: `yours` is where an operator's agent with no team, and an unregistered name doing work, sit.
TEAMS: tuple[tuple[str, str, Optional[str]], ...] = (
    ("leadership", "Leadership", None),
    ("architecture", "Architecture", None),
    ("engineering", "Engineering", None),
    ("quality", "Quality", "Quality and security"),
    ("operations", "Operations", None),
    ("cost", "Cost", "Cost and providers"),
    ("media", "Media", None),
    ("thinkers", "Thinkers", "Inspired by a way of thinking. Not real people."),
    ("yours", "Yours", None),
)
TEAM_KEYS = tuple(key for key, _, _ in TEAMS)
_TEAM_LABELS = {key: (label, tip) for key, label, tip in TEAMS}
_TEAM_KEY = re.compile(r"[^a-z0-9]+")


def team_of(spec: Any) -> dict[str, Any]:
    """THE ONE RESOLVER for which team an agent sits in: `spec.team`, else `yours`.

    Live's Agent force, Organization › Zones, Directory's sections and the Skills "Who can"
    matrix all group by this, so an agent is in one team everywhere. A shipped key gets its
    label (and tooltip); free text an operator typed is its own team, labelled as written.
    Pass the role's spec (a dict, or a row with `spec`); anything else is `yours`."""
    if isinstance(spec, dict) and "spec" in spec and "team" not in spec:
        spec = spec.get("spec")
    raw = spec.get("team") if isinstance(spec, dict) else None
    text_value = str(raw or "").strip()
    key = _TEAM_KEY.sub("_", text_value.lower()).strip("_")[:40]
    if not key:
        key = "yours"
    if key in _TEAM_LABELS:
        label, tip = _TEAM_LABELS[key]
        return {"key": key, "label": label, **({"tip": tip} if tip else {})}
    return {"key": key, "label": text_value[:40] or key}


def teams_by_role(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    """{role_key: team_of(spec)} for every agent on the project. A role_key missing from the
    answer (an unregistered name doing work) is `yours`: call team_of(None)."""
    rows = connection.execute(
        text("SELECT role_key, spec FROM roles WHERE project_id = :p AND role_key <> 'human'"),
        {"p": project_id},
    ).mappings().all()
    return {str(r["role_key"]): team_of(r["spec"]) for r in rows}


def aliases(connection: Connection, project_id: str) -> dict[str, str]:
    """{title or name (lower case) → role_key}: ALIASES plus this project's names."""
    out = dict(ALIASES)
    for r in identities(connection, project_id):
        for name in (r["display_name"], r.get("callsign")):
            if name and str(name).strip().lower() not in out:
                out[str(name).strip().lower()] = r["role_key"]
    return out


# ── The Passport ─────────────────────────────────────────────────────────────────────────────


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _vitals(connection: Connection, project_id: str, row: dict[str, Any]) -> dict[str, float]:
    runs = int(row.get("successful_runs") or 0)
    faults = sum(int(row.get(k) or 0) for k in (
        "policy_violation_count", "hidden_failure_count", "low_quality_output_count",
        "scope_violation_count"))
    key = row["role_key"]
    # METERED MONEY ONLY, attributed once (llm_telemetry.attribute_llm_calls — the Directory and
    # Organization read the same helper): a call on a subscription is counted as a call
    # (`plan_calls_7d`), never as dollars (operator decision 2026-10-04).
    from .llm_telemetry import attribute_llm_calls, canonical_role

    mine = [c for c in attribute_llm_calls(connection, project_id, 168)
            if c["role"] == (canonical_role(key) or key)]
    cost = sum(c["metered_micros"] for c in mine)
    plan_calls = sum(c["calls"] for c in mine if c["included"])
    panels = 0
    for keys in connection.execute(
            text("SELECT role_keys FROM meeting_runs WHERE project_id = :p "
                 "ORDER BY created_at DESC LIMIT 1000"), {"p": project_id}).scalars():
        if any(k == key or resolve(str(k)) == key for k in (keys or [])):
            panels += 1
    try:
        from .agent_assignment import PROJECT_DEFAULT

        apps = connection.execute(
            text("SELECT count(DISTINCT app_key) FROM project_agents WHERE project_id = :p "
                 "AND role_key = :r AND app_key <> :d"),
            {"p": project_id, "r": key, "d": PROJECT_DEFAULT},
        ).scalar() or 0
    except Exception:  # noqa: BLE001
        apps = 0
    return {"trust": int(row.get("trust_score") or 0), "runs": runs,
            "clean_rate": round(runs / (runs + faults), 4) if runs + faults else 0.0,
            "cost_7d": round(int(cost) / 1_000_000, 4), "plan_calls_7d": int(plan_calls),
            "panels": panels, "apps": int(apps)}


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m"
    hours = minutes / 60
    return f"{int(hours)}h" if hours < 36 else f"{int(hours / 24)}d"


def presence(connection: Connection, project_id: str, role_key: str,
             lifecycle: str = "active") -> dict[str, Any]:
    """What it is doing now, from the work ledger (spec §4.5). Built from data, never a model.

    The Roster's live presence is one CTE in console_v2_workforce (S-acts); this is the same
    rule for one agent, so the Passport can answer without that screen's whole read.
    """
    if lifecycle in ("paused", "retired"):
        return {"state": lifecycle, "line": lifecycle.title()}
    try:
        with connection.begin_nested():
            live = connection.execute(
                text("SELECT act_id, kind, status, label, started_at FROM agent_acts "
                     "WHERE project_id = :p AND role_key = :r AND ended_at IS NULL "
                     "ORDER BY (status = 'awaiting_input') DESC, (kind = 'seat') DESC, "
                     "started_at DESC LIMIT 1"),
                {"p": project_id, "r": role_key},
            ).mappings().first()
            last = None if live else connection.execute(
                text("SELECT act_id, label, ended_at, EXTRACT(EPOCH FROM now() - ended_at) AS ago "
                     "FROM agent_acts WHERE project_id = :p AND role_key = :r "
                     "AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1"),
                {"p": project_id, "r": role_key},
            ).mappings().first()
    except Exception:  # noqa: BLE001 — no ledger yet: honestly idle
        live = last = None
    if live:
        if live["status"] == "awaiting_input":
            state, line = "needs_you", f"Waiting on you: {live['label']}"
        elif live["kind"] == "seat":
            state, line = "working", f"In a panel: {live['label']}"
        else:
            state, line = "working", f"Now: {live['label']}"
        return {"state": state, "act_id": live["act_id"], "line": line,
                "since": _iso(live["started_at"])}
    if last:
        return {"state": "idle", "act_id": last["act_id"],
                "line": f"Last: {last['label']} · {_ago(float(last['ago'] or 0))}",
                "since": _iso(last["ended_at"])}
    return {"state": "idle", "line": "Idle"}


def passport(connection: Connection, project_id: str, role_key: str) -> Passport:
    """GET /v1/agents/{role_key}/passport (spec §5.4). Gives an id to an agent that lacks one."""
    row = _require(connection, project_id, role_key)
    if not row.get("agent_id") or not row.get("fingerprint"):
        backfill(connection, project_id)
        row = _require(connection, project_id, role_key)
    persona = _persona(row["spec"])
    born_at = connection.execute(
        text("SELECT min(created_at) FROM events WHERE project_id = :p AND entity_type = 'agent' "
             "AND entity_id = :a AND event_type = 'agent.born'"),
        {"p": project_id, "a": row["agent_id"]},
    ).scalar() or row.get("created_at")
    lifecycle = row.get("lifecycle") or "active"
    return {
        "agent_id": row["agent_id"],
        "role_key": row["role_key"],
        "display_name": row["display_name"],
        "persona": persona,
        "avatar": avatar_of(row["role_key"], row["agent_id"], persona),
        "born": {"at": _iso(born_at), "by": row.get("born_by") or "spec-pack",
                 "from": row.get("born_from") or {}},
        "sponsor": row.get("sponsor"),
        "lifecycle": lifecycle,
        "version": int(row.get("identity_version") or 1),
        "fingerprint": row.get("fingerprint") or "",
        "reports_to": row.get("reports_to"),
        "vitals": _vitals(connection, project_id, row),
        "presence": presence(connection, project_id, row["role_key"], lifecycle),
        "identity": {"mode": "local", "tokens": False},
    }


def summary(connection: Connection, project_id: str, role_key: str) -> dict[str, Any]:
    """The identity fields other reads add to an agent: id, version, lifecycle, persona."""
    row = _row(connection, project_id, role_key)
    if row is None:
        return {}
    persona = _persona(row["spec"])
    return {"agent_id": row.get("agent_id"), "lifecycle": row.get("lifecycle") or "active",
            "version": int(row.get("identity_version") or 1),
            "fingerprint": row.get("fingerprint") or "", "sponsor": row.get("sponsor"),
            "born_by": row.get("born_by"), "born_from": row.get("born_from") or {},
            "persona": persona,
            "avatar": avatar_of(row["role_key"], row.get("agent_id"), persona)}


# ── Lifecycle ────────────────────────────────────────────────────────────────────────────────

_VERB = {"active": "resume", "paused": "pause", "retired": "retire"}


def set_lifecycle(connection: Connection, project_id: str, role_key: str, state: str,
                  actor: str, reason: str, *, is_owner: bool = False) -> dict:
    """Pause, resume or retire an agent. Only its sponsor or an owner may (403 otherwise).

    `is_owner` is decided by the caller the way every owner-only act is (a HUMAN member with
    the ceo role — main.require_autonomy_operator), never by role_review alone. An agent
    nobody sponsors (every shipped one) is the owners' and only theirs: role_review let any
    reviewer through, including the `coo` system member — an agent able to retire agents.
    Paused agents are skipped by Auto and a step naming one fails fast; retired ones also
    leave the pickers. Their work history stays.
    """
    wanted = str(state or "").strip().lower()
    if wanted not in LIFECYCLES:
        raise IdentityError("state must be active, paused or retired.")
    row = _require(connection, project_id, role_key, lock=True)
    sponsor = str(row.get("sponsor") or "").strip()
    if sponsor and actor != sponsor and not is_owner:
        raise IdentityError(f"Only the sponsor can {_VERB[wanted]} this agent", status=403)
    if not sponsor and not is_owner:
        raise IdentityError(f"Only an owner can {_VERB[wanted]} this agent", status=403)
    current = row.get("lifecycle") or "active"
    if current == wanted:
        return passport(connection, project_id, row["role_key"])
    connection.execute(
        text("UPDATE roles SET lifecycle = :s WHERE project_id = :p AND role_key = :r"),
        {"s": wanted, "p": project_id, "r": row["role_key"]},
    )
    emit_event(connection, project_id, "agent.lifecycle", actor, "agent",
               str(row.get("agent_id") or row["role_key"]),
               {"role_key": row["role_key"], "from": current, "to": wanted,
                "reason": str(reason or "")[:400]})
    seal_identity(connection, project_id, row["role_key"], "lifecycle", actor)
    return passport(connection, project_id, row["role_key"])


def paused_message(name: str, lifecycle: str) -> str:
    """`<Name> is paused` — the one sentence a run, a start check and a pick all use."""
    if lifecycle == "retired":
        return (f"{name} is retired. Give the step to another agent, or leave it for BOSS "
                "to pick.")
    return (f"{name} is paused. Resume it on its page in Agent Studio, or give the step to another "
            "agent.")


def display_name_of(row: dict[str, Any]) -> str:
    persona = _persona(row.get("spec"))
    return str(persona.get("callsign") or row.get("display_name") or row.get("name")
               or row.get("role_key") or "This agent")


# ── The card ─────────────────────────────────────────────────────────────────────────────────


def _org_of(connection: Connection, project_id: str) -> str:
    try:
        org = connection.execute(
            text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                 "WHERE p.id = :p"), {"p": project_id}).scalar()
    except Exception:  # noqa: BLE001
        org = None
    return str(org or project_id)


def agent_card(connection: Connection, project_id: str, role_key: str) -> dict:
    """A2A Agent Card v1.0 + `x-boss`. Unsigned: the UI says `Version`, never `Signed`.

    Never carries repositories, MCP server names, costs or internal ids beyond the agt_ id.
    `iconUrl` is left to the web, which draws the face (lib/agent-avatar.ts) — one renderer.
    """
    p = passport(connection, project_id, role_key)
    persona = p["persona"]
    skills = connection.execute(
        text("SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :r "
             "ORDER BY skill_id"), {"p": project_id, "r": p["role_key"]},
    ).scalars().all()
    description = " ".join(x for x in (persona.get("tagline"), persona.get("about_me")) if x)
    name = (f"{persona['callsign']} · {p['display_name']}" if persona.get("callsign")
            else p["display_name"])
    return {
        "protocolVersion": "1.0",
        "name": name,
        "description": description or f"{p['display_name']}, an AI agent in BOSS.",
        "version": f"{p['version']}.0.0",
        "capabilities": {"streaming": True, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{"id": s, "name": s, "description": f"Holds {s}", "tags": [s.split(".")[0]]}
                   for s in skills],
        "x-boss": {
            "agent_id": p["agent_id"],
            "uri": f"boss://{_org_of(connection, project_id)}/agents/{p['agent_id']}",
            "role_key": p["role_key"],
            "archetype": persona.get("archetype"),
            "born": p["born"]["at"],
            "sponsor": p["sponsor"],
            "fingerprint": p["fingerprint"],
            "ai_agent": True,
            "signed": False,
        },
    }
