"""A skill as a thing an operator can author, attach and reuse.

WHY THIS EXISTS
---------------
`project_skills` recorded that a project MAY use a skill; it never said what the skill DOES.
Behaviour lived in a Python executor registered inside BOSS, so every capability a customer
wanted was a pull request against the product. That is the wrong boundary for the thing most
worth customising: the operator knows their house rules, their tooling and their accounts.

A skill authored here is attachable at both ends: `role_skills` for an agent (the grant the
runtime checks — see services/agents.py for the one agent model) and
`workflow_custom_steps.skill_id` for a step.

THE THREE BEHAVIOURS, AND WHY THERE ARE EXACTLY THREE
-----------------------------------------------------
* ``builtin`` — names an executor BOSS already ships. Keeps every existing skill working and
  lets one be given knowledge and a risk tier without touching its code.
* ``mcp_tool`` — anything reachable over a protocol BOSS already speaks. This is the answer for
  third-party accounts: an MCP server holds the integration, the skill names one of its tools.
* ``sandboxed_command`` — a declared command in an isolated sandbox. This is what makes ffmpeg,
  pandoc, ImageMagick and a hundred others authorable without writing Python.

An "integration" is deliberately not a fourth kind, for the reason `workflow_custom_steps`
already states: in BOSS a connection grants authority to a skill, it is not something you can
invoke on its own.

WHAT IS REFUSED, AND WHY IT IS REFUSED HERE
--------------------------------------------
A `sandboxed_command` is operator-authored code that runs. Left unconstrained it is remote code
execution with extra steps, so the constraints are structural rather than advisory: the image is
pinned by digest, the command is a template whose inputs are shell-quoted at build time and
never concatenated, the network is off unless declared, a timeout is mandatory, and the whole
kind refuses to execute at all unless the deployment has real isolation configured. It fails
closed with a sentence, which is the only acceptable behaviour for the alternative — running a
customer's command on the control plane's own host.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

#: At or above this tier a step must be approved by a person. The same floor every capability
#: obeys — imported rather than restated so the two cannot drift.
from .workflow_capabilities import MANUAL_APPROVAL_RISK_FLOOR

BEHAVIOURS = ("builtin", "mcp_tool", "sandboxed_command", "agent_program")

#: The fourth behaviour, and the only one no person authored.
#:
#: An agent writes a program mid-run to do something no skill it holds covers, and if it worked,
#: the workflow KEEPS it. The next run of that workflow is offered the program as a tool, so the
#: customer's second project reuses what the first one produced instead of paying a model to
#: rediscover it. That is the difference between an agent that improvises every time and one that
#: accumulates.
#:
#: It is not a `sandboxed_command` wearing a different name. That kind demands a digest-pinned
#: image because a person chose it; this one runs in the deployment's own container boundary,
#: which is the only place BOSS will run code it did not ship. It carries the risk tier of the
#: thing that wrote it, which is to say it needs a person until somebody says otherwise.
AGENT_PROGRAM_LANGUAGES = ("python", "bash", "sh")

#: A digest-pinned image. A floating tag means the thing that ran last week is not the thing
#: that runs today, which is not a sandbox, it is a moving target with a sandbox around it.
_DIGEST = re.compile(r"^[a-z0-9][\w./-]*@sha256:[0-9a-f]{64}$")
_KEY = re.compile(r"^[a-z][a-z0-9_]{2,48}$")


class SkillDefinitionError(ValueError):
    """Something the operator can fix, phrased for them."""


class SkillExistsError(SkillDefinitionError):
    """A create named a key that is already taken. Its own class so the API can answer 409."""


class SkillInUseError(SkillDefinitionError):
    """A delete would leave something pointing at nothing. The API answers 409."""


#: How a save says what it means. See save_skill.
SAVE_MODES = ("create", "update", "upsert")


def _clean(value: Any, field: str, *, limit: int = 400) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        raise SkillDefinitionError(f"{field} is required.")
    return text_value[:limit]


def _whole_number(value: Any, default: int, field: str) -> int:
    """An integer field, refused with a sentence rather than escaping as a 500.

    `int("five minutes")` raised a bare ValueError, which the API's SkillDefinitionError handler
    did not catch, so a typo in a timeout came back as an Internal Server Error.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise SkillDefinitionError(f"{field} must be a whole number.")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SkillDefinitionError(f"{field} must be a whole number — got {value!r}.") from None


def _named_list(raw: Any, field: str) -> list[dict[str, Any]]:
    """`inputs`/`produces` as [{name, required, description}] — names only, validated."""
    out: list[dict[str, Any]] = []
    for entry in raw or []:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            raise SkillDefinitionError(f"each {field} entry must be a name or an object.")
        name = str(entry.get("name") or "").strip()
        if not _KEY.match(name):
            raise SkillDefinitionError(
                f"{field} name {name!r} must be lower_snake_case, 3-49 characters.")
        out.append({
            "name": name,
            "required": bool(entry.get("required", True)),
            "description": str(entry.get("description") or "").strip()[:200],
        })
    names = [e["name"] for e in out]
    if len(set(names)) != len(names):
        raise SkillDefinitionError(f"two {field} entries share a name.")
    return out


