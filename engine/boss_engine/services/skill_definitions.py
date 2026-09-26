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


def _shadows_a_builtin(connection: Connection, project_id: str, skill_key: str) -> bool:
    """Would authoring this key hide a skill BOSS ships?

    Asked of the entitlements catalogue rather than project_skills: a shipped skill needs no
    row there to exist, which is the same mistake `_skill_exists` was first written with.
    """
    try:
        from ..config import get_settings
        from .entitlements import EntitlementBrokerService

        catalogue = EntitlementBrokerService(get_settings()).list_skills(
            project_id, connection=connection)
        return any(str(item.get("id")) == skill_key for item in catalogue)
    except Exception:  # noqa: BLE001
        # A catalogue that cannot be read must not block authoring — the collision is rare and
        # the refusal would be unexplainable.
        return False


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
    authored = connection.execute(
        text("SELECT 1 FROM skill_definitions WHERE project_id = :p AND skill_key = :k"),
        {"p": project_id, "k": skill_id},
    ).first()
    if authored:
        return "authored"
    # Imported here, not at module scope: entitlements reaches back into this package, and the
    # cycle only bites at import time.
    from ..config import get_settings
    from .entitlements import EntitlementBrokerService

    catalogue = EntitlementBrokerService(get_settings()).list_skills(
        project_id, connection=connection)
    return "builtin" if any(s.get("id") == skill_id for s in catalogue) else None


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
    emit_event(connection, project_id, "skill.assigned_to_agent", actor, "role_skill",
               str(row["id"]), {"role_key": role, "skill_id": skill, "source": source})
    return {"role_key": role, "skill_id": skill, "source": source, "assigned": True}


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
    roles = connection.execute(
        text("""SELECT DISTINCT ON (role_key) role_key, spec FROM roles
                WHERE project_id = :p ORDER BY role_key, created_at DESC, id DESC"""),
        {"p": project_id},
    ).mappings().all()

    assigned: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    for row in roles:
        spec = row["spec"] if isinstance(row["spec"], dict) else {}
        wanted = ((spec.get("permissions") or {}).get("allowed_skills")) or []
        for skill_id in wanted:
            skill_id = str(skill_id).strip()
            # A wildcard is a description, not a grant. "infra.*" cannot be cashed in, and
            # expanding it here would hand an agent whatever happens to match it next week.
            if not skill_id or "*" in skill_id:
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
            "unavailable": missing, "unavailable_count": len(missing)}


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
    emit_event(connection, project_id, "skill.unassigned_from_agent", actor, "role_skill",
               str(removed[0]), {"role_key": role, "skill_id": skill_id})
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
    items: dict[str, dict[str, Any]] = {}

    # BUILT-IN FIRST, so an authored skill of the same key overwrites it below — which matches
    # `_skill_exists`, where authored wins. The two must agree, or the picker offers a skill and
    # the assignment records it under the other source.
    try:
        from ..config import get_settings
        from .entitlements import EntitlementBrokerService

        for skill in EntitlementBrokerService(get_settings()).list_skills(
                project_id, connection=connection):
            skill_id = str(skill.get("id") or "").strip()
            if not skill_id:
                continue
            items[skill_id] = {
                "skill_id": skill_id,
                "label": skill_id.replace("_", " ").replace(".", " · "),
                "summary": str(skill.get("description") or ""),
                "source": "builtin",
                "behaviour_kind": str(skill.get("kind") or ""),
                "risk_tier": int(skill.get("risk_tier") or 0),
                "enabled": bool(skill.get("enabled", True)),
            }
    except Exception:  # noqa: BLE001
        # A broken entitlements catalogue must not empty the agent page. The authored half below
        # still renders, and a missing built-in shows up as "cannot be assigned" at the point of
        # assignment, with a sentence, rather than as a page that will not load.
        pass

    for authored in list_skills(connection, project_id):
        key = str(authored["skill_key"])
        items[key] = {
            "skill_id": key,
            "label": authored["label"] or key,
            "summary": authored["summary"] or "",
            "source": "authored",
            "behaviour_kind": authored["behaviour_kind"],
            "risk_tier": authored["risk_tier"],
            "enabled": authored["enabled"],
        }

    return sorted(items.values(), key=lambda s: s["skill_id"])


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
