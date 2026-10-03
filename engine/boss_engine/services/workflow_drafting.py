"""Turning a description into workflow steps, and wiring a step's inputs to where they come from.

Shared by the chat agent's `draft_workflow` tool and the console's workflow editor (Describe,
and the server twin of its Auto-wire). Nothing here publishes: `_spec_from_steps` builds a spec
from a plain list a model can produce, `wire_spec` connects inputs, and `draft_from_description`
asks the project's model for the list. Only `_ensure_steps_for_*` write — they define the
custom step a drafted step names — and only the tool calls them; the editor's draft never
writes, so it resolves those steps read-only (`_bind_named_tools`).
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .._compat import services_module


def _spec_from_steps(steps: Any, args: dict) -> Any:
    """A workflow spec from the plain list a model can actually produce.

    The runtime's spec format carries `next`, `on_failure`, binding snapshots and per-step
    approval blocks. Asking a model to emit that by hand is asking it to get a graph right in
    JSON, and the failures are silent — a mistyped `next` makes a workflow that stops after
    step one. So the model describes a SEQUENCE and this builds the graph, which is the part
    that can be got right once.
    """
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "error": "steps must be a non-empty list"}
    if len(steps) > 24:
        return {"ok": False, "error": "24 steps is the most a workflow may have"}
    built: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(steps):
        if isinstance(raw, str):
            raw = {"capability": raw}
        if not isinstance(raw, dict):
            return {"ok": False, "error": f"step {index + 1} is not an object"}
        capability = str(raw.get("capability") or raw.get("kind") or "").strip().lower()
        # THE SHAPES THE BUILDER CAN DRAW, so the agent can draft them too. Only built-in
        # capabilities and skill-bound custom steps were expressible here, so a workflow the
        # agent drafted could never leave the agent to BOSS, scope a step's skills, call an MCP
        # tool, or stop for a person — and "agent_work" was refused as "not a capability".
        if raw.get("agent") is True or capability in _AGENT_STEP_WORDS:
            capability = "agent_work"
        elif isinstance(raw.get("gate"), dict) or raw.get("gate") is True \
                or capability in _GATE_WORDS:
            capability = "gate"
        elif raw.get("wait_seconds") is not None or capability in _WAIT_WORDS:
            capability = "wait"
        elif raw.get("run_workflow") or capability in _SUBWORKFLOW_WORDS:
            capability = "run_workflow"
        elif isinstance(raw.get("mcp_tool"), dict):
            capability = "mcp_tool"
        elif raw.get("skill") and not capability:
            # `{id, skill}` is a shape the tool documents; it was refused here as "has no
            # capability" because only the other shapes were mapped before this check.
            capability = "custom"
        if not capability:
            return {"ok": False, "error": f"step {index + 1} has no capability"}
        step_id = re.sub(r"[^a-z0-9_]+", "_",
                         str(raw.get("id") or capability).strip().lower()) or f"step_{index}"
        # A repeated id silently overwrites a step in the graph, so it is renamed rather than
        # refused: the operator asked for two previews and should get two.
        base = step_id
        suffix = 2
        while step_id in seen:
            step_id = f"{base}_{suffix}"
            suffix += 1
        seen.add(step_id)
        # THE RUNTIME'S OWN SHAPE, not a plausible-looking one. Every step is
        # kind="capability" with what it runs inside `binding` — a custom step names the step
        # key it defined, a standard one names the capability. Writing `kind: "preview"` would
        # have been refused by `validate_workflow_spec`, which is the good outcome; writing a
        # shape it ACCEPTS but the runtime reads differently is the bad one.
        entry: dict[str, Any] = {"id": step_id, "kind": "capability", "next": []}
        if capability == "agent_work":
            entry["binding"] = _agent_step_binding(raw)
        elif capability == "gate":
            gate = raw.get("gate") if isinstance(raw.get("gate"), dict) else {}
            entry = {"id": step_id, "kind": "decision", "next": [], "binding": {},
                     "question": str(gate.get("question") or raw.get("question") or "").strip(),
                     "guidance": str(gate.get("guidance") or raw.get("guidance") or "").strip(),
                     # A PERSON answers a gate unless the draft names somebody else.
                     "captain": str(raw.get("captain") or "human").strip().lower()}
        elif capability == "wait":
            entry = {"id": step_id, "kind": "timer", "next": [], "binding": {},
                     "wait_seconds": int(raw.get("wait_seconds") or 3600)}
        elif capability == "run_workflow":
            # Named by KEY, the way a model knows a workflow; resolved to the version that is
            # live now once there is a connection (`_resolve_subworkflows`).
            entry = {"id": step_id, "kind": "subworkflow", "next": [],
                     "binding": {"workflow_version_id": ""},
                     "_workflow_key": str(raw.get("run_workflow") or raw.get("workflow")
                                          or "").strip()}
        elif capability == "mcp_tool":
            tool = raw["mcp_tool"]
            entry["binding"] = {"custom_step": "", "inputs": raw.get("input") or {}}
            # Resolved to a custom step (made if missing) once there is a connection; see
            # `_ensure_steps_for_mcp_tools`.
            entry["_mcp_tool"] = {"server": str(tool.get("server") or "").strip(),
                                  "tool": str(tool.get("tool") or tool.get("name") or "").strip()}
        elif raw.get("skill") or capability == "custom":
            entry["binding"] = {"custom_step": str(raw.get("skill") or "").strip().lower(),
                                "inputs": raw.get("input") or {}}
            # The binding names the step, not the capability — but the capability is where
            # `required_inputs` and `produces` are declared, so the wiring pass needs it. Held
            # under a leading underscore and stripped before the spec is published.
            entry["_capability"] = capability
        else:
            entry["binding"] = {"capability": capability, "inputs": raw.get("input") or {}}
        if raw.get("captain") and entry["kind"] != "decision":
            entry["captain"] = str(raw["captain"]).strip().lower()
        mode = str(raw.get("approval") or "").strip().lower()
        if mode in ("manual", "auto") and entry["kind"] == "capability":
            entry["approval"] = {"mode": mode}
        built.append(entry)
    # THE CHAIN, BUILT NOT ASKED FOR. `next` is a LIST in the runtime's format; a bare string
    # is accepted by the validator and normalised, but emitting the real shape means the spec
    # the agent wrote and the spec the builder draws are byte-comparable.
    for index, entry in enumerate(built[:-1]):
        entry["next"] = [built[index + 1]["id"]]

    # AND THE DATA FLOW, FOR THE SAME REASON THE CHAIN IS.
    #
    # A step declares required inputs; `video_edit` needs `media_artifact_id`. Nothing wired
    # them, so a described sequence published as "step 'bookend' needs 'media_artifact_id',
    # which is not set" — and the model, told only that, had no way to know the answer was a
    # reference to the step above rather than a value it should invent. It looped.
    #
    # Asking a model to emit {"from_step": "import", "field": "media_artifact_id"} is asking it
    # to get data flow right in JSON, which is the same class of thing as getting `next` right,
    # and it is exactly as mechanical: an input is satisfied by the NEAREST EARLIER STEP that
    # produces a field of that name, and otherwise by the run's own input.
    # What the OPERATOR supplies when they start this workflow. Declared, never guessed: see
    # `_wire_inputs`.
    declared = set(_drafted_input_details(args.get("inputs"), args.get("input_details")))
    _wire_inputs(built, declared)
    for entry in built:
        entry.pop("_capability", None)

    # WHAT THE OPERATOR MUST SUPPLY, WRITTEN DOWN — otherwise the start form is blank.
    #
    # `inputs` was used to decide wiring and then dropped, so a published version carried
    # `input_schema: {}` while its steps referenced `from_input`. The bindings resolved and
    # the workflow was startable only by a caller that already knew the two field names; the
    # console's start form, which renders FROM the schema, had nothing to draw.
    #
    # Found by the Program Manager agent reading its own published spec back out of the
    # database rather than trusting the tool's success reply — which is the check that was
    # missing from every earlier attempt.
    #
    # Taken from what the wiring actually BOUND, not from what was asked for: an input the
    # model listed but no step consumes is not something to put on a form.
    #
    # AND AS JSON SCHEMA, with a label and a hint per field. It was `{name: "string"}`, which the
    # start form can only render as a bare box named after a variable, and which says nothing
    # about required — so a run started without the one value every step needed was accepted,
    # and failed three steps in. Every name here is read with `from_input` by some step, so each
    # IS required; one the model marked `required: false` is offered and not demanded.
    described = _drafted_input_details(args.get("inputs"), args.get("input_details"))
    bound: list[str] = []
    for entry in built:
        for value in ((entry.get("binding") or {}).get("inputs") or {}).values():
            if isinstance(value, dict) and value.get("from_input"):
                name = str(value["from_input"])
                if name not in bound:
                    bound.append(name)
    spec: dict[str, Any] = {"entry_step": built[0]["id"], "steps": built}
    if bound:
        properties: dict[str, Any] = {}
        for name in bound:
            said = described.get(name, {})
            prop: dict[str, Any] = {
                "type": "string",
                "title": str(said.get("title") or name.replace("_", " ").strip().capitalize()),
            }
            if said.get("description"):
                prop["description"] = str(said["description"])
            if said.get("format"):
                prop["format"] = str(said["format"])
            elif name == "instructions":
                # What an agent step is told to do is a paragraph, not a one-line box.
                prop["format"] = "textarea"
            properties[name] = prop
        spec["input_schema"] = {
            "type": "object",
            "required": [n for n in bound if described.get(n, {}).get("required", True)],
            "properties": properties,
        }
    return spec


#: Words a model uses for "let an agent work it out". All mean binding.agent_work.
_AGENT_STEP_WORDS = frozenset({"agent_work", "agent", "agent_step"})
#: Words for "stop and ask a person". All mean a `decision` step.
_GATE_WORDS = frozenset({"gate", "approval", "approval_gate", "decision", "human_approval"})
#: Words for "pause for a while". All mean a `timer` step.
_WAIT_WORDS = frozenset({"wait", "timer", "delay"})
#: Words for "run another workflow here". All mean a `subworkflow` step.
_SUBWORKFLOW_WORDS = frozenset({"run_workflow", "subworkflow"})


def _agent_step_binding(raw: dict[str, Any]) -> dict[str, Any]:
    """An agent step from what a model wrote: an instruction, and optionally a scope.

    The instruction defaults to the run's own `instructions` field — "say what you want when you
    start it", the shape almost every one of these has — and a literal pins it.
    """
    said = raw.get("instructions")
    if said is None or (isinstance(said, str) and not said.strip()):
        said = {"from_input": "instructions"}
    binding: dict[str, Any] = {
        "agent_work": True,
        "inputs": {**(raw.get("input") or {}), "instructions": said},
    }
    for field, source in (("skills", "skills"), ("mcp", "mcp"), ("mcp", "mcp_servers"),
                          ("produces", "produces"), ("agents", "agents")):
        value = raw.get(source)
        if isinstance(value, list) and value:
            binding[field] = [str(v).strip() for v in value if str(v).strip()]
    if raw.get("max_steps"):
        binding["max_steps"] = int(raw["max_steps"])
    return binding


def _drafted_input_details(raw: Any, details: Any = None) -> dict[str, dict[str, Any]]:
    """`inputs` as a model writes it — names, or {name, title, description, required, format} —
    with `input_details` ({name: {title, description, required}}) folded in.

    NOT `_declared_inputs`, which reads a PUBLISHED version's input_schema in either shape. This
    helper was first written under that same name further down the module; Python kept the later
    definition, so `describe_workflow_run` began reading JSON Schema with it and reported inputs
    called "required" and "properties" again — the exact incident `_declared_inputs` was written
    to end. test_no_module_defines_a_name_twice now refuses any such shadowing."""
    out: dict[str, dict[str, Any]] = {}
    for item in raw or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            out[str(item["name"]).strip()] = dict(item)
        elif str(item).strip() and not isinstance(item, dict):
            out[str(item).strip()] = {}
    if isinstance(details, dict):
        for name, said in details.items():
            if str(name).strip() and isinstance(said, dict):
                out.setdefault(str(name).strip(), {}).update(said)
    return out


def _wire_inputs(built: list[dict[str, Any]], declared: set[str]) -> None:
    """Point each step's unset required inputs at whatever upstream produces them.

    A drafted sequence, in order: the same rule as `wire_spec`, over a chain. A custom step still
    names its capability (`_capability`) and that is where `produces` is declared — so this
    works for a skill-bound step exactly as for a standard one.
    """
    caps = services_module("workflow_capabilities")
    palette = {x["key"]: x for x in caps.palette()}

    def contract(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
        binding = entry.get("binding") or {}
        cap = palette.get(str(binding.get("capability") or entry.get("_capability") or ""))
        if not cap:
            return [], []
        return list(cap.get("required_inputs") or ()), list(cap.get("produces") or ())

    order = [str(entry["id"]) for entry in built]
    _wire(built, order, {step_id: set(order[:i]) for i, step_id in enumerate(order)},
          contract, declared)


def _wire(
    steps: list[dict[str, Any]],
    order: list[str],
    upstream: dict[str, set[str]],
    contract: Callable[[dict[str, Any]], tuple[list[str], list[str]]],
    declared: set[str],
) -> list[dict[str, Any]]:
    """THE WIRING RULE, once. Mutates `steps`; returns what it connected.

    An input is satisfied by the NEAREST EARLIER STEP that produces a field of that name —
    nearest in `order`, among the steps `upstream` of it — and otherwise by the run's own input
    when the workflow declares one by that name. The console's `wireInputs` mirrors this.
    """
    position = {step_id: index for index, step_id in enumerate(order)}
    by_id = {str(step.get("id")): step for step in steps}
    wired: list[dict[str, Any]] = []
    for step_id in order:
        entry = by_id.get(step_id)
        if entry is None:
            continue
        required, _ = contract(entry)
        if not required:
            continue
        binding = entry.get("binding") if isinstance(entry.get("binding"), dict) else {}
        inputs = dict(binding.get("inputs") or {})
        changed = False
        for name in required:
            if inputs.get(name) not in (None, ""):
                continue
            producers = [other for other in upstream.get(step_id, ())
                         if other in by_id and name in contract(by_id[other])[1]]
            if producers:
                source = max(producers, key=lambda other: position.get(other, -1))
                inputs[name] = {"from_step": source, "field": name}
            elif name in declared:
                # The operator supplies it when they start the workflow. The first step of a
                # chain has no upstream by definition, so this is its only truthful wiring.
                inputs[name] = {"from_input": name}
            else:
                # OTHERWISE LEFT UNSET, DELIBERATELY.
                #
                # Wiring every unmatched input `from_input` would satisfy the validator and
                # produce a workflow that publishes cleanly and dies at run time on a value
                # nobody supplies — `run_id`, say, which is PRODUCED by a build step and is not
                # something an operator types. A spec the validator accepts but the runtime
                # reads differently is the worst outcome available here; refusing with "needs
                # 'run_id', which is not set" names the real design error.
                continue
            changed = True
            wired.append({"step_id": step_id, "input": name, "source": inputs[name]})
        if changed:
            binding["inputs"] = inputs
            entry["binding"] = binding
    return wired


def _ensure_steps_for_skills(connection, project_id: str, spec: dict, actor: str) -> list[str]:
    """Define a custom step for any binding that names a skill but has no step yet.

    THE TWO OBJECTS, AND WHY CONFUSING THEM LOOKED LIKE AN UNRESPONSIVE BUTTON.
    A SKILL is the unit of doing — `video.bookend`, `publish_youtube`. A CUSTOM STEP is a
    workflow-shaped wrapper around one, with a label, inputs and a risk tier, so a graph can
    place it. A model asked for a workflow names skills, because that is what the work IS; the
    step is bookkeeping it has no reason to know about.

    Both kinds of skill count: `skill_definitions` holds what this project authored, and the
    entitlements catalogue holds what BOSS ships. Only the first was ever checked, so every
    built-in — the whole media pipeline — read as "this project does not have that skill".

    Returns the step keys it created, so the caller can say what it did.
    """
    steps_service = services_module("workflow_custom_steps")
    skills = services_module("skill_definitions")
    created: list[str] = []
    for entry in spec.get("steps", []):
        binding = entry.get("binding") or {}
        wanted = str(binding.get("custom_step") or "").strip()
        if not wanted:
            continue
        existing = connection.execute(
            text("SELECT step_key FROM workflow_custom_steps "
                  "WHERE project_id = :p AND step_key = :k LIMIT 1"),
            {"p": project_id, "k": wanted},
        ).first()
        if existing:
            continue
        # Not a step. Is it a skill — authored, or one BOSS ships?
        source = skills._skill_exists(connection, project_id, wanted)
        if source is None:
            # Leave it. `_draft_problems` says which skills this project has, which is a more
            # useful sentence than anything invented here.
            continue
        # A step key cannot hold a dot; a skill id routinely does (`video.bookend`).
        step_key = re.sub(r"[^a-z0-9_]+", "_", wanted.lower()).strip("_")[:48]
        label = wanted.replace(".", " ").replace("_", " ").strip().title()
        try:
            steps_service.create(
                connection, project_id=project_id, actor=actor,
                step_key=step_key, label=label,
                summary=f"Runs the {wanted} skill.",
                binding_kind="skill", skill_id=wanted,
                inputs=[], produces=[], knowledge="",
            )
        except Exception:  # noqa: BLE001 — a step that will not define is a problem
            continue                                   # _draft_problems reports it properly
        binding["custom_step"] = step_key
        created.append(step_key)
    return created


def _resolve_subworkflows(connection, project_id: str, spec: dict) -> list[str]:
    """Point each "run another workflow" step at the live version of the workflow it names."""
    problems: list[str] = []
    for entry in spec.get("steps", []):
        if "_workflow_key" not in entry:
            continue
        key = entry.pop("_workflow_key")
        version = connection.execute(
            text("SELECT active_version_id FROM workflow_definitions "
                  "WHERE project_id = :p AND workflow_key = :k LIMIT 1"),
            {"p": project_id, "k": key},
        ).scalar() if key else None
        if not version:
            problems.append(
                f"step '{entry.get('id')}' runs workflow '{key or '?'}', which this project has "
                "no published version of. Name a workflow this project has "
                "(describe_workflow lists them).")
            continue
        entry["binding"]["workflow_version_id"] = str(version)
    return problems


def _ensure_steps_for_mcp_tools(connection, project_id: str, spec: dict, actor: str) -> list[str]:
    """Bind each `{mcp_tool: {server, tool}}` step to a custom step that runs that tool.

    Reuses a step already bound to the same server and tool, and defines one otherwise — the
    same thing the builder's "New step" does — so a drafted MCP step is an ordinary custom step.
    A server that is not registered is left unbound; `_draft_problems` says so.
    """
    step_agents = services_module("step_agents")
    steps_service = services_module("workflow_custom_steps")
    created: list[str] = []
    for entry in spec.get("steps", []):
        wanted = entry.pop("_mcp_tool", None)
        if not wanted:
            continue
        servers, missing = step_agents.resolve_mcp(connection, project_id, [wanted["server"]])
        if missing or not wanted["tool"]:
            entry["binding"]["custom_step"] = (
                f"mcp:{wanted['server'] or '?'}:{wanted['tool'] or '?'}")
            continue
        server = servers[0]
        existing = connection.execute(
            text("SELECT step_key FROM workflow_custom_steps WHERE project_id = :p "
                  "AND binding_kind = 'mcp_tool' AND mcp_server_id = :s AND mcp_tool_name = :t "
                  "ORDER BY created_at LIMIT 1"),
            {"p": project_id, "s": server["id"], "t": wanted["tool"]},
        ).scalar()
        if existing:
            entry["binding"]["custom_step"] = str(existing)
            continue
        label = f"{server['name']} {wanted['tool']}".replace("_", " ").strip().title()
        try:
            made = steps_service.create(
                connection, project_id=project_id, actor=actor, label=label,
                summary=f"Calls {wanted['tool']} on {server['name']}.",
                binding_kind="mcp_tool", mcp_server_id=str(server["id"]),
                mcp_tool_name=wanted["tool"], inputs=[], produces=[], knowledge="",
            )
        except Exception:  # noqa: BLE001 — reported by _draft_problems as a missing step
            entry["binding"]["custom_step"] = f"mcp:{server['name']}:{wanted['tool']}"
            continue
        entry["binding"]["custom_step"] = made["step_key"]
        created.append(made["step_key"])
    return created




# ── Wiring a whole spec (the editor's Auto-wire, server side) ─────────────────────────────────


def _declared_input_names(schema: Any) -> set[str]:
    """Every field a workflow's start form asks for, in either shape it is written in.

    Not `workflows.declared_input_names`, which reads only the REQUIRED list of JSON Schema: an
    optional field the operator may fill is still a field a step can be wired to.
    """
    if not isinstance(schema, dict) or not schema:
        return set()
    if "required" in schema or "properties" in schema:
        names = {str(n) for n in schema.get("required") or [] if str(n).strip()}
        properties = schema.get("properties")
        if isinstance(properties, dict):
            names |= {str(n) for n in properties}
        return names
    return {str(n) for n in schema}


def graph_order(spec: dict[str, Any]) -> tuple[list[str], dict[str, set[str]]]:
    """The steps in flow order, and every step upstream of each.

    Flow order is topological over every edge the runtime follows (`next`, branches, joins,
    condition arms, loop bodies), ties broken by the order the steps are written in. Anything a
    cycle leaves out is appended in written order, so a half-edited spec still wires.
    """
    from .workflows import _targets

    steps = [s for s in spec.get("steps") or [] if isinstance(s, dict) and s.get("id")]
    ids = [str(s["id"]) for s in steps]
    known = set(ids)
    edges = {str(s["id"]): [t for t in _targets(s) if t in known] for s in steps}
    incoming = {step_id: 0 for step_id in ids}
    for targets in edges.values():
        for target in targets:
            incoming[target] += 1
    ready = [step_id for step_id in ids if incoming[step_id] == 0]
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for target in edges[current]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
        ready.sort(key=ids.index)
    order += [step_id for step_id in ids if step_id not in order]

    parents: dict[str, set[str]] = {step_id: set() for step_id in ids}
    for source, targets in edges.items():
        for target in targets:
            parents[target].add(source)
    upstream: dict[str, set[str]] = {}
    for step_id in ids:
        seen: set[str] = set()
        frontier = list(parents[step_id])
        while frontier:
            parent = frontier.pop()
            if parent in seen or parent == step_id:
                continue
            seen.add(parent)
            frontier.extend(parents[parent])
        upstream[step_id] = seen
    return order, upstream


def step_contracts(connection: Optional[Connection], project_id: str
                   ) -> Callable[[dict[str, Any]], tuple[list[str], list[str]]]:
    """What each step needs and makes: (required inputs, produced fields).

    A built-in step's come from its capability, a custom step's from its definition, and an
    agent task makes what its `produces` lists (it needs only its instruction, which the
    operator writes). Control steps neither need nor make anything.
    """
    caps = services_module("workflow_capabilities")
    custom: dict[str, dict[str, Any]] = {}
    if connection is not None:
        custom = {s["step_key"]: s for s in
                  services_module("workflow_custom_steps").list_steps(connection, project_id)}

    def contract(step: dict[str, Any]) -> tuple[list[str], list[str]]:
        binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
        if step.get("kind") != "capability":
            return [], []
        if binding.get("agent_work"):
            return [], [str(p) for p in binding.get("produces") or [] if str(p).strip()]
        if binding.get("custom_step"):
            own = custom.get(str(binding["custom_step"])) or {}
            return ([str(i["name"]) for i in own.get("inputs") or []
                     if isinstance(i, dict) and i.get("required") and i.get("name")],
                    [str(p) for p in own.get("produces") or []])
        key = str(binding.get("capability") or "")
        if key in caps.CAPABILITIES:
            capability = caps.get(key)
            return list(capability.required_inputs), list(capability.produces)
        return [], []

    return contract


def wire_spec(connection: Optional[Connection], project_id: str, spec: dict[str, Any],
              ids: Optional[list[str]] = None) -> dict[str, Any]:
    """Connect every unset required input in `spec` (or only in steps `ids`). Writes nothing.

    Returns {"spec": the wired copy, "wired": [{step_id, input, source}]}. An input already set
    is never changed, and one nothing upstream makes and the start form does not declare is
    left unset for Check to name.
    """
    wired_spec = copy.deepcopy(spec) if isinstance(spec, dict) else {"steps": []}
    order, upstream = graph_order(wired_spec)
    if ids is not None:
        wanted = {str(i) for i in ids}
        order_to_wire = [step_id for step_id in order if step_id in wanted]
    else:
        order_to_wire = order
    contract = step_contracts(connection, project_id)
    position = {step_id: index for index, step_id in enumerate(order)}
    # Producers are judged in full flow order, whichever steps are being wired.
    steps = [s for s in wired_spec.get("steps") or [] if isinstance(s, dict)]
    wired = _wire(steps, sorted(order_to_wire, key=position.get), upstream, contract,
                  _declared_input_names(wired_spec.get("input_schema")))
    return {"spec": wired_spec, "wired": wired}


# ── Describe it: a draft from a sentence (the editor's empty state) ───────────────────────────


class DraftError(RuntimeError):
    """The model's answer could not be turned into a workflow. Says why, for the operator."""