def validate_config(behaviour: str, config: dict[str, Any], inputs: Iterable[str]) -> dict[str, Any]:
    """The per-behaviour settings, checked before anything is stored.

    Rejecting at authoring time rather than at 2am in the middle of a run is the whole point of
    having a definition at all.
    """
    given = dict(config or {})
    if behaviour == "builtin":
        return {"executor_id": _clean(given.get("executor_id"), "executor_id", limit=120)}

    if behaviour == "mcp_tool":
        return {
            "mcp_server_id": _clean(given.get("mcp_server_id"), "mcp_server_id", limit=64),
            "tool_name": _clean(given.get("tool_name"), "tool_name", limit=120),
        }

    if behaviour == "agent_program":
        language = str(given.get("language") or "python").strip().lower()
        if language not in AGENT_PROGRAM_LANGUAGES:
            raise SkillDefinitionError(
                f"language must be one of {', '.join(AGENT_PROGRAM_LANGUAGES)} — got "
                f"{language!r}.")
        source = str(given.get("source") or "")
        if not source.strip():
            raise SkillDefinitionError("source is required — the program to run.")
        if len(source) > 60_000:
            raise SkillDefinitionError(
                "the program is over 60,000 characters. Something that big is a repository, not "
                "a step.")
        timeout = _whole_number(given.get("timeout_seconds"), 60, "timeout_seconds")
        if not 5 <= timeout <= 300:
            raise SkillDefinitionError("timeout_seconds must be between 5 and 300.")
        return {
            "language": language,
            "source": source,
            "timeout_seconds": timeout,
            # PROVENANCE, because this is the one kind where "who wrote this" is not obvious from
            # the fact that it exists. An operator looking at a skill an agent invented needs to
            # know which agent, on which workflow, and on which run.
            "written_by": str(given.get("written_by") or "").strip()[:120],
            "written_for": str(given.get("written_for") or "").strip()[:120],
            "written_in_run": str(given.get("written_in_run") or "").strip()[:120],
        }

    # sandboxed_command
    image = _clean(given.get("image"), "image", limit=300)
    if not _DIGEST.match(image):
        raise SkillDefinitionError(
            "image must be pinned by digest, e.g. "
            "ghcr.io/acme/ffmpeg@sha256:<64 hex>. A floating tag means the thing that ran "
            "last week is not the thing that runs today.")
    command = _clean(given.get("command"), "command", limit=2000)
    # EVERY PLACEHOLDER MUST BE A DECLARED INPUT. A template referring to something the skill
    # never asks for fails at run time with an empty substitution — silently producing the wrong
    # output, which is worse than refusing.
    declared = set(inputs)
    used = set(re.findall(r"\{([a-z][a-z0-9_]*)\}", command))
    unknown = sorted(used - declared - {"workdir"})
    if unknown:
        raise SkillDefinitionError(
            f"the command uses {', '.join(unknown)}, which this skill does not take as input.")
    timeout = _whole_number(given.get("timeout_seconds"), 300, "timeout_seconds")
    if not 5 <= timeout <= 3600:
        raise SkillDefinitionError("timeout_seconds must be between 5 and 3600.")
    outputs = _named_list(given.get("outputs"), "outputs")
    return {
        "image": image,
        "command": command,
        "timeout_seconds": timeout,
        # OFF UNLESS ASKED FOR. A command that can reach the internet can exfiltrate whatever
        # it was given, and most media work needs no network at all.
        "network": bool(given.get("network", False)),
        "outputs": outputs,
    }


def _row(row: Any) -> dict[str, Any]:
    def loads(value: Any, fallback: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return fallback
        return value if value is not None else fallback

    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "skill_key": row["skill_key"],
        "label": row["label"],
        "summary": row["summary"],
        "behaviour_kind": row["behaviour_kind"],
        "knowledge": row["knowledge"],
        "inputs": loads(row["inputs"], []),
        "produces": loads(row["produces"], []),
        "config": loads(row["config"], {}),
        "risk_tier": int(row["risk_tier"]),
        "trusted": bool(row["trusted"]),
        "enabled": bool(row["enabled"]),
        "requires_approval": int(row["risk_tier"]) >= MANUAL_APPROVAL_RISK_FLOOR,
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def list_skills(
    connection: Connection, project_id: str, *, limit: int = 200,
    with_references: bool = False,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            "SELECT * FROM skill_definitions WHERE project_id = :p "
            "ORDER BY enabled DESC, skill_key LIMIT :lim"
        ),
        {"p": project_id, "lim": int(limit)},
    ).mappings().all()
    items = [_row(r) for r in rows]
    if with_references and items:
        # One read for the whole page. The Skills page shows each skill's uses beside its
        # Delete button, so the operator knows before pressing it what deleting would break.
        refs = _references_by_key(connection, project_id)
        for item in items:
            item["references"] = refs.get(item["skill_key"], _no_references())
    return items


