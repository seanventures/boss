"""Reusable, mapped, durable workflow runtime.

The runtime deliberately owns orchestration only.  Agent groups, skills,
integrations, decisions, and subworkflows remain separate capabilities and are
referenced through pinned bindings.  All control-flow evaluation is
declarative; no workflow field is evaluated as Python or shell code.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from . import two_pass
from .core import emit_event, json_hash, make_id, now_utc
from .workflow_custom_steps import (
    optional_inputs as custom_step_optional_inputs,
    validate_step as defined_step_problems,
    validate_step_fields as defined_step_field_problems,
)
from .workflow_capabilities import (
    approval_mode_for as effective_approval_mode,
    validate_custom_step as custom_step_problems,
    effective_approval as effective_approval_policy,
    estimate_minutes as capability_minutes,
    project_requirements as capability_project_requirements,
    validate_step_fields as capability_problems,
    validate_captain,
)
from .step_agents import (
    AGENT_RULES_KEY,
    CURRENT_AGENT_RULES,
    assignment_problems as step_agent_problems,
    captain_shape_problems as step_agent_captain_problems,
    named_agent as step_named_agent,
    scope_shape_problems as step_agent_scope_problems,
    tag_steps as tag_steps_with_agent_rules,
    under_current_rules as under_current_agent_rules,
)
from .typed_decisions import step_problems as typed_decision_problems

logger = logging.getLogger(__name__)


WORKFLOW_KINDS = {
    "task",
    # A PRODUCT CAPABILITY: research, meeting, build, deploy, monitor.
    #
    # Added, never substituted. A step could already call a skill, an integration or an agent
    # group, and none of those said "build it" — so the five things a delivery workflow is made
    # of were exactly the five things a step could not be. Every existing kind behaves as it
    # always did; see services/workflow_capabilities.py for what a capability declares.
    "capability",
    "skill",
    "integration",
    "agent_group",
    "decision",
    "timer",
    "subworkflow",
    "condition",
    "parallel",
    "join",
    "bounded_loop",
    "for_each",
}
CONTROL_KINDS = {
    "condition",
    "parallel",
    "join",
    "bounded_loop",
    "for_each",
    "decision",
    "timer",
    "subworkflow",
}
#: Whose workflow a definition is. See the column comment in db/schema.sql.
NAMESPACES = {"system", "product"}

#: Product workflows BOSS runs for the operator but does not offer as templates: Repo and
#: models' Try it (`setup_first_pr`). Product namespace so the capability runner ticks them and
#: their runs list with the operator's other work; hidden from the Library and its counts.
HIDDEN_PRODUCT_KEYS: tuple[str, ...] = ("setup_first_pr",)
SYSTEM_NAMESPACE = "system"
PRODUCT_NAMESPACE = "product"

TERMINAL_RUN_STATES = {
    "completed",
    "failed",
    "deadlettered",
    "cancelled",
    "compensated",
}
TERMINAL_STEP_STATES = {"succeeded", "skipped", "failed", "deadlettered", "cancelled"}
ACTIVE_STEP_STATES = {"ready", "leased", "running", "waiting"}
_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class WorkflowError(RuntimeError):
    """`problems`, when given, is every problem found, one per step and field (see `problem`)."""

    def __init__(self, *args: Any, problems: Optional[list[dict[str, Any]]] = None) -> None:
        super().__init__(*args)
        self.problems: list[dict[str, Any]] = list(problems or [])


class WorkflowNotFoundError(WorkflowError):
    pass


class WorkflowConflictError(WorkflowError):
    pass


class WorkflowValidationError(WorkflowError):
    pass


class WorkflowMappingNotFoundError(WorkflowError):
    pass


class WorkflowMappingAmbiguousError(WorkflowError):
    pass


class WorkflowLeaseError(WorkflowError):
    pass


class WorkflowNamespaceError(WorkflowError):
    """A caller reached across the system/product line.

    Raised rather than returning "not found" because the two failures need different answers: a
    missing workflow is a typo, and this is a customer surface touching BOSS's own machinery,
    which somebody needs to see in the logs.
    """


class WorkflowBindingBlockedError(WorkflowError):
    pass


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _clean_text(value: Any, name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise WorkflowValidationError(f"{name} is required.")
    return cleaned


def _step_map(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(step["id"]): step for step in spec.get("steps", [])}


def _targets(step: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    next_value = step.get("next", [])
    if isinstance(next_value, str):
        targets.append(next_value)
    elif isinstance(next_value, list):
        targets.extend(str(item) for item in next_value)
    kind = step.get("kind")
    if kind == "condition":
        targets.extend([str(step.get("on_true") or ""), str(step.get("on_false") or "")])
    elif kind == "parallel":
        targets.extend(str(item) for item in step.get("branches", []))
        if step.get("join"):
            targets.append(str(step["join"]))
    elif kind in ("bounded_loop", "for_each") and step.get("body"):
        targets.append(str(step["body"]))
    # An error path is an edge too: a step that fails terminally goes on to it. Counted here so
    # an error-only step is reachable, an error path back into the graph is a cycle, and a step
    # an in-flight step may still fail into is not skipped (`_skip_unless_reachable`).
    if isinstance(step.get("on_error"), str) and step["on_error"].strip():
        targets.append(step["on_error"].strip())
    return [target for target in targets if target]


def _success_targets(step: dict[str, Any]) -> list[str]:
    """The edges a step is followed along when it does NOT fail: `_targets` without its
    `on_error` edge (a step that is both its next step and its error step stays, as a next)."""
    return _targets({**step, "on_error": None})


#: Retry backoff, between automatic attempts of one step (`retry_backoff` on a step).
RETRY_BACKOFF_DEFAULTS: dict[str, Any] = {"initial_seconds": 0, "multiplier": 2,
                                          "max_seconds": 3600}
#: How long one attempt of a step may run (`timeout_seconds` on a step), at most a day.
STEP_TIMEOUT_MAX_SECONDS = 86_400
#: The reason a timed-out attempt is failed with: in the step's output and on its events.
STEP_TIMEOUT_REASON = "step_timeout"
#: Stamped on a step that failed terminally and whose run went on along its `on_error` path.
#: It stays `failed`; this is what says nobody has to decide about it (`_first_stopped_step`).
ERROR_PATH_TAKEN = "error_path_taken"


def _backoff_problems(step_id: str, raw: Any) -> tuple[Optional[dict[str, Any]], list[str]]:
    """`retry_backoff` normalised with its defaults, and every problem with it."""
    if not isinstance(raw, dict):
        return None, [f"step '{step_id}' retry_backoff must be an object with initial_seconds, "
                      "multiplier and max_seconds."]
    out: dict[str, Any] = {}
    problems: list[str] = []
    for key, low, high, whole in (("initial_seconds", 0, 3600, True),
                                  ("multiplier", 1, 10, False),
                                  ("max_seconds", 0, 86_400, True)):
        value = raw.get(key, RETRY_BACKOFF_DEFAULTS[key])
        number: Optional[float]
        try:
            if isinstance(value, bool):
                raise TypeError
            number = float(value)
            if whole and number != int(number):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            number = None
        if number is None or number < low or number > high:
            what = "a whole number of seconds" if whole else "a number"
            problems.append(f"step '{step_id}' retry_backoff {key} must be {what} between "
                            f"{low} and {high}.")
            continue
        out[key] = int(number) if number == int(number) else number
    unknown = sorted(str(k) for k in set(raw) - set(RETRY_BACKOFF_DEFAULTS))
    if unknown:
        problems.append(f"step '{step_id}' retry_backoff has unknown field(s): "
                        f"{', '.join(unknown)}.")
    return (out if not problems else None), problems


def retry_backoff_seconds(step: Optional[dict[str, Any]], attempt: int) -> int:
    """How long a step waits before its next automatic attempt, after attempt `attempt` (1 is
    the first) failed: min(initial * multiplier^(attempt-1), max). Zero with no backoff."""
    backoff = (step or {}).get("retry_backoff")
    if not isinstance(backoff, dict):
        return 0
    initial = float(backoff.get("initial_seconds", RETRY_BACKOFF_DEFAULTS["initial_seconds"]))
    multiplier = float(backoff.get("multiplier", RETRY_BACKOFF_DEFAULTS["multiplier"]))
    ceiling = float(backoff.get("max_seconds", RETRY_BACKOFF_DEFAULTS["max_seconds"]))
    if initial <= 0:
        return 0
    delay = initial
    # Multiplied up to the cap, never past it, so a long chain of attempts cannot overflow.
    for _ in range(max(0, int(attempt) - 1)):
        if delay >= ceiling:
            break
        delay *= multiplier
    return int(min(delay, ceiling))


#: The shapes an input_schema is written in. BOTH already exist in real databases and a third
#: would make the reconciliation worse, not better:
#:
#:   * ``{"video_url": "string"}``                  — name-to-type, what draft_workflow emits
#:   * ``{"required": [...], "properties": {...}}`` — JSON Schema, 21 of 45 versions in one DB
#:
#: So this VALIDATES both and invents neither. Richer description — a label, a hint, an
#: example — belongs in JSON Schema's own `title`, `description` and `examples`, which is what
#: those keys are for and what any other tool already understands.


def declared_input_names(raw: Any) -> list[str]:
    """The input names a spec declares, in whichever shape it wrote them.

    Reading `.keys()` on the JSON Schema shape yields ["required"], which told operators to
    supply an input called "required" — confidently wrong, and truthy enough to displace the
    real diagnosis. Kept here beside the validator so the runtime and the tools agree.
    """
    if not isinstance(raw, dict) or not raw:
        return []
    if "required" in raw or "properties" in raw:
        required = raw.get("required")
        if isinstance(required, list):
            return [str(x) for x in required]
        properties = raw.get("properties")
        return [str(k) for k in properties] if isinstance(properties, dict) else []
    return [str(k) for k in raw]


def validate_input_schema(raw: Any) -> dict[str, Any]:
    """What this workflow needs supplied when a run is started.

    WHY IT MATTERS. `from_input` has always resolved ANY field from a run's input — the
    resolver looks the name up in a dict — so a step could already read `video_url`. What no
    entry point could do was PASS one: start_workflow accepted directive, workflow_key, title
    and from_discovery and nothing else, so a workflow needing a file to fetch or a channel to
    publish to had nowhere to receive them.

    Returned unchanged when it is one of the two known shapes. This is a check, not a
    rewrite: versions already published carry these values, and normalising them into a third
    shape would strand every one of them.
    """
    if raw in (None, "", {}):
        return {}
    if not isinstance(raw, dict):
        raise WorkflowValidationError("input_schema must be an object.")
    for name in declared_input_names(raw):
        if not _STEP_ID.fullmatch(str(name)):
            raise WorkflowValidationError(
                f"declared input '{name}' must use letters, numbers, dot, underscore, or dash.")
    return raw


def consent_task_title(connection: Connection, project_id: str, definition_id: str,
                       input_data: Any) -> str:
    """What the consent sheet says the person is agreeing to run: the WORKFLOW and the APP.

    The sheet said "To run this workflow run" when the input had no title (a security review
    is started with only the app), naming neither. Now: "Security review for Member portal",
    or "<workflow>: <title>" for a titled run, the app resolved from any of its handles.
    """
    data = input_data if isinstance(input_data, dict) else {}
    try:
        workflow = str(connection.execute(
            text("SELECT name FROM workflow_definitions WHERE id = :d"),
            {"d": definition_id}).scalar() or "").strip()
    except Exception:  # noqa: BLE001 — a title must never stop a start
        workflow = ""
    workflow = workflow or "this workflow"
    tpl = data.get("template") if isinstance(data.get("template"), dict) else {}
    handle = str(data.get("app_name") or data.get("app") or data.get("app_key")
                 or tpl.get("app_key") or "").strip()
    app = ""
    if handle:
        try:
            from ..app_links.service import app_index, resolve_app_key

            with connection.begin_nested():
                key = resolve_app_key(connection, project_id, handle)
                app = app_index(connection, project_id)[1].get(key or "", "") if key else ""
        except Exception:  # noqa: BLE001
            app = ""
        app = app or handle
    titled = str(data.get("title") or data.get("directive") or "").strip()
    if app and titled and app.casefold() not in titled.casefold():
        return f"{workflow} for {app}: {titled}"[:200]
    if titled:
        return f"{workflow}: {titled}"[:200]
    if app:
        return f"{workflow} for {app}"[:200]
    return workflow


def missing_required_inputs(spec: dict[str, Any], input_data: dict[str, Any]) -> list[str]:
    """Declared inputs a run was started without. Empty when it can proceed.

    Only JSON Schema distinguishes required from optional — the name-to-type shape says
    nothing about it, so nothing there is treated as mandatory. Guessing would refuse runs
    that have always been legal.

    Checked at START rather than when the step that needs it runs: a workflow that fails on
    its third step because nobody supplied a video URL has already paid for the first two.
    """
    schema = (spec or {}).get("input_schema") or {}
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    supplied = input_data or {}
    return sorted(
        str(name) for name in required
        if not str(supplied.get(str(name)) or "").strip()
    )


def project_roster(connection: Connection, project_id: str) -> frozenset[str]:
    """Every agent a step in this project may name — what validate_captain accepts.

    The bundled REGISTERED_ROLES alone refused every agent an operator created, so the
    quick-create promise "it can be put on a workflow step as soon as it exists" was false.
    tests/test_a_created_agent_can_captain_a_step.py checks every validate_workflow_spec call
    that has a connection passes this.

    THE PROJECT'S AGENTS, PLUS THE BUNDLED ROLES IT HAS NOT DELETED. Unioning every bundled role
    in (review of milestone/agents, round 2) let a workflow name 'qa' after the operator had
    deleted qa: publish accepted it, and the absence only surfaced at start or minutes into a
    run. A deleted bundled agent is tombstoned (seed_exclusions 'role'), so it is left out here
    and publish refuses it with the sentence that says what to do. Bundled roles a project has
    simply not installed stay in, as before — several captains run through the agent process
    template without a pack installing them (test_every_step_has_a_captain.KNOWN_UNROOTED).
    """
    from . import seed_exclusions
    from .workflow_capabilities import HUMAN_ROLES, REGISTERED_ROLES

    roles = {
        str(key).strip().lower() for key in connection.execute(
            text("SELECT role_key FROM roles WHERE project_id = :p"), {"p": project_id},
        ).scalars().all()
    }
    deleted = seed_exclusions.excluded(connection, project_id, "role") - roles
    return frozenset(roles | (REGISTERED_ROLES - deleted) | HUMAN_ROLES)


#: The longest step title the editor may set. A title names a node on a canvas; anything longer
#: is a description, and the step already has one.
STEP_TITLE_MAX = 80

#: "step 'build' (Build) needs …" → "needs …": the step travels as `step_id`, so the editor
#: does not repeat it in front of every message.
_STEP_PREFIX = re.compile(r"^step '[^']*'(?: \([^)]*\))?:?\s+")


def problem(step_id: Optional[str], field: Optional[str], detail: str) -> dict[str, Any]:
    """One thing wrong with a spec, said where it is.

    `detail` is the full sentence (what an exception or the chat agent says). `message` is the
    same sentence without the step prefix, for the editor, which already shows the step.
    """
    detail = str(detail).strip()
    short = _STEP_PREFIX.sub("", detail) if step_id else detail
    return {"step_id": step_id, "field": field,
            "message": (short[:1].upper() + short[1:]) if short else detail,
            "detail": detail}


def problems_sentence(problems: Iterable[dict[str, Any]]) -> str:
    """Every problem as the one sentence an exception carries."""
    return " ".join(p["detail"] for p in problems)


#: The comparisons `evaluate_expression` knows. Kept beside it in meaning: a new one is added
#: there and here together, or a spec the validator passes is refused by the runtime mid-run.
EXPRESSION_OPS = ("exists", "truthy", "eq", "ne", "in", "not_in", "contains",
                  "gt", "gte", "lt", "lte")
_LIST_OPS = {"in", "not_in"}
_VALUE_OPS = {"gt", "gte", "lt", "lte"}


def expression_problems(expression: Any) -> list[str]:
    """What `evaluate_expression` would refuse in this expression, said before a run hits it.

    A CONDITION THAT CANNOT BE READ USED TO PUBLISH. The validator only asked for an object, so
    `{}` — a Branch nobody had said anything about yet — saved cleanly and then stopped the run
    the moment it was reached ("expression.path is required"). The rules here are the
    evaluator's, read in the same order: `all`, then `any`, then `not`, then one comparison.
    Keys it does not read are left alone, so nothing that runs today is refused.
    """
    if not isinstance(expression, dict):
        return ["the condition must be an object."]
    for group in ("all", "any"):
        if group in expression:
            items = expression[group]
            if not isinstance(items, list) or not items:
                return [f"'{group}' must list at least one comparison."]
            found: list[str] = []
            for item in items:
                found.extend(expression_problems(item))
            return found
    if "not" in expression:
        return expression_problems(expression["not"])
    path = expression.get("path")
    if not isinstance(path, str) or not path.strip():
        return ["the condition does not say what to check: choose a value to compare."]
    op = str(expression.get("op", "truthy")).lower()
    if op not in EXPRESSION_OPS:
        known = ", ".join(EXPRESSION_OPS)
        return [f"'{op}' is not a comparison BOSS knows; use one of: {known}."]
    if op in _LIST_OPS and not isinstance(expression.get("value"), list):
        return [f"'{op}' compares against a list of values, and '{path}' has none."]
    if op in _VALUE_OPS and expression.get("value") is None:
        return [f"'{op}' needs a value to compare '{path}' against."]
    return []


#: FOR EACH (#512). How many items one for_each may go through, at most and by default, and how
#: many of its body runs may be live at once. A longer list is refused at run time, never cut.
FOR_EACH_MAX_ITEMS_LIMIT = 1000
FOR_EACH_DEFAULT_MAX_ITEMS = 100
FOR_EACH_MAX_CONCURRENCY = 20
#: Where `items` may read from: what the run was started with, what an earlier step produced, or
#: the run's merged context. The same paths a condition reads (`evaluate_expression`).
_ITEMS_PATH = re.compile(r"^(input|steps|context)(\.[^.\s]+)+$")
#: Stamped on a body run that failed for good: its for_each failed because of it, and the run
#: stopped at the for_each (`_first_stopped_step` looks there, not here).
FOR_EACH_FAILED = "for_each_failed"


def for_each_bodies(spec: dict[str, Any]) -> dict[str, str]:
    """Every step some for_each repeats, to the for_each that repeats it."""
    return {str(s.get("body")): str(s.get("id")) for s in spec.get("steps") or []
            if isinstance(s, dict) and s.get("kind") == "for_each" and s.get("body")}


def _whole_number(raw: Any, default: int) -> Optional[int]:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return None
    try:
        as_number = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return int(as_number) if as_number == int(as_number) else None


def _for_each_shape(step: dict[str, Any], bad: Callable[[Optional[str], str], None]) -> None:
    """A for_each's own fields, normalised in place, each mistake said on its field."""
    sid = step["id"]
    if not str(step.get("body") or "").strip():
        bad("body", f"for_each step '{sid}' body is required: pick the step to run for each "
                    "item.")
    maximum = _whole_number(step.get("max_items"), FOR_EACH_DEFAULT_MAX_ITEMS)
    if maximum is None or maximum < 1 or maximum > FOR_EACH_MAX_ITEMS_LIMIT:
        bad("max_items", f"for_each step '{sid}' max_items must be a whole number between 1 "
                         f"and {FOR_EACH_MAX_ITEMS_LIMIT}.")
        maximum = None
    else:
        step["max_items"] = maximum
    at_once = _whole_number(step.get("concurrency"), 1)
    if at_once is None or at_once < 1 or at_once > FOR_EACH_MAX_CONCURRENCY:
        bad("concurrency", f"for_each step '{sid}' concurrency must be a whole number between "
                           f"1 and {FOR_EACH_MAX_CONCURRENCY}.")
    else:
        step["concurrency"] = at_once
    items = step.get("items")
    if isinstance(items, list):
        if maximum is not None and len(items) > maximum:
            bad("items", f"for_each step '{sid}' lists {len(items)} items and goes through at "
                         f"most {maximum} (max_items).")
        return
    if isinstance(items, str) and _ITEMS_PATH.fullmatch(items.strip()):
        step["items"] = items.strip()
        return
    bad("items", f"for_each step '{sid}' items must say which list to go through: a path such "
                 "as input.urls or steps.<step>.output.<field>, or a list.")


def _items_source(step: dict[str, Any]) -> Optional[str]:
    """The step a for_each's `items` path reads from, if it reads one."""
    items = step.get("items")
    if isinstance(items, str) and items.startswith("steps."):
        return items.split(".")[1]
    return None


def _reads_item(value: Any) -> bool:
    return isinstance(value, dict) and ("from_item" in value or "from_item_index" in value)


def _for_each_graph_problems(
    normalized_steps: list[dict[str, Any]], steps: dict[str, dict[str, Any]],
    loop_bodies: dict[str, str], each_bodies: dict[str, str],
) -> list[dict[str, Any]]:
    """What a for_each needs of the steps around it, once every step id is known.

    ITS BODY IS ITS OWN, AND ONE STEP. The runtime starts the body once per item and returns each
    run to the for_each (`_advance_for_each`), so the body does work (not control flow), has no
    `next` of its own, and nothing else leads to it. A loop cannot repeat a for_each yet: each
    item is a step run numbered by its index, and a second round would reuse those numbers.
    """
    found: list[dict[str, Any]] = []
    for step in normalized_steps:
        sid, kind = str(step["id"]), step["kind"]
        if kind == "for_each":
            body = str(step.get("body") or "")
            target = steps.get(body)
            if body == sid:
                found.append(problem(sid, "body", f"for_each step '{sid}' cannot repeat "
                                                  "itself; pick the step to run for each item."))
            elif target is not None and target["kind"] in CONTROL_KINDS:
                found.append(problem(sid, "body", (
                    f"for_each step '{sid}' repeats a {target['kind']} step '{body}'. It repeats "
                    "one step that does work: pick an agent task or a capability.")))
            source = _items_source(step)
            if source is not None and source not in steps:
                found.append(problem(sid, "items", (
                    f"for_each step '{sid}' items reads step '{source}', which is not in this "
                    "workflow.")))
        if kind == "bounded_loop" and steps.get(str(step.get("body") or ""), {}).get(
                "kind") == "for_each":
            found.append(problem(sid, "body", (
                f"bounded_loop step '{sid}' repeats for_each step '{step['body']}', and a loop "
                "cannot repeat a for_each yet. Put the for_each before or after the loop.")))
        if sid in each_bodies:
            if step.get("next"):
                found.append(problem(sid, "next", (
                    f"step '{sid}' is the step for_each '{each_bodies[sid]}' repeats, so it "
                    "returns to the for_each after each item and cannot have next steps. Put "
                    f"what comes after on '{each_bodies[sid]}'.")))
        else:
            inputs = (step.get("binding") or {}).get("inputs") or {}
            for name, value in (inputs.items() if isinstance(inputs, dict) else ()):
                if _reads_item(value):
                    found.append(problem(sid, str(name), (
                        f"step '{sid}' reads the item of a for_each in {name}, and only the "
                        "step a for_each repeats has one.")))
        for target in _success_targets(step):
            if target in each_bodies and each_bodies[target] != sid:
                found.append(problem(sid, "next", (
                    f"step '{sid}' leads to step '{target}', the step for_each "
                    f"'{each_bodies[target]}' repeats. Only its for_each may start it.")))
    return found