class NoModelError(DraftError):
    """This project has no model to draft with."""


#: What a step may be, in the words the model writes them. The `draft_workflow` tool documents
#: the same shapes to the chat agent; both go through `_spec_from_steps`.
STEP_SHAPES = (
    "{id, capability, captain?, approval?} a built-in step; "
    "{id, skill: <custom step key>, captain?} runs one of this project's own steps; "
    "{id, agent: true, instructions?, captain?, skills?: [...], mcp?: [server names], "
    "produces?: [...]} lets an agent work it out (instructions default to the run's "
    "'instructions' input; leave captain out and BOSS picks the agent holding the skills); "
    "{id, gate: {question, guidance}} stops for a person; {id, wait_seconds} pauses. "
    "`captain` is one of the project's agents. `approval` is 'manual' or 'auto'. Do NOT wire "
    "inputs: a step's required input is connected to the nearest earlier step that makes it."
)


def model_connected(connection: Connection, settings: Any, project_id: str) -> bool:
    """Whether this project can ask a model anything — the same check a run's checklist makes."""
    from .workflow_readiness import check

    return bool(check(connection, settings, project_id, "model")[0])


def _ask_model(connection: Connection, settings: Any, project_id: str, prompt: str) -> str:
    """One drafting question to the project's chat model. Raises whatever the adapter raises."""
    from . import model_routing

    reply = model_routing.project_chat_adapter(connection, settings, project_id).create_text_response(
        prompt,
        system_prompt="You design delivery workflows as a list of steps. You output JSON only.",
    )
    if isinstance(reply, tuple):
        return str(reply[1] if len(reply) > 1 else reply[0] or "")
    return str(reply or "")