def _no_references() -> dict[str, list]:
    return {"custom_steps": [], "agents": [], "workflows": []}


def _references_by_key(connection: Connection, project_id: str) -> dict[str, dict[str, list]]:
    refs: dict[str, dict[str, list]] = {}
    for row in connection.execute(
        text("SELECT skill_id, step_key, label, enabled FROM workflow_custom_steps "
             "WHERE project_id = :p AND binding_kind = 'skill' ORDER BY step_key"),
        {"p": project_id},
    ).mappings().all():
        refs.setdefault(str(row["skill_id"]), _no_references())[
            "custom_steps"].append({"step_key": row["step_key"], "label": row["label"],
                                    "enabled": bool(row["enabled"])})
    for row in connection.execute(
        text("SELECT skill_id, role_key FROM role_skills WHERE project_id = :p "
             "ORDER BY role_key"),
        {"p": project_id},
    ).mappings().all():
        refs.setdefault(str(row["skill_id"]), _no_references())[
            "agents"].append(str(row["role_key"]))
    # A WORKFLOW THAT BINDS THE SKILL DIRECTLY, with a `kind: skill` step
    # (workflows._validate_bindings reads binding.skill_id). Round-1 review: the check read
    # custom steps and agents only, so deleting a skill bound this way passed and the workflow
    # then failed at its next publish or run. Read from each live workflow's ACTIVE version —
    # the one a run would start from; a retired definition starts nothing.
    for row in connection.execute(
        text("SELECT DISTINCT step->'binding'->>'skill_id' AS skill_id, d.workflow_key, d.name "
             "FROM workflow_definitions d "
             "JOIN workflow_versions v ON v.id = d.active_version_id "
             "CROSS JOIN LATERAL jsonb_array_elements("
             "  CASE WHEN jsonb_typeof(v.spec->'steps') = 'array' THEN v.spec->'steps' "
             "  ELSE '[]'::jsonb END) AS step "
             "WHERE d.project_id = :p AND d.status <> 'retired' AND step->>'kind' = 'skill' "
             "ORDER BY d.workflow_key"),
        {"p": project_id},
    ).mappings().all():
        if not row["skill_id"]:
            continue
        refs.setdefault(str(row["skill_id"]), _no_references())[
            "workflows"].append({"workflow_key": str(row["workflow_key"]),
                                 "name": str(row["name"] or row["workflow_key"])})
    return refs


def skill_references(connection: Connection, project_id: str, skill_key: str) -> dict[str, list]:
    """What points at one skill: the custom steps that run it, the workflows whose steps bind
    it directly (`kind: skill`), and the agents that hold it."""
    return _references_by_key(connection, project_id).get(
        skill_key, _no_references())


def get_skill(connection: Connection, project_id: str, skill_key: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT * FROM skill_definitions WHERE project_id = :p AND skill_key = :k"),
        {"p": project_id, "k": skill_key},
    ).mappings().first()
    return _row(row) if row else None


def _shadows_a_builtin(connection: Connection, project_id: str, skill_key: str) -> bool:
    """Would authoring this key hide a skill BOSS ships?

    Asked of the entitlements catalogue rather than project_skills: a shipped skill needs no
    row there to exist, which is the same mistake `_skill_exists` was first written with.
    """
    try:
        from ..config import get_settings
        from .entitlements import EntitlementBrokerService

        # include_authored=False: the catalogue now carries authored skills too, and asking it
        # with them in would refuse every EDIT of an authored skill as shadowing itself.
        catalogue = EntitlementBrokerService(get_settings()).list_skills(
            project_id, connection=connection, include_authored=False)
        return any(str(item.get("id")) == skill_key for item in catalogue)
    except Exception:  # noqa: BLE001
        # A catalogue that cannot be read must not block authoring — the collision is rare and
        # the refusal would be unexplainable.
        return False


