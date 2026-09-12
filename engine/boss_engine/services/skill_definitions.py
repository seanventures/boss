"""A skill as a thing an operator can author, attach and reuse.

WHY THIS EXISTS
---------------
`project_skills` recorded that a project MAY use a skill; it never said what the skill DOES.
Behaviour lived in a Python executor registered inside BOSS, so every capability a customer
wanted was a pull request against the product. That is the wrong boundary for the thing most
worth customising: the operator knows their house rules, their tooling and their accounts.

A skill authored here is attachable at both ends that already existed —
`agent_versions.requested_skills` for an agent, `workflow_custom_steps.skill_id` for a step —
so nothing downstream had to be invented to make one reusable.

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

BEHAVIOURS = ("builtin", "mcp_tool", "sandboxed_command")

#: A digest-pinned image. A floating tag means the thing that ran last week is not the thing
#: that runs today, which is not a sandbox, it is a moving target with a sandbox around it.
_DIGEST = re.compile(r"^[a-z0-9][\w./-]*@sha256:[0-9a-f]{64}$")
_KEY = re.compile(r"^[a-z][a-z0-9_]{2,48}$")


class SkillDefinitionError(ValueError):
    """Something the operator can fix, phrased for them."""


def _clean(value: Any, field: str, *, limit: int = 400) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        raise SkillDefinitionError(f"{field} is required.")
    return text_value[:limit]


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
    timeout = int(given.get("timeout_seconds") or 300)
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


def list_skills(connection: Connection, project_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            "SELECT * FROM skill_definitions WHERE project_id = :p "
            "ORDER BY enabled DESC, skill_key LIMIT :lim"
        ),
        {"p": project_id, "lim": int(limit)},
    ).mappings().all()
    return [_row(r) for r in rows]


def get_skill(connection: Connection, project_id: str, skill_key: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT * FROM skill_definitions WHERE project_id = :p AND skill_key = :k"),
        {"p": project_id, "k": skill_key},
    ).mappings().first()
    return _row(row) if row else None


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
) -> dict[str, Any]:
    """Create or replace one skill. Idempotent on (project, skill_key)."""
    key = str(skill_key or "").strip().lower()
    if not _KEY.match(key):
        raise SkillDefinitionError(
            "skill_key must be lower_snake_case, 3-49 characters — it is an identifier other "
            "objects point at, so it has to be stable and typeable.")
    behaviour = str(behaviour_kind or "").strip().lower()
    if behaviour not in BEHAVIOURS:
        raise SkillDefinitionError(f"behaviour_kind must be one of: {', '.join(BEHAVIOURS)}.")
    tier = int(risk_tier)
    if not 0 <= tier <= 3:
        raise SkillDefinitionError("risk_tier must be between 0 and 3.")
    parsed_inputs = _named_list(inputs, "inputs")
    parsed_produces = _named_list(produces, "produces")
    parsed_config = validate_config(behaviour, config or {},
                                    [entry["name"] for entry in parsed_inputs])
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


def delete_skill(connection: Connection, *, project_id: str, actor: str, skill_key: str) -> dict[str, Any]:
    """Remove a skill, refusing while anything still points at it.

    A dangling reference does not fail loudly — `validate_custom_step` reports "this project
    does not have that skill", which reads as a mistake in the WORKFLOW rather than as somebody
    having deleted the skill out from under it.
    """
    users = connection.execute(
        text(
            "SELECT step_key FROM workflow_custom_steps "
            "WHERE project_id = :p AND skill_id = :k AND enabled LIMIT 5"
        ),
        {"p": project_id, "k": skill_key},
    ).scalars().all()
    if users:
        raise SkillDefinitionError(
            f"{skill_key} is still used by: {', '.join(users)}. Remove those steps first, or "
            "disable the skill instead of deleting it.")
    removed = connection.execute(
        text("DELETE FROM skill_definitions WHERE project_id = :p AND skill_key = :k "
             "RETURNING id"),
        {"p": project_id, "k": skill_key},
    ).first()
    if not removed:
        raise SkillDefinitionError(f"no skill {skill_key!r} in this project.")
    emit_event(connection, project_id, "skill.deleted", actor, "skill_definition",
               str(removed[0]), {"skill_key": skill_key})
    return {"deleted": skill_key}
