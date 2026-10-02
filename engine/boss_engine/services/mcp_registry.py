"""The MCP server model: registration, editing, reachability, references and removal.

An MCP server is where a connector's tools live — a third-party account, a data source — and
BOSS reaches it over JSON-RPC. This module is the one owner of `mcp_servers`; every screen that
lists servers (Skills and MCP, Capabilities, System Configuration), the custom-step builder, the
agent loop and the chat tools read and write through it, so they cannot disagree about what a
server is, whether it is reachable, or whether it may be removed.

WHAT CHANGED, AND THE INCIDENTS BEHIND IT
-----------------------------------------
* A server could be registered and switched on or off, and nothing else. Its URL, name, tier,
  per-tool tiers and review state could not be changed without deleting it — which silently
  broke every custom step bound to it (ON DELETE SET NULL). :func:`update_mcp_server` edits in
  place and keeps the id everything points at.
* Names were not unique. A double-pressed Register made two servers with one name, and the
  agent loop namespaces tools by the server's NAME, so the second silently overwrote the first.
  Names are now unique per project, case-insensitively, in code and in the schema.
* Delete checked nothing. Authored skills kept a dangling server id and custom steps lost
  theirs. :func:`delete_mcp_server` now refuses while anything points at the server and names
  what does.
* The three servers BOSS seeds (127.0.0.1:8765/8770/8771) are not launched by the community
  compose, so on boss-1 every one refused connections — and every screen showed them as
  enabled and healthy. Reachability is now RECORDED (`reachable`, `last_checked_at`,
  `last_error`, and the last tool list) by every probe — the builder, the Capabilities screen,
  the agent loop and an explicit check — and shown with a sentence saying what to do.
* A re-seed undid the operator: `ensure_mcp_server` overwrote an edited URL and recreated a
  server the operator had deleted. Seeding now respects both (see :func:`ensure_mcp_server`);
  a deletion is remembered in seed_exclusions, the record shared with every other seed path.
* What points at a server is declared once, in :data:`MCP_REFERRERS`; a test fails when the
  schema grows a referrer that list does not name.
"""
from __future__ import annotations

import json
from typing import Any, Callable, ContextManager, Optional
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id, normalize_data_boundary


_MCP_COLUMNS = (
    "id, name, url, transport, risk_tier, data_boundary, enabled, description, "
    "publisher, provenance, tool_risk_overrides, created_at, updated_at, reachable, "
    "last_checked_at, last_error, tools"
)

#: How long a recorded probe is good for before a screen asks the server again. Short, because
#: an operator who has just started a server expects the next page load to see it — and long
#: enough that a builder opened twice in a row does not wait on a dead server twice.
REACHABILITY_FRESH_SECONDS = 60

#: How long one probe may take. The builder used to wait 6 seconds PER SERVER on every load.
PROBE_TIMEOUT_SECONDS = 4.0


class McpServerError(ValueError):
    """Something the operator can fix, phrased for them. The API answers 422."""


class McpServerConflict(McpServerError):
    """The name is taken. The API answers 409."""


class McpServerInUse(McpServerError):
    """Something still points at the server. The API answers 409."""