def normalise_workflow_spec(
    spec: Any,
    roster: Optional[Iterable[str]] = None, *,
    check_agents: bool = True,
    require_gate_question: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The spec as the runtime stores it, and EVERYTHING wrong with it.

    Every problem at once, each with the step and field it is about, because a builder that
    surfaces one error at a time makes a customer fix a workflow five times to publish it once.
    `validate_workflow_spec` raises these; `spec_problems` adds what needs the project. The
    normalised spec is best effort when there are problems and must not be published.

    `require_gate_question` is the editor's rule: a gate a person will be asked has to say what
    it asks. Off for BOSS's own workflows, which compose a question when the gate opens.
    """
    problems: list[dict[str, Any]] = []
    if not isinstance(spec, dict):
        return {}, [problem(None, None, "workflow spec must be an object.")]
    raw_steps = spec.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return dict(spec), [problem(None, "steps",
                                    "workflow spec must contain at least one step.")]

    normalized_steps: list[dict[str, Any]] = []
    ids: set[str] = set()
    # Whether the graph can be walked. A step with no usable id, kind or `next` makes edges
    # meaningless, so the reachability checks wait until those are fixed.
    walkable = True
    for raw in raw_steps:
        if not isinstance(raw, dict):
            problems.append(problem(None, None, "every workflow step must be an object."))
            walkable = False
            continue
        step = dict(raw)
        try:
            step_id = _clean_text(step.get("id"), "step.id")
        except WorkflowValidationError as error:
            problems.append(problem(None, "id", str(error)))
            walkable = False
            continue
        if not _STEP_ID.fullmatch(step_id):
            problems.append(problem(step_id, "id", (
                f"step id '{step_id}' must use letters, numbers, dot, underscore, or dash.")))
            walkable = False
            continue
        if step_id in ids:
            problems.append(problem(step_id, "id", f"duplicate workflow step id '{step_id}'."))
            walkable = False
            continue
        ids.add(step_id)

        def bad(field: Optional[str], detail: str, _id: str = step_id) -> None:
            problems.append(problem(_id, field, detail))

        try:
            kind = _clean_text(step.get("kind"), f"step '{step_id}' kind").lower()
        except WorkflowValidationError as error:
            bad("kind", str(error))
            walkable = False
            continue
        if kind not in WORKFLOW_KINDS:
            bad("kind", f"step '{step_id}' kind must be one of: "
                        f"{', '.join(sorted(WORKFLOW_KINDS))}.")
            walkable = False
            continue
        step["id"] = step_id
        step["kind"] = kind
        # WHAT THE OPERATOR CALLS IT. Optional, set by the editor, and the one name the canvas,
        # the plan and a run all show. Absent unless somebody wrote one: a standard saved as a
        # copy with no edits must come back exactly as it went in.
        if "title" in step:
            title = step["title"]
            if title is None or (isinstance(title, str) and not title.strip()):
                step.pop("title")
            elif not isinstance(title, str):
                bad("title", f"step '{step_id}' title must be text.")
            elif len(title.strip()) > STEP_TITLE_MAX:
                bad("title", f"step '{step_id}' title must be {STEP_TITLE_MAX} characters "
                             "or fewer.")
            else:
                step["title"] = title.strip()
        try:
            attempts = int(step.get("max_attempts", 3))
        except (TypeError, ValueError):
            attempts = 0
        if attempts < 1 or attempts > 25:
            bad("max_attempts", f"step '{step_id}' max_attempts must be between 1 and 25.")
        else:
            step["max_attempts"] = attempts
        # ERROR HANDLING (#512): how long between automatic attempts, how long one attempt may
        # run, and where the run goes when the step fails for good. All optional, and a step
        # without them behaves exactly as before. An unset value (null, or an empty on_error) is
        # the field left out, so an editor can clear one by sending it empty.
        for field in ("retry_backoff", "timeout_seconds", "on_error"):
            if field in step and (step[field] is None or (
                    isinstance(step[field], str) and not step[field].strip())):
                step.pop(field)
        handled = [field for field in ("retry_backoff", "timeout_seconds", "on_error")
                   if field in step]
        if handled and kind in CONTROL_KINDS:
            # Only a step a worker runs has attempts: a condition, a gate or a timer never fails
            # through `fail_step`, so these would be promises nothing keeps.
            for field in handled:
                bad(field, f"step '{step_id}' is a {kind} step, which has no attempts of its "
                           f"own, so it cannot have {field}. Put it on a step that does work.")
                # Said once; not walked as an edge, so it is not refused twice over.
                step.pop(field)
        else:
            if "retry_backoff" in step:
                backoff, details = _backoff_problems(step_id, step["retry_backoff"])
                for detail in details:
                    bad("retry_backoff", detail)
                if backoff is not None:
                    step["retry_backoff"] = backoff
            if "timeout_seconds" in step:
                raw_timeout = step["timeout_seconds"]
                try:
                    if isinstance(raw_timeout, bool):
                        raise TypeError
                    as_number = float(raw_timeout)
                    timeout = int(as_number) if as_number == int(as_number) else 0
                except (TypeError, ValueError, OverflowError):
                    timeout = 0
                if timeout < 1 or timeout > STEP_TIMEOUT_MAX_SECONDS:
                    bad("timeout_seconds", f"step '{step_id}' timeout_seconds must be a whole "
                                           "number of seconds between 1 and "
                                           f"{STEP_TIMEOUT_MAX_SECONDS}.")
                else:
                    step["timeout_seconds"] = timeout
            if "on_error" in step:
                if not isinstance(step["on_error"], str):
                    bad("on_error", f"step '{step_id}' on_error must name the step to go on to "
                                    "when it fails.")
                    step.pop("on_error")
                else:
                    step["on_error"] = step["on_error"].strip()
        if "next" in step and isinstance(step["next"], str):
            step["next"] = [step["next"]]
        elif "next" not in step:
            step["next"] = []
        elif not isinstance(step["next"], list):
            bad("next", f"step '{step_id}' next must be a list.")
            walkable = False

        binding = step.get("binding") or {}
        if not isinstance(binding, dict):
            bad("binding", f"step '{step_id}' binding must be an object.")
            normalized_steps.append(step)
            continue
        step["binding"] = binding
        # WHO RUNS IT, FOR EVERY KIND OF STEP — the part that needs no project to see: `human`
        # only on a gate, a name that is an agent key, and skills/MCP lists only where something
        # reads them. Whether the named agent EXISTS, and whether it holds what the step needs,
        # is `step_agents.assignment_problems`, run at publish and at start with the project in
        # hand. That used to be a hard-coded set of role keys checked here, which refused every
        # agent made in Agent Studio.
        captain_shape = step_agent_captain_problems(step) if check_agents else []
        for detail in captain_shape:
            bad("captain", detail)
        if check_agents:
            for field, detail in step_agent_scope_problems(step):
                bad(field, detail)

        def captain_problems() -> None:
            # The shape was said above; saying it again from validate_captain would double it.
            if check_agents and not captain_shape:
                for detail in validate_captain(step, roster):
                    bad("captain", detail)

        def approval_mode() -> None:
            approval = dict(step.get("approval") or {})
            mode = str(approval.get("mode", "auto")).lower()
            if mode not in ("auto", "manual"):
                bad("approval", f"step '{step_id}' approval mode must be 'auto' or 'manual'.")
                return
            # AUTO DOES NOT MEAN UNATTENDED. The agent's own loop suspends on any skill at or
            # above the risk floor, so a step that reaches a tier-3 publish stops for a person
            # whatever this says. Recorded as asked rather than rewritten, because the gate that
            # fires is the skill's, not the step's.
            approval["mode"] = mode
            approval.setdefault("on_timeout", "block")
            step["approval"] = approval

        if kind == "capability" and binding.get("agent_work"):
            # A STEP WHOSE SPECIFICATION IS A SENTENCE. It binds no capability and no custom
            # step, so neither of the checks below applies — what it needs is an instruction and,
            # optionally, an agent. See services/agent_steps.py for why this shape exists.
            wiring = binding.get("inputs") or {}
            if not isinstance(wiring, dict) or "instructions" not in wiring:
                bad("instructions", (
                    f"step '{step_id}' must wire 'instructions' — that is the whole of what it "
                    "tells the agent to do. Read it from the run's input, or pin a literal."))
            # WHETHER THE NAMED AGENT EXISTS is checked here against the project's roster
            # (agents stream); whether it HOLDS what the step is scoped to is
            # `step_agents.assignment_problems` at publish and at start (steps stream). Both
            # fail closed, because a step given to an agent that can do nothing wastes a run.
            captain_problems()
            approval_mode()
        elif kind == "capability" and binding.get("typed_decision"):
            # A JUDGEMENT WITH ITS ANSWERS NAMED UP FRONT. The step asks one question about what
            # it is wired, a decision model picks one of the options, and an ordinary `condition`
            # routes on `steps.<id>.output.answer`. See services/typed_decisions.py.
            for detail in typed_decision_problems(step_id, binding):
                bad("binding", detail)
            # Nothing to approve: it reads and answers, it acts on nothing. What acts is the
            # branch it selects, and that branch carries its own gate.
            step["approval"] = {"mode": "auto", "on_timeout": "block"}
        elif kind == "capability" and binding.get("custom_step"):
            # A step the customer defined. It cannot be checked here — whether the skill it
            # binds still exists, and what risk tier it really carries, are facts about the
            # project, not about the spec. `spec_problems` does it with a connection in hand;
            # this pass only confirms the shape.
            if not str(binding.get("custom_step") or "").strip():
                bad("binding", f"step '{step_id}' does not say which custom step it runs.")
            # A CUSTOM STEP MAY NAME AN AGENT TOO, and until now only the built-in capabilities
            # had their captain checked — so a typo in a role on a customer's step published
            # cleanly and was denied at run time, twenty minutes into a workflow, as
            # "caller 'vidoe_editor' not in allowed_callers". Who may RUN it is
            # `step_agents.assignment_problems`.
            captain_problems()
            approval_mode()
        elif kind == "capability":
            # Every problem at once. A builder that surfaces one error at a time makes a
            # customer fix a workflow five times to publish it once.
            found = [
                (field, detail) for field, detail in capability_problems(
                    step if check_agents else {k: v for k, v in step.items() if k != "captain"},
                    roster if check_agents else None)
                if not (field == "captain" and captain_shape)
            ]
            for field, detail in found:
                bad(field, detail)
            if not found:
                # Store the EFFECTIVE approval mode, not the requested one: a plan that reads
                # "auto" beside a step that will stop and wait is a plan nobody can trust.
                step["approval"] = effective_approval_policy(
                    str(binding["capability"]), step.get("approval")
                )
        if kind == "skill" and not binding.get("skill_id"):
            bad("binding", f"skill step '{step_id}' requires binding.skill_id.")
        if kind == "skill" and step.get("approval"):
            # A customer's own step is under the same floor as a standard one. Without this,
            # "custom step" would be the documented way to deploy to production unattended.
            # The skill's real risk tier is checked against the project registry in
            # `_validate_bindings`, which is the only place that can see it.
            approval = dict(step["approval"])
            mode = str(approval.get("mode", "auto")).lower()
            if mode not in ("auto", "manual"):
                bad("approval", f"step '{step_id}' approval mode must be 'auto' or 'manual'.")
            else:
                approval["mode"] = mode
                step["approval"] = approval
        if kind == "integration" and not binding.get("integration_connection_id"):
            bad("binding",
                f"integration step '{step_id}' requires binding.integration_connection_id.")
        if kind == "agent_group" and not binding.get("agent_group_version_id"):
            bad("binding",
                f"agent_group step '{step_id}' requires binding.agent_group_version_id.")
        if kind == "subworkflow" and not binding.get("workflow_version_id"):
            bad("binding", f"subworkflow step '{step_id}' requires binding.workflow_version_id.")
        if kind == "condition":
            if not isinstance(step.get("expression"), dict):
                bad("expression",
                    f"condition step '{step_id}' requires a declarative expression.")
            else:
                for detail in expression_problems(step["expression"]):
                    bad("expression", f"condition step '{step_id}': {detail}")
            for branch in ("on_true", "on_false"):
                if not str(step.get(branch) or "").strip():
                    bad(branch, f"condition step '{step_id}' {branch} is required.")
                    walkable = False
        if kind == "parallel":
            branches = step.get("branches")
            if not isinstance(branches, list) or len(branches) < 2:
                bad("branches", f"parallel step '{step_id}' requires at least two branches.")
                walkable = walkable and isinstance(branches, list)
            if not str(step.get("join") or "").strip():
                bad("join", f"parallel step '{step_id}' join is required.")
        if kind == "join":
            wait_for = step.get("wait_for")
            if not isinstance(wait_for, list) or not wait_for:
                bad("wait_for", f"join step '{step_id}' requires wait_for.")
                step["wait_for"] = []
            if step.get("success_policy", "all") not in {"all", "any"}:
                bad("success_policy",
                    f"join step '{step_id}' success_policy must be all or any.")
        if kind == "bounded_loop":
            if not str(step.get("body") or "").strip():
                bad("body", f"bounded_loop step '{step_id}' body is required.")
            if not isinstance(step.get("expression"), dict):
                bad("expression",
                    f"bounded_loop step '{step_id}' requires a declarative expression.")
            else:
                for detail in expression_problems(step["expression"]):
                    bad("expression", f"bounded_loop step '{step_id}': {detail}")
            try:
                maximum = int(step.get("max_iterations", 0))
            except (TypeError, ValueError):
                maximum = 0
            if maximum < 1 or maximum > 100:
                bad("max_iterations", f"bounded_loop step '{step_id}' max_iterations must be "
                                      "between 1 and 100.")
            else:
                step["max_iterations"] = maximum
        if kind == "for_each":
            _for_each_shape(step, bad)
        if kind == "timer":
            try:
                seconds = int(step.get("wait_seconds", 0))
            except (TypeError, ValueError):
                seconds = -1
            if seconds < 0 or seconds > 31_536_000:
                bad("wait_seconds", f"timer step '{step_id}' wait_seconds must be between 0 "
                                    "and 31536000.")
            else:
                step["wait_seconds"] = seconds
        if kind == "decision":
            # A QUESTION IS WANTED. BOSS's own gates compose one when they open, from the
            # workflow and the step they follow. The editor asks for one and refuses a gate
            # without it (`require_gate_question`): the person answering it should be asked what
            # its author meant, not a sentence guessed from the step before.
            question = str(step.get("question") or "").strip()
            step["question"] = question[:400] if question else ""
            if require_gate_question and not question:
                bad("question", f"step '{step_id}' is an approval gate with no question. "
                                "Say what the person is approving.")
            # What the reviewer should look at. Carried as written; the gate joins it to
            # whatever the earlier steps produced when it opens.
            step["guidance"] = str(step.get("guidance") or "").strip()[:1000]
        if "compensation" in step and not isinstance(step["compensation"], dict):
            bad("compensation", f"step '{step_id}' compensation must be an object.")
        normalized_steps.append(step)

    entry: Optional[str] = None
    try:
        entry = _clean_text(spec.get("entry_step"), "entry_step")
    except WorkflowValidationError as error:
        problems.append(problem(None, "entry_step", str(error)))
    if entry is not None and entry not in ids and walkable:
        problems.append(problem(None, "entry_step", f"entry step '{entry}' does not exist."))
        entry = None
    try:
        input_schema = validate_input_schema(spec.get("input_schema"))
    except WorkflowValidationError as error:
        problems.append(problem(None, "input_schema", str(error)))
        input_schema = spec.get("input_schema") if isinstance(spec.get("input_schema"),
                                                               dict) else {}

    normalized = {
        **spec,
        "entry_step": entry if entry is not None else spec.get("entry_step"),
        "input_schema": input_schema,
        "output_schema": spec.get("output_schema") or {},
        "steps": normalized_steps,
    }
    if not walkable:
        return normalized, problems

    steps = _step_map(normalized)
    dangling = False
    for step in normalized_steps:
        for target in _success_targets(step):
            if target not in steps:
                dangling = True
                problems.append(problem(step["id"], "next", (
                    f"step '{step['id']}' references missing step '{target}'.")))
        if step["kind"] == "join":
            for dependency in step.get("wait_for") or []:
                if dependency not in steps:
                    dangling = True
                    problems.append(problem(step["id"], "wait_for", (
                        f"join step '{step['id']}' waits for missing step '{dependency}'.")))
    loop_bodies = {str(s.get("body")): str(s["id"]) for s in normalized_steps
                   if s["kind"] == "bounded_loop" and s.get("body")}
    each_bodies = for_each_bodies(normalized)
    for found in _for_each_graph_problems(normalized_steps, steps, loop_bodies, each_bodies):
        dangling = True
        problems.append(found)
    for step in normalized_steps:
        if not step.get("on_error") or step["kind"] in CONTROL_KINDS:
            continue
        sid, target = str(step["id"]), str(step["on_error"])
        refusal = None
        if target not in steps:
            refusal = (f"step '{sid}' on_error goes to step '{target}', which does not exist. "
                       "Pick a step in this workflow.")
        elif target == sid:
            refusal = (f"step '{sid}' on_error goes to itself. A step retries itself with "
                       "max_attempts; on_error must name another step.")
        elif entry is not None and target == entry:
            refusal = (f"step '{sid}' on_error goes back to the start step '{target}', which "
                       "would run the whole workflow again. Pick a step after the start.")
        elif steps[target]["kind"] == "join":
            refusal = (f"step '{sid}' on_error goes to join step '{target}'. A join waits for "
                       "its branches; send the error path to a step that does work.")
        elif target in loop_bodies:
            refusal = (f"step '{sid}' on_error goes to step '{target}', the body of loop "
                       f"'{loop_bodies[target]}'. Only its loop may start it.")
        elif sid in loop_bodies:
            refusal = (f"step '{sid}' is the body of loop '{loop_bodies[sid]}', and a loop "
                       "body cannot have an error path yet. Let it fail, or drop on_error.")
        elif target in each_bodies:
            refusal = (f"step '{sid}' on_error goes to step '{target}', the step for_each "
                       f"'{each_bodies[target]}' repeats. Only its for_each may start it.")
        elif sid in each_bodies:
            refusal = (f"step '{sid}' is the step for_each '{each_bodies[sid]}' repeats, and a "
                       "failed item fails its for_each, so it cannot have an error path. Drop "
                       "on_error; Retry on the for_each runs the failed items again.")
        if refusal:
            dangling = True
            problems.append(problem(sid, "on_error", refusal))
    if dangling or entry is None:
        return normalized, problems

    # Reachable along every edge, error paths included: a step only an error path leads to is
    # part of the workflow.
    reachable = _reachable_from(normalized, [entry])

    # Declared edges must be acyclic.  Repetition is represented only by a
    # bounded_loop controller, never by an arbitrary graph cycle.
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle: list[str] = []

    def visit(step_id: str) -> None:
        if cycle:
            return
        if step_id in visiting:
            cycle.append(step_id)
            return
        if step_id in visited:
            return
        visiting.add(step_id)
        for target in _success_targets(steps[step_id]):
            visit(target)
        visiting.remove(step_id)
        visited.add(step_id)

    for start in [entry] + sorted(reachable - {entry}):
        visit(start)
    if cycle:
        problems.append(problem(cycle[0], "next", (
            f"unbounded cycle detected at step '{cycle[0]}'; use bounded_loop.")))
        return normalized, problems
    # An error path back to a step that leads to the failing one is a cycle as well: every
    # failure would run that stretch again, with nothing bounding how often.
    looped = False
    for step in normalized_steps:
        sid, target = str(step["id"]), str(step.get("on_error") or "")
        if target and sid in reachable and sid in _reachable_from(normalized, [target]):
            looped = True
            problems.append(problem(sid, "on_error", (
                f"step '{sid}' on_error goes to step '{target}', which leads back to "
                f"'{sid}': an error path cannot loop. Use max_attempts to retry, or "
                "bounded_loop to repeat.")))
    if looped:
        return normalized, problems
    for step_id in sorted(ids - reachable):
        problems.append(problem(step_id, None, (
            f"step '{step_id}' cannot be reached from the entry step: no step leads to it.")))
    return normalized, problems


def _runs_before(normalized: dict[str, Any]) -> dict[str, set[str]]:
    """For each reachable step, the steps that have certainly finished when it starts.

    An ancestor on EVERY path, not merely on some: after a condition only what came before the
    condition is certain. Parallel branches all run, so a join that waits for all of them has
    every branch behind it (and one that releases on any has only what the branches share). A
    loop body is not counted after its loop, nor is a branch counted at its parallel's own
    `join` edge, which only says where the branches meet.
    """
    steps = _step_map(normalized)
    preds: dict[str, list[str]] = {sid: [] for sid in steps}
    for sid, step in steps.items():
        skip = str(step.get("join") or "") if step.get("kind") == "parallel" else ""
        for target in _targets(step):
            if target in preds and target != skip:
                preds[target].append(sid)
    for sid, step in steps.items():
        # A join no branch leads to explicitly still follows its parallel step.
        if step.get("kind") == "parallel" and str(step.get("join") or "") in preds:
            join = str(step["join"])
            if not preds[join]:
                preds[join].append(sid)
    entry = str(normalized.get("entry_step") or "")
    if entry not in steps:
        return {}
    reachable, frontier = {entry}, [entry]
    while frontier:
        for target in _targets(steps[frontier.pop()]):
            if target in steps and target not in reachable:
                reachable.add(target)
                frontier.append(target)
    before: dict[str, set[str]] = {entry: set()}
    pending = [sid for sid in steps if sid in reachable and sid != entry]
    # Settled in dependency order. A cycle (refused elsewhere) leaves its steps unsettled.
    while pending:
        progressed = False
        for sid in list(pending):
            incoming = [p for p in preds[sid] if p in reachable]
            if not incoming or any(p not in before for p in incoming):
                continue
            # Reached only by p's error path, p did not finish: it failed. What came before
            # p is certain; p's own output is not.
            sets = [before[p] | ({p} if sid in _success_targets(steps[p]) else set())
                    for p in incoming]
            step = steps[sid]
            if step.get("kind") == "join" and step.get("success_policy", "all") == "all":
                before[sid] = set().union(*sets)
            else:
                before[sid] = set.intersection(*sets)
            pending.remove(sid)
            progressed = True
        if not progressed:
            break
    return before


def _capability_optional_inputs(step: dict[str, Any]) -> frozenset[str]:
    """A capability step's optional inputs, the set the runner's `_resolve_inputs` is given."""
    from .workflow_capabilities import CAPABILITIES

    capability = CAPABILITIES.get(str((step.get("binding") or {}).get("capability") or ""))
    return frozenset(capability.optional_inputs or ()) if capability else frozenset()


def binding_order_problems(
    normalized: dict[str, Any],
    optional_inputs: Optional[Callable[[dict[str, Any]], frozenset[str]]] = None,
) -> list[dict[str, Any]]:
    """A step that reads another step's output must come after it, on every path it needs it.

    Dragging Preview above Build left Preview reading Build's run id: Check said Ready and Save
    would publish a workflow whose second step waits on output that cannot exist yet. One
    problem per input, on the step that reads it and the input that is wired wrong.

    AN OPTIONAL INPUT MAY READ FROM A STEP THAT DOES NOT ALWAYS RUN. The runner leaves it out
    when its source did not run (`_resolve_inputs`) — the panel reading research that sits
    behind "do we have research?". Only a required input is refused for that. Reading a step
    that runs after it, or one not in the workflow, is wrong whatever the input is.

    `optional_inputs(step)` is the step's optional set — the same one the runner uses. Without
    it, capability steps use the catalogue's and every other input counts as required.
    """
    optional_for = optional_inputs or _capability_optional_inputs
    steps = _step_map(normalized)
    before = _runs_before(normalized)
    found: list[dict[str, Any]] = []
    for sid, step in steps.items():
        if sid not in before:
            continue  # unreachable: said already, and that is the thing to fix
        source = _items_source(step) if step.get("kind") == "for_each" else None
        if source and source in steps and source not in before[sid]:
            label = _step_label(steps[source], source)
            when = ("runs after it" if source == sid or sid in before.get(source, set())
                    else "does not always run before it")
            found.append(problem(sid, "items", (
                f"for_each step '{sid}' goes through a list from “{label}”, which {when}.")))
        inputs = (step.get("binding") or {}).get("inputs") or {}
        if not isinstance(inputs, dict):
            continue
        for name, value in inputs.items():
            if not isinstance(value, dict) or "from_step" not in value:
                continue
            source = str(value.get("from_step") or "")
            if source in before[sid]:
                continue
            if source not in steps:
                detail = (f"step '{sid}' gets {name} from step '{source}', "
                          "which is not in this workflow.")
            elif source == sid:
                detail = f"step '{sid}' gets {name} from itself."
            elif sid in before.get(source, set()):
                label = _step_label(steps[source], source)
                detail = f"step '{sid}' gets {name} from “{label}”, which runs after it."
            elif str(name) in optional_for(step):
                continue  # left out at run time when its source did not run
            else:
                label = _step_label(steps[source], source)
                detail = (f"step '{sid}' gets {name} from “{label}”, "
                          "which does not always run before it.")
            found.append(problem(sid, str(name), detail))
    return found


def validate_workflow_spec(spec: dict[str, Any],
                           roster: Optional[Iterable[str]] = None, *,
                           check_agents: bool = True) -> dict[str, Any]:
    """Normalise a spec, refusing anything the runtime would misread.

    `roster` is the project's agents (project_roster); with it a named agent must be one of
    them. Without it only the step-agent SHAPE is checked (`human` only on a gate, scopes only on
    agent steps) — whether a named agent exists and holds what the step needs is then
    `step_agents.assignment_problems`, run at publish and at start with the project in hand.

    `check_agents=False` skips the step-agent rules altogether. Those rules are new. A version
    published before them carries no `step_agent_rules` stamp, and is started and run under the
    rules it was published under — see `_published_spec`. Publishing always applies them.

    Raises with every problem, not the first; `error.problems` carries them one per field.
    """
    normalized, problems = normalise_workflow_spec(spec, roster, check_agents=check_agents)
    if problems:
        raise WorkflowValidationError(problems_sentence(problems), problems=problems)
    return normalized


_PROBLEM_STEP = re.compile(r"^step '([^']+)'")


def _agent_problem(detail: str, steps: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """An `assignment_problems` sentence, placed on its step and the control that fixes it.

    Every sentence that function writes opens with "step '<id>'" — the step is read from there,
    and the field from what the step is: its skills or MCP list when that is what was named
    wrongly, otherwise the agent picker.
    """
    match = _PROBLEM_STEP.match(detail)
    step_id = match.group(1) if match and match.group(1) in steps else None
    if step_id is None:
        return problem(None, None, detail)
    if " is scoped to " in detail:
        field = "skills"
    elif "names MCP server" in detail or "holds MCP server" in detail:
        field = "mcp"
    else:
        field = "captain"
    return problem(step_id, field, detail)


def spec_problems(
    connection: Connection, project_id: str, spec: Any, *,
    namespace: str = PRODUCT_NAMESPACE,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Everything that would stop this spec publishing in this project, and nothing else.

    THE ONE COLLECTOR. `publish_version` raises what this returns and the editor's Check shows
    it, so a spec Check passes is a spec Save publishes, and one it fails is refused by Save
    with the same problems. Two lists drifting apart is how a builder says "ready" and a save
    then fails.

    Returns (normalised spec, shape problems, binding problems). Shape problems need only the
    spec and the roster; binding problems need the project — a custom step's real risk tier, and
    whether the agent named on each step exists and holds what the step needs. A step with a
    shape problem is not asked the binding questions: its first problem is the one to fix.
    """
    product = namespace == PRODUCT_NAMESPACE
    normalized, shape = normalise_workflow_spec(
        spec, project_roster(connection, project_id), require_gate_question=product)
    # A step reading output from one that has not run yet. Here, in the one collector, so Check
    # shows it and Save refuses it with the same words.
    said = {(p["step_id"], p["field"]) for p in shape}
    def optional_for(step: dict[str, Any]) -> frozenset[str]:
        custom = (step.get("binding") or {}).get("custom_step")
        if custom:
            return custom_step_optional_inputs(connection, project_id, str(custom))
        return _capability_optional_inputs(step)

    shape = shape + [p for p in binding_order_problems(normalized, optional_for)
                     if (p["step_id"], p["field"]) not in said]
    steps = [s for s in normalized.get("steps") or [] if isinstance(s, dict) and s.get("id")]
    flagged = {p["step_id"] for p in shape if p["step_id"]}
    binding: list[dict[str, Any]] = []
    # Custom steps first, and only the customer's: their risk tier comes from the skill or MCP
    # server they bind, so this is the first moment the real tier is knowable — and the builder
    # is where the customer can still do something about it. BOSS's own definitions are
    # governed by the machinery around them (see `_validate_bindings`).
    if product:
        for step in steps:
            if step["id"] in flagged or not (step.get("binding") or {}).get("custom_step"):
                continue
            for field, detail in defined_step_field_problems(connection, project_id, step):
                binding.append(problem(step["id"], field, detail))
        flagged |= {p["step_id"] for p in binding}
    # WHO RUNS EACH STEP, settled while the operator is still in the builder: a named agent this
    # project has, holding what its step is scoped to, or — where nobody is named — at least one
    # agent that may run it. After the risk checks above, because "this needs a person" is the
    # more fundamental thing to fix. See services/step_agents.py.
    clean = [s for s in steps if s["id"] not in flagged]
    if clean:
        by_id = {s["id"]: s for s in clean}
        binding.extend(
            _agent_problem(detail, by_id)
            for detail in step_agent_problems(connection, project_id, {**normalized,
                                                                       "steps": clean}))
    return normalized, shape, binding


def check_spec(
    connection: Connection, project_id: str, spec: Any, *,
    namespace: str = PRODUCT_NAMESPACE,
) -> dict[str, Any]:
    """What Save would say, without saving: every problem, and the plan in numbers.

    Writes nothing. The summary is read from the spec as written, so the editor's chips agree
    with the steps on screen rather than with the version last published.
    """
    from .workflow_readiness import requirements

    normalized, shape, binding = spec_problems(connection, project_id, spec,
                                               namespace=namespace)
    # WHAT A STEP NEEDS SET UP IN SKILLS & MCP (skills-mcp spec §3.3): a built-in step whose
    # ability is not set up — `build` with no repo connected says "Needs Open pull requests set
    # up". A WARNING here, not a refusal: the workflow is fine to save, it just cannot run yet,
    # and the problem links to the ability.
    try:
        from ..config import get_settings
        from .kit import ability_problems

        with connection.begin_nested():
            needs = [{**problem(p["step_id"], "ability", p["message"]), "severity": "warning",
                      "ability": p["ability"], "fix_label": p.get("fix_label"), "fix_href": p.get("fix_href")}
                     for p in ability_problems(connection, get_settings(), project_id, normalized)]
    except Exception:  # noqa: BLE001 — the Kit must never be why a check fails
        logger.debug("ability problems could not be read", exc_info=True)
        needs = []
    problems = shape + binding + needs
    steps = [s for s in normalized.get("steps") or [] if isinstance(s, dict)]
    agents: list[str] = []
    for step in steps:
        named = step_named_agent(step)
        if named and named != "human" and named not in agents:
            agents.append(named)
    approvals = [str(s.get("id")) for s in steps
                 if s.get("kind") == "decision"
                 or str((s.get("approval") or {}).get("mode") or "").lower() == "manual"]
    try:
        minutes = capability_minutes({"steps": steps}) or None
    except Exception:  # noqa: BLE001 — an estimate must never be why a check fails
        minutes = None
    needs: list[str] = []
    try:
        for item in requirements({**normalized, "steps": steps},
                                 connection=connection, project_id=project_id):
            if item["key"] not in needs:
                needs.append(item["key"])
    except Exception:  # noqa: BLE001 — nor a needs list on a half-written spec
        logger.debug("needs of an unfinished spec could not be read", exc_info=True)
    return {
        "ok": not (shape + binding),
        "problems": problems,
        "summary": {"steps": len(steps), "agents": agents, "approvals": approvals,
                    "typical_minutes": minutes, "needs": needs},
    }


def _serialize_definition(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_key": row["workflow_key"],
        "name": row["name"],
        "description": row.get("description"),
        "status": row["status"],
        "namespace": row.get("namespace") or SYSTEM_NAMESPACE,
        "is_catalogue": bool(row.get("is_catalogue")),
        "forked_from_version_id": row.get("forked_from_version_id"),
        "active_version_id": row.get("active_version_id"),
        "created_by": row["created_by"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _serialize_version(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_definition_id": row["workflow_definition_id"],
        "version": int(row["version"]),
        "status": row["status"],
        "spec": _json(row["spec"], {}),
        "content_hash": row["content_hash"],
        "created_by": row["created_by"],
        "created_at": _iso(row["created_at"]),
        "published_at": _iso(row["published_at"]),
    }


def _serialize_mapping(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_version_id": row["workflow_version_id"],
        "selector_kind": row["selector_kind"],
        "selector_value": row["selector_value"],
        "scope_type": row["scope_type"],
        "scope_id": row["scope_id"],
        "priority": int(row["priority"]),
        "enabled": bool(row["enabled"]),
        "created_by": row["created_by"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def require_namespace(
    connection: Connection,
    project_id: str,
    definition_id: str,
    expected: str,
) -> dict[str, Any]:
    """Fetch a definition and refuse it if it belongs to the other tier.

    THE WHOLE POINT OF PHASE 0. Customer workflows and BOSS's own share this runtime because
    leasing, retries, evidence and approval are hard and already correct here. They must never
    share an editing surface: `governed_coding_pipeline` governs every build in the system, and
    a customer-facing builder that could reach it is one mis-scoped query away from rewriting
    the thing that reviews its own code.

    Called by every product-facing entry point. A guard that some callers remember is not a
    guard, so the test suite asserts the set of entry points that call it.
    """
    row = connection.execute(
        text("SELECT * FROM workflow_definitions WHERE id=:id AND project_id=:project_id"),
        {"id": definition_id, "project_id": project_id},
    ).mappings().first()
    if row is None:
        raise WorkflowNotFoundError(f"workflow definition '{definition_id}' was not found.")
    actual = row.get("namespace") or SYSTEM_NAMESPACE
    if actual != expected:
        raise WorkflowNamespaceError(
            f"workflow '{row['workflow_key']}' is a {actual} workflow and cannot be used "
            f"from the {expected} surface."
        )
    return dict(row)


def shipped_workflow_keys(workflow_root: Path) -> frozenset[str]:
    """The keys of the workflows BOSS ships as its own machinery (specpack/workflows/*.yaml)."""
    keys: set[str] = set()
    try:
        paths = sorted(Path(workflow_root).glob("*.yaml"))
    except OSError:
        return frozenset()
    for path in paths:
        try:
            source = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            source = None
        # Same rule as seed_specpack_workflows: the file's `id`, else its name.
        key = (str(source.get("id") or path.stem) if isinstance(source, dict)
               else path.stem).strip()
        if key:
            keys.add(key)
    return frozenset(keys)


def refuse_operator_edit_of_boss_workflow(definition: dict[str, Any], workflow_root: Path) -> None:
    """An operator surface may not publish a new version of BOSS's own workflows.

    The generic /v1/workflows/{id}/versions endpoint applied no namespace check, and Agent
    Studio's Flows tab put every definition — the governed coding pipeline, meeting consensus,
    the decision inbox, deployment, self-improvement — in a JSON editor with "Publish version".
    One paste could replace the pipeline that reviews every build. Only the shipped system
    workflows are protected here: a system-namespace definition somebody created through the
    same API (tests do) is theirs. BOSS's own seeding calls publish_version directly and is
    unaffected. None of these is editable from the console.
    """
    if str(definition.get("namespace") or SYSTEM_NAMESPACE) != SYSTEM_NAMESPACE:
        return
    key = str(definition.get("workflow_key") or "")
    if key in shipped_workflow_keys(workflow_root):
        # This used to say "fork it in Workflow > Catalogue". The catalogue lists the product
        # templates, not BOSS's own workflows, so the advice could not be followed.
        raise WorkflowNamespaceError(
            f"'{key}' is one of BOSS's own workflows and is not editable from the console — a "
            "change to it changes how BOSS itself works. To make something like it, start your "
            "own in Workflows > Library > + New workflow, or open a standard and Save as my copy."
        )


def delete_definition(
    connection: Connection, project_id: str, definition_id: str, actor: str
) -> dict[str, Any]:
    """Remove a customer's own workflow.

    "Build your own" without this is a list that only grows: every fork, every import and every
    mistake stays in the picker forever, and a project ends up offering several entries with the
    same name and no way to tell them apart. That is the state this was written in.

    THREE THINGS IT REFUSES, and each is a way to lose work rather than tidy it:

      * A catalogue workflow — the twelve BOSS ships are not the customer's to delete.
      * Anything outside the product namespace — same rule as every other product entry point.
      * A workflow with a RUN THAT IS STILL GOING. Deleting the definition under a live run
        strands it: the runtime reads the spec to decide what comes next, so it would be a run
        that cannot finish and cannot say why.

    Finished runs are kept and so are the versions they pin, because a run's history has to stay
    readable after the workflow it came from is gone. Only the definition and its mappings go —
    which is what stops it appearing in the picker.
    """
    definition = require_namespace(connection, project_id, definition_id, PRODUCT_NAMESPACE)
    require_editable(definition)

    live = connection.execute(
        text(
            """
            SELECT count(*) FROM workflow_runs
            WHERE workflow_definition_id = :id
              AND status IN ('running', 'waiting', 'queued', 'validating')
            """
        ),
        {"id": definition_id},
    ).scalar() or 0
    if int(live):
        raise WorkflowConflictError(
            f"'{definition['name']}' has {int(live)} run"
            f"{'' if int(live) == 1 else 's'} still going. Let them finish, or cancel them, "
            "before deleting it."
        )

    # The mapping goes either way: whatever happens to the definition, no new work should be
    # routed to it.
    connection.execute(
        text("DELETE FROM workflow_mappings WHERE workflow_version_id IN "
             "(SELECT id FROM workflow_versions WHERE workflow_definition_id=:id)"),
        {"id": definition_id},
    )

    # HAS IT EVER RUN?
    #
    # This decides between two genuinely different acts, and the database enforces the
    # difference: `workflow_versions` cascades from the definition, so deleting one that has
    # history would take the specs with it — and a finished run whose spec has vanished cannot
    # be read back. `workflow_runs` has no cascade at all, so the delete simply fails.
    #
    # Never run  -> a mistake, and removing it leaves nothing behind.
    # Has runs   -> RETIRED. It leaves the picker, its history stays readable.
    ever = connection.execute(
        text("SELECT count(*) FROM workflow_runs WHERE workflow_definition_id=:id"),
        {"id": definition_id},
    ).scalar() or 0

    if int(ever):
        connection.execute(
            text("UPDATE workflow_definitions SET status='retired', updated_at=now() "
                 "WHERE id=:id AND project_id=:project_id"),
            {"id": definition_id, "project_id": project_id},
        )
        outcome = "retired"
    else:
        connection.execute(
            text("DELETE FROM workflow_definitions WHERE id=:id AND project_id=:project_id"),
            {"id": definition_id, "project_id": project_id},
        )
        outcome = "deleted"

    emit_event(
        connection, project_id, f"workflow.definition_{outcome}", actor,
        "workflow_definition", definition_id,
        {"workflow_key": definition["workflow_key"], "name": definition["name"],
         "runs": int(ever)},
    )
    return {"outcome": outcome, "id": definition_id, "runs": int(ever),
            "workflow_key": definition["workflow_key"], "name": definition["name"]}


def require_editable(definition: dict[str, Any]) -> None:
    """Refuse to edit a catalogue workflow in place.

    The twelve BOSS ships are read-only on purpose: forking is what keeps "reset to the
    standard" meaningful and keeps the customer's diff against it visible. Editing in place
    would quietly make every fleet's "Standard Product Delivery" a different thing.
    """
    if definition.get("is_catalogue"):
        raise WorkflowNamespaceError(
            f"'{definition['workflow_key']}' is a standard workflow BOSS ships and cannot be "
            "edited in place. Save it as your copy to change it."
        )


def create_definition(
    connection: Connection,
    *,
    project_id: str,
    workflow_key: str,
    name: str,
    description: Optional[str],
    actor: str,
    namespace: str = SYSTEM_NAMESPACE,
    is_catalogue: bool = False,
    forked_from_version_id: Optional[str] = None,
    made_by_boss: bool = False,
) -> dict[str, Any]:
    """Create a workflow definition.

    `made_by_boss` records that BOSS itself made it (its seeding, or the worker), so the
    startup adoption of operator-made flows (adopt_operator_made_workflows) never moves it to
    the customer's namespace. It travels on the creation event: see BOSS_MADE_EVENT_FIELD.
    """
    if namespace not in NAMESPACES:
        raise WorkflowValidationError(
            f"namespace must be one of: {', '.join(sorted(NAMESPACES))}."
        )
    # A key or name another maker is choosing right now is not free yet: wait for it.
    hold_workflow_names(connection, project_id)
    definition_id = make_id("wfd")
    try:
        row = connection.execute(
            text(
                """
                INSERT INTO workflow_definitions (
                    id, project_id, workflow_key, name, description, status, created_by,
                    namespace, is_catalogue, forked_from_version_id
                ) VALUES (:id, :project_id, :workflow_key, :name, :description, 'active', :actor,
                          :namespace, :is_catalogue, :forked_from)
                RETURNING *
                """
            ),
            {
                "id": definition_id,
                "project_id": project_id,
                "workflow_key": _clean_text(workflow_key, "workflow_key"),
                "name": _clean_text(name, "name"),
                "description": description,
                "actor": _clean_text(actor, "actor"),
                "namespace": namespace,
                "is_catalogue": bool(is_catalogue),
                "forked_from": forked_from_version_id,
            },
        ).mappings().one()
    except Exception as error:
        if "workflow_definitions_project_id_workflow_key_key" in str(error):
            raise WorkflowConflictError(
                f"workflow key '{workflow_key}' already exists in this project."
            ) from error
        raise
    emit_event(
        connection,
        project_id,
        "workflow.definition_created",
        actor,
        "workflow_definition",
        definition_id,
        {"workflow_key": workflow_key, "name": name, "namespace": namespace,
         BOSS_MADE_EVENT_FIELD: bool(made_by_boss)},
    )
    return _serialize_definition(row)


def list_definitions(
    connection: Connection,
    project_id: str,
    *,
    include_retired: bool = False,
    namespace: Optional[str] = None,
) -> list[dict[str, Any]]:
    clause = "" if include_retired else "AND d.status <> 'retired'"
    if namespace is not None:
        if namespace not in NAMESPACES:
            raise WorkflowValidationError(
                f"namespace must be one of: {', '.join(sorted(NAMESPACES))}."
            )
        # COALESCE, because rows written before the column existed carry NULL until the default
        # is applied — and a system workflow invisible to the system list is worse than noise.
        clause += " AND COALESCE(d.namespace, 'system') = :namespace"
        if namespace == PRODUCT_NAMESPACE:
            # BOSS-owned customer work that is not a template: run like any product workflow
            # (the capability runner ticks product runs only), never offered in the Library.
            clause += " AND NOT (d.workflow_key = ANY(:hidden_keys))"
    rows = connection.execute(
        text(
            f"""
            SELECT d.*,
                   COALESCE((SELECT count(*) FROM workflow_versions v
                             WHERE v.workflow_definition_id=d.id), 0) AS version_count,
                   -- HOW BIG IT IS, so a workflow the operator made can be DRAWN and not
                   -- merely counted. The catalogue grid renders shipped entries as cards with
                   -- a step rail; the operator's own appeared nowhere but a dropdown, because
                   -- this list carried no steps to draw. Reported as: "the workflow itself
                   -- does not exist in Catalogue and how is it coming to Edit mode".
                   COALESCE((SELECT jsonb_array_length(v.spec->'steps')
                             FROM workflow_versions v
                             WHERE v.id = d.active_version_id), 0) AS step_count,
                   -- AND WHICH STEPS THEY ARE. A card reading "Steps 4" tells an operator how
                   -- big their workflow is and nothing about what it does, so the only way to
                   -- find out was to open the editor. The names are three fields away in the
                   -- spec that was already being measured here.
                   (SELECT v.spec->'steps' FROM workflow_versions v
                     WHERE v.id = d.active_version_id) AS step_specs,
                   (SELECT v.version FROM workflow_versions v
                     WHERE v.id = d.active_version_id) AS active_version,
                   -- WHAT STARTING IT WILL ASK FOR. The console's start form renders FROM
                   -- this; without it a workflow whose steps read `from_input` is startable
                   -- only by a caller that already knows the field names.
                   COALESCE((SELECT v.spec->'input_schema'
                             FROM workflow_versions v
                             -- Doubled braces: this SQL is an f-string ({clause} above).
                             WHERE v.id = d.active_version_id), '{{}}'::jsonb) AS input_schema,
                   -- WHAT IT IS A COPY OF, so the library can group copies under their source
                   -- and say "from Standard Product Delivery" instead of leaving fifty
                   -- "(copy)" rows that nobody can tell apart.
                   src.workflow_key AS forked_from_workflow_key,
                   src.name AS forked_from_name,
                   -- WHETHER IT HAS EVER RUN, which decides Delete (nothing to keep) versus
                   -- Retire (its history must stay readable) before the operator presses it.
                   (SELECT count(*) FROM workflow_runs r
                     WHERE r.workflow_definition_id = d.id) AS run_count,
                   last_run.status AS last_run_status,
                   last_run.created_at AS last_run_at,
                   -- The classes of work routed to it, so a card can say what it is used for.
                   ARRAY(SELECT DISTINCT m.selector_value FROM workflow_mappings m
                           JOIN workflow_versions mv ON mv.id = m.workflow_version_id
                          WHERE mv.workflow_definition_id = d.id
                            AND m.selector_kind = 'work_class' AND m.enabled
                          ORDER BY m.selector_value) AS used_for
            FROM workflow_definitions d
            LEFT JOIN workflow_versions srcv ON srcv.id = d.forked_from_version_id
            LEFT JOIN workflow_definitions src ON src.id = srcv.workflow_definition_id
            LEFT JOIN LATERAL (
                SELECT r.status, r.created_at FROM workflow_runs r
                 WHERE r.workflow_definition_id = d.id
                 ORDER BY r.created_at DESC LIMIT 1
            ) last_run ON TRUE
            WHERE d.project_id=:project_id {clause}
            ORDER BY d.updated_at DESC, d.workflow_key
            """
        ),
        {"project_id": project_id, **({"namespace": namespace} if namespace else {}),
         **({"hidden_keys": list(HIDDEN_PRODUCT_KEYS)}
            if namespace == PRODUCT_NAMESPACE else {})},
    ).mappings().all()
    # Labels for the customer's own steps, resolved once for the whole list rather than per
    # workflow: a project has a handful of them and every workflow draws from the same set.
    step_labels: dict[str, str] = {}
    if _table_exists(connection, "workflow_custom_steps"):
        step_labels = {
            r["step_key"]: r["label"] for r in connection.execute(
                text("SELECT step_key, label FROM workflow_custom_steps WHERE project_id=:p"),
                {"p": project_id},
            ).mappings()
        }

    items = []
    for row in rows:
        item = _serialize_definition(row)
        item["version_count"] = int(row["version_count"])
        item["step_count"] = int(row["step_count"] or 0)
        item["steps"] = _step_outline(row["step_specs"], step_labels)
        item["active_version"] = (
            int(row["active_version"]) if row["active_version"] is not None else None)
        # WHAT STARTING IT WILL ASK FOR. The query above already loads this from the live
        # version. Leaving it off the item made every workflow look like it declared nothing,
        # so "Start one" sent a sample brief instead of asking for the fields the steps read.
        schema = _json(row["input_schema"], {})
        item["input_schema"] = schema if isinstance(schema, dict) else {}
        # The library's own names for what is already here (WorkflowSummary in the console's
        # workflow-editor-types), so the card reads the row as it comes.
        item["definition_id"] = item["id"]
        item["version"] = item["active_version"]
        item["forked_from_workflow_key"] = row["forked_from_workflow_key"]
        item["forked_from_name"] = row["forked_from_name"]
        item["run_count"] = int(row["run_count"] or 0)
        item["last_run"] = ({"status": row["last_run_status"], "at": _iso(row["last_run_at"])}
                            if row["last_run_status"] else None)
        item["used_for"] = list(row["used_for"] or [])
        items.append(item)
    return items


def step_title(step: Any) -> str:
    """The name the operator gave a step on the canvas, or "" when they gave none.

    ONE NAME PER STEP. The builder, the plan and a run each derived their own label — the
    capability's, the custom step's, the id — so one step was called three things. A `title`
    on the step, when present, is what all of them show; without one each falls back as before.
    """
    title = step.get("title") if isinstance(step, dict) else None
    return title.strip() if isinstance(title, str) else ""


def _step_outline(specs: Any, step_labels: dict[str, str]) -> list[dict[str, Any]]:
    """Just enough of each step to DRAW a workflow rather than count it.

    Name, WHO RUNS IT, whether it is the customer's own, and whether it stops for a person —
    which is the set of facts somebody deciding whether to start a run actually needs. Not the
    bindings or the input wiring: those belong on the step itself, and a card that carried them
    would be a worse version of the editor.

    THE AGENT WAS MISSING AND IT IS THE FIRST THING PEOPLE LOOK FOR. The product's claim is that
    agents do the work and steps track it; a card that lists three step names and no agent
    invites exactly the question it was asked — "did you assign the agents or not?" — about a
    workflow where all three were assigned correctly.
    """
    from .step_agents import is_default_captain
    from .workflow_capabilities import CAPABILITY_KEYS, get as get_capability

    if not isinstance(specs, list):
        return []
    outline: list[dict[str, Any]] = []
    for step in specs:
        if not isinstance(step, dict):
            continue
        binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
        custom = str(binding.get("custom_step") or "")
        capability = str(binding.get("capability") or "")
        # The operator's own name for it where there is one: the title they gave the step,
        # else the label of their custom step. Falling back to the key beats falling back to
        # nothing: a step whose definition was deleted still has to appear.
        label = (step_title(step) or step_labels.get(custom) or custom or capability
                 or str(step.get("id") or ""))
        outline.append({
            "id": str(step.get("id") or ""),
            "label": label,
            # What the library card's mini-lane tooltips (WorkflowSummary.steps[].title): a name
            # a person reads, so a built-in step is its capability's label, never its key.
            "title": (step_title(step) or step_labels.get(custom)
                      or (get_capability(capability).label if capability in CAPABILITY_KEYS
                          else "")
                      or label),
            # Which built-in it runs, so the card can draw its icon; absent on every other kind.
            "capability": capability or None,
            "kind": str(step.get("kind") or ""),
            "mine": bool(custom),
            # WHO ANSWERS FOR IT. Empty is meaningful and differs by kind: on an ordinary step it
            # is the anonymous workflow runner; on an agent step it is "whichever agent fits",
            # which BOSS resolves per run. The console says which.
            "captain": str(step.get("captain") or "").strip(),
            # Only the catalogue's soft default, not somebody's choice: a start card may replace
            # it (step_agents.with_run_agent).
            "default_captain": is_default_captain(step),
            # A step that binds nothing and works from an instruction. Worth flagging on the card
            # because it is the one kind whose behaviour is not fixed by its name.
            "agent_work": bool(binding.get("agent_work")),
            "approval": str((step.get("approval") or {}).get("mode") or "auto"),
        })
    return outline


def get_definition(
    connection: Connection, project_id: str, definition_id: str
) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT * FROM workflow_definitions WHERE id=:id AND project_id=:project_id"),
        {"id": definition_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow definition not found.")
    versions = connection.execute(
        text(
            """
            SELECT * FROM workflow_versions
            WHERE workflow_definition_id=:definition_id
            ORDER BY version DESC
            """
        ),
        {"definition_id": definition_id},
    ).mappings().all()
    result = _serialize_definition(row)
    result["versions"] = [_serialize_version(version) for version in versions]
    return result


def list_definition_details(
    connection: Connection, project_id: str, *, include_retired: bool = False,
    namespace: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List full definitions without issuing one versions query per definition.

    `namespace` narrows it to one tier. A customer surface must pass PRODUCT_NAMESPACE: Agent
    Studio's Flows tab called this without it and offered BOSS's 32 system workflows — the
    governed coding pipeline among them — in a JSON editor with Publish.
    """
    definitions = list_definitions(
        connection, project_id, include_retired=include_retired, namespace=namespace,
    )
    if not definitions:
        return []
    versions = connection.execute(
        text(
            """
            SELECT * FROM workflow_versions
            WHERE project_id=:project_id
            ORDER BY workflow_definition_id, version DESC
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    versions_by_definition: dict[str, list[dict[str, Any]]] = {}
    for version in versions:
        versions_by_definition.setdefault(
            version["workflow_definition_id"], []
        ).append(_serialize_version(version))
    for definition in definitions:
        definition["versions"] = versions_by_definition.get(definition["id"], [])
    return definitions


#: Recorded on `workflow.definition_created`: true when BOSS itself made the definition.
BOSS_MADE_EVENT_FIELD = "made_by_boss"
#: The actors BOSS seeds its own workflows as (bootstrap, the API's boot seed, internal jobs).
#: An operator signs in as a member, never as one of these.
BOSS_ACTORS = frozenset({"bootstrap", "boss", "system"})


def adopt_operator_made_workflows(
    connection: Connection, project_id: str, workflow_root: Path, *, actor: str,
) -> list[str]:
    """Move workflows an operator made under the system namespace into the product one.

    Agent Studio's Flows tab used to create flows by posting /v1/workflows, which files a new
    definition under the SYSTEM namespace. When Flows was narrowed to the product namespace so
    an operator could no longer open the governed coding pipeline in a JSON editor, every flow
    made that older way vanished from Studio and from Workflow alike, with nothing to say where
    it went — and the product save path refused it even if it had been found.

    They are the customer's work, and refuse_operator_edit_of_boss_workflow already treats a
    system definition whose key BOSS does not ship as theirs. So they are moved to where the
    customer's work lives. Idempotent: once moved there is nothing left to move. BOSS's own
    workflows (specpack/workflows) are never touched, and a missing specpack moves nothing,
    because without it BOSS cannot tell its own workflows from the customer's.

    Returns the keys it moved.
    """
    shipped = shipped_workflow_keys(workflow_root)
    if not shipped:
        return []
    rows = connection.execute(
        text(
            """
            SELECT d.id, d.workflow_key, d.created_by,
                   EXISTS (SELECT 1 FROM events e
                           WHERE e.project_id = d.project_id
                             AND e.event_type = 'workflow.definition_created'
                             AND e.entity_id = d.id
                             AND e.data ->> :made_by_boss = 'true') AS made_by_boss
            FROM workflow_definitions d
            WHERE d.project_id = :p AND COALESCE(d.namespace, 'system') = :system
              AND COALESCE(d.is_catalogue, false) = false
            """
        ),
        {"p": project_id, "system": SYSTEM_NAMESPACE, "made_by_boss": BOSS_MADE_EVENT_FIELD},
    ).mappings().all()
    moved: list[str] = []
    for row in rows:
        key = str(row["workflow_key"] or "")
        if key in shipped:
            continue
        # BOSS'S OWN, EVEN WHEN ITS KEY IS NOT SHIPPED (review round 3). Since this runs at
        # every boot, it would otherwise hand the customer a definition the worker made under
        # system on purpose, and a workflow an earlier release shipped and a later one removed
        # from the specpack, as operator-editable flows in Studio. A definition made since this
        # change says so on its creation event; one seeded before it was made by a BOSS actor.
        if row["made_by_boss"] or str(row["created_by"] or "") in BOSS_ACTORS:
            continue
        connection.execute(
            text("UPDATE workflow_definitions SET namespace = :product, updated_at = now() "
                 "WHERE id = :id AND project_id = :p"),
            {"product": PRODUCT_NAMESPACE, "id": row["id"], "p": project_id},
        )
        emit_event(
            connection, project_id, "workflow.adopted_into_product", actor,
            "workflow_definition", str(row["id"]),
            {"workflow_key": key, "from": SYSTEM_NAMESPACE, "to": PRODUCT_NAMESPACE,
             "why": "made by an operator before Studio was limited to the customer's own "
                    "workflows; it is not one BOSS ships"},
        )
        moved.append(key)
    return moved


def adopt_operator_made_workflows_everywhere(
    connection: Connection, workflow_root: Path, *, actor: str = "boss",
) -> dict[str, list[str]]:
    """Run `adopt_operator_made_workflows` once for every project. Called at API startup.

    It used to run inside GET /v1/agent-studio/catalog (review round 2): a read wrote to the
    database for any caller, and flows came back in Workflow only after somebody had opened
    Studio. At startup it runs once per boot, before anyone reads, and is a no-op after the
    first. POST /v1/workflows no longer files a key BOSS does not ship under the system
    namespace, so nothing produces new ones; this moves the ones already there.
    """
    moved: dict[str, list[str]] = {}
    for project_id in connection.execute(text("SELECT id FROM projects")).scalars().all():
        keys = adopt_operator_made_workflows(connection, str(project_id), workflow_root,
                                             actor=actor)
        if keys:
            moved[str(project_id)] = keys
    return moved


def namespace_for_new_definition(workflow_key: str, workflow_root: Path, *,
                                 boss_itself: bool) -> str:
    """Where a definition made through the generic POST /v1/workflows belongs.

    The system namespace is BOSS's own machinery. A key BOSS ships, or a definition the worker
    (BOSS itself) makes, belongs there; anything else an operator makes is the customer's work
    and belongs in product, where Studio and Workflow list it and the product rules (custom
    step floor, save path) apply. Filing it under system is how the older Studio's flows
    vanished from both screens.
    """
    if boss_itself or workflow_key.strip() in shipped_workflow_keys(workflow_root):
        return SYSTEM_NAMESPACE
    return PRODUCT_NAMESPACE


def _hold_new_typed_decision_work(spec: dict[str, Any]) -> None:
    """Publishing is allowed. The spend hold is on the attempt, not the spec.

    run_step still refuses a call that has no committed reservation, lease, and
    receipt. Key proof stays on its own hold. The argument is accepted so older
    callers and tests can keep patching this function.
    """
    del spec


def publish_version(
    connection: Connection,
    *,
    project_id: str,
    definition_id: str,
    spec: dict[str, Any],
    actor: str,
    activate: bool = True,
) -> dict[str, Any]:
    # Before the roster is read: a publish must not land between an agent delete's check and
    # its commit (hold_workflow_reach).
    hold_workflow_reach(connection, project_id)
    definition = connection.execute(
        text("SELECT * FROM workflow_definitions WHERE id=:id AND project_id=:project_id FOR UPDATE"),
        {"id": definition_id, "project_id": project_id},
    ).mappings().first()
    if not definition:
        raise WorkflowNotFoundError("Workflow definition not found.")
    # THE SAME COLLECTOR THE EDITOR'S CHECK RUNS (`spec_problems`), so check-ok is publish-ok.
    # A spec that is malformed is refused as such; one that is well formed but cannot run in
    # this project — a custom step over the risk floor, an agent that holds nothing it needs —
    # is refused as blocked. Either way with every problem, one per step and field.
    normalized, shape, blocked = spec_problems(
        connection, project_id, spec,
        namespace=str(definition.get("namespace") or SYSTEM_NAMESPACE))
    if shape:
        raise WorkflowValidationError(problems_sentence(shape + blocked),
                                      problems=shape + blocked)
    if blocked:
        raise WorkflowBindingBlockedError(problems_sentence(blocked), problems=blocked)
    # STAMPED WITH THE AGENT RULES IT WAS CHECKED UNDER, so a start or a run months from now
    # applies those rules and not whatever this code says then. A step-level copy (which
    # `_run_spec` adds for the runner) is never trusted from a caller.
    normalized[AGENT_RULES_KEY] = CURRENT_AGENT_RULES
    for step in normalized["steps"]:
        step.pop(AGENT_RULES_KEY, None)
    _hold_new_typed_decision_work(normalized)

    digest = json_hash(normalized)
    existing = connection.execute(
        text(
            """
            SELECT * FROM workflow_versions
            WHERE workflow_definition_id=:definition_id AND content_hash=:content_hash
            """
        ),
        {"definition_id": definition_id, "content_hash": digest},
    ).mappings().first()
    if existing:
        result = _serialize_version(existing)
        result["duplicate"] = True
        # IDENTICAL TO AN OLDER VERSION IS NOT THE SAME AS IDENTICAL TO THE CURRENT ONE.
        #
        # Returning the match and stopping is right when it IS the active version: nothing to
        # do. When it is an older one, the operator has just submitted a spec that differs from
        # what is live and been told nothing changed — so reverting by re-saving a previous
        # configuration silently did nothing and left the version they were correcting in place.
        # Found by restoring a workflow after a bad save and watching the bad one stay active.
        #
        # Publishing the same content twice must not create two versions, so the match is
        # ACTIVATED rather than copied: the history stays honest and the intent is carried out.
        # A RETIRED MATCH IS PUBLISHED AGAIN. Versions are retired when an agent they named is
        # deleted (retire_versions). Re-saving that content passed the roster check just above,
        # so what it names exists again — making it active while it stayed retired would leave
        # the workflow pointing at a version `_version_row` refuses.
        if existing["status"] == "retired":
            connection.execute(
                text("UPDATE workflow_versions SET status = 'published' WHERE id = :id"),
                {"id": existing["id"]})
            result["status"] = "published"
            result["republished"] = True
        if str(definition.get("active_version_id") or "") != str(existing["id"]):
            connection.execute(
                text(
                    """
                    UPDATE workflow_definitions
                    SET active_version_id=:version_id, status='active', updated_at=now()
                    WHERE id=:definition_id AND project_id=:project_id
                    """
                ),
                {"version_id": existing["id"], "definition_id": definition_id,
                 "project_id": project_id},
            )
            result["reactivated"] = True
            emit_event(
                connection, project_id, "workflow.version_reactivated", actor,
                "workflow_version", str(existing["id"]),
                {"definition_id": definition_id, "version": existing["version"]},
            )
        return result
    next_version = int(
        connection.execute(
            text(
                "SELECT COALESCE(max(version), 0) + 1 FROM workflow_versions "
                "WHERE workflow_definition_id=:definition_id"
            ),
            {"definition_id": definition_id},
        ).scalar_one()
    )
    version_id = make_id("wfv")
    row = connection.execute(
        text(
            """
            INSERT INTO workflow_versions (
                id, project_id, workflow_definition_id, version, status,
                spec, content_hash, created_by
            ) VALUES (
                :id, :project_id, :definition_id, :version, 'published',
                CAST(:spec AS JSONB), :content_hash, :actor
            ) RETURNING *
            """
        ),
        {
            "id": version_id,
            "project_id": project_id,
            "definition_id": definition_id,
            "version": next_version,
            "spec": json.dumps(normalized),
            "content_hash": digest,
            "actor": actor,
        },
    ).mappings().one()
    if activate:
        connection.execute(
            text(
                """
                UPDATE workflow_definitions
                SET active_version_id=:version_id, status='active', updated_at=now()
                WHERE id=:definition_id
                """
            ),
            {"version_id": version_id, "definition_id": definition_id},
        )
    emit_event(
        connection,
        project_id,
        "workflow.version_published",
        actor,
        "workflow_definition",
        definition_id,
        {
            "workflow_version_id": version_id,
            "version": next_version,
            "content_hash": digest,
            "activated": activate,
        },
    )
    result = _serialize_version(row)
    result["duplicate"] = False
    return result


# ── The workflow editor: one atomic save ──────────────────────────────────────────────────────
#
# The console's editor saves through `publish_from_editor` and nothing else. Saving used to be
# two calls — create (or fork) a definition, then publish a version — so a save whose publish
# failed left an empty definition behind, and a repeated save of a standard minted another
# "(copy)" each time: one project had fifty-four of them. Here the definition and its first
# version are written together or not at all, a copy is updated rather than duplicated, and a
# name the operator already uses is refused before anything is written.
#
# NAMES ARE UNIQUE ACROSS WHAT THE LIBRARY LISTS: the operator's own live workflows AND the
# standards. A copy saved under its standard's name sits next to it as two rows nobody can tell
# apart. And the check is read-then-write, so every reader of the names and keys holds
# `_NAMES_LOCK` first (`hold_workflow_names`): two saves at once are serialised rather than both
# passing the check — or both deriving `race_x` and one failing on the database's unique key
# with a 409 that is not a name clash at all.

#: Held exclusively, until the transaction ends, by whatever reads names or keys to write one.
_NAMES_LOCK = "boss.workflow_names:"


def hold_workflow_names(connection: Connection, project_id: str) -> None:
    """Serialise naming in this project until the transaction ends (see _NAMES_LOCK).

    Taken by `free_workflow_key`, `refuse_name_in_use` and `create_definition`, so every maker
    — create, fork, the editor's save and rename, and a caller naming its own key — is
    covered without remembering to.
    Re-entrant: a transaction that already holds it takes it again at no cost.
    """
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                       {"k": _NAMES_LOCK + str(project_id)})


class WorkflowNameInUseError(WorkflowConflictError):
    """The operator already has a workflow by that name. Carries a free one to offer."""

    def __init__(self, name: str, suggested_name: str) -> None:
        super().__init__(f"'{name}' is already the name of one of your workflows.")
        self.name = name
        self.suggested_name = suggested_name


def free_workflow_key(connection: Connection, project_id: str, name: str) -> str:
    """A key derived from the name, so a customer never has to invent an identifier.

    Shared by every way a workflow is made — create, import, fork and the editor's save. Two
    callers deriving keys their own way is how one of them ends up 409ing on a collision the
    other would have avoided — which is exactly what the first version of forking did.
    """
    from uuid import uuid4

    # Before reading what is taken: the key is only free until the caller's insert commits.
    hold_workflow_names(connection, project_id)
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_") or "workflow"
    taken = {
        row[0] for row in connection.execute(
            text("SELECT workflow_key FROM workflow_definitions WHERE project_id=:p"),
            {"p": project_id},
        )
    }
    key = base
    for suffix in range(2, 500):
        if key not in taken:
            return key
        key = f"{base}_{suffix}"
    return f"{base}_{uuid4().hex[:6]}"


def _library_names(connection: Connection, project_id: str,
                   exclude_id: Optional[str] = None) -> dict[str, bool]:
    """Every name the library lists, folded for comparison -> whether it is a standard's.

    The operator's own live workflows and the standards: a copy named like its standard is as
    indistinguishable as two copies named alike. A retired workflow has left the library, and
    BOSS's own workflows are never listed there.
    """
    rows = connection.execute(
        text("SELECT name, COALESCE(is_catalogue, false) AS standard "
             "FROM workflow_definitions "
             "WHERE project_id = :p AND COALESCE(namespace, 'system') = 'product' "
             "AND status <> 'retired' "
             "AND (CAST(:exclude AS TEXT) IS NULL OR id <> :exclude)"),
        {"p": project_id, "exclude": exclude_id},
    ).mappings().all()
    names: dict[str, bool] = {}
    for row in rows:
        folded = str(row["name"]).strip().casefold()
        names[folded] = names.get(folded, False) or bool(row["standard"])
    return names


def refuse_name_in_use(connection: Connection, project_id: str, name: str,
                       exclude_id: Optional[str] = None) -> None:
    """Refuse a name the library already lists, offering a free one.

    Two workflows with one name is how a library ends up listing several entries nobody can
    tell apart — a standard and a copy of it as much as two copies. Case and surrounding space
    do not make a name different. A standard's name is offered back as `<name> (mine)`, the
    name the editor suggests for a copy; any other as `<name> 2`.
    """
    hold_workflow_names(connection, project_id)
    taken = _library_names(connection, project_id, exclude_id)
    folded = name.strip().casefold()
    if folded not in taken:
        return
    if taken[folded]:
        base = f"{name.strip()} (mine)"
        if base.casefold() not in taken:
            raise WorkflowNameInUseError(name, base)
    else:
        base = re.sub(r"\s+\d+$", "", name.strip()) or name.strip()
    for number in range(2, 500):
        candidate = f"{base} {number}"
        if candidate.casefold() not in taken:
            raise WorkflowNameInUseError(name, candidate)
    raise WorkflowNameInUseError(name, f"{base} {make_id('n')[-4:]}")


def rename_definition(
    connection: Connection, project_id: str, definition_id: str, *, actor: str,
    name: Optional[str] = None, description: Optional[str] = None,
) -> dict[str, Any]:
    """Change what one of the operator's workflows is called, or its description.

    No new version: a name is not part of what runs, so renaming must not make running work
    look as though its workflow changed under it. Refuses a standard and BOSS's own, like every
    other edit, and a name the operator already uses.
    """
    definition = require_namespace(connection, project_id, definition_id, PRODUCT_NAMESPACE)
    require_editable(definition)
    changes: dict[str, Any] = {}
    if name is not None:
        cleaned = _clean_text(name, "name")
        if cleaned != definition["name"]:
            refuse_name_in_use(connection, project_id, cleaned, exclude_id=definition_id)
            changes["name"] = cleaned
    if description is not None:
        cleaned_description = str(description).strip() or None
        if cleaned_description != definition.get("description"):
            changes["description"] = cleaned_description
    if changes:
        assignments = ", ".join(f"{column} = :{column}" for column in changes)
        connection.execute(
            text(f"UPDATE workflow_definitions SET {assignments}, updated_at = now() "
                 "WHERE id = :id AND project_id = :p"),
            {**changes, "id": definition_id, "p": project_id},
        )
        emit_event(connection, project_id, "workflow.definition_renamed", actor,
                   "workflow_definition", definition_id,
                   {"workflow_key": definition["workflow_key"], **changes,
                    "previous_name": definition["name"]})
    return {"id": definition_id, "workflow_key": definition["workflow_key"],
            "name": changes.get("name", definition["name"]),
            "description": changes.get("description", definition.get("description")),
            "changed": sorted(changes)}


def publish_from_editor(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    name: str,
    spec: Any,
    definition_id: Optional[str] = None,
    description: Optional[str] = None,
    from_workflow_key: Optional[str] = None,
    update_copy_id: Optional[str] = None,
) -> dict[str, Any]:
    """Save what the editor holds, as one act: the operator's own workflow, a new one, or a copy.

      * `update_copy_id` — publish onto that existing copy (the "Update my copy" choice).
      * `definition_id` of the operator's own workflow — publish its next version.
      * no `definition_id`, or a standard's — create the operator's workflow and publish v1.
        `from_workflow_key` (or the standard's own key) is recorded as what it was copied from.

    Name and description travel with the save. A name is checked only when it changes, and a
    name the library already lists is refused (409, with a free one) — a standard's included.
    One exception: on `update_copy_id`, the name of the standard being saved from is the
    editor's title for the standard, not a rename, so the copy keeps its own name. Every
    problem is found BEFORE anything is written, so a refused save leaves nothing behind even
    for a caller that commits anyway; the definition and its first version are then written
    in the caller's one transaction.

    Returns {"unchanged": True} when the save would change nothing at all, else
    {"definition": {id, workflow_key, name}, "version": n, "created": bool}.
    """
    cleaned_name = _clean_text(name, "name")
    target: Optional[dict[str, Any]] = None
    source_key = str(from_workflow_key or "").strip() or None
    standard: Optional[dict[str, Any]] = None
    if definition_id:
        loaded = require_namespace(connection, project_id, definition_id, PRODUCT_NAMESPACE)
        if loaded.get("is_catalogue"):
            # A STANDARD IS NEVER EDITED IN PLACE. Saving one makes the operator's copy of it.
            standard = loaded
            source_key = source_key or str(loaded["workflow_key"])
        elif not update_copy_id:
            target = loaded
    if update_copy_id:
        target = require_namespace(connection, project_id, update_copy_id, PRODUCT_NAMESPACE)
        require_editable(target)
        # The editor titles a standard with the standard's name. Sent back on "Update my
        # copy", that is not a rename: renaming the copy to it would list the copy and the
        # standard under one name. A different name is a rename, checked like any other.
        if standard is not None and (cleaned_name.casefold()
                                     == str(standard["name"]).strip().casefold()):
            cleaned_name = str(target["name"])

    if target is None or cleaned_name != target["name"]:
        refuse_name_in_use(connection, project_id, cleaned_name,
                           exclude_id=str(target["id"]) if target else None)
    _, shape, blocked = spec_problems(connection, project_id, spec,
                                      namespace=PRODUCT_NAMESPACE)
    if shape:
        raise WorkflowValidationError(problems_sentence(shape + blocked),
                                      problems=shape + blocked)
    if blocked:
        raise WorkflowBindingBlockedError(problems_sentence(blocked), problems=blocked)

    created = False
    renamed = False
    if target is None:
        source = None
        if source_key:
            source = running_version(connection, project_id, source_key)
            if source is None:
                raise WorkflowNotFoundError(f"no workflow '{source_key}' to copy.")
        target = create_definition(
            connection, project_id=project_id,
            workflow_key=free_workflow_key(connection, project_id, cleaned_name),
            name=cleaned_name, description=(str(description or "").strip() or None),
            actor=actor, namespace=PRODUCT_NAMESPACE, is_catalogue=False,
            forked_from_version_id=str(source["version_id"]) if source else None,
        )
        created = True
    else:
        renamed = bool(rename_definition(
            connection, project_id, str(target["id"]), actor=actor, name=cleaned_name,
            description=description)["changed"])

    version = publish_version(connection, project_id=project_id,
                              definition_id=str(target["id"]), spec=spec, actor=actor)
    if (not created and not renamed and version.get("duplicate")
            and not version.get("reactivated")):
        return {"unchanged": True}
    current = connection.execute(
        text("SELECT id, workflow_key, name FROM workflow_definitions WHERE id = :id"),
        {"id": target["id"]},
    ).mappings().one()
    return {"definition": dict(current), "version": int(version["version"]),
            "created": created}


def create_mapping(
    connection: Connection,
    *,
    project_id: str,
    workflow_version_id: str,
    selector_kind: str,
    selector_value: str,
    scope_type: str,
    scope_id: str,
    priority: int,
    actor: str,
) -> dict[str, Any]:
    # A mapping makes its version one work starts: not while an agent delete is deciding.
    hold_workflow_reach(connection, project_id)
    version = connection.execute(
        text(
            """
            SELECT id FROM workflow_versions
            WHERE id=:id AND project_id=:project_id AND status='published'
            """
        ),
        {"id": workflow_version_id, "project_id": project_id},
    ).first()
    if not version:
        raise WorkflowNotFoundError("Published workflow version not found.")
    scope_type = _clean_text(scope_type, "scope_type").lower()
    if scope_type not in {"org", "workspace", "project"}:
        raise WorkflowValidationError("scope_type must be org, workspace, or project.")
    ancestry = _project_ancestry(connection, project_id)
    expected_scope = ancestry[f"{scope_type}_id"]
    if scope_id != expected_scope:
        raise WorkflowValidationError(
            f"scope_id does not match the workflow project's {scope_type}."
        )
    mapping_id = make_id("wfm")
    try:
        row = connection.execute(
            text(
                """
                INSERT INTO workflow_mappings (
                    id, project_id, workflow_version_id, selector_kind, selector_value,
                    scope_type, scope_id, priority, enabled, created_by
                ) VALUES (
                    :id, :project_id, :version_id, :selector_kind, :selector_value,
                    :scope_type, :scope_id, :priority, true, :actor
                ) RETURNING *
                """
            ),
            {
                "id": mapping_id,
                "project_id": project_id,
                "version_id": workflow_version_id,
                "selector_kind": _clean_text(selector_kind, "selector_kind").lower(),
                "selector_value": _clean_text(selector_value, "selector_value"),
                "scope_type": scope_type,
                "scope_id": scope_id,
                "priority": int(priority),
                "actor": actor,
            },
        ).mappings().one()
    except Exception as error:
        if "uq_workflow_mappings_active_selector" in str(error):
            raise WorkflowConflictError(
                "An enabled mapping already occupies this selector, scope, and priority."
            ) from error
        raise
    emit_event(
        connection,
        project_id,
        "workflow.mapping_created",
        actor,
        "workflow_mapping",
        mapping_id,
        {
            "selector_kind": selector_kind,
            "selector_value": selector_value,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "priority": int(priority),
            "workflow_version_id": workflow_version_id,
        },
    )
    return _serialize_mapping(row)


def repoint_mapping(connection: Connection, *, project_id: str, mapping_id: str,
                    actor: str) -> dict[str, Any]:
    """Point ONE mapping at its workflow's current (active) version.

    THE INCIDENT (review of milestone/agents, round 3). A mapping pins one exact version, and
    nothing an operator could press moved an arbitrary one: saving the workflow does not, the
    catalogue's Apply only replaces the project-scoped slot at its own priority, and
    `_repoint_default` only moves BOSS's own default. So an agent named in an older version that
    a mapping still sent work to could never be deleted, and the refusal told the operator to
    save the workflow — which changed nothing. This is the move that refusal now names, by the
    mapping's id, and the agent's page offers as a button.
    """
    hold_workflow_reach(connection, project_id)
    row = connection.execute(
        text("SELECT m.id, m.workflow_version_id, m.selector_kind, m.selector_value, m.enabled, "
             "       d.id AS definition_id, d.name, d.active_version_id, "
             "       (SELECT a.version FROM workflow_versions a "
             "         WHERE a.id = d.active_version_id) AS active_version "
             "  FROM workflow_mappings m "
             "  JOIN workflow_versions v ON v.id = m.workflow_version_id "
             "  JOIN workflow_definitions d ON d.id = v.workflow_definition_id "
             " WHERE m.id = :m AND m.project_id = :p FOR UPDATE OF m"),
        {"m": mapping_id, "p": project_id},
    ).mappings().first()
    if row is None:
        raise WorkflowNotFoundError("Workflow mapping not found.")
    if not row["active_version_id"]:
        raise WorkflowConflictError(
            f"'{row['name']}' has no current version to point this mapping at. Publish the "
            "workflow (saving it in the builder makes the new version current), then try again.")
    current = _version_row(connection, project_id, str(row["active_version_id"]))
    before = str(row["workflow_version_id"])
    if before != str(current["id"]):
        connection.execute(
            text("UPDATE workflow_mappings SET workflow_version_id = :v, updated_at = now() "
                 "WHERE id = :m"),
            {"v": current["id"], "m": mapping_id})
        emit_event(connection, project_id, "workflow.mapping_repointed", actor,
                   "workflow_mapping", mapping_id,
                   {"selector_kind": row["selector_kind"],
                    "selector_value": row["selector_value"],
                    "from_version_id": before, "to_version_id": str(current["id"])})
    updated = connection.execute(
        text("SELECT * FROM workflow_mappings WHERE id = :m"), {"m": mapping_id},
    ).mappings().one()
    return {**_serialize_mapping(updated), "workflow": row["name"],
            "version": int(current["version"]), "moved": before != str(current["id"])}


def list_mappings(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT * FROM workflow_mappings
            WHERE project_id=:project_id
            ORDER BY enabled DESC, selector_kind, selector_value, priority DESC, created_at
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    return [_serialize_mapping(row) for row in rows]


def _project_ancestry(connection: Connection, project_id: str) -> dict[str, str]:
    row = connection.execute(
        text(
            """
            SELECT p.id AS project_id, p.workspace_id, w.org_id
            FROM projects p JOIN workspaces w ON w.id=p.workspace_id
            WHERE p.id=:project_id
            """
        ),
        {"project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Project not found.")
    return dict(row)


def resolve_mapping(
    connection: Connection,
    *,
    project_id: str,
    selector_kind: str,
    selector_value: str,
) -> dict[str, Any]:
    ancestry = _project_ancestry(connection, project_id)
    rows = connection.execute(
        text(
            """
            SELECT m.*, v.status AS version_status, d.status AS definition_status,
                   d.workflow_key, d.name AS workflow_name
            FROM workflow_mappings m
            JOIN workflow_versions v ON v.id=m.workflow_version_id
            JOIN workflow_definitions d ON d.id=v.workflow_definition_id
            WHERE m.project_id=:project_id
              AND m.selector_kind=:selector_kind
              AND m.selector_value IN (:selector_value, '*')
            ORDER BY m.created_at, m.id
            """
        ),
        {
            "project_id": project_id,
            "selector_kind": _clean_text(selector_kind, "selector_kind").lower(),
            "selector_value": _clean_text(selector_value, "selector_value"),
        },
    ).mappings().all()
    scope_score = {"org": 1, "workspace": 2, "project": 3}
    candidates: list[tuple[tuple[int, int, int], Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        reason: Optional[str] = None
        if not row["enabled"]:
            reason = "mapping_disabled"
        elif row["version_status"] != "published":
            reason = "version_not_published"
        elif row["definition_status"] != "active":
            reason = "definition_not_active"
        elif ancestry[f"{row['scope_type']}_id"] != row["scope_id"]:
            reason = "scope_mismatch"
        if reason:
            rejected.append({**_serialize_mapping(row), "reason": reason})
            continue
        score = (
            scope_score[row["scope_type"]],
            1 if row["selector_value"] == selector_value else 0,
            int(row["priority"]),
        )
        candidates.append((score, row))
    if not candidates:
        return {
            "status": "unmapped",
            "project_id": project_id,
            "selector_kind": selector_kind,
            "selector_value": selector_value,
            "selected": None,
            "rejected": rejected,
            "reason": "No approved workflow mapping matched this class of work.",
        }
    candidates.sort(key=lambda item: item[0], reverse=True)
    top_score = candidates[0][0]
    tied = [row for score, row in candidates if score == top_score]
    if len(tied) > 1:
        ids = ", ".join(row["id"] for row in tied)
        raise WorkflowMappingAmbiguousError(
            f"Ambiguous workflow mappings at equal precedence: {ids}."
        )
    selected_row = candidates[0][1]
    for score, row in candidates[1:]:
        rejected.append(
            {**_serialize_mapping(row), "reason": f"lower_precedence:{score}"}
        )
    selected = _serialize_mapping(selected_row)
    selected.update(
        {
            "workflow_key": selected_row["workflow_key"],
            "workflow_name": selected_row["workflow_name"],
            "precedence": list(top_score),
        }
    )
    return {
        "status": "selected",
        "project_id": project_id,
        "selector_kind": selector_kind,
        "selector_value": selector_value,
        "selected": selected,
        "rejected": rejected,
        "reason": "Most-specific scope, exact selector, and highest priority won.",
    }


def _version_row(connection: Connection, project_id: str, version_id: str) -> Any:
    row = connection.execute(
        text(
            """
            SELECT v.*, d.workflow_key, d.name AS workflow_name,
                   d.status AS definition_status
            FROM workflow_versions v
            JOIN workflow_definitions d ON d.id=v.workflow_definition_id
            WHERE v.id=:id AND v.project_id=:project_id
            """
        ),
        {"id": version_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow version not found.")
    if row["status"] == "retired" and row["definition_status"] == "active":
        raise WorkflowConflictError(
            f"v{row['version']} of '{row['workflow_name']}' was retired (an agent it named was "
            "deleted), so it cannot start. Start the workflow's current version instead.")
    if (row["status"] not in STARTABLE_VERSION_STATUSES
            or row["definition_status"] not in STARTABLE_DEFINITION_STATUSES):
        raise WorkflowConflictError("Workflow version is not active and published.")
    return row


#: WHAT `_version_row` LETS A RUN START FROM — the one gate every run passes (start_run is the
#: only writer of workflow_runs, and it always calls `_version_row`). Named so the delete guard
#: below reads the same rule rather than a copy of it.
STARTABLE_VERSION_STATUSES = ("published",)
STARTABLE_DEFINITION_STATUSES = ("active",)
#: A 'draft' definition is not startable YET, but publishing any version to it makes it active
#: and every published version it already has startable by id, with no further check.
GUARDED_DEFINITION_STATUSES = STARTABLE_DEFINITION_STATUSES + ("draft",)

#: How a run reaches a version, as `versions_a_run_can_reach` labels it. Only `older` may be
#: retired to free what it names: nothing points at it, so retiring it breaks no default start,
#: mapping or parent workflow. Every other reach is something that WILL start that version.
VERSION_REACH = {
    "active": "the version the workflow starts by default",
    "not yet active": "published without being made active, ready to be switched on",
    "mapped": "a workflow mapping sends work to it",
    "subworkflow": "another workflow's step starts it as a subworkflow",
    "older": "an older published version — still startable by its id, though nothing points at it",
}
RETIRABLE_REACH = frozenset({"older"})


def versions_a_run_can_reach(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every workflow version a run in this project can still start from, with how.

    THE INCIDENT (review of milestone/agents, round 2). The agent-delete guard read only each
    workflow's NEWEST version. publish_version makes an older one active again when its content
    is re-saved (the restore-after-a-bad-save flow), and can publish without activating, and
    start_run accepts any published version by id. So an agent named only in the active v1,
    with a v2 that did not name it, was deleted with 200 and tombstoned; the next run of v1 was
    refused with "not a role this workforce has", for good.

    So this reads the same statuses `_version_row` accepts (plus drafts, which one publish makes
    startable), not a selection of them, and labels each version with the strongest reason a
    run would reach it (VERSION_REACH). A new way to choose a version cannot slip past it: any
    version it could choose is one of these rows already.
    """
    rows = connection.execute(
        text(
            """
            SELECT d.id AS definition_id, d.workflow_key, d.name, d.active_version_id,
                   v.id AS version_id, v.version, v.spec,
                   (SELECT a.version FROM workflow_versions a
                     WHERE a.id = d.active_version_id) AS active_version
              FROM workflow_versions v JOIN workflow_definitions d
                ON d.id = v.workflow_definition_id
             WHERE v.project_id = :p
               AND v.status = ANY(:version_statuses)
               AND d.status = ANY(:definition_statuses)
             ORDER BY d.workflow_key, v.version DESC
            """
        ),
        {"p": project_id, "version_statuses": list(STARTABLE_VERSION_STATUSES),
         "definition_statuses": list(GUARDED_DEFINITION_STATUSES)},
    ).mappings().all()
    # WHO POINTS AT IT, not only that something does. The delete refusal said "(v1, mapped)"
    # and then "save the workflow, then delete again" — which never moves a mapping or a
    # parent's binding, so an operator who did exactly what it said was refused again, for good
    # (review of milestone/agents, round 3). Each version now carries every mapping and parent
    # step that starts it (`pointed_by`, each with `via`: 'mapped' or 'subworkflow'), so the
    # refusal can name the kind of work or the parent workflow and say how to move THAT.
    pointers: dict[str, list[dict[str, Any]]] = {}
    for m in connection.execute(
        text("SELECT id, workflow_version_id, selector_kind, selector_value "
             "FROM workflow_mappings "
             "WHERE project_id = :p AND enabled ORDER BY selector_kind, selector_value, id"),
        {"p": project_id},
    ).mappings().all():
        pointers.setdefault(str(m["workflow_version_id"]), []).append(
            {"via": "mapped", "mapping_id": str(m["id"]), "selector_kind": str(m["selector_kind"]),
             "selector_value": str(m["selector_value"])})
    specs = {str(row["version_id"]): (row["spec"] if isinstance(row["spec"], dict)
                                      else _json(row["spec"], {})) or {}
             for row in rows}
    parents: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for step in specs[str(row["version_id"])].get("steps") or []:
            if isinstance(step, dict) and step.get("kind") == "subworkflow":
                child = (step.get("binding") or {}).get("workflow_version_id")
                if child:
                    parents.setdefault(str(child), []).append({
                        "via": "subworkflow", "parent_version_id": str(row["version_id"]),
                        "workflow_key": row["workflow_key"],
                        "workflow": row["name"], "version": int(row["version"]),
                        "step": str(step.get("label") or step.get("name")
                                    or step.get("id") or "")})

    # A PARENT ONLY STARTS ITS CHILD IF SOMETHING STARTS THE PARENT. Counting every binding made
    # a child bound by an OLDER parent version unretirable for good: re-binding the parent (what
    # the refusal says to do) publishes a new parent version, and the old one — which nothing
    # starts by default — still bound the child (review of milestone/agents, round 3). So a
    # binding counts only from a parent that is itself reached: active, mapped, not yet active,
    # a reached child in turn, or carrying a run that can still go on. A child bound only by
    # older parents is older too, and retiring it retires those parents with it (`retires_with`)
    # rather than leaving them to fail at their subworkflow step.
    from .workflow_run_actions import CLOSED_RUN_STATES

    held = {str(v) for v in connection.execute(
        text("SELECT DISTINCT workflow_version_id FROM workflow_runs "
             "WHERE project_id = :p AND NOT (status = ANY(:closed))"),
        {"p": project_id, "closed": sorted(CLOSED_RUN_STATES)},
    ).scalars().all()}
    own: dict[str, str] = {}
    for row in rows:
        vid = str(row["version_id"])
        active = row["active_version"]
        if vid == str(row["active_version_id"] or ""):
            own[vid] = "active"
        elif any(p["via"] == "mapped" for p in pointers.get(vid, [])):
            own[vid] = "mapped"
        elif active is None or int(row["version"]) > int(active):
            own[vid] = "not yet active"
        else:
            own[vid] = "older"
    reached = {vid for vid, r in own.items() if r != "older"} | held
    grew = True
    while grew:
        grew = False
        for child, bound in parents.items():
            if child not in reached and any(b["parent_version_id"] in reached for b in bound):
                reached.add(child)
                grew = True

    out: list[dict[str, Any]] = []
    for row in rows:
        vid = str(row["version_id"])
        bound = parents.get(vid, [])
        strong = [b for b in bound if b["parent_version_id"] in reached]
        reach = own[vid]
        if reach in ("not yet active", "older") and strong:
            reach = "subworkflow"
        out.append({"definition_id": row["definition_id"], "workflow_key": row["workflow_key"],
                    "name": row["name"], "version_id": vid, "version": int(row["version"]),
                    "spec": specs[vid], "reach": reach,
                    "pointed_by": pointers.get(vid, []) + strong,
                    "retires_with": [b["parent_version_id"] for b in bound
                                     if b["parent_version_id"] not in reached]})
    return out


def retired_with(versions: list[dict[str, Any]], version_id: str) -> list[dict[str, Any]]:
    """The older parent versions that must be retired along with `version_id`, transitively —
    each binds it (or one of them) as a subworkflow and nothing starts it (see
    versions_a_run_can_reach). Never the version itself."""
    by_id = {v["version_id"]: v for v in versions}
    found: list[dict[str, Any]] = []
    queue = list((by_id.get(str(version_id)) or {}).get("retires_with") or [])
    while queue:
        parent = by_id.get(queue.pop(0))
        if parent is None or parent["reach"] not in RETIRABLE_REACH or parent in found:
            continue
        found.append(parent)
        queue.extend(parent.get("retires_with") or [])
    return found


#: THE LOCK BETWEEN "WHAT CAN START WHICH VERSION" AND DELETING AN AGENT.
#:
#: THE INCIDENT (review of milestone/agents, round 3). delete_agent read the versions a run can
#: reach, then retired the older ones and deleted the role, with nothing held in between. A
#: publish committing in that gap — re-saving an older version's content makes it active again
#: — left the role deleted under the version that now runs, while `retire_versions` (which
#: re-reads the reach) quietly skipped it. The same gap was open to a new mapping, a run started
#: by id, and a paused run migrated onto an older version.
#:
#: So every writer that changes which versions a run can start from takes this lock SHARED (they
#: never wait for one another), and delete_agent takes it EXCLUSIVE before it reads anything: a
#: writer either commits before the delete looks, or runs after it and finds the agent gone.
#: tests/test_every_version_a_run_can_start_guards_its_agents.py scans for every such writer and
#: fails until it takes this lock.
_REACH_LOCK = "boss.workflow_reach:"


def hold_workflow_reach(connection: Connection, project_id: str, *,
                        exclusive: bool = False) -> None:
    """Hold this project's workflow reach until the transaction ends (see _REACH_LOCK)."""
    fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    connection.execute(text(f"SELECT {fn}(hashtextextended(:k, 0))"),
                       {"k": _REACH_LOCK + str(project_id)})


def retire_versions(connection: Connection, project_id: str, version_ids: Iterable[str], *,
                    actor: str, reason: str) -> list[str]:
    """Retire older published versions so no run can start from them; returns those retired.

    Only versions `versions_a_run_can_reach` labels `older` — never the active one, a mapped
    one or a subworkflow child, which are what work actually starts from. History is kept: the
    row stays, with its runs; only `_version_row` stops accepting it. Re-saving the same content
    later publishes it again (publish_version), after the spec is re-checked.

    The older parent versions that bind a retired version as a subworkflow (`retired_with`) are
    retired with it, so none is left startable by id only to fail at that step.
    """
    wanted = {str(v) for v in version_ids}
    versions = versions_a_run_can_reach(connection, project_id)
    retirable = [(v, reason) for v in versions
                 if v["version_id"] in wanted and v["reach"] in RETIRABLE_REACH]
    for version, _ in list(retirable):
        for parent in retired_with(versions, version["version_id"]):
            if all(parent is not r for r, _ in retirable):
                retirable.append((parent, f"starts v{version['version']} of "
                                          f"'{version['name']}' as a subworkflow, retired "
                                          f"because it {reason}"))
    for version, why in retirable:
        connection.execute(
            text("UPDATE workflow_versions SET status = 'retired' "
                 "WHERE id = :id AND project_id = :p AND status = 'published'"),
            {"id": version["version_id"], "p": project_id})
        emit_event(connection, project_id, "workflow.version_retired", actor,
                   "workflow_version", version["version_id"],
                   {"definition_id": version["definition_id"],
                    "version": version["version"], "reason": why})
    return [v["version_id"] for v, _ in retirable]


def _table_exists(connection: Connection, table_name: str) -> bool:
    return bool(
        connection.execute(
            text("SELECT to_regclass(:table_name) IS NOT NULL"),
            {"table_name": table_name},
        ).scalar_one()
    )


def _skill_facts(row: Any) -> dict[str, Any]:
    """What a project_skills row says about risk and provenance.

    `risk_tier` lives in the skill's config rather than in a column, so read it there and
    default to the FLOOR when it is absent — an unlabelled skill is treated as dangerous, not
    as safe. Guessing 0 for something nobody classified is how an ungoverned step ships.
    """
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    declared = config.get("risk_tier")
    return {
        "enabled": bool(row.get("enabled", True)),
        "risk_tier": int(declared) if declared is not None else 2,
        "provenance": provenance,
        # The catalogue's sentence when a person must approve whatever the tier (an authored
        # mcp_tool skill on an unreviewed server — mcp_registry.needs_person), else None.
        "requires_person": row.get("requires_person"),
    }


def _validate_bindings(
    connection: Connection,
    project_id: str,
    version_id: str,
    spec: dict[str, Any],
    namespace: str = SYSTEM_NAMESPACE,
) -> None:
    for step in spec["steps"]:
        binding = step.get("binding") or {}
        role_key = binding.get("role_key")
        if role_key:
            row = connection.execute(
                text(
                    "SELECT 1 FROM roles WHERE project_id=:project_id AND role_key=:role_key"
                ),
                {"project_id": project_id, "role_key": role_key},
            ).first()
            if not row:
                raise WorkflowBindingBlockedError(
                    f"step '{step['id']}' references missing role '{role_key}'."
                )
        if step["kind"] == "skill":
            skill_id = binding["skill_id"]
            # THE ONE CATALOGUE (skill_catalogue). Reading project_skills here refused every
            # authored skill and every shipped skill without a per-project row — which on a
            # real install was all of the ones agents actually hold.
            from .skill_catalogue import entry as _catalogue_entry

            found = _catalogue_entry(connection, project_id, str(skill_id))
            row = None if found is None else {
                "enabled": found["enabled"] and found["usable"],
                "config": {"risk_tier": found["risk_tier"]} if found["risk_declared"] else {},
                "provenance": {"trusted": found["trusted"]},
                "requires_person": found.get("requires_person"),
            }
            if not row or not bool(row["enabled"]):
                raise WorkflowBindingBlockedError(
                    f"step '{step['id']}' skill '{skill_id}' is missing or disabled."
                )
            # THE FLOOR APPLIES TO A CUSTOMER'S OWN STEPS TOO — BUT ONLY TO THEIRS.
            #
            # This is the only place that can see a skill's real risk tier and provenance, so
            # it is the only place the check can be made. Without it, "attach your own skill"
            # would be the documented way around every gate in the product.
            #
            # Scoped to `product`, deliberately. BOSS's own thirty-three definitions were
            # written before this rule existed and are governed by the machinery around them;
            # applying a new check to them here would fail to publish workflows that have been
            # running correctly for months, which is exactly what "the infra workflows do not
            # change" rules out.
            if namespace == PRODUCT_NAMESPACE:
                problems = custom_step_problems(step, _skill_facts(row))
                if problems:
                    raise WorkflowBindingBlockedError(" ".join(problems))
        if step["kind"] == "capability" and binding.get("custom_step"):
            # THE ONLY PLACE A CUSTOM STEP'S REAL RISK CAN BE SEEN. The tier comes from the
            # skill or MCP server it binds, which means it takes a connection and a project to
            # read — and it is re-read here on every publish, because a step that was tier 0
            # when it was drawn can be tier 3 by the time it is published.
            problems = defined_step_problems(connection, project_id, step)
            if problems:
                raise WorkflowBindingBlockedError(" ".join(problems))
        if step["kind"] == "integration":
            if not _table_exists(connection, "integration_connections"):
                raise WorkflowBindingBlockedError(
                    "Integration runtime is not installed; integration binding fails closed."
                )
            row = connection.execute(
                text(
                    """
                    SELECT status FROM integration_connections
                    WHERE id=:id AND project_id=:project_id
                    """
                ),
                {"id": binding["integration_connection_id"], "project_id": project_id},
            ).mappings().first()
            if not row or row["status"] not in {"active", "healthy"}:
                raise WorkflowBindingBlockedError(
                    f"step '{step['id']}' integration is missing, revoked, or unhealthy; "
                    "the integration binding fails closed."
                )
        if step["kind"] == "agent_group":
            if not _table_exists(connection, "agent_group_versions"):
                raise WorkflowBindingBlockedError(
                    "Agent Group runtime is not installed; group binding fails closed."
                )
            row = connection.execute(
                text(
                    """
                    SELECT status FROM agent_group_versions
                    WHERE id=:id AND project_id=:project_id
                    """
                ),
                {"id": binding["agent_group_version_id"], "project_id": project_id},
            ).mappings().first()
            if not row or row["status"] != "published":
                raise WorkflowBindingBlockedError(
                    f"step '{step['id']}' Agent Group version is unavailable."
                )
        if step["kind"] == "subworkflow":
            child_id = str(binding["workflow_version_id"])
            if child_id == version_id:
                raise WorkflowBindingBlockedError(
                    f"step '{step['id']}' cannot recursively invoke its own version."
                )
            _version_row(connection, project_id, child_id)
    # WHO RUNS EACH STEP, re-asked at start: an agent can be deleted, or a grant taken away,
    # after the workflow was published. See services/step_agents.py. A version published before
    # those rules is started under the ones it was published under.
    agent_problems = step_agent_problems(connection, project_id, spec,
                                         legacy=not under_current_agent_rules(spec))
    if agent_problems:
        raise WorkflowBindingBlockedError(" ".join(agent_problems))


#: Run inputs that claim work already happened. Only the server sets them — see `start_run`.
ADOPT_PREFIX = "adopt_"
#: The run-input key the start door's pre-check writes (workflow_precheck.RECORD_KEY).
PRECHECK_KEY = "_precheck"


def start_run(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    input_data: dict[str, Any],
    dedupe_key: str,
    workflow_version_id: Optional[str] = None,
    selector_kind: Optional[str] = None,
    selector_value: Optional[str] = None,
    policy_snapshot: Optional[dict[str, Any]] = None,
    parent_run_id: Optional[str] = None,
    adopted: Optional[dict[str, Any]] = None,
    precheck_record: Optional[dict[str, Any]] = None,
    mode: str = "normal",
    pins: Optional[dict[str, Any]] = None,
    allow_live: Optional[list[str]] = None,
    rerun_of: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Start a run.

    `mode` 'test', `pins`, `allow_live` and `rerun_of` are set only through
    workflow_test_runs (start_test_run / rerun_from_step), which validates them against the
    version's spec; see that module for what they do. A normal run has none of them.

    `precheck_record` is what the start door's pre-check recorded (workflow_precheck.gate_start:
    the will_fail checks started anyway, the steps skipped, who and why). It is written under
    `_precheck` in the run's input; a caller's own `_precheck` is dropped, like `adopt_*`, so a
    skip can be recorded by the server and never asserted by a client.

    ADOPTION IS DECIDED BY THE SERVER, HERE, FOR EVERY ENTRY POINT. An `adopt_*` input tells the
    runtime "this work already happened — watch it rather than do it". The HTTP start endpoint
    stripped caller-supplied ones on purpose, but the agent's `start_workflow` copied its
    `inputs` straight through, so a model could assert research had been done and the run would
    build on it — the boss-1 incident (a run built on research that failed) through the agent's
    door, past both the "Build on this research" rule and the adopt-key rule (review of
    0fd6c45). So every `adopt_*` key in `input_data` is dropped here, whoever sent it; the only
    way to set one is `adopted`, which callers fill from a source they have CHECKED
    (`workflow_run_actions.research_to_adopt`, or `start_project_run`, true by construction).
    """
    dedupe_key = _clean_text(dedupe_key, "dedupe_key")
    if mode not in ("normal", "test"):
        raise WorkflowValidationError(f"mode must be 'normal' or 'test', not {mode!r}")
    # Identity is never a caller's to set (IDENTITY.md §2): `_identity*` keys are dropped here.
    input_data = {k: v for k, v in (input_data or {}).items()
                  if not str(k).startswith(ADOPT_PREFIX) and not str(k).startswith("_identity")
                  and str(k) != PRECHECK_KEY}
    if precheck_record:
        input_data[PRECHECK_KEY] = dict(precheck_record)
    for key, value in (adopted or {}).items():
        if not str(key).startswith(ADOPT_PREFIX):
            raise WorkflowValidationError(f"{key!r} is not an adoption key")
        if value not in (None, ""):
            input_data[str(key)] = value
    existing = connection.execute(
        text(
            "SELECT id FROM workflow_runs WHERE project_id=:project_id AND dedupe_key=:dedupe_key"
        ),
        {"project_id": project_id, "dedupe_key": dedupe_key},
    ).mappings().first()
    if existing:
        result = get_run(connection, project_id, existing["id"])
        result["duplicate"] = True
        return result

    mapping_id: Optional[str] = None
    if not workflow_version_id:
        if not selector_kind or not selector_value:
            raise WorkflowValidationError(
                "workflow_version_id or selector_kind/selector_value is required."
            )
        resolution = resolve_mapping(
            connection,
            project_id=project_id,
            selector_kind=selector_kind,
            selector_value=selector_value,
        )
        if resolution["status"] != "selected":
            raise WorkflowMappingNotFoundError(resolution["reason"])
        mapping_id = resolution["selected"]["id"]
        workflow_version_id = resolution["selected"]["workflow_version_id"]
    # A run started now reaches its version's steps: not while an agent delete is deciding.
    hold_workflow_reach(connection, project_id)
    version = _version_row(connection, project_id, str(workflow_version_id))
    spec = _published_spec(version["spec"], project_roster(connection, project_id))
    _hold_new_typed_decision_work(spec)
    version_namespace = connection.execute(
        text(
            "SELECT COALESCE(d.namespace, 'system') FROM workflow_definitions d "
            "WHERE d.id = :definition_id"
        ),
        {"definition_id": version["workflow_definition_id"]},
    ).scalar() or SYSTEM_NAMESPACE
    # One catalogue build for the whole publish, not one per skill step (round-2 review).
    from .skill_catalogue import one_pass

    with one_pass(connection):
        _validate_bindings(connection, project_id, version["id"], spec, str(version_namespace))

    # REFUSED HERE, NOT FOUR STEPS IN. A workflow that fails on its third step because nobody
    # supplied a video URL has already paid for the first two, and the error it gives is about
    # a missing binding rather than about the thing the operator forgot.
    absent = missing_required_inputs(spec, input_data)
    if absent:
        declared = spec.get("input_schema") or {}
        named = ", ".join(
            f"{name} ({declared[name].get('label') or name})" if declared.get(name) else name
            for name in absent)
        raise WorkflowValidationError(
            f"this workflow needs {len(absent)} input(s) it was not given: {named}")

    run_id = make_id("wfr")
    root_run_id = run_id
    # WHOSE AUTHORITY THIS RUN USES (IDENTITY.md §7.3): a person starting it needs a grant that
    # covers the agents and permissions its steps will use, else 409 consent_required. The result
    # is recorded server-side on the run; every job, build and sub-agent the worker starts for it
    # carries it, so worker-started work acts for that person under that grant.
    from boss_engine.identity import consent as _consent

    identity_acting = _consent.require_for_workflow(
        connection, project_id=project_id, spec=spec, input_data=input_data, run_id=run_id,
        parent_run_id=parent_run_id,
        title=consent_task_title(connection, project_id, version["workflow_definition_id"],
                                 input_data))
    policy_snapshot = _consent.record_on_run(policy_snapshot, identity_acting)
    if parent_run_id:
        parent = connection.execute(
            text(
                "SELECT root_run_id FROM workflow_runs WHERE id=:id AND project_id=:project_id"
            ),
            {"id": parent_run_id, "project_id": project_id},
        ).mappings().first()
        if not parent:
            raise WorkflowNotFoundError("Parent workflow run not found.")
        root_run_id = parent["root_run_id"] or parent_run_id
    # TWO PRESSES, ONE RUN. The unique (project_id, dedupe_key) index settles a race between two
    # identical starts; the loser gets the winner's run back instead of an error. ON CONFLICT DO
    # NOTHING rather than catching the error: a failed INSERT would abort this transaction.
    inserted = connection.execute(
        text(
            """
            INSERT INTO workflow_runs (
                id, project_id, workflow_definition_id, workflow_version_id,
                workflow_mapping_id, parent_run_id, root_run_id, actor, status,
                input, context, policy_snapshot, dedupe_key,
                on_behalf_of, grant_id, authority_org_id, authority_run_id,
                mode, pins, allow_live, rerun_of
            ) VALUES (
                :id, :project_id, :definition_id, :version_id, :mapping_id,
                :parent_run_id, :root_run_id, :actor, 'validating',
                CAST(:input AS JSONB), CAST(:context AS JSONB),
                CAST(:policy AS JSONB), :dedupe_key,
                :on_behalf_of, :grant_id, :authority_org_id, :authority_run_id,
                :mode, CAST(:pins AS JSONB), CAST(:allow_live AS JSONB),
                CAST(:rerun_of AS JSONB)
            )
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "id": run_id,
            "project_id": project_id,
            "definition_id": version["workflow_definition_id"],
            "version_id": version["id"],
            "mapping_id": mapping_id,
            "parent_run_id": parent_run_id,
            "root_run_id": root_run_id,
            "actor": actor,
            "input": json.dumps(input_data or {}),
            "context": json.dumps({}),
            "policy": json.dumps(policy_snapshot or {}),
            "dedupe_key": dedupe_key,
            "mode": mode,
            "pins": json.dumps(pins or {}),
            "allow_live": json.dumps(list(allow_live or [])),
            "rerun_of": json.dumps(rerun_of) if rerun_of else None,
            **_consent.authority_columns(identity_acting),
        },
    )
    if not inserted.rowcount:
        winner = connection.execute(
            text("SELECT id FROM workflow_runs WHERE project_id=:p AND dedupe_key=:d"),
            {"p": project_id, "d": dedupe_key},
        ).mappings().first()
        if winner:
            result = get_run(connection, project_id, winner["id"])
            result["duplicate"] = True
            return result
    for step in spec["steps"]:
        _insert_step_run(
            connection,
            project_id=project_id,
            run_id=run_id,
            step=step,
            iteration=0,
            status="pending",
        )
    connection.execute(
        text("UPDATE workflow_runs SET status='running', updated_at=now() "
             "WHERE id=:id AND status='validating'"),
        {"id": run_id},
    )
    # WHO IS ON THIS, seeded before the first step can be refused for not knowing.
    #
    # Assignment is enforced at the gates, so an app that starts with nobody on it would have
    # every gate refuse — the feature would read as breakage rather than governance. The seed is
    # exactly the agents that would have done the work anyway: the STAGE_ROLES set BOSS already
    # attributes every model call to, plus whatever this workflow names as an approver. An app
    # that already has an assignment is left alone, so a re-run never overwrites a decision.
    try:
        from . import agent_assignment
        app_name = str((input_data or {}).get("title")
                       or (input_data or {}).get("directive") or "").strip()
        if app_name:
            agent_assignment.ensure_defaults(
                connection, project_id=project_id, app_name=app_name,
                actor=actor, spec=spec)
    except Exception as error:  # noqa: BLE001 — a run must not fail because seeding did
        logger.warning("could not seed agent assignment for %s: %s", run_id, error)
    emit_event(
        connection,
        project_id,
        "workflow.run_started",
        actor,
        "workflow_run",
        run_id,
        {
            "workflow_definition_id": version["workflow_definition_id"],
            "workflow_version_id": version["id"],
            "workflow_mapping_id": mapping_id,
            "dedupe_key": dedupe_key,
            "parent_run_id": parent_run_id,
            "mode": mode,
            **({"pinned_steps": sorted(pins)} if pins else {}),
            **({"allow_live": list(allow_live)} if allow_live else {}),
            **({"rerun_of": rerun_of} if rerun_of else {}),
        },
    )
    _activate_step(connection, run_id, spec["entry_step"], 0)
    _drain_control_steps(connection, run_id)
    # SIGNED FROM THE FIRST MOMENT: the person who started the run is the root of its chain.
    from . import agent_acts

    agent_acts.quietly(connection, "sign the run's start", agent_acts.sync_run, run_id)
    result = get_run(connection, project_id, run_id)
    result["duplicate"] = False
    return result


def _insert_step_run(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    step: dict[str, Any],
    iteration: int,
    status: str,
) -> str:
    step_run_id = make_id("wfs")
    connection.execute(
        text(
            """
            INSERT INTO workflow_step_runs (
                id, project_id, workflow_run_id, step_id, step_kind, iteration,
                status, input, binding_snapshot, max_attempts
            ) VALUES (
                :id, :project_id, :run_id, :step_id, :step_kind, :iteration,
                :status, CAST(:input AS JSONB), CAST(:binding AS JSONB), :max_attempts
            ) ON CONFLICT (workflow_run_id, step_id, iteration) DO NOTHING
            """
        ),
        {
            "id": step_run_id,
            "project_id": project_id,
            "run_id": run_id,
            "step_id": step["id"],
            "step_kind": step["kind"],
            "iteration": iteration,
            "status": status,
            "input": json.dumps(step.get("input") or {}),
            "binding": json.dumps(step.get("binding") or {}),
            "max_attempts": int(step.get("max_attempts", 3)),
        },
    )
    return step_run_id


def _run_row(connection: Connection, run_id: str, *, lock: bool = False) -> Any:
    lock_sql = " FOR UPDATE" if lock else ""
    row = connection.execute(
        text(f"SELECT * FROM workflow_runs WHERE id=:id{lock_sql}"),
        {"id": run_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow run not found.")
    return row


def _run_spec(connection: Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT v.spec, r.project_id,
                   ARRAY(SELECT DISTINCT s.step_id FROM workflow_step_runs s
                         WHERE s.workflow_run_id = r.id) AS step_ids
            FROM workflow_runs r
            JOIN workflow_versions v ON v.id=r.workflow_version_id
            WHERE r.id=:run_id
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow run not found.")
    # A RUN KEEPS THE SHAPE IT STARTED WITH. Every step's row is written when a run starts, so a
    # run started before the two-pass rule (two_pass) has no row for the More tests step it
    # would now add, and gets its version as it was: a deploy rebound to a step that never ran
    # would fail a run that was fine.
    as_stored = _json(row["spec"], {})
    started_with = {str(i) for i in (row["step_ids"] or [])}
    two_passed = as_stored
    # Today's shape first, then the rc10.2 inline one a run may have started with.
    for shape in (two_pass.with_more_tests, two_pass.with_more_tests_inline):
        candidate = shape(as_stored)
        if not two_pass.added_step_ids(as_stored, candidate) - started_with:
            two_passed = candidate
            break
    # A run in flight keeps the rules its version was published under — see _published_spec.
    # Each step is tagged with them, because the runner is handed a step, not the version.
    # Re-read on every step of a run, so a run whose step names an operator-made agent must not
    # be refused half-way through by a check that only knows the bundled roles: the project's
    # roster goes with it.
    return tag_steps_with_agent_rules(
        _published_spec(two_passed, project_roster(connection, str(row["project_id"])),
                        two_pass_builds=False))


def _published_spec(raw: Any, roster: Optional[Iterable[str]] = None, *,
                    two_pass_builds: bool = True) -> dict[str, Any]:
    """A STORED version's spec, validated under the agent rules it was published under.

    THE INCIDENT THIS PREVENTS. The step-agent rules (`human` only on a gate; a custom step that
    names nobody needs an agent holding its skill) arrived after workflows had been published
    without them — the builder's default for a custom step was "no agent — runs as the
    workflow". Applying them to those versions refused their starts and failed their in-flight
    runs mid-way, on an upgrade, for something the operator had done right at the time. So a
    version without the `step_agent_rules` stamp is read under the old rules, everywhere it is
    read; publishing a new version is where the new rules are met, with the reason given.
    """
    spec = _json(raw, {})
    if two_pass_builds:
        # EVERY BUILD GETS ITS MORE TESTS PASS, in every workflow a run can start: catalogue,
        # forked, drafted or written by hand (two_pass). Applied here, where a version becomes
        # the steps a run walks, so no workflow has to remember to wire it.
        spec = two_pass.with_more_tests(spec)
    return validate_workflow_spec(spec, roster, check_agents=under_current_agent_rules(spec))


def _what_there_is_to_review(connection: Connection, run_id: str) -> dict[str, Any]:
    """The thing the previous steps made, so an approval can point at it.

    A gate is asked about WORK, and the work is whatever the run has produced by the time it
    gets here. Taking the most recent step's output means a video gate carries the video, a
    document gate carries the document, and neither had to declare which kind it was.

    Only fields a reviewer can act on: a link to open, an id to trace, and what the agent said
    it did. Not the whole output blob — a wall of JSON is not review material.
    """
    row = connection.execute(
        text(
            """
            SELECT step_id, output FROM workflow_step_runs
            WHERE workflow_run_id = :run_id AND status = 'succeeded'
            ORDER BY completed_at DESC NULLS LAST
            LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    output = (row or {}).get("output")
    if not isinstance(output, dict):
        return {}
    review: dict[str, Any] = {"produced_by": (row or {}).get("step_id")}
    for field in ("view_url", "media_artifact_id", "artifact_id", "preview_url", "pr_url",
                  "summary", "duration_seconds", "width", "height"):
        value = output.get(field)
        if value not in (None, "", [], {}):
            review[field] = value
    return review if len(review) > 1 else {}


def _step_row(
    connection: Connection,
    run_id: str,
    step_id: str,
    iteration: int,
    *,
    lock: bool = False,
) -> Any:
    lock_sql = " FOR UPDATE" if lock else ""
    return connection.execute(
        text(
            f"""
            SELECT * FROM workflow_step_runs
            WHERE workflow_run_id=:run_id AND step_id=:step_id AND iteration=:iteration
            {lock_sql}
            """
        ),
        {"run_id": run_id, "step_id": step_id, "iteration": iteration},
    ).mappings().first()


def _serialize_step(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_run_id": row["workflow_run_id"],
        "step_id": row["step_id"],
        "step_kind": row["step_kind"],
        "iteration": int(row["iteration"]),
        "status": row["status"],
        "input": _json(row["input"], {}),
        "output": _json(row["output"], {}),
        "binding_snapshot": _json(row["binding_snapshot"], {}),
        "evidence": _json(row["evidence"], []),
        "attempt": int(row["attempt"]),
        "max_attempts": int(row["max_attempts"]),
        "run_after": _iso(row["run_after"]),
        "lease_owner": row.get("lease_owner"),
        "lease_expires_at": _iso(row.get("lease_expires_at")),
        # When the current attempt runs out of time (`timeout_seconds`); null with no timeout.
        "attempt_timeout_at": _iso(row.get("attempt_timeout_at")),
        "heartbeat_at": _iso(row.get("heartbeat_at")),
        "decision_id": row.get("decision_id"),
        "child_workflow_run_id": row.get("child_workflow_run_id"),
        "last_error": row.get("last_error"),
        "started_at": _iso(row.get("started_at")),
        "completed_at": _iso(row.get("completed_at")),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _serialize_run(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_definition_id": row["workflow_definition_id"],
        "workflow_version_id": row["workflow_version_id"],
        "workflow_mapping_id": row.get("workflow_mapping_id"),
        "parent_run_id": row.get("parent_run_id"),
        "root_run_id": row.get("root_run_id"),
        "actor": row["actor"],
        "status": row["status"],
        "input": _json(row["input"], {}),
        "output": _json(row["output"], {}),
        "context": _json(row["context"], {}),
        "policy_snapshot": _json(row["policy_snapshot"], {}),
        "current_frontier": _json(row["current_frontier"], []),
        "dedupe_key": row.get("dedupe_key"),
        # TEST RUNS AND RE-RUNS (workflow_test_runs): every run says which it is.
        "mode": row.get("mode") or "normal",
        "pins": _json(row.get("pins"), {}),
        "allow_live": _json(row.get("allow_live"), []),
        "rerun_of": _json(row.get("rerun_of"), None),
        "blocked_reason": row.get("blocked_reason"),
        "error": row.get("error"),
        "paused_at": _iso(row.get("paused_at")),
        "completed_at": _iso(row.get("completed_at")),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def get_run(connection: Connection, project_id: str, run_id: str) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT r.*, d.workflow_key, d.name AS workflow_name, v.version
            FROM workflow_runs r
            JOIN workflow_definitions d ON d.id=r.workflow_definition_id
            JOIN workflow_versions v ON v.id=r.workflow_version_id
            WHERE r.id=:run_id AND r.project_id=:project_id
            """
        ),
        {"run_id": run_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow run not found.")
    steps = connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE workflow_run_id=:run_id
            ORDER BY created_at, step_id, iteration
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    migrations = connection.execute(
        text(
            """
            SELECT * FROM workflow_migrations
            WHERE workflow_run_id=:run_id ORDER BY created_at DESC
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    compensations = connection.execute(
        text(
            """
            SELECT * FROM workflow_compensation_runs
            WHERE workflow_run_id=:run_id ORDER BY sequence DESC
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    events = connection.execute(
        text(
            """
            SELECT id, event_type, actor, entity_type, entity_id, data, created_at
            FROM events
            WHERE project_id=:project_id AND entity_type='workflow_run' AND entity_id=:run_id
            ORDER BY created_at, id
            """
        ),
        {"project_id": project_id, "run_id": run_id},
    ).mappings().all()
    result = _serialize_run(row)
    result.update(
        {
            "workflow_key": row["workflow_key"],
            "workflow_name": row["workflow_name"],
            "version": int(row["version"]),
            "steps": [_serialize_step(step) for step in steps],
            "migrations": [_serialize_migration(item) for item in migrations],
            "compensations": [_serialize_compensation(item) for item in compensations],
            "timeline": [
                {
                    "id": event["id"],
                    "event_type": event["event_type"],
                    "actor": event["actor"],
                    "data": _json(event["data"], {}),
                    "created_at": _iso(event["created_at"]),
                }
                for event in events
            ],
        }
    )
    # WHAT AN OPERATOR MAY DO TO IT — the same decision every operation on it enforces. See
    # workflow_run_actions.
    from .workflow_run_actions import actions_for, standing

    result["actions"] = actions_for(connection, project_id, [run_id]).get(run_id, [])
    result["standing"] = standing(result.get("status"))
    return result


def list_runs(
    connection: Connection,
    project_id: str,
    *,
    status: Optional[str] = None,
    limit: int = 50,
    include_test: bool = False,
    only_test: bool = False,
) -> list[dict[str, Any]]:
    """Runs of this project, newest first. Test runs (workflow_test_runs) are left out unless
    `include_test`; `only_test` lists nothing else."""
    if limit < 1 or limit > 200:
        raise WorkflowValidationError("limit must be between 1 and 200.")
    status_clause = "AND r.status=:status" if status else ""
    if only_test:
        status_clause += " AND r.mode = 'test'"
    elif not include_test:
        status_clause += " AND r.mode <> 'test'"
    params: dict[str, Any] = {"project_id": project_id, "limit": limit}
    if status:
        params["status"] = status
    rows = connection.execute(
        text(
            f"""
            SELECT r.*, d.workflow_key, d.name AS workflow_name, v.version
            FROM workflow_runs r
            JOIN workflow_definitions d ON d.id=r.workflow_definition_id
            JOIN workflow_versions v ON v.id=r.workflow_version_id
            WHERE r.project_id=:project_id {status_clause}
            ORDER BY r.updated_at DESC LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    items = []
    for row in rows:
        item = _serialize_run(row)
        item.update(
            {
                "workflow_key": row["workflow_key"],
                "workflow_name": row["workflow_name"],
                "version": int(row["version"]),
            }
        )
        items.append(item)
    # THE AGENT'S LIST CARRIES THE SAME ACTIONS AS THE CONSOLE'S. Without them the agent and the
    # MCP list surface could not see what a run offers, and would learn it only by trying —
    # batched, so a page of runs costs three queries, not three per run.
    from .workflow_run_actions import actions_for, standing

    offered = actions_for(connection, project_id, [item["id"] for item in items])
    for item in items:
        item["actions"] = offered.get(item["id"], [])
        item["standing"] = standing(item.get("status"))
    return items


def _run_context(connection: Connection, run_id: str) -> dict[str, Any]:
    run = _run_row(connection, run_id)
    rows = connection.execute(
        text(
            """
            SELECT step_id, iteration, status, output
            FROM workflow_step_runs WHERE workflow_run_id=:run_id
            ORDER BY iteration, created_at
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    outputs: dict[str, Any] = {}
    for row in rows:
        key = row["step_id"]
        value = {
            "status": row["status"],
            "iteration": int(row["iteration"]),
            "output": _json(row["output"], {}),
        }
        if key in outputs:
            existing = outputs[key]
            if not isinstance(existing, list):
                outputs[key] = [existing]
            outputs[key].append(value)
        else:
            outputs[key] = value
    return {
        "input": _json(run["input"], {}),
        "context": _json(run["context"], {}),
        "steps": outputs,
    }


def _lookup_path(payload: Any, path: str) -> tuple[bool, Any]:
    parts = path.strip().lstrip("$").lstrip(".").split(".") if path else []
    value = payload
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value


def evaluate_expression(expression: dict[str, Any], payload: dict[str, Any]) -> bool:
    if not isinstance(expression, dict):
        raise WorkflowValidationError("expression must be an object.")
    if "all" in expression:
        values = expression["all"]
        if not isinstance(values, list):
            raise WorkflowValidationError("expression.all must be a list.")
        return all(evaluate_expression(item, payload) for item in values)
    if "any" in expression:
        values = expression["any"]
        if not isinstance(values, list):
            raise WorkflowValidationError("expression.any must be a list.")
        return any(evaluate_expression(item, payload) for item in values)
    if "not" in expression:
        return not evaluate_expression(expression["not"], payload)
    path = _clean_text(expression.get("path"), "expression.path")
    operator = str(expression.get("op", "truthy")).lower()
    exists, actual = _lookup_path(payload, path)
    expected = expression.get("value")
    if operator == "exists":
        return exists
    if operator == "truthy":
        return exists and bool(actual)
    if operator == "eq":
        return exists and actual == expected
    if operator == "ne":
        return not exists or actual != expected
    if operator == "in":
        return exists and isinstance(expected, list) and actual in expected
    if operator == "not_in":
        return not exists or not isinstance(expected, list) or actual not in expected
    if operator == "contains":
        # Text that contains `value` (ignoring case), or a list holding it: "does the plan have
        # an Increment 3" is a question about a document a step wrote.
        if not exists:
            return False
        if isinstance(actual, str):
            return str(expected).lower() in actual.lower()
        return isinstance(actual, list) and expected in actual
    if operator in {"gt", "gte", "lt", "lte"}:
        if not exists:
            return False
        try:
            if operator == "gt":
                return actual > expected
            if operator == "gte":
                return actual >= expected
            if operator == "lt":
                return actual < expected
            return actual <= expected
        except TypeError:
            return False
    raise WorkflowValidationError(
        "expression.op must be exists, truthy, eq, ne, in, not_in, contains, gt, gte, lt, "
        "or lte."
    )


def _emit_step_event(
    connection: Connection,
    run: Any,
    event_type: str,
    step: Any,
    data: Optional[dict[str, Any]] = None,
) -> None:
    emit_event(
        connection,
        run["project_id"],
        event_type,
        run["actor"],
        "workflow_run",
        run["id"],
        {
            "workflow_step_run_id": step["id"],
            "step_id": step["step_id"],
            "step_kind": step["step_kind"],
            "iteration": int(step["iteration"]),
            **(data or {}),
        },
    )


def _activate_step(
    connection: Connection, run_id: str, step_id: str, iteration: int = 0
) -> None:
    run = _run_row(connection, run_id)
    if run["status"] in TERMINAL_RUN_STATES or run["status"] in {"paused", "compensating"}:
        return
    spec = _run_spec(connection, run_id)
    template = _step_map(spec).get(step_id)
    if not template:
        raise WorkflowValidationError(f"workflow step '{step_id}' is missing from the pinned version.")
    row = _step_row(connection, run_id, step_id, iteration)
    if row is None:
        _insert_step_run(
            connection,
            project_id=run["project_id"],
            run_id=run_id,
            step=template,
            iteration=iteration,
            status="pending",
        )
        row = _step_row(connection, run_id, step_id, iteration)
    if row["status"] != "pending":
        return
    # A PINNED STEP (a test run's or a re-run's) is settled with its pinned output instead of
    # running; a test run refuses a step that could spend or reach outside BOSS. Either way the
    # step never becomes `ready`, so no runner can claim it (workflow_test_runs).
    from . import workflow_test_runs

    if workflow_test_runs.on_activate(connection, run, template, row):
        return
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs SET status='ready', run_after=now(), updated_at=now()
            WHERE id=:id AND status='pending'
            """
        ),
        {"id": row["id"]},
    )
    ready = _step_row(connection, run_id, step_id, iteration)
    _emit_step_event(connection, run, "workflow.step_ready", ready)
    _refresh_frontier(connection, run_id)


def _mark_skipped(connection: Connection, run_id: str, step_id: str, reason: str) -> None:
    row = _step_row(connection, run_id, step_id, 0)
    if not row or row["status"] != "pending":
        return
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='skipped', output=CAST(:output AS JSONB), completed_at=clock_timestamp(), updated_at=now()
            WHERE id=:id AND status='pending'
            """
        ),
        {"id": row["id"], "output": json.dumps({"reason": reason})},
    )
    run = _run_row(connection, run_id)
    skipped = _step_row(connection, run_id, step_id, 0)
    _emit_step_event(
        connection, run, "workflow.step_skipped", skipped, {"reason": reason}
    )


def _succeed_internal(
    connection: Connection,
    run_id: str,
    step_id: str,
    iteration: int,
    output: dict[str, Any],
) -> Any:
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='succeeded', output=CAST(:output AS JSONB),
                lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                completed_at=clock_timestamp(), updated_at=now()
            WHERE workflow_run_id=:run_id AND step_id=:step_id AND iteration=:iteration
            """
        ),
        {
            "run_id": run_id,
            "step_id": step_id,
            "iteration": iteration,
            "output": json.dumps(output or {}),
        },
    )
    if output:
        connection.execute(
            text(
                """
                UPDATE workflow_runs
                SET context=context || CAST(:output AS JSONB), updated_at=now()
                WHERE id=:run_id
                """
            ),
            {"run_id": run_id, "output": json.dumps(output)},
        )
    row = _step_row(connection, run_id, step_id, iteration)
    run = _run_row(connection, run_id)
    _emit_step_event(connection, run, "workflow.step_succeeded", row, {"output": output})
    return row


def _after_success(
    connection: Connection,
    run_id: str,
    step_id: str,
    iteration: int,
) -> None:
    run = _run_row(connection, run_id)
    if run["status"] not in {"running", "waiting"}:
        _refresh_frontier(connection, run_id)
        return
    spec = _run_spec(connection, run_id)
    steps = _step_map(spec)
    template = steps[step_id]

    # A for_each's body returns to its for_each, which starts the next item or finishes.
    each = for_each_bodies(spec).get(step_id)
    if each in steps:
        _advance_for_each(connection, run_id, steps[each])
        _drain_control_steps(connection, run_id)
        _maybe_finish_run(connection, run_id)
        return

    # A loop body returns to its controller rather than following ordinary
    # downstream edges.  Each iteration gets distinct durable step rows.
    for controller in spec["steps"]:
        if controller["kind"] == "bounded_loop" and controller.get("body") == step_id:
            waiting = _step_row(connection, run_id, controller["id"], iteration)
            if waiting and waiting["status"] == "waiting":
                _succeed_internal(
                    connection,
                    run_id,
                    controller["id"],
                    iteration,
                    {"continued": True, "iteration": iteration},
                )
                _activate_step(connection, run_id, controller["id"], iteration + 1)
                _drain_control_steps(connection, run_id)
                return

    for target in template.get("next", []):
        _maybe_activate(connection, run_id, str(target), iteration)
    # IT DID NOT FAIL, so its error path was not taken. The error step is skipped unless a live
    # path still reaches it — the same rule as the path a condition did not take (#550).
    error_step = str(template.get("on_error") or "").strip()
    if error_step in steps:
        _skip_unless_reachable(connection, run_id, step_id, error_step, None, spec,
                               reason="error_path_not_taken")
    _drain_control_steps(connection, run_id)
    _maybe_finish_run(connection, run_id)


def _maybe_activate(
    connection: Connection, run_id: str, step_id: str, iteration: int = 0
) -> None:
    spec = _run_spec(connection, run_id)
    template = _step_map(spec)[step_id]
    if template["kind"] != "join":
        _activate_step(connection, run_id, step_id, iteration)
        return
    dependency_rows = connection.execute(
        text(
            """
            SELECT step_id, status, output FROM workflow_step_runs
            WHERE workflow_run_id=:run_id AND iteration=:iteration
              AND step_id IN :step_ids
            """
        ).bindparams(bindparam("step_ids", expanding=True)),
        {
            "run_id": run_id,
            "iteration": iteration,
            "step_ids": tuple(template["wait_for"]),
        },
    ).mappings().all()
    settled = {row["step_id"] for row in dependency_rows if _settled_for_downstream(row)}
    policy = template.get("success_policy", "all")
    ready = (
        all(item in settled for item in template["wait_for"])
        if policy == "all"
        else any(item in settled for item in template["wait_for"])
    )
    if ready:
        _activate_step(connection, run_id, step_id, iteration)
        if policy == "any":
            # Branches that failed before this released are now gone on without — say so on
            # each, so none of them stops the run later (see `_an_any_join_may_go_on_without`).
            _stamp_tolerated(connection, run_id, step_id, iteration,
                             [str(w) for w in template["wait_for"]])


def _drain_control_steps(connection: Connection, run_id: str) -> None:
    for _ in range(500):
        run = _run_row(connection, run_id)
        if run["status"] not in {"running", "waiting"}:
            return
        row = connection.execute(
            text(
                """
                SELECT * FROM workflow_step_runs
                WHERE workflow_run_id=:run_id AND status='ready'
                  AND step_kind IN (
                    'condition', 'parallel', 'join', 'bounded_loop', 'for_each',
                    'decision', 'timer', 'subworkflow'
                  )
                  AND run_after <= now()
                ORDER BY created_at, iteration, step_id LIMIT 1
                """
            ),
            {"run_id": run_id},
        ).mappings().first()
        if not row:
            break
        _process_control_step(connection, run, row)
    else:
        raise WorkflowConflictError("Workflow control-step drain exceeded its safety bound.")
    _refresh_frontier(connection, run_id)
    _maybe_finish_run(connection, run_id)


def _reachable_from(spec: dict[str, Any], starts: list[str]) -> set[str]:
    """Every step a path starting at any of `starts` can reach, `starts` included.

    Follows every edge (`_targets`): both paths of a condition, every branch of a parallel and
    a loop's body, because which way a later condition goes is not known yet.
    """
    steps = _step_map(spec)
    seen: set[str] = set()
    stack = [start for start in starts if start in steps]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(t for t in _targets(steps[current]) if t in steps and t not in seen)
    return seen


def _skip_unless_reachable(
    connection: Connection, run_id: str, condition_id: str, rejected: str,
    selected: Optional[str], spec: dict[str, Any], *, reason: str = "condition_not_selected",
) -> None:
    """Skip the first step of the path a condition did not take — unless a live path reaches it.

    A PATH NOT TAKEN MAY STILL BE ARRIVED AT. "If urgent, hotfix then deploy; otherwise deploy"
    is `on_true: hotfix`, `on_false: deploy`, `hotfix → deploy`. Skipping `deploy` because it
    heads the "no" path meant it never ran on "yes" (a skipped step is never started again,
    `_activate_step`), and the run completed without deploying (#550). Four shipped specpack
    workflows have that shape.

    So the step is skipped only when no live path can reach it: not the path just taken, and
    not any step still in flight (a parallel sibling, a loop waiting on its body). Otherwise it
    stays pending, and the event says so: a live path starts it when it gets there, a later
    condition that goes the other way skips it then, and a run that finishes without reaching
    it records it `path_not_reached` (`_maybe_finish_run`). It is never silently dropped.

    An error path not taken is the same question (`reason="error_path_not_taken"`): the step
    that succeeded is `condition_id`, its `on_error` step is `rejected`, and nothing is
    `selected` beyond what is already live.
    """
    live = ([selected] if selected else []) + [str(r) for r in connection.execute(
        text(
            """
            SELECT step_id FROM workflow_step_runs
            WHERE workflow_run_id=:run_id
              AND status IN ('ready', 'leased', 'running', 'waiting')
            """
        ),
        {"run_id": run_id},
    ).scalars().all()]
    if rejected not in _reachable_from(spec, live):
        _mark_skipped(connection, run_id, rejected, reason)
        return
    row = _step_row(connection, run_id, rejected, 0)
    if row is None or row["status"] != "pending":
        return
    _emit_step_event(
        connection, _run_row(connection, run_id), "workflow.step_not_skipped", row,
        {"reason": "reachable_from_live_path", "condition": condition_id,
         "selected": selected}
        if reason == "condition_not_selected" else
        {"reason": "reachable_from_live_path", "error_path_of": condition_id,
         "not_taken_because": reason},
    )


def _for_each_items(template: dict[str, Any],
                    context: dict[str, Any]) -> tuple[Optional[list[Any]], Optional[str]]:
    """The list a for_each goes through, or the sentence saying why there is none to use."""
    sid = str(template["id"])
    raw = template.get("items")
    maximum = int(template.get("max_items") or FOR_EACH_DEFAULT_MAX_ITEMS)
    if isinstance(raw, list):
        where, value = "the list on this step", raw
    else:
        where = str(raw or "")
        exists, value = _lookup_path(context, where)
        if not exists:
            return None, (f"for_each '{sid}' found nothing at {where}: the run has no such "
                          "value, so there is no list to go through")
    if not isinstance(value, list):
        return None, (f"for_each '{sid}' reads {where}, which is not a list (it is "
                      f"{type(value).__name__}), so there is nothing to go through one by one")
    if len(value) > maximum:
        return None, (f"for_each '{sid}' got {len(value)} items from {where} and goes through "
                      f"at most {maximum} (max_items). Raise max_items or narrow the list; "
                      "BOSS does not cut a list short")
    return value, None


def _fail_for_each(connection: Connection, run_id: str, row: Any, reason: str,
                   **data: Any) -> None:
    """A for_each that cannot finish: it fails with `reason`, and the run stops at it."""
    connection.execute(
        text("UPDATE workflow_step_runs SET status='failed', last_error=:reason, "
             "completed_at=clock_timestamp(), updated_at=now() WHERE id=:id"),
        {"id": row["id"], "reason": reason[:2000]},
    )
    run = _run_row(connection, run_id)
    failed = _step_row(connection, run_id, str(row["step_id"]), int(row["iteration"]))
    _emit_step_event(connection, run, "workflow.for_each_failed", failed,
                     {"reason": reason[:500], **data})
    _stop_run_at(connection, run_id, failed)


def _fail_for_each_item(connection: Connection, run_id: str, body_row: Any, each_id: str,
                        error: str) -> None:
    """A body run failed for good (its own attempts used up): its for_each fails with it."""
    connection.execute(
        text("UPDATE workflow_step_runs SET output = COALESCE(output, '{}'::jsonb) "
             "|| CAST(:mark AS JSONB), updated_at=now() WHERE id=:id"),
        {"id": body_row["id"], "mark": json.dumps({FOR_EACH_FAILED: each_id})},
    )
    each = _step_row(connection, run_id, each_id, 0)
    if each is None or each["status"] != "waiting":
        _refresh_frontier(connection, run_id)
        return
    index = int(body_row["iteration"])
    count = int((_json(each["output"], {}) or {}).get("count") or 0)
    why = str(error or "its work failed").strip().rstrip(".")
    _fail_for_each(connection, run_id, each, f"item {index + 1} of {count} failed: {why}",
                   item_index=index, body_step_id=str(body_row["step_id"]))


def _activate_for_each_item(connection: Connection, run_id: str, each_id: str,
                            body: dict[str, Any], index: int, item: Any, count: int) -> None:
    """Start the body for one item: its own step run, numbered by the item's index, with the
    item on its `input` (what a worker reads it from, `workflow_capability_runner.item_context`)."""
    body_id = str(body["id"])
    if _step_row(connection, run_id, body_id, index) is None:
        run = _run_row(connection, run_id)
        _insert_step_run(connection, project_id=run["project_id"], run_id=run_id, step=body,
                         iteration=index, status="pending")
    connection.execute(
        text("UPDATE workflow_step_runs SET input = COALESCE(input, '{}'::jsonb) "
             "|| CAST(:item AS JSONB), updated_at=now() WHERE workflow_run_id=:r "
             "AND step_id=:s AND iteration=:i AND status='pending'"),
        {"r": run_id, "s": body_id, "i": index,
         "item": json.dumps({"for_each": each_id, "item": item, "item_index": index,
                             "item_count": count})},
    )
    _activate_step(connection, run_id, body_id, index)
    started = _step_row(connection, run_id, body_id, index)
    if started is not None and started["status"] == "ready":
        _emit_step_event(connection, _run_row(connection, run_id),
                         "workflow.for_each_item_started", started,
                         {"for_each": each_id, "item_index": index, "item_count": count})


def _start_for_each(connection: Connection, run: Any, row: Any, template: dict[str, Any],
                    context: dict[str, Any]) -> None:
    """A for_each is reached (or retried): resolve its list once, then start its items.

    THE LIST IS KEPT ON THE STEP. Resolved when the for_each is first reached and stored on its
    output, so the items a Retry runs are the items the first pass ran, whatever an earlier
    step's output says by then. A Retry re-queues only the items that failed; those that
    succeeded keep their results.
    """
    run_id, sid = run["id"], str(row["step_id"])
    previous = _json(row["output"], {}) or {}
    items = previous.get("items") if isinstance(previous.get("items"), list) else None
    retried = items is not None
    if items is None:
        items, refusal = _for_each_items(template, context)
        if refusal:
            _fail_for_each(connection, run_id, row, refusal)
            return
    assert items is not None
    body_id = str(template["body"])
    count = len(items)
    data = {"count": count, "concurrency": int(template.get("concurrency") or 1),
            "max_items": int(template.get("max_items") or FOR_EACH_DEFAULT_MAX_ITEMS),
            "body": body_id, **({"retried": True} if retried else {})}
    if not count:
        _emit_step_event(connection, run, "workflow.for_each_started", row, data)
        _succeed_internal(connection, run_id, sid, int(row["iteration"]),
                          {"for_each": sid, "count": 0, "done": 0, "results": []})
        _mark_skipped(connection, run_id, body_id, "no_items")
        _emit_step_event(connection, run, "workflow.for_each_finished",
                         _step_row(connection, run_id, sid, int(row["iteration"])),
                         {"count": 0})
        _after_success(connection, run_id, sid, int(row["iteration"]))
        return
    if retried:
        connection.execute(
            text("UPDATE workflow_step_runs SET status='pending', attempt=0, last_error=NULL, "
                 "started_at=NULL, completed_at=NULL, lease_owner=NULL, lease_expires_at=NULL, "
                 "heartbeat_at=NULL, attempt_timeout_at=NULL, run_after=now(), "
                 "output = COALESCE(output, '{}'::jsonb) - CAST(:mark AS TEXT), "
                 "updated_at=now() WHERE workflow_run_id=:r AND step_id=:s "
                 "AND status IN ('failed', 'deadlettered', 'cancelled')"),
            {"r": run_id, "s": body_id, "mark": FOR_EACH_FAILED},
        )
    connection.execute(
        text("UPDATE workflow_step_runs SET status='waiting', last_error=NULL, "
             "completed_at=NULL, output = COALESCE(output, '{}'::jsonb) || CAST(:out AS JSONB), "
             "updated_at=now() WHERE id=:id"),
        {"id": row["id"], "out": json.dumps({"for_each": sid, "items": items, "count": count,
                                             "done": 0})},
    )
    _emit_step_event(connection, run, "workflow.for_each_started",
                     _step_row(connection, run_id, sid, int(row["iteration"])), data)
    _advance_for_each(connection, run_id, template)


def _advance_for_each(connection: Connection, run_id: str, template: dict[str, Any]) -> None:
    """Where a waiting for_each stands: start items up to its concurrency, or finish.

    Idempotent, and a no-op unless the for_each is waiting. Called when one of its items
    succeeds (`_after_success`), when it is reached, and when a stopped run moves again.
    Its output is the ordered list of body outputs — by item, never by finishing order. An item
    an operator skipped counts as done, and its place in `results` says it was skipped.
    """
    run = _run_row(connection, run_id)
    if run["status"] not in {"running", "waiting"}:
        return
    sid, body_id = str(template["id"]), str(template["body"])
    each = _step_row(connection, run_id, sid, 0)
    if each is None or each["status"] != "waiting":
        return
    out = _json(each["output"], {}) or {}
    items = out.get("items") if isinstance(out.get("items"), list) else []
    count = len(items)
    rows = {int(r["iteration"]): r for r in connection.execute(
        text("SELECT * FROM workflow_step_runs WHERE workflow_run_id=:r AND step_id=:s"),
        {"r": run_id, "s": body_id}).mappings().all()}
    done = [i for i in range(count) if i in rows and _settled_for_downstream(rows[i])]
    if len(done) == count:
        results = [
            _json(rows[i]["output"], {}) if rows[i]["status"] == "succeeded"
            else {"skipped": True, "reason": (_json(rows[i]["output"], {}) or {}).get(
                "skip_reason")}
            for i in range(count)
        ]
        finished = _succeed_internal(connection, run_id, sid, 0,
                                     {"for_each": sid, "count": count, "done": count,
                                      "results": results})
        _emit_step_event(connection, run, "workflow.for_each_finished", finished,
                         {"count": count})
        _after_success(connection, run_id, sid, 0)
        return
    if int(out.get("done") or 0) != len(done):
        connection.execute(
            text("UPDATE workflow_step_runs SET output = output || CAST(:d AS JSONB), "
                 "updated_at=now() WHERE id=:id"),
            {"id": each["id"], "d": json.dumps({"done": len(done)})},
        )
    live = sum(1 for r in rows.values() if r["status"] in ACTIVE_STEP_STATES)
    room = int(template.get("concurrency") or 1) - live
    body = _step_map(_run_spec(connection, run_id))[body_id]
    for index in range(count):
        if room <= 0:
            break
        if index not in rows or rows[index]["status"] == "pending":
            _activate_for_each_item(connection, run_id, sid, body, index, items[index], count)
            room -= 1


def _process_control_step(connection: Connection, run: Any, row: Any) -> None:
    spec = _run_spec(connection, run["id"])
    template = _step_map(spec)[row["step_id"]]
    kind = template["kind"]
    iteration = int(row["iteration"])
    context = _run_context(connection, run["id"])
    if kind == "condition":
        passed = evaluate_expression(template["expression"], context)
        selected = template["on_true"] if passed else template["on_false"]
        rejected = template["on_false"] if passed else template["on_true"]
        _succeed_internal(
            connection,
            run["id"],
            row["step_id"],
            iteration,
            {"result": passed, "selected": selected},
        )
        _skip_unless_reachable(connection, run["id"], row["step_id"], rejected, selected,
                               spec)
        _activate_step(connection, run["id"], selected, iteration)
        return
    if kind == "parallel":
        _succeed_internal(
            connection,
            run["id"],
            row["step_id"],
            iteration,
            {"branches": template["branches"], "join": template["join"]},
        )
        for branch in template["branches"]:
            _activate_step(connection, run["id"], branch, iteration)
        return
    if kind == "join":
        _succeed_internal(
            connection,
            run["id"],
            row["step_id"],
            iteration,
            {"joined": template["wait_for"], "policy": template.get("success_policy", "all")},
        )
        _after_success(connection, run["id"], row["step_id"], iteration)
        return
    if kind == "bounded_loop":
        should_continue = evaluate_expression(template["expression"], context)
        maximum = int(template["max_iterations"])
        if should_continue and iteration >= maximum:
            reason = (
                f"bounded loop '{row['step_id']}' exhausted max_iterations={maximum}."
            )
            connection.execute(
                text(
                    """
                    UPDATE workflow_step_runs SET status='failed', last_error=:reason,
                        completed_at=clock_timestamp(), updated_at=now() WHERE id=:id
                    """
                ),
                {"id": row["id"], "reason": reason},
            )
            _block_run(connection, run["id"], reason)
            return
        if not should_continue:
            _succeed_internal(
                connection,
                run["id"],
                row["step_id"],
                iteration,
                {"continued": False, "iteration": iteration},
            )
            for target in template.get("next", []):
                _maybe_activate(connection, run["id"], target, 0)
            return
        connection.execute(
            text(
                """
                UPDATE workflow_step_runs SET status='waiting',
                    output=CAST(:output AS JSONB), updated_at=now()
                WHERE id=:id
                """
            ),
            {
                "id": row["id"],
                "output": json.dumps({"continued": True, "iteration": iteration}),
            },
        )
        _activate_step(connection, run["id"], template["body"], iteration)
        return
    if kind == "for_each":
        _start_for_each(connection, run, row, template, context)
        return
    if kind == "timer":
        wake_at = now_utc() + timedelta(seconds=int(template.get("wait_seconds", 0)))
        connection.execute(
            text(
                """
                UPDATE workflow_step_runs
                SET status='waiting', run_after=:wake_at,
                    output=CAST(:output AS JSONB), updated_at=now()
                WHERE id=:id
                """
            ),
            {
                "id": row["id"],
                "wake_at": wake_at,
                "output": json.dumps({"wake_at": wake_at.isoformat()}),
            },
        )
        connection.execute(
            text("UPDATE workflow_runs SET status='waiting', updated_at=now() "
                 "WHERE id=:id AND status IN ('running','waiting')"),
            {"id": run["id"]},
        )
        _emit_step_event(
            connection,
            run,
            "workflow.step_waiting_timer",
            _step_row(connection, run["id"], row["step_id"], iteration),
            {"wake_at": wake_at.isoformat()},
        )
        return
    if kind == "decision":
        # WHAT A REVIEWER NEEDS IN ORDER TO ANSWER.
        #
        # A gate used to arrive as a bare question and "Workflow wfr_… is waiting at decision
        # step publish." Nobody can approve that. Reviewing a VIDEO means watching it, so the
        # link has to be here; reviewing a PRD means reading it, so the document does.
        #
        # There is no taxonomy of approval TYPES, deliberately — the run already knows what it
        # is doing. The step's author writes one sentence of guidance, and this joins it to
        # whatever the earlier steps actually produced. The same mechanism serves a video, a
        # spec and a deploy without any of them being a special case.
        review = _what_there_is_to_review(connection, run["id"])
        question = str(template.get("question") or "").strip() or (
            f"Approve {row['step_id']} in {run.get('workflow_key') or 'this workflow'}")
        guidance = str(template.get("guidance") or "").strip()
        recommendation = {
            **(template.get("recommendation") or {}),
            **({"what_to_check": guidance} if guidance else {}),
            **({"review": review} if review else {}),
            # LAST, so a spec's own recommendation cannot replace it. This card is the step's
            # gate: approving it must release the step, and the buttons the inbox draws are
            # decided by this action (decision_actions). It came first, before the spread.
            "action": "resume_workflow",
        }
        where = ""
        if review.get("view_url"):
            where = f" Review it at {review['view_url']}."
        elif review.get("produced_by"):
            where = f" It was produced by step '{review['produced_by']}'."
        # ONE GATE, ONE RUN. The card belongs to this run's step: open_step_decision records
        # whose question it is (run_decisions withdraws it when the run ends) and keeps a second
        # run at the same gate from being merged onto this card — one Approve used to release
        # every run waiting here.
        from .run_decisions import open_step_decision

        decision_id = open_step_decision(
            connection,
            run["project_id"],
            None,
            question,
            recommendation,
            template.get("cost_summary") or {},
            (f"{guidance} " if guidance else "")
            + f"Workflow {run['id']} is waiting at decision step {row['step_id']}.{where}",
            run_id=run["id"],
            step_id=row["step_id"],
            risk_tier=int(template.get("risk_tier", 2)),
            default_action=str(template.get("default_action", "hold")),
        )
        connection.execute(
            text(
                """
                UPDATE workflow_step_runs
                SET status='waiting', decision_id=:decision_id, updated_at=now()
                WHERE id=:id
                """
            ),
            {"id": row["id"], "decision_id": decision_id},
        )
        connection.execute(
            text("UPDATE workflow_runs SET status='waiting', updated_at=now() "
                 "WHERE id=:id AND status IN ('running','waiting')"),
            {"id": run["id"]},
        )
        _emit_step_event(
            connection,
            run,
            "workflow.step_waiting_decision",
            _step_row(connection, run["id"], row["step_id"], iteration),
            {"decision_id": decision_id},
        )
        return
    if kind == "subworkflow":
        binding = template["binding"]
        child = start_run(
            connection,
            project_id=run["project_id"],
            actor=run["actor"],
            input_data=template.get("input") or {},
            dedupe_key=f"subworkflow:{run['id']}:{row['step_id']}:{iteration}",
            workflow_version_id=binding["workflow_version_id"],
            policy_snapshot=_json(run["policy_snapshot"], {}),
            parent_run_id=run["id"],
        )
        connection.execute(
            text(
                """
                UPDATE workflow_step_runs
                SET status='waiting', child_workflow_run_id=:child_id, updated_at=now()
                WHERE id=:id
                """
            ),
            {"id": row["id"], "child_id": child["id"]},
        )
        connection.execute(
            text("UPDATE workflow_runs SET status='waiting', updated_at=now() "
                 "WHERE id=:id AND status IN ('running','waiting')"),
            {"id": run["id"]},
        )
        return


#: How a decision step's answer ends its wait: approved releases it, the rest refuse it.
_DECISION_RELEASES = frozenset({"approved"})
_DECISION_REFUSES = frozenset({"rejected", "expired"})
#: How a child run ends a "Run another workflow" step's wait: completed releases it, and ANY
#: other terminal state blocks the parent. Derived from `TERMINAL_RUN_STATES`, so a run state
#: added there can never leave a parent waiting on a child that is over.
_CHILD_RELEASES = frozenset({"completed"})
_CHILD_BLOCKS = frozenset(TERMINAL_RUN_STATES) - _CHILD_RELEASES

#: What `_wait_outcome` can say about a waiting step.
WAIT_RELEASE, WAIT_REFUSE, WAIT_BLOCK = "release", "refuse", "block"


def _wait_outcome(
    step_kind: str,
    *,
    run_after: Optional[datetime],
    decision_status: Optional[str],
    child_status: Optional[str],
    now: datetime,
) -> Optional[str]:
    """THE one rule for when a waiting timer, gate or "Run another workflow" step is over, and
    how: `WAIT_RELEASE` (the step succeeds), `WAIT_REFUSE` (the gate's step fails and the run is
    blocked), `WAIT_BLOCK` (the run is blocked) — or None, still waiting.

    `advance_run` acts on it and `a_wait_is_over` (the product tick's question) asks it. They
    used to hold two copies of these conditions, one in Python and one in SQL, and nothing kept
    them equal: a kind or state added to one and not the other is a run left `waiting` for ever
    with no error. Other kinds (loops, capability steps) are not released here.
    """
    if step_kind == "timer":
        return WAIT_RELEASE if run_after is not None and run_after <= now else None
    if step_kind == "decision":
        if decision_status in _DECISION_RELEASES:
            return WAIT_RELEASE
        if decision_status in _DECISION_REFUSES:
            return WAIT_REFUSE
        return None
    if step_kind == "subworkflow":
        if child_status in _CHILD_RELEASES:
            return WAIT_RELEASE
        if child_status in _CHILD_BLOCKS:
            return WAIT_BLOCK
        return None
    return None


def _waiting_steps(connection: Connection, run_id: str) -> list[dict[str, Any]]:
    """A run's waiting steps, each with what `_wait_outcome` reads: its gate's and child's status."""
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT s.*, d.status AS decision_status, c.status AS child_status,
                       c.output AS child_output
                FROM workflow_step_runs s
                LEFT JOIN decisions d ON d.id = s.decision_id
                LEFT JOIN workflow_runs c ON c.id = s.child_workflow_run_id
                WHERE s.workflow_run_id = :run_id AND s.status = 'waiting'
                ORDER BY s.created_at, s.iteration
                """
            ),
            {"run_id": run_id},
        ).mappings().all()
    ]


def _outcome_of(step: dict[str, Any], now: datetime) -> Optional[str]:
    return _wait_outcome(
        str(step["step_kind"]),
        run_after=step.get("run_after"),
        decision_status=step.get("decision_status"),
        child_status=step.get("child_status"),
        now=now,
    )


def a_wait_is_over(connection: Connection, run_id: str) -> bool:
    """Whether one of this run's waiting steps can now move: a timer that is due, a gate a
    person has answered, a child run that has ended.

    The product tick asks this before calling `advance_run`, which is the only code that
    releases those three kinds of wait. Both read `_wait_outcome`, so they cannot disagree
    about what "over" means; capability steps waiting on a gate or on outside work are the
    runner's own.
    """
    now = now_utc()
    return any(_outcome_of(step, now) is not None for step in _waiting_steps(connection, run_id))


def advance_run(connection: Connection, project_id: str, run_id: str) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] in TERMINAL_RUN_STATES or run["status"] in {"paused", "compensating"}:
        return get_run(connection, project_id, run_id)
    progressed = False
    now = now_utc()
    for step in _waiting_steps(connection, run_id):
        outcome = _outcome_of(step, now)
        if outcome is None:
            # Still waiting. Loop steps resume only when their body completes.
            continue
        step_id, iteration = step["step_id"], int(step["iteration"])
        if outcome == WAIT_RELEASE:
            if step["step_kind"] == "timer":
                output: dict[str, Any] = {"woke_at": now_utc().isoformat()}
            elif step["step_kind"] == "decision":
                output = {"decision_id": step["decision_id"], "outcome": "approved"}
            else:
                output = {
                    "child_workflow_run_id": step["child_workflow_run_id"],
                    "output": _json(step["child_output"], {}),
                }
            _succeed_internal(connection, run_id, step_id, iteration, output)
            _after_success(connection, run_id, step_id, iteration)
        elif outcome == WAIT_REFUSE:
            # THE STEP HAS TO SAY SO TOO.
            #
            # `_block_run` updates `workflow_runs` and nothing else, so a rejected gate left
            # its step `waiting` — for ever, and with no record of the answer. The operator
            # pressed Reject, watched the gate go on saying "waiting", and had nothing to
            # tell them their decision had landed. The run was blocked the whole time.
            reject_gate(connection, run_id, step_id, iteration,
                        decision_id=str(step["decision_id"]),
                        outcome=str(step["decision_status"]))
        else:
            _block_run(
                connection,
                run_id,
                f"subworkflow {step['child_workflow_run_id']} ended in {step['child_status']}",
            )
        progressed = True
    if progressed:
        connection.execute(
            text(
                """
                UPDATE workflow_runs SET status='running', updated_at=now()
                WHERE id=:id AND status='waiting'
                """
            ),
            {"id": run_id},
        )
    _drain_control_steps(connection, run_id)
    _maybe_finish_run(connection, run_id)
    return get_run(connection, project_id, run_id)


def _refresh_frontier(connection: Connection, run_id: str) -> None:
    rows = connection.execute(
        text(
            """
            SELECT id, step_id, step_kind, iteration, status, lease_owner,
                   lease_expires_at, run_after, decision_id, child_workflow_run_id
            FROM workflow_step_runs
            WHERE workflow_run_id=:run_id
              AND status IN ('ready', 'leased', 'running', 'waiting')
            ORDER BY created_at, iteration, step_id
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    frontier = [
        {
            "id": row["id"],
            "step_id": row["step_id"],
            "step_kind": row["step_kind"],
            "iteration": int(row["iteration"]),
            "status": row["status"],
            "lease_owner": row.get("lease_owner"),
            "lease_expires_at": _iso(row.get("lease_expires_at")),
            "run_after": _iso(row.get("run_after")),
            "decision_id": row.get("decision_id"),
            "child_workflow_run_id": row.get("child_workflow_run_id"),
        }
        for row in rows
    ]
    connection.execute(
        text(
            """
            UPDATE workflow_runs SET current_frontier=CAST(:frontier AS JSONB), updated_at=now()
            WHERE id=:id
            """
        ),
        {"id": run_id, "frontier": json.dumps(frontier)},
    )


#: A step whose work failed and that nobody has decided about yet. While a run has one, the run
#: cannot complete and cannot quietly declare its remaining steps unreachable: it is stopped at
#: that step until the operator retries it or skips it.
STOPPED_STEP_STATES = ("failed", "deadlettered")


def _step_label(template: dict[str, Any], step_id: str) -> str:
    """What the operator calls this step — the catalogue's label, not the internal id."""
    from .workflow_capabilities import CAPABILITY_KEYS, get as get_capability

    label = step_title(template) or str(template.get("label") or "").strip()
    if label:
        return label
    key = str((template.get("binding") or {}).get("capability") or "")
    if key in CAPABILITY_KEYS:
        return get_capability(key).label
    return step_id


def _named_step(connection: Connection, project_id: str, template: dict[str, Any],
                step_id: str) -> str:
    """`_step_label`, and for a customer's own step the label they gave it.

    A custom step's spec carries only its key (`binding.custom_step`), so a step with no title
    of its own was named by its id on the run card — "Stopped at extract" for a step the
    workflow calls "Read the meeting". The label lives on the custom step's row."""
    label = step_title(template) or str(template.get("label") or "").strip()
    key = str((template.get("binding") or {}).get("custom_step") or "")
    if not label and key:
        try:
            with connection.begin_nested():
                found = connection.execute(
                    text("SELECT label FROM workflow_custom_steps "
                         "WHERE project_id = :p AND step_key = :k"),
                    {"p": project_id, "k": key}).scalar()
        except Exception:  # noqa: BLE001 — a name is never worth failing a stop over
            found = None
        if found:
            return str(found)
    return _step_label(template, step_id)


#: Output key stamped on a failed branch that a `success_policy: any` join went on without.
TOLERATED_BY_JOIN = "tolerated_by_join"


def _any_joins_after(steps: dict[str, dict[str, Any]], step_id: str) -> list[str]:
    """The joins this step feeds, when EVERY next step is a join that releases on any branch.

    Only then can the run go on without it: a failed step that also feeds anything else is a
    hole in the run, whatever a join somewhere thinks."""
    targets = [str(t) for t in (steps.get(step_id) or {}).get("next", []) or []]
    joins = [t for t in targets
             if (steps.get(t) or {}).get("kind") == "join"
             and (steps.get(t) or {}).get("success_policy", "all") == "any"]
    return joins if targets and len(joins) == len(targets) else []


def _stamp_tolerated(connection: Connection, run_id: str, join_id: str, iteration: int,
                     wait_for: list[str]) -> None:
    """Record, on each failed branch of a released `any` join, that the run went on without it.

    THE PARTIAL FAILURE STAYS VISIBLE. The step keeps its failed status and its error — the
    bundled fan-out template promises to "make partial failures visible" — and gains the name
    of the join that released without it, which is what exempts it from stopping the run."""
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET output = COALESCE(output, '{}'::jsonb)
                         || jsonb_build_object(CAST(:key AS TEXT), CAST(:join_id AS TEXT)),
                updated_at = now()
            WHERE workflow_run_id=:run_id AND iteration=:iteration
              AND step_id IN :step_ids AND status IN ('failed', 'deadlettered')
              AND output->>CAST(:key AS TEXT) IS NULL
            """
        ).bindparams(bindparam("step_ids", expanding=True)),
        {"run_id": run_id, "iteration": iteration, "step_ids": tuple(wait_for),
         "key": TOLERATED_BY_JOIN, "join_id": join_id},
    )


def _an_any_join_may_go_on_without(connection: Connection, run_id: str, row: Any) -> bool:
    """Can a `success_policy: any` join still release without this failed step?

    THE FAN-OUT TEMPLATE COULD NOT TOLERATE ONE FAILED SCOPE. `agent_group_fan_out_join` joins
    three scope workers on `any` and says it "makes partial failures visible" — but once a
    failed step stopped its run, one failed worker stopped the whole run and `_maybe_finish_run`
    refused to complete past it (review of c8de36b). So a branch whose every next step is an
    `any` join does not stop the run while that join has released, or another of its branches
    has succeeded or is still working. If every branch fails, the last failure stops the run as
    any failure would.
    """
    steps = _step_map(_run_spec(connection, run_id))
    joins = _any_joins_after(steps, str(row["step_id"]))
    if not joins:
        return False
    iteration = int(row["iteration"])
    for join_id in joins:
        released = connection.execute(
            text("SELECT 1 FROM workflow_step_runs WHERE workflow_run_id=:r AND step_id=:s "
                 "AND iteration=:i AND status <> 'pending'"),
            {"r": run_id, "s": join_id, "i": iteration},
        ).first()
        wait_for = [str(w) for w in steps[join_id].get("wait_for", [])]
        if released:
            _stamp_tolerated(connection, run_id, join_id, iteration, wait_for)
            continue
        others = connection.execute(
            text("SELECT step_id, status, output FROM workflow_step_runs "
                 "WHERE workflow_run_id=:r AND iteration=:i AND step_id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"r": run_id, "i": iteration,
             "ids": tuple(w for w in wait_for if w != str(row["step_id"])) or ("",)},
        ).mappings().all()
        # `pending` counts as "may yet": a branch further up its chain can still reach the
        # join. If it never does, `_maybe_finish_run` finds this step unexcused when nothing is
        # left running, and stops the run at it then.
        if not any(o["status"] in ACTIVE_STEP_STATES or o["status"] == "pending"
                   or _settled_for_downstream(o) for o in others):
            return False
    return True


def _first_stopped_step(connection: Connection, run_id: str) -> Any:
    """The earliest step of this run that failed and has not been retried or skipped — and that
    no `any` join has gone on without (`TOLERATED_BY_JOIN`), nor the run gone on from along its
    own `on_error` path (`ERROR_PATH_TAKEN`)."""
    return connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE workflow_run_id=:run_id AND status IN :states
              AND output->>'tolerated_by_join' IS NULL
              AND output->>'error_path_taken' IS NULL
              AND output->>'for_each_failed' IS NULL
            ORDER BY created_at, iteration, step_id LIMIT 1
            """
        ).bindparams(bindparam("states", expanding=True)),
        {"run_id": run_id, "states": tuple(STOPPED_STEP_STATES)},
    ).mappings().first()


def _stop_run_at(connection: Connection, run_id: str, row: Any) -> None:
    """Stop the run at a failed step, saying so in a sentence an operator can act on.

    THE ONE PLACE A RUN STOPS FOR A FAILED STEP. A dead-lettered step, an awaited build that
    died, and a run whose last live branch finished while another had failed all end here, so
    they all leave the run in the same shape: `failed` or `deadlettered` (whichever the step
    is), `error` holding the cause, and `blocked_reason` holding the sentence the console shows
    on the run card — "Stopped at Research: <why>. Try it again, or skip it and carry on."

    The sentence is the fix for a run that stopped and said nothing: the cause was written to
    `error`, the console renders `blocked_reason`, and the card of a stopped run was blank.
    """
    run = _run_row(connection, run_id)
    template = _step_map(_run_spec(connection, run_id)).get(str(row["step_id"])) or {}
    why = str(row["last_error"] or "its work failed").strip().rstrip(".")
    # The sentence offers what the buttons offer (`workflow_run_actions.skip_refusal`): a
    # standalone gate is never skipped past; a refused action can be.
    gate = _is_gate_step(template)
    refused = gate or bool(
        (_json(dict(row).get("output"), {}) or {}).get("rejected_by_approver"))
    choices = ("Retry asks again; Stop ends the run." if gate
               else "Retry asks again; Skip carries on without it." if refused
               else "Try it again, or skip it and carry on.")
    named = _named_step(connection, run["project_id"], template, str(row["step_id"]))
    sentence = f"Stopped at {named}: {why}. {choices}"
    status = "deadlettered" if row["status"] == "deadlettered" else "failed"
    connection.execute(
        text(
            """
            UPDATE workflow_runs
            SET status=:status, error=:error, blocked_reason=:sentence, updated_at=now()
            WHERE id=:id AND status NOT IN ('completed','cancelled','compensating','compensated',
                                            'paused')
            """
        ),
        {"id": run_id, "status": status, "error": why[:500], "sentence": sentence[:1000]},
    )
    if run["status"] != status:
        emit_event(connection, run["project_id"], "workflow.run_stopped", run["actor"],
                   "workflow_run", run_id,
                   {"step_id": row["step_id"], "step_status": row["status"], "reason": why[:500]})
    _refresh_frontier(connection, run_id)


def _follow_edges_left_behind(connection: Connection, run_id: str) -> None:
    """Activate the next steps of every step that finished while the run was not moving.

    `_after_success` only follows a step's edges while its run is `running` or `waiting`. When
    one parallel branch fails, the run stops at once — and a sibling branch still in flight can
    finish AFTER that. It is recorded succeeded, and its next step is never started. Once the
    operator retries or skips the failed branch, nothing revisits it, so that next step would sit
    `pending` until the run "completed" around it. The same is true of a step that finished
    while the run was paused.

    So whenever a run is set moving again (`_set_moving_again`), every step that is settled for
    downstream — succeeded, or skipped by an operator — has its ordinary `next` edges followed.
    `_maybe_activate` is idempotent: a step already started is left alone, and a join still
    waits for everything it waits for. Control steps choose their own targets when they are
    processed (a condition picks one branch, a loop re-enters its body), so their edges are not
    re-followed here; neither are a loop body's, which return to their controller.
    """
    spec = _run_spec(connection, run_id)
    steps = _step_map(spec)
    loop_bodies = {str(s.get("body")) for s in spec.get("steps", [])
                   if s.get("kind") == "bounded_loop" and s.get("body")}
    rows = connection.execute(
        text(
            """
            SELECT step_id, iteration, status, output FROM workflow_step_runs
            WHERE workflow_run_id=:run_id AND (status IN ('succeeded', 'skipped')
                  OR (status='failed' AND output->>'error_path_taken' IS NOT NULL))
            ORDER BY created_at, iteration
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    for row in rows:
        taken = (_json(row["output"], {}) or {}).get(ERROR_PATH_TAKEN)
        if row["status"] == "failed" and taken:
            # A step that failed into its error path while the run was paused: the error step.
            if str(taken) in steps:
                _maybe_activate(connection, run_id, str(taken), int(row["iteration"]))
            continue
        if not _settled_for_downstream(row):
            continue
        template = steps.get(str(row["step_id"])) or {}
        if template.get("kind") in {"condition", "bounded_loop", "parallel", "for_each"}:
            continue
        if str(row["step_id"]) in loop_bodies or str(row["step_id"]) in for_each_bodies(spec):
            continue
        for target in template.get("next", []) or []:
            if str(target) in steps:
                _maybe_activate(connection, run_id, str(target), int(row["iteration"]))
    # A for_each whose items finished while the run was stopped or paused takes up where it was:
    # the next items start, or it finishes (`_advance_for_each` is a no-op unless it waits).
    for template in spec.get("steps", []):
        if template.get("kind") == "for_each":
            _advance_for_each(connection, run_id, template)


#: The run states `_set_moving_again` may set running: a run that is moving, or one stopped (or
#: blocked) waiting for somebody. Never a closed run, a paused one, or one compensating.
MOVABLE_RUN_STATES = frozenset({"running", "waiting", "failed", "deadlettered", "blocked"})


def _set_moving_again(
    connection: Connection, run_id: str, *, released: Optional[tuple[str, int]] = None,
) -> str:
    """After an operator's Try again or Skip: the run moves again, or stays where it must.

    ONE IMPLEMENTATION OF "THE OPERATOR DECIDED, NOW WHAT", shared by `retry_failed_step` and
    `skip_failed_step`, because the two used to differ in exactly the ways that hurt:

    * A PAUSED run stays paused. Skip set the run to `running` unconditionally, so skipping a
      step silently un-paused a run somebody had paused on purpose. The decision is recorded;
      the run proceeds when they press Resume (`resume_run` follows the edges left behind).
    * Otherwise the run is set moving, the edges of steps that finished while it was stopped are
      followed (`_follow_edges_left_behind`), and `_maybe_finish_run` decides what it is now —
      running, waiting, completed, or STOPPED AGAIN at another failed step the operator has not
      decided about yet. It is never completed past one.

    `released` is a step the operator just settled (a skip): it is released exactly as a success
    would be, through `_after_success`, so a skipped loop body returns to its loop controller
    rather than leaving the controller waiting for ever.

    THE ONLY DOOR OUT OF A STOP. Every path that sets a stopped run moving comes through here —
    the operator's Try again and Skip, and the runner reopening or re-pointing a step onto a
    retried build (`reopen_failed_step`, `repoint_waiting_step`). Those two used to write
    `status='waiting'` themselves and leave `blocked_reason` behind, so a run revived by a
    retried build kept "Stopped at Build: … Try it again, or skip it and carry on." on its card
    while it waited on the new build, and still after it COMPLETED — a red instruction with no
    buttons under a finished run (review of c8de36b). Here the sentence is cleared with the
    status, in one statement, and a test enumerates every other writer of a run's status.

    Never revives a run that is over: a cancelled, completed or compensated run — or one busy
    compensating — is left exactly as it is. The UPDATE carries that guard itself, so a Stop
    that commits between an operator's check and this write still wins.

    Returns the run's status afterwards.
    """
    run = _run_row(connection, run_id)
    if run["status"] == "paused":
        _refresh_frontier(connection, run_id)
        return "paused"
    if run["status"] not in MOVABLE_RUN_STATES:
        _refresh_frontier(connection, run_id)
        return str(run["status"])
    connection.execute(
        text("UPDATE workflow_runs SET status='running', blocked_reason=NULL, error=NULL, "
             "updated_at=now() WHERE id=:id AND status IN :movable")
        .bindparams(bindparam("movable", expanding=True)),
        {"id": run_id, "movable": tuple(sorted(MOVABLE_RUN_STATES))},
    )
    _follow_edges_left_behind(connection, run_id)
    if released is not None:
        _after_success(connection, run_id, released[0], released[1])
    _drain_control_steps(connection, run_id)
    _maybe_finish_run(connection, run_id)
    return str(_run_row(connection, run_id)["status"])


def _maybe_finish_run(connection: Connection, run_id: str) -> None:
    run = _run_row(connection, run_id)
    if run["status"] not in {"running", "waiting"}:
        _refresh_frontier(connection, run_id)
        return
    counts = connection.execute(
        text(
            """
            SELECT status, count(*) AS count FROM workflow_step_runs
            WHERE workflow_run_id=:run_id GROUP BY status
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    by_status = {row["status"]: int(row["count"]) for row in counts}
    if not any(by_status.get(status, 0) for status in ACTIVE_STEP_STATES):
        # NOTHING LEFT RUNNING IS NOT THE SAME AS NOTHING LEFT TO DO.
        #
        # This completed a run whenever no step was active, and recorded every pending step as
        # `path_not_reached`. With two failed parallel branches, an operator skipped one; the
        # join could not release (its other branch was still dead-lettered), nothing was active,
        # and the run went to COMPLETED with the join and everything after it "skipped" — a
        # failure turned into a finished run, the exact lie a failed step stopping the run was
        # meant to end (boss-1, 2026-10-02). A run that still has a step the operator has not
        # decided about stops AT that step instead, and its pending steps stay pending.
        stopped = _first_stopped_step(connection, run_id)
        if stopped is not None:
            _stop_run_at(connection, run_id, stopped)
            return
    if any(by_status.get(status, 0) for status in ACTIVE_STEP_STATES):
        connection.execute(
            text(
                """
                UPDATE workflow_runs
                SET status=CASE
                    WHEN EXISTS (
                      SELECT 1 FROM workflow_step_runs
                      WHERE workflow_run_id=:id AND status IN ('ready','leased','running')
                    ) THEN 'running' ELSE 'waiting' END,
                    updated_at=now()
                WHERE id=:id AND status IN ('running','waiting')
                """
            ),
            {"id": run_id},
        )
        _refresh_frontier(connection, run_id)
        return
    # Pending rows at this point are unreachable paths.  Record them as skipped
    # before completing so no step disappears silently.
    pending = connection.execute(
        text(
            "SELECT step_id FROM workflow_step_runs WHERE workflow_run_id=:id AND status='pending'"
        ),
        {"id": run_id},
    ).scalars().all()
    for step_id in pending:
        _mark_skipped(connection, run_id, step_id, "path_not_reached")
    connection.execute(
        text(
            """
            UPDATE workflow_runs
            SET status='completed', output=context, current_frontier='[]'::jsonb,
                completed_at=clock_timestamp(), updated_at=now()
            WHERE id=:id AND status IN ('running','waiting')
            """
        ),
        {"id": run_id},
    )
    emit_event(
        connection,
        run["project_id"],
        "workflow.run_completed",
        run["actor"],
        "workflow_run",
        run_id,
        {"status_counts": by_status},
    )
    # A run can finish past a question nobody answered (its step skipped, or failed and then
    # skipped). The question is moot now; see run_decisions.
    from .run_decisions import withdraw_with_run

    withdraw_with_run(connection, run["project_id"], run_id,
                      why=f"its run {run_id} finished without it", actor=run["actor"] or "workflow")


def _refuse_step(
    connection: Connection, run_id: str, step_id: str, iteration: int,
    *, decision_id: str, outcome: str,
) -> None:
    """Record that a gate was answered NO, on the step that asked.

    A rejection is an answer, and the step is where somebody looks for it. Leaving it `waiting`
    said the opposite of what had happened: the person had decided, and the console showed the
    question still open.

    `failed` rather than a status of its own — the run is blocked either way, and a step that
    did not pass is the thing every other reader of this table already understands.
    """
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='failed', completed_at=clock_timestamp(), updated_at=now(),
                last_error=:reason,
                output = COALESCE(output, '{}'::jsonb) || CAST(:out AS JSONB)
            WHERE workflow_run_id=:run_id AND step_id=:step_id AND iteration=:iteration
              AND status='waiting'
            """
        ),
        {"run_id": run_id, "step_id": step_id, "iteration": iteration,
         "reason": ("you rejected it" if outcome == "rejected"
                    else f"the approval {outcome} before anybody answered"),
         "out": json.dumps({"decision_id": decision_id, "outcome": outcome,
                            "rejected_by_approver": outcome == "rejected"})},
    )


def _step_display(connection: Connection, run: Any, step_id: str) -> str:
    """A step's name as the run view shows it, for a short sentence about it."""
    try:
        template = _step_map(_run_spec(connection, run["id"])).get(str(step_id)) or {}
        named = str(_named_step(connection, run["project_id"], template, str(step_id)))
        if named == str(step_id):
            from .command_center import step_label

            named = step_label(template, str(step_id))
        return named
    except Exception:  # noqa: BLE001 - a name for a toast never fails the action
        return str(step_id)


def reject_gate(
    connection: Connection, run_id: str, step_id: str, iteration: int,
    *, decision_id: str, outcome: str,
) -> None:
    """A gate somebody answered NO: its step FAILS and the run stops there (agents-home D3).

    THE ONE PATH for a refused approval — a decision step and a capability step held at its
    manual approval both end here. It used to block the run with the step left `waiting` (or
    failed and blocked), so the run sat "waiting on a person" who had already answered, with no
    way forward. Now the run is stopped at a failed step like any other, and offers Try again
    (asks the gate afresh — a new decision), recorded with who chose it. Skip is NOT offered for
    a refused approval: skipping would turn the person's no into a yes
    (workflow_run_actions.require, skip_failed_step).
    """
    _refuse_step(connection, run_id, step_id, iteration, decision_id=decision_id,
                 outcome=outcome)
    row = connection.execute(
        text("SELECT * FROM workflow_step_runs WHERE workflow_run_id=:r AND step_id=:s "
             "AND iteration=:i"),
        {"r": run_id, "s": step_id, "i": iteration},
    ).mappings().first()
    if row is None or row["status"] != "failed":
        return
    _stop_run_at(connection, run_id, row)


def refuse_gates_of(connection: Connection, decision_id: str, outcome: str = "rejected") -> int:
    """Fail every step waiting on this decision NOW, as the person answers — not on the next
    tick, which left the run saying "waiting on a person" for up to 20 seconds after a No."""
    rows = connection.execute(
        text("SELECT workflow_run_id, step_id, iteration FROM workflow_step_runs "
             "WHERE decision_id = :d AND status = 'waiting'"),
        {"d": decision_id},
    ).mappings().all()
    for row in rows:
        run = _run_row(connection, str(row["workflow_run_id"]), lock=True)
        if run is None or run["status"] in TERMINAL_RUN_STATES:
            continue
        reject_gate(connection, str(row["workflow_run_id"]), str(row["step_id"]),
                    int(row["iteration"]), decision_id=decision_id, outcome=outcome)
    return len(rows)


def _block_run(connection: Connection, run_id: str, reason: str) -> None:
    run = _run_row(connection, run_id)
    connection.execute(
        text(
            """
            UPDATE workflow_runs SET status='blocked', blocked_reason=:reason, updated_at=now()
            WHERE id=:id AND status NOT IN ('completed','cancelled','compensated')
            """
        ),
        {"id": run_id, "reason": reason},
    )
    emit_event(
        connection,
        run["project_id"],
        "workflow.run_blocked",
        run["actor"],
        "workflow_run",
        run_id,
        {"reason": reason},
    )


def claim_step(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    worker_id: str,
    lease_seconds: int = 60,
) -> Optional[dict[str, Any]]:
    if lease_seconds < 5 or lease_seconds > 3600:
        raise WorkflowValidationError("lease_seconds must be between 5 and 3600.")
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] not in {"running", "waiting"}:
        return None
    # Attempts past their step's timeout end first, so one is failed (and maybe retried, or
    # sent down its error path) rather than reclaimed below as a mere lapsed lease.
    if _time_out_overdue_steps(connection, run_id):
        if _run_row(connection, run_id)["status"] not in {"running", "waiting"}:
            return None
    _drain_control_steps(connection, run_id)
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE workflow_run_id=:run_id
              AND step_kind NOT IN (
                'condition','parallel','join','bounded_loop','for_each','decision','timer',
                'subworkflow'
              )
              AND run_after <= now()
              AND (
                status='ready'
                OR (status IN ('leased','running') AND lease_expires_at < now())
              )
            ORDER BY run_after, created_at, iteration, step_id
            FOR UPDATE SKIP LOCKED LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    if not row:
        return None
    # A TEST RUN NEVER LEASES WORK IT MAY NOT DO. Activation already refuses such a step; this
    # is the second lock on the same door, for a step put back by Try again or a re-check.
    from . import workflow_test_runs

    if workflow_test_runs.is_test(run):
        template = _step_map(_run_spec(connection, run_id)).get(str(row["step_id"])) or {}
        why = workflow_test_runs.refusal_for(run, template)
        if why:
            workflow_test_runs.refuse(connection, run, row, why)
            return None
    reclaimed = row["status"] in {"leased", "running"}
    if reclaimed:
        _emit_step_event(
            connection,
            run,
            "workflow.step_lease_reclaimed",
            row,
            {"previous_owner": row.get("lease_owner")},
        )
    expires_at = now_utc() + timedelta(seconds=lease_seconds)
    # THE ATTEMPT'S CLOCK STARTS NOW, when it is leased: `timeout_seconds` bounds this attempt,
    # not the step's whole life, and not the time it sat waiting for a backoff.
    timeout = (_step_map(_run_spec(connection, run_id)).get(str(row["step_id"])) or {}).get(
        "timeout_seconds")
    updated = connection.execute(
        text(
            """
            -- clock_timestamp(), NOT now(). `now()` is the TRANSACTION's start time and is
            -- constant for its whole duration, so a step claimed and completed inside one tick
            -- got the same value for `started_at` and `completed_at`. Every step in the console
            -- therefore reported a duration of zero, and the run view showed nothing at all —
            -- which is what the operator noticed. The lease columns keep now(): a lease is
            -- about the transaction's view of time, a duration is about the wall clock.
            UPDATE workflow_step_runs
            SET status='leased', attempt=attempt+1, lease_owner=:worker_id,
                lease_expires_at=:expires_at, heartbeat_at=now(),
                attempt_timeout_at=CASE WHEN CAST(:timeout AS INTEGER) IS NULL THEN NULL
                    ELSE now() + make_interval(secs => CAST(:timeout AS INTEGER)) END,
                started_at=COALESCE(started_at, clock_timestamp()), updated_at=now()
            WHERE id=:id
            RETURNING *
            """
        ),
        {"id": row["id"], "worker_id": worker_id, "expires_at": expires_at,
         "timeout": int(timeout) if timeout else None},
    ).mappings().one()
    connection.execute(
        text("UPDATE workflow_runs SET status='running', updated_at=now() "
             "WHERE id=:id AND status IN ('running','waiting')"),
        {"id": run_id},
    )
    waited = (_json(row["output"], {}) or {}).get("waiting")
    if isinstance(waited, dict) and waited.get("sentence"):
        # THE WAIT IS OVER, SO ITS SENTENCE GOES. `wait_step` put "… Waiting for the
        # per-minute budget" on the run's card; the step being claimed again is the end of
        # that wait. Only that exact sentence is cleared — never a stop's, never another's.
        connection.execute(
            text("UPDATE workflow_runs SET blocked_reason=NULL, updated_at=now() "
                 "WHERE id=:id AND blocked_reason=:sentence"),
            {"id": run_id, "sentence": str(waited["sentence"])[:1000]},
        )
    _emit_step_event(
        connection,
        run,
        "workflow.step_leased",
        updated,
        {"worker_id": worker_id, "lease_expires_at": expires_at.isoformat(),
         **({"timeout_seconds": int(timeout),
             "attempt_timeout_at": _iso(updated["attempt_timeout_at"])} if timeout else {})},
    )
    _refresh_frontier(connection, run_id)
    return _serialize_step(updated)


def heartbeat_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    worker_id: str,
    lease_seconds: int = 60,
) -> dict[str, Any]:
    expires_at = now_utc() + timedelta(seconds=lease_seconds)
    row = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='running', heartbeat_at=now(), lease_expires_at=:expires_at,
                updated_at=now()
            WHERE id=:id AND project_id=:project_id AND lease_owner=:worker_id
              AND status IN ('leased','running') AND lease_expires_at >= now()
              -- An attempt past its timeout is not kept alive: the reaper ends it.
              AND (attempt_timeout_at IS NULL OR attempt_timeout_at >= now())
            RETURNING *
            """
        ),
        {
            "id": step_run_id,
            "project_id": project_id,
            "worker_id": worker_id,
            "expires_at": expires_at,
        },
    ).mappings().first()
    if not row:
        raise WorkflowLeaseError("Workflow step lease is missing, expired, or owned by another worker.")
    _refresh_frontier(connection, row["workflow_run_id"])
    return _serialize_step(row)


def force_complete_waiting_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    output: dict[str, Any],
) -> dict[str, Any]:
    """Complete a step that was PARKED, not leased.

    `complete_step` requires the caller to hold the lease, which is right for work a worker is
    executing. A parked step deliberately holds none — nothing is running, it is waiting for a
    build that may take twenty minutes, and a lease held that long would be reclaimed as a
    crashed worker. So its completion is a separate door, and it only opens for a step actually
    in `waiting`.
    """
    row = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='succeeded', output=CAST(:output AS JSONB),
                completed_at=clock_timestamp(), updated_at=now()
            WHERE id=:id AND project_id=:project_id AND status='waiting'
            RETURNING *
            """
        ),
        {"id": step_run_id, "project_id": project_id, "output": json.dumps(output)},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Waiting workflow step not found.")
    connection.execute(
        text("UPDATE workflow_runs SET status='running', updated_at=now() "
             "WHERE id=:id AND status='waiting'"),
        {"id": row["workflow_run_id"]},
    )
    # EVERYTHING `complete_step` DOES AFTER THE UPDATE, because a step finishing is more than a
    # status. The first version wrote the row and stopped, so the next step was never scheduled:
    # the run went straight to `completed` with the following step marked `skipped`. A build
    # that finished correctly still lost the deploy that was supposed to follow it.
    if output:
        # Into the run context, which is where `{"from_step": ..., "field": ...}` reads from.
        # Without this "deploy the thing you just built" resolves to nothing.
        connection.execute(
            text(
                """
                UPDATE workflow_runs
                SET context=context || CAST(:output AS JSONB), updated_at=now()
                WHERE id=:run_id
                """
            ),
            {"run_id": row["workflow_run_id"], "output": json.dumps(output)},
        )
    _emit_step_event(
        connection, _run_row(connection, row["workflow_run_id"]),
        "workflow.step_succeeded", row, {"awaited": True},
    )
    _after_success(
        connection, row["workflow_run_id"], row["step_id"], int(row["iteration"]),
    )
    return _serialize_step(row)


def force_fail_waiting_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    error: str,
    output: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Fail a parked step because the work it was waiting for failed.

    Not retried: the retry belongs to the thing that failed, not to the act of waiting for it.
    Re-running this step would start a SECOND build rather than reconsider the first.

    THE RUN STOPS HERE AND WAITS FOR A PERSON. What the work reached before it died is MERGED
    onto the failed step's own output, so the operator deciding between "Try again" and "Skip
    and proceed" can see it — but it never enters the run context, which reads only steps that
    succeeded. That distinction is the whole of the boss-1 incident (2026-10-02): research died,
    the runner recorded it succeeded as `failed_continued`, and its half-written dossier walked
    on into a panel, an auto-approved spec and a build that nobody had decided should happen.
    """
    row = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='failed', last_error=:error, completed_at=clock_timestamp(),
                output = COALESCE(output, '{}'::jsonb) || CAST(:output AS JSONB),
                updated_at=now()
            WHERE id=:id AND project_id=:project_id AND status='waiting'
            RETURNING *
            """
        ),
        {"id": step_run_id, "project_id": project_id, "error": error[:500],
         "output": json.dumps(output or {})},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Waiting workflow step not found.")
    _emit_step_event(
        connection, _run_row(connection, row["workflow_run_id"]),
        "workflow.step_failed", row, {"error": error[:500], "awaited": True},
    )
    # Stopped here, with the sentence the run card shows — see `_stop_run_at`.
    if not _an_any_join_may_go_on_without(connection, row["workflow_run_id"], row):
        _stop_run_at(connection, row["workflow_run_id"], row)
    else:
        # Gone on without: the run may now have nothing left to do but finish.
        _maybe_finish_run(connection, row["workflow_run_id"])
    return _serialize_step(row)


#: The step states an operator can act on with "Try again" or "Skip and proceed". A step in any
#: of these has had its verdict; one in any other state is still live, and acting on it would
#: run the work twice or abandon it mid-flight.
OPERATOR_RECOVERABLE_STEP_STATES = ("failed", "deadlettered", "cancelled")

#: Skipping is narrower than retrying. A CANCELLED step belongs to a run somebody stopped; skipping
#: it would quietly restart work the operator asked to end. Retry reopens it deliberately.
SKIPPABLE_STEP_STATES = ("failed", "deadlettered")


def skipped_by_operator(row: Any) -> bool:
    """Was this step skipped BY AN OPERATOR — "Skip and proceed" — rather than by the run itself
    (a branch never taken: `condition_not_selected`, `path_not_reached`)?

    THE ONE PREDICATE. The runner's input resolution and the join release both ask it; the
    runner used to treat every skipped row as an operator's skip, so a step reading from a
    branch the run never took was failed with "step x was skipped… Retry x" — and x had never
    failed, so Retry was refused (review of c8de36b)."""
    if row["status"] != "skipped":
        return False
    output = _json(row["output"], {})
    return bool(isinstance(output, dict) and output.get("skipped_by_operator"))


def _settled_for_downstream(row: Any) -> bool:
    """May the steps after this one run?

    Succeeded, or skipped BY AN OPERATOR who decided the run should go on without it. A step
    skipped because its branch was never taken (`path_not_reached`) is not a decision anybody
    made about this step, so it does not release a join.
    """
    return row["status"] == "succeeded" or skipped_by_operator(row)


def _latest_step_row(
    connection: Connection, project_id: str, run_id: str, step_id: str
) -> Any:
    return connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE workflow_run_id=:run_id AND project_id=:project_id AND step_id=:step_id
            ORDER BY created_at DESC, iteration DESC LIMIT 1
            FOR UPDATE
            """
        ),
        {"run_id": run_id, "project_id": project_id, "step_id": step_id},
    ).mappings().first()


def retry_failed_step(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    step_id: str,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    """Put ONE failed, dead-lettered or cancelled step back in the queue.

    `note` is "Retry with a note": what the operator wants changed this time (at most
    RETRY_NOTE_LIMIT characters). It is stored on the step run as `output.retry_note` and in the
    `workflow.step_retried` event, and a build step hands it, verbatim, to the builder it starts
    (workflow_capability_runner._retried_build).

    ONE IMPLEMENTATION. The console's "Try … again" (POST .../retry-step) and the agent's
    `retry_workflow_step` tool each carried their own copy of this UPDATE, and only the tool's
    recorded an event — so a retry pressed in the console left no trace in the run's history.
    Both now call this.

    Everything the run already produced is kept; only this step runs again. The run comes back
    to `running` too, or a re-queued step would sit ready under a dead run.
    """
    # THE RUN FIRST, LOCKED — as Skip, Stop and Remove take it. This read the run unlocked, so a
    # Stop committing between the check below and the write in `_set_moving_again` was undone:
    # a cancelled run set running again, the incident this rule exists to end. Run before step,
    # the order every other operation locks in, so Try again and Skip cannot deadlock.
    note = str(note or "").strip()
    if len(note) > RETRY_NOTE_LIMIT:
        raise WorkflowValidationError(
            f"The note is {len(note)} characters; keep it under {RETRY_NOTE_LIMIT}.")
    run = _run_row(connection, run_id, lock=True)
    if run is None or run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    row = _latest_step_row(connection, project_id, run_id, step_id)
    if not row:
        raise WorkflowNotFoundError(f"no step '{step_id}' on run {run_id}")
    # THE BUTTON'S RULE, NOT A COPY OF IT. This kept its own precondition (the step's state) and
    # nothing about the run, so a step of a CANCELLED run — whose Try again the console no longer
    # offered, because Stop promises the run "cannot be resumed" — was still revived by the
    # endpoint, the agent's tool and a re-check. `require` refuses whatever the run's actions do
    # not offer enabled, in the same words the console shows.
    from .workflow_run_actions import require

    require(connection, project_id, run_id, "retry_step", step_id=step_id)
    from . import workflow_test_runs

    why = (workflow_test_runs.refusal_for(
        run, _step_map(_run_spec(connection, run_id)).get(str(step_id)) or {})
        if workflow_test_runs.is_test(run) else None)
    if why:
        raise WorkflowConflictError(
            f"this is a test run, and step '{step_id}' {why}. Start a new test run that pins "
            "its output or allows it to run live.")
    # A RETRY RUNS UNDER AUTHORITY THAT COVERS IT. When the grant recorded on the run no longer
    # covers what the run needs (an agent it never named, a narrowed grant), the person is asked
    # again (409 consent_required, the start's own sheet) and the covering grant is recorded on
    # the run — never a retry that is refused again for the same reason.
    from boss_engine.identity import consent as _consent

    version = _version_row(connection, project_id, run["workflow_version_id"])
    if version is not None:
        _consent.require_for_retry(
            connection, project_id=project_id, run_id=run_id,
            spec=_json(version["spec"], {}), input_data=_json(run["input"], {}),
            title=consent_task_title(connection, project_id, run["workflow_definition_id"],
                                     _json(run["input"], {})))
    # A PERSON'S TRY AGAIN STARTS THE WAITS AFRESH, as it does the attempts. `waiting.count`
    # bounds how often one step waits for the budget (wait_step); kept across a Retry, a step
    # stopped after MAX_STEP_WAITS failed again at its very next wait — the person raised the
    # cap as told, pressed Try again, and got "still not started after 30 tries" at once.
    # A REFUSED GATE ASKS AGAIN (agents-home D3). Its row kept the rejected decision's id, and
    # a ready step with a decision on it reads as released — so Retry ran the work the person
    # had said no to. The answer is cleared (it stays in the step's history as
    # `refused_decision_id`), and the step opens a fresh decision when it is reached.
    refused = bool((_json(row["output"], {}) or {}).get("rejected_by_approver"))
    # WHAT THE NEXT ATTEMPT IS TOLD. The operator's note, and which build the failed attempt
    # was — so a retried build resumes that lineage instead of starting blind from main.
    previous = _json(row["output"], {}) or {}
    retry_marker = {"retry_note": note, "retried_by": actor,
                    "retried_from_code_task_run_id": _code_task_of(previous)}
    connection.execute(
        # The retry is a new attempt and is timed as one: its clock starts when it is leased.
        # Keeping the first attempt's start made a deploy retried the next morning read
        # "14h 16m" for a step that ran in twenty seconds.
        text("UPDATE workflow_step_runs SET status='ready', attempt=0, last_error=NULL, "
             "started_at=NULL, completed_at=NULL, "
             "lease_owner=NULL, lease_expires_at=NULL, run_after=now(), updated_at=now(), "
             "output = ((COALESCE(output, '{}'::jsonb) - 'waiting'"
             + (" - 'rejected_by_approver' - 'outcome') || jsonb_build_object("
                "'refused_decision_id', CAST(decision_id AS text))) "
                "|| CAST(:retry AS JSONB), decision_id = NULL "
                if refused else ") || CAST(:retry AS JSONB)) ")
             + "WHERE id=:id"),
        {"id": row["id"], "retry": json.dumps(retry_marker)},
    )
    if row["status"] == "skipped":
        # UN-SKIPPED: the operator's skip is kept as history (`unskipped`), never as the state of
        # a step that is about to run — a later success must not still read "skipped by …".
        connection.execute(
            text("UPDATE workflow_step_runs SET completed_at=NULL, output = (output "
                 "- 'skipped_by_operator' - 'skip_reason' - 'skipped_by' - 'skipped_from' "
                 "- 'status') || jsonb_build_object('unskipped', jsonb_build_object("
                 "'reason', output->'skip_reason', 'by', output->'skipped_by')) WHERE id=:id"),
            {"id": row["id"]})
    emit_event(connection, project_id, "workflow.step_retried", actor, "workflow_run", run_id,
               {"step_id": step_id, "was": row["status"],
                "workflow_step_run_id": row["id"],
                **({"note": note} if note else {})})
    # The run moves again — unless it is paused, or until it reaches another failed step the
    # operator has not decided about. See `_set_moving_again`.
    run_status = _set_moving_again(connection, run_id)
    named = _step_display(connection, run, step_id)
    asks_again = bool((_json(row["output"], {}) or {}).get("rejected_by_approver"))
    return {"run_id": run_id, "step_id": step_id, "status": "ready", "was": row["status"],
            "run_status": run_status,
            # The words the console shows for it: short, never a server fragment.
            "message": f"Asking again: {named}" if asks_again else f"Retrying {named}"}


#: The longest "Retry with a note" note a step accepts.
RETRY_NOTE_LIMIT = 4000


def _code_task_of(output: dict[str, Any]) -> Optional[str]:
    """The code task a step's output says it started or waited on, if any."""
    if not isinstance(output, dict):
        return None
    found = output.get("code_task_run_id")
    awaited = output.get("await")
    if not found and isinstance(awaited, dict) and awaited.get("kind") == "code_task":
        found = awaited.get("id")
    found = str(found or "").strip()
    return found or None


def _is_gate_step(template: dict[str, Any]) -> bool:
    """An approval step, by its template. ONE test, shared with the run's action list — see
    `workflow_run_actions.is_gate`."""
    from .workflow_run_actions import is_gate

    binding = template.get("binding") or {}
    return is_gate(template.get("kind"), binding.get("capability") if isinstance(binding, dict) else None)


def skip_failed_step(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    step_id: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """"Skip and proceed": the operator decides the run goes on without this step.

    THE OTHER HALF OF "A FAILED STEP STOPS THE RUN". Once a failure is terminal for its step —
    which it now is for every capability, after research that died was recorded as succeeded
    and the workflow walked on into a panel, an auto-approved spec and a build (boss-1,
    2026-10-02) — the operator needs exactly two ways forward: try the step again, or go on
    without it. The product owner's words: "if a step fails it should stop, that's the
    expectation; there should be a button to skip and proceed."

    What a skip is, precisely:

    * The step becomes `skipped`, never `succeeded`. Its output keeps whatever the failed
      attempt recorded and gains `skipped_by_operator`, the reason, who skipped it and what it
      was before — so the history still says it failed, and says who chose to go on.
    * Nothing it produced reaches the run context. Downstream steps that bind to it receive an
      explicit "skipped" marker (see the runner's `_resolve_inputs`): an optional input is left
      out and named as skipped on the consuming step; a required one stops THAT step with a
      sentence naming the skip, which is again the operator's call.
    * The next steps are activated exactly as a success would activate them — unless another
      step of the run is still failed (the run stops again THERE; it never completes past a
      failure nobody decided about), or the run is paused (they start when it is resumed).

    Refused for an approval gate. A gate that "failed" is a person who said no, and skipping it
    would turn their no into a yes. Refused without a reason, because "why did this run go on
    without research?" has to be answerable from the record, not from memory.
    """
    reason = str(reason or "").strip()
    if not reason:
        raise WorkflowValidationError(
            "Say why you are skipping this step. The run keeps the reason with your name.")
    run = _run_row(connection, run_id, lock=True)
    if run is None or run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    row = _latest_step_row(connection, project_id, run_id, step_id)
    if not row:
        raise WorkflowNotFoundError(f"no step '{step_id}' on run {run_id}")
    existing = _json(row["output"], {})
    if row["status"] == "skipped" and isinstance(existing, dict) \
            and existing.get("skipped_by_operator"):
        # Pressed twice. The first press already decided; report it rather than refuse it.
        return {"run_id": run_id, "step_id": step_id, "status": "skipped",
                "already": True, "reason": existing.get("skip_reason")}
    # THE BUTTON'S RULE, NOT A COPY OF IT. The step's state, the run's state and "is this an
    # approval" were three hand-written checks here and three more in the action list, the gate
    # test a copy of a copy. `require` is the one decision: a step that did not fail, a run
    # that is stopped or finished, an approval (a person's no — a skip would make it a yes) and a
    # run busy undoing itself are each refused in the words the console shows beside the button.
    from .workflow_run_actions import require

    require(connection, project_id, run_id, "skip_step", step_id=step_id)

    # WHO, BY NAME. The rail read "skipped by usr_kdzhawzy7mxmp5hpb3bqlgqojs": the record keeps
    # the id (the audit's key) and the screen gets the person's name beside it.
    who = connection.execute(text(
        "SELECT display_name FROM project_members WHERE member_id = :m AND display_name <> '' "
        "UNION ALL SELECT display_name FROM org_members WHERE member_id = :m "
        "AND display_name <> '' LIMIT 1"), {"m": actor}).scalar()
    marker = {
        "status": "skipped",
        "skipped_by_operator": True,
        "skip_reason": reason[:500],
        "skipped_by": actor,
        "skipped_by_name": str(who) if who else None,
        "skipped_from": row["status"],
        "failure": row["last_error"],
        "note": f"skipped by {who or 'an operator'}: {reason[:300]}",
    }
    updated = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='skipped', lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                completed_at=clock_timestamp(),
                output = COALESCE(output, '{}'::jsonb) || CAST(:output AS JSONB),
                updated_at=now()
            WHERE id=:id
            RETURNING *
            """
        ),
        {"id": row["id"], "output": json.dumps(marker)},
    ).mappings().one()
    # The operator's act, attributed to the operator — not to whoever started the run.
    emit_event(
        connection, project_id, "workflow.step_skipped", actor, "workflow_run", run_id,
        {"workflow_step_run_id": updated["id"], "step_id": step_id,
         "step_kind": updated["step_kind"], "iteration": int(updated["iteration"]),
         "reason": reason[:500], "by_operator": True, "was": row["status"],
         "failure": row["last_error"]},
    )
    # NOT "set it running and follow this step's edges" — that was the first version, and with
    # two failed parallel branches it COMPLETED the run past the one nobody had skipped. The
    # shared path releases the step like a success, and the run then stops again at any other
    # failed step (`_maybe_finish_run`), or stays paused if somebody paused it.
    run_status = _set_moving_again(
        connection, run_id, released=(step_id, int(updated["iteration"])))
    return {"run_id": run_id, "step_id": step_id, "status": "skipped",
            "was": row["status"], "reason": reason[:500], "run_status": run_status,
            "message": f"Skipped {_step_display(connection, run, step_id)}"}


def repoint_waiting_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    handle: dict[str, Any],
    note: str,
) -> dict[str, Any]:
    """Move a parked step onto a LATER attempt at the same work.

    A step parks against one id. Retrying a build starts a fresh attempt with a new id, so the
    step went on waiting for the corpse — the operator watched a new build run to completion
    while the workflow above it read "failed — the code_task it started finished as
    'cancelled'", stuck at 3 of 8 permanently.

    The step stays WAITING. Nothing about it has succeeded or failed; it is waiting for
    different work, and the note records the substitution so the operator can see why the id
    under the step changed rather than finding a stranger there.
    """
    row = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET output = COALESCE(output, '{}'::jsonb)
                         || jsonb_build_object('await', CAST(:handle AS JSONB),
                                               'awaited_note', CAST(:note AS TEXT),
                                               'code_task_run_id', CAST(:new_id AS TEXT)),
                updated_at = now()
            WHERE id=:id AND project_id=:project_id AND status='waiting'
            RETURNING *
            """
        ),
        {"id": step_run_id, "project_id": project_id,
         "handle": json.dumps(handle), "note": note[:500],
         "new_id": str(handle.get("id") or "")},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Waiting workflow step not found.")
    # The RUN moves again too. A previous attempt may have marked it failed before the retry
    # existed, and leaving it failed above a step that is actively waiting is the same
    # contradiction in the other direction. Through the one door out of a stop, so the "Stopped
    # at …" sentence goes with the status and a paused or closed run is left as it is.
    _set_moving_again(connection, row["workflow_run_id"])
    return _serialize_step(row)


def reopen_failed_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    handle: dict[str, Any],
    note: str,
) -> dict[str, Any]:
    """Put a step that failed with its build back to waiting, on the retry.

    The sequence that actually happens: the build dies, the step fails, and THEN the operator
    presses retry. So the recovery has to reach a step that has already been given its verdict —
    otherwise the operator watches a new build run to completion while the workflow above it
    reads "failed", permanently, at 3 of 8.

    Narrow on purpose. Only a step whose awaited work has a LIVE later attempt is reopened, only
    the await handle and the status change, and the note records the substitution — a step that
    silently changed which run it was watching would be worse than the stuck one.
    """
    row = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='waiting', last_error=NULL, completed_at=NULL,
                lease_owner=NULL, lease_expires_at=NULL,
                -- A reopened step starts its budget waits afresh, as a Try again does.
                output = (COALESCE(output, '{}'::jsonb) - 'waiting')
                         || jsonb_build_object('await', CAST(:handle AS JSONB),
                                               'awaited_note', CAST(:note AS TEXT),
                                               'code_task_run_id', CAST(:new_id AS TEXT)),
                updated_at = now()
            WHERE id=:id AND project_id=:project_id AND status='failed'
            RETURNING *
            """
        ),
        {"id": step_run_id, "project_id": project_id,
         "handle": json.dumps(handle), "note": note[:500],
         "new_id": str(handle.get("id") or "")},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Failed workflow step not found.")
    # The run was marked failed when the step was. It is waiting again now, and leaving it
    # failed above an actively-waiting step is the same contradiction the other way round —
    # as is leaving its "Stopped at …" sentence on the card. `_set_moving_again` clears both,
    # and stops the run again at any OTHER failed step nobody has decided about.
    _set_moving_again(connection, row["workflow_run_id"])
    return _serialize_step(row)


def park_step_awaiting(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    worker_id: str,
    output: dict[str, Any],
) -> dict[str, Any]:
    """Park a step that STARTED long-running work and must not be called done.

    An async capability's tool returns as soon as the work is accepted: create_code_task hands
    back a run id in milliseconds for a build that takes twenty minutes. Completing the step on
    that return made the workflow race past `build` into `review` and `test` while the build was
    still cloning — every run finishing in seconds, with a shape that looked like success.

    So the step goes to `waiting`, holding the id it is waiting on, and the runner completes it
    when the underlying work reaches a terminal state. The lease is released because nothing is
    executing: a lease held for twenty minutes would be reclaimed as a crashed worker.
    """
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE id=:id AND project_id=:project_id FOR UPDATE
            """
        ),
        {"id": step_run_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow step run not found.")
    if row["status"] not in {"leased", "running"} or row["lease_owner"] != worker_id:
        raise WorkflowLeaseError("Workflow step is not leased by this worker.")
    updated = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='waiting', output=CAST(:output AS JSONB),
                lease_owner=NULL, lease_expires_at=NULL,
                started_at=COALESCE(started_at, clock_timestamp()), updated_at=now()
            WHERE id=:id
            RETURNING *
            """
        ),
        {"id": step_run_id, "output": json.dumps(output)},
    ).mappings().first()
    # ONLY A MOVING RUN GOES TO `waiting`. This was unconditional, so a branch that parked after
    # its run had been stopped at a failed sibling — or cancelled while the branch was still
    # leased — turned the run back to `waiting` under the operator: a stopped run that no longer
    # said so, or a cancelled one quietly revived.
    connection.execute(
        text("UPDATE workflow_runs SET status='waiting', updated_at=now() "
             "WHERE id=:id AND status IN ('running','waiting')"),
        {"id": row["workflow_run_id"]},
    )
    return _serialize_step(updated)


def complete_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    worker_id: str,
    output: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE id=:id AND project_id=:project_id FOR UPDATE
            """
        ),
        {"id": step_run_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow step run not found.")
    if row["status"] == "succeeded":
        return _serialize_step(row)
    if row["status"] not in {"leased", "running"} or row["lease_owner"] != worker_id:
        raise WorkflowLeaseError("Workflow step is not leased by this worker.")
    if row["lease_expires_at"] and row["lease_expires_at"] < now_utc():
        raise WorkflowLeaseError("Workflow step lease has expired.")
    if row.get("attempt_timeout_at") and row["attempt_timeout_at"] < now_utc():
        # FINISHED TOO LATE. The attempt ran past its step's `timeout_seconds`, so its result
        # does not count: it is ended as timed out, as the reaper would have, and retries or
        # takes its error path from there. Said, not raised, so the caller's transaction keeps
        # the record.
        return _time_out_attempt(connection, row)
    updated = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='succeeded', output=CAST(:output AS JSONB),
                evidence=CAST(:evidence AS JSONB), lease_owner=NULL,
                lease_expires_at=NULL, heartbeat_at=NULL,
                completed_at=clock_timestamp(), updated_at=now()
            WHERE id=:id RETURNING *
            """
        ),
        {
            "id": step_run_id,
            "output": json.dumps(output or {}),
            "evidence": json.dumps(evidence or []),
        },
    ).mappings().one()
    if output:
        connection.execute(
            text(
                """
                UPDATE workflow_runs
                SET context=context || CAST(:output AS JSONB), updated_at=now()
                WHERE id=:run_id
                """
            ),
            {"run_id": row["workflow_run_id"], "output": json.dumps(output)},
        )
    run = _run_row(connection, row["workflow_run_id"])
    _emit_step_event(
        connection,
        run,
        "workflow.step_succeeded",
        updated,
        {"evidence_count": len(evidence or [])},
    )
    spec = _run_spec(connection, row["workflow_run_id"])
    template = _step_map(spec)[row["step_id"]]
    compensation = template.get("compensation")
    if compensation:
        sequence = int(
            connection.execute(
                text(
                    """
                    SELECT COALESCE(max(sequence), -1) + 1
                    FROM workflow_compensation_runs WHERE workflow_run_id=:run_id
                    """
                ),
                {"run_id": row["workflow_run_id"]},
            ).scalar_one()
        )
        connection.execute(
            text(
                """
                INSERT INTO workflow_compensation_runs (
                    id, project_id, workflow_run_id, workflow_step_run_id,
                    sequence, status, action, dedupe_key, max_attempts
                ) VALUES (
                    :id, :project_id, :run_id, :step_run_id, :sequence,
                    'pending', CAST(:action AS JSONB), :dedupe_key, :max_attempts
                ) ON CONFLICT (project_id, dedupe_key) DO NOTHING
                """
            ),
            {
                "id": make_id("wfc"),
                "project_id": project_id,
                "run_id": row["workflow_run_id"],
                "step_run_id": row["id"],
                "sequence": sequence,
                "action": json.dumps(compensation),
                "dedupe_key": f"workflow-compensate:{row['id']}",
                "max_attempts": int(compensation.get("max_attempts", 3)),
            },
        )
    _after_success(
        connection,
        row["workflow_run_id"],
        row["step_id"],
        int(row["iteration"]),
    )
    return _serialize_step(updated)


#: How many times one step waits for something that clears by itself before it stops and asks
#: a person. At a wait of up to a minute each, half an hour of a project's per-minute budget
#: never having room is not "busy" any more: something else is spending it continuously.
MAX_STEP_WAITS = 30


def wait_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    worker_id: str,
    reason: str,
    wait_seconds: int,
    kind: str = "budget_wait",
) -> dict[str, Any]:
    """Put a leased step back to try again after `wait_seconds`: NOT a failure, NOT an attempt.

    For a step that could not start yet for a reason that clears by itself — the per-minute
    budget, spent by the runs started a few seconds earlier. Those used to fail the step, and a
    one-attempt step dead-lettered its run at step one ("'Read the meeting' was denied: budget
    denied: denied") while the budget had dollars to spare.

    The step returns to `ready` with `run_after` in the future, so the ordinary claim takes it
    again when the wait is over; the attempt it was claimed with is handed back, so waiting
    never uses up the retries a real failure is owed. The run is `waiting`, and its card says
    what for (`blocked_reason`), until a claim moves it again (`claim_step` clears it).

    Bounded: after MAX_STEP_WAITS waits the step fails, settled, with the reason — something is
    spending the budget continuously, and that is for a person to look at.
    """
    row = connection.execute(
        text("SELECT * FROM workflow_step_runs WHERE id=:id AND project_id=:project_id "
             "FOR UPDATE"),
        {"id": step_run_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow step run not found.")
    if row["status"] not in {"leased", "running"} or row["lease_owner"] != worker_id:
        raise WorkflowLeaseError("Workflow step is not leased by this worker.")
    earlier = (_json(row["output"], {}) or {}).get("waiting") or {}
    waits = int(earlier.get("count") or 0) + 1 if isinstance(earlier, dict) else 1
    if waits > MAX_STEP_WAITS:
        # What keeps it full decides what to do: a per-minute cap spent continuously, or other
        # work holding the budget's headroom for a long time (model_failures.explain).
        held = "other work" in reason.lower()
        if kind == "budget_wait":
            then = ("See what is holding the budget in Budget, or raise the daily or monthly "
                    "cap, then try it again." if held else
                    "Raise the per-minute cap in Budget, then try it again.")
        else:
            # Not the budget: a busy model source or a secret store that kept not answering.
            then = "Look at what is using it (Running work), then try it again."
        return fail_step(
            connection, project_id=project_id, step_run_id=step_run_id, worker_id=worker_id,
            error=(f"{reason.rstrip('.')} — still not started after {MAX_STEP_WAITS} tries. "
                   + then),
            retryable=False)
    delay = max(1, int(wait_seconds))
    wake_at = now_utc() + timedelta(seconds=delay)
    template = _step_map(_run_spec(connection, row["workflow_run_id"])).get(
        str(row["step_id"])) or {}
    sentence = (f"{_named_step(connection, project_id, template, str(row['step_id']))}: "
                f"{reason.rstrip('.')}. It resumes by itself — nothing to do.")[:1000]
    waiting = {"kind": kind, "reason": reason, "until": wake_at.isoformat(), "count": waits,
               "sentence": sentence}
    updated = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='ready', attempt=GREATEST(attempt - 1, 0), run_after=:wake_at,
                lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                output = COALESCE(output, '{}'::jsonb) || CAST(:output AS JSONB),
                updated_at=now()
            WHERE id=:id RETURNING *
            """
        ),
        {"id": row["id"], "wake_at": wake_at,
         "output": json.dumps({"waiting": waiting})},
    ).mappings().one()
    run = _run_row(connection, row["workflow_run_id"])
    connection.execute(
        text("UPDATE workflow_runs SET status='waiting', blocked_reason=:sentence, "
             "updated_at=now() WHERE id=:id AND status IN ('running','waiting')"),
        {"id": row["workflow_run_id"], "sentence": sentence},
    )
    _emit_step_event(connection, run, "workflow.step_waiting", updated,
                     {"waiting_for": kind, "reason": reason, "until": wake_at.isoformat(),
                      "count": waits})
    _refresh_frontier(connection, row["workflow_run_id"])
    return _serialize_step(updated)


def fail_step(
    connection: Connection,
    *,
    project_id: str,
    step_run_id: str,
    worker_id: str,
    error: str,
    retryable: bool = True,
    retry_delay_seconds: int = 0,
    #: Whatever the capability returned. Kept because a failed step stored `{}` and the only
    #: record of what happened was one sentence — which, on the deploy gate that prompted this,
    #: read "the capability reported failure without a reason". Evidence outlives the summary.
    output: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE id=:id AND project_id=:project_id FOR UPDATE
            """
        ),
        {"id": step_run_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow step run not found.")
    if row["status"] not in {"leased", "running"} or row["lease_owner"] != worker_id:
        raise WorkflowLeaseError("Workflow step is not leased by this worker.")
    # A FAILURE NO RETRY CAN FIX, SAID AS ITS FIX — whichever kind of step raised it. Here,
    # where every step failure is recorded, so no step path can miss it (model_failures).
    from .model_failures import settled_failure

    explained = settled_failure(error)
    if explained is not None:
        error, retryable = explained, False
    # BOSS'S OWN DATABASE FAILING IS SAID AS THAT, NOT AS ITS SQL. A step's last_error is what
    # the run screen shows; the database's diagnostics go to the step's output and the log.
    from .model_failures import explain, is_storage_failure

    if is_storage_failure(error):
        said = explain(error)
        logger.warning("workflow step %s failed on the database: %s", step_run_id, error)
        output = {**(output or {}), "error_detail": said["detail"]}
        error = f"{said['headline']} {said['action']}"
    return _fail_attempt(connection, project_id=project_id, row=row, error=error,
                         retryable=retryable, retry_delay_seconds=retry_delay_seconds,
                         output=output)


def _fail_attempt(
    connection: Connection,
    *,
    project_id: str,
    row: Any,
    error: str,
    retryable: bool,
    retry_delay_seconds: int = 0,
    output: Optional[dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """End one attempt of a leased step as failed: try again, take its error path, or stop.

    THE ONE PLACE AN ATTEMPT FAILS — a worker's `fail_step` and a timed-out attempt
    (`_time_out_overdue_steps`) both end here, so they retry, back off and stop the same way.

    * Attempts left and the failure retryable: the step goes back to `ready`, not to be leased
      before its `retry_backoff` (or the caller's `retry_delay_seconds`, whichever is later).
    * Out of attempts, or not retryable, with an `on_error` step: the step is recorded `failed`
      and stamped `error_path_taken`, `workflow.error_path_taken` is written, and the run goes
      on at the error step instead of stopping.
    * Otherwise it is dead-lettered and the run stops at it, as every failure always has.
    """
    attempts = int(row["attempt"])
    can_retry = retryable and attempts < int(row["max_attempts"])
    if not can_retry and _is_advisory_step(row):
        # AN ADVISORY STEP NEVER STOPS THE RUN, AT START EITHER — see `ADVISORY_CAPABILITIES`.
        return _complete_advisory_failure(connection, row, error, output)
    spec_steps = _step_map(_run_spec(connection, row["workflow_run_id"]))
    template = spec_steps.get(str(row["step_id"])) or {}
    why = reason or ("attempts_exhausted" if retryable else "not_retryable")
    if reason:
        # `reason` too, the key a step's output already says why under (a skip's reason), so a
        # reader looking for `step_timeout` finds it where it looks for any step's reason.
        output = {**(output or {}), "failure_reason": reason, "reason": reason}
    error_step = str(template.get("on_error") or "").strip() if not can_retry else ""
    if error_step not in spec_steps:
        error_step = ""
    backoff = retry_backoff_seconds(template, attempts) if can_retry else 0
    delay = max(0, int(retry_delay_seconds), backoff)
    status = "ready" if can_retry else ("failed" if error_step else "deadlettered")
    run_after = now_utc() + timedelta(seconds=delay)
    if error_step:
        output = {**(output or {}), ERROR_PATH_TAKEN: error_step}
    updated = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status=:status, last_error=:error, run_after=:run_after,
                lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                attempt_timeout_at=NULL,
                completed_at=CASE WHEN :terminal THEN now() ELSE NULL END,
                -- MERGED, not replaced: a step that parked and then failed keeps the await
                -- handle it was waiting on, which is how anything can be reconstructed later.
                output = COALESCE(output, '{}'::jsonb) || CAST(:output AS JSONB),
                updated_at=now()
            WHERE id=:id RETURNING *
            """
        ),
        {
            "id": row["id"],
            "status": status,
            "error": _clean_text(error, "error"),
            "run_after": run_after,
            "terminal": not can_retry,
            "output": json.dumps(output or {}),
        },
    ).mappings().one()
    run = _run_row(connection, row["workflow_run_id"])
    if error_step:
        # HANDLED, NOT HIDDEN. The step keeps its failure and its error; the run history says
        # the failure happened and where the run went because of it.
        _emit_step_event(connection, run, "workflow.step_failed", updated,
                         {"error": error, "retryable": False, "reason": why,
                          "attempts": attempts})
        _emit_step_event(connection, run, "workflow.error_path_taken", updated,
                         {"error_step_id": error_step, "reason": why, "attempts": attempts,
                          "error": error})
        if run["status"] in {"running", "waiting"}:
            _maybe_activate(connection, row["workflow_run_id"], error_step,
                            int(row["iteration"]))
            _drain_control_steps(connection, row["workflow_run_id"])
            _maybe_finish_run(connection, row["workflow_run_id"])
        # A paused run starts the error step when it is resumed (`_follow_edges_left_behind`).
        _refresh_frontier(connection, row["workflow_run_id"])
        return _serialize_step(updated)
    _emit_step_event(
        connection,
        run,
        "workflow.step_retry_scheduled" if can_retry else "workflow.step_deadlettered",
        updated,
        {"error": error, "retryable": can_retry, "run_after": run_after.isoformat(),
         "attempt": attempts, "backoff_seconds": backoff,
         **({"reason": reason} if reason else {})},
    )
    if not can_retry:
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (:id, :project_id, 'workflow_step', :source_id, :reason, CAST(:payload AS JSONB))
                """
            ),
            {
                "id": make_id("dlq"),
                "project_id": project_id,
                "source_id": row["id"],
                "reason": error,
                "payload": json.dumps(
                    {"workflow_run_id": row["workflow_run_id"], "step_id": row["step_id"]}
                ),
            },
        )
        each = for_each_bodies({"steps": list(spec_steps.values())}).get(str(row["step_id"]))
        # The run stops at this step, in the one shape every stop takes — see `_stop_run_at` —
        # unless an `any` join can still go on without it (a fan-out's partial failure). A
        # for_each's item stops it at its for_each instead, which is what Retry re-runs.
        if each in spec_steps:
            _fail_for_each_item(connection, row["workflow_run_id"], updated, each, error)
        elif not _an_any_join_may_go_on_without(connection, row["workflow_run_id"], updated):
            _stop_run_at(connection, row["workflow_run_id"], updated)
        else:
            # Gone on without: the run may now have nothing left to do but finish.
            _maybe_finish_run(connection, row["workflow_run_id"])
    _refresh_frontier(connection, row["workflow_run_id"])
    return _serialize_step(updated)


def _time_out_overdue_steps(connection: Connection, run_id: str) -> int:
    """End every attempt of this run that has run past its step's `timeout_seconds`.

    THE TIMEOUT REAPER, beside the lease reaper (`claim_step` reclaims an expired lease): an
    attempt whose clock ran out is failed with reason `step_timeout`, after a
    `workflow.step_timed_out` event, and then retries, backs off, takes its error path or stops
    exactly as any failed attempt does (`_fail_attempt`). It counts as an attempt. The lease is
    taken from the worker, so its late heartbeat or completion is refused.
    """
    rows = connection.execute(
        text(
            """
            SELECT * FROM workflow_step_runs
            WHERE workflow_run_id=:run_id AND status IN ('leased','running')
              AND attempt_timeout_at IS NOT NULL AND attempt_timeout_at < now()
            ORDER BY attempt_timeout_at, created_at
            FOR UPDATE
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    for row in rows:
        _time_out_attempt(connection, row)
    return len(rows)


def _time_out_attempt(connection: Connection, row: Any) -> dict[str, Any]:
    template = _step_map(_run_spec(connection, row["workflow_run_id"])).get(
        str(row["step_id"])) or {}
    seconds = template.get("timeout_seconds")
    run = _run_row(connection, row["workflow_run_id"])
    _emit_step_event(connection, run, "workflow.step_timed_out", row, {
        "reason": STEP_TIMEOUT_REASON, "timeout_seconds": seconds,
        "attempt": int(row["attempt"]), "max_attempts": int(row["max_attempts"]),
        "worker_id": row["lease_owner"],
    })
    error = (f"Timed out: attempt {int(row['attempt'])} ran longer than its "
             f"{seconds} second limit." if seconds else
             f"Timed out: attempt {int(row['attempt'])} ran past its time limit.")
    return _fail_attempt(connection, project_id=row["project_id"], row=row, error=error,
                         retryable=True, reason=STEP_TIMEOUT_REASON)


#: Steps whose failure never stops the run (operator, 2026-10-13: "pass 2 never blocks pass 1").
#: The runner's `_ADVISORY_CAPABILITIES` is the same set (a test holds them equal); it covers an
#: advisory step whose WORK failed. This covers one that failed to START (refused, broken, or out
#: of attempts before its work existed). That failure came through `fail_step`, which did not
#: ask, so a More tests pass that could not start stopped the whole run, and the `all` join of
#: two_pass's pass 2 took the deploy running beside it down too (BOSSAPP-540).
ADVISORY_CAPABILITIES = frozenset({"more_tests"})


def _is_advisory_step(row: Any) -> bool:
    """A step bound to an advisory capability, or marked `inputs.advisory` (two_pass's
    automatic preview): the two the runner completes rather than fails."""
    binding = _json(dict(row).get("binding_snapshot"), {}) or {}
    inner = binding.get("binding") if isinstance(binding.get("binding"), dict) else {}
    capability = str(binding.get("capability") or inner.get("capability") or "")
    inputs = binding.get("inputs") or inner.get("inputs") or {}
    marked = isinstance(inputs, dict) and str(inputs.get("advisory")).lower() == "true"
    return capability in ADVISORY_CAPABILITIES or marked


def _complete_advisory_failure(
    connection: Connection, row: Any, error: str, output: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """An advisory step out of attempts completes WITH THE FAILURE SAID, and the run goes on.

    Never silent: `last_error` keeps the cause, the output says `advisory_failed` with a
    sentence, and `workflow.step_advisory_failed` is recorded before the step's success is.
    Nothing is dead-lettered: the run did not stop, so there is nothing for an operator to
    decide. The output does not enter the run context: nothing downstream reads a step that
    did not do its work.
    """
    note = (f"this step could not start ({str(error).strip() or 'no reason given'}); it is "
            "advisory, so the run carries on without it.")[:600]
    said = {**(output or {}), "status": "not_applicable", "advisory": True,
            "advisory_failed": True, "note": note}
    updated = connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='succeeded', last_error=:error,
                lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                completed_at=clock_timestamp(),
                output = COALESCE(output, '{}'::jsonb) || CAST(:output AS JSONB),
                updated_at=now()
            WHERE id=:id RETURNING *
            """
        ),
        {"id": row["id"], "error": _clean_text(error, "error"), "output": json.dumps(said)},
    ).mappings().one()
    run = _run_row(connection, row["workflow_run_id"])
    _emit_step_event(connection, run, "workflow.step_advisory_failed", updated,
                     {"error": error, "note": note})
    _emit_step_event(connection, run, "workflow.step_succeeded", updated,
                     {"advisory_failed": True})
    _after_success(connection, row["workflow_run_id"], row["step_id"], int(row["iteration"]))
    return _serialize_step(updated)


def pause_run(
    connection: Connection, project_id: str, run_id: str, actor: str
) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] in TERMINAL_RUN_STATES:
        raise WorkflowConflictError("Terminal workflow runs cannot be paused.")
    if run["status"] != "paused":
        connection.execute(
            text(
                "UPDATE workflow_runs SET status='paused', paused_at=now(), updated_at=now() "
                "WHERE id=:id AND status NOT IN ('completed','failed','deadlettered','cancelled',"
                "'compensated')"
            ),
            {"id": run_id},
        )
        emit_event(
            connection,
            project_id,
            "workflow.run_paused",
            actor,
            "workflow_run",
            run_id,
            {},
        )
    return get_run(connection, project_id, run_id)


def resume_run(
    connection: Connection, project_id: str, run_id: str, actor: str
) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] != "paused":
        raise WorkflowConflictError("Only paused workflow runs can resume.")
    connection.execute(
        text(
            """
            UPDATE workflow_runs SET status='running', paused_at=NULL, updated_at=now()
            WHERE id=:id AND status='paused'
            """
        ),
        {"id": run_id},
    )
    emit_event(
        connection,
        project_id,
        "workflow.run_resumed",
        actor,
        "workflow_run",
        run_id,
        {},
    )
    # Steps that finished, or were skipped by an operator, while the run was paused had their
    # next steps held back (`_activate_step` does nothing on a paused run). Start them now.
    _follow_edges_left_behind(connection, run_id)
    return advance_run(connection, project_id, run_id)


#: Every column that points at a run and does NOT cascade. They are all nullable, so a deleted
#: run leaves the panel or studio record it belonged to intact, holding nothing instead of a
#: dangling id. Listed here rather than discovered at DELETE time, because the first anyone would
#: hear of a missing one is a foreign key violation in front of an operator.
POINTS_AT_A_RUN = (
    ("workflow_runs", "parent_run_id"),
    ("workflow_step_runs", "child_workflow_run_id"),
    ("panel_runs", "workflow_run_id"),
    ("agent_studio_runs", "workflow_run_id"),
    ("agent_group_runs", "workflow_run_id"),
)


def delete_run(connection: Connection, project_id: str, run_id: str, actor: str) -> dict[str, Any]:
    """Remove a finished run and everything it owns.

    WHY THIS EXISTS. There was no way to get rid of a run at all. Cancelling stops the work and
    leaves the row, and a deadlettered run is already terminal, so cancelling one does nothing —
    the operator is simply stuck with it. Eighteen runs from building one workflow filled the
    console: "lots of workflows shows error and waiting… please clean up the mess please."

    A run that is still going is NOT deleted, it is refused. Deleting the bookkeeping while the
    work carries on is the failure `cancel_run` documents above it, one table further down. Cancel
    it first, which stops the work, and then it can be removed. A finished child whose parent
    step is still waiting on it is refused for the same reason: the parent resumes from that
    child id, and clearing the pointer leaves the parent waiting forever.

    The steps, migrations and compensation rows go with it by cascade. The records that merely
    POINT at it — a panel, a studio run — are kept and their reference cleared: they are somebody
    else's rows, and deleting them because they mention this one would be the tidy-up destroying
    more than it was asked to.
    """
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    # Offered, and refused, by ONE rule: the run's "remove" action. A run still going — or one
    # busy undoing itself — is refused with what to do instead ("Cancel it first").
    from .workflow_run_actions import require

    require(connection, project_id, run_id, "remove")

    # A FINISHED CHILD IS STILL LOAD-BEARING WHILE ITS PARENT IS WAITING ON IT.
    #
    # A subworkflow step stores the child id and stays `waiting` until a later advance copies
    # the child's output (or records that the child failed). Nulling that pointer — which the
    # loop below does for every reference — makes the advance skip the step, because a
    # subworkflow with no child id is not a subworkflow it knows how to resume. The parent
    # then waits forever, and the child's output is already gone. The console lists that
    # child as its own finished run, so "clean up the error runs" deletes exactly the row
    # the waiting parent still needs.
    #
    # Cancelled parents are safe: cancel marks their waiting steps cancelled, which is
    # terminal, so they no longer match. A step that already succeeded has the output copied
    # onto itself and does not match either.
    # A parent STOPPED at a failed step is not over: once the operator retries or skips, its
    # waiting subworkflow step resumes from this child. Only a CLOSED parent no longer needs it.
    from .workflow_run_actions import CLOSED_RUN_STATES

    terminal_steps = ", ".join(f":done_{i}" for i in range(len(TERMINAL_STEP_STATES)))
    terminal_runs = ", ".join(f":closed_{i}" for i in range(len(CLOSED_RUN_STATES)))
    waiter = connection.execute(
        text(
            f"""
            SELECT r.id AS run_id, r.status AS run_status, s.step_id
            FROM workflow_step_runs s
            JOIN workflow_runs r ON r.id = s.workflow_run_id
            WHERE s.child_workflow_run_id = :run_id
              AND s.workflow_run_id <> :run_id
              AND s.status NOT IN ({terminal_steps})
              AND r.status NOT IN ({terminal_runs})
            LIMIT 1
            """
        ),
        {
            "run_id": run_id,
            **{f"done_{i}": status for i, status in enumerate(sorted(TERMINAL_STEP_STATES))},
            **{f"closed_{i}": status for i, status in enumerate(sorted(CLOSED_RUN_STATES))},
        },
    ).mappings().first()
    if waiter:
        raise WorkflowError(
            f"Run {waiter['run_id']} is {waiter['run_status']} and its step "
            f"'{waiter['step_id']}' is still waiting on this run. Removing it would leave "
            f"that run waiting forever — let it finish, or cancel it, and then this run "
            f"can be removed.")

    # BEFORE THE ROWS GO: a decision step's card is linked to its run only through the step row
    # this delete cascades away. Removed on boss-1, its gates stayed answerable. See
    # run_decisions.
    from .run_decisions import withdraw_with_run

    withdraw_with_run(connection, project_id, run_id,
                      why=f"its run {run_id} was removed", actor=actor)
    for table, column in POINTS_AT_A_RUN:
        connection.execute(
            text(f"UPDATE {table} SET {column} = NULL WHERE {column} = :run_id"),
            {"run_id": run_id},
        )
    # Recorded BEFORE the row goes, so the event has something to say about what was removed.
    emit_event(
        connection, project_id, "workflow.run_deleted", actor, "workflow_run", run_id,
        {"status": run["status"], "workflow_definition_id": run["workflow_definition_id"]},
    )
    connection.execute(text("DELETE FROM workflow_runs WHERE id = :id"), {"id": run_id})
    return {"run_id": run_id, "deleted": True, "was": run["status"]}


def cancel_run(
    connection: Connection,
    project_id: str,
    run_id: str,
    actor: str,
    *,
    reason: str,
    compensate: bool = True,
) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    from .workflow_run_actions import CLOSED_RUN_STATES, require

    if run["status"] in CLOSED_RUN_STATES:
        # Pressed twice, or a run that finished while the press was in flight: it is already
        # over, which is what was asked for. Reported, not refused — like a second Skip.
        #
        # CLOSED, NOT "TERMINAL". This returned early for `failed` and `deadlettered` too — a
        # run STOPPED at a failed step, which is waiting for the operator, not over. With a
        # parallel branch still leased under it, Stop did nothing and the branch kept working:
        # the 2026-09-02 shape (a cancel that did not stop the work), found by the review of
        # c8de36b. A stopped run is cancelled like any other: its live steps and their work.
        return get_run(connection, project_id, run_id)
    # The run's "stop" action decides the rest: a run busy compensating is refused (stopping it
    # would race the undo), in the words the console shows on its disabled Stop.
    require(connection, project_id, run_id, "stop")

    # STOP THE WORK, NOT JUST THE BOOKKEEPING.
    #
    # Cancelling used to mark the step rows and the run row and stop there, which cancels
    # nothing an operator would recognise as work: the code task the build step started keeps
    # running, on its own job, in its own sandbox.
    #
    # On 2026-09-02 that gap turned a cancelled verification run into a destroyed database. The
    # run was cancelled at 04:52 believing the build was stopped; the code task it had spawned
    # carried on to its QA step and, twenty minutes later, ran the API test suite against the
    # production DATABASE_URL it had inherited. 204 tables.
    #
    # "Cancel" has to mean the work stops. Anything else is a button that lies.
    cancelled_work = _cancel_work_this_run_started(connection, project_id, run_id, actor, reason)

    connection.execute(
        text(
            """
            UPDATE workflow_step_runs SET status='cancelled', completed_at=clock_timestamp(),
                lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL, updated_at=now()
            -- LEASED AND RUNNING TOO. A step a worker holds right now was left `leased` under
            -- a cancelled run; when its tool returned, the worker parked it against the build
            -- it had just started — work set going after Stop. Cancelled here, the worker's
            -- complete/park is refused (it no longer holds a lease) and rolls its step back.
            WHERE workflow_run_id=:run_id
              AND status IN ('pending','ready','waiting','leased','running')
            """
        ),
        {"run_id": run_id},
    )
    connection.execute(
        text(
            """
            UPDATE workflow_runs SET status='cancelled', error=:reason, blocked_reason=NULL,
                current_frontier='[]'::jsonb, completed_at=clock_timestamp(), updated_at=now()
            WHERE id=:id
            """
        ),
        {"id": run_id, "reason": reason},
    )
    emit_event(
        connection,
        project_id,
        "workflow.run_cancelled",
        actor,
        "workflow_run",
        run_id,
        {"reason": reason, "compensate": compensate},
    )
    # ITS QUESTIONS GO WITH IT. A stopped run's gates stayed in Approvals with Approve and
    # Reject on them (boss-1, 2026-10-04: 21 of them). See run_decisions.
    from .run_decisions import withdraw_with_run

    withdraw_with_run(connection, project_id, run_id,
                      why=f"its run {run_id} was cancelled ({reason})", actor=actor)
    _cancel_child_runs(connection, project_id, run_id, actor, compensate=compensate)
    if compensate:
        compensation_count = connection.execute(
            text(
                "SELECT count(*) FROM workflow_compensation_runs WHERE workflow_run_id=:run_id"
            ),
            {"run_id": run_id},
        ).scalar_one()
        if compensation_count:
            begin_compensation(connection, project_id, run_id, actor, reason=reason)
    return get_run(connection, project_id, run_id)


def _cancel_child_runs(connection: Connection, project_id: str, run_id: str, actor: str, *,
                       compensate: bool) -> None:
    """Stop the runs a "Run another workflow" step of this run started.

    A child is this run's work as much as a code task is, and nothing consumes its output once
    the parent is cancelled — yet it went on, and its own gates went on asking for answers about
    a parent that had been stopped. Cancelled through `cancel_run`, so each child stops its own
    work, withdraws its own decisions and stops its own children. A child busy compensating is
    left to finish undoing itself (cancelling it would race that).
    """
    from .workflow_run_actions import CLOSED_RUN_STATES

    children = connection.execute(
        text("SELECT id, status FROM workflow_runs WHERE parent_run_id = :r AND project_id = :p"),
        {"r": run_id, "p": project_id},
    ).mappings().all()
    for child in children:
        if child["status"] in CLOSED_RUN_STATES or child["status"] == "compensating":
            continue
        try:
            cancel_run(connection, project_id, str(child["id"]), actor,
                       reason=f"its parent run {run_id} was cancelled", compensate=compensate)
        except WorkflowError:
            # Refused before it wrote anything (the child's own "stop" rule). The parent's
            # cancel must not fail because a child could not be stopped with it.
            logger.exception("could not cancel child run %s of %s", child["id"], run_id)


#: The long-running thing each async step starts, and the table that records its state. A step
#: parks against one of these; cancelling the run has to reach through to it.
_WORK_TABLES: dict[str, str] = {
    "code_task": "code_task_runs",
    "discovery": "discovery_runs",
    "meeting": "meeting_runs",
    "preview": "app_previews",
    # A deploy step now WAITS on its deployment run (it used to go green on "planned"), so a
    # cancelled run has one more kind of work it may have set going. Planned-and-unapplied is
    # the common case, and cancelling the record is what stops somebody applying it later
    # believing the workflow still wants it.
    "deployment": "deployment_runs",
    # An architecture waiting for a person: cancelling the run closes the review, and the
    # run's decision is no longer answerable (run_decisions.refusal_to_answer).
    "architecture": "architecture_reviews",
    # Waiting for CI (ci_gate): cancelling the run stops the looking; the wait no longer gates.
    "ci_wait": "ci_waits",
    # The security_review workflow's scan. Its job carries {"scan_id"} (jobs.run_job_match_sql).
    "security_scan": "security_scans",
}

#: How a table records "this was stopped", where it differs from status='cancelled' and
#: updated_at. security_scans allows only queued/running/done/failed and has no updated_at:
#: the generic UPDATE fails there, and inside the cancel's transaction it would abort it.
_WORK_CANCEL_SQL: dict[str, str] = {
    "security_scans": "status='failed', ended_at=now()",
}

_WORK_TERMINAL: frozenset[str] = frozenset(
    {"completed", "succeeded", "failed", "cancelled", "blocked", "stopped", "expired",
     # A deployment's own finished states. Without them a cancel would relabel a deployment
     # that already shipped (`deployed`, `healthy`) or already failed as "cancelled".
     "deployed", "healthy", "unhealthy", "rolled_back", "rollback_pending",
     # A security scan's own finished state.
     "done",
     # An architecture a person already answered stays answered.
     "approved", "rejected"}
)


#: What each kind of awaited work is called in the words the console already uses for the step
#: that starts it, so "waiting on ..." names something the operator can see on the same screen.
_AWAIT_LABELS: dict[str, str] = {
    "discovery": "the research to finish",
    "meeting": "the panel to finish",
    "code_task": "the build to finish",
    "preview": "the preview to come up",
    "deployment": "the deployment to finish",
    "ci_wait": "CI to finish",
    "agent_run": "the agent's approved run to finish",
    "architecture": "a person to approve the architecture",
}


#: A code task awaited by a step that is not the build, named after that step.
_CODE_TASK_BY_CAPABILITY: dict[str, str] = {
    "plrr": "the readiness fixes to finish",
    "more_tests": "the More tests pass to finish",
    "redeploy": "the rebuild to finish",
}


def waiting_on_label(step: dict[str, Any]) -> Optional[str]:
    """What a waiting step is waiting FOR — or None when that is not recorded.

    THE BUG THIS REPLACES. The console said "waiting on a decision" for every waiting step
    that had no approver, because "a decision" was the fallback. A research step that had
    started a discovery run and was waiting for it to finish therefore told the operator to go
    and decide something that did not exist, on a run that was working perfectly:

        "waiting on a decision" — while drun_864aa33554b14eb0b541 was running normally.

    Sending someone to look for a decision that is not there is worse than saying nothing. It
    costs them the trip, and then it costs the console its credibility, because the next
    genuine "waiting on you" reads like noise.

    So the fallback is None: the status pill already says `waiting`, and silence is the honest
    answer to a question we cannot answer.
    """
    approver = (step.get("approval") or {}).get("approver_role")
    if approver:
        return str(approver)
    if step.get("decision_id"):
        return "a decision"
    awaited = (step.get("output") or {}).get("await")
    if isinstance(awaited, dict):
        kind = str(awaited.get("kind") or "").strip()
        # NOT EVERY CODE TASK IS THE BUILD. Readiness extends the build's task, and its run
        # said "waiting on the build to finish" beside a build step marked succeeded.
        capability = str(step.get("capability") or (step.get("output") or {}).get("capability") or "")
        if kind == "code_task" and capability and capability != "build":
            return _CODE_TASK_BY_CAPABILITY.get(capability, "the code work to finish")
        label = _AWAIT_LABELS.get(kind)
        if label:
            return label
    return None


def _cancel_work_this_run_started(
    connection: Connection, project_id: str, run_id: str, actor: str, reason: str,
) -> list[dict[str, Any]]:
    """Stop the code tasks, discoveries, meetings and previews this run set going.

    Each async step records what it is waiting on in its output, as {"await": {kind, id}} — the
    same handle `resume_awaited` uses to complete it. That handle is the thread back to the real
    work, so cancellation follows it rather than guessing from step ids.

    Jobs are cancelled with force=True deliberately. The usual rule is that a running job is
    left alone because killing it leaves a half-written branch — but here an operator has said
    stop, and a half-written branch on an abandoned task is unambiguously better than an agent
    that keeps running after being cancelled. That is precisely the trade the incident settled.

    Best-effort per item, and never raises: a code task that cannot be stopped must not prevent
    the run from being cancelled, or "cancel" fails exactly when it is needed most.
    """
    # JobQueue HAS NEVER EXISTED HERE. `cancel_queued_job` is a method on JobLeaseService, so
    # this import raised ImportError the first time anybody cancelled a run — the worst moment
    # for a bad import, and directly above the comment promising that "cancel has to mean the
    # work stops. Anything else is a button that lies." It lied by 500.
    from ..config import get_settings
    from .jobs import JobLeaseService, run_job_match_sql

    job_service = JobLeaseService(get_settings())
    stopped: list[dict[str, Any]] = []
    rows = connection.execute(
        text(
            """
            SELECT step_id, output FROM workflow_step_runs
            WHERE workflow_run_id = :run_id AND status IN ('waiting', 'running', 'leased')
            """
        ),
        {"run_id": run_id},
    ).mappings().all()

    for row in rows:
        output = _json(row["output"], {}) or {}
        awaited = output.get("await") if isinstance(output.get("await"), dict) else None
        if not awaited:
            continue
        kind = str(awaited.get("kind") or "")
        work_id = str(awaited.get("id") or "").strip()
        if not work_id or kind not in _WORK_TABLES:
            continue

        # The job first: that is the thing with a process behind it.
        try:
            jobs = connection.execute(
                text(
                    f"SELECT j.id FROM jobs j WHERE "
                    f"{run_job_match_sql(_WORK_TABLES[kind], ':work_id')} "
                    "AND j.project_id = :project_id "
                    "AND j.status NOT IN ('succeeded','cancelled','deadlettered')"
                ),
                {"work_id": work_id, "project_id": project_id},
            ).scalars().all()
            for job_id in jobs:
                job_service.cancel_queued_job(
                    connection, job_id, force=True, actor=actor,
                    reason=f"workflow run {run_id} was cancelled: {reason}",
                )
        except Exception:  # noqa: BLE001 — a job that will not stop must not block the cancel
            logger.exception("could not cancel jobs for %s %s", kind, work_id)

        # Then the record, so the console and every resume path agree it is over.
        table = _WORK_TABLES[kind]
        stop = _WORK_CANCEL_SQL.get(table, "status='cancelled', updated_at=now()")
        try:
            # Its own savepoint: a statement that fails must not abort the cancel around it.
            with connection.begin_nested():
                connection.execute(
                    text(
                        f"""
                        UPDATE {table} SET {stop}
                        WHERE id = :work_id AND project_id = :project_id
                          AND status NOT IN :terminal
                        """
                    ).bindparams(bindparam("terminal", expanding=True)),
                    {"work_id": work_id, "project_id": project_id,
                     "terminal": sorted(_WORK_TERMINAL)},
                )
        except Exception:  # noqa: BLE001 — table shapes differ; the job cancel is what matters
            logger.exception("could not mark %s %s cancelled", kind, work_id)

        stopped.append({"step_id": row["step_id"], "kind": kind, "id": work_id})

    if stopped:
        emit_event(
            connection, project_id, "workflow.work_cancelled", actor, "workflow_run", run_id,
            {"stopped": stopped, "reason": reason},
        )
    return stopped


def begin_compensation(
    connection: Connection,
    project_id: str,
    run_id: str,
    actor: str,
    *,
    reason: str,
) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    rows = connection.execute(
        text(
            """
            SELECT * FROM workflow_compensation_runs
            WHERE workflow_run_id=:run_id AND status IN ('pending','ready')
            ORDER BY sequence DESC
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    if not rows:
        raise WorkflowConflictError("Workflow run has no pending compensation actions.")
    connection.execute(
        text(
            """
            UPDATE workflow_compensation_runs SET status='pending', updated_at=now()
            WHERE workflow_run_id=:run_id AND status='ready'
            """
        ),
        {"run_id": run_id},
    )
    connection.execute(
        text(
            "UPDATE workflow_compensation_runs SET status='ready', updated_at=now() WHERE id=:id"
        ),
        {"id": rows[0]["id"]},
    )
    connection.execute(
        text(
            """
            UPDATE workflow_runs SET status='compensating', error=:reason,
                blocked_reason=NULL, updated_at=now() WHERE id=:id
            """
        ),
        {"id": run_id, "reason": reason},
    )
    emit_event(
        connection,
        project_id,
        "workflow.compensation_started",
        actor,
        "workflow_run",
        run_id,
        {"reason": reason, "action_count": len(rows)},
    )
    return get_run(connection, project_id, run_id)


def claim_compensation(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    worker_id: str,
    lease_seconds: int = 60,
) -> Optional[dict[str, Any]]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] != "compensating":
        return None
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_compensation_runs
            WHERE workflow_run_id=:run_id AND (
              status='ready' OR (status IN ('leased','running') AND lease_expires_at < now())
            ) ORDER BY sequence DESC FOR UPDATE SKIP LOCKED LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    if not row:
        return None
    updated = connection.execute(
        text(
            """
            UPDATE workflow_compensation_runs
            SET status='leased', attempt=attempt+1, lease_owner=:worker_id,
                lease_expires_at=:expires_at, heartbeat_at=now(), updated_at=now()
            WHERE id=:id RETURNING *
            """
        ),
        {
            "id": row["id"],
            "worker_id": worker_id,
            "expires_at": now_utc() + timedelta(seconds=lease_seconds),
        },
    ).mappings().one()
    return _serialize_compensation(updated)


def complete_compensation(
    connection: Connection,
    *,
    project_id: str,
    compensation_id: str,
    worker_id: str,
    output: dict[str, Any],
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_compensation_runs
            WHERE id=:id AND project_id=:project_id FOR UPDATE
            """
        ),
        {"id": compensation_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow compensation run not found.")
    if row["status"] == "succeeded":
        return _serialize_compensation(row)
    if row["status"] not in {"leased", "running"} or row["lease_owner"] != worker_id:
        raise WorkflowLeaseError("Compensation is not leased by this worker.")
    updated = connection.execute(
        text(
            """
            UPDATE workflow_compensation_runs
            SET status='succeeded', output=CAST(:output AS JSONB), lease_owner=NULL,
                lease_expires_at=NULL, heartbeat_at=NULL, updated_at=now()
            WHERE id=:id RETURNING *
            """
        ),
        {"id": compensation_id, "output": json.dumps(output or {})},
    ).mappings().one()
    next_row = connection.execute(
        text(
            """
            SELECT * FROM workflow_compensation_runs
            WHERE workflow_run_id=:run_id AND status='pending'
            ORDER BY sequence DESC LIMIT 1
            """
        ),
        {"run_id": row["workflow_run_id"]},
    ).mappings().first()
    run = _run_row(connection, row["workflow_run_id"])
    if next_row:
        connection.execute(
            text(
                "UPDATE workflow_compensation_runs SET status='ready', updated_at=now() WHERE id=:id"
            ),
            {"id": next_row["id"]},
        )
    else:
        connection.execute(
            text(
                """
                UPDATE workflow_runs SET status='compensated', completed_at=clock_timestamp(),
                    blocked_reason=NULL, updated_at=now() WHERE id=:id
                """
            ),
            {"id": run["id"]},
        )
        emit_event(
            connection,
            project_id,
            "workflow.compensation_completed",
            run["actor"],
            "workflow_run",
            run["id"],
            {},
        )
        from .run_decisions import withdraw_with_run

        withdraw_with_run(connection, project_id, run["id"],
                          why=f"its run {run['id']} was undone", actor=run["actor"] or "workflow")
    return _serialize_compensation(updated)


def fail_compensation(
    connection: Connection,
    *,
    project_id: str,
    compensation_id: str,
    worker_id: str,
    error: str,
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT * FROM workflow_compensation_runs
            WHERE id=:id AND project_id=:project_id FOR UPDATE
            """
        ),
        {"id": compensation_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        raise WorkflowNotFoundError("Workflow compensation run not found.")
    if row["status"] not in {"leased", "running"} or row["lease_owner"] != worker_id:
        raise WorkflowLeaseError("Compensation is not leased by this worker.")
    retry = int(row["attempt"]) < int(row["max_attempts"])
    status = "ready" if retry else "deadlettered"
    updated = connection.execute(
        text(
            """
            UPDATE workflow_compensation_runs
            SET status=:status, last_error=:error, lease_owner=NULL,
                lease_expires_at=NULL, heartbeat_at=NULL, updated_at=now()
            WHERE id=:id RETURNING *
            """
        ),
        {"id": compensation_id, "status": status, "error": error},
    ).mappings().one()
    if not retry:
        connection.execute(
            text(
                """
                UPDATE workflow_runs SET status='deadlettered',
                    error=:error, updated_at=now() WHERE id=:id
                """
            ),
            {"id": row["workflow_run_id"], "error": f"compensation failed: {error}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (:id, :project_id, 'workflow_compensation', :source_id,
                        :reason, CAST(:payload AS JSONB))
                """
            ),
            {
                "id": make_id("dlq"),
                "project_id": project_id,
                "source_id": compensation_id,
                "reason": error,
                "payload": json.dumps({"workflow_run_id": row["workflow_run_id"]}),
            },
        )
    return _serialize_compensation(updated)


def _serialize_compensation(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_run_id": row["workflow_run_id"],
        "workflow_step_run_id": row["workflow_step_run_id"],
        "sequence": int(row["sequence"]),
        "status": row["status"],
        "action": _json(row["action"], {}),
        "output": _json(row["output"], {}),
        "dedupe_key": row["dedupe_key"],
        "attempt": int(row["attempt"]),
        "max_attempts": int(row["max_attempts"]),
        "lease_owner": row.get("lease_owner"),
        "lease_expires_at": _iso(row.get("lease_expires_at")),
        "last_error": row.get("last_error"),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def migrate_run(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    target_version_id: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] != "paused":
        raise WorkflowConflictError("Only paused workflow runs can migrate.")
    active = connection.execute(
        text(
            """
            SELECT count(*) FROM workflow_step_runs
            WHERE workflow_run_id=:run_id AND status IN ('leased','running')
            """
        ),
        {"run_id": run_id},
    ).scalar_one()
    if active:
        raise WorkflowConflictError("A paused run with active leases cannot migrate.")
    # The run will reach the target's steps: not while an agent delete is deciding.
    hold_workflow_reach(connection, project_id)
    target = _version_row(connection, project_id, target_version_id)
    if target["workflow_definition_id"] != run["workflow_definition_id"]:
        raise WorkflowValidationError("Target version belongs to another workflow definition.")
    old_spec = _run_spec(connection, run_id)
    new_spec = _published_spec(target["spec"], project_roster(connection, project_id))
    old_steps = _step_map(old_spec)
    new_steps = _step_map(new_spec)
    rows = connection.execute(
        text("SELECT * FROM workflow_step_runs WHERE workflow_run_id=:run_id ORDER BY created_at"),
        {"run_id": run_id},
    ).mappings().all()
    incompatible: list[str] = []
    for row in rows:
        if row["status"] in {"succeeded", "ready", "waiting"}:
            target_step = new_steps.get(row["step_id"])
            if not target_step or target_step["kind"] != row["step_kind"]:
                incompatible.append(row["step_id"])
    if incompatible:
        raise WorkflowConflictError(
            "Target version is incompatible with completed/frontier steps: "
            + ", ".join(sorted(set(incompatible)))
        )
    snapshot = {
        "workflow_version_id": run["workflow_version_id"],
        "steps": [
            {"id": row["id"], "step_id": row["step_id"], "iteration": int(row["iteration"]), "status": row["status"]}
            for row in rows
        ],
    }
    migration_id = make_id("wfmg")
    connection.execute(
        text(
            """
            INSERT INTO workflow_migrations (
                id, project_id, workflow_run_id, from_version_id, to_version_id,
                status, compatibility, rollback_snapshot, reason, created_by
            ) VALUES (
                :id, :project_id, :run_id, :from_version_id, :to_version_id,
                'completed', CAST(:compatibility AS JSONB), CAST(:snapshot AS JSONB),
                :reason, :actor
            )
            """
        ),
        {
            "id": migration_id,
            "project_id": project_id,
            "run_id": run_id,
            "from_version_id": run["workflow_version_id"],
            "to_version_id": target_version_id,
            "compatibility": json.dumps({"compatible": True, "preserved_steps": len(rows)}),
            "snapshot": json.dumps(snapshot),
            "reason": _clean_text(reason, "reason"),
            "actor": actor,
        },
    )
    connection.execute(
        text(
            "UPDATE workflow_runs SET workflow_version_id=:version_id, updated_at=now() WHERE id=:id"
        ),
        {"version_id": target_version_id, "id": run_id},
    )
    existing_ids = {(row["step_id"], int(row["iteration"])) for row in rows}
    for step in new_spec["steps"]:
        if (step["id"], 0) not in existing_ids:
            _insert_step_run(
                connection,
                project_id=project_id,
                run_id=run_id,
                step=step,
                iteration=0,
                status="pending",
            )
    for step_id in set(old_steps) - set(new_steps):
        connection.execute(
            text(
                """
                UPDATE workflow_step_runs SET status='cancelled', updated_at=now()
                WHERE workflow_run_id=:run_id AND step_id=:step_id AND status='pending'
                """
            ),
            {"run_id": run_id, "step_id": step_id},
        )
    emit_event(
        connection,
        project_id,
        "workflow.run_migrated",
        actor,
        "workflow_run",
        run_id,
        {
            "migration_id": migration_id,
            "from_version_id": run["workflow_version_id"],
            "to_version_id": target_version_id,
            "reason": reason,
        },
    )
    return get_run(connection, project_id, run_id)


def rollback_migration(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    migration_id: str,
    actor: str,
) -> dict[str, Any]:
    run = _run_row(connection, run_id, lock=True)
    if run["project_id"] != project_id:
        raise WorkflowNotFoundError("Workflow run not found.")
    if run["status"] != "paused":
        raise WorkflowConflictError("Only paused workflow runs can roll back migration.")
    migration = connection.execute(
        text(
            """
            SELECT * FROM workflow_migrations
            WHERE id=:id AND workflow_run_id=:run_id AND project_id=:project_id
            FOR UPDATE
            """
        ),
        {"id": migration_id, "run_id": run_id, "project_id": project_id},
    ).mappings().first()
    if not migration:
        raise WorkflowNotFoundError("Workflow migration not found.")
    if migration["status"] == "rolled_back":
        return get_run(connection, project_id, run_id)
    if migration["status"] != "completed" or run["workflow_version_id"] != migration["to_version_id"]:
        raise WorkflowConflictError("Migration is not the active completed migration.")
    # Rolling back puts the run on its old version again. That version may have been retired
    # since (an agent it named was deleted — review of milestone/agents, round 3), and a run put
    # back on it would fail at the step naming the agent that is gone. Only a RETIRED version is
    # refused here; everything rollback accepted before, it still accepts.
    hold_workflow_reach(connection, project_id)
    retired = connection.execute(
        text("SELECT version FROM workflow_versions WHERE id = :id AND status = 'retired'"),
        {"id": migration["from_version_id"]},
    ).scalar()
    if retired is not None:
        raise WorkflowConflictError(
            f"this run cannot go back to v{retired}: that version was retired because an agent "
            "it named was deleted. Keep the run on the version it migrated to, or stop it and "
            "start the workflow's current version.")
    snapshot = _json(migration["rollback_snapshot"], {})
    keep_ids = {item["id"] for item in snapshot.get("steps", [])}
    rows = connection.execute(
        text("SELECT id, status FROM workflow_step_runs WHERE workflow_run_id=:run_id"),
        {"run_id": run_id},
    ).mappings().all()
    new_rows = [row for row in rows if row["id"] not in keep_ids]
    if any(row["status"] not in {"pending", "cancelled", "skipped"} for row in new_rows):
        raise WorkflowConflictError("New-version steps have executed; migration rollback is unsafe.")
    if new_rows:
        connection.execute(
            text("DELETE FROM workflow_step_runs WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": tuple(row["id"] for row in new_rows)},
        )
    for item in snapshot.get("steps", []):
        connection.execute(
            text("UPDATE workflow_step_runs SET status=:status, updated_at=now() WHERE id=:id"),
            {"id": item["id"], "status": item["status"]},
        )
    connection.execute(
        text(
            "UPDATE workflow_runs SET workflow_version_id=:version_id, updated_at=now() WHERE id=:id"
        ),
        {"version_id": migration["from_version_id"], "id": run_id},
    )
    connection.execute(
        text(
            """
            UPDATE workflow_migrations SET status='rolled_back', rolled_back_by=:actor,
                rolled_back_at=now() WHERE id=:id
            """
        ),
        {"id": migration_id, "actor": actor},
    )
    emit_event(
        connection,
        project_id,
        "workflow.migration_rolled_back",
        actor,
        "workflow_run",
        run_id,
        {"migration_id": migration_id, "restored_version_id": migration["from_version_id"]},
    )
    return get_run(connection, project_id, run_id)


def _serialize_migration(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_run_id": row["workflow_run_id"],
        "from_version_id": row["from_version_id"],
        "to_version_id": row["to_version_id"],
        "status": row["status"],
        "compatibility": _json(row["compatibility"], {}),
        "reason": row["reason"],
        "created_by": row["created_by"],
        "rolled_back_by": row.get("rolled_back_by"),
        "created_at": _iso(row["created_at"]),
        "rolled_back_at": _iso(row.get("rolled_back_at")),
    }


def seed_specpack_workflows(
    connection: Connection,
    *,
    project_id: str,
    workflow_root: Path,
    actor: str = "bootstrap",
) -> list[dict[str, Any]]:
    """Register the shipped YAML workflow contracts as immutable runtime versions.

    The historical specpack schema describes ordered ``phases`` rather than
    executable runtime ``steps``.  The adapter preserves the complete source
    contract as metadata while producing a conservative, sequential task graph;
    future native runtime specs can use ``entry_step``/``steps`` directly.
    """
    registered: list[dict[str, Any]] = []
    for path in sorted(workflow_root.glob("*.yaml")):
        source = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            continue
        workflow_key = str(source.get("id") or path.stem).strip()
        definition_row = connection.execute(
            text(
                """
                SELECT * FROM workflow_definitions
                WHERE project_id=:project_id AND workflow_key=:workflow_key
                """
            ),
            {"project_id": project_id, "workflow_key": workflow_key},
        ).mappings().first()
        if definition_row:
            definition = _serialize_definition(definition_row)
        else:
            definition = create_definition(
                connection,
                project_id=project_id,
                workflow_key=workflow_key,
                name=str(source.get("name") or workflow_key.replace("_", " ").title()),
                description=source.get("description") or source.get("purpose"),
                actor=actor,
                # BOSS's own whoever asked for the seed, so a later release that drops this
                # file from the specpack does not hand it to the customer at the next boot.
                made_by_boss=True,
            )

        if isinstance(source.get("steps"), list) and source.get("entry_step"):
            runtime_spec = source
        else:
            raw_phases = source.get("phases") or source.get("states") or ["contract"]
            phases = raw_phases if isinstance(raw_phases, list) else ["contract"]
            steps: list[dict[str, Any]] = []
            used_ids: set[str] = set()
            for index, phase in enumerate(phases):
                raw_id = phase.get("id") if isinstance(phase, dict) else phase
                base_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw_id or f"phase-{index + 1}"))
                base_id = base_id.strip("-.") or f"phase-{index + 1}"
                step_id = base_id
                suffix = 2
                while step_id in used_ids:
                    step_id = f"{base_id}-{suffix}"
                    suffix += 1
                used_ids.add(step_id)
                step: dict[str, Any] = {
                    "id": step_id,
                    "kind": "task",
                    "input": {
                        "source_phase": phase,
                        "source_file": path.name,
                    },
                }
                steps.append(step)
            for current, following in zip(steps, steps[1:]):
                current["next"] = [following["id"]]
            runtime_spec = {
                "entry_step": steps[0]["id"],
                "steps": steps,
                "source_contract": source,
                "source_file": path.name,
                "source_schema": "specpack.workflow.v0",
            }
        version = publish_version(
            connection,
            project_id=project_id,
            definition_id=definition["id"],
            spec=runtime_spec,
            actor=actor,
            activate=True,
        )
        registered.append(
            {
                "workflow_definition_id": definition["id"],
                "workflow_version_id": version["id"],
                "workflow_key": workflow_key,
                "duplicate": bool(version.get("duplicate")),
                "source_file": path.name,
            }
        )
    return registered


#: THE SELECTOR THE PRODUCT ALREADY USES.
#:
#: `POST /v1/delivery-workflows/apply` — the "use this workflow for this kind of work" action in
#: the Workflows area — creates mappings with `selector_kind="work_class"`. My first version of
#: this resolver invented `selector_kind="code_task"` instead, which nothing writes: applying a
#: workflow in the console would have had no effect on what a project resolved to, and the two
#: halves of the same feature would have been permanently unable to see each other.
#:
#: A code task is the build of a product, so it resolves against the same work classes the
#: catalogue declares. `'*'` is not used here — the catalogue's own wildcard is `"any"`.
CODE_TASK_SELECTOR_KIND = "work_class"

#: What a code task counts AS, in work-class terms. `project.new` is what the catalogue gives
#: Standard Product Delivery, and `any` is the catalogue's own wildcard — tried in order, so a
#: workflow applied specifically to new projects beats one applied to everything.
CODE_TASK_WORK_CLASSES = ("project.new", "any")


def workflow_for_code_task(
    connection: Connection,
    *,
    project_id: str,
    task_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Which workflow governs this code task, or None when nothing maps to it.

    The workflow runtime and the coding pipeline were built as separate systems: 54 definitions
    existed, one workflow run had ever been started, and no code task had ever recorded which
    workflow it belonged to — there was nowhere to record it. So the console could show a
    catalogue of workflows and a list of runs with no way to say that either had anything to do
    with the other.

    This is the first half of the join: the fact, resolved and written down at creation. It
    does NOT change how a run executes — the pipeline's phase sequence is still its own — and
    saying otherwise on screen would be worse than saying nothing.

    Returns None rather than raising. A project with no mapping is the normal state today, and
    an unmapped task must still run.
    """
    selected: dict[str, Any] = {}
    for work_class in CODE_TASK_WORK_CLASSES:
        try:
            resolution = resolve_mapping(
                connection,
                project_id=project_id,
                selector_kind=CODE_TASK_SELECTOR_KIND,
                selector_value=work_class,
            )
        except Exception:  # noqa: BLE001 — a missing link must never stop a task starting
            return None
        if resolution.get("status") == "selected":
            selected = resolution.get("selected") or {}
            break
    version_id = selected.get("workflow_version_id")
    if not version_id:
        return _default_workflow(connection, project_id)
    row = connection.execute(
        text(
            """
            SELECT d.id AS definition_id, d.workflow_key, d.name, v.version
            FROM workflow_versions v
            JOIN workflow_definitions d ON d.id = v.workflow_definition_id
            WHERE v.id = :version_id
            """
        ),
        {"version_id": version_id},
    ).mappings().first()
    if not row:
        return None
    return {
        "definition_id": row["definition_id"],
        "version_id": version_id,
        "mapping_id": selected.get("id"),
        "workflow_key": row["workflow_key"],
        "name": row["name"],
        "version": row["version"],
        # A mapping somebody made, as opposed to the fallback below. The console says which,
        # because "this project was deliberately put on this workflow" and "nothing was chosen
        # so it got the standard one" are different facts and only one of them is a decision.
        "source": "mapping",
    }


#: The workflow a project gets when nobody has chosen one. `key` is stable across fleets by
#: design, which is what makes a default safe to hard-code here.
DEFAULT_WORKFLOW_KEY = "standard_product_delivery"


def running_version(
    connection: Connection,
    project_id: str,
    workflow_key: str,
    *,
    namespace: Optional[str] = PRODUCT_NAMESPACE,
    definition_status: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """The version that actually runs, not the highest-numbered one.

    Re-saving an earlier spec reactivates that version without minting a newer number, so
    `ORDER BY version DESC` returns the spec the operator just replaced. Starting, forking,
    or falling back to the standard workflow from that ordering runs the abandoned one while
    the builder — which reads the active version — shows the one they restored.

    The active version wins. A definition that has never been activated falls back to the
    newest published version, which is the only one it has.
    """
    filters = [
        "d.project_id = :project_id",
        "d.workflow_key = :workflow_key",
        "v.status = 'published'",
    ]
    params: dict[str, Any] = {"project_id": project_id, "workflow_key": workflow_key}
    if namespace is not None:
        filters.append("COALESCE(d.namespace, 'system') = :namespace")
        params["namespace"] = namespace
    if definition_status is not None:
        filters.append("d.status = :definition_status")
        params["definition_status"] = definition_status
    row = connection.execute(
        text(
            f"""
            SELECT d.id AS definition_id, d.workflow_key, d.name, d.description,
                   v.id AS version_id, v.version, v.spec
            FROM workflow_definitions d
            JOIN workflow_versions v ON v.workflow_definition_id = d.id
            WHERE {' AND '.join(filters)}
            ORDER BY (v.id = d.active_version_id) DESC NULLS LAST,
                     v.version DESC
            LIMIT 1
            """
        ),
        params,
    ).mappings().first()
    return dict(row) if row else None


def _default_workflow(
    connection: Connection, project_id: str
) -> Optional[dict[str, Any]]:
    """The standard workflow, when no mapping selects one.

    Every project is unmapped until somebody maps it, so without this the answer to "which
    workflow governs my project" would be "none" for every project that has ever existed —
    technically true and useless. A new project is on the standard path unless told otherwise,
    which is what the catalogue is FOR.

    Marked `source: "default"` and NOT written as a mapping. Inventing a mapping row on the
    customer's behalf would make an inferred fallback indistinguishable from a choice they
    made, and the next person to read the mappings table could not tell which was which.
    """
    row = running_version(
        connection, project_id, DEFAULT_WORKFLOW_KEY,
        namespace=None, definition_status="active",
    )
    if not row:
        return None
    return {
        "definition_id": row["definition_id"],
        "version_id": row["version_id"],
        "mapping_id": None,
        "workflow_key": row["workflow_key"],
        "name": row["name"],
        "version": row["version"],
        "source": "default",
    }


def workflow_by_version(
    connection: Connection, project_id: str, version_id: str
) -> Optional[dict[str, Any]]:
    """A workflow the operator PICKED, resolved from the version they picked.

    An explicit choice outranks a mapping and outranks the default: the person starting the
    work said which path it should follow, and nothing inferred later should quietly override
    that. Recorded as `source: "chosen"` so the console can say so.

    Scoped to the project on purpose — a version id from another project must not be usable by
    passing it in the payload.
    """
    row = connection.execute(
        text(
            """
            SELECT d.id AS definition_id, d.workflow_key, d.name, v.id AS version_id, v.version
            FROM workflow_versions v
            JOIN workflow_definitions d ON d.id = v.workflow_definition_id
            WHERE v.id = :version_id AND d.project_id = :project_id
            """
        ),
        {"version_id": version_id, "project_id": project_id},
    ).mappings().first()
    if not row:
        return None
    return {
        "definition_id": row["definition_id"],
        "version_id": row["version_id"],
        "mapping_id": None,
        "workflow_key": row["workflow_key"],
        "name": row["name"],
        "version": row["version"],
        "source": "chosen",
    }


#: The work class a new project is. `start_project_run` resolves the project's workflow through
#: the same mapping the console's "apply" action writes, so what the operator chose in the
#: console is what a project actually runs.
PROJECT_WORK_CLASS = "project.new"


def start_project_run(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    title: str,
    directive: str,
    workflow_version_id: Optional[str] = None,
    extra_input: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Start the OUTER workflow for a project, and return the run.

    A project's life is research → panel → approve the spec → build → review → test → preview →
    deploy → monitor. Every one of those is a workflow step with a duration, a status and
    something it produced. Until now nothing started such a run for real work: the console
    created a code task directly, so the only thing anyone could watch was the coding
    pipeline's own internal phases — which are one step of the nine, and the step the operator
    cares least about.

    Returns None when the project has no workflow to run, rather than raising. A fleet that has
    not installed the catalogue must still be able to start work; it simply gets the old direct
    path and no outer run to watch.
    """
    resolved = None
    if workflow_version_id:
        resolved = workflow_by_version(connection, project_id, str(workflow_version_id))
    if resolved is None:
        resolved = workflow_for_code_task(connection, project_id=project_id, task_id=None)
    if not resolved or not resolved.get("version_id"):
        return None
    extra = {k: v for k, v in (extra_input or {}).items() if v not in (None, "", [], {})}
    run_input = {
        "title": title,
        # `directive` is what the catalogue's first steps read: research seeds from it and the
        # panel debates it. It is the instructions, not the title — a title is a label, and a
        # panel handed a label has nothing to discuss.
        "directive": directive,
        **{k: v for k, v in extra.items() if not k.startswith(ADOPT_PREFIX)},
    }
    run = start_run(
        connection,
        project_id=project_id,
        actor=actor,
        workflow_version_id=str(resolved["version_id"]),
        input_data=run_input,
        # The one place adoption is true by construction: the code task that creates this run
        # IS the build, and its origin names the panel and research it came from.
        adopted={k: v for k, v in extra.items() if k.startswith(ADOPT_PREFIX)},
        # One outer run per project. Starting the same project twice — a double-click, a retry
        # of the request — must not produce two nine-step pipelines racing each other over one
        # repository.
        dedupe_key=f"project:{project_id}:{title[:80]}:{directive[:120]}",
    )
    # CHECKED, NOT REFUSED. The build this run governs is already going, so refusing would only
    # leave it unwatched — but the run still ends in a deploy, and on a project with no host it
    # failed there with no warning while the console's door refused the same template. The
    # run now carries what is missing from its first second (workflow_readiness explains).
    if run and not run.get("duplicate"):
        from .workflow_readiness import note_unready_start

        try:
            note_unready_start(connection, None, project_id, actor, str(run.get("id") or ""),
                               version_id=str(resolved["version_id"]), input_data=run_input)
        except Exception:  # noqa: BLE001 — a warning that cannot be computed must not stop a build
            logger.warning("start_project_run: readiness could not be checked", exc_info=True)
    return run