def _draft_prompt(description: str, palette: dict[str, Any]) -> str:
    def listed(values: Any) -> str:
        return ", ".join(str(v) for v in list(values or [])[:8]) or "nothing"

    capabilities = "\n".join(
        f"- {c['key']}: {c['label']} — {c['summary']} "
        f"(needs {listed(c['required_inputs'])}; makes {listed(c['produces'])})"
        for c in palette["capabilities"])
    own = "\n".join(f"- {s['step_key']}: {s['label']} — {s.get('summary') or ''}"
                    for s in palette["custom_steps"]) or "- (none)"
    agents = "\n".join(f"- {a['role_key']}: {a['name']} (holds {listed(a.get('holds'))})"
                       for a in palette["agents"]) or "- (none)"
    return (
        "Design a workflow for this request. Reply with ONE JSON object and nothing else:\n"
        '{"name": "<3-6 word name>", "description": "<one sentence>", '
        '"steps": [...], "inputs": ["<what the operator supplies at start>"]}\n\n'
        f"Each step is one of: {STEP_SHAPES}\n\n"
        f"Built-in capabilities:\n{capabilities}\n\n"
        f"This project's own steps:\n{own}\n\n"
        f"This project's agents:\n{agents}\n\n"
        "Prefer built-in capabilities and this project's own steps; use an agent step for "
        "anything they do not cover. Put a gate before anything risky. 2 to 12 steps.\n\n"
        f"Request: {description.strip()}"
    )