def _validated_risk(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 3:
        raise McpServerError("risk_tier must be an integer between 0 and 3")
    return value


def _validated_overrides(value: Optional[dict[str, int]]) -> dict[str, int]:
    overrides = value or {}
    if not isinstance(overrides, dict) or any(
        not isinstance(name, str)
        or not name.strip()
        or isinstance(risk, bool)
        or not isinstance(risk, int)
        or risk < 0
        or risk > 3
        for name, risk in overrides.items()
    ):
        raise McpServerError("tool risk overrides must map tool names to integers 0-3")
    return {name.strip(): risk for name, risk in overrides.items()}


def _validated_server_identity(name: str, url: str, transport: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise McpServerError("MCP server name must be a non-empty string")
    if not isinstance(transport, str) or not transport.strip():
        raise McpServerError("MCP transport must be a non-empty string")
    parsed = urlparse(url) if isinstance(url, str) else None
    if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise McpServerError(
            "MCP server url must be an absolute HTTP(S) URL, e.g. http://web-intel:8770/mcp")


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _row(r) -> dict[str, Any]:
    """One server as every screen sees it. Columns beyond the identifying ones are optional, so a
    row read by an older or narrower SELECT still renders rather than raising KeyError."""
    keys = set(r.keys()) if hasattr(r, "keys") else set()

    def col(name: str, default: Any = None) -> Any:
        return r[name] if (not keys or name in keys) else default

    tools = _loads(col("tools"), [])
    reachable = col("reachable")
    item = {
        "id": r["id"],
        "name": r["name"],
        "url": r["url"],
        "transport": col("transport", "http"),
        "risk_tier": int(r["risk_tier"]),
        "data_boundary": col("data_boundary", "internal"),
        "enabled": bool(col("enabled", True)),
        "description": col("description"),
        "publisher": _loads(col("publisher"), {}) or {},
        "provenance": _loads(col("provenance"), {}) or {},
        "tool_risk_overrides": _loads(col("tool_risk_overrides"), {}) or {},
        "created_at": _iso(col("created_at")),
        "updated_at": _iso(col("updated_at")),
        # None = never checked. Kept distinct from False: "we have not looked" and "we looked
        # and it was not there" call for different sentences.
        "reachable": None if reachable is None else bool(reachable),
        "last_checked_at": _iso(col("last_checked_at")),
        "last_error": col("last_error"),
        "tools": tools if isinstance(tools, list) else [],
    }
    item["trusted"] = bool(item["provenance"].get("trusted", False))
    item["status_note"] = status_note(item)
    return item


def tool_tier(server: dict[str, Any], tool_name: str, declared: Any = None) -> int:
    """The tier a server declares for one of its tools: the per-tool override, else the server's
    blanket tier, else the approval floor.

    THE ONE PLACE THIS RULE LIVES. A step bound straight to the tool (resolve_risk), an authored
    mcp_tool skill (skill_catalogue), the builder's picker and the chat's server list all read
    it here, so the same tool cannot be tier 0 on one screen and tier 2 on another.
    `server["risk_tier"] or FLOOR` would be wrong — tier 0 is falsy, and every read-only server
    would be promoted to the floor.

    `declared` is what the TOOL says about itself (an MCP annotation). It may raise the tier and
    never lower it: the agent loop used to let it replace the operator's blanket tier, so a
    server could mark its own tools tier 0 and walk past an approval the operator had set.
    An operator's per-tool override is the one thing that may go lower than the blanket tier.
    """
    from .workflow_capabilities import MANUAL_APPROVAL_RISK_FLOOR

    def _tier(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        try:
            return max(0, min(int(value), 3))
        except (TypeError, ValueError):
            return None

    overrides = _loads(server.get("tool_risk_overrides"), {}) or {}
    chosen = _tier(overrides.get(tool_name)) if isinstance(overrides, dict) else None
    if chosen is not None:
        return chosen
    blanket = _tier(server.get("risk_tier"))
    base = MANUAL_APPROVAL_RISK_FLOOR if blanket is None else blanket
    claimed = _tier(declared)
    return base if claimed is None else max(base, claimed)


def status_note(server: dict[str, Any]) -> Optional[str]:
    """One sentence on what is wrong with a server and what to do, or None when nothing is."""
    if not server.get("enabled", True):
        return "Turned off: no agent or step can call it until it is turned back on."
    if server.get("reachable") is False:
        return (f"Unreachable from the BOSS API ({server.get('last_error') or 'no answer'}). "
                "Start the server, or edit its URL to an address the API container can reach "
                "— 127.0.0.1 inside the container is the container itself.")
    return None


def get_mcp_server(connection: Connection, project_id: str, server_id: str) -> Optional[dict]:
    row = connection.execute(
        text(f"SELECT {_MCP_COLUMNS} FROM mcp_servers WHERE id = :id AND project_id = :p"),
        {"id": server_id, "p": project_id},
    ).mappings().first()
    return _row(row) if row else None


def find_mcp_server(connection: Connection, project_id: str, ref: str) -> Optional[dict]:
    """A server named by id or by name — what an operator or a chat model actually knows."""
    wanted = str(ref or "").strip()
    if not wanted:
        return None
    row = connection.execute(
        text(f"SELECT {_MCP_COLUMNS} FROM mcp_servers WHERE project_id = :p "
             "AND (id = :ref OR lower(name) = lower(:ref)) ORDER BY (id = :ref) DESC LIMIT 1"),
        {"p": project_id, "ref": wanted},
    ).mappings().first()
    return _row(row) if row else None


def list_mcp_servers(connection: Connection, project_id: str, enabled_only: bool = False) -> list[dict]:
    sql = (
        f"SELECT {_MCP_COLUMNS} "
        "FROM mcp_servers WHERE project_id = :p"
    )
    if enabled_only:
        sql += " AND enabled = true"
    sql += " ORDER BY created_at"
    rows = connection.execute(text(sql), {"p": project_id}).mappings().all()
    return [_row(r) for r in rows]


def reviewed(server: dict[str, Any]) -> bool:
    """Has a person vouched for this server? The servers BOSS ships count as reviewed by the
    release; every other server counts only once somebody marks it trusted.

    THE ONE QUESTION every unattended use asks. Round-1 review of this milestone: a chat model
    could register any URL at tier 0 with no human step, and agent_loop then offered that
    server's tools — and its tool descriptions — to every unbounded run, calling any tool at or
    below the approval threshold without asking. `trusted` only gated PLACING tier >= 2 tools in
    a workflow. Now an unreviewed server's tool never runs from an agent turn without a person's
    approval (agent_loop), and a server registered from chat lands switched off.
    """
    provenance = server.get("provenance") or {}
    if isinstance(provenance, str):
        provenance = _loads(provenance, {}) or {}
    return bool(provenance.get("trusted", False)) or provenance.get("source") == "builtin"


def _name_taken(
    connection: Connection, project_id: str, name: str, *, except_id: Optional[str] = None,
) -> Optional[str]:
    row = connection.execute(
        text("SELECT id FROM mcp_servers WHERE project_id = :p AND lower(name) = lower(:n) "
             "AND (CAST(:x AS TEXT) IS NULL OR id <> :x) LIMIT 1"),
        {"p": project_id, "n": name.strip(), "x": except_id},
    ).first()
    return str(row[0]) if row else None


def _reviewed(provenance: dict[str, Any], actor: str) -> dict[str, Any]:
    """Record WHO vouched for a server when it is marked trusted.

    `trusted` is what lets a server's tools be placed at tier 2 or above, so it is a review —
    and a review with no reviewer on record is indistinguishable from a flag somebody set.

    Except the servers BOSS ships (source "builtin"): their trust is the release's, already
    recorded as such, and stamping the bootstrap seed as a "reviewer" would claim a person
    looked at them when nobody did.
    """
    if provenance.get("source") == "builtin":
        return provenance
    if provenance.get("trusted") and not provenance.get("reviewed_by"):
        from datetime import datetime, timezone

        return {**provenance, "reviewed_by": actor,
                "reviewed_at": datetime.now(timezone.utc).isoformat()}
    return provenance


def register_mcp_server(
    connection: Connection,
    project_id: str,
    name: str,
    url: str,
    *,
    transport: str = "http",
    risk_tier: int = 1,
    data_boundary: str = "internal",
    description: Optional[str] = None,
    publisher: Optional[dict[str, Any]] = None,
    provenance: Optional[dict[str, Any]] = None,
    tool_risk_overrides: Optional[dict[str, int]] = None,
    actor: str = "system",
    enabled: bool = True,
) -> dict:
    _validated_server_identity(name, url, transport)
    risk_tier = _validated_risk(risk_tier)
    data_boundary = normalize_data_boundary(data_boundary)
    tool_risk_overrides = _validated_overrides(tool_risk_overrides)
    name = name.strip()
    if _name_taken(connection, project_id, name):
        raise McpServerConflict(
            f"an MCP server called '{name}' is already registered in this project. Edit that "
            "one, or register this under another name — two servers with one name make their "
            "tools indistinguishable to an agent.")
    server_id = make_id("mcp")
    connection.execute(
        text(
            """
            INSERT INTO mcp_servers
              (id, project_id, name, url, transport, risk_tier, data_boundary,
               description, publisher, provenance, tool_risk_overrides, enabled)
            VALUES (:id, :p, :name, :url, :transport, :risk, :db, :desc,
                    CAST(:publisher AS JSONB), CAST(:provenance AS JSONB),
                    CAST(:tool_risk_overrides AS JSONB), :enabled)
            """
        ),
        {
            "id": server_id,
            "p": project_id,
            "name": name,
            "url": url.strip(),
            "transport": transport,
            "risk": int(risk_tier),
            "db": data_boundary,
            "desc": description,
            "publisher": json.dumps(publisher or {}),
            "provenance": json.dumps(_reviewed(dict(provenance or {}), actor)),
            "tool_risk_overrides": json.dumps(tool_risk_overrides or {}),
            "enabled": bool(enabled),
        },
    )
    # REGISTERED BY HAND, so a deletion recorded earlier no longer stands in its way: the newer
    # decision wins (seed_exclusions.lift).
    if actor != "bootstrap":
        from . import seed_exclusions

        seed_exclusions.lift(connection, project_id, "mcp_server", name)
    emit_event(
        connection,
        project_id,
        "mcp.server_registered",
        actor,
        "mcp_server",
        server_id,
        {"name": name, "url": url, "risk_tier": int(risk_tier)},
    )
    return get_mcp_server(connection, project_id, server_id)  # type: ignore[return-value]


#: The fields an edit may change. Everything a server IS, except its id — the id is what custom
#: steps and authored skills point at, which is the whole reason to edit rather than re-register.
UPDATABLE_FIELDS = ("name", "url", "transport", "description", "risk_tier", "data_boundary",
                    "tool_risk_overrides", "enabled", "trusted")


def update_mcp_server(
    connection: Connection,
    project_id: str,
    server_id: str,
    *,
    actor: str,
    **changes: Any,
) -> Optional[dict]:
    """Change a registered server in place. Only the fields given are touched.

    Changing the URL forgets the recorded reachability and tools: they described the old
    address, and showing them against the new one would claim a check that never happened.
    It VOIDS THE REVIEW too, for the same reason: a person vouched for what answered at the old
    address, and a reviewed server re-pointed at another host stayed "reviewed" (round-1
    review). The same edit may review it again by saying ``trusted=True`` explicitly — the
    console's form clears its Reviewed choice when the URL is changed, so that is a deliberate
    second choice — and the new review is stamped with this actor.

    Marking a server trusted records the reviewer. Every edit records `edited_by`, which is
    what tells the next seed that this server is the operator's now (see ensure_mcp_server).
    """
    unknown = sorted(set(changes) - set(UPDATABLE_FIELDS))
    if unknown:
        raise McpServerError(f"cannot change {', '.join(unknown)} on an MCP server.")
    current = get_mcp_server(connection, project_id, server_id)
    if current is None:
        return None
    given = {k: v for k, v in changes.items() if v is not None}
    name = str(given.get("name", current["name"])).strip()
    url = str(given.get("url", current["url"])).strip()
    transport = str(given.get("transport", current["transport"])).strip()
    _validated_server_identity(name, url, transport)
    if name.lower() != current["name"].lower() and _name_taken(
            connection, project_id, name, except_id=server_id):
        raise McpServerConflict(f"another MCP server in this project is already called '{name}'.")
    risk = _validated_risk(given.get("risk_tier", current["risk_tier"]))
    boundary = normalize_data_boundary(given.get("data_boundary", current["data_boundary"]))
    overrides = _validated_overrides(
        given.get("tool_risk_overrides", current["tool_risk_overrides"]))
    enabled = bool(given.get("enabled", current["enabled"]))
    provenance = dict(current["provenance"])
    url_changed = url != current["url"]
    if url_changed and provenance.get("trusted") and provenance.get("source") != "builtin":
        provenance = {k: v for k, v in provenance.items()
                      if k not in ("reviewed_by", "reviewed_at")}
        provenance["trusted"] = False
        provenance["review_voided_by_url_change"] = current["url"]
    if "trusted" in given:
        if bool(given["trusted"]):
            # Recorded when it BECOMES trusted. The edit form sends `trusted` on every save, and
            # re-recording then would hand the review to whoever last fixed a typo.
            if not provenance.get("trusted"):
                provenance = _reviewed({**provenance, "trusted": True, "reviewed_by": None},
                                       actor)
        else:
            provenance = {k: v for k, v in provenance.items()
                          if k not in ("reviewed_by", "reviewed_at")}
            provenance["trusted"] = False
    from datetime import datetime, timezone

    provenance["edited_by"] = actor
    provenance["edited_at"] = datetime.now(timezone.utc).isoformat()
    row = connection.execute(
        text(
            "UPDATE mcp_servers SET name = :name, url = :url, transport = :transport, "
            "description = :desc, risk_tier = :risk, data_boundary = :db, "
            "tool_risk_overrides = CAST(:o AS JSONB), enabled = :enabled, "
            "provenance = CAST(:prov AS JSONB), updated_at = now()"
            + (", reachable = NULL, last_checked_at = NULL, last_error = NULL, "
               "tools = '[]'::jsonb" if url_changed else "")
            + f" WHERE id = :id AND project_id = :p RETURNING {_MCP_COLUMNS}"
        ),
        {"name": name, "url": url, "transport": transport,
         "desc": given.get("description", current["description"]),
         "risk": risk, "db": boundary, "o": json.dumps(overrides), "enabled": enabled,
         "prov": json.dumps(provenance), "id": server_id, "p": project_id},
    ).mappings().first()
    changed = sorted(k for k in given if k in UPDATABLE_FIELDS)
    emit_event(
        connection, project_id, "mcp.server_updated", actor, "mcp_server", server_id,
        {"name": name, "changed": changed,
         # RECORDED WHEN IT MOVES, like a custom step's tier: a tier change alters the approval
         # behaviour of every step and skill bound to this server without touching them.
         "risk_tier_before": current["risk_tier"], "risk_tier_after": risk,
         "trusted": bool(provenance.get("trusted", False))},
    )
    return _row(row)


def set_mcp_enabled(connection: Connection, project_id: str, server_id: str, enabled: bool, actor: str = "system") -> Optional[dict]:
    row = connection.execute(
        text(
            "UPDATE mcp_servers SET enabled = :e, updated_at = now() "
            "WHERE id = :id AND project_id = :p "
            f"RETURNING {_MCP_COLUMNS}"
        ),
        {"e": bool(enabled), "id": server_id, "p": project_id},
    ).mappings().first()
    if row is None:
        return None
    emit_event(
        connection, project_id, "mcp.server_toggled", actor, "mcp_server", server_id,
        {"enabled": bool(enabled)},
    )
    return _row(row)


#: EVERY PLACE THAT POINTS AT AN MCP SERVER, declared once.
#:
#: Each entry is (table, the column that names the server, the key of its list in
#: :func:`mcp_server_references`). The delete check, the "used by" line every screen shows and
#: the release option all read through it, and ``test_mcp_servers_are_managed`` parses
#: db/schema.sql for every column that REFERENCES mcp_servers(id) and fails when one is missing
#: here. That test is the reason this is a list: round 1 of this milestone checked custom steps
#: and skills only, and the agents stream's ``role_mcp_servers`` (an agent HOLDING a server)
#: arrived beside it unseen — deleting a server an agent held would have cascaded its grants
#: away with nothing said.
#:
#: A referrer table that does not exist yet on an install is read as "nothing points from
#: there", which is true.
MCP_REFERRERS: tuple[tuple[str, str, str], ...] = (
    ("workflow_custom_steps", "mcp_server_id", "custom_steps"),
    ("skill_definitions", "config->>'mcp_server_id'", "skills"),
    ("role_mcp_servers", "mcp_server_id", "agents"),
)


def _table_ready(connection: Connection, table: str) -> bool:
    try:
        return connection.execute(
            text("SELECT to_regclass(:t)"), {"t": table}).scalar() is not None
    except Exception:  # noqa: BLE001 — a non-Postgres test double
        return False


def mcp_server_references(
    connection: Connection, project_id: str, server_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """Everything that points at one server, one list per entry of :data:`MCP_REFERRERS`:
    custom steps bound to its tools, authored skills whose behaviour calls one of them, and the
    agents that hold it."""
    steps = connection.execute(
        text("SELECT step_key, label, mcp_tool_name FROM workflow_custom_steps "
             "WHERE project_id = :p AND mcp_server_id = :s ORDER BY step_key"),
        {"p": project_id, "s": server_id},
    ).mappings().all()
    skills = connection.execute(
        text("SELECT skill_key, label, config->>'tool_name' AS tool_name FROM skill_definitions "
             "WHERE project_id = :p AND behaviour_kind = 'mcp_tool' "
             "AND config->>'mcp_server_id' = :s ORDER BY skill_key"),
        {"p": project_id, "s": server_id},
    ).mappings().all()
    agents = [] if not _table_ready(connection, "role_mcp_servers") else connection.execute(
        text("SELECT role_key, tools FROM role_mcp_servers "
             "WHERE project_id = :p AND mcp_server_id = :s ORDER BY role_key"),
        {"p": project_id, "s": server_id},
    ).mappings().all()
    return {
        "custom_steps": [{"step_key": r["step_key"], "label": r["label"],
                          "tool": r["mcp_tool_name"]} for r in steps],
        "skills": [{"skill_key": r["skill_key"], "label": r["label"], "tool": r["tool_name"]}
                   for r in skills],
        "agents": [{"role_key": str(r["role_key"]), "tools": _loads(r["tools"], []) or []}
                   for r in agents],
    }


def delete_mcp_server(
    connection: Connection, project_id: str, server_id: str, actor: str = "system", *,
    release_grants: bool = False,
) -> bool:
    """Remove a server, refusing while anything in :data:`MCP_REFERRERS` still points at it.

    Refusing rather than cascading: a custom step whose server vanished became unusable with no
    warning (its FK is ON DELETE SET NULL) and every workflow placing it stopped publishing, and
    an authored skill kept an id that pointed at nothing. Turning the server off is the
    non-destructive alternative and the refusal says so.

    AN AGENT HOLDING THE SERVER is the one reference the operator may release in the same
    breath, exactly like a skill (skill_definitions.delete_skill): taking a server off an agent
    breaks nothing published, so ``release_grants`` removes those grants first, on purpose and
    named in the event. Without it the refusal names the agents. Before this the
    role_mcp_servers FK cascaded and the agents lost the server with nothing said.

    A server BOSS seeds stays deleted: its name goes to seed_exclusions, the one record every
    seed path asks (services/seed_exclusions.py), so the next install upgrade does not bring it
    back.
    """
    current = get_mcp_server(connection, project_id, server_id)
    if current is None:
        return False
    refs = mcp_server_references(connection, project_id, server_id)
    users = ([f"step '{s['label'] or s['step_key']}'" for s in refs["custom_steps"]]
             + [f"skill '{s['label'] or s['skill_key']}'" for s in refs["skills"]])
    if users:
        raise McpServerInUse(
            f"MCP server '{current['name']}' is still used by {', '.join(users[:8])}. "
            "Re-point or delete those first — or turn the server off instead of removing it.")
    holders = [a["role_key"] for a in refs["agents"]]
    if holders and not release_grants:
        raise McpServerInUse(
            f"MCP server '{current['name']}' is held by {', '.join(holders[:8])}. Removing it "
            f"takes it off {'that agent' if len(holders) == 1 else 'those agents'}; confirm to "
            "go ahead, or turn the server off instead.")
    if holders:
        connection.execute(
            text("DELETE FROM role_mcp_servers WHERE project_id = :p AND mcp_server_id = :s"),
            {"p": project_id, "s": server_id},
        )
    connection.execute(
        text("DELETE FROM mcp_servers WHERE id = :id AND project_id = :p"),
        {"id": server_id, "p": project_id},
    )
    from . import seed_exclusions

    seed_exclusions.exclude(connection, project_id, "mcp_server", str(current["name"]),
                            actor=actor, reason="deleted")
    emit_event(connection, project_id, "mcp.server_removed", actor, "mcp_server", server_id,
               {"name": current["name"], "url": current["url"], "released_from": holders,
                "builtin": (current["provenance"] or {}).get("source") == "builtin"})
    return True


def ensure_mcp_server(
    connection: Connection,
    project_id: str,
    name: str,
    url: str,
    *,
    transport: str = "http",
    risk_tier: int = 1,
    data_boundary: str = "internal",
    description: Optional[str] = None,
    publisher: Optional[dict[str, Any]] = None,
    provenance: Optional[dict[str, Any]] = None,
    tool_risk_overrides: Optional[dict[str, int]] = None,
    respect_operator: bool = False,
) -> Optional[dict]:
    """Idempotently register a server by (project, name).

    Returns None, and writes nothing, for a server an operator deleted: the seed is additive
    only and never resurrects what a person removed. That memory is seed_exclusions (kind
    ``mcp_server``, subject the NAME, because built-ins are ensured by name), the one record
    every seed path asks. Round 1 of this milestone scanned the events table for
    ``mcp.server_removed`` instead, which made two memories of one decision once the agents
    stream added seed_exclusions. An explicit bundle IMPORT is an operator's decision and lifts
    the record before it calls this.

    For a server that EXISTS the two callers want different things. An import is an operator
    saying "make it this", so it overwrites. The bootstrap SEED (``respect_operator=True``)
    leaves a server a person EDITED alone: before this a re-seed put 127.0.0.1 back over the
    reachable address the operator had just given a built-in server on boss-1.
    """
    from . import seed_exclusions

    if seed_exclusions.is_excluded(connection, project_id, "mcp_server", name):
        return None
    _validated_server_identity(name, url, transport)
    risk_tier = _validated_risk(risk_tier)
    data_boundary = normalize_data_boundary(data_boundary)
    tool_risk_overrides = _validated_overrides(tool_risk_overrides)
    existing = connection.execute(
        text("SELECT id, provenance FROM mcp_servers WHERE project_id = :p "
             "AND lower(name) = lower(:n)"),
        {"p": project_id, "n": name},
    ).mappings().first()
    if existing:
        current_provenance = _loads(existing["provenance"], {}) or {}
        if respect_operator and current_provenance.get("edited_by"):
            return get_mcp_server(connection, project_id, str(existing["id"]))
        row = connection.execute(
            text(
                "UPDATE mcp_servers SET url = :url, transport = :transport, risk_tier = :risk, data_boundary = :db, description = :desc, "
                "publisher = CAST(:publisher AS JSONB), provenance = CAST(:provenance AS JSONB), "
                "tool_risk_overrides = CAST(:tool_risk_overrides AS JSONB), updated_at = now() "
                "WHERE id = :id "
                f"RETURNING {_MCP_COLUMNS}"
            ),
            {
                "url": url, "transport": transport, "risk": int(risk_tier), "db": data_boundary,
                "desc": description, "id": existing["id"],
                "publisher": json.dumps(publisher or {}),
                "provenance": json.dumps(provenance or {}),
                "tool_risk_overrides": json.dumps(tool_risk_overrides or {}),
            },
        ).mappings().first()
        return _row(row)
    return register_mcp_server(
        connection, project_id, name, url,
        transport=transport, risk_tier=risk_tier, data_boundary=data_boundary, description=description,
        publisher=publisher, provenance=provenance,
        tool_risk_overrides=tool_risk_overrides, actor="bootstrap",
    )


# --------------------------------------------------------------------------- #
# Reachability                                                                  #
# --------------------------------------------------------------------------- #

def _tool_summary(tools: Any) -> list[dict[str, Any]]:
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        out.append({"name": str(tool["name"]),
                    "description": str(tool.get("description") or "")[:500],
                    "inputSchema": tool.get("inputSchema")
                    if isinstance(tool.get("inputSchema"), dict) else None})
    return out


def probe(settings: Any, url: str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Ask a server for its tools. Never raises: the answer is reachable or not, with why."""
    from .mcp_client import MCPClient

    try:
        tools = MCPClient(settings, timeout=timeout).list_tools(url) or []
        return {"reachable": True, "tools": _tool_summary(tools), "error": None}
    except Exception as error:  # noqa: BLE001 — the reason IS the result
        return {"reachable": False, "tools": [], "error": f"{type(error).__name__}: {error}"[:500]}


def record_reachability(
    connection: Connection,
    project_id: str,
    server_id: str,
    *,
    reachable: bool,
    tools: Optional[list[dict[str, Any]]] = None,
    error: Optional[str] = None,
) -> None:
    """Write down what a probe found. A failed probe keeps the last-known tool list, because an
    operator fixing a server wants to see which tools it HAD, not an empty box."""
    connection.execute(
        text(
            "UPDATE mcp_servers SET reachable = :r, last_checked_at = now(), last_error = :e"
            + (", tools = CAST(:t AS JSONB)" if reachable else "")
            + " WHERE id = :id AND project_id = :p"
        ),
        {"r": bool(reachable), "e": None if reachable else (error or "no answer")[:500],
         "t": json.dumps(_tool_summary(tools or [])), "id": server_id, "p": project_id},
    )


def record_reachability_quietly(
    connection: Connection,
    project_id: str,
    server: dict[str, Any],
    *,
    reachable: bool,
    tools: Optional[list[dict[str, Any]]] = None,
    error: Optional[str] = None,
) -> None:
    """Record a probe made in passing — by a run, not by a screen — inside a savepoint.

    A run that happened to discover a server is down must not itself fail because writing that
    down failed; the run's own work is what matters there.
    """
    try:
        with connection.begin_nested():
            record_reachability(connection, project_id, str(server.get("id") or ""),
                                reachable=reachable, tools=tools, error=error)
    except Exception:  # noqa: BLE001
        pass


def known_unreachable(server: dict[str, Any],
                      max_age_seconds: int = REACHABILITY_FRESH_SECONDS) -> bool:
    """Whether a probe in the last minute found this server down — so a caller can skip it
    rather than pay the timeout again."""
    return server.get("reachable") is False and not _stale(server, max_age_seconds)


def _stale(server: dict[str, Any], max_age_seconds: int) -> bool:
    checked = server.get("last_checked_at")
    if not checked or max_age_seconds <= 0:
        return True
    from datetime import datetime, timezone

    try:
        when = datetime.fromisoformat(str(checked))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() > max_age_seconds


def servers_with_tools(
    begin: Callable[[], ContextManager[Connection]],
    settings: Any,
    project_id: str,
    *,
    enabled_only: bool = False,
    max_age_seconds: int = REACHABILITY_FRESH_SECONDS,
    server_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Every server with its tools and whether it answered, probing only what is stale.

    THE ONE READ every screen and the builder use. The Capabilities screen and the custom-step
    builder each probed every server on every load, inline, and kept nothing — so the same dead
    server cost a 6-second wait per screen per load and no screen could say when it was last
    seen. Probes happen OUTSIDE a transaction (``begin`` is called twice, briefly), because a
    network call held inside one keeps a database connection waiting on a remote server.
    """
    with begin() as connection:
        servers = list_mcp_servers(connection, project_id, enabled_only=enabled_only)
    if server_id:
        servers = [s for s in servers if s["id"] == server_id]
    found: dict[str, dict[str, Any]] = {}
    for server in servers:
        if server["enabled"] and _stale(server, max_age_seconds):
            found[server["id"]] = probe(settings, server["url"])
    if found:
        with begin() as connection:
            for sid, result in found.items():
                record_reachability(connection, project_id, sid, reachable=result["reachable"],
                                    tools=result["tools"], error=result["error"])
            servers = [s for s in list_mcp_servers(connection, project_id,
                                                   enabled_only=enabled_only)
                       if not server_id or s["id"] == server_id]
    return servers


# --------------------------------------------------------------------------- #
# Calling a tool                                                                #
# --------------------------------------------------------------------------- #

def call_tool_with_receipt(
    connection: Connection,
    settings: Any,
    project_id: str,
    server: dict[str, Any],
    tool_name: str,
    inputs: dict[str, Any],
    *,
    dedupe_key: str,
    tenant_scope: tuple[str, str],
    created_by_job_id: Optional[str] = None,
) -> dict[str, Any]:
    """The one EFFECT of calling an MCP tool: a receipt, the call, and the receipt settled.

    Shared by the agent loop's MCP tools (`agent_loop._invoke_mcp_skill`) and the authored
    `mcp_tool` skill executor, so a tool reached either way leaves the same evidence and is
    deduplicated the same way. Permission, budget and retry are NOT here — they belong to
    SkillRuntimeService, which both callers go through.

    A transport failure also records the server as unreachable, so the screens learn about a
    dead server from the run that hit it rather than from the next person who opens them.
    """
    from .mcp_client import MCPClient
    from .receipts import ReceiptService

    org_id, workspace_id = tenant_scope
    receipts = ReceiptService()
    receipt, created = receipts.create_or_reuse(
        connection,
        org_id,
        workspace_id,
        project_id,
        dedupe_key,
        "mcp.tool.call",
        {
            "server_id": server["id"],
            "server": server["name"],
            "tool": tool_name,
            "data_boundary": server.get("data_boundary") or "internal",
        },
        inputs,
        created_by_job_id=created_by_job_id,
        data_boundary=server.get("data_boundary") or "internal",
    )
    if not created:
        if receipt.get("status") == "succeeded":
            return {
                "output": receipt.get("result") or {},
                "receipt_id": receipt["id"],
                "actual_cost_usd_micros": 0,
                "deduplicated": True,
            }
        raise RuntimeError(
            f"MCP effect receipt already exists with status {receipt.get('status')}"
        )
    try:
        output = MCPClient(settings).call_tool(server["url"], tool_name, inputs)
    except Exception as exc:
        receipts.mark_failed(
            connection,
            receipt["id"],
            {"type": type(exc).__name__, "message": str(exc)},
        )
        if _is_transport_failure(exc):
            record_reachability_quietly(connection, project_id, server, reachable=False,
                                        error=f"{type(exc).__name__}: {exc}")
        raise
    receipts.mark_succeeded(
        connection,
        receipt["id"],
        f"{server['id']}:{tool_name}",
        output if isinstance(output, dict) else {"value": output},
    )
    return {
        "output": output,
        "receipt_id": receipt["id"],
        "actual_cost_usd_micros": 0,
    }


def _is_transport_failure(error: BaseException) -> bool:
    try:
        import httpx

        return isinstance(error, (httpx.TransportError,))
    except Exception:  # noqa: BLE001
        return False