def _resolve_mcp_binding(
    connection: Connection, project_id: str, config: dict[str, Any],
) -> dict[str, Any]:
    """Point an mcp_tool skill at a server that EXISTS, and at a tool it has, when that is known.

    The form used to ask for a free-text "MCP server id" that no screen displayed, and nothing
    checked it — so a typo saved fine and the skill failed on its first run with a lookup
    error. The server may be named by id or by name (what an operator and a chat model actually
    know); the stored value is always the id, because names can be edited.

    The tool is checked against the server's last-seen tool list only when the server was
    reachable at that check. A server that has never been reached says nothing about its tools,
    and refusing then would make an mcp_tool skill impossible to author before its server is up.
    """
    wanted = str(config.get("mcp_server_id") or "").strip()
    rows = connection.execute(
        text("SELECT id, name, reachable, tools FROM mcp_servers WHERE project_id = :p "
             "ORDER BY name"),
        {"p": project_id},
    ).mappings().all()
    server = next((r for r in rows if r["id"] == wanted), None) or next(
        (r for r in rows if str(r["name"]).strip().lower() == wanted.lower()), None)
    if server is None:
        known = ", ".join(f"{r['name']} ({r['id']})" for r in rows) or "none registered yet"
        raise SkillDefinitionError(
            f"there is no MCP server {wanted!r} in this project. Registered servers: {known}. "
            "Register the server on the Skills and MCP page first.")
    tool_name = str(config.get("tool_name") or "").strip()
    tools = server["tools"] if isinstance(server["tools"], list) else []
    names = [str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name")]
    if server["reachable"] is True and names and tool_name not in names:
        raise SkillDefinitionError(
            f"MCP server '{server['name']}' has no tool {tool_name!r}. Its tools are: "
            f"{', '.join(sorted(names))}.")
    return {**config, "mcp_server_id": str(server["id"]), "tool_name": tool_name}


def save_skill(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    skill_key: str,
    label: str,
    behaviour_kind: str,
    summary: str = "",
    knowledge: str = "",
    inputs: Any = (),
    produces: Any = (),
    config: Optional[dict[str, Any]] = None,
    risk_tier: int = 1,
    trusted: bool = False,
    enabled: bool = True,
    mode: str = "upsert",
) -> dict[str, Any]:
    """Create or replace one skill.

    `mode` says which the caller MEANT, because the two look identical on the wire and are not:
    ``create`` refuses a key that already exists, ``update`` refuses one that does not, and
    ``upsert`` (the original behaviour, kept for scripts that rely on it) does either.

    WHY THE DISTINCTION. The Skills page's form header switched to "Edit <key> by hand" as soon
    as a key was typed, and saving silently replaced whatever skill already had that key — an
    operator writing a new skill whose key happened to match an old one overwrote the old one,
    knowledge body and all, with no warning. Creating and editing are different intentions and
    the store now asks which one it is being given.
    """
    key = str(skill_key or "").strip().lower()
    if not _KEY.match(key):
        raise SkillDefinitionError(
            "skill_key must be lower_snake_case, 3-49 characters — it is an identifier other "
            "objects point at, so it has to be stable and typeable.")
    behaviour = str(behaviour_kind or "").strip().lower()
    if behaviour not in BEHAVIOURS:
        raise SkillDefinitionError(f"behaviour_kind must be one of: {', '.join(BEHAVIOURS)}.")
    intent = str(mode or "upsert").strip().lower()
    if intent not in SAVE_MODES:
        raise SkillDefinitionError(f"mode must be one of: {', '.join(SAVE_MODES)}.")
    exists = connection.execute(
        text("SELECT 1 FROM skill_definitions WHERE project_id = :p AND skill_key = :k"),
        {"p": project_id, "k": key},
    ).first() is not None
    if intent == "create" and exists:
        raise SkillExistsError(
            f"a skill called {key!r} already exists in this project. Open it with Edit to "
            "change it, or pick another key for the new one.")
    if intent == "update" and not exists:
        raise SkillDefinitionError(
            f"there is no skill {key!r} to update in this project — it may have been deleted. "
            "Save it as a new skill instead.")

    # AN AUTHORED SKILL MAY NOT TAKE A BUILT-IN'S NAME.
    #
    # `get_skill` reads skill_definitions and is consulted first everywhere, so a project skill
    # sharing a key with one BOSS ships makes the built-in UNREACHABLE — and silently. A real
    # case: an authored `publish_youtube` (tier 3, untrusted, pointing at an MCP server that
    # did not exist) shadowed the shipped one, so every workflow naming publish_youtube failed
    # validation with "only a reviewed skill can be given that much reach" — about a skill that
    # IS reviewed and ships with the product. It cost two debugging passes and looked, from the
    # outside, like the agent being unable to create a workflow.
    #
    # Refused at authoring time, where the operator can pick another name, rather than
    # discovered later as an unexplainable refusal.
    if _shadows_a_builtin(connection, project_id, key):
        raise SkillDefinitionError(
            f"{key!r} is the name of a skill BOSS already ships. Pick another key — an "
            "authored skill with a built-in's name hides the built-in everywhere, and nothing "
            "downstream can tell which one it was supposed to run.")
    tier = _whole_number(risk_tier, 1, "risk_tier")
    if not 0 <= tier <= 3:
        raise SkillDefinitionError("risk_tier must be between 0 and 3.")
    parsed_inputs = _named_list(inputs, "inputs")
    parsed_produces = _named_list(produces, "produces")
    parsed_config = validate_config(behaviour, config or {},
                                    [entry["name"] for entry in parsed_inputs])
    if behaviour == "mcp_tool":
        parsed_config = _resolve_mcp_binding(connection, project_id, parsed_config)
    # A sandboxed command's declared outputs ARE what it produces; asking an operator to list
    # the same names twice is a way to have them disagree.
    if behaviour == "sandboxed_command" and not parsed_produces:
        parsed_produces = parsed_config.get("outputs") or []

    row = connection.execute(
        text(
            """
            INSERT INTO skill_definitions (
                id, project_id, skill_key, label, summary, behaviour_kind, knowledge,
                inputs, produces, config, risk_tier, trusted, enabled, created_by
            ) VALUES (
                :id, :p, :k, :label, :summary, :behaviour, :knowledge,
                CAST(:inputs AS JSONB), CAST(:produces AS JSONB), CAST(:config AS JSONB),
                :tier, :trusted, :enabled, :actor
            )
            ON CONFLICT (project_id, skill_key) DO UPDATE SET
                label = EXCLUDED.label, summary = EXCLUDED.summary,
                behaviour_kind = EXCLUDED.behaviour_kind, knowledge = EXCLUDED.knowledge,
                inputs = EXCLUDED.inputs, produces = EXCLUDED.produces,
                config = EXCLUDED.config, risk_tier = EXCLUDED.risk_tier,
                trusted = EXCLUDED.trusted, enabled = EXCLUDED.enabled,
                updated_at = now()
            RETURNING *
            """
        ),
        {"id": make_id("skd"), "p": project_id, "k": key, "label": _clean(label, "label", limit=120),
         "summary": str(summary or "").strip()[:400], "behaviour": behaviour,
         "knowledge": str(knowledge or "")[:20000],
         "inputs": json.dumps(parsed_inputs), "produces": json.dumps(parsed_produces),
         "config": json.dumps(parsed_config), "tier": tier, "trusted": bool(trusted),
         "enabled": bool(enabled), "actor": actor},
    ).mappings().one()
    item = _row(row)
    # WHAT WAS AUTHORED, never the knowledge body — it can hold a customer's internal doc.
    emit_event(connection, project_id, "skill.authored", actor, "skill_definition", item["id"],
               {"skill_key": key, "behaviour": behaviour, "risk_tier": tier,
                "trusted": bool(trusted)})
    return item


def set_skill_enabled(
    connection: Connection, *, project_id: str, actor: str, skill_key: str, enabled: bool,
) -> dict[str, Any]:
    """Turn an authored skill on or off without touching anything else about it.

    The delete refusal has always said "disable the skill instead", and there was no way to:
    no control on the page, and no API short of re-saving every field with enabled=false.
    """
    row = connection.execute(
        text("UPDATE skill_definitions SET enabled = :e, updated_at = now() "
             "WHERE project_id = :p AND skill_key = :k RETURNING *"),
        {"e": bool(enabled), "p": project_id, "k": skill_key},
    ).mappings().first()
    if row is None:
        raise SkillDefinitionError(f"no skill {skill_key!r} in this project.")
    item = _row(row)
    emit_event(connection, project_id, "skill.enabled" if enabled else "skill.disabled", actor,
               "skill_definition", item["id"], {"skill_key": skill_key})
    return item


def delete_skill(
    connection: Connection, *, project_id: str, actor: str, skill_key: str,
    release_grants: bool = False,
) -> dict[str, Any]:
    """Remove a skill, refusing while anything still points at it.

    A dangling reference does not fail loudly — `validate_custom_step` reports "this project
    does not have that skill", which reads as a mistake in the WORKFLOW rather than as somebody
    having deleted the skill out from under it.

    TWO KINDS OF REFERENCE, TREATED DIFFERENTLY ON PURPOSE.

    * A custom step that runs the skill (enabled or not) always refuses the delete. Deleting
      would break every workflow placing that step, and a disabled step is one somebody means
      to turn back on.
    * An agent holding the skill refuses UNLESS the caller says `release_grants`. Taking a skill
      off an agent breaks nothing that is published, so it is a choice the operator can make in
      the same breath — but a choice, made on purpose. Before this, deleting left the grant
      behind and the agent page showed "no such skill here any more" forever.
    """
    refs = skill_references(connection, project_id, skill_key)
    if refs["custom_steps"]:
        steps = ", ".join(s["label"] or s["step_key"] for s in refs["custom_steps"][:8])
        raise SkillInUseError(
            f"{skill_key} is run by these workflow steps: {steps}. Delete or re-point those "
            "steps first — or turn the skill off instead of deleting it.")
    if refs["workflows"]:
        flows = ", ".join(w["name"] for w in refs["workflows"][:8])
        raise SkillInUseError(
            f"{skill_key} is a step in these workflows: {flows}. Remove it from those "
            "workflows (or retire them) first — or turn the skill off instead of deleting it.")
    if refs["agents"] and not release_grants:
        raise SkillInUseError(
            f"{skill_key} is held by {', '.join(refs['agents'])}. Deleting it takes it off "
            f"{'that agent' if len(refs['agents']) == 1 else 'those agents'}; confirm to go "
            "ahead, or turn the skill off instead.")
    removed = connection.execute(
        text("DELETE FROM skill_definitions WHERE project_id = :p AND skill_key = :k "
             "RETURNING id"),
        {"p": project_id, "k": skill_key},
    ).first()
    if not removed:
        raise SkillDefinitionError(f"no skill {skill_key!r} in this project.")
    for role_key in refs["agents"]:
        unassign_skill_from_role(connection, project_id=project_id, actor=actor,
                                 role_key=role_key, skill_id=skill_key)
    emit_event(connection, project_id, "skill.deleted", actor, "skill_definition",
               str(removed[0]), {"skill_key": skill_key, "released_from": refs["agents"]})
    return {"deleted": skill_key, "released_from": refs["agents"]}


# ---------------------------------------------------------------------------
# Assigning a skill to an agent
# ---------------------------------------------------------------------------
#
# A skill attaches at two independent ends: a workflow step RUNS it, and an agent HOLDS it.
# The step end already existed (`workflow_custom_steps.skill_id`). This is the agent end.
#
# Independent on purpose. The same skill can be given to an agent, placed in a step, both, or
# neither — because those answer different questions. A step in a workflow is "this happens at
# this point in the path"; an agent holding a skill is "this member of the workforce can do
# this when its own work calls for it", which is what an admin-style agent owning a YouTube
# account actually needs. Forcing a choice between them would make one of those impossible.

#: Both kinds of skill an agent can be given. `authored` lives in skill_definitions and was
#: written in this project; `builtin` ships with BOSS and lives in project_skills.
ASSIGNABLE_SOURCES = ("authored", "builtin")


def _skill_exists(connection: Connection, project_id: str, skill_id: str) -> Optional[str]:
    """Return the source a skill exists under, or None. Authored wins if somehow both.

    The built-in half deliberately asks the ENTITLEMENTS CATALOGUE rather than reading
    `project_skills`. That table records a per-project override — enabled, on-demand, config —
    and a skill BOSS ships needs no row there to exist or to work. Checking the table instead
    rejected every one of the seventeen shipped skills as "no skill in this project", which is
    exactly the population this feature was built to let an operator assign.
    """
    # THE ONE CATALOGUE (skill_catalogue), so "can this be assigned" and "will this run" are
    # the same question. A `registered` project_skills row is reported as builtin, which is the
    # source vocabulary role_skills has always used for anything not authored here.
    from .skill_catalogue import entry

    found = entry(connection, project_id, skill_id)
    if found is None or found["source"] == "registered":
        # A registered row with no spec is something a custom step can bind but no agent can
        # be offered as a tool, so it is not assignable.
        return None
    return "authored" if found["source"] == "authored" else "builtin"


def assign_skill_to_role(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    role_key: str,
    skill_id: str,
) -> dict[str, Any]:
    """Give one skill to one agent. Idempotent: assigning twice is not an error."""
    role = str(role_key or "").strip().lower()
    skill = str(skill_id or "").strip()
    if not role:
        raise SkillDefinitionError("role_key is required.")
    if not skill:
        raise SkillDefinitionError("skill_id is required.")

    known = connection.execute(
        text("SELECT 1 FROM roles WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role},
    ).first()
    if not known:
        # NAME WHAT EXISTS. "Unknown role" sends the reader hunting for a typo; the list tells
        # them whether they are even in the right project.
        available = connection.execute(
            text("SELECT role_key FROM roles WHERE project_id = :p ORDER BY role_key LIMIT 40"),
            {"p": project_id},
        ).scalars().all()
        raise SkillDefinitionError(
            f"no agent {role!r} on this project. Available: {', '.join(available) or 'none'}.")

    source = _skill_exists(connection, project_id, skill)
    if source is None:
        raise SkillDefinitionError(
            f"no skill {skill!r} in this project — author one, or enable a built-in first.")

    row = connection.execute(
        text(
            "INSERT INTO role_skills (id, project_id, role_key, skill_id, source, assigned_by) "
            "VALUES (:id, :p, :r, :k, :src, :by) "
            "ON CONFLICT (project_id, role_key, skill_id) DO UPDATE SET source = EXCLUDED.source "
            "RETURNING id, created_at"
        ),
        {"id": make_id("rsk"), "p": project_id, "r": role, "k": skill,
         "src": source, "by": actor},
    ).mappings().first()
    # GIVEN BACK BY HAND, so a seed exclusion from an earlier removal no longer applies: the
    # newer decision wins. (sync_role_skills never reaches here for an excluded grant.)
    from . import seed_exclusions

    seed_exclusions.lift(connection, project_id, "role_skill",
                         seed_exclusions.role_skill_subject(role, skill))
    emit_event(connection, project_id, "skill.assigned_to_agent", actor, "role_skill",
               str(row["id"]), {"role_key": role, "skill_id": skill, "source": source})
    # A NEW GRANT IS A NEW VERSION of the agent (agent_identity; once per request when batched).
    from .agent_identity import quietly, seal_identity

    quietly(connection, seal_identity, project_id, role, "grant", actor)
    # SAY NOW WHAT THE RUN WOULD SAY LATER. A skill that calls an MCP tool runs only for an
    # agent that also holds the server (agents.mcp_reach_gap). The grant is kept — a person
    # may be about to give the server next — but the answer names the missing half.
    from .agents import mcp_reach_gap

    gap = mcp_reach_gap(connection, project_id, role, skill)
    return {"role_key": role, "skill_id": skill, "source": source, "assigned": True,
            **({"needs_mcp_server": {k: gap[k] for k in ("mcp_server_id", "server", "tool")},
                "warning": gap["message"]} if gap else {})}


def sync_role_skills(
    connection: Connection, *, project_id: str, actor: str = "system",
) -> dict[str, Any]:
    """Give every installed agent the skills its role spec says it may use.

    WHY THIS HAD TO EXIST. A role spec declares `permissions.allowed_skills`, and until now
    that list went exactly one place: into the agent's PROMPT, as a line reading "Role allowed
    skills: …". The runtime does not read prompts. It reads `role_skills` — see
    skill_runtime._assigned_to_agent, whose own comment says "GIVING A SKILL TO AN AGENT IS THE
    GRANT, and this is where it is cashed in."

    Nothing connected the two. So every install had agents that were TOLD what they could use
    and HELD nothing, and every permission that worked did so because a skill happened to list
    the role in `allowed_callers` — never because an agent had been given anything. An operator
    looking at the Agents page saw exactly what was there: agents with no skills.

    Idempotent, and additive only. It never removes an assignment: an operator who has granted
    something by hand has made a decision, and a sync that reverts it on the next run is a sync
    nobody can trust. Taking a skill away stays deliberate — that is `unassign_skill_from_role`.

    A skill a role names but the project does not have is REPORTED, not silently skipped. It is
    usually the real problem: the bundle it lives in was never enabled here.
    """
    # WHAT AN OPERATOR TOOK OFF AN AGENT STAYS OFF. This sync runs on every install and
    # upgrade; it re-granted every unassigned skill its role spec names, so a removal lasted
    # until the next deploy (boss-1: 48 assignment events for 16 grants).
    from . import seed_exclusions

    removed = seed_exclusions.excluded(connection, project_id, "role_skill")
    kept_off: list[dict[str, str]] = []
    roles = connection.execute(
        text("""SELECT DISTINCT ON (role_key) role_key, spec FROM roles
                WHERE project_id = :p ORDER BY role_key, created_at DESC, id DESC"""),
        {"p": project_id},
    ).mappings().all()

    # A GRANT ALREADY HELD IS LEFT ALONE: re-assigning it wrote a `skill.assigned_to_agent`
    # event per grant per run, and this now runs on every boot for every project.
    held = {(str(r["role_key"]), str(r["skill_id"])) for r in connection.execute(
        text("SELECT role_key, skill_id FROM role_skills WHERE project_id = :p"),
        {"p": project_id}).mappings()}
    assigned: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    for row in roles:
        spec = row["spec"] if isinstance(row["spec"], dict) else {}
        wanted = ((spec.get("permissions") or {}).get("allowed_skills")) or []
        for skill_id in wanted:
            skill_id = str(skill_id).strip()
            if (str(row["role_key"]), skill_id) in held:
                continue
            # A wildcard is a description, not a grant. "infra.*" cannot be cashed in, and
            # expanding it here would hand an agent whatever happens to match it next week.
            if not skill_id or "*" in skill_id:
                continue
            if seed_exclusions.role_skill_subject(row["role_key"], skill_id) in removed:
                kept_off.append({"role_key": row["role_key"], "skill_id": skill_id})
                continue
            try:
                assign_skill_to_role(
                    connection, project_id=project_id, actor=actor,
                    role_key=str(row["role_key"]), skill_id=skill_id)
                assigned.append({"role_key": row["role_key"], "skill_id": skill_id})
            except SkillDefinitionError as error:
                missing.append({"role_key": row["role_key"], "skill_id": skill_id,
                                "reason": str(error)})
    return {"assigned": assigned, "assigned_count": len(assigned),
            "unavailable": missing, "unavailable_count": len(missing),
            "kept_off_by_operator": kept_off}


def unassign_skill_from_role(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    role_key: str,
    skill_id: str,
) -> dict[str, Any]:
    """Take one skill back off one agent. Does not touch the skill or any workflow step."""
    role = str(role_key or "").strip().lower()
    removed = connection.execute(
        text("DELETE FROM role_skills WHERE project_id = :p AND role_key = :r "
             "AND skill_id = :k RETURNING id"),
        {"p": project_id, "r": role, "k": skill_id},
    ).first()
    if not removed:
        raise SkillDefinitionError(f"{role} does not hold {skill_id!r}.")
    # A REMOVAL THE NEXT SEED MUST NOT UNDO. See services/seed_exclusions.py.
    from . import seed_exclusions

    seed_exclusions.exclude(connection, project_id, "role_skill",
                            seed_exclusions.role_skill_subject(role, skill_id), actor=actor,
                            reason="taken off the agent")
    emit_event(connection, project_id, "skill.unassigned_from_agent", actor, "role_skill",
               str(removed[0]), {"role_key": role, "skill_id": skill_id})
    from .agent_identity import quietly, seal_identity

    quietly(connection, seal_identity, project_id, role, "revoke", actor)
    return {"role_key": role, "skill_id": skill_id, "assigned": False}


def list_role_skills(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every assignment in the project, so one read can render the whole page."""
    rows = connection.execute(
        text(
            "SELECT role_key, skill_id, source, assigned_by, created_at FROM role_skills "
            "WHERE project_id = :p ORDER BY role_key, skill_id"
        ),
        {"p": project_id},
    ).mappings().all()
    return [
        {
            "role_key": r["role_key"],
            "skill_id": r["skill_id"],
            "source": r["source"],
            "assigned_by": r["assigned_by"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


def skills_for_role(connection: Connection, project_id: str, role_key: str) -> list[str]:
    """The skill ids one agent holds. What the agent's own prompt assembly reads."""
    return list(
        connection.execute(
            text("SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :r "
                 "ORDER BY skill_id"),
            {"p": project_id, "r": str(role_key or "").strip().lower()},
        ).scalars().all()
    )


def assignable_catalogue(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every skill this project could give an agent, authored and built-in, in one list.

    WHY IT IS ONE LIST. An operator opening an agent asks "what can this one do, and what else
    could it do" — not "which of two catalogues is this skill in". The Skills page grew the
    split because authoring and enabling are different acts; on an agent they are the same act,
    so the divide is noise. `source` is kept so the page can say where a skill came from.

    Deliberately includes skills that are not enabled, marked so. A skill an operator has
    authored but not enabled is exactly the one they are about to enable, and omitting it from
    the picker reads as "BOSS lost it".
    """
    from .skill_catalogue import entries

    items: list[dict[str, Any]] = []
    try:
        catalogue = entries(connection, project_id)
    except Exception:  # noqa: BLE001
        # A broken spec pack must not empty the agent page. The authored half still renders,
        # and a missing built-in shows up as "cannot be assigned" at the point of assignment,
        # with a sentence, rather than as a page that will not load.
        catalogue = {
            a["skill_key"]: {"skill_id": a["skill_key"], "label": a["label"],
                             "summary": a["summary"], "source": "authored",
                             "behaviour_kind": a["behaviour_kind"], "risk_tier": a["risk_tier"],
                             "enabled": a["enabled"]}
            for a in list_skills(connection, project_id)
        }
    for item in catalogue.values():
        authored = item["source"] == "authored"
        if item["source"] == "registered":
            # A bare registration with no spec cannot be offered to an agent as a tool, so it
            # is not offered to the agent page either — the same rule as `_skill_exists`.
            continue
        items.append({
            "skill_id": item["skill_id"],
            "label": item["label"] if authored
                     else item["skill_id"].replace("_", " ").replace(".", " · "),
            "summary": item["summary"],
            # role_skills' vocabulary: anything not written here ships with BOSS.
            "source": "authored" if authored else "builtin",
            "behaviour_kind": item["behaviour_kind"] if authored
                              else str((item.get("spec") or {}).get("kind") or item["kind"]),
            "risk_tier": item["risk_tier"],
            "enabled": item["enabled"],
        })
    return sorted(items, key=lambda s: s["skill_id"])


def role_skill_detail(connection: Connection, project_id: str, role_key: str) -> dict[str, Any]:
    """What one agent holds, and what else it could be given.

    THE AGENT'S OWN VIEW, and the one that was missing. `role_skills` has always been
    many-to-many — an agent holds many skills, a skill is held by many agents — but the only way
    to write it was from the skill's side, one skill at a time. Someone building a team thinks
    the other way round: here is my editor, here is what it can do.

    `holds` and `available` are disjoint, so the page never offers a skill the agent already has.
    """
    role = str(role_key or "").strip().lower()
    held = {
        str(r["skill_id"]): r
        for r in connection.execute(
            text("SELECT skill_id, source, assigned_by, created_at FROM role_skills "
                 "WHERE project_id = :p AND role_key = :r"),
            {"p": project_id, "r": role},
        ).mappings().all()
    }
    catalogue = assignable_catalogue(connection, project_id)
    known = {s["skill_id"]: s for s in catalogue}

    holds: list[dict[str, Any]] = []
    for skill_id, row in sorted(held.items()):
        entry = dict(known.get(skill_id) or {
            # HELD BUT NOT IN THE CATALOGUE. A skill removed from a spec pack, or authored and
            # then deleted, leaves the grant behind. Shown as the problem it is rather than
            # dropped, because a grant nothing can execute is worth seeing.
            "skill_id": skill_id, "label": skill_id, "summary": "",
            "source": str(row["source"] or ""), "behaviour_kind": "", "risk_tier": 0,
            "enabled": False,
        })
        entry["missing"] = skill_id not in known
        entry["assigned_by"] = row["assigned_by"]
        entry["assigned_at"] = row["created_at"].isoformat() if row["created_at"] else None
        holds.append(entry)

    return {
        "role_key": role,
        "holds": holds,
        "available": [s for s in catalogue if s["skill_id"] not in held],
    }
