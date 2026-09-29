"""What a workflow will actually do, before anyone runs it.

THE PLAN WAS A LIST OF NAMES.
-----------------------------
The builder drew "Edit the video / auto", "UX sign-off / manual", "Publish to a channel /
manual", "Document / auto" — four labels and four dropdowns. An operator asked, reasonably:

    "Is it possible to see what the workflow will do, something like a workflow step
     definition? We need to know what kind of results it will bring, any agents associated or
     any task associated — right now those info are missing."

Every one of those facts already existed and none of them were joined up. The capability
palette knows what each step DOES, what it PRODUCES, which agent captains it, what it costs in
minutes, and whether its risk tier forces a manual gate. The stored spec knows where each
step's inputs come from. The custom-step table knows which skill a step actually runs.

So this is a JOIN, not a new subsystem: one function that turns a stored version into the plan
a person can read and agree to. It is deliberately shared — the console renders it and the
agent answers from it — because two descriptions of one workflow drift, and the operator is
the one who finds out.

NOTHING HERE EXECUTES ANYTHING. It reads a published version and returns a description.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import workflow_capabilities as caps


class WorkflowExplainError(ValueError):
    """Something the operator can act on, phrased for them."""


#: Where an input comes from, in words rather than in binding JSON. `{"from_step": "edit",
#: "field": "media_artifact_id"}` is precise and unreadable; "from the Edit the video step" is
#: what a person needs in order to agree that the plan is right.
def _source_of(binding: Any, step_labels: dict[str, str]) -> str:
    if isinstance(binding, dict):
        if binding.get("from_step"):
            origin = str(binding["from_step"])
            return f"from the “{step_labels.get(origin, origin)}” step"
        if binding.get("from_input"):
            return "you supply it when starting the run"
        if "value" in binding:
            return f"fixed: {binding['value']!r}"
    if binding not in (None, "", {}):
        return f"fixed: {binding!r}"
    return "NOT SET — this step cannot run until it is"


def _declared_inputs(raw: Any) -> list[tuple[str, bool]]:
    """Read capability names and the stored custom-step input schema alike.

    Custom steps persist ``[{name, required, description}, ...]`` while built-in
    capabilities declare plain names. Using a schema object as a dict key made
    the plan endpoint throw a TypeError for otherwise valid custom workflows.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            name, required = item.strip(), True
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            required = bool(item.get("required", True))
        else:
            continue
        if name and name not in seen:
            out.append((name, required))
            seen.add(name)
    return out