def _only_json(value: str) -> dict[str, Any]:
    trimmed = str(value or "").strip()
    if trimmed.startswith("```"):
        trimmed = trimmed.strip("`")
        trimmed = trimmed[4:] if trimmed.startswith("json") else trimmed
    start, end = trimmed.find("{"), trimmed.rfind("}")
    if start == -1 or end <= start:
        raise DraftError("The model did not answer with a workflow. Try describing it again.")
    try:
        parsed = json.loads(trimmed[start:end + 1])
    except ValueError as error:
        raise DraftError("The model's answer was not readable. Try describing it again.") \
            from error
    if not isinstance(parsed, dict):
        raise DraftError("The model did not answer with a workflow. Try describing it again.")
    return parsed


def _bind_named_tools(connection: Connection, project_id: str, spec: dict[str, Any]) -> None:
    """Resolve what a drafted step names WITHOUT defining anything (the editor's draft writes
    nothing; the tool's `_ensure_steps_for_*` define steps because it publishes).

    A named skill becomes the project's own step that runs it, when there is one, and otherwise
    an agent task limited to that skill; a named MCP tool likewise, limited to its server. Both
    are steps the operator can see and change before saving.
    """
    custom = services_module("workflow_custom_steps").list_steps(connection, project_id)
    step_agents = services_module("step_agents")
    by_key = {s["step_key"]: s for s in custom}
    by_skill = {str(s["skill_id"]): s for s in custom if s.get("skill_id")}
    for entry in spec.get("steps") or []:
        tool = entry.pop("_mcp_tool", None)
        binding = entry.get("binding") or {}
        if tool:
            servers, _ = step_agents.resolve_mcp(connection, project_id, [tool["server"]])
            server_id = str(servers[0]["id"]) if servers else ""
            match = next((s for s in custom if s.get("binding_kind") == "mcp_tool"
                          and s.get("mcp_tool_name") == tool["tool"]
                          and str(s.get("mcp_server_id") or "") == server_id), None)
            if server_id and match:
                binding["custom_step"] = match["step_key"]
                continue
            entry["binding"] = {
                "agent_work": True, "mcp": [tool["server"]] if tool["server"] else [],
                "inputs": {"instructions": f"Call {tool['tool'] or 'the tool'} on "
                                           f"{tool['server'] or 'the MCP server'}."}}
            continue
        wanted = str(binding.get("custom_step") or "")
        if not wanted or wanted in by_key:
            continue
        if wanted in by_skill:
            binding["custom_step"] = by_skill[wanted]["step_key"]
            continue
        entry["binding"] = {"agent_work": True, "skills": [wanted],
                            "inputs": {"instructions": f"Use the {wanted} skill."}}