def explain_version(
    connection: Connection,
    *,
    project_id: str,
    workflow_key: str = "",
    definition_id: str = "",
    version: Optional[int] = None,
) -> dict[str, Any]:
    """The readable plan for one published version.

    Either `workflow_key` or `definition_id`. Without `version`, the ACTIVE one — because the
    question "what will this do" is about what would run now, not about what once ran.
    """
    if not workflow_key and not definition_id:
        raise WorkflowExplainError("workflow_key or definition_id is required.")

    where = "d.workflow_key = :k" if workflow_key else "d.id = :d"
    row = connection.execute(
        text(
            f"""
            SELECT d.id AS definition_id, d.workflow_key, d.name, d.description,
                   d.is_catalogue, d.active_version_id,
                   v.id AS version_id, v.version, v.status AS version_status, v.spec
              FROM workflow_definitions d
              LEFT JOIN workflow_versions v
                     ON v.workflow_definition_id = d.id
                    AND ({'v.version = :v' if version is not None
                          else 'v.id = d.active_version_id'})
             WHERE d.project_id = :p AND {where}
             LIMIT 1
            """
        ),
        {"p": project_id, "k": workflow_key, "d": definition_id,
         **({"v": version} if version is not None else {})},
    ).mappings().first()
    if row is None:
        raise WorkflowExplainError(
            f"no workflow {workflow_key or definition_id!r} in this project.")
    if row["version_id"] is None:
        # A definition with no published version is a real state, not an error: the builder
        # creates one before anything is saved. Saying so beats a 404 the console cannot draw.
        return {
            "workflow_key": row["workflow_key"], "name": row["name"],
            "description": row["description"], "definition_id": row["definition_id"],
            "version": None, "published": False,
            "summary": "This workflow has no published version yet, so there is nothing to "
                       "run. Add steps in the builder and save a version.",
            "steps": [], "you_must_supply": [], "results": [], "agents": [], "gates": 0,
        }

    spec = row["spec"] if isinstance(row["spec"], dict) else {}
    raw_steps = [s for s in (spec.get("steps") or []) if isinstance(s, dict)]
    palette = {c["key"]: c for c in caps.palette()}

    custom = {
        r["step_key"]: dict(r)
        for r in connection.execute(
            text("SELECT step_key, label, summary, binding_kind, skill_id, mcp_tool_name, "
                 "       inputs, produces "
                 "  FROM workflow_custom_steps WHERE project_id = :p"),
            {"p": project_id}).mappings().all()
    }

    # Labels first, so a later step can say where its input comes from BY NAME.
    labels: dict[str, str] = {}
    for step in raw_steps:
        binding = step.get("binding") or {}
        key = str(binding.get("capability") or "")
        step_key = str(binding.get("custom_step") or "")
        labels[str(step.get("id"))] = (
            custom.get(step_key, {}).get("label")
            or palette.get(key, {}).get("label")
            or str(step.get("id")))

    steps: list[dict[str, Any]] = []
    results: list[str] = []
    agents: list[str] = []
    supply: list[str] = []
    minutes = 0
    gates = 0

    for index, step in enumerate(raw_steps):
        step_id = str(step.get("id"))
        binding = step.get("binding") or {}
        cap_key = str(binding.get("capability") or "")
        step_key = str(binding.get("custom_step") or "")
        spec_of = palette.get(cap_key, {})
        own = custom.get(step_key, {})

        # A custom step runs a SKILL; the capability underneath it still decides risk and
        # gating. Both are reported, because "which skill" and "how dangerous" are different
        # questions and the operator asked the first one.
        runs = ""
        if step_key:
            runs = (own.get("mcp_tool_name") or own.get("skill_id") or step_key)
        elif spec_of.get("skills"):
            runs = ", ".join(spec_of["skills"])
        else:
            # FALL BACK TO THE CAPABILITY, rather than to nothing. Most built-in capabilities
            # declare no skill — they ARE the unit of work — so "runs" came back empty for
            # every step of a normal workflow and the field silently disappeared. Naming the
            # capability is the honest answer to "what does this actually execute".
            runs = cap_key

        produces = list(own.get("produces") or spec_of.get("produces") or [])
        results.extend(produces)

        effective = str((step.get("approval") or {}).get("mode") or "auto").lower()
        forced = bool(spec_of.get("approval_forced_manual"))
        if forced:
            effective = "manual"
        if effective == "manual" or spec_of.get("is_gate"):
            gates += 1

        captain = str(step.get("captain") or spec_of.get("captain") or "")
        if captain and captain not in agents:
            agents.append(captain)
        minutes += int(spec_of.get("typical_minutes") or 0)

        inputs = []
        declared = _declared_inputs(
            spec_of.get("required_inputs") or own.get("inputs") or [])
        wired = binding.get("inputs") or {}
        if not isinstance(wired, dict):
            wired = {}
        for name, required in declared:
            where_from = _source_of(wired.get(name), labels)
            if not required:
                if "NOT SET" in where_from:
                    where_from = "optional — you may supply it when starting the run"
                elif "you supply" in where_from:
                    where_from = "you may supply it when starting the run"
            if required and "you supply" in where_from and name not in supply:
                supply.append(name)
            inputs.append({"name": name, "source": where_from,
                           "required": required,
                           "ready": not required or "NOT SET" not in where_from})

        steps.append({
            "position": index + 1,
            "id": step_id,
            "label": labels.get(step_id, step_id),
            # WHAT IT DOES, in the capability's own words. This is the sentence the operator
            # was missing: a step called "Document" says nothing about what documenting is.
            "does": own.get("summary") or spec_of.get("summary") or "",
            "capability": cap_key or None,
            "runs": runs or None,
            # WHO ANSWERS FOR IT. "any agents associated" — the captain was already decided by
            # the capability and simply never shown.
            "agent": captain or None,
            "approval": effective,
            "approval_is_forced": forced,
            "approval_note": (
                "Forced: this step's risk tier is at or above the manual-approval floor, so it "
                "stops for a person whatever the workflow asks for."
                if forced else
                "A person must release this step." if effective == "manual" else
                "Runs without stopping."),
            "risk_tier": spec_of.get("risk_tier"),
            "typical_minutes": spec_of.get("typical_minutes"),
            "inputs": inputs,
            "produces": produces,
            "next": list(step.get("next") or []),
        })

    blocked = [
        f"“{s['label']}” has no source for {i['name']!r}"
        for s in steps for i in s["inputs"] if i["required"] and not i["ready"]
    ]
    return {
        "workflow_key": row["workflow_key"],
        "name": row["name"],
        "description": row["description"],
        "definition_id": row["definition_id"],
        "version": row["version"],
        "version_status": row["version_status"],
        "is_active": row["version_id"] == row["active_version_id"],
        "published": True,
        "summary": _one_paragraph(row["name"], steps, supply, results, gates, minutes),
        "steps": steps,
        # The three questions asked, answered at the top rather than inferred from the list.
        "you_must_supply": supply,
        "results": sorted(set(results)),
        "agents": agents,
        "gates": gates,
        "typical_minutes": minutes,
        "can_start": not blocked,
        "blocked_because": blocked,
    }


def _one_paragraph(name, steps, supply, results, gates, minutes) -> str:
    """The plan in a sentence, for the person who will not read the table."""
    if not steps:
        return f"{name} has no steps yet."
    chain = " → ".join(s["label"] for s in steps)
    parts = [f"{name} runs {len(steps)} step{'s' if len(steps) != 1 else ''}: {chain}."]
    if supply:
        parts.append(f"You supply {', '.join(supply)} when you start it.")
    if results:
        parts.append(f"It produces {', '.join(sorted(set(results)))}.")
    if gates:
        parts.append(
            f"{gates} step{'s' if gates != 1 else ''} stop{'' if gates != 1 else 's'} for "
            "your approval before continuing.")
    if minutes:
        parts.append(f"Typically about {minutes} minutes end to end.")
    return " ".join(parts)