def draft_from_description(engine: Any, settings: Any, project_id: str,
                           description: str) -> dict[str, Any]:
    """A workflow drafted from a sentence, for the operator to review. Writes nothing.

    Reads what this project can use (its capabilities, its own steps, its agents), asks its
    model for a list of steps, and builds the spec the same way the chat agent's
    `draft_workflow` does — chained, and with every input wired that can be. The model is
    asked outside any transaction: a draft can take a minute, and a transaction held that long
    blocks the writes it was never going to make.
    """
    said = str(description or "").strip()
    if not said:
        raise DraftError("Say what the workflow should do.")
    caps = services_module("workflow_capabilities")
    step_agents = services_module("step_agents")
    with engine.connect() as connection:
        if not model_connected(connection, settings, project_id):
            raise NoModelError("no model")
        palette = {
            "capabilities": caps.palette(),
            "custom_steps": [s for s in services_module("workflow_custom_steps").list_steps(
                connection, project_id) if s["enabled"]],
            "agents": step_agents.project_agents(connection, project_id),
        }
        prompt = _draft_prompt(said, palette)
        connection.rollback()
    with engine.connect() as connection:
        answer = _only_json(_ask_model(connection, settings, project_id, prompt))
        connection.rollback()
    steps = answer.get("steps")
    spec = _spec_from_steps(steps if isinstance(steps, list) else [], {
        "inputs": answer.get("inputs") if isinstance(answer.get("inputs"), list) else [],
        "input_details": answer.get("input_details")
        if isinstance(answer.get("input_details"), dict) else None})
    if isinstance(spec, dict) and spec.get("ok") is False:
        raise DraftError(f"The draft could not be used: {spec.get('error')}.")
    with engine.connect() as connection:
        _bind_named_tools(connection, project_id, spec)
        # A step naming a workflow this project does not have keeps an empty pin, for the
        # operator to point at one — Check names it — rather than refusing the whole draft.
        _resolve_subworkflows(connection, project_id, spec)
        # Wire again now every step is resolved: a custom step's inputs are its definition's.
        spec = wire_spec(connection, project_id, spec)["spec"]
        connection.rollback()
    for entry in spec["steps"]:
        for private in ("_capability", "_mcp_tool", "_workflow_key"):
            entry.pop(private, None)
    name = re.sub(r"\s+", " ", str(answer.get("name") or "")).strip()[:80] or "Drafted workflow"
    result: dict[str, Any] = {"name": name, "spec": spec}
    if str(answer.get("description") or "").strip():
        result["description"] = str(answer["description"]).strip()[:500]
    return result
