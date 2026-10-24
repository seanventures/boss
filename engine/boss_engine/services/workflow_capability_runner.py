"""Executes the capability steps of a customer's delivery workflow.

The runtime schedules steps, leases them and records their evidence; it has never executed
anything itself. Control steps it drains internally, and everything else has always been
claimed and completed by an outside caller. For customer workflows there was no such caller —
so this is it.

Three rules shape the whole file:

  * IT ONLY TOUCHES PRODUCT WORKFLOWS. BOSS's own thirty-three definitions are claimed by the
    machinery that has always claimed them. A runner that reached across the namespace could
    execute the governed coding pipeline twice.
  * IT DOES NOT REIMPLEMENT ANYTHING. Every capability dispatches to a tool BOSS already has,
    already governs by risk tier, and already runs from the chat agent and MCP. A second path
    to "deploy" is a second definition of what deploying means.
  * A GATE IS NOT WORK. A step that waits for a person is never executed here; it raises a
    decision and stays waiting, which is the runtime's existing mechanism.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import get_settings
from . import (agent_acts, change_record, deploy_repair, step_agents,
               self_fix, step_recovery, workflows)
from . import workflow_custom_steps as custom_steps
from .core import emit_event, now_utc
from .deploy_policy import promises_a_person
# Named import so "who approved this" is compared against one constant rather than a
# string literal repeated wherever the question is asked.
from .decisions import SYSTEM_ACTOR as DECISION_SYSTEM_ACTOR
from .run_decisions import open_step_decision
from . import release_scope, security_review_steps, workflow_capabilities, workflow_outcomes
from .workflow_capabilities import CAPABILITY_KEYS, get as get_capability

log = logging.getLogger(__name__)


def _knowledge_precedent(
    connection: Connection,
    project_id: str,
    state: Any,
    run_id: str,
    step_id: str,
    actor: str,
) -> str:
    """Quoted precedent for one decision. A lookup failure does not stop the decision."""
    query = state if isinstance(state, str) else ""
    if not query.strip():
        return ""
    try:
        from .knowledge.runtime import knowledge_mode

        if knowledge_mode(connection, project_id) != "on":     # off, or keyword search only
            return ""
        from .knowledge.scopes import policy_for
        from .knowledge.service import KnowledgeService

        service = KnowledgeService()
        found = service.precedent(connection, project_id=project_id, query=query[:500],
                                  policy=policy_for(stage="decide"))
        fact_ids = [str(item) for item in (found.get("cited_fact_ids") or [])]
        if fact_ids:
            service.log_pack(
                connection,
                project_id=project_id,
                run_id=run_id,
                step=step_id,
                fact_ids=fact_ids,
            )
        return str(found.get("section") or "")
    except Exception as error:
        try:
            emit_event(
                connection,
                project_id,
                "knowledge.precedent_failed",
                actor or "system",
                "workflow_run",
                run_id,
                {"error": type(error).__name__},
            )
        except Exception:
            return ""
        return ""

#: Long enough for a tool call that talks to GitHub or a sandbox, short enough that a runner
#: which dies does not strand the step for long. The runtime reclaims an expired lease.
LEASE_SECONDS = 120

#: The longest lease a step that works inside its call may hold (a crashed runner strands it
#: no longer than this).
MAX_INLINE_LEASE_SECONDS = 900


def lease_for(spec_step: dict[str, Any]) -> int:
    """How long a claimed step's lease must last: LEASE_SECONDS, or three times the typical
    minutes of a capability that thinks with a model inside the call (not one that starts work
    and parks on it), capped at MAX_INLINE_LEASE_SECONDS."""
    key = str(((spec_step or {}).get("binding") or {}).get("capability") or "")
    cap = workflow_capabilities.CAPABILITIES.get(key)
    if not cap or cap.is_async or not cap.needs_model or cap.tool:
        return LEASE_SECONDS
    return max(LEASE_SECONDS, min(MAX_INLINE_LEASE_SECONDS, int(cap.typical_minutes or 0) * 180))


#: Per tick. The point is steady progress across many runs, not draining one to completion
#: while the others wait.
MAX_STEPS_PER_TICK = 10


class CapabilityExecutionError(RuntimeError):
    """The capability could not be performed. Carries the reason the step will record."""


#: Set on a RETURNED failure that would fail the same way again — an agent step whose agent
#: declared it failed or was blocked, or did not produce what the next step needs. The runner
#: stops the run at once for Retry / Skip instead of spending automatic retries on it.
SETTLED_FAILURE = "settled_failure"


class SettledCapabilityError(CapabilityExecutionError):
    """The capability failed on a FACT that cannot change, so retrying is pointless.

    A tool call can fail because a network blipped; running it again is the right response. A
    recorded quality gate is not like that: `security_clean did not pass` is a verdict already
    written down about a build that has finished. Retrying re-reads the same row and reaches the
    same conclusion.

    Measured on wfr_c8578ea1abbf4a1383cc: the review step failed, retried twice more on a
    schedule, and deadlettered 3m47s later with the identical message it had at 0s — three
    ticks and four minutes to learn nothing.
    """


class CapabilityWaitError(CapabilityExecutionError):
    """The capability could not start YET, for a reason that clears by itself.

    The per-minute budget is the case: five runs started within seconds of each other spend
    the minute's allowance, and the sixth reservation is refused until the window moves on.
    That is not a failure, not an attempt, and nothing a person has to decide — the step goes
    back to wait `wait_seconds`, the run says what it is waiting for, and the next tick after
    that tries again (`workflows.wait_step`). It used to fail the step, which with one attempt
    dead-lettered the run at its first step.
    """

    def __init__(self, message: str, *, wait_seconds: int = 30, kind: str = "budget_wait"):
        super().__init__(message)
        self.wait_seconds = int(wait_seconds)
        self.kind = kind


def _as_wait(error: CapabilityExecutionError) -> Optional[CapabilityWaitError]:
    """The wait a capability failure really is, or None.

    Raised explicitly by the paths that know (a custom step's skill, `CapabilityWaitError`),
    and recognised here for any other capability that could not start for a reason that clears
    by itself — the per-minute cap, a model source whose every lease is held by other work, a
    secret store that did not answer — read by the one classifier every surface uses
    (model_failures.waiting_for), so a wait waits wherever in the runner it surfaces."""
    if isinstance(error, CapabilityWaitError):
        return error
    if isinstance(error, SettledCapabilityError):
        return None
    from .model_failures import waiting_for

    said = waiting_for(str(error))
    if said is None:
        return None
    return CapabilityWaitError(said["headline"], wait_seconds=30, kind=said["kind"])


def _as_settled(error: CapabilityExecutionError) -> CapabilityExecutionError:
    """The error as it should be FAILED: settled when only a person can lift it.

    The other half of `_as_wait`, read by the same classifier. A budget refusal a person must
    lift — the cap, a tier-2 freeze, a used-up allocation — reaches the runner from more than
    one door: a custom step says so itself (raise_for_budget), but a typed decision's
    reservation, or a model gateway's `ModelCapacityDeniedError('Budget cap exceeded. Decision
    created.')`, arrived as a plain retryable error. fail_step then retried it with no delay up
    to max_attempts, each retry reserving again and opening ANOTHER blocking "Budget
    reservation denied" decision, and the step dead-lettered instead of stopping once with
    what to do. Classified here, once, every kind of step stops the same way."""
    if isinstance(error, SettledCapabilityError):
        return error
    from .model_failures import explain

    said = explain(str(error))
    if said.get("kind") != "budget_refused":
        return error
    detail = str(error).strip().rstrip(".")
    return SettledCapabilityError(
        f"{said['headline'].rstrip('.')}: {detail}. {said['action']}")


#: Where `_resolve_inputs` records the explicit marker for each optional input whose source step
#: was skipped. Kept on the step's CONTEXT, not among its inputs, so no tool ever receives it as
#: an argument; `execute_step` writes it onto the step's output as `skipped_inputs`.
SKIPPED_INPUTS = "skipped_inputs"

#: On a failed step's output: the approval this step depended on was REFUSED by a person (a
#: stack deployment's decision is its deploy step's gate). Such a step is an approval for the
#: purposes of Skip — see `workflow_run_actions.is_refused_approval`.
REJECTED_BY_APPROVER = "rejected_by_approver"

#: On a deploy step's binding snapshot: the stack target the step's gate was waived for,
#: because that stack deployment's own decision is the approval. Execution may ship there and
#: nowhere else without an approval.
STACK_GATE_TARGET = "stack_gate_target_id"
#: The waiver's target when it is a declared app's FIRST deploy: the target is made by the step
#: (stack_deploy._first_target), so the gate names no id — but execution must still ship
#: through a stack deployment, never fall to the ungated per-app path.
FIRST_STACK_TARGET = "first-stack-target"


def item_context(claimed: dict[str, Any]) -> dict[str, Any]:
    """The item a for_each handed this step run, as `_resolve_inputs` reads it — or nothing.

    A for_each starts its body once per item and writes the item on that step run's `input`
    (`workflows._activate_for_each_item`); a step that is not a for_each's body gets {}.
    """
    given = claimed.get("input") if isinstance(claimed, dict) else None
    if not isinstance(given, dict) or "for_each" not in given:
        return {}
    return {"item": given.get("item"), "item_index": given.get("item_index"),
            "item_count": given.get("item_count"), "for_each": given.get("for_each")}


def _resolve_inputs(
    inputs: dict[str, Any], context: dict[str, Any],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Substitute references to earlier steps' outputs.

    `{"run_id": {"from_step": "build", "field": "code_task_run_id"}}` becomes the id that the
    build step actually produced. Without this a workflow could only ever pass literals, and
    "deploy the thing you just built" — the most ordinary sentence in delivery — would be
    impossible to express.
    """
    resolved: dict[str, Any] = {}
    for name, value in (inputs or {}).items():
        if isinstance(value, dict) and ("from_item" in value or "from_item_index" in value):
            # THE ITEM A FOR_EACH IS ON. `{"from_item": ""}` is the whole item, a path reads into
            # it (`"url"`, `"tags.0"`), and `{"from_item_index": true}` is its 0-based index.
            if "for_each" not in context:
                raise CapabilityExecutionError(
                    f"'{name}' reads a for_each item, and this step was not started by one.")
            if "from_item_index" in value:
                resolved[name] = context.get("item_index")
                continue
            found, item = workflows._lookup_path(context.get("item"),
                                                 str(value.get("from_item") or ""))
            if not found:
                if name in optional:
                    continue
                raise CapabilityExecutionError(
                    f"item {int(context.get('item_index') or 0) + 1} has no "
                    f"'{value.get('from_item')}', which this step needs as '{name}'.")
            resolved[name] = item
        elif isinstance(value, dict) and "from_input" in value:
            # What the run was STARTED with. A catalogue workflow cannot hard-code the thing
            # being built — "Standard Product Delivery" has to work for any directive — so its
            # first steps read from the run's input instead of a literal.
            field = str(value["from_input"])
            run_input = context.get("input") or {}
            supplied = run_input.get(field)
            if field not in run_input or (supplied in (None, "") and "default" in value):
                if "default" in value:
                    resolved[name] = value["default"]
                    continue
                # AN OPTIONAL INPUT THAT WAS NOT SUPPLIED IS SIMPLY ABSENT.
                #
                # Same rule `from_step` has had all along, and it was missing here: any
                # optional binding read from the run's input made that input MANDATORY, so
                # binding something a wizard collects — a chosen repository, acceptance
                # criteria — would have broken every run started from anywhere else. A
                # workflow must accept extra information without requiring it.
                if name in optional:
                    continue
                raise CapabilityExecutionError(
                    f"this workflow was started without '{field}', which this step needs."
                )
            resolved[name] = supplied
        elif isinstance(value, dict) and "from_step" in value:
            override_field = str(value.get("override_from_input") or "")
            if override_field:
                supplied = (context.get("input") or {}).get(override_field)
                if isinstance(supplied, str) and supplied.strip():
                    resolved[name] = supplied.strip()
                    continue
            step_id = str(value["from_step"])
            field = str(value.get("field") or "")
            skipped = (context.get("skipped") or {}).get(step_id)
            if skipped is not None:
                # THE STEP THIS READS FROM WAS SKIPPED, AND THAT IS WHAT THIS STEP IS TOLD.
                #
                # Not a value, and not a silence that looks like one. An operator pressed
                # "Skip and proceed" on a failed step (see `workflows.skip_failed_step`), so it
                # produced nothing — and whatever it half-produced before failing is not
                # evidence anybody vouched for. Before this existed, research that died had its
                # partial dossier forwarded to the panel as though it were the research
                # (boss-1, 2026-10-02).
                #
                # Optional: left out of the inputs, and named in `skipped_inputs` on this step's
                # output so the record says the panel argued without research and why.
                # Required: this step cannot do its work, and it says exactly why — which stops
                # the run here for the operator to decide again, rather than inventing input.
                if name in optional:
                    marker = {"skipped": True, "from_step": step_id, "field": field or None,
                              "reason": skipped.get("reason"),
                              "skipped_by": skipped.get("skipped_by")}
                    context.setdefault(SKIPPED_INPUTS, {})[name] = marker
                    continue
                # SETTLED, NOT RETRIED. The skip is a recorded fact and will not change between
                # attempts; retrying spent ticks and minutes to reach the same sentence, which is
                # what SettledCapabilityError exists to prevent (wfr_c8578ea1abbf4a1383cc).
                raise SettledCapabilityError(
                    f"step '{step_id}' was skipped"
                    + (f" by {skipped.get('skipped_by')}" if skipped.get("skipped_by") else "")
                    + (f" ({skipped.get('reason')})" if skipped.get("reason") else "")
                    + f", so it produced no '{field or step_id}', which this step needs. "
                      f"Retry '{step_id}', or skip this step too."
                )
            produced = (context.get("steps") or {}).get(step_id) or {}
            if field:
                if field not in produced and name in optional:
                    # AN OPTIONAL REFERENCE THAT CANNOT RESOLVE IS SIMPLY ABSENT.
                    #
                    # `meeting` reads the research dossier, and a run whose research was
                    # skipped — a project started directly, with no research phase — produces
                    # none. Failing there would make the binding that IMPROVES a panel the
                    # thing that stops it happening at all.
                    continue
                if field not in produced:
                    raise CapabilityExecutionError(
                        f"step '{step_id}' did not produce '{field}', which this step needs."
                    )
                resolved[name] = produced[field]
            else:
                resolved[name] = produced
        else:
            resolved[name] = value
    return resolved


def _run_context(connection: Connection, run_id: str) -> dict[str, Any]:
    """What earlier steps in this run produced, keyed by step id."""
    # WHAT WAS PRODUCED, AND WHAT AN OPERATOR SKIPPED — one pass, the LATEST row per step.
    #
    # A skipped step's output still holds whatever its failed attempt recorded; putting it in
    # `steps` would hand that to the next step as though it were a result, so `_resolve_inputs`
    # turns a binding to a skipped step into an explicit marker instead. Two rules keep that
    # marker honest (review of c8de36b):
    #
    # * ONLY AN OPERATOR'S SKIP. A branch the run never took (`condition_not_selected`,
    #   `path_not_reached`) is not "skipped" in that sense — nobody decided anything about it,
    #   and a step reading from it was failed with "Retry x" for an x that never failed.
    # * THE LATEST ATTEMPT WINS. A loop body skipped in iteration N and succeeding in N+1 has
    #   produced a value; a later step must not be told it was skipped. Ordered as
    #   `workflow_run_actions.latest_attempts` orders them: created, then iteration.
    rows = connection.execute(
        text(
            """
            SELECT step_id, status, output FROM workflow_step_runs
            WHERE workflow_run_id = :run_id AND status IN ('succeeded', 'skipped')
            ORDER BY created_at, iteration, completed_at
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    steps: dict[str, Any] = {}
    skipped: dict[str, Any] = {}
    for row in rows:
        output = row["output"] if isinstance(row["output"], dict) else {}
        if row["status"] == "succeeded":
            if isinstance(row["output"], dict):
                steps[row["step_id"]] = output
            skipped.pop(row["step_id"], None)
        elif workflows.skipped_by_operator(row):
            steps.pop(row["step_id"], None)
            skipped[row["step_id"]] = {
                "reason": output.get("skip_reason") or output.get("reason"),
                "skipped_by": output.get("skipped_by"),
            }
    # WHAT THIS RUN IS FOR, not just what it was asked to do.
    #
    # A step knew its own inputs and nothing about the workflow it was part of, so every panel
    # in all twelve catalogue workflows convened the same generic PRD meeting. Research-Led
    # Discovery — whose own description is "ends in a decision and a document, not in code" —
    # asked its panel for an implementation plan, exactly as Standard Product Delivery did.
    # The objective is written on the workflow and travels with its spec; it costs one join.
    row = connection.execute(
        text("SELECT r.input, v.spec, d.name AS workflow_name "
             "FROM workflow_runs r "
             "JOIN workflow_versions v ON v.id = r.workflow_version_id "
             "JOIN workflow_definitions d ON d.id = v.workflow_definition_id "
             "WHERE r.id = :r"),
        {"r": run_id},
    ).mappings().first()
    run_input = (row or {}).get("input")
    spec = (row or {}).get("spec")
    spec = spec if isinstance(spec, dict) else {}
    # run_id and step count travel with the context because a custom step's MCP call needs
    # them to build its effect-receipt dedupe key — the thing that stops a retried step from
    # sending the same message twice.
    return {
        "steps": steps,
        "skipped": skipped,
        "input": run_input if isinstance(run_input, dict) else {},
        "run_id": run_id,
        "step_index": len(rows),
        "objective": str(spec.get("objective") or ""),
        "workflow_name": str(spec.get("workflow_name") or (row or {}).get("workflow_name") or ""),
        # Whether this run ends in a deploy: the build is then told to write the app's
        # deploy manifest itself (DEPLOY_MANIFEST_BRIEF), so nobody writes one by hand.
        "deploys": any(isinstance(s, dict) and (s.get("binding") or {}).get("capability") == "deploy"
                       for s in (spec.get("steps") or [])),
    }


#: Said to a build whose workflow deploys it. The app's own manifest is how BOSS deploys it
#: without anybody filling a "manifest path" or a start command in by hand.
#: The words that open DEPLOY_MANIFEST_BRIEF: a build whose brief holds them was asked for its
#: own manifest (stack_deploy.undeclared_deploy_refusal).
DEPLOY_BRIEF_MARKER = "THIS APP WILL BE DEPLOYED"
#: The words that open deploy_repair.host_brief: what the deploy's machine has.
DEPLOY_HOST_MARKER = "THE MACHINE THIS APP DEPLOYS TO"

DEPLOY_MANIFEST_BRIEF = (
    f"{DEPLOY_BRIEF_MARKER} by BOSS after review. Write a boss.deploy.yaml IN THE APP'S OWN "
    "FOLDER — next to its boss.preview.command; the repository root only for an app that lives "
    "at the root. A boss.deploy.yaml already at the repository root belongs to another app: "
    "never edit it, and do not count it as this app's. It says how the app runs, so nobody fills "
    "it in by hand:\n"
    "  version: 1\n  name: <the app's short name, lower-case-with-dashes>\n  kind: commands\n"
    "  install: [<the commands that install dependencies and start the app in the background on "
    "port $PORT>]\n  health: [{url: \"{target.public_url}\", expect: 200}]\n"
    "  open: {url: \"{target.public_url}\"}\n"
    "Never set PORT in it: BOSS passes the target's. Health-check a route the app really serves. "
    "IF THE APP USES POSTGRES, the manifest brings it: `resources: {db: {capability: "
    "database.relational, engine: postgres}}` (BOSS provisions it and passes DATABASE_URL), or "
    "kind: compose with a postgres service in its compose file. A deploy of a Postgres app whose "
    "manifest declares no database is refused before anyone is asked to approve it. "
    "A demo/seed password BOSS provides (DEMO_PASSWORD) is allowed in production: do not refuse "
    "it; make demo seeding an explicit opt-in flag (e.g. ALLOW_DEMO_SEED) instead. "
    "Use compose (kind: compose, compose: {file: docker-compose.yml}) when the app already runs "
    "with docker compose. Keep it in the same pull request.")


def _tool_registry():
    """The same registry the chat agent and MCP use. Imported lazily so this module can be
    read (and unit-tested) without constructing the whole tool layer."""
    from ..tools.builtin import build_default_registry

    return build_default_registry()


#: True inside `_step_authority`: `_invoke_tool` refuses a step's tool call made outside it.
_IN_STEP_AUTHORITY: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "boss_in_step_authority", default=False)


@contextlib.contextmanager
def _step_authority(ctx: Any, connection: Connection, project_id: str, context: Optional[dict[str, Any]],
                    step: Optional[dict[str, Any]], role_key: Optional[str], scope: Optional[frozenset[str]] = None):
    """Enter the run's recorded authority for a step's work (`_run_authority`), and mark that
    the work is under it: `_invoke_tool` refuses a step's tool call made anywhere else."""
    with _run_authority(ctx, connection, project_id, context, step, role_key, scope) as decided:
        marker = _IN_STEP_AUTHORITY.set(True)
        try:
            yield decided
        finally:
            _IN_STEP_AUTHORITY.reset(marker)


def _run_authority(ctx: Any, connection: Connection, project_id: str, context: Optional[dict[str, Any]],
                   step: Optional[dict[str, Any]], role_key: Optional[str], scope: Optional[frozenset[str]] = None):
    """In-process step work under the workflow run's RECORDED authority (IDENTITY.md §7.14), not
    under whoever called in to advance the run (the worker). With an agent Passport for the step,
    a delegated run token (`run_wf:<run>/step:<step>`: person ∩ ceilings ∩ grant ∩ task); with
    none, the person's own live principal. A run with no recorded authority runs as before."""
    import contextlib
    import re

    from boss_engine.identity import catalogue as _catalogue
    from boss_engine.identity import consent as _consent
    from boss_engine.identity import runs as _runs

    run_id = str((context or {}).get("run_id") or "")
    recorded = _consent.acting_for_workflow_run(connection, run_id) if run_id else None
    if recorded is None:
        if run_id and _consent.recorded_authority(connection, run_id) is not None:
            # The run HAS an authority and it is no longer live (grant revoked/expired, person
            # gone): fail closed rather than fall back to whoever is advancing the run.
            raise CapabilityExecutionError(
                "the authority this run was started under has ended; renew it to continue.")
        return contextlib.nullcontext()
    engine = getattr(ctx, "engine", None) or connection.engine
    step_id = re.sub(r"[^A-Za-z0-9_.-]", "", str((step or {}).get("id") or "step"))[:60] or "step"

    from boss_engine.identity.tokens import TokenError

    @contextlib.contextmanager
    def under() -> Any:
        with contextlib.ExitStack() as stack:
            try:
                decided = stack.enter_context(_runs.delegated(
                    engine, kind="step", entity_id=step_id, project_id=project_id, role_key=role_key,
                    scope=scope or _catalogue.delegable(), acting=recorded))
                if decided is None:
                    decided = stack.enter_context(_runs.on_behalf(engine, recorded))
            except TokenError as lost:
                # Fail closed: the person or grant this run acts for is no longer live.
                raise CapabilityExecutionError(
                    f"this run's authority has ended ({lost.reason}); renew it to continue.") from lost
            yield decided

    return under()


def _invoke_tool(
    ctx: Any, tool_name: str, project_id: str, actor: str, args: dict[str, Any]
) -> dict[str, Any]:
    if tool_name in {"query_db", "write_db"}:
        raise CapabilityExecutionError(
            "raw SQL is unavailable in project-scoped workflow steps"
        )
    if not _IN_STEP_AUTHORITY.get():
        # A step's tool call made bare runs as whoever advanced the run (the worker) and is
        # refused under enforce. Every dispatch enters `_step_authority`; one that does not is
        # a bug in BOSS, said once, not retried.
        raise SettledCapabilityError(
            f"'{tool_name}' was called outside this run's authority; BOSS refused to run it.")
    spec, refused = _authorize_step_tool(ctx, tool_name, project_id, args)
    if refused is not None:
        return refused
    result = spec.handler(ctx, project_id=project_id, actor=actor, args=args)
    if isinstance(result, dict) and result.get("ok") is False:
        raise CapabilityExecutionError(str(result.get("error") or f"{tool_name} failed."))
    return result if isinstance(result, dict) else {"result": result}


def _authorize_step_tool(
    ctx: Any, tool_name: str, project_id: str, args: dict[str, Any]
) -> tuple[Any, Optional[dict[str, Any]]]:
    """The checks a step's tool call passes before its handler runs: (spec, refusal-or-None).
    Shared by `_invoke_tool` and the resumed build (`_retried_build`), so a retried build is
    decided exactly as the step's own `create_code_task` would be."""
    registry = _tool_registry()
    spec = {s.name: s for s in registry.specs()}.get(tool_name)
    if spec is None:
        raise CapabilityExecutionError(
            f"this BOSS does not have the '{tool_name}' tool installed."
        )
    # IDENTITY (S2): a workflow step calls the handler directly, so the dispatch check that
    # registry.invoke runs must run here too, or a workflow would be a way around it.
    from ..tools.registry import CURRENT_TOOL_AUTH, identity_gate

    refused = identity_gate(spec, CURRENT_TOOL_AUTH.get(), project_id, args)
    if refused is not None:
        return spec, refused
    # Inside a delegated run, the step's tool is decided against the run token (IDENTITY.md §7.4);
    # this path calls the handler directly, so the check cannot be left to the registry.
    from boss_engine.identity import lineage as _identity_lineage
    from boss_engine.identity import runs as _identity_runs

    _delegated = _identity_lineage.current_context()
    if _delegated is not None and _delegated.run_token is not None and getattr(ctx, "engine", None) is not None:
        with ctx.engine.begin() as _db:
            try:
                _identity_runs.authorize_tool(getattr(spec, "permission", None), project_id, db=_db)
            except _identity_runs.ToolRefused as refused:
                # SETTLED: an identity refusal is the same on every retry; only a person's
                # grant changes it.
                raise SettledCapabilityError(
                    f"'{tool_name}' was refused for this agent: {refused.permission} ({refused.reason})"
                ) from refused
    return spec, None


#: Work a run was started WITH, which the matching step should wait on rather than start again.
_ADOPTABLE: dict[str, tuple[str, str]] = {
    "build": ("adopt_code_task_run_id", "code_task"),
    # Work that began with research waits on THAT research rather than commissioning more.
    "research": ("adopt_discovery_run_id", "discovery"),
    # A project created from a PRD panel already HAD its meeting — that panel is why the
    # project exists. Convening another one debates a question that was settled.
    "meeting": ("adopt_meeting_run_id", "meeting"),
}


def _adopted_handle(key: str, context: dict[str, Any]) -> Optional[dict[str, Any]]:
    spec = _ADOPTABLE.get(key)
    if not spec:
        return None
    field, kind = spec
    value = (context.get("input") or {}).get(field)
    if isinstance(value, str) and value.strip():
        return {"kind": kind, "id": value.strip(), "adopted": True}
    return None


#: A gate the run was started with an ANSWER to, and where the run input carries it.
#:
#: The panel path approves the spec BEFORE the project exists: BOSS convenes a panel, drafts a
#: PRD, a person approves it, and only then is the code task created — which is what starts this
#: workflow. Opening a `spec_review` gate at that point asks the same person the same question a
#: second time, about a build already running.
_ADOPTABLE_GATES: dict[str, str] = {"spec_review": "adopt_decision_id"}

#: An answered decision, in the several shapes "answered" takes. `rejected` is deliberately not
#: here: a declined spec is not an approved one, and the gate should be asked again.
_GATE_ANSWERED = frozenset({"approved", "executed", "completed"})


def _decision_from_the_panel(
    connection: Connection, project_id: str, context: dict[str, Any]
) -> Optional[str]:
    """The PRD approval the meeting step's panel already raised, if it has.

    When the workflow DRIVES — Build starts the run, and its `meeting` step convenes the panel —
    the panel produces a PRD and the decision to approve it. `spec_review` raising a second,
    generic "approve the spec" would ask the same person about the same document twice, one
    dialog after the other. The panel's own decision IS this gate.
    """
    task_id = None
    for produced in (context.get("steps") or {}).values():
        if not isinstance(produced, dict):
            continue
        meeting = produced.get("meeting")
        if isinstance(meeting, dict):
            task_id = meeting.get("task_id") or task_id
        task_id = produced.get("task_id") or task_id
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    row = connection.execute(
        text("SELECT id FROM decisions WHERE project_id = :p AND task_id = :t "
             "ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "t": task_id.strip()},
    ).mappings().first()
    return row["id"] if row else None


def _adopted_gate(
    connection: Connection, key: str, context: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """The approval this run already has for `key`, or None to ask for one.

    The decision is READ, never trusted from the run input alone — the input carries an id, and
    an id is not evidence. A run started with the id of a decision that was rejected, expired or
    never answered gets its gate opened as normal.
    """
    field = _ADOPTABLE_GATES.get(key)
    if not field:
        return None
    value = (context.get("input") or {}).get(field)
    if not isinstance(value, str) or not value.strip():
        return None
    row = connection.execute(
        text("SELECT status, approved_by, approved_at FROM decisions WHERE id = :id"),
        {"id": value.strip()},
    ).mappings().first()
    if row is None or str(row["status"] or "") not in _GATE_ANSWERED:
        return None
    approved_at = row["approved_at"]
    return {
        "decision_id": value.strip(),
        "decision_status": str(row["status"]),
        "approved_by": row["approved_by"],
        "approved_at": approved_at.isoformat() if hasattr(approved_at, "isoformat")
                       else approved_at,
    }


def _no_prd_to_approve(key: str, context: Optional[dict[str, Any]]) -> bool:
    """A build started as a DIRECT CHANGE has no PRD, so its run's `spec_review` has nothing to
    approve (factory test D2). It opened a "PRD approval (automatic)" decision that nobody — and
    no automation — ever answered: one pending decision per build, counted on the pill and in
    Approvals while the build had long opened its PR. A run that came from a panel (a meeting
    or a decision to adopt) still waits on that panel's PRD exactly as before."""
    if key != "spec_review" or not isinstance(context, dict):
        return False
    run_input = context.get("input") or {}
    if not isinstance(run_input, dict) or not run_input.get("adopt_code_task_run_id"):
        return False
    if run_input.get("adopt_decision_id") or run_input.get("adopt_meeting_run_id"):
        return False
    # The setting follows identity enforcement unless the operator explicitly overrides it.
    # Missing/unreadable policy cannot grant permission to bypass a person's approval.
    try:
        return getattr(get_settings(), "meeting_prd_auto_approve", False) is True
    except Exception:  # noqa: BLE001
        log.warning("PRD auto-approval settings unavailable; retaining manual spec review")
        return False


#: Phases that HAPPEN BEFORE a build. On a retrospective run — one started for a project whose
#: build is already going — running these forwards spends money re-deciding a settled question.
_RETROSPECTIVE_KINDS = frozenset({"research", "meeting"})


def _fill_optional_from_run_input(
    capability: Any, inputs: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Let the run's own input satisfy optional inputs the step did not bind.

    A catalogue workflow's `build` step binds only title and instructions, because those are the
    two things every directive has. But a project started from the New project form also carries
    acceptance criteria, test commands and a repository — real instructions that simply had
    nowhere to go, so the build ran without the tests it was supposed to pass.

    Only OPTIONAL inputs, and only ones the step did not bind itself: an explicit binding is a
    decision by whoever wrote the workflow and must not be second-guessed by whatever happens to
    be in the run input. Required inputs are deliberately excluded — a workflow that forgot one
    should fail saying so, not quietly pick up something with a matching name.
    """
    run_input = context.get("input") or {}
    if not isinstance(run_input, dict) or not run_input:
        return inputs
    filled = dict(inputs)
    # A binding whose source was SKIPPED stays empty. Back-filling it from a same-named run input
    # would replace the explicit "skipped" marker with a value the workflow never bound.
    skipped = context.get(SKIPPED_INPUTS) or {}
    for name in getattr(capability, "optional_inputs", ()) or ():
        if name in filled or name in skipped:
            continue
        value = run_input.get(name)
        if value not in (None, "", [], {}):
            filled[name] = value
    return filled


def deploy_step_inputs(connection: Connection, spec_step: dict[str, Any],
                       context: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """A deploy step's inputs as it will run with them, resolved before it runs; None when they
    cannot be told yet."""
    binding = spec_step.get("binding") or {}
    if str(binding.get("capability") or "") != "deploy" or context is None:
        return None
    point = connection.begin_nested()
    try:
        capability = get_capability("deploy")
        scratch = dict(context)
        inputs = _resolve_inputs(binding.get("inputs") or {}, scratch,
                                 optional=frozenset(capability.optional_inputs or ()))
        inputs = _fill_optional_from_run_input(capability, inputs, scratch)
    except Exception:  # noqa: BLE001 — unresolvable now
        point.rollback()
        return None
    point.commit()
    return inputs


def stack_target_for_step(
    connection: Connection,
    project_id: str,
    spec_step: dict[str, Any],
    context: Optional[dict[str, Any]],
    *,
    default_owner: str = "",
) -> Optional[dict[str, Any]]:
    """The stack target this deploy step ships to, or None when it takes the per-app path.

    Asked BEFORE the step runs — by the gate, and by the console — with the same inputs the
    step will run with, through the same `stack_target_for_deploy` the step itself calls, so
    "this is a stack deploy" can never be answered one way at the gate and another at the run.
    An input that cannot be resolved yet (the build has not finished) is simply "not known to
    be a stack deploy": the step is gated as it always was.
    """
    binding = spec_step.get("binding") or {}
    if str(binding.get("capability") or "") != "deploy" or context is None:
        return None
    point = connection.begin_nested()
    try:
        capability = get_capability("deploy")
        scratch = dict(context)
        inputs = _resolve_inputs(binding.get("inputs") or {}, scratch,
                                 optional=frozenset(capability.optional_inputs or ()))
        inputs = _fill_optional_from_run_input(capability, inputs, scratch)
        from .stack_deploy import stack_target_for_deploy

        target = stack_target_for_deploy(connection, project_id, inputs,
                                         default_owner=default_owner)
    except Exception:  # noqa: BLE001 — unresolvable now means "not known to be a stack deploy"
        point.rollback()
        return None
    point.commit()
    return target


def stack_deploy_for_step(
    connection: Connection,
    project_id: str,
    *,
    run_id: str,
    spec_step: dict[str, Any],
    status: str,
    output: Any,
    default_owner: str = "",
) -> Optional[dict[str, Any]]:
    """What the console shows on a deploy step that ships to a stack target, or None.

    `deployment` is the stack deployment the step planned — the same record, with the same
    engine-decided `actions`, that Infra renders — so the step's Approve/Reject are that
    deployment's decision and nothing else. Before the step has planned one, the target it
    will ship to is named, so the step never offers the per-app "Set target" form.
    """
    from .stack_deploy import get_stack_deployment

    out = output if isinstance(output, dict) else {}
    handle = out.get("await") if isinstance(out.get("await"), dict) else {}
    reached = out.get("reached_before_failing")
    reached = reached if isinstance(reached, dict) else {}
    deployment_id = str(
        out.get("stack_deployment_id")
        or (handle.get("id") if handle.get("kind") == "stack_deployment" else "")
        or reached.get("stack_deployment_id") or "")
    deployment = None
    target_id = ""
    target: Optional[dict[str, Any]] = None
    if deployment_id:
        point = connection.begin_nested()
        try:
            deployment = get_stack_deployment(connection, deployment_id)
        except Exception:  # noqa: BLE001 — a page must render without it
            point.rollback()
            deployment = None
        else:
            point.commit()
        if deployment and deployment.get("project_id") != project_id:
            deployment = None
        target_id = str((deployment or {}).get("target_id") or "")
    if not deployment:
        if status in ("succeeded", "failed", "skipped", "cancelled") or not spec_step:
            return None
        target = stack_target_for_step(connection, project_id, spec_step,
                                       _run_context(connection, run_id),
                                       default_owner=default_owner)
        if not target:
            return None
        target_id = str(target.get("id") or "")
    row = connection.execute(
        text("SELECT t.id, t.app_name, t.environment, h.name AS host_name, "
             "       h.address AS host_address "
             "FROM deployment_targets t LEFT JOIN deployment_hosts h ON h.id = t.host_id "
             "WHERE t.id = :t AND t.project_id = :p"),
        {"t": target_id, "p": project_id},
    ).mappings().first() if target_id else None
    return {
        "stack_deployment_id": (deployment or {}).get("id"),
        "target_id": target_id or None,
        # A first deploy's target is named before it is made (stack_deploy._first_target).
        "target_name": (row or {}).get("app_name") or (target or {}).get("app_name"),
        "environment": (row or {}).get("environment") or (target or {}).get("environment"),
        "host": (row or {}).get("host_name"),
        "host_address": (row or {}).get("host_address"),
        "deployment": deployment,
    }


def _github_owner(ctx: Any) -> str:
    return str(getattr(getattr(ctx, "settings", None), "target_github_owner", "") or "")


#: Which of the build's quality gates each observing step speaks for. A step that reported
#: "succeeded" while the gate it stands for had failed would be the same lie as a `research`
#: step that succeeded in 0 seconds.
_OBSERVED_GATES: dict[str, tuple[str, ...]] = {
    "review": ("code_review_passed", "security_clean", "consistent"),
    "test": ("tests_strong_enough", "nothing_broke"),
}


def _this_runs_app(connection: Connection, context: dict[str, Any]) -> Optional[str]:
    """The app THIS workflow run is about, as an identity key, or None.

    A run is about one app. Read from the deploy step first — `applied.app` is the thing this
    run actually shipped, which is the strongest claim available — then from the code task's own
    app name. Compared through `identity_key` because the two records spell it differently:
    the deploy says `meeting-cost-meter` and the target says `Meeting cost meter`.
    """
    from .naming import identity_key, project_name

    for produced in (context.get("steps") or {}).values():
        if not isinstance(produced, dict):
            continue
        applied = ((produced.get("detail") or {}).get("applied") or {})
        name = applied.get("app") if isinstance(applied, dict) else None
        if isinstance(name, str) and name.strip():
            return identity_key(name)

    run_id = _build_run_id(context)
    if not run_id:
        return None
    try:
        row = connection.execute(
            text("SELECT title FROM code_task_runs WHERE id = :r"), {"r": run_id},
        ).mappings().first()
    except Exception:  # noqa: BLE001 — an unscoped step is better than a failed one
        return None
    if not row:
        return None
    display, _blurb, _label = project_name(str(row["title"] or ""))
    return identity_key(display) if display and display != "Untitled app" else None


def _reason_of_last_resort(output: dict[str, Any], step: dict[str, Any]) -> str:
    """What to say when a capability failed and offered no explanation.

    Never "without a reason". That sentence is the absence of an error message dressed as one:
    it names nothing to look at, nothing to try, and no reason to believe the system knows what
    happened — which, in the case that produced it, it did not.
    """
    capability = str(output.get("capability") or step.get("step_id") or "the step")
    tool = output.get("tool")

    # IF IT SAID WHY, THAT IS THE MESSAGE — DO NOT ANNOUNCE ITS ABSENCE OVER THE TOP OF IT.
    #
    # An operator was shown, in full: "deploy failed and gave no error message. What it did
    # return: reason=3 active 'staging' targets share repo 'boss-sandbox'... — deploying the
    # wrong app is worse than not d". Every clause of that is wrong together: there WAS an error
    # message, it was announced as missing, it was relabelled "what it did return" as though it
    # were debris, and it was cut off mid-word. The operator then had to ask BOSS what their own
    # error meant.
    for key in ("reason", "detail", "message", "note"):
        spoken = str((output or {}).get(key) or "").strip()
        if spoken:
            return spoken

    # Anything the capability did say, even under a key nobody planned for. A returned dict is
    # evidence and belongs in front of the operator, not summarised into silence.
    said = {
        k: v for k, v in (output or {}).items()
        if k not in ("capability", "tool", "status", "inputs") and v not in (None, "", [], {})
    }
    if said:
        # Whole values. The 160-character cut landed inside a word ("worse than not d") and the
        # operator pasted the stump into chat asking what it meant, because that is all they had.
        rendered = "; ".join(f"{k}={v}" for k, v in list(said.items())[:6])
        return (f"{capability} failed without an error message. It returned: {rendered}. "
                f"Read the activity log for this run, or ask BOSS about this step.")

    return (
        f"{capability} failed and returned nothing at all"
        + (f" (tool: {tool})" if tool else "")
        + ". There is no error text because the capability produced none — that is a defect in "
          "the capability, not a description of your app. Next: read_logs(service='worker') "
          "around this step's time, and diagnose_run on the build this step was shipping."
    )


def _observed_monitoring(
    connection: Connection, project_id: str, context: dict[str, Any]
) -> dict[str, Any]:
    """Whether anything is actually WATCHING what this project shipped.

    "Set up monitoring" was a step that recorded a sentence and checked nothing. BOSS already
    probes every active deployment target on a sweep and stores each sample, so the honest
    version of this step is: name the targets being watched, and say what the probes have found
    so far.

    A step called "Set up monitoring" that succeeds while nothing is being probed is the same
    lie as a review step that goes green over a failed gate — and a worse one, because the whole
    point of the step is to guarantee somebody notices when the app falls over.
    """
    targets = connection.execute(
        text(
            """
            SELECT id, app_name, environment, domains, health_checks
            FROM deployment_targets
            WHERE project_id = :p AND status = 'active'
            ORDER BY app_name
            """
        ),
        {"p": project_id},
    ).mappings().all()

    # THIS RUN'S APP, NOT EVERY APP IN THE PROJECT.
    #
    # The Meeting cost meter workflow reported "watching Incident-Timeline Builder · staging,
    # Meeting cost meter · staging, Vendor-Status-Watcher · staging" — every active target the
    # project has. A run is about ONE app, so two thirds of that was somebody else's work
    # presented as this step's result, and once the step grew links it became three prominent
    # links to the wrong apps.
    #
    # Unscoped only when the run does not say which app it is about: a step that silently
    # narrowed to nothing would be worse than one that over-reports.
    from .naming import identity_key

    wanted = _this_runs_app(connection, context)
    if wanted:
        mine = [t for t in targets if identity_key(str(t["app_name"] or "")) == wanted]
        # An app with no target of its own is a real answer — "nothing is watching this yet" —
        # and far better than listing the neighbours. The caller reports the empty case.
        targets = mine

    if not targets:
        if wanted:
            raise SettledCapabilityError(
                "nothing is watching this app: it has no active deployment target, so no health "
                "probe will ever run against it. Other apps in this project may be watched; "
                "this one is not."
            )
        raise SettledCapabilityError(
            "nothing is being watched: this project has no active deployment target, so no "
            "health probe will ever run against it."
        )

    watched = []
    for target in targets:
        latest = connection.execute(
            text(
                """
                SELECT ok, checked_at, status_code, latency_ms, error
                FROM deployment_health_samples
                WHERE deployment_target_id = :t
                ORDER BY checked_at DESC LIMIT 1
                """
            ),
            {"t": target["id"]},
        ).mappings().first()
        watched.append({
            "target_id": target["id"],
            "app_name": target["app_name"],
            "environment": target["environment"],
            "url": (target["domains"] or [None])[0],
            "checks": len(target["health_checks"] or []),
            # No sample yet is NOT a failure: the sweep runs on an interval, and a target
            # registered a minute ago has simply not been probed. Saying "not probed yet" is
            # the difference between "we are watching" and "we looked and it was fine".
            "last_probe": latest["checked_at"].isoformat()
            if latest and latest["checked_at"] else None,
            "last_ok": bool(latest["ok"]) if latest else None,
            "last_error": latest["error"] if latest else None,
        })

    probed = [w for w in watched if w["last_probe"]]

    # AND IS THERE A DASHBOARD? "Set up monitoring" set nothing up.
    #
    # The step reported health probing, which BOSS does for every active target on a sweep
    # regardless — so the step was true and useless. Meanwhile the Grafana dashboard and the
    # Prometheus scrape config are only written by an explicit stack deploy that nothing calls
    # automatically, so an app onboarded after the last one had a console link to a dashboard
    # that had never been created. Nine apps deployed, three dashboards.
    #
    # It still does not deploy the stack from here: that SSHes to a host, and a workflow step
    # that silently reconfigures shared infrastructure is a worse failure than a missing panel.
    # It REPORTS the gap, so the step stops claiming a setup it never did and names the one
    # action that closes it.
    dashboard: dict[str, Any] = {}
    from .observability_stack import DEFAULT_PORTS, provisioned_dashboards

    host = _monitoring_host(connection, project_id)
    have = None
    if host:
        base = f"http://{host}:{DEFAULT_PORTS['grafana']}"
        try:
            # ONLY the call to Grafana is allowed to fail quietly. The whole block sat in this
            # try, so `dashboard_uid(w["app"])` — the key is `app_name` — raised KeyError on
            # every run and was logged as "could not check dashboards": the check never ran.
            have = provisioned_dashboards(base)
        except Exception as error:  # noqa: BLE001 — an unreachable Grafana must not fail the step
            log.warning("could not check dashboards for %s: %s", project_id, error)
        if have is not None:
            dashboard = {"grafana": base, "missing_dashboards": missing_dashboards(watched, have)}

    missing = dashboard.get("missing_dashboards") or []
    return {
        "capability": "monitor",
        "tool": None,
        "status": "observed",
        "targets": watched,
        **dashboard,
        "note": (
            f"{len(watched)} target(s) under health probing"
            + (f", {len(probed)} probed so far." if probed else ", none probed yet.")
            + (f" {len(missing)} of them have no Grafana dashboard yet "
               f"({', '.join(missing[:3])}"
               f"{'…' if len(missing) > 3 else ''}) — redeploy the observability stack from "
               f"Infra and Models to create them." if missing else "")
        ),
    }


def missing_dashboards(watched: list[dict[str, Any]], have: set[str]) -> list[str]:
    """The watched apps Grafana has no dashboard for, by app name."""
    from .observability_stack import dashboard_uid

    return sorted({w["app_name"] for w in watched if dashboard_uid(w["app_name"]) not in have})


def _monitoring_host(connection: Connection, project_id: str) -> str:
    """The host the observability stack runs on, from the targets it already watches.

    Read rather than configured: every deployment target already names the machine it ships to,
    and the stack lives on the same one. A second place to record it is a second place for it to
    be wrong.
    """
    row = connection.execute(
        text("SELECT h.address FROM deployment_targets t "
             "JOIN deployment_hosts h ON h.id = t.host_id "
             "WHERE t.project_id = :p AND t.status = 'active' AND h.address <> '' "
             "ORDER BY t.created_at LIMIT 1"),
        {"p": project_id},
    ).scalar()
    return str(row or "").strip()


def _build_run_id(context: dict[str, Any]) -> Optional[str]:
    """The code task this workflow run is about.

    Read from whichever step produced it rather than from a step named "build": a customer can
    rename their steps, and the canonical field is the contract, not the id.
    """
    for produced in (context.get("steps") or {}).values():
        if isinstance(produced, dict):
            value = produced.get("code_task_run_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    value = (context.get("input") or {}).get("adopt_code_task_run_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def release_base_for(connection: Connection, context: dict[str, Any]) -> Optional[str]:
    """The serving commit a release-targeting build is cut from, or None for any other build:
    the run input's `release_base_sha`, else, for an incident's run, the commit its
    investigation found serving (or the incident's own record of it)."""
    run_input = context.get("input") or {}
    named = str(run_input.get("release_base_sha") or "").strip()
    if named:
        return named
    incident_id = str(run_input.get("incident_id") or "").strip()
    if not incident_id:
        return None
    found = str(_from_any_step(context, "serving_commit_sha") or "").strip()
    if found:
        return found
    row = connection.execute(text(
        "SELECT finalize_ctx->'deploy'->>'commit_sha' FROM incidents WHERE id = :i"),
        {"i": incident_id}).scalar()
    return str(row).strip() if row else None


def _from_any_step(context: dict[str, Any], field: str) -> Any:
    """The first step that produced `field`, whatever the step happens to be called.

    Same rule as `_build_run_id`: the canonical field is the contract, not the step id. A
    customer can rename "meeting" to "Kickoff" and the document still finds the PRD.
    """
    for produced in (context.get("steps") or {}).values():
        if isinstance(produced, dict):
            value = produced.get(field)
            if value not in (None, "", [], {}):
                return value
    return None


def _write_the_record(
    connection: Connection, project_id: str, context: dict[str, Any], inputs: dict[str, Any]
) -> dict[str, Any]:
    """Assemble this run's record from what its steps produced, and store it as a document.

    THE STEP THAT DOCUMENTED NOTHING, then documented it badly. `document` had `tool=None` and
    fell through to the no-op branch, so a Research-Led Discovery run — a workflow whose own
    description is "ends in a decision and a document" — finished 4-of-4 green with no document.
    The first fix concatenated each step's output, which produced a readable-by-nobody dump: the
    build brief including its shared quality bar, the directive twice, and twenty-three research
    findings as inline JSON dicts. Worse, it buried the finding that mattered — that research had
    contradicted the premise.

    Composition now lives in `change_record`, which leads with a summary and reads the discovery
    run's OWN structured state rather than the flattened brief the panel was handed. A PDF is
    rendered alongside, because a document nobody can open is the same problem in a new format.

    Nothing is invented: every line is a field some step recorded, and the gaps are listed.
    """
    from ..config import get_settings
    from .core import create_artifact

    run_input = context.get("input") or {}
    # THE APP'S NAME, via the namer the rest of the console titles things with.
    #
    # Run through the namer WHATEVER the source, not only when falling back to the directive.
    # The run's own `title` is often the directive already truncated for display — this one was
    # "Build an interview scorecard app.\nWho it is for: a hiring manager tir…", ellipsis and
    # all — so preferring it verbatim put a broken sentence at the top of the document and left
    # the namer with nothing to fix. `project_name` leaves a real name alone ("Change Calendar"
    # in, "Change Calendar" out) and turns that one into "Interview Scorecard App".
    from .naming import project_name
    candidate = (str(inputs.get("title") or "").strip()
                 or str(run_input.get("title") or "").strip()
                 or str(run_input.get("directive") or "").strip())
    title = (project_name(candidate)[0].strip().rstrip(".").strip()[:80]
             or candidate.split("\n")[0].strip()[:80]
             or "Change record")
    directive = str(run_input.get("directive") or "").strip()

    dossier_text = _from_any_step(context, "dossier")
    dossier_text = dossier_text if isinstance(dossier_text, str) else ""
    discovery_id = str(_from_any_step(context, "discovery_id") or "")
    prd = str(_from_any_step(context, "prd_markdown") or "")
    meeting_run_id = str(_from_any_step(context, "meeting_run_id") or "")
    decision_id = str(_from_any_step(context, "decision_id") or "")
    pr_url = str(_from_any_step(context, "pull_request_url") or "")
    code_task_run_id = _build_run_id(context) or ""

    # THE DOSSIER, FROM THE RESEARCH RUN ITSELF. `_dossier_brief` flattens lists to JSON on
    # purpose — it is written to be fed to a language model — and reading it back for a human
    # document is what put `{"angle": "competitor", "finding": ...}` on the page.
    dossier_state: Optional[dict[str, Any]] = None
    if discovery_id:
        try:
            state = connection.execute(
                text("SELECT state FROM discovery_runs WHERE id=:d AND project_id=:p"),
                {"d": discovery_id, "p": project_id},
            ).scalar()
            dossier_state = state if isinstance(state, dict) else None
        except Exception as error:  # noqa: BLE001 — an unreadable dossier must not sink the run
            log.warning("cannot read discovery %s for the record: %s", discovery_id, error)

    decision: Optional[dict[str, Any]] = None
    if decision_id:
        try:
            row = connection.execute(
                text("SELECT question, status, approved_by, resolution_note FROM decisions "
                     "WHERE id = :d AND project_id = :p"),
                {"d": decision_id, "p": project_id},
            ).mappings().first()
            decision = dict(row) if row else None
        except Exception as error:  # noqa: BLE001
            log.warning("cannot read decision %s for the record: %s", decision_id, error)

    markdown, sections = change_record.compose(
        title=title, directive=directive, dossier_state=dossier_state,
        dossier_text=dossier_text, discovery_id=discovery_id, prd=prd,
        meeting_run_id=meeting_run_id, decision=decision, decision_id=decision_id,
        pull_request_url=pr_url, code_task_run_id=code_task_run_id,
        workflow_name=str(context.get("workflow_name") or ""),
        objective=str(context.get("objective") or ""),
    )

    # WHAT A CHECKLIST CAN SEE, checked. Deterministic and free, so it runs every time and
    # always terminates — unlike asking an agent whether the prose is good, which is a taste
    # judgement and can be argued forever. Findings are RECORDED rather than used to block: a
    # document held back for a defect nobody can see is worse than one that names its defects.
    defects = change_record.review(markdown, sections)

    pdf_bytes = b""
    pdf_error = ""
    try:
        from .pdf_render import render_markdown_pdf
        # The renderer draws its own title block, so the markdown's H1 is handed over rather
        # than left in the body — the first PDF printed the title twice, two lines apart.
        pdf_title, pdf_subtitle, pdf_body = change_record.body_for_pdf(markdown)
        pdf_bytes = render_markdown_pdf(
            pdf_body, title=pdf_title or title, subtitle=pdf_subtitle or None)
    except Exception as error:  # noqa: BLE001 — no PDF is a worse document, not a failed step
        pdf_error = f"{type(error).__name__}: {error}"
        log.warning("could not render the change record to PDF: %s", pdf_error)

    settings = get_settings()
    pdf_path = ""
    if pdf_bytes:
        try:
            directory = settings.artifact_root / project_id
            directory.mkdir(parents=True, exist_ok=True)
            pdf_path = str(directory / f"record-{context.get('run_id') or 'run'}.pdf")
            with open(pdf_path, "wb") as handle:
                handle.write(pdf_bytes)
        except Exception as error:  # noqa: BLE001
            pdf_error = f"{type(error).__name__}: {error}"
            pdf_path = ""

    document_id = create_artifact(
        connection,
        settings,
        project_id,
        _from_any_step(context, "task_id") or (context.get("input") or {}).get("task_id"),
        "change_record",
        title,
        {
            "markdown": markdown,
            "sections": sections,
            "defects": defects,
            "pdf_path": pdf_path,
            "pdf_bytes": len(pdf_bytes),
            "pdf_error": pdf_error,
            "workflow_run_id": context.get("run_id"),
            "discovery_id": discovery_id,
            "meeting_run_id": meeting_run_id,
            "decision_id": decision_id,
            "code_task_run_id": code_task_run_id,
            "pull_request_url": pr_url,
        },
    )
    note = (f"Change record written from {', '.join(sections)}."
            if sections else
            "Change record written, but this run produced nothing to put in it.")
    if defects:
        note += f" {len(defects)} thing(s) the checks flagged."
    return {
        "capability": "document",
        "tool": None,
        "status": "published",
        "document_id": document_id,
        "sections": sections,
        "defects": defects,
        "pdf": bool(pdf_bytes),
        "note": note,
    }


def rebuild_record(connection: Connection, project_id: str, artifact_id: str) -> dict[str, Any]:
    """Recompose an existing change record from the run it came from.

    A record is a rendering of facts that are still in the database, so a composition fix should
    reach the documents already written rather than only the next one. Without this the operator
    has to take "it is fixed now" on trust, on a document that still shows the old dump —
    which, after three rounds of exactly this, is not a reasonable thing to ask.

    Rewrites the artifact in place: same id, same link, new body. It cannot invent anything the
    run did not produce, because it reads the same context the step read.
    """
    row = connection.execute(
        text("SELECT title, content FROM artifacts "
             "WHERE id = :a AND project_id = :p AND artifact_type = 'change_record'"),
        {"a": artifact_id, "p": project_id},
    ).mappings().first()
    if not row:
        return {"ok": False, "error": f"no change record {artifact_id} in this project"}
    content = row["content"] if isinstance(row["content"], dict) else {}
    run_id = str(content.get("workflow_run_id") or "")
    if not run_id:
        return {"ok": False,
                "error": "this record does not name the run it came from, so it cannot be "
                         "rebuilt — only a new run will produce a corrected one"}
    context = _run_context(connection, run_id)
    # NO TITLE PASSED IN. Handing back the stored one preserved exactly what the rebuild is for
    # — the first rebuild kept "Build an interview scorecard app.\nWho it is for: a hiring
    # manager tir…" as the heading and the repetition check flagged it again. The writer
    # derives the title from the run's own input, which is where a good one comes from.
    result = _write_the_record(connection, project_id, context, {})
    new_id = result.get("document_id")
    fresh = connection.execute(
        text("SELECT title, content FROM artifacts WHERE id = :a"), {"a": new_id},
    ).mappings().first()
    if not fresh:
        return {"ok": False, "error": "the rebuild produced no artifact"}
    # Update IN PLACE and drop the duplicate, so links to this record keep working.
    connection.execute(
        text("UPDATE artifacts SET title = :t, content = CAST(:c AS JSONB) WHERE id = :a"),
        {"t": fresh["title"], "c": json.dumps(fresh["content"]), "a": artifact_id},
    )
    connection.execute(text("DELETE FROM artifacts WHERE id = :a"), {"a": new_id})
    defects = (fresh["content"] or {}).get("defects") or []
    return {"ok": True, "artifact_id": artifact_id, "run_id": run_id,
            "sections": (fresh["content"] or {}).get("sections") or [],
            "defects": defects,
            "note": f"Rebuilt from {run_id}."
                    + (f" {len(defects)} check(s) still flag it." if defects else "")}


def _write_the_run_report(
    connection: Connection, project_id: str, context: dict[str, Any]
) -> dict[str, Any]:
    """This run's own report: what each step cost, who did it, and what went wrong.

    A STEP, so that it is produced rather than merely available. The report could always be
    assembled on demand, and an operator still had to know to ask for it. Asked for by name:

        "I want this report to be added before the Preview"

    Placed after the build and before the preview because that is the moment the expensive,
    failure-prone part is over and the numbers are worth reading — not at the very end, where a
    run parked on a deploy approval would never reach it. It reads what has happened SO FAR;
    steps after it are listed as pending, which is part of the answer rather than a gap.

    A report that cannot be written FAILS the step, like any other work that did not happen.
    Whether the run goes on without it is the operator's decision (Skip and proceed).
    """
    from ..config import get_settings
    from .core import create_artifact
    from .run_retrospective import assemble_workflow, workflow_markdown

    run_id = str(context.get("run_id") or "")
    if not run_id:
        raise CapabilityExecutionError("there is no run to report on, so no report was written.")

    report = assemble_workflow(connection, project_id, run_id)
    if not report.get("ok"):
        raise CapabilityExecutionError(
            "the run report could not be assembled: "
            + str(report.get("error") or "the run's history was unreadable."))

    markdown = workflow_markdown(report)
    title = f"Run report — {report.get('workflow') or run_id}"
    artifact_id = create_artifact(
        connection, get_settings(), project_id,
        _from_any_step(context, "task_id") or (context.get("input") or {}).get("task_id"),
        "workflow_report", title,
        {"markdown": markdown, "run_id": run_id, "report": report},
    )

    blockers = [f for f in report["findings"] if f["severity"] == "blocker"]
    warnings = [f for f in report["findings"] if f["severity"] == "warn"]
    done = len([s for s in report["steps"]
                if s["status"] in {"succeeded", "skipped"}])
    # The note is what the step card shows without expanding anything, so it carries the
    # three numbers somebody would otherwise open the report to find.
    note = (f"{done} of {len(report['steps'])} steps, "
            f"{report['working_minutes']} min working, "
            f"${report['total_usd']:.2f}.")
    if blockers or warnings:
        note += f" {len(blockers)} blocker(s), {len(warnings)} warning(s)."
    else:
        note += " Nothing went wrong that was recorded."

    return {
        "capability": "report",
        "tool": None,
        "status": "recorded",
        "artifact_id": artifact_id,
        "report": report,
        "total_usd": report["total_usd"],
        "working_minutes": report["working_minutes"],
        "blockers": len(blockers),
        "warnings": len(warnings),
        "note": note,
    }


def _observed_from_the_build(
    connection: Connection, key: str, project_id: str, context: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """What the governed pipeline already found, reported as this step's own result.

    Returns None when there is nothing to observe — no such step kind, no build to look at, or
    no gate recorded — so the caller falls back to saying so rather than inventing a verdict.
    """
    if key == "monitor":
        return _observed_monitoring(connection, project_id, context)
    gate_names = _OBSERVED_GATES.get(key)
    if not gate_names:
        return None
    run_id = _build_run_id(context)
    if not run_id:
        return None
    try:
        row = connection.execute(
            text(
                """
                SELECT status, tests_passed, adversarial_verdict, gates_passed, gates_failed,
                       evidence, risk_tier
                FROM quality_gates
                WHERE project_id = :p AND code_task_run_id = :r
                """
            ),
            {"p": project_id, "r": run_id},
        ).mappings().first()
    except Exception as error:  # noqa: BLE001 — an unreadable gate must not sink the run
        log.warning("cannot read the quality gate for %s: %s", run_id, error)
        return None
    if row is None:
        return None

    failed = [g for g in (row["gates_failed"] or []) if g in gate_names]
    passed = [g for g in (row["gates_passed"] or []) if g in gate_names]
    evidence = row["evidence"] if isinstance(row["evidence"], dict) else {}
    detail: dict[str, Any] = {
        "capability": key,
        "tool": None,
        "code_task_run_id": run_id,
        "gates_passed": passed,
        "gates_failed": failed,
        "risk_tier": row["risk_tier"],
        "seats": evidence.get("steps_run") or [],
    }
    if key == "test":
        detail["tests_passed"] = bool(row["tests_passed"])
        detail["test_commands"] = evidence.get("test_commands") or []
        if not row["tests_passed"]:
            failed = [*failed, "tests_passed"]
    else:
        detail["adversarial_verdict"] = row["adversarial_verdict"]

    if not failed:
        detail["status"] = "observed"
        detail["note"] = (
            f"{', '.join(passed) if passed else 'nothing'} passed on the build "
            f"(risk tier {row['risk_tier']})."
        )
        return detail

    # REPORTED, NEVER A GATE. The inner pipeline decides whether a finding blocks — it already
    # did, by opening or not opening a pull request, and the build step carries that answer.
    # Failing or parking out here overrode a decision that was not the outer loop's to make.
    detail["findings"] = failed
    detail["status"] = "observed"
    detail["note"] = (
        f"the build's reviewers flagged {', '.join(failed)}. The build opened its pull request "
        f"anyway, which is the inner pipeline's call, not this step's."
    )
    return detail



def _running_preview(
    connection: Connection, project_id: str, run_id: Any
) -> Optional[dict[str, Any]]:
    """A preview already serving this build, or None.

    Returns the same field names `start_preview` produces, so a downstream step referencing
    `preview_url` cannot tell the difference between one this step started and one it adopted.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        return None
    row = connection.execute(
        text(
            """
            SELECT id, url FROM app_previews
            WHERE project_id = :p AND run_id = :r AND status = 'running' AND url IS NOT NULL
            ORDER BY created_at DESC LIMIT 1
            """
        ),
        {"p": project_id, "r": run_id.strip()},
    ).mappings().first()
    if not row:
        return None
    # A ROW THAT SAYS "running" IS NOT AN APP THAT ANSWERS.
    #
    # This adopted on the database's word alone, and app_previews goes stale in every ordinary
    # way: the container is removed, the host is rebuilt, the sandbox is reaped. The step then
    # reported success with a URL that served nothing, `deploy` ran next because its inputs come
    # from `build` rather than from here, and a build nobody could look at shipped anyway —
    # against an explicit instruction that without a preview the run must not move to deploy.
    #
    # Any HTTP answer counts, including a 500: the question is whether something is listening,
    # not whether the app is bug-free.
    if not _preview_answers(row["url"]):
        log.info(
            "preview %s is marked running but %s does not answer — not adopting it",
            row["id"], row["url"],
        )
        return None
    # preview_url is the internal address this module probes; open_url is the one to show.
    return {"preview_id": row["id"], "preview_url": row["url"], "adopted": True,
            "open_url": f"/v1/previews/{row['id']}/web/"}


def _architecture_step(connection: Connection, ctx: Any, project_id: str, step: dict[str, Any],
                       context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    """Run the Architecture step (services/architecture_docs.py) and translate its refusals.

    No approved PRD upstream is SETTLED — a retry would be refused the same way until somebody
    approves the PRD. A model failure is an ordinary, retryable failure.
    """
    from . import architecture_docs

    run_id = str(context.get("run_id") or "")
    try:
        spec_steps = {s["id"]: s for s in workflows._run_spec(connection, run_id).get("steps", [])}
    except Exception:  # noqa: BLE001 — without the spec, only the panel decision proves approval
        spec_steps = {}
    try:
        return architecture_docs.start(
            connection, getattr(ctx, "settings", None), project_id=project_id, run_id=run_id,
            step=step, context=context, spec_steps=spec_steps,
            constraints=str(inputs.get("constraints") or "").strip())
    except architecture_docs.ArchitectureRefused as refused:
        raise SettledCapabilityError(str(refused)) from refused
    except architecture_docs.ArchitectureDraftError as error:
        raise CapabilityExecutionError(str(error)) from error


def _plrr_brief(connection: Connection, project_id: str, context: dict[str, Any]) -> str:
    """The instructions a readiness repair is given, built from THIS run's own spec.

    Deliberately not a generic checklist. The operations lens in the panel names the signals
    for the features in the spec, so the review's job is to hold the built app against what
    that spec promised — not against a list of things somebody thought were good practice.
    """
    run_id = str((context or {}).get("run_id") or "")
    directive, criteria = "", []
    try:
        row = connection.execute(
            text("SELECT input FROM workflow_runs WHERE id = :r"), {"r": run_id},
        ).mappings().first()
        if row:
            data = row["input"] or {}
            directive = str(data.get("directive") or data.get("title") or "")
            criteria = list(data.get("acceptance_criteria") or [])
    except Exception:  # noqa: BLE001 — a brief that cannot read the run still has a job
        log.exception("plrr could not read the run's input for %s", run_id)

    wanted = "\n".join(f"- {c}" for c in criteria) or "- (none recorded on the run)"
    return (
        "PRODUCTION LAUNCH READINESS REVIEW (PLRR)\n\n"
        "This app has been built and previewed. It is NOT going to production until it can be "
        "operated. Your job is to close that gap in code — not to write a report.\n\n"
        f"WHAT IT WAS ASKED TO DO:\n{directive[:1500]}\n\n"
        f"ACCEPTANCE CRITERIA:\n{wanted}\n\n"
        "GO THROUGH EACH OF THESE AND FIX WHAT IS MISSING:\n"
        "1. SIGNAL PER FEATURE. For every acceptance criterion above, is there a metric that "
        "   shows it working and one that shows it failing? Name them after the domain action "
        "   and split by result. A feature with no signal is not production-ready.\n"
        "2. /metrics. Present, Prometheus text format, with http_requests_total, "
        "   http_request_duration_seconds, app_info and app_uptime_seconds. Route labels "
        "   TEMPLATED — two path parameters must collapse to one series.\n"
        "3. HEALTH AND READINESS. An endpoint that answers whether the app can serve, distinct "
        "   from whether the process is alive. It must not report healthy while a dependency it "
        "   needs is down.\n"
        "4. FAILURE BEHAVIOUR. What happens when a dependency is unavailable, input is "
        "   malformed, or the disk is full? An unhandled 500 with no log line is a night "
        "   somebody loses.\n"
        "5. DIAGNOSABILITY. When this misbehaves at 3am, what is the first command? If the "
        "   answer is 'read the source', add the log lines or the endpoint that answers it.\n"
        "6. ROLLBACK. Can this release be undone without data loss? If a migration is "
        "   one-way, say so in the README where somebody will find it.\n\n"
        "RULES\n"
        "- Change code and tests. Every gap you close gets a test that fails without the fix.\n"
        "- Do NOT weaken or delete existing tests to make anything pass.\n"
        "- If a gap cannot be closed in code, say which one and why, in one line.\n"
        "- End with a list of the gaps you closed and the ones you did not."
    )


def _plrr_step(ctx, connection: Connection, project_id: str, actor: str,
               context: dict[str, Any], step: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Review readiness, and start the work that closes what is missing.

    Sits between preview and deploy on purpose. Preview answers "does it do the thing"; this
    answers "can anyone run it once it is real". Companies usually hold that conversation after
    launch, which is why the first on-call night is the one that discovers the app has no
    metrics, no health check and no way back.

    Returns `awaiting` on a code task when there is work to do, so the workflow waits for the
    repair exactly as it waits for the build.
    """
    run_id = str((context or {}).get("run_id") or "")
    build_run = ""
    try:
        build_run = str(connection.execute(
            text("SELECT COALESCE("
                 "  (SELECT input->>'adopt_code_task_run_id' FROM workflow_runs WHERE id = :r),"
                 "  (SELECT output->>'code_task_run_id' FROM workflow_step_runs "
                 "     WHERE workflow_run_id = :r AND output ? 'code_task_run_id' "
                 "     ORDER BY created_at DESC LIMIT 1)) AS build"),
            {"r": run_id}).scalar() or "")
    except Exception:  # noqa: BLE001
        log.exception("plrr could not resolve the build for %s", run_id)

    if not build_run:
        # Nothing was built in this run, so there is nothing to hold against the spec. Saying so
        # beats inventing a verdict — and beats blocking a workflow that never built anything.
        return {"capability": "plrr", "status": "not_applicable",
                "note": "no build in this run, so there is nothing to review for readiness."}

    started = _start_on_the_branch(ctx, connection, project_id=project_id, actor=actor,
                                   context=context, step=step, capability_key="plrr",
                                   base_run=build_run, args={
        "run_id": build_run,
        "instructions": _plrr_brief(connection, project_id, context),
        "title": "Production launch readiness",
    })
    return {"capability": "plrr", "tool": "extend_code_task", **started}


from .build_scope import MORE_TESTS_LIGHT, MORE_TESTS_MARKER  # noqa: E402 — pass 2's phrases
from .two_pass import LIGHT as LIGHT_PASS  # noqa: E402


def _more_tests_brief(light: bool = False) -> str:
    """Pass 2's instructions, appended to the build's brief by extend_code_task. The marker is
    what build_scope reads to lift pass 1's limits (browser tests, a larger test budget).

    Pass 1 wrote the P0/P1 tests (the app starts, sign-in/authz, the core happy path, the main
    path of each feature); this asks for P2. A LIGHT pass (a hotfix, two_pass.LIGHT) asks only
    for regression tests around the change, within pass 1's budget and without a browser suite.
    """
    if light:
        return (
            f"{MORE_TESTS_MARKER} ({MORE_TESTS_LIGHT}): the fix on this branch works and its "
            "P0/P1 tests pass. Add P2 regression tests around THE CHANGE only: the failure it "
            "fixes, its error paths and the inputs at its edges. No browser (Playwright) tests "
            "and no tests for code the change did not touch.\n"
            "RULES\n"
            "- Do not change behaviour the existing tests pin, and do not weaken or delete them.\n"
            "- If a new test exposes a real bug, fix the implementation and list it under "
            "`BUGS FIXED:` in your summary, one line each; write `BUGS FIXED: none` otherwise.\n"
            "- Do not write tests for an applied connector (sign-in, payments): it is tested in "
            "its own suite."
        )
    return (
        f"{MORE_TESTS_MARKER}: the app on this branch works and its first, lean test suite "
        "(the P0/P1 critical paths) passes. Add the P2 coverage for the features already built: "
        "edge cases, error paths, input validation and the API contracts (status codes, "
        "response shapes), and the browser (Playwright) tests the task asked for, if it asked "
        "for any.\n"
        "RULES\n"
        "- Do not change behaviour the existing tests pin, and do not weaken or delete them.\n"
        "- If a new test exposes a real bug, fix the implementation and list it under "
        "`BUGS FIXED:` in your summary, one line each; write `BUGS FIXED: none` otherwise.\n"
        "- Do not write tests for an applied connector (sign-in, payments): it is tested in its "
        "own suite."
    )


def _more_tests_step(ctx, connection: Connection, project_id: str, actor: str,
                     context: dict[str, Any],
                     inputs: Optional[dict[str, Any]] = None,
                     step: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """PASS 2 OF A BUILD: more tests on the same branch and PR, after the preview.

    Pass 1 ships a working app with a lean suite, so the user sees it early; this pass adds the
    coverage in its own commits. It never holds the run up: a failed pass 2 completes the step
    with the failure said (resume_awaited, _ADVISORY_CAPABILITIES) and the run goes on with
    pass 1's code."""
    run_id = str((context or {}).get("run_id") or "")
    build_run = ""
    try:
        # THE NEWEST FINISHED PASS ON THE BRANCH (build, readiness, an earlier More tests), not
        # the newest step that mentions a run: pass 2 now runs beside the deploy (two_pass), so
        # "the latest output with a code_task_run_id" could be the deploy's.
        build_run = _latest_code_run(connection, run_id) or str(connection.execute(
            text("SELECT input->>'adopt_code_task_run_id' FROM workflow_runs WHERE id = :r"),
            {"r": run_id}).scalar() or "") or str((inputs or {}).get("run_id") or "")
    except Exception:  # noqa: BLE001
        log.exception("more_tests could not resolve the build for %s", run_id)
    if not build_run:
        return {"capability": "more_tests", "status": "not_applicable",
                "note": "no build in this run, so there is nothing to add tests to."}
    light = str((inputs or {}).get("depth") or "") == LIGHT_PASS
    started = _start_on_the_branch(ctx, connection, project_id=project_id, actor=actor,
                                   context=context, step=step, capability_key="more_tests",
                                   base_run=build_run, args={
        "run_id": build_run, "instructions": _more_tests_brief(light),
        "title": "More tests (light)" if light else "More tests",
    })
    return {"capability": "more_tests", "tool": "extend_code_task", "base_run_id": build_run,
            **started}


def _more_tests_started(ctx, connection: Connection, *, project_id: str, actor: str,
                        context: dict[str, Any], step: dict[str, Any], capability: Any,
                        inputs: dict[str, Any]) -> dict[str, Any]:
    """More tests, started — or, when it cannot even start, completed with the failure said.

    PASS 2 NEVER STOPS THE RUN, AT START EITHER. _ADVISORY_CAPABILITIES covered a pass that
    failed while it ran (resume_awaited). One refused or broken at START failed its step, was
    retried three times, and the `all` join before the done step (two_pass) stopped the whole
    run on a pass that only adds tests (wfr_9575c86ab4a74defa0d2). A wait (a budget window, a
    busy model) still waits: that is not a failure."""
    base_now = _latest_code_run(connection, str(context.get("run_id") or ""))
    # PASS 1'S DEPLOY SHIPS PASS 1. Pass 2 pushes beside the deploy (two_pass), and a deploy
    # planned from `pull/N` after pass 2's first push would ship commits pass 1's review never
    # saw. The head pass 1 left is read before pass 2 starts and kept on this step's output.
    pinned = pass_one_head(connection, getattr(ctx, "settings", None), project_id,
                           base_now) if base_now else None
    try:
        started = _more_tests_step(ctx, connection, project_id, actor, context, inputs, step)
        if started.get("status") == "not_applicable":
            return started
        settled = _settle_started_work(capability, "more_tests", started)
        why = str(settled.get("message") or settled.get("error") or "")
        failed = str(settled.get("status") or "") == "failed"
        base = str(settled.get("base_run_id") or "")
    except CapabilityExecutionError as error:
        if _as_wait(error) is not None:
            raise
        failed, why, base = True, str(error), ""
    if not failed:
        return {**settled, PASS_ONE_HEAD: pinned} if pinned else settled
    base = base or _latest_code_run(connection, str(context.get("run_id") or ""))
    return advisory_start_failure(why, base)


PASS_ONE_HEAD = "pass_one_head_sha"


def pass_one_head(connection: Connection, settings: Any, project_id: str, base_run: str, *,
                  resolve: Any = None) -> Optional[str]:
    """The pull request's head as pass 1 (`base_run`) left it, or None when it cannot be told
    (no pull request, GitHub unreachable) — the deploy then plans the pull request's head."""
    try:
        row = connection.execute(
            text("SELECT spec, pull_request_number FROM code_task_runs "
                 "WHERE id = :i AND project_id = :p"),
            {"i": base_run, "p": project_id}).mappings().first()
        if not row or not row["pull_request_number"]:
            return None
        from .stack_deploy import build_repository, github_source_for, split_repository

        repository = build_repository(row["spec"], str(getattr(settings, "target_github_owner", "")
                                                       or ""))
        if not repository:
            return None
        owner, repo = split_repository(repository)
        ref = f"pull/{int(row['pull_request_number'])}"
        return (resolve(owner, repo, ref) if resolve is not None else
                github_source_for(connection, settings, project_id, owner, repo)
                .resolve_commit(owner, repo, ref))
    except Exception as error:  # noqa: BLE001 — unpinned: the deploy plans the PR's head
        log.warning("could not read pass 1's head for %s: %s", base_run, error)
        return None


def pinned_pass_one_commit(connection: Connection, run_id: str) -> Optional[str]:
    """The commit this run's first deploy ships: the head pass 1 left, read when this run's
    More tests started — only while that pass 1 is still the newest (a later build or repair
    makes the pin stale, and the pull request's head is right again)."""
    if not run_id:
        return None
    row = connection.execute(
        text("SELECT output->>:k AS sha, output->>'base_run_id' AS base FROM workflow_step_runs "
             "WHERE workflow_run_id = :r AND COALESCE(output->>:k, '') <> '' "
             "AND COALESCE(binding_snapshot->>'capability', "
             "             binding_snapshot->'binding'->>'capability', '') = 'more_tests' "
             "ORDER BY created_at DESC LIMIT 1"),
        {"r": run_id, "k": PASS_ONE_HEAD}).mappings().first()
    if not row:
        return None
    newest_pass_one = str(connection.execute(
        text("SELECT output->>'code_task_run_id' FROM workflow_step_runs "
             "WHERE workflow_run_id = :r AND status = 'succeeded' "
             "AND COALESCE(binding_snapshot->>'capability', "
             "             binding_snapshot->'binding'->>'capability', '') IN "
             "    ('build', 'plrr', 'retry_build') "
             "AND COALESCE(output->>'code_task_run_id', '') <> '' "
             "ORDER BY completed_at DESC NULLS LAST, updated_at DESC LIMIT 1"),
        {"r": run_id}).scalar() or "")
    return str(row["sha"]) if row["base"] and row["base"] == newest_pass_one else None


def advisory_start_failure(why: str, base: str) -> dict[str, Any]:
    """The output of a More tests pass that could not start: completed, saying so."""
    return {"capability": "more_tests", "tool": "extend_code_task", "status": "not_applicable",
            "advisory": True, "advisory_failed": True, "base_run_id": base,
            "code_task_run_id": base or None,
            "note": (f"the More tests pass could not start ({why.strip() or 'no reason given'})"
                     "; the run carries on with the first pass's build.")[:600]}


#: The tools a step may start work on the build's branch with (in-process, by capability).
_BRANCH_TOOLS = frozenset({"extend_code_task"})


def _start_on_the_branch(ctx, connection: Connection, *, project_id: str, actor: str,
                         context: dict[str, Any], step: Optional[dict[str, Any]],
                         capability_key: str, base_run: str,
                         args: dict[str, Any]) -> dict[str, Any]:
    """Start work on the build's branch (PLRR, More tests, an `extend` build) the way every
    other step calls its tool: under the run's recorded authority (`_step_authority`), as the
    BUILDER the branch's task runs as (runs.resolved_builder of the base run — the agent the
    consent named for the build and whose Passport holds code_task:retry), past the same tool
    checks (`_invoke_tool`), for a tool the capability declares (`Capability.calls`, which is
    what the consent asked for). An identity refusal is SETTLED: retrying it three times
    cannot grant anything."""
    from boss_engine.identity import consent as _consent
    from boss_engine.identity import runs as _runs

    tool = "extend_code_task"
    cap = workflow_capabilities.CAPABILITIES.get(capability_key)
    if cap is None or tool not in _consent.capability_tools(cap):
        raise SettledCapabilityError(
            f"'{capability_key}' calls {tool} without declaring it (Capability.calls), so the "
            "consent for this run never asked for it.")
    builder = ""
    try:
        row = connection.execute(
            text("SELECT spec, finalize_ctx FROM code_task_runs WHERE id = :i"),
            {"i": base_run}).mappings().first()
        if row is not None:
            builder = _runs.resolved_builder(_as_dict(row["spec"]), _as_dict(row["finalize_ctx"]))
    except Exception:  # noqa: BLE001 — unknown builder: the default builder, as consent assumes
        log.exception("could not read the builder of %s", base_run)
    builder = builder or _runs.resolved_builder()
    with _step_authority(ctx, connection, project_id, context, step, builder):
        started = _invoke_tool(ctx, tool, project_id, actor, args)
    if started.get("ok") is False and started.get("permission"):
        raise SettledCapabilityError(str(started.get("message") or started.get("error")
                                         or f"{tool} was refused for this run's authority."))
    return started


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


# ── THE APP NEVER WAITS FOR PASS 2 (two_pass) ─────────────────────────────────────────────────

def _is_advisory_preview_row(connection: Connection, run_id: str) -> bool:
    try:
        return bool(connection.execute(
            text("SELECT 1 FROM workflow_step_runs WHERE workflow_run_id = :r "
                 "AND step_id = 'preview' "
                 "AND COALESCE(binding_snapshot->'inputs'->>'advisory', "
                 "             binding_snapshot->'binding'->'inputs'->>'advisory') = 'true' "
                 "LIMIT 1"),
            {"r": run_id}).first())
    except Exception:  # noqa: BLE001 — unknown means the ordinary rule applies
        return False


def deploy_environment(spec_step: dict[str, Any], run_input: Any) -> str:
    """Where a deploy step ships (deploy_policy.step_environment)."""
    from .deploy_policy import step_environment

    return step_environment(spec_step, run_input)


def policy_ships_without_asking(connection: Connection, project_id: str,
                                environment: str) -> bool:
    """The environment's policy, set by a person, says a deploy there asks nobody. Says nothing
    about a STEP: whether a step may ship on it is `policy_answers_for_step`."""
    from .deploy_policy import pre_approval

    try:
        return bool((pre_approval(connection, project_id, environment) or {}).get(
            "source") == "policy")
    except Exception:  # noqa: BLE001 — unknown means ask
        return False


def policy_answers_for_step(connection: Connection, project_id: str,
                            spec_step: dict[str, Any], run_input: Any) -> bool:
    """THE ONE ANSWER for a Deploy step (deploy_policy.step_answer): the environment's policy
    answers only for a step that said it may (`approval.follows_policy`). A step that promised a
    person is asked, whatever the policy says — the words every surface shows come from the
    same function, so the promise and the behaviour cannot disagree."""
    from .deploy_policy import step_answer

    try:
        return step_answer(connection, project_id, spec_step, run_input)["who"] == "policy"
    except Exception:  # noqa: BLE001 — unknown means ask
        return False


def _seen_live_by_policy(connection: Connection, project_id: str,
                         context: dict[str, Any]) -> Optional[str]:
    """The environment this run's first deploy reaches with nobody asked, if it has one: the
    app is seen there, straight after pass 1, so an automatic preview adds nothing."""
    run_id = str(context.get("run_id") or "")
    try:
        steps = workflows._run_spec(connection, run_id).get("steps") or []
    except Exception:  # noqa: BLE001
        return None
    for spec_step in steps:
        binding = spec_step.get("binding") or {}
        if binding.get("capability") == "deploy" and not (binding.get("inputs") or {}).get(
                "pass_2_of"):
            env = deploy_environment(spec_step, context.get("input"))
            return env if policy_answers_for_step(connection, project_id, spec_step,
                                                  context.get("input")) else None
    return None


def _advisory_preview(connection: Connection, ctx: Any, *, project_id: str, actor: str,
                      step: dict[str, Any], context: dict[str, Any], capability: Any,
                      inputs: dict[str, Any]) -> dict[str, Any]:
    """The preview BOSS adds after pass 1 where a workflow had none (two_pass): the first
    running app. It never blocks: an app that cannot be previewed says so and the run goes on."""
    def unpreviewable(why: str) -> dict[str, Any]:
        return {"capability": "preview", "tool": None, "status": "not_applicable",
                "advisory": True,
                "note": (f"This app could not be previewed ({why.strip() or 'no reason given'})."
                         " The run goes on without a preview.")[:600]}

    live_on = _seen_live_by_policy(connection, project_id, context)
    if live_on:
        return {"capability": "preview", "tool": None, "status": "not_applicable",
                "advisory": True,
                "note": f"No separate preview: the app goes live on {live_on} straight after "
                        "this build, by that environment's policy, and that is where to see it."}
    try:
        out = _perform_capability(connection, ctx, project_id=project_id, actor=actor,
                                  step=step, context=context, key="preview",
                                  capability=capability, inputs=inputs)
    except CapabilityExecutionError as error:
        if _as_wait(error) is not None:
            raise
        return unpreviewable(str(error))
    if str(out.get("status") or "").lower() in _DID_NOT_SHIP:
        return unpreviewable(str(out.get("error") or out.get("note") or ""))
    return {**out, "advisory": True}


def _latest_code_run(connection: Connection, run_id: str) -> str:
    """The newest finished pass on this run's branch: the build, readiness or More tests run
    the last of those steps left (a failed More tests pass leaves the build's)."""
    return str(connection.execute(
        text("SELECT output->>'code_task_run_id' FROM workflow_step_runs "
             "WHERE workflow_run_id = :r AND status = 'succeeded' "
             "AND COALESCE(binding_snapshot->>'capability', "
             "             binding_snapshot->'binding'->>'capability', '') IN "
             "    ('build', 'plrr', 'more_tests', 'retry_build') "
             "AND COALESCE(output->>'code_task_run_id', '') <> '' "
             "ORDER BY completed_at DESC NULLS LAST, updated_at DESC LIMIT 1"),
        {"r": run_id}).scalar() or "")


def _build_on_the_branch(connection: Connection, ctx: Any, *, project_id: str, actor: str,
                         context: dict[str, Any], capability: Any, step: dict[str, Any],
                         inputs: dict[str, Any]) -> dict[str, Any]:
    """A build with `extend` (Incremental Build's later increments): it builds on THIS run's
    branch, from the newest finished pass, as more commits on the same pull request."""
    base = _latest_code_run(connection, str(context.get("run_id") or ""))
    if not base:
        raise SettledCapabilityError(
            "this build adds to the app an earlier step of this run built, and no earlier "
            "build in the run finished, so there is nothing to build on.")
    started = _start_on_the_branch(ctx, connection, project_id=project_id, actor=actor,
                                   context=context, step=step, capability_key="build",
                                   base_run=base, args={
        "run_id": base, "instructions": str(inputs.get("instructions") or ""),
        "title": str(inputs.get("title") or "Next increment"),
    })
    return _settle_started_work(capability, "build", {
        "capability": "build", "tool": "extend_code_task", "base_run_id": base, **started})


def pass_two_summary(connection: Connection, run_id: str, more_step_id: str) -> dict[str, Any]:
    """What a More tests pass did, for the redeploy's step and its approval card."""
    row = connection.execute(
        text("SELECT status, output FROM workflow_step_runs WHERE workflow_run_id = :r "
             "AND step_id = :s ORDER BY iteration DESC LIMIT 1"),
        {"r": run_id, "s": more_step_id}).mappings().first()
    out = (row or {}).get("output") or {}
    if isinstance(out, str):
        out = json.loads(out or "{}")
    summary: dict[str, Any] = {"step_id": more_step_id, "status": (row or {}).get("status"),
                               "advisory_failed": bool(out.get("advisory_failed")),
                               "code_task_run_id": out.get("code_task_run_id"),
                               "base_run_id": out.get("base_run_id"), "changed": None,
                               "test_files": 0, "files_changed": None, "bugs_fixed": []}
    pass_two = str(out.get("code_task_run_id") or "")
    if pass_two and pass_two != str(out.get("base_run_id") or ""):
        task = connection.execute(
            text("SELECT status, state FROM code_task_runs WHERE id = :i"),
            {"i": pass_two}).mappings().first()
        state = (task or {}).get("state") or {}
        if isinstance(state, str):
            state = json.loads(state or "{}")
        tests = state.get("test_files") or []
        impl = state.get("impl_files") or []
        files = state.get("files_changed")
        summary.update({"test_files": len(tests), "files_changed": files,
                        "task_status": (task or {}).get("status")})
        known = "files_changed" in state or "test_files" in state or "impl_files" in state
        summary["changed"] = bool(tests or impl or files) if known else None
        said = str(state.get("summary") or "")
        at = said.find("BUGS FIXED:")
        if at >= 0:
            lines = [ln.strip(" -*") for ln in said[at + len("BUGS FIXED:"):].splitlines()]
            summary["bugs_fixed"] = [ln for ln in lines[:12]
                                     if ln and ln.lower() != "none"][:10]
    return summary


def pass_two_sentence(summary: dict[str, Any]) -> str:
    bugs = summary.get("bugs_fixed") or []
    tests = int(summary.get("test_files") or 0)
    parts = [f"More tests added {tests} test file(s)" if tests else "More tests added tests"]
    parts.append(f"and fixed {len(bugs)} bug(s): " + "; ".join(bugs[:5]) if bugs
                 else "and fixed no bugs")
    return " ".join(parts) + "."


def pass_two_redeploy_refusal(connection: Connection, run_id: str, step: dict[str, Any],
                              pass_2_of: str) -> Optional[str]:
    """Why "Deploy again" ships nothing, or None when pass 2 landed commits on a deploy that
    shipped pass 1. Said on the step, so the run says why there was no redeploy."""
    wired = ((step.get("binding") or {}).get("inputs") or {}).get("run_id")
    more_step = str(wired.get("from_step") or "") if isinstance(wired, dict) else ""
    first = connection.execute(
        text("SELECT status, output FROM workflow_step_runs WHERE workflow_run_id = :r "
             "AND step_id = :s ORDER BY iteration DESC LIMIT 1"),
        {"r": run_id, "s": pass_2_of}).mappings().first()
    first_out = (first or {}).get("output") or {}
    if isinstance(first_out, str):
        first_out = json.loads(first_out or "{}")
    if not first or first["status"] != "succeeded" or str(
            first_out.get("status") or "") in ("not_applicable", "skipped"):
        return ("No redeploy: the first deploy did not ship, so there is nothing running to "
                "update with the More tests pass.")
    summary = pass_two_summary(connection, run_id, more_step)
    if summary["status"] != "succeeded" or summary["advisory_failed"] or not summary[
            "code_task_run_id"] or summary["code_task_run_id"] == summary["base_run_id"]:
        return ("No redeploy: the More tests pass did not finish, so what is deployed is the "
                "first pass's build.")
    if summary["changed"] is False:
        from .monitoring_pack import landed

        if landed(connection, run_id, step):
            return None            # the monitoring pack is new on the branch: release it
        return "No redeploy: the More tests pass changed nothing."
    return None


def _preview_missing_for_run(
    connection: Connection, context: dict[str, Any]
) -> Optional[str]:
    """Why `deploy` must not run yet, or None.

    Only speaks up when the run HAS a preview step: a workflow with no preview — a hotfix lane,
    a data migration — is not asking for one and must not be blocked by its absence.
    """
    run_id = str((context or {}).get("run_id") or "")
    if not run_id:
        return None
    try:
        row = connection.execute(
            text(
                "SELECT status, output FROM workflow_step_runs "
                "WHERE workflow_run_id = :r AND step_id = 'preview' "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"r": run_id},
        ).mappings().first()
    except Exception:  # noqa: BLE001 — a check that cannot run must not block a release
        log.exception("could not read the preview step for run %s", run_id)
        return None
    if row is None:
        return None                       # no preview step in this workflow: nothing to require
    if _is_advisory_preview_row(connection, run_id):
        return None                       # BOSS added it to show the app; it never gates a ship
    if _preview_waived(row):
        return None                       # a person chose to ship without one, and said why
    if row["status"] != "succeeded":
        return (f"the preview step is '{row['status']}', so there is nothing to look at yet. "
                "Run the preview, then deploy — or Skip the preview with a reason to deploy "
                "without one.")
    output = row["output"] if isinstance(row["output"], dict) else json.loads(row["output"] or "{}")
    # IS A PREVIEW SERVING RIGHT NOW — not "does the URL we wrote down still answer".
    #
    # The recorded `preview_url` is a loopback address on the API host, captured when the step
    # ran. Previews expire and are replaced, so that string goes stale by design: an operator
    # with the app open and working in another tab was told "the preview at
    # http://127.0.0.1:43383 does not answer, so it cannot have been reviewed", and the
    # release was held on the strength of a dead port number.
    #
    # `app_previews` holds the live fact. Ask it first, and about the app rather than this one
    # build — a later build of the same app serving a preview is still a preview somebody can
    # review, which is the same boundary the console's preview LINK already uses.
    live = _live_preview_for_run(connection, run_id)
    if live:
        return None
    url = output.get("preview_url") or output.get("url")
    if not url:
        return "the preview step finished without a URL, so nothing was previewable."
    if not _preview_answers(url):
        return ("no preview is running for this app right now, so it cannot have been "
                f"reviewed (the one this step started, {url}, has gone). Start a fresh "
                "preview, then deploy.")
    return None


def _preview_waived(row: Any) -> Optional[dict[str, Any]]:
    """Who chose to deploy without a preview, and why — or None.

    Some apps cannot be previewed (a service the preview host cannot provide, a target that
    only exists in the real environment), and the operator must still be able to ship. The
    choice is the operator's Skip on the preview step: a person, with a reason, recorded by
    `skip_failed_step`. A preview the SYSTEM skipped (refused, not applicable) is not a choice
    anyone made, so it still holds the deploy.
    """
    if row is None or row["status"] != "skipped":
        return None
    output = row["output"] if isinstance(row["output"], dict) else json.loads(row["output"] or "{}")
    if not output.get("skipped_by_operator") or not output.get("skipped_by"):
        return None
    return {"by": output.get("skipped_by"), "reason": output.get("skip_reason") or ""}


def _preview_waiver_for_run(connection: Connection, run_id: str) -> Optional[dict[str, Any]]:
    try:
        row = connection.execute(
            text("SELECT status, output FROM workflow_step_runs "
                 "WHERE workflow_run_id = :r AND step_id = 'preview' "
                 "ORDER BY created_at DESC LIMIT 1"),
            {"r": run_id},
        ).mappings().first()
    except Exception:  # noqa: BLE001
        log.exception("could not read the preview step for run %s", run_id)
        return None
    return _preview_waived(row)


def _live_preview_for_run(connection: Connection, run_id: str) -> Optional[str]:
    """The id of a preview RUNNING NOW for this workflow's app, or None.

    Matched through the code task the run built, widened to every build sharing that app's
    identity — `finalize_ctx->>'app_id'`, which hashes the project in, so it can never reach
    another project's app.
    """
    try:
        # EVERY BUILD OF THE RUN, not only the newest step's: pass 2 runs beside the deploy
        # (two_pass), so the newest code task is often the More tests pass, still working, which
        # has no preview of its own while pass 1's is serving.
        tasks = [str(t) for t in connection.execute(
            text("SELECT input->>'adopt_code_task_run_id' FROM workflow_runs WHERE id = :r "
                 "UNION SELECT output->>'code_task_run_id' FROM workflow_step_runs "
                 "WHERE workflow_run_id = :r AND output ? 'code_task_run_id'"),
            {"r": run_id},
        ).scalars() if t]
        if not tasks:
            return None
        return connection.execute(
            text("""
                SELECT p.id FROM app_previews p
                WHERE p.status = 'running'
                  AND (p.run_id = ANY(:t)
                       OR p.run_id IN (
                            SELECT c.id FROM code_task_runs c
                            WHERE NULLIF(c.finalize_ctx->>'app_id', '') IN (
                                SELECT NULLIF(finalize_ctx->>'app_id', '')
                                FROM code_task_runs WHERE id = ANY(:t))
                              AND NULLIF(c.finalize_ctx->>'app_id', '') IS NOT NULL))
                ORDER BY p.created_at DESC LIMIT 1
            """),
            {"t": tasks},
        ).scalar()
    except Exception:  # noqa: BLE001 — a check that cannot run must not block a release
        log.exception("could not read live previews for run %s", run_id)
        return None


def _preview_answers(url: Any, timeout: float = 6.0) -> bool:
    """Does anything reply on this URL right now?

    Deliberately generous: any status code is proof of life, and a timeout or refused
    connection is proof of the opposite. Errors are answered False rather than raised — a probe
    that cannot run must not adopt, because adopting is the risky direction.
    """
    import urllib.error
    import urllib.request

    if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
        return False
    try:
        with urllib.request.urlopen(url.strip(), timeout=timeout) as resp:
            return bool(resp.status)
    except urllib.error.HTTPError:
        return True          # it answered, and an error page is still something serving
    except Exception:        # noqa: BLE001 — refused, DNS, timeout: nothing is there
        return False


def _step_name(step: dict[str, Any]) -> str:
    name = str(step.get("title") or step.get("label") or step.get("name")
               or step.get("id") or "this step").strip()
    return name[:1].upper() + name[1:] if name else "This step"


def _optional_inputs_of(connection: Connection, project_id: str,
                        step: dict[str, Any]) -> Optional[frozenset[str]]:
    """The inputs a step can run without — the SAME rule `_execute_step` resolves them by.
    None when that cannot be told here, which the caller treats as "not provably optional"."""
    binding = step.get("binding") or {}
    wiring = binding.get("inputs") or {}
    if binding.get("agent_work"):
        return frozenset(k for k in wiring if k != "instructions")
    if binding.get("custom_step"):
        try:
            return frozenset(custom_steps.optional_inputs(
                connection, project_id, str(binding["custom_step"])))
        except Exception:  # noqa: BLE001 — unknown here; the step's own run will say
            return None
    key = str(binding.get("capability") or "")
    if key in CAPABILITY_KEYS:
        return frozenset(get_capability(key).optional_inputs or ())
    return None


def required_products(connection: Connection, project_id: str, step: dict[str, Any],
                      spec_steps: Optional[dict[str, dict[str, Any]]]) -> list[str]:
    """What an agent step MUST hand on: what it declares it requires (`binding.requires`), and
    every field a later step reads from it as a REQUIRED input.

    DERIVED FROM THE WIRING, so no workflow has to remember to declare it. The video example's
    publish step reads `media_artifact_id` from `edit`; an edit that rendered nothing has not
    produced it, and that is decided here, at the edit — before a publish gate can open over it.
    A reference with `override_from_input` is not required: the run's input may supply it.
    """
    step_id = str(step.get("id") or "")
    wanted: list[str] = [str(f).strip() for f in
                         ((step.get("binding") or {}).get("requires") or []) if str(f).strip()]
    for other in (spec_steps or {}).values():
        if not isinstance(other, dict) or str(other.get("id") or "") == step_id:
            continue
        wiring = (other.get("binding") or {}).get("inputs") or {}
        if not isinstance(wiring, dict):
            continue
        optional: Optional[frozenset[str]] = None
        for name, ref in wiring.items():
            if not (isinstance(ref, dict) and str(ref.get("from_step") or "") == step_id):
                continue
            field = str(ref.get("field") or "").strip()
            if not field or ref.get("override_from_input") or field in wanted:
                continue
            if optional is None:
                optional = _optional_inputs_of(connection, project_id, other) or frozenset()
            if name not in optional:
                wanted.append(field)
    return wanted


def _judged_agent_step(connection: Connection, project_id: str, step: dict[str, Any],
                       spec_steps: Optional[dict[str, dict[str, Any]]],
                       output: dict[str, Any]) -> dict[str, Any]:
    """An agent step's output with its verdict applied (`agent_steps.verdict`).

    The agent loop finishing says nothing about whether the WORK was done. A step whose agent
    declared failed or blocked, or that is missing a product a later step needs, comes back as
    status failed/blocked with its one-line reason in `error` — which the runner records as a
    failed step, stopping the run with Retry / Skip. The full report stays in `summary`.
    """
    from . import agent_steps

    judged = agent_steps.verdict(
        output, required=required_products(connection, project_id, step, spec_steps),
        step_name=_step_name(step))
    output = {**output, "agent_outcome": {k: judged[k] for k in
                                          ("outcome", "reason", "needs", "declared", "missing")}}
    if judged["outcome"] == agent_steps.COMPLETED:
        return output
    return {**output, "status": judged["outcome"], "error": judged["error"],
            SETTLED_FAILURE: True,
            **({"needs": judged["needs"]} if judged["needs"] else {})}


def execute_step(
    connection: Connection,
    ctx: Any,
    *,
    project_id: str,
    actor: str,
    step: dict[str, Any],
    context: dict[str, Any],
    spec_steps: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Perform one capability. Returns the step's output.

    Raises CapabilityExecutionError with a sentence an operator can act on — the step records
    it, and "step failed" with no reason is the thing this exists to avoid.

    Every kind of step goes through here, so this is where the skipped-input markers that
    `_resolve_inputs` recorded are written onto the output: whichever branch ran, the record of
    this step says which of its inputs were missing because an operator skipped their source.
    """
    context = dict(context)
    context.pop(SKIPPED_INPUTS, None)
    # A run started from a conversation's start card runs each step as the agent the card named
    # (step_agents.with_run_agent says which steps a card may fill).
    step = step_agents.with_run_agent(connection, project_id, step, context.get("input"))
    output = _execute_step(connection, ctx, project_id=project_id, actor=actor,
                           step=step, context=context)
    if (step.get("binding") or {}).get("agent_work") and isinstance(output, dict) \
            and str(output.get("status") or "") != "awaiting":
        output = _judged_agent_step(connection, project_id, step, spec_steps, output)
    skipped = context.get(SKIPPED_INPUTS)
    if skipped and isinstance(output, dict):
        output = {**output, SKIPPED_INPUTS: skipped}
    return output


def _execute_step(
    connection: Connection,
    ctx: Any,
    *,
    project_id: str,
    actor: str,
    step: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    binding = step.get("binding") or {}

    # SKIPPED AT START, BY A PERSON, ON THE RECORD. The start door's pre-check let the operator
    # skip a step it said would fail (workflow_precheck.gate_start refuses a skip of a gate that
    # protects a deploy); the run's input carries who and why, written by the server only.
    from .workflow_precheck import skipped_by_precheck

    skip = skipped_by_precheck(context, str(step.get("id") or ""))
    if skip:
        return {"capability": str(binding.get("capability") or step.get("kind") or ""),
                "tool": None, "status": "not_applicable", "skipped_by_precheck": skip,
                "note": (f"skipped at start by {skip.get('by') or 'the operator'}: "
                         f"{skip.get('reason') or 'no reason given'}")[:600]}

    # APPROVING SENDS EXACTLY WHAT WAS PREVIEWED. A step a gate released checks that its
    # inputs still hash to the content the approver saw; if an upstream step was re-run in
    # between, it refuses rather than send words nobody approved.
    stale = _approved_content_refusal(connection, project_id, step, context)
    if stale:
        raise StaleApprovalError(stale)

    # A STEP THAT SAYS WHAT IT WANTS AND LETS AN AGENT WORK OUT HOW.
    #
    # Checked before everything else because it binds nothing: no capability key, no custom step,
    # no skill. Its whole specification is a sentence, and the agent's granted skills are the
    # vocabulary. This is what stops the product growing a special case per customer — an
    # instruction asking for something new needs the agent to HOLD a skill, not BOSS to ship a
    # step. See services/agent_steps.py.
    if binding.get("agent_work"):
        from . import agent_steps, step_agents

        resolved = _resolve_inputs(
            binding.get("inputs") or {}, context,
            # ONLY `instructions` IS REQUIRED. Everything else a step passes an agent is context
            # that may or may not exist on a given run, and a framework step cannot know which.
            frozenset(k for k in (binding.get("inputs") or {}) if k != "instructions"))
        said = str(resolved.pop("instructions", "") or "").strip()
        if not said:
            raise CapabilityExecutionError(
                "instructions are required — this step's whole specification is what you want "
                "done. Fill in the instructions when you start the run, or pin them on the step.")
        settings = getattr(ctx, "settings", None)
        skills, mcp = step_agents.step_scope(step)
        try:
            # WHO, decided by the one rule every step kind uses: the agent the step names (the
            # same `captain` field as everywhere else), otherwise the agent holding what the
            # step is scoped to, otherwise BOSS's pick among the agents that could do it.
            ran_as = step_agents.resolve(connection, settings, project_id, step,
                                         actor=actor, instructions=said)
            # The agent's turns and tool calls run under the run's recorded authority: a run
            # token for THIS agent acting for the person who started the run.
            with _step_authority(ctx, connection, project_id, context, step, ran_as["agent"]):
                outcome = agent_steps.run(
                    connection, settings, project_id,
                    instructions=said,
                    role_key=ran_as["agent"],
                    chosen=ran_as,
                    context=resolved,
                    actor=actor,
                    max_steps=(int(binding["max_steps"]) if binding.get("max_steps") else None),
                    # Preserve explicit outputs that the next step wires to.
                    produces=list(binding.get("produces") or []) or None,
                    skills=skills or None,
                    mcp=mcp or None,
                    # Where "Why this model" finds the route (model router): the run and the step.
                    route_ref={"workflow_run_id": str(context.get("run_id") or ""),
                               "step_id": str(step.get("id") or "")},
                )
        except (agent_steps.AgentStepError, step_agents.StepAgentError) as error:
            raise CapabilityExecutionError(str(error)) from error
        if isinstance(outcome.get("ran_as"), dict):
            outcome["ran_as"]["considered"] = ran_as.get("considered") or []
        # THE AGENT STOPPED FOR THE PER-MINUTE BUDGET, NOT ON THE WORK. Its tool calls were
        # refused by a cap that clears by itself, waited out as long as a call may wait
        # (agent_loop.BudgetWaiter), and the run ended `budget_wait` instead of letting the
        # agent declare itself blocked. The step waits and runs again (workflows.wait_step);
        # it is neither a failure nor anything a person has to decide.
        if str(outcome.get("status") or "") == agent_steps.BUDGET_WAIT:
            from .model_failures import explain

            budget = outcome.get("budget") or {}
            said = explain(str(budget.get("message") or "burst cap"))
            if said.get("kind") != "budget_wait":
                said = explain("burst cap")
            raise CapabilityWaitError(
                said["headline"],
                wait_seconds=int(outcome.get("retry_after_seconds")
                                 or budget.get("retry_after_seconds") or 30),
                kind="budget_wait")
        # SUSPENDED IS NOT DONE.
        #
        # The agent loop stops on a skill above the autonomous risk floor and returns
        # status `suspended` with a decision id. The runner only parks `awaiting` and
        # `needs_decision`. Everything else, including `suspended`, was recorded as
        # succeeded — so a step that reached `code.write_and_run` or a tier-3 publish
        # went green, the next step ran on whatever the agent had produced *before*
        # the approval, and approving the decision later executed the skill anyway,
        # after the workflow had moved on.
        if str(outcome.get("status") or "") == "suspended":
            waited = str(outcome.get("agent_run_id") or "").strip()
            if not waited:
                raise CapabilityExecutionError(
                    "the agent stopped for a person's approval but did not record which "
                    "run to resume, so this step cannot wait for it."
                )
            return {
                "kind": "agent_work",
                **outcome,
                "status": "awaiting",
                "await": {"kind": "agent_run", "id": waited},
            }
        return {"kind": "agent_work", **outcome}

    # A question with its answers named up front — see services/typed_decisions.py. It binds no
    # capability either, so it is checked before the capability lookup for the same reason.
    if binding.get("typed_decision"):
        from . import typed_decisions

        from . import project_settings

        state = _resolve_inputs(binding.get("inputs") or {}, context).get("state")
        settings = getattr(ctx, "settings", None)
        if connection is not None and settings is not None:
            # The project's own choice of decision model, set in the console.
            settings = project_settings.applied(connection, project_id, settings)
        spend = None
        engine = getattr(connection, "engine", None)
        if (
            settings is not None
            and engine is not None
            and project_id
            and context.get("run_id")
            and step.get("id")
        ):
            from .typed_decision_spend import TypedDecisionSpend

            spend = TypedDecisionSpend.for_runner(
                settings,
                engine=engine,
                project_id=project_id,
                run_id=str(context["run_id"]),
                step_id=str(step["id"]),
                attempt=int(context.get("step_attempt") or 1),
                actor=actor,
                worker_id=str(context.get("worker_id") or actor),
            )
            spend.knowledge_precedent = _knowledge_precedent(
                connection, project_id, state, str(context["run_id"]), str(step["id"]), actor
            )
        try:
            answered = typed_decisions.run_step(settings, binding, state, spend=spend)
            if isinstance(answered, dict):
                # WHO ANSWERED, in the shape every other step records. No agent: a typed
                # decision is put to the project's decision model, with its answers fixed.
                answered = {**answered, "ran_as": {
                    "agent": "decision model", "how": "decision_model",
                    "why": "a typed decision is answered by the project's decision model from "
                           "the options the step names; no agent runs it",
                    "considered": [], "skills_offered": [], "skills_used": []}}
            return answered
        except typed_decisions.TypedDecisionGovernanceHold as error:
            # No reservation, lease, and receipt on this call. Settled, not retried
            # into a provider. fail_step records it and emits its audit event.
            raise SettledCapabilityError(str(error)) from error
        except Exception as error:
            from .typed_decision_spend import (
                TypedDecisionReconciliationRequired,
                TypedDecisionSpendError,
            )

            if isinstance(error, TypedDecisionReconciliationRequired):
                raise SettledCapabilityError(str(error)) from error
            if isinstance(error, TypedDecisionSpendError):
                raise CapabilityExecutionError(str(error)) from error
            # The step records the sentence. A provider fault is retryable; a
            # reconciliation is settled above so it cannot spend twice.
            raise CapabilityExecutionError(f"typed decision failed: {error}") from error

    # A step the customer defined. Checked first because it is the only kind that has no
    # capability key at all — falling through to the capability lookup would report it as an
    # unknown capability, which is true and useless.
    if binding.get("custom_step"):
        from . import step_agents

        try:
            # WHO RUNS IT — the named agent, or the agent that holds the skill or tool. It used
            # to be the anonymous `workflow_runner`, granted the skill on the fly by widening
            # its allowed_callers; a step is not a way to grant anything.
            ran_as = step_agents.resolve(
                connection, getattr(ctx, "settings", None), project_id, step, actor=actor)
            # THE STEP'S OWN OPTIONAL INPUTS ARE OPTIONAL HERE TOO. Without this every
            # `from_input` was mandatory, so a workflow offering "an image to overlay, if you
            # have one" failed the moment somebody left it blank — and the message named a
            # missing input the form had presented as optional.
            return custom_steps.execute(
                connection, getattr(ctx, "settings", None),
                project_id=project_id, actor=actor, step=step,
                inputs=_resolve_inputs(
                    binding.get("inputs") or {}, context,
                    custom_steps.optional_inputs(
                        connection, project_id, str(binding["custom_step"]))),
                run_id=str(context.get("run_id") or ""),
                step_index=int(context.get("step_index") or 0),
                # The gate decision that released this step, if it had one. The MCP effect
                # checks it is APPROVED before an unreviewed server's tool runs.
                approved_by=context.get("approval_decision_id"),
                ran_as=ran_as,
            )
        except step_agents.StepAgentError as error:
            raise CapabilityExecutionError(str(error)) from error
        except custom_steps.CustomStepWait as error:
            # Not yet, and it clears by itself: the step waits rather than fails.
            raise CapabilityWaitError(str(error), wait_seconds=error.wait_seconds,
                                      kind=error.kind) from error
        except custom_steps.CustomStepError as error:
            # Translated, not wrapped: the runner's failure path records
            # CapabilityExecutionError, and the customer's sentence is the part worth keeping.
            # A refusal only a person can lift is settled: one stop, with Retry / Skip.
            if getattr(error, "settled", False):
                raise SettledCapabilityError(str(error)) from error
            raise CapabilityExecutionError(str(error)) from error

    key = str(binding.get("capability") or "")
    if key not in CAPABILITY_KEYS:
        raise CapabilityExecutionError(f"unknown capability '{key}'.")
    capability = get_capability(key)

    if capability.is_gate:
        # Except when the run already HAS the answer — `_gate_subject` declined to open a gate,
        # so the step arrived here as ordinary work. Record the approval that was given and move
        # on; the audit trail names the decision, so "who approved this" is still answerable.
        if _no_prd_to_approve(key, context):
            return workflow_outcomes.apply(key, {
                "capability": key, "tool": None, "status": "not_applicable",
                "note": "No PRD to approve — this build was started as a direct change.",
            }, {})
        if key == "security_decide" and not security_review_steps.has_choices(
                connection, project_id, context):
            return workflow_outcomes.apply(key, security_review_steps.not_applicable(
                key, "Nothing to choose: the scan found nothing to fix."), {})
        adopted_gate = _adopted_gate(connection, key, context)
        if adopted_gate:
            return workflow_outcomes.apply(key, {
                "capability": key,
                "tool": None,
                "status": "adopted",
                "note": (
                    f"'{capability.label}' was answered before this run started "
                    f"({adopted_gate['decision_status']})."
                ),
                **adopted_gate,
            }, {})
        # Otherwise: gates are turned into decisions before they are claimable.
        raise CapabilityExecutionError(
            f"'{capability.label}' waits for a person and is not executable work."
        )

    owned = _owned_by_change_set(key, context)
    if owned:
        return workflow_outcomes.apply(key, owned, {})

    inputs = _resolve_inputs(binding.get("inputs") or {}, context,
                             optional=frozenset(capability.optional_inputs or ()))
    inputs = _fill_optional_from_run_input(capability, inputs, context)

    # EVERY CAPABILITY'S RESULT IS JUDGED BY ITS OWN VERDICT (services/workflow_outcomes.py).
    #
    # Whether work happened used to be decided by generic checks — a `status` in a short list,
    # or `ok: false` — and real tools do not all speak that way: a smoke test of an app that was
    # down went green because `http_probe` reports `{"status": 503}`, and a deploy went green on
    # `{"status": "planned"}` before anything had shipped. Each capability now says, in one
    # table beside the catalogue, what success, failure and "still going" look like for it.
    return workflow_outcomes.apply(
        key,
        _perform_capability(connection, ctx, project_id=project_id, actor=actor, step=step,
                            context=context, key=key, capability=capability, inputs=inputs),
        inputs,
    )


#: What a multi-repo change set does itself, for every repository at once (multirepo/
#: change_sets.py): preview and readiness repairs per build, the deploy with its one rollback. A
#: run whose build was a change set does not do them again per build — there is no single build.
_SET_OWNED = frozenset({"preview", "plrr", "more_tests", "deploy", "redeploy", "retry_build"})


def _owned_by_change_set(key: str, context: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if key not in _SET_OWNED or not context:
        return None
    cs = _from_any_step(context, "change_set_id")
    if not cs:
        return None
    words = {"deploy": "deployed together by the change set, in dependency order, with one rollback",
             "redeploy": "deployed together by the change set",
             "preview": "reviewed as a set: linked pull requests and the cross-repo check",
             "plrr": "checked per repository by each lane's own gates",
             "more_tests": "tested per repository by each lane's own build",
             "retry_build": "retried per repository from the change set's lanes"}[key]
    return {"capability": key, "tool": None, "status": "not_applicable", "change_set_id": cs,
            "note": f"A change across several repositories: {words}."}


def _notification_text(context: dict[str, Any], message: Any) -> tuple[str, str]:
    """The subject and body of a notify step: what the run is, and what it produced.

    The step was given the run's title and nothing else, which is a name and not news. What a
    reader wants is where the thing is — the deployed URL, the pull request, the preview — and
    those are already in the run, so the body is assembled from them.
    """
    title = str(message or (context.get("input") or {}).get("title") or "").strip()
    workflow = str(context.get("workflow_name") or "Workflow").strip()
    subject = f"{workflow}: {title}"[:180] if title else workflow
    lines = [subject, ""]
    for label, field in (("Live at", "deployed_url"), ("Preview", "preview_url"),
                         ("Pull request", "pull_request_url"), ("Record", "document_id")):
        value = _from_any_step(context, field)
        if value:
            lines.append(f"{label}: {value}")
    lines.append(f"Run: {context.get('run_id')}")
    return subject, "\n".join(lines)


def _notify(
    connection: Connection, ctx: Any, project_id: str, step: dict[str, Any],
    context: dict[str, Any], inputs: dict[str, Any],
) -> dict[str, Any]:
    """Send the notification, skip it visibly, or fail saying why — never a green no-op.

    Three catalogue templates ended in a notify that recorded `status: recorded` and went green
    having told nobody, and Deprecation / Sunset's announcement — the point of that workflow —
    went nowhere before a seven-day wait (boss-1 map, 2026-10-02).

    ONE SEND PER STEP. A retried step that already reached its channel must not send again, so a
    successful send is recorded as an event keyed on run, step and channel, and found first.
    """
    channel = str(inputs.get("channel") or "").strip()
    if not channel:
        return {"capability": "notify", "status": "not_applicable", "posted": False,
                "note": ("Nobody was told: this run was given no channel. Start the next one "
                         "with 'Who to tell' set to email:someone@example.com or "
                         "mattermost:<channel id>.")}
    run_id = str(context.get("run_id") or "")
    step_id = str(step.get("id") or "")
    earlier = connection.execute(
        text("SELECT data FROM events WHERE project_id = :p AND event_type = 'workflow.notified' "
             "AND entity_id = :r AND data->>'step_id' = :s AND data->>'channel' = :c "
             "ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "r": run_id, "s": step_id, "c": channel},
    ).mappings().first()
    if earlier:
        data = earlier["data"] if isinstance(earlier["data"], dict) else {}
        return {"capability": "notify", "status": "succeeded", "posted": True,
                "channel": channel, "receipt": data.get("receipt"), "already_sent": True}

    subject, body = _notification_text(context, inputs.get("message"))
    subject = str(inputs.get("subject") or subject)
    from ..config import get_settings
    from .github_adapter import IntegrationConfigurationError

    settings = getattr(ctx, "settings", None) or get_settings()
    kind = channel.split(":", 1)[0].lower()
    try:
        if kind == "email":
            from .mail_transport import send

            receipt = send(channel, subject, body, connection=connection, settings=settings,
                           project_id=project_id)
        elif kind == "mattermost":
            from .mattermost_adapter import MattermostAdapter

            receipt = MattermostAdapter(settings).post_message(channel, subject, body)
        else:
            return {"capability": "notify", "status": "failed", "posted": False,
                    "channel": channel,
                    "error": (f"'{channel}' is not a channel BOSS can post to. Use "
                              "email:someone@example.com or mattermost:<channel id>, then "
                              "retry this step — or skip it.")}
    except IntegrationConfigurationError as error:
        need = "email" if kind == "email" else "mattermost"
        from .workflow_readiness import need_for

        return {"capability": "notify", "status": "failed", "posted": False, "channel": channel,
                "error": f"Could not tell {channel}: {error} {need_for(need).fix} Then retry "
                         "this step, or skip it."}
    except Exception as error:  # noqa: BLE001 — a refused send is the step's result, not a crash
        return {"capability": "notify", "status": "failed", "posted": False, "channel": channel,
                "error": f"Could not tell {channel}: {type(error).__name__}: "
                         f"{str(error)[:300]}. Retry this step once it is reachable, or skip it."}
    emit_event(connection, project_id, "workflow.notified", "workflow", "workflow_run", run_id,
               {"step_id": step_id, "channel": channel, "receipt": receipt, "subject": subject})
    return {"capability": "notify", "status": "succeeded", "posted": True, "channel": channel,
            "receipt": receipt, "subject": subject}


def _start_ci_wait(connection: Connection, project_id: str, context: dict[str, Any],
                   inputs: dict[str, Any]) -> dict[str, Any]:
    """`wait_for_ci`: record a wait on the pull request head's checks and park on it.

    The pull request is the step's input, else the build's (`pull_request_url` from the step
    that produced one). Its head commit is read from GitHub unless `head_sha` is given."""
    from ..config import get_settings
    from . import ci_gate

    settings = get_settings()
    url = str(inputs.get("pull_request_url") or _from_any_step(context, "pull_request_url") or "")
    sha = str(inputs.get("head_sha") or "").strip()
    parsed = ci_gate.parse_pr_url(url)
    repository = parsed[0] if parsed else ""
    try:
        reader = ci_gate.reader_for_project(connection, settings, project_id, repository or None)
        repository = repository or reader.repository
        if not sha and parsed:
            sha = str(((reader.pull_request(parsed[1]) or {}).get("head") or {}).get("sha") or "")
        if not sha:
            return {"capability": "wait_for_ci", "status": "failed",
                    "error": "There is no pull request or commit to wait for CI on: give the "
                             "step a pull_request_url, or put it after the build."}
        minutes = inputs.get("timeout_minutes")
        timeout = (int(float(minutes) * 60) if minutes not in (None, "")
                   else int(getattr(settings, "ci_wait_timeout_seconds", 0) or 0)
                   or ci_gate.DEFAULT_TIMEOUT_SECONDS)
        wait = ci_gate.start_wait(connection, project_id=project_id, repository=repository,
                                  head_sha=sha, pull_request_url=url or None,
                                  timeout_seconds=timeout, reader=reader,
                                  workflow_run_id=str(context.get("run_id") or "") or None,
                                  actor="workflow")
        wait = ci_gate.poll_wait(connection, wait["id"], reader, force=True)
    except ci_gate.CiSettingError as error:
        return {"capability": "wait_for_ci", "status": "failed", "error": error.message}
    return {"capability": "wait_for_ci", "tool": None, "status": "awaiting",
            "await": {"kind": "ci_wait", "id": wait["id"]}, "ci_wait_id": wait["id"],
            "ci_status": wait["status"], "note": wait["summary"] or "waiting for CI"}


def _look_at_ci(connection: Connection, project_id: str, wait_id: str) -> None:
    """One look at a parked CI wait, when due. Its own savepoint: a failed read never poisons
    the tick's transaction."""
    from ..config import get_settings
    from . import ci_gate

    point = connection.begin_nested()
    try:
        repo = connection.execute(text("SELECT repository FROM ci_waits WHERE id = :i"),
                                  {"i": wait_id}).scalar()
        try:
            reader = ci_gate.reader_for_project(connection, get_settings(), project_id, repo)
        except Exception as error:  # noqa: BLE001 — GitHub unreachable: the deadline still holds
            reader = ci_gate.UnreadableCi(str(error))
        ci_gate.poll_wait(connection, wait_id, reader)
        point.commit()
    except Exception as error:  # noqa: BLE001 — looked at again next tick; the deadline holds
        point.rollback()
        log.warning("cannot look at CI wait %s: %s", wait_id, error)


#: What an incident's fix is held to, when no person wrote other criteria.
INCIDENT_FIX_CRITERIA = (
    "A regression test reproduces the incident's failing requests and FAILS on the faulty "
    "release; it passes with the fix.",
    "The change is the smallest that fixes the root cause; nothing unrelated is changed.",
    "The pull request description states the root cause, the regression test and the fix.",
)


def _incident_id(context: dict[str, Any], inputs: dict[str, Any]) -> str:
    return str(inputs.get("incident_id") or (context.get("input") or {}).get("incident_id")
               or "").strip()


def _investigate_incident(connection: Connection, ctx: Any, project_id: str,
                          context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    """`investigate_incident`: metrics x logs x release diff -> the fix brief. Records this run
    as the incident's response, so the alert firing again starts no second one."""
    from . import incident_response

    incident_id = _incident_id(context, inputs)
    if not incident_id:
        raise SettledCapabilityError("There is no incident to investigate: start this workflow "
                                     "from an incident (its incident_id).")
    try:
        got = incident_response.investigate(connection, getattr(ctx, "settings", None)
                                            or _settings(), project_id, incident_id)
    except (LookupError, ValueError) as refused:
        raise SettledCapabilityError(str(refused)) from refused
    run_id = str(context.get("run_id") or "")
    if run_id:
        incident_response._merge_ctx(connection, incident_id, {"response": {
            "workflow_run_id": run_id, "started_at": incident_response._iso(
                incident_response._now())}})
    return {"capability": "investigate_incident", "tool": None, "status": "succeeded",
            **{k: got[k] for k in ("incident_id", "title", "incident_brief", "root_cause",
                                   "suspect_commit", "extends_run_id")},
            "serving_commit_sha": got.get("serving_commit_sha"),
            "basis": got["hypothesis"]["basis"], "confidence": got["hypothesis"]["confidence"],
            "note": f"{got['logs_word']}; {got['diff_word']}. {got['root_cause']}"}


def _set_up_monitoring(connection: Connection, ctx: Any, project_id: str,
                       context: dict[str, Any], step: dict[str, Any],
                       inputs: dict[str, Any]) -> dict[str, Any]:
    """`monitoring_pack`: the SRE agent's pack before the release. No pack fails the step under
    `monitoring_gate: block` (the release cannot ship without one) and is said, without
    failing the run, under `warn`."""
    from . import monitoring_pack

    run_id = str(context.get("run_id") or "")
    gate = monitoring_pack.run_gate(connection, run_id)
    try:
        return {**monitoring_pack.run_step(
            connection, getattr(ctx, "settings", None) or _settings(), project_id,
            run_id=run_id, step_id=str(step.get("id") or ""), inputs=inputs, gate=gate),
            "gate": gate}
    except monitoring_pack.PackRefused as refused:
        if gate == monitoring_pack.WARN:
            return {"capability": "monitoring_pack", "tool": None, "status": "not_applicable",
                    "gate": gate,
                    "note": f"No monitoring pack: {refused}. The workflow's monitoring_gate is "
                            "warn, so the release goes on without one."}
        raise SettledCapabilityError(
            f"No monitoring pack: {refused}. The release cannot ship without one "
            "(monitoring_gate: block).") from refused


def _settings() -> Any:
    from ..config import get_settings

    return get_settings()


def _start_recovery_check(connection: Connection, project_id: str, context: dict[str, Any],
                          inputs: dict[str, Any]) -> dict[str, Any]:
    """`verify_recovery`: judged from the release the run shipped. Its clock starts when that
    deploy finished (the step after it), never before — a window that reached back into the
    incident would read the failures as the fix's."""
    from . import incident_response

    incident_id = _incident_id(context, inputs)
    if not incident_id:
        return {"capability": "verify_recovery", "status": "failed",
                "error": "There is no incident to verify: start this workflow from one."}
    since = None
    sdep = _from_any_step(context, "stack_deployment_id")
    if sdep:
        since = connection.execute(text(
            "SELECT COALESCE(finished_at, updated_at) FROM stack_deployments WHERE id = :i"),
            {"i": str(sdep)}).scalar()
    try:
        check = incident_response.start_recovery_check(
            connection, project_id, incident_id, since=since,
            workflow_run_id=str(context.get("run_id") or "") or None,
            stable_minutes=float(inputs.get("stable_minutes")
                                 or incident_response.DEFAULT_STABLE_MINUTES),
            timeout_minutes=float(inputs.get("timeout_minutes")
                                  or incident_response.DEFAULT_RECOVERY_TIMEOUT_MINUTES))
    except (LookupError, ValueError) as refused:
        return {"capability": "verify_recovery", "status": "failed", "error": str(refused)}
    return {"capability": "verify_recovery", "tool": None, "status": "awaiting",
            "await": {"kind": "recovery_check", "id": check["id"]},
            "recovery_check_id": check["id"], "recovery_status": check["status"],
            "recovery_summary": check["summary"], "note": check["summary"]}


def _look_at_recovery(connection: Connection, check_id: str) -> None:
    """One look at a parked recovery check, in its own savepoint (like _look_at_ci)."""
    from . import incident_response

    point = connection.begin_nested()
    try:
        incident_response.look_at_recovery(connection, check_id)
        point.commit()
    except Exception as error:  # noqa: BLE001 — looked at again next tick; the deadline holds
        point.rollback()
        log.warning("cannot look at recovery check %s: %s", check_id, error)


def _write_the_incident_report(connection: Connection, ctx: Any, project_id: str,
                               context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    """`incident_report`: the timeline, root cause and cost, stored with the incident."""
    from . import incident_response

    incident_id = _incident_id(context, inputs)
    if not incident_id:
        raise SettledCapabilityError("There is no incident to report on.")
    rep = incident_response.report(connection, project_id, incident_id,
                                   workflow_run_id=str(context.get("run_id") or "") or None)
    told = incident_response.tell_reporters(connection, getattr(ctx, "settings", None)
                                            or _settings(), project_id, incident_id, rep)
    return {"capability": "incident_report", "tool": None, "status": "succeeded",
            "report_markdown": rep["markdown"],
            "minutes_to_recover": rep.get("minutes_to_recover"),
            "cost_billed_usd": rep["cost"]["billed_usd"], "reporters_told": told,
            "note": (f"Recovered in {rep['minutes_to_recover']} min" if
                     rep.get("minutes_to_recover") else "Not recovered yet")
                    + f"; billed ${rep['cost']['billed_usd']:.2f}."}


def _existing_app_note(connection: Connection, project_id: str, run_id: str) -> str:
    """What a panel must know when the run builds onto an existing app; "" when it does not."""
    if not run_id:
        return ""
    from .code_task_service import load_code_task_run, work_branch_for
    from .factory_view import app_name

    run = load_code_task_run(connection, run_id)
    if not run or str(run.get("project_id")) != str(project_id):
        return ""
    return (f"This is a change to an EXISTING, working app, {app_name(run)!r}, whose code is on "
            f"branch {work_branch_for(run)}. Write a PRD for this feature only: do not "
            "re-scaffold the app and do not redesign it. Its existing behaviour and its "
            "existing tests must keep passing.")


#: Tool-less capabilities that start work on the build's branch (extend_code_task): they take
#: the builder's authority themselves, in _start_on_the_branch.
_ON_THE_BRANCH = frozenset({"plrr", "more_tests"})


def _perform_toolless(
    connection: Connection,
    ctx: Any,
    *,
    project_id: str,
    actor: str,
    step: dict[str, Any],
    context: dict[str, Any],
    key: str,
    capability: Any,
    inputs: dict[str, Any],
    extend: bool = False,
) -> dict[str, Any]:
    """A capability with no tool of its own (or a build with `extend`), dispatched in-process.
    Called by `_perform_capability` under the run's authority, never bare."""
    if extend:
        return _build_on_the_branch(connection, ctx, project_id=project_id, actor=actor,
                                    step=step, context=context, capability=capability,
                                    inputs=inputs)
    # `document` has no tool because it commissions nothing — it WRITES, from what the run
    # already produced. Falling through to the no-op below made the one step whose entire
    # job is to leave an artefact behind the only step that left nothing.
    # PRODUCTION LAUNCH READINESS, WHICH HAS NEVER RUN.
    #
    # `plrr` declares `tool=None` -- it has no single tool, because it reads the run's own
    # spec and then starts the repair that closes what the app is missing. Its dispatch sat
    # AFTER this branch, which returns unconditionally, so it was unreachable from the day
    # it was written. Every run recorded the step as:
    #
    #     "'Production launch readiness' is recorded from the work it accompanies."
    #
    # started and completed in the same second, green, having done nothing. It is declared
    # to produce `findings`, `gaps_closed` and `code_task_run_id` and produced none of
    # them, so the one step that exists to ask whether anyone can operate the thing at 3am
    # was the step that never ran.
    if key == "plrr":
        # NOT a bare return. This branch exits before the shared "started is not finished"
        # path below, and plrr's tool result is `status: extending` with the new run id
        # nested under `result`. Completing the step there lets preview and deploy run
        # while the repair is still writing the same branch.
        started = _plrr_step(ctx, connection, project_id, actor, context, step)
        if started.get("status") == "not_applicable":
            return started
        return _settle_started_work(capability, key, started)
    if key == "more_tests":
        return _more_tests_started(ctx, connection, project_id=project_id, actor=actor,
                                   context=context, step=step, capability=capability,
                                   inputs=inputs)
    if key == "document":
        return _write_the_record(connection, project_id, context, inputs)
    if key == "architecture":
        # THE ARCHITECT DESIGNS FROM THE APPROVED PRD, THEN A PERSON SIGNS IT OFF. The step
        # waits on its review (services/architecture_docs.py) like a build waits on its task.
        return _settle_started_work(
            capability, key,
            _architecture_step(connection, ctx, project_id, step, context, inputs))
    if key == "notify":
        return _notify(connection, ctx, project_id, step, context, inputs)
    if key == "wait_for_ci":
        return _settle_started_work(
            capability, key, _start_ci_wait(connection, project_id, context, inputs))
    # INCIDENT RESPONSE (services/incident_response.py): the investigation and the report read
    # what is recorded; the recovery check parks on BOSS's own monitor like a CI wait.
    if key == "investigate_incident":
        return _investigate_incident(connection, ctx, project_id, context, inputs)
    # SET UP MONITORING (services/monitoring_pack.py): Beacon's pack, written on the branch.
    if key == "monitoring_pack":
        return _set_up_monitoring(connection, ctx, project_id, context, step, inputs)
    if key == "verify_recovery":
        return _settle_started_work(
            capability, key, _start_recovery_check(connection, project_id, context, inputs))
    if key == "incident_report":
        return _write_the_incident_report(connection, ctx, project_id, context, inputs)
    # Same shape as `document`: no tool because it commissions nothing — it READS what the
    # run has already recorded and leaves a document behind.
    # THE RE-REVIEW AND THE SECURITY REPORT (services/security_review_steps.py).
    if key == "security_rereview":
        return _security_rereview(connection, ctx, project_id, context, inputs)
    if key == "security_report":
        return _security_report(connection, ctx, project_id, context, inputs)
    if key == "report":
        # A REPORT THAT WAS NOT WRITTEN IS A STEP THAT FAILED.
        #
        # This caught every exception and returned `status: recorded`, so the step went
        # green with no report behind it — the same shape as research recorded succeeded
        # after it died (boss-1, 2026-10-02). The reasoning was that a missing report is no
        # reason to stop a build that succeeded; that is a fair call, and it is the
        # OPERATOR'S call, made with "Skip and proceed", not one the runner makes by
        # pretending the report exists.
        return _write_the_run_report(connection, project_id, context)
    # OBSERVED, NOT STARTED. `review` and `test` do not commission anything — the governed
    # pipeline already reviewed and tested the build, and recorded the verdicts. Reporting
    # "recorded from the work it accompanies" left three of the nine steps saying nothing
    # about work that had definitely happened and was definitely written down.
    observed = _observed_from_the_build(connection, key, project_id, context)
    if observed is not None:
        return observed
    # Capabilities with no tool yet are honest no-ops that say so, rather than silently
    # succeeding as though the work had happened.
    return {
        "capability": key,
        "status": "recorded",
        "note": f"'{capability.label}' is recorded from the work it accompanies.",
        "inputs": inputs,
    }


def _perform_capability(
    connection: Connection,
    ctx: Any,
    *,
    project_id: str,
    actor: str,
    step: dict[str, Any],
    context: dict[str, Any],
    key: str,
    capability: Any,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Do the work of catalogue capability `key`, with its inputs already resolved. What it
    returns is judged by `workflow_outcomes` before the runner records anything."""
    # THE TWO-PASS RULE'S OWN INPUTS (two_pass): said to BOSS, never passed to a tool.
    inputs = dict(inputs or {})
    advisory_preview = key == "preview" and bool(inputs.pop("advisory", False))
    pass_2_of = str(inputs.pop("pass_2_of", "") or "") if key == "deploy" else ""
    monitoring_step = str(inputs.pop("monitoring_step", "") or "") if key == "deploy" else ""
    extend = key == "build" and bool(inputs.pop("extend", False))
    increment = inputs.pop("increment", None) if key == "build" else None
    if increment:
        # INCREMENTAL BUILD: this build builds one increment of the plan, said first.
        from .workflow_catalogue import increment_brief

        told = increment_brief(int(increment))
        inputs["instructions"] = (told if extend else
                                  f"{told}\n\n{inputs.get('instructions') or ''}".strip())
    if advisory_preview:
        return _advisory_preview(connection, ctx, project_id=project_id, actor=actor,
                                 step=step, context=context, capability=capability,
                                 inputs=inputs)
    if pass_2_of:
        why_not = pass_two_redeploy_refusal(connection, str(context.get("run_id") or ""),
                                            step, pass_2_of)
        if why_not:
            return {"capability": key, "tool": None, "status": "not_applicable",
                    "pass_2_of": pass_2_of, "note": why_not}
    if monitoring_step:
        # THE RELEASE CARRIES ITS MONITORING PACK (monitoring_pack): under `block` it does not
        # ship without one, in words, before anything is planned.
        from . import monitoring_pack

        run_id = str(context.get("run_id") or "")
        refused = monitoring_pack.release_refusal(connection, run_id, step,
                                                  monitoring_pack.run_gate(connection, run_id))
        if refused:
            raise SettledCapabilityError(refused)
    if extend or capability.tool is None:
        # EVERY IN-PROCESS DISPATCH RUNS UNDER THE RUN'S RECORDED AUTHORITY, like a tool step.
        # These returned from here bare, as whoever advanced the run (the worker), so under
        # enforce More tests was refused "needs code_task:retry" and shown as "returned no id
        # to wait on" (wfr_9575c86ab4a74defa0d2). Work started on the build's branch (PLRR,
        # More tests, an `extend` build) runs as the builder that branch's task runs as
        # (_start_on_the_branch); every other tool-less step as its own agent. `_invoke_tool`
        # refuses a call made outside either, so a new dispatch cannot forget.
        if extend or key in _ON_THE_BRANCH:
            return _perform_toolless(connection, ctx, project_id=project_id, actor=actor,
                                     step=step, context=context, key=key,
                                     capability=capability, inputs=inputs, extend=extend)
        from .step_agents import StepAgentError
        from .step_agents import resolve as who_runs_it

        try:
            agent: Optional[str] = who_runs_it(connection, getattr(ctx, "settings", None),
                                               project_id, step, actor=actor)["agent"]
        except StepAgentError:
            # A tool-less step never needed an agent on duty to run: the person's own authority.
            agent = None
        with _step_authority(ctx, connection, project_id, context, step, agent):
            return _perform_toolless(connection, ctx, project_id=project_id, actor=actor,
                                     step=step, context=context, key=key,
                                     capability=capability, inputs=inputs, extend=extend)

    # ADOPT, DO NOT DUPLICATE.
    #
    # The console creates the code task and THEN starts the outer run, because the operator
    # pressed a button that makes a project. If `build` also called create_code_task there
    # would be two builds racing over one repository — a far worse outcome than a missing step.
    # So the run carries the id of the task it already has, and this step waits on that one.
    # THE PROJECT ALREADY EXISTS, so a step that comes BEFORE the build cannot be run forwards.
    #
    # A run started with `adopt_code_task_run_id` is retrospective: the build was under way
    # before the workflow was. Executing `research` there commissions web research, at real
    # cost, for a thing already being written — and `meeting` convenes a panel to debate a
    # question the code has answered. Recorded as not applicable, with the reason, which is what
    # actually happened: this project skipped that phase.
    adopted = _adopted_handle(key, context)
    if (
        key in _RETROSPECTIVE_KINDS
        and not adopted
        and (context.get("input") or {}).get("adopt_code_task_run_id")
    ):
        return {
            "capability": key,
            "tool": None,
            "status": "not_applicable",
            "note": (
                f"No {capability.label.lower()} phase — this project was started directly, "
                f"and the build was already running when this workflow began."
            ),
        }

    if adopted:
        return {
            "capability": key,
            "tool": None,
            "status": "awaiting",
            "await": adopted,
            "adopted": True,
            # Named as a downstream step expects to find it, so `preview` and `deploy` can
            # reference an adopted build exactly as they would one this step had started.
            **({_CANONICAL_FIELD[adopted["kind"]]: adopted["id"]}
               if adopted["kind"] in _CANONICAL_FIELD else {}),
            "note": f"waiting on the {adopted['kind'].replace('_', ' ')} "
                    f"this project was started with",
        }

    # RESEARCH RUNS WHEN IT CAN, AND STANDS ASIDE — SAYING WHY — ONLY WHEN IT CANNOT.
    #
    # In Community a discovery is a governed `discovery.run` job: every research stage is
    # reserved, leased and receipted (discovery_spend.py), so the tool below commissions it like
    # any other edition and the step waits on it. Research is the Claude CLI's web search on
    # the Claude Code subscription. Where that subscription cannot be used — not connected, its
    # login rejected, out of capacity — the job could only be refused at its first stage, so the
    # step is recorded as not applicable with that reason, a `workflow.research_skipped` event
    # names it, and the run carries on to the panel, whose research binding is optional. The
    # check is the model route the job itself would take (discovery_service.research_unavailable),
    # never the edition. A budget refused at run time is not prejudged here: the job records it
    # with a decision, and the step reports research that failed and the run continues. An
    # adopted dossier above still wins: reading research that already exists spends nothing.
    if key == "research":
        from .discovery_service import RESEARCH_SOURCE_UNAVAILABLE, research_unavailable

        settings = getattr(ctx, "settings", None)
        unavailable: Optional[dict[str, Any]] = None
        if settings is not None and connection is not None:
            try:
                unavailable = research_unavailable(connection, settings, project_id, actor)
            except Exception as error:  # noqa: BLE001 — recorded as the skip's reason below
                log.warning("research availability check failed for %s: %s",
                            context.get("run_id"), error)
                unavailable = {
                    "reason": "research_check_failed",
                    "detail": f"BOSS could not check whether research can run: {error}"[:500],
                }
        if unavailable:
            detail = str(unavailable.get("detail") or "")
            emit_event(
                connection, project_id, "workflow.research_skipped", actor,
                "workflow_run", str(context.get("run_id") or ""),
                {"step_id": str(step.get("id") or ""), **unavailable},
            )
            return {
                "capability": key,
                "tool": None,
                "status": "not_applicable",
                "reason": str(unavailable.get("reason") or RESEARCH_SOURCE_UNAVAILABLE),
                "note": "Skipped: research cannot run here. The panel works from your "
                        "request instead. " + detail,
            }

    # NO PREVIEW, NO DEPLOY.
    #
    # A workflow that has a `preview` step has it for a reason: somebody looks at the thing
    # before it ships. But `deploy` binds its inputs from `build`, not from `preview`, so the
    # two steps were only related by running order — and a preview that produced nothing still
    # let deploy run. The instruction was explicit: without a preview, do not move to deploy.
    #
    # Enforced here rather than by binding preview_url into eleven catalogue entries, five of
    # which have a preview step: a rule written once cannot be forgotten by the next person to
    # add a workflow.
    #
    # A STACK TARGET IS THE EXCEPTION, AND IT IS NOT A LOOPHOLE. An app that declares how it is
    # deployed (boss.deploy.yaml) is planned straight from the build's pull-request head onto
    # its host, and held for approval like every governed effect. Its manifest's health checks
    # are the proof it runs; a platform of several services cannot be previewed as one
    # container, so requiring a preview would make such apps undeployable from a workflow.
    if key == "deploy":
        from .stack_deploy import plan_for_workflow_step, stack_target_for_deploy

        stack_target = stack_target_for_deploy(
            connection, project_id, inputs,
            default_owner=str(getattr(getattr(ctx, "settings", None), "target_github_owner", "")
                              or ""),
            # THE DEPLOY ITSELF: a declared app's first deploy makes its target here.
            create=True)
        approved = bool(context.get("approval_decision_id"))
        waived_for = str(context.get(STACK_GATE_TARGET) or "")
        if waived_for and not approved and (
                stack_target is None or (waived_for != FIRST_STACK_TARGET
                                         and str(stack_target.get("id")) != waived_for)):
            # THE GATE WAS WAIVED FOR ONE STACK TARGET; EXECUTION MUST SHIP THERE OR NOWHERE.
            # Between the waiver and this call the target can be deactivated, a second stack
            # target added for the repository, or its host changed — and falling through would
            # run the per-app deploy with no approval at all.
            # Settled, not retried: the operator decides, and Try again re-asks the gate.
            raise SettledCapabilityError(
                f"this step's approval was to come from a stack deployment to target "
                f"{waived_for}, but the build no longer resolves to that target, so nothing "
                "was deployed. Check the repository's stack targets in Infra, then Try again — "
                "BOSS will ask for the approval that fits.")
        if stack_target is not None:
            refusal = _assignment_refusal(
                connection, project_id, context.get("input") or {},
                str((step.get("approval") or {}).get("approver_role") or ""),
                run_id=str(context.get("run_id") or "") or None)
            if refusal:
                emit_event(connection, project_id, "workflow.assignment_refused", actor,
                           "workflow_run", str(context.get("run_id") or ""),
                           {"approver_role": refusal["approver_role"], "app": refusal["app"],
                            "assigned": refusal["assigned"], "step_id": step.get("id")})
                # Settled, exactly as `_open_gate` fails the step: retrying cannot change who
                # is assigned.
                raise SettledCapabilityError(refusal["reason"])
            # FIX AND REDEPLOY (deploy_repair): a step queued again after its repair build
            # finished ships the pull request's NEW head, from that build — never the pin.
            run_id = str(context.get("run_id") or "")
            step_id = str(step.get("id") or "")
            before = _latest_step_output(connection, project_id, run_id, step_id) \
                if run_id and step_id else {}
            redeploy = before.get(deploy_repair.REDEPLOY_KEY) \
                if isinstance(before.get(deploy_repair.REDEPLOY_KEY), dict) else None
            fixes = _deploy_fixes(before) if redeploy else []
            if redeploy and str(redeploy.get("build_run_id") or ""):
                inputs = {**inputs, "run_id": str(redeploy["build_run_id"])}
            # THE RECOVERY LOOP (step_recovery): what it decided last travels with the step —
            # its history and budget, and for clean_and_retry this app's own earlier releases
            # to retire as a step of the new deployment (guarded on the host, approved with it).
            recovery_state = step_recovery.state_of(before)
            pending = before.get(step_recovery.PENDING_KEY) \
                if isinstance(before.get(step_recovery.PENDING_KEY), dict) else {}
            retire = [str(p) for p in pending.get("retire_projects") or [] if p]
            planned = plan_for_workflow_step(
                ctx.engine, getattr(ctx, "settings", None), project_id=project_id,
                actor=actor, target=stack_target, inputs=inputs,
                workflow_run_id=run_id or None,
                approver_role=str((step.get("approval") or {}).get("approver_role") or "")
                or None,
                step_id=step_id or None,
                # ONE APPROVAL PER DEPLOY: the gate a person already answered, passed on.
                approval_decision_id=str(context.get("approval_decision_id") or "") or None,
                # A PROMISED PERSON: the stack deployment's own decision is this step's gate,
                # so the environment's policy may not answer it either.
                person_required=promises_a_person(step),
                # Pass 1's deploy ships the head pass 1 left; "Deploy again" ships pass 2's.
                # A release a person approved from its card ships exactly the head that card
                # listed (release_scope), never a head that moved after.
                commit=(None if (pass_2_of or redeploy) else
                        (release_scope.approved_head(connection, run_id)
                         or pinned_pass_one_commit(connection, run_id))),
                retire_projects=retire or None)
            if redeploy:
                planned = {**planned, deploy_repair.FIXES_KEY: fixes,
                           deploy_repair.REDEPLOY_KEY: redeploy}
            if recovery_state:
                planned = {**planned, step_recovery.STATE_KEY: recovery_state}
            failure = planned.get("deploy_failure") if planned.get("status") == "failed" else None
            if failure and run_id and step_id:
                # REFUSED BEFORE ANYTHING WAS PLANNED (a program the machine lacks, a manifest
                # that does not fit, a host that needs a person): the deploy step's agent
                # decides what happens next (step_recovery) — never the operator by default.
                failure = deploy_repair.failure_of_refusal(
                    connection, failure, str(inputs.get("run_id") or ""))
                recovered = _recover_refused_plan(
                    connection, ctx, project_id=project_id, run_id=run_id, step_id=step_id,
                    failure=failure, before=before, fixes=fixes, redeploy=redeploy,
                    base={"tool": None, "stack_target_id": stack_target.get("id"),
                          "stack_target_name": stack_target.get("app_name")})
                if recovered is not None and recovered.get("status") == "awaiting":
                    return recovered
                if recovered is not None:
                    planned = {**planned, **recovered,
                               "error": f"{recovered.get('error') or ''} — "
                                        f"{planned.get('error') or ''}".strip(" —")}
                    failure = None
            if deploy_repair.is_app_side(failure) and run_id and step_id:
                # REFUSED FOR THE APP'S REASON BEFORE ANYTHING WAS PLANNED (a program the
                # machine lacks, a manifest that does not fit): the app is repaired and the
                # step waits on that build, then plans again.
                failure = deploy_repair.failure_of_refusal(
                    connection, failure, str(inputs.get("run_id") or ""))
                fix, why_not = _start_deploy_fix(
                    connection, ctx, project_id=project_id, run_id=run_id, step_id=step_id,
                    failure=failure, fixes=fixes)
                if fix is not None:
                    return _deploy_fix_awaiting(failure, fix, fixes, base={
                        "tool": None, "stack_target_id": stack_target.get("id"),
                        "stack_target_name": stack_target.get("app_name")})
                # SETTLED: the same commit is refused the same way on every retry; a person
                # decides now (Try again after a change, or Skip).
                planned = {**planned, SETTLED_FAILURE: True,
                           "error": f"{planned.get('error') or ''} BOSS did not fix "
                                    f"it by itself: {why_not}.".strip()}
            if fixes and planned.get("status") == "failed":
                planned = {**planned, "error": (
                    f"{planned.get('error') or ''} (after {len(fixes)} automatic fix"
                    f"{'es' if len(fixes) != 1 else ''}: {deploy_repair.fixes_sentence(fixes)})"
                ).strip()}
            return planned
    if key == "deploy" and not pass_2_of:
        from .stack_deploy import undeclared_deploy_refusal

        undeclared = undeclared_deploy_refusal(connection, str(inputs.get("run_id") or ""))
        if undeclared:
            # Settled, in words, before anything is planned (see `_gate_subject`).
            raise SettledCapabilityError(undeclared)
        missing = _preview_missing_for_run(connection, context)
        if missing:
            return {"capability": key, "tool": None, "status": "failed",
                    "error": missing,
                    "note": "this workflow previews before it ships; nothing was previewable."}
        waiver = _preview_waiver_for_run(connection, str(context.get("run_id") or ""))
        if waiver:
            # Shipping unpreviewed is allowed, never silent: the run's history says who chose it.
            emit_event(connection, project_id, "workflow.deploy_without_preview", actor,
                       "workflow_run", str(context.get("run_id") or ""),
                       {"step_id": step.get("id"), "preview_skipped_by": waiver["by"],
                        "reason": waiver["reason"][:300]})

    # A SMOKE TEST ASKS THE DEPLOYMENT, NOT A URL. A free URL probe is refused in Community
    # (every smoke test after a deploy dead-lettered there) and, where it ran, "GET / is 200"
    # passed a blank page. The run's own deployment is probed through its own target with the
    # health the app declares (deployment_smoke). A run that deployed nothing still calls the
    # tool with its URL, which each edition allows or refuses on its own terms.
    if key == "smoke_test":
        from .deployment_smoke import run_deployment, smoke_deployment

        run_id = str(context.get("run_id") or "")
        if run_deployment(connection, run_id) is not None:
            return smoke_deployment(connection, getattr(ctx, "settings", None),
                                    getattr(ctx, "engine", None),
                                    project_id=project_id, run_id=run_id)

    # ADOPT A PREVIEW THAT IS ALREADY UP. The build pipeline starts a preview itself, so a
    # workflow that called start_preview again would boot a second copy of the same app on a
    # second port — and the operator, who has the first one open in a browser, would be told
    # about a different URL. Same adopt-or-start rule as `build`: use what exists, start what
    # does not.
    if key == "preview":
        existing = _running_preview(connection, project_id, inputs.get("run_id"))
        if existing:
            return {"capability": key, "tool": None, "status": "observed",
                    **existing,
                    "note": "this app is already previewable — adopted the running preview."}

    # A BUILD THIS RUN STARTS BELONGS TO THIS RUN. Without saying so, start_code_task would
    # open a second workflow run for the task, nesting a pipeline inside the step that made it.
    #
    # `plrr` was dispatched HERE and could never reach it: the capability declares `tool=None`,
    # and that branch above returns for every key it handles. Moved up rather than duplicated.
    if key == "build":
        inputs = {**inputs, "workflow_run_id": str(context.get("run_id") or "")}
        # A FEATURE FOR AN APP THAT EXISTS is that app's next build, not a new app: the tool
        # starts it from the app's branch and lineage. Read from the run input, which the start
        # endpoint validated, rather than bound by each workflow — every build step honours it.
        extends = str((context.get("input") or {}).get("extends_run_id") or "").strip()
        if extends:
            inputs = {**inputs, "extends_run_id": extends}
            # The preview the operator was looking at (validated by the start endpoint).
            preview = str((context.get("input") or {}).get("preview_id") or "").strip()
            if preview:
                inputs = {**inputs, "preview_id": preview}
            # A FIX THAT TARGETS A RELEASE (an incident's, or a run started for one) is cut
            # from the commit serving on the target, on a branch of its own — never built on
            # the app's open PR branch, which may carry other runs' rejected commits (D2).
            serving = release_base_for(connection, context)
            if serving:
                inputs = {**inputs, "release_base_sha": serving}

    # WHAT THE PANEL IS BEING CONVENED FOR.
    #
    # The panel received the directive and, at best, the research dossier — never which
    # workflow it sat in or what the run had to end with. So the panel in Research-Led
    # Discovery (a workflow that produces a decision and a document, and never builds) argued
    # exactly like the panel in Standard Product Delivery, and wrote an implementation plan for
    # code that was never going to be written. Twelve workflows, one panel behaviour.
    #
    # Passed as its own field rather than pasted into the directive here: `request_meeting`
    # owns how a brief is folded into the panel's prompt, and it already does that for research.
    if key == "meeting":
        objective = str(context.get("objective") or "").strip()
        # A PANEL ASKED FOR A FEATURE OF A WORKING APP writes a PRD for a whole new product
        # unless told otherwise — and the build then follows that PRD. Said in the objective,
        # which request_meeting already puts first in every seat's prompt.
        existing = _existing_app_note(
            connection, project_id,
            str((context.get("input") or {}).get("extends_run_id") or "").strip())
        if existing:
            objective = f"{objective}\n\n{existing}".strip()
        if objective:
            inputs = {**inputs, "objective": objective,
                      "workflow_name": str(context.get("workflow_name") or "").strip()}

    # THE PANEL'S PRD IS THE BUILD BRIEF — the objective of Standard Product Delivery says so
    # in those words. It was not: `build` bound `instructions` to the original one-line
    # directive, so an engineering agent received the operator's sentence and nothing the
    # research or the panel had produced. The interview-scorecard build got 745 characters
    # after a 7m49s research sweep and a 22m48s panel, and shipped exactly what its brief
    # described — an API, no screen — because no screen had been asked for anywhere it could
    # see. Folded in here rather than in the catalogue because a binding carries a value and
    # this needs two of them joined; `instructions` stays the field create_code_task owns.
    if key == "build" and context.get("deploys") and "boss.deploy.yaml" not in str(inputs.get("instructions") or ""):
        inputs = {**inputs, "instructions": f"{str(inputs.get('instructions') or '').strip()}\n\n{DEPLOY_MANIFEST_BRIEF}"}
    if key == "build" and context.get("deploys") \
            and DEPLOY_HOST_MARKER not in str(inputs.get("instructions") or ""):
        # THE MACHINE IT SHIPS TO, BEFORE THE MANIFEST IS WRITTEN. HelpDesk's build wrote a
        # `kind: commands` manifest running npm for a host that has Docker and no Node, and
        # the deploy found out at its install step (deploy_repair). Told up front, the build
        # writes for what the machine has.
        brief = deploy_repair.host_brief(connection, project_id, context)
        if brief:
            inputs = {**inputs, "instructions":
                      f"{str(inputs.get('instructions') or '').strip()}\n\n{brief}"}
    # AN INCIDENT'S INVESTIGATION IS THE FIX'S BRIEF (Incident Response): what the monitor saw,
    # what the app logged, what the serving release changed, and the hypothesis — before the
    # one-line directive, with the incident's own acceptance criteria unless a person gave some.
    if key == "build":
        brief = str(_from_any_step(context, "incident_brief") or "").strip()
        if brief and brief not in str(inputs.get("instructions") or ""):
            inputs = {**inputs, "instructions":
                      f"{brief}\n\n---\n\n{str(inputs.get('instructions') or '').strip()}"}
            if not inputs.get("acceptance_criteria"):
                inputs = {**inputs, "acceptance_criteria": list(INCIDENT_FIX_CRITERIA)}
            fixed_title = str(_from_any_step(context, "title") or "").strip()
            if fixed_title.startswith("Fix "):
                inputs = {**inputs, "title": fixed_title}
    if key == "build":
        spec = str(inputs.get("spec_markdown") or "").strip()
        inputs = {k: v for k, v in inputs.items() if k != "spec_markdown"}
        if spec:
            directive = str(inputs.get("instructions") or "").strip()
            inputs = {**inputs, "instructions": (
                f"{directive}\n\n"
                f"---\n\n"
                f"THE PRODUCT SPEC BELOW IS THE BRIEF. The directive above is the one-line\n"
                f"request that started this; what follows is the specification a panel of\n"
                f"agents produced from it, and it is what you are building. Where the two\n"
                f"differ in detail, the spec is the decision.\n\n"
                f"{spec}"
            )}
        # THE APPROVED ARCHITECTURE TRAVELS WITH THE SPEC. Read from whichever step produced it
        # (only a SUCCEEDED step is in the context, and an architecture step succeeds only when
        # a person approved it), so every workflow that has one gets it — no binding to forget.
        architecture = str(_from_any_step(context, "architecture_markdown") or "").strip()
        if architecture:
            inputs = {**inputs, "instructions": (
                f"{str(inputs.get('instructions') or '').strip()}\n\n"
                f"---\n\n"
                f"THE APPROVED ARCHITECTURE BELOW IS BINDING. A person approved this design\n"
                f"before the build started; follow its components, data model, APIs and\n"
                f"security model. Where you must deviate, say so and why in the pull request.\n\n"
                f"{architecture}"
            ).strip()}

    # THE SECURITY REVIEW'S TOOL-BACKED STEPS (services/security_review_steps.py): the scan, the
    # panel and the fix build their tool's arguments from what the run produced — or say why
    # there is nothing to do (no findings, nothing chosen, round 1 already passed).
    if key in security_review_steps.TOOL_KEYS:
        try:
            prepared = security_review_steps.prepare(connection, project_id, key, context, inputs)
        except security_review_steps.StepRefused as refused:
            raise SettledCapabilityError(str(refused)) from refused
        if "args" not in prepared:
            return prepared
        inputs = prepared["args"]

    # WHOSE WORK THIS IS. A step may name a captain, overriding the capability's default —
    # the console already shows it as "the captain this WORKFLOW named for this step", and
    # validate_captain already refuses one the workforce does not have. Nothing read it at
    # execution, so a workflow that said "the publisher agent runs this step" was recorded,
    # validated, displayed, and then ignored: the tool ran as whoever started the run.
    #
    # That is the whole point of naming one. An agent holds the connection, the knowledge and
    # the standing to act — a step that names the publisher and then publishes as the operator
    # has authorised nothing and attributed the act to the wrong party.
    #
    # Decided by the same rule as every other step kind (services/step_agents.py): the named
    # agent, or — for a built-in, whose tool is BOSS's own and needs no grant — whoever started
    # the run, which is what every catalogue workflow has always done. Recorded as `ran_as`.
    from .step_agents import resolve as who_runs_it

    ran_as = who_runs_it(connection, getattr(ctx, "settings", None), project_id, step,
                         actor=actor)
    step_actor = ran_as["agent"]
    # RETRY WITH A NOTE. A build the operator retried resumes the failed attempt's lineage
    # (its branch, its design, its findings and failure) with their note as the feedback; a
    # retry with nothing to resume carries the note into the new build's instructions. The
    # resume is started EXACTLY like this step's build: as `step_actor`, under the run's
    # recorded authority, through the same tool checks (_retried_build).
    retry_note = ""
    if key == "build":
        resumed, inputs, retry_note = _retried_build(
            connection, ctx, project_id, step, context, inputs,
            ran_as=ran_as, tool_name=capability.tool)
        if resumed is not None:
            return _settle_started_work(capability, key, resumed)
    # Under the run's recorded authority (the step's agent for that person), never the worker's.
    with _step_authority(ctx, connection, project_id, context, step, step_actor):
        if key == "deploy" and context.get("approval_decision_id"):
            # ONE APPROVAL PER DEPLOY: the step's released gate goes to the deploy explicitly,
            # so the deployment runs under it instead of asking a second time.
            inputs = {**inputs, "approval_decision_id": str(context["approval_decision_id"])}
        if key == "deploy" and promises_a_person(step):
            # The step promised a person: the deployment it starts may not run on the
            # environment's policy, whatever happened to the gate on the way here.
            inputs = {**inputs, "person_required": True}
        output = _invoke_tool(ctx, capability.tool, project_id, step_actor, inputs)
    result = {"capability": key, "tool": capability.tool, **output,
              "ran_as": {**ran_as, "tool": capability.tool,
                         "skills_offered": [], "skills_used": []},
              # Kept on the step run: the step's output is replaced when it parks.
              **({"retry_note": retry_note} if retry_note else {})}
    skipped = _preview_not_permitted(key, result, step_actor)
    if skipped:
        # NEVER A SILENT SKIP: the run's history records that a whole step did not run, and why.
        emit_event(connection, project_id, "workflow.step_skipped", step_actor or actor,
                   "workflow_run", str(context.get("run_id") or ""),
                   {"step_id": step.get("id"), "capability": key,
                    "reason": f"skipped: {skipped['reason']}"[:500],
                    "permission": result.get("permission")})
        return skipped
    if output.get("ok") is False and output.get("permission"):
        # AN IDENTITY REFUSAL IS SETTLED: the same grant refuses the same call on every retry.
        raise SettledCapabilityError(str(output.get("message") or output.get("error")
                                         or f"'{capability.tool}' was refused."))
    failed = _tool_failed_to_start(key, result)
    if failed:
        raise CapabilityExecutionError(failed)

    # STARTED IS NOT FINISHED. See `_settle_started_work`.
    return _settle_started_work(capability, key, result)


#: The heading the operator's retry note is given under, in every build it reaches.
RETRY_NOTE_HEADING = "## What to change on this retry (from the operator)"

#: A previous attempt in one of these states is resumed by a retry, never rebuilt from scratch.
_RESUMABLE_BUILD = frozenset({"failed", "blocked", "cancelled", "error", "rejected",
                              "awaiting_gate_override"})


def _retried_build(
    connection: Connection, ctx: Any, project_id: str, step: dict[str, Any],
    context: dict[str, Any], inputs: dict[str, Any], *,
    ran_as: Optional[dict[str, Any]] = None, tool_name: str = "create_code_task",
) -> tuple[Optional[dict[str, Any]], dict[str, Any], str]:
    """A build step the operator retried (workflows.retry_failed_step): what it starts.

    WHO STARTS THE RESUME. Exactly who starts the step's own build: the step's actor (`ran_as`,
    step_agents.resolve), under the workflow run's recorded authority (`_step_authority`), past
    the same tool checks as `tool_name` (`_authorize_step_tool`). The resumed code task's job is
    therefore stamped with that person's grant, as a `create_code_task` from the step would be.
    Called bare it ran as whoever advanced the run (the worker, a system principal), and the
    resumed build was refused `no_grant` the moment it started. No grant is borrowed: a run
    whose authority has ended is refused here, before anything is queued.

    Returns (result, inputs, note). `result` is the resumed attempt when the failed build is resumed
    — `code_task_service.retry_code_task`, the same operation as the code task's own Retry, so
    the new attempt keeps the branch and the design (code_task.design_resumed) and is told the
    previous findings, the gates that failed and the verify output's tail, with the note as the
    operator's feedback. Otherwise None, and `inputs` carry the note into a fresh build — always
    so when no attempt of the build kept work (code_task_service.lineage_kept_work): a resume
    of nothing is the step's own build again, never a repair of an empty tree.
    """
    run_id = str(context.get("run_id") or "")
    step_id = str(step.get("id") or "")
    if not run_id or not step_id or connection is None:
        return None, inputs, ""
    try:
        row = connection.execute(
            text("SELECT output FROM workflow_step_runs WHERE workflow_run_id=:r "
                 "AND project_id=:p AND step_id=:s ORDER BY created_at DESC LIMIT 1"),
            {"r": run_id, "p": project_id, "s": step_id}).mappings().first()
    except Exception as error:  # noqa: BLE001 — a retry falls back to a fresh build
        log.warning("cannot read step %s of %s for a retry: %s", step_id, run_id, error)
        return None, inputs, ""
    output = (row or {}).get("output") if row else None
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            output = None
    if not isinstance(output, dict) or "retried_by" not in output:
        return None, inputs, ""
    note = str(output.get("retry_note") or "").strip()
    previous = str(output.get("retried_from_code_task_run_id") or "").strip()
    from . import code_task_service as cts

    prev = cts.load_code_task_run(connection, previous) if previous else None
    if (prev is not None and str(prev.get("project_id")) == str(project_id)
            and not cts.lineage_kept_work(ctx.engine, prev, previous)):
        # NOTHING TO RESUME: no attempt of this build kept any work (crun_ced396a4 was blocked
        # by the secret scan and kept no file; its resume, crun_3933d79d, "repaired" an empty
        # tree). The step runs its OWN build again, from its original inputs, with the note as
        # the operator's guidance: never a repair of an empty diff.
        emit_event(connection, project_id, "workflow.build_restarted", "boss", "workflow_run",
                   run_id, {"step_id": step_id, "from_code_task_run_id": previous,
                            "with_note": bool(note),
                            "why": "the previous attempt kept no work to build onto, so the "
                                   "step's build runs in full from its original inputs"})
        prev = None
    if prev is not None and str(prev.get("project_id")) == str(project_id) and (
            str(prev.get("status") or "") in _RESUMABLE_BUILD or note):
        if ran_as is None:
            from .step_agents import resolve as who_runs_it

            ran_as = who_runs_it(connection, getattr(ctx, "settings", None), project_id, step,
                                 actor=str(output.get("retried_by") or "ceo"))
        step_actor = ran_as["agent"]
        with _step_authority(ctx, connection, project_id, context, step, step_actor):
            _, refused = _authorize_step_tool(ctx, tool_name, project_id, inputs)
            if refused is not None:
                raise CapabilityExecutionError(str(
                    refused.get("error") or refused.get("message")
                    or f"'{tool_name}' was refused for this run's authority."))
            try:
                result = cts.retry_code_task(
                    ctx.engine, getattr(ctx, "settings", None) or get_settings(), ctx.jobs,
                    previous, fix_note=note)
            except ValueError as error:
                raise CapabilityExecutionError(
                    f"could not resume the previous build {previous}: {error}") from error
        emit_event(connection, project_id, "workflow.build_resumed", "boss", "workflow_run",
                   run_id, {"step_id": step_id, "from_code_task_run_id": previous,
                            "code_task_run_id": result.get("run_id"),
                            "with_note": bool(note)})
        return ({"capability": "build", "tool": "retry_code_task", **result,
                 "ran_as": {**ran_as, "tool": "retry_code_task",
                            "skills_offered": [], "skills_used": []},
                 "resumed_from_code_task_run_id": previous,
                 **({"retry_note": note} if note else {})}, inputs, note)
    if not note:
        return None, inputs, ""
    instructions = str(inputs.get("instructions") or "").strip()
    return (None, {**inputs, "instructions": f"{instructions}\n\n{RETRY_NOTE_HEADING}\n{note}"},
            note)


def _deploy_fixes(output: Any) -> list[dict[str, Any]]:
    """The repairs a deploy step has started so far (deploy_repair), oldest first."""
    fixes = output.get(deploy_repair.FIXES_KEY) if isinstance(output, dict) else None
    return [f for f in fixes if isinstance(f, dict)] if isinstance(fixes, list) else []


def _both_causes(output: Any, handle: dict[str, Any], build_id: str, how: str) -> str:
    """A deploy step's repair build that did not pass: the deploy's cause AND the build's, so
    the person deciding next reads both (deploy_repair)."""
    if handle.get("then") != deploy_repair.THEN_REDEPLOY:
        return how
    fixes = _deploy_fixes(output)
    cause = (fixes[-1].get("headline") or fixes[-1].get("cause")) if fixes else ""
    return (f"the deploy failed ({str(cause or 'for the app').rstrip('.')}), and BOSS's repair "
            f"build {build_id} did not pass: {how}. Nothing new was deployed.")


def _latest_step_output(connection: Connection, project_id: str, run_id: str,
                        step_id: str) -> dict[str, Any]:
    if not run_id or not step_id:
        return {}
    row = connection.execute(
        text("SELECT output FROM workflow_step_runs WHERE workflow_run_id=:r "
             "AND project_id=:p AND step_id=:s ORDER BY created_at DESC LIMIT 1"),
        {"r": run_id, "p": project_id, "s": step_id}).scalar()
    output = json.loads(row) if isinstance(row, str) else row
    return output if isinstance(output, dict) else {}


def _start_deploy_fix(
    connection: Connection, ctx: Any, *, project_id: str, run_id: str, step_id: str,
    failure: dict[str, Any], fixes: list[dict[str, Any]], extra_note: str = "",
) -> tuple[Optional[dict[str, Any]], str]:
    """Start the repair build for a deploy that failed for the app's reason (deploy_repair).

    Returns (the fix, "") when a build was started, or (None, why not) — the ceiling is reached,
    the project turned it off, or the build could not be resumed. WHO STARTS IT: exactly who
    starts the run's own build — the build step's agent under the run's recorded authority, past
    the same tool checks (`_retried_build`) — so a repair is never a way around a grant.
    `extra_note`: the recovery agent's own precise note (step_recovery), added to BOSS's.
    """
    settings = getattr(ctx, "settings", None) or get_settings()
    cap = deploy_repair.fix_cap(connection, project_id, settings)
    if cap <= 0:
        return None, ("automatic deploy fixes are off for this project "
                      f"({deploy_repair.CAP_SETTING} = 0)")
    if len(fixes) >= cap:
        return None, (f"BOSS already fixed and redeployed this app {len(fixes)} time"
                      f"{'s' if len(fixes) != 1 else ''} (the most it does by itself is {cap})")
    build_run_id = str(failure.get("build_run_id") or "")
    if not build_run_id:
        return None, "the deployment names no build to fix"
    attempt = len(fixes) + 1
    note = deploy_repair.fix_note(failure, attempt=attempt, cap=cap)
    if extra_note.strip():
        note = f"{note}\n\nTHE DEPLOY AGENT'S DIAGNOSIS: {extra_note.strip()}"
    new_id, why_not = _start_recovery_build(connection, ctx, project_id=project_id,
                                            run_id=run_id, step_id=step_id,
                                            build_run_id=build_run_id, note=note)
    if not new_id:
        return None, why_not
    fix = {"attempt": attempt, "build_run_id": new_id, "from_build_run_id": build_run_id,
           "deployment_id": failure.get("deployment_id"), "phase": failure.get("phase"),
           "cause": deploy_repair.short_cause(failure), "headline": failure.get("headline"),
           "host": deploy_repair.where(failure), "started_at": now_utc().isoformat()}
    emit_event(connection, project_id, deploy_repair.FIX_EVENT, "boss", "workflow_run", run_id,
               {"step_id": step_id, **{k: v for k, v in fix.items() if k != "started_at"}})
    return fix, ""


def _start_recovery_build(
    connection: Connection, ctx: Any, *, project_id: str, run_id: str, step_id: str,
    build_run_id: str, note: str,
) -> tuple[str, str]:
    """A new attempt of `build_run_id`'s lineage (code_task_service.retry_code_task), told
    `note` — started by the run's BUILD step's agent under the run's recorded authority, past
    the same tool check as any build it starts. Returns (new run id, "") or ("", why not)."""
    from . import code_task_service as cts

    settings = getattr(ctx, "settings", None) or get_settings()
    if ctx is None or getattr(ctx, "engine", None) is None or getattr(ctx, "jobs", None) is None:
        return "", "the repair build could not be started here (no job queue)"
    spec_steps = [s for s in (workflows._run_spec(connection, run_id).get("steps") or [])
                  if isinstance(s, dict)]
    build_step = next((s for s in spec_steps if (s.get("binding") or {}).get("capability")
                       in ("build", "retry_build")), None) or {"id": step_id}
    run_actor = str(connection.execute(
        text("SELECT actor FROM workflow_runs WHERE id = :r"), {"r": run_id}).scalar() or "ceo")
    try:
        from .step_agents import resolve as who_runs_it

        ran_as = who_runs_it(connection, settings, project_id, build_step, actor=run_actor)
        with _step_authority(ctx, connection, project_id, {"run_id": run_id}, build_step,
                             ran_as["agent"]):
            _, refused = _authorize_step_tool(ctx, "create_code_task", project_id, {})
            if refused is not None:
                return "", str(refused.get("error") or refused.get("message")
                               or "the build step's agent may not start a build")
            started = cts.retry_code_task(ctx.engine, settings, ctx.jobs, build_run_id,
                                          fix_note=note)
    except Exception as error:  # noqa: BLE001 — said on the step, never a crash
        log.warning("recovery build for %s/%s not started: %s", run_id, step_id, error)
        return "", f"the repair build could not be started ({error})"
    new_id = str(started.get("run_id") or "")
    if not new_id:
        return "", "the repair build returned no run id"
    if started.get("created") is False:
        # THE REPAIR WAS NOT STARTED: another attempt of this app was already running on its
        # branch, and the retry returned THAT run — without this note. Recording it as the fix
        # sent the deploy back to wait on Order API's More tests pass, which never touched the
        # manifest Pilot had diagnosed (crun_cfb9f2e2, D2); the agent then saw "fix applied,
        # manifest unchanged" and could not tell why. Said as it is, so the agent (or a person)
        # starts the repair once that attempt has finished.
        return "", (f"the repair was not started: another attempt of this app ({new_id}) is "
                    "still running on its branch, and a second build on the same branch is "
                    "refused. Start the repair (with this note) when it has finished.")
    return new_id, ""


def _deploy_fix_awaiting(failure: dict[str, Any], fix: dict[str, Any],
                         fixes: list[dict[str, Any]], *,
                         base: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The deploy step's output while it waits for its repair build."""
    note = deploy_repair.progress_note(failure, fix["build_run_id"])
    return {**(base or {}), "capability": "deploy", "status": "awaiting",
            "await": {"kind": "code_task", "id": fix["build_run_id"],
                      "then": deploy_repair.THEN_REDEPLOY, "attempt": fix["attempt"]},
            "code_task_run_id": fix["build_run_id"],
            deploy_repair.FIXES_KEY: [*fixes, fix], "note": note,
            "deploy_failure": {k: v for k, v in failure.items()
                               if k not in ("manifest_text", "log_tail", "steps")}}


# ── THE RECOVERY AGENT (step_recovery): a failed step's own agent decides what happens next ──

def _recovery_begin(connection: Connection, ctx: Any, *, project_id: str, run_id: str,
                    step_id: str, kind: str, output: Any, evidence: dict[str, Any]
                    ) -> Optional[tuple[dict[str, Any], dict[str, Any], dict[str, str]]]:
    """(the decision, the step's recovery state, the agent) — or None when recovery is off for
    the project, the kind is not recoverable, or there is no worker context to act in."""
    if ctx is None or kind not in step_recovery.KINDS:
        return None
    settings = getattr(ctx, "settings", None) or get_settings()
    if not step_recovery.enabled(connection, settings, project_id):
        return None
    state = step_recovery.state_of(output)
    if not state.get("started_at"):
        state = step_recovery.open_window(state)
    elif step_recovery.last_decision(state).get("action") == step_recovery.ASK_PERSON:
        # A NEW FAILURE AFTER A PERSON WAS ASKED means a person retried (or BOSS was updated):
        # a fresh window — the budget, the clock and the per-cause rule start again.
        state = step_recovery.open_window(
            state, why="the step was run again after a person was asked")
    spec_step = workflows._step_map(workflows._run_spec(connection, run_id)).get(step_id) or {}
    agent = step_recovery.agent_of(connection, project_id, spec_step)
    entry = step_recovery.decide(connection, settings, project_id=project_id, run_id=run_id,
                                 step_id=step_id, kind=kind, evidence=evidence, state=state,
                                 agent=agent)
    return entry, state, agent


def _park_for_recovery(connection: Connection, project_id: str, step_run_id: str,
                       handle: dict[str, Any], merge: dict[str, Any]) -> None:
    """A WAITING step now waits on what its recovery decided (a repair build, a backoff, a BOSS
    update): its await handle replaced, the decision merged onto its output."""
    connection.execute(
        text("UPDATE workflow_step_runs SET output = (COALESCE(output, '{}'::jsonb) - 'await') "
             "|| CAST(:o AS JSONB), updated_at = now() WHERE id = :i AND project_id = :p "
             "AND status = 'waiting'"),
        {"i": step_run_id, "p": project_id,
         "o": json.dumps({**merge, "await": handle}, default=str)})


def _build_fix_note(failure: dict[str, Any], entry: dict[str, Any]) -> str:
    """The brief of a build the recovery agent starts again: what failed, and what to change."""
    why = str(failure.get("why") or failure.get("error") or "").strip()
    lines = [f"THE LAST ATTEMPT OF THIS BUILD FAILED ({str(failure.get('status') or 'failed')})."
             f" {entry.get('agent') or 'BOSS'}, the step's agent, started this attempt."]
    if why:
        lines.append(f"Why it ended: {why[:1500]}")
    if entry.get("note"):
        lines.append(f"What to change: {entry['note']}")
    elif entry.get("action") == step_recovery.RETRY:
        lines.append("The cause looked transient: build it again as it was, keeping the work.")
    return "\n\n".join(lines)


def _recover_refused_plan(
    connection: Connection, ctx: Any, *, project_id: str, run_id: str, step_id: str,
    failure: dict[str, Any], before: dict[str, Any], fixes: list[dict[str, Any]],
    redeploy: Optional[dict[str, Any]], base: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """The recovery decision for a deploy whose PLAN was refused, inside the deploy capability.
    Returns the step's output: `awaiting` (a repair build, a backoff, a BOSS update) or a
    SETTLED failure with the agent's one question — or None when recovery is off."""
    settings = getattr(ctx, "settings", None) or get_settings()
    evidence = step_recovery.deploy_evidence(connection, settings, failure)
    began = _recovery_begin(connection, ctx, project_id=project_id, run_id=run_id,
                            step_id=step_id, kind="deploy", output=before, evidence=evidence)
    if began is None:
        return None
    entry, state, agent = began
    action = entry["action"]
    carry = {**base, "capability": "deploy", step_recovery.STATE_KEY: state,
             "note": entry["sentence"],
             "deploy_failure": {k: v for k, v in failure.items()
                                if k not in ("manifest_text", "log_tail", "steps")},
             **({deploy_repair.FIXES_KEY: fixes, deploy_repair.REDEPLOY_KEY: redeploy}
                if redeploy else {})}
    why_not = ""
    if action == step_recovery.FIX_APP:
        fix, why_not = _start_deploy_fix(
            connection, ctx, project_id=project_id, run_id=run_id, step_id=step_id,
            failure=failure, fixes=fixes, extra_note=str(entry.get("note") or ""))
        if fix is not None:
            state["build_ids"] = [*state.get("build_ids", []), fix["build_run_id"]]
            waiting = _deploy_fix_awaiting(failure, fix, fixes, base=base)
            return {**waiting, step_recovery.STATE_KEY: state,
                    "note": f"{agent.get('name') or 'BOSS'}: {waiting['note']}"}
    elif action in (step_recovery.RETRY, step_recovery.CLEAN_AND_RETRY):
        return {**carry, "status": "awaiting",
                "await": step_recovery.run_again_handle(entry, kind="deploy")}
    elif action in step_recovery.BOSS_SIDE_ACTIONS:
        handle, merge = _boss_side_wait(connection, ctx, project_id=project_id, run_id=run_id,
                                        step_id=step_id, kind="deploy", entry=entry,
                                        evidence=evidence, state=state)
        return {**carry, **merge, "status": "awaiting", "await": handle}
    if why_not:
        entry = step_recovery.record_ask(
            connection, project_id=project_id, run_id=run_id, step_id=step_id, state=state,
            agent=agent, failure=failure, reason=why_not,
            signature=str(entry.get("signature") or ""))
    # SETTLED: the same commit is refused the same way on every retry; a person decides now.
    return {step_recovery.STATE_KEY: state, "note": entry["sentence"], SETTLED_FAILURE: True,
            "recovery_question": entry.get("question"), "error": step_recovery.ask_error(entry),
            **({"recovery_resume_on_update": step_recovery.boss_build()}
               if step_recovery.resumes_on_update(entry, failure) else {})}


def _boss_side_wait(connection: Connection, ctx: Any, *, project_id: str, run_id: str,
                    step_id: str, kind: str, entry: dict[str, Any], evidence: dict[str, Any],
                    state: dict[str, Any], build_run_id: Optional[str] = None
                    ) -> tuple[dict[str, Any], dict[str, Any]]:
    """A BOSS-side decision (wait_for_boss or fix_boss): recorded for BOSS's developers as
    always, then considered for a SELF-FIX (services/self_fix.py, BOSSAPP-571). Returns (the
    step's await handle, what goes on its output): the self-fix's own handle when one started
    or another one already holds this cause; today's wait for a BOSS update otherwise, with the
    reason the self-fix did not start."""
    settings = getattr(ctx, "settings", None) or get_settings()
    rec = step_recovery.recommend_boss_fix(connection, settings, project_id=project_id,
                                           run_id=run_id, step_id=step_id, entry=entry,
                                           evidence=evidence)
    try:
        with connection.begin_nested():
            proposed = self_fix.propose(
                connection, settings, project_id=project_id, run_id=run_id, step_id=step_id,
                kind=kind, entry=entry, evidence=evidence, state=state,
                recommendation_id=rec.get("id"), jobs=getattr(ctx, "jobs", None))
    except Exception as error:  # noqa: BLE001 — a self-fix that cannot start is today's wait
        log.warning("self-fix for %s/%s did not start: %s", run_id, step_id, error)
        proposed = {"outcome": "skipped",
                    "why": f"BOSS could not start a self-fix ({type(error).__name__})"}
    merge: dict[str, Any] = {"boss_recommendation_id": rec.get("id")}
    if proposed.get("outcome") in ("started", "waiting_on"):
        sfx = proposed["self_fix"]
        waiting = proposed["outcome"] == "waiting_on"
        handle = self_fix.handle_for(sfx, step_kind=kind, waiting_on=waiting,
                                     decision=entry.get("n"), build_run_id=build_run_id)
        merge.update({"self_fix_id": sfx["id"],
                      "self_fix": self_fix.step_view(sfx, waiting_on=waiting),
                      "note": (f"BOSS is already fixing this ({sfx['id']}); this step waits "
                               "for that fix." if waiting else self_fix.step_line(sfx))})
        return handle, merge
    merge.update({"note": entry["sentence"], "self_fix_skipped": proposed.get("why") or ""})
    return step_recovery.boss_update_handle(entry, kind=kind, build_run_id=build_run_id), merge


def _follow_self_fix(connection: Connection, ctx: Any, *, project_id: str, run_id: str,
                     step: Any, output: dict[str, Any], handle: dict[str, Any]
                     ) -> Optional[dict[str, Any]]:
    """A step waiting on a self-fix (its own, or another run's that holds the same cause): it
    resumes when the fix is installed and running, and falls back to today's wait for a BOSS
    update when the self-fix ended without holding — a person's own fix still resumes it."""
    verdict, why, sfx = self_fix.handle_verdict(connection, handle)
    step_id = str(step["step_id"])
    if verdict == "wait":
        line = self_fix.step_line(sfx) if sfx else why
        view = (self_fix.step_view(sfx, waiting_on=bool(handle.get("waiting_on")))
                if sfx else None)
        if line and (line != output.get("note") or view != output.get("self_fix")):
            connection.execute(
                text("UPDATE workflow_step_runs SET output = COALESCE(output, '{}'::jsonb) || "
                     "CAST(:o AS JSONB), updated_at = now() WHERE id = :i AND status = "
                     "'waiting'"), {"i": step["id"], "o": json.dumps(
                         {"note": line, **({"self_fix": view} if view else {})}, default=str)})
        return None
    if verdict == "fallback":
        fallback = {"kind": step_recovery.AWAIT_KIND, "then": step_recovery.THEN_BOSS_UPDATE,
                    "step_kind": handle.get("step_kind") or "deploy",
                    "decision": handle.get("decision"), "boss_build": step_recovery.boss_build(),
                    "signature": handle.get("signature"),
                    **({"build_run_id": handle["build_run_id"]}
                       if handle.get("build_run_id") else {})}
        _park_for_recovery(connection, project_id, step["id"], fallback, {
            "note": f"BOSS did not fix itself ({why}); this step resumes by itself when BOSS "
                    "is updated.", "self_fix_outcome": (sfx or {}).get("status")})
        return {"run_id": run_id, "step_id": step_id, "outcome": "waiting_for_boss",
                "awaited": "self_fix", "because": why}
    if ctx is None:
        return None  # a revival sweep never resumes work
    own = not handle.get("waiting_on") and sfx is not None and sfx.get("run_id") == run_id \
        and sfx.get("step_id") == step_id
    self_fix.mark_resumed(connection, str(handle.get("id")), run_id=run_id, step_id=step_id,
                          own=own)
    resumed = _resume_recovery(
        connection, ctx, project_id=project_id, run_id=run_id, step=step, output=output,
        handle={"kind": step_recovery.AWAIT_KIND, "then": step_recovery.THEN_BOSS_UPDATE,
                "step_kind": handle.get("step_kind") or "deploy",
                "decision": handle.get("decision"), "signature": handle.get("signature"),
                **({"build_run_id": handle["build_run_id"]} if handle.get("build_run_id")
                   else {})},
        note=f"BOSS fixed itself ({handle.get('id')}) — trying the step again",
        self_fix_id=str(handle.get("id")))
    return resumed


def _recover_awaited(
    connection: Connection, ctx: Any, *, project_id: str, run_id: str, step: Any,
    output: dict[str, Any], handle: dict[str, Any], kind: str, failure: dict[str, Any],
    evidence: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Run one recovery decision for a WAITING step whose awaited work failed, and carry it out.

    Returns None (recovery is off: the caller fails the step as before), {"done": …} when the
    step now waits on what was decided, or {"ask": the error's words, "output": …} for the
    caller to fail the step for a person, with the agent's one question."""
    step_id = str(step["step_id"])
    began = _recovery_begin(connection, ctx, project_id=project_id, run_id=run_id,
                            step_id=step_id, kind=kind, output=output, evidence=evidence)
    if began is None:
        return None
    entry, state, agent = began
    action = entry["action"]
    done = {"run_id": run_id, "step_id": step_id, "awaited": "recovery", "action": action,
            "decision": entry.get("n")}
    why_not = ""
    if action == step_recovery.FIX_APP and kind == "deploy":
        fixes = _deploy_fixes(output)
        fix, why_not = _start_deploy_fix(
            connection, ctx, project_id=project_id, run_id=run_id, step_id=step_id,
            failure=failure, fixes=fixes, extra_note=str(entry.get("note") or ""))
        if fix is not None:
            state["build_ids"] = [*state.get("build_ids", []), fix["build_run_id"]]
            waiting = _deploy_fix_awaiting(failure, fix, fixes, base=output)
            _park_for_recovery(connection, project_id, step["id"], waiting["await"], {
                deploy_repair.FIXES_KEY: waiting[deploy_repair.FIXES_KEY],
                "deploy_failure": waiting["deploy_failure"],
                "code_task_run_id": fix["build_run_id"],
                "note": f"{agent.get('name') or 'BOSS'}: {waiting['note']}",
                step_recovery.STATE_KEY: state})
            return {"done": {**done, "outcome": "fixing_the_app", "now": fix["build_run_id"]}}
    elif action in (step_recovery.FIX_APP, step_recovery.RETRY) and kind != "deploy":
        build_run_id = str(failure.get("build_run_id") or "")
        new_id, why_not = _start_recovery_build(
            connection, ctx, project_id=project_id, run_id=run_id, step_id=step_id,
            build_run_id=build_run_id, note=_build_fix_note(failure, entry)) \
            if build_run_id else ("", "the step names no build to start again")
        if new_id:
            state["build_ids"] = [*state.get("build_ids", []), new_id]
            follow = {"kind": "code_task", "id": new_id,
                      **({"then": handle.get("then"), "attempt": handle.get("attempt")}
                         if handle.get("then") == deploy_repair.THEN_REDEPLOY else {})}
            _park_for_recovery(connection, project_id, step["id"], follow, {
                "code_task_run_id": new_id, "note": entry["sentence"],
                step_recovery.STATE_KEY: state})
            return {"done": {**done, "outcome": "building_again", "now": new_id}}
    elif action in (step_recovery.RETRY, step_recovery.CLEAN_AND_RETRY):
        _park_for_recovery(connection, project_id, step["id"],
                           step_recovery.run_again_handle(entry, kind=kind),
                           {"note": entry["sentence"], step_recovery.STATE_KEY: state})
        return {"done": {**done, "outcome": "will_run_again"}}
    elif action in step_recovery.BOSS_SIDE_ACTIONS:
        handle, merge = _boss_side_wait(
            connection, ctx, project_id=project_id, run_id=run_id, step_id=step_id, kind=kind,
            entry=entry, evidence=evidence, state=state,
            build_run_id=str(failure.get("build_run_id") or "") or None)
        _park_for_recovery(connection, project_id, step["id"], handle,
                           {**merge, step_recovery.STATE_KEY: state})
        return {"done": {**done, "outcome": ("fixing_boss" if handle.get("kind") ==
                                             self_fix.AWAIT_KIND else "waiting_for_boss"),
                         "recommendation": merge.get("boss_recommendation_id"),
                         **({"self_fix": merge["self_fix_id"]} if merge.get("self_fix_id")
                            else {})}}
    if why_not:
        entry = step_recovery.record_ask(
            connection, project_id=project_id, run_id=run_id, step_id=step_id, state=state,
            agent=agent, failure=failure, reason=why_not,
            signature=str(entry.get("signature") or ""))
    extra: dict[str, Any] = {step_recovery.STATE_KEY: state, "note": entry["sentence"],
                             "recovery_question": entry.get("question")}
    if step_recovery.resumes_on_update(entry, failure):
        extra["recovery_resume_on_update"] = step_recovery.boss_build()
    return {"ask": step_recovery.ask_error(entry), "output": extra}


def _recover_build_step(
    connection: Connection, ctx: Any, *, project_id: str, run_id: str, step: Any,
    output: dict[str, Any], handle: dict[str, Any], build_run_id: str, status: str, why: str,
) -> Optional[dict[str, Any]]:
    """A build the step waited on failed (or finished without passing): the step's agent
    decides (step_recovery). A deploy step's own repair build is `deploy_repair`; a build step
    is `build`; any other step that waits on a build is not recovered here."""
    if ctx is None or not build_run_id:
        return None
    kind = _build_recovery_kind(connection, run_id, step, handle)
    if kind is None:
        return None
    if status == "cancelled":
        return None  # somebody stopped it: never started again by itself
    evidence = step_recovery.build_evidence(connection, build_run_id, why)
    failure = {**evidence["failure"], "build_run_id": build_run_id}
    evidence["failure"] = failure
    return _recover_awaited(connection, ctx, project_id=project_id, run_id=run_id, step=step,
                            output=output, handle=handle, kind=kind, failure=failure,
                            evidence=evidence)


def _build_recovery_kind(connection: Connection, run_id: str, step: Any,
                         handle: dict[str, Any]) -> Optional[str]:
    """Whose build this is: a deploy step's repair build (`deploy_repair`), a build step's own
    build (`build`), or None — any other step that waits on a build is not recovered here."""
    if handle.get("then") == deploy_repair.THEN_REDEPLOY:
        return "deploy_repair"
    spec_step = workflows._step_map(workflows._run_spec(connection, run_id)).get(
        str(step["step_id"])) or {}
    capability = str((spec_step.get("binding") or {}).get("capability") or "")
    return "build" if capability in ("build", "retry_build") else None


#: The step-output key naming the parked build whose findings the agent already answered.
FINDINGS_ANSWERED_KEY = "recovery_findings_answered"


def _recover_open_findings(
    connection: Connection, ctx: Any, *, project_id: str, run_id: str, step: Any,
    output: dict[str, Any], handle: dict[str, Any], build_run_id: str,
) -> Optional[dict[str, Any]]:
    """A build the step waits on is PARKED FOR A WAIVER: its reviewers left findings open after
    their fix rounds (governed_code_task_service, phase awaiting_gate_override).

    THE CASE (HelpDesk AI recording run, 2026-10-16): a security finding was still open, and
    the build went straight to a person to waive the gates. The step's OWN agent answers first
    (step_recovery, kind `review_findings`): told the failing gates and the reviewers' findings
    verbatim, it starts a repair build of the lineage to fix them (fix_app), and the step
    follows it. A person is asked to waive only after that — the same findings back after the
    fix, the budget spent, or the repair refused — and then the step goes on waiting on the
    build's own waiver card, never failed: the waiver (or a person's retry) is the answer.

    Decided ONCE per parked build (`FINDINGS_ANSWERED_KEY`), and only with a worker that can
    start a build (a job queue): a sweep that cannot act never asks the model. Returns the
    tick's record, or None when nothing was decided.

    WITH `open_pr_when_checks_fail` ON (the default, checks_failing.py) NOTHING WAITS FOR A
    WAIVER. A new build no longer parks at all (its PR opens with the checks failing), and a
    repair build the agent starts does the same. A build parked anyway (before the rule, or
    while it was off) still gets its agent first; where the agent would ask a person to waive
    — or recovery is off, or it already asked — BOSS opens the parked build's PR with its checks
    failing instead (code_task_service.open_parked_with_checks_failing) and the step goes on to
    the merge decision."""
    if ctx is None or getattr(ctx, "jobs", None) is None or not build_run_id:
        return None
    try:
        with connection.begin_nested():
            row = connection.execute(
                text("SELECT phase, error FROM code_task_runs WHERE id = :i"),
                {"i": build_run_id}).mappings().first()
    except Exception:  # noqa: BLE001 — unread, the build's card still holds the wait
        return None
    if not row or str(row["phase"] or "") != step_recovery.GATE_WAIT_PHASE:
        return None  # a plan to approve or a budget to raise is a person's, not the agent's
    if _build_recovery_kind(connection, run_id, step, handle) is None:
        return None
    from ..templates.gate import held_run

    try:
        with connection.begin_nested():
            if held_run(connection, build_run_id) is not None:
                return None  # a template gate cannot be fixed by waiving or by this loop
    except Exception:  # noqa: BLE001
        pass
    from . import checks_failing

    settings = getattr(ctx, "settings", None) or get_settings()
    open_instead = checks_failing.enabled(connection, settings, project_id)
    if str(output.get(FINDINGS_ANSWERED_KEY) or "") == build_run_id:
        # Already answered (asked to waive): under the rule the PR opens instead of waiting.
        return (_open_parked_with_checks_failing(
            connection, ctx, settings, project_id=project_id, run_id=run_id, step=step,
            build_run_id=build_run_id, why="the build was waiting for a waiver")
            if open_instead else None)
    evidence = step_recovery.build_evidence(connection, build_run_id, str(row["error"] or ""))
    failure = {**evidence["failure"], "build_run_id": build_run_id}
    evidence["failure"] = failure
    recovered = _recover_awaited(connection, ctx, project_id=project_id, run_id=run_id,
                                 step=step, output=output, handle=handle,
                                 kind=step_recovery.FINDINGS_KIND, failure=failure,
                                 evidence=evidence)
    if recovered is not None and recovered.get("done"):
        return recovered["done"]
    if open_instead:
        # The agent did not start a fix (it would ask a person, or recovery is off): the
        # parked build's PR opens with its checks failing, and the step goes on.
        if recovered is None:
            why = "the build's recovery agent did not act on it"
        else:
            said = dict(recovered.get("output") or {}).get(step_recovery.STATE_KEY) \
                or step_recovery.state_of(output)
            why = ("its agent could not close the findings: "
                   + str(step_recovery.last_decision(said).get("reasoning") or ""))
        opened = _open_parked_with_checks_failing(
            connection, ctx, settings, project_id=project_id, run_id=run_id, step=step,
            build_run_id=build_run_id, why=why)
        if opened is not None:
            return opened
    if recovered is None:
        return None
    # A PERSON IS ASKED NOW, AND ONLY NOW: the step keeps waiting on the build's waiver card,
    # saying what the agent tried and the one question.
    extra = dict(recovered.get("output") or {})
    state = extra.get(step_recovery.STATE_KEY) or step_recovery.state_of(output)
    last = step_recovery.last_decision(state)
    ask = step_recovery.waiver_ask(failure, reason=str(last.get("reasoning") or ""))
    connection.execute(
        text("UPDATE workflow_step_runs SET output = COALESCE(output, '{}'::jsonb) "
             "|| CAST(:o AS JSONB), updated_at = now() WHERE id = :i AND project_id = :p "
             "AND status = 'waiting'"),
        {"i": step["id"], "p": project_id,
         "o": json.dumps({step_recovery.STATE_KEY: state, FINDINGS_ANSWERED_KEY: build_run_id,
                          "note": str(extra.get("note") or last.get("sentence") or ""),
                          "recovery_question": ask["question"]}, default=str)})
    return {"run_id": run_id, "step_id": step["step_id"], "awaited": "code_task",
            "outcome": "asked_to_waive", "build": build_run_id}


def _open_parked_with_checks_failing(
    connection: Connection, ctx: Any, settings: Any, *, project_id: str, run_id: str,
    step: Any, build_run_id: str, why: str,
) -> Optional[dict[str, Any]]:
    """Open a parked build's PR with its checks failing (checks_failing.py), saying so on the
    step. None when it could not be opened (the step then waits as before, saying why)."""
    from . import code_task_service as cts

    try:
        result = cts.open_parked_with_checks_failing(
            ctx.engine, settings, ctx.jobs, build_run_id, actor="boss", why=why[:300])
    except Exception as error:  # noqa: BLE001 — said on the step, never a crash
        log.warning("checks-failing PR for %s not opened: %s", build_run_id, error)
        result = {"status": "refused", "why": str(error)[:300]}
    if str(result.get("status") or "") != "completed":
        connection.execute(
            text("UPDATE workflow_step_runs SET output = COALESCE(output, '{}'::jsonb) "
                 "|| CAST(:o AS JSONB), updated_at = now() WHERE id = :i AND project_id = :p "
                 "AND status = 'waiting'"),
            {"i": step["id"], "p": project_id,
             "o": json.dumps({FINDINGS_ANSWERED_KEY: build_run_id,
                              "note": "The pull request could not open with its checks "
                                      f"failing: {result.get('why') or 'unknown'}"},
                             default=str)})
        return None
    emit_event(connection, project_id, "workflow.build_opened_with_checks_failing", "boss",
               "workflow_run", run_id,
               {"step_id": step["step_id"], "build_run_id": build_run_id,
                "pull_request_url": result.get("pull_request_url"),
                "followup_run_id": result.get("followup_run_id"), "why": why[:300]})
    return {"run_id": run_id, "step_id": step["step_id"], "awaited": "code_task",
            "outcome": "opened_with_checks_failing", "build": build_run_id,
            "pull_request_url": result.get("pull_request_url")}


def _resume_recovery(connection: Connection, ctx: Any, *, project_id: str, run_id: str,
                     step: Any, output: dict[str, Any], handle: dict[str, Any],
                     note: Optional[str] = None, self_fix_id: Optional[str] = None
                     ) -> Optional[dict[str, Any]]:
    """A step the recovery loop parked, whose wait is over (its backoff passed, or BOSS was
    updated): a deploy step is queued to plan again (with this app's earlier releases to retire,
    for clean_and_retry); a build step starts its build again."""
    step_id = str(step["step_id"])
    state = step_recovery.state_of(output)
    updated = handle.get("then") == step_recovery.THEN_BOSS_UPDATE
    if updated:
        state = step_recovery.open_window(
            state, why=(f"BOSS fixed itself ({self_fix_id}; now {step_recovery.boss_build()})"
                        if self_fix_id else f"BOSS was updated (now {step_recovery.boss_build()})"))
    agent = state.get("agent") or "BOSS"
    retiring = bool(handle.get("retire_projects"))
    why_resumed = "self_fix_installed" if self_fix_id else "boss_updated"
    note = (f"{agent}: {note}" if note else
            f"{agent}: BOSS was updated — trying again" if updated else
            f"{agent}: retiring this app's earlier release and deploying again" if retiring else
            f"{agent}: trying again")
    kind = str(handle.get("step_kind") or "deploy")
    if kind == "deploy":
        workflows.requeue_waiting_step(
            connection, project_id=project_id, step_run_id=step["id"],
            output={step_recovery.PENDING_KEY: {
                        "action": handle.get("action") or step_recovery.RETRY,
                        "retire_projects": list(handle.get("retire_projects") or []),
                        "decision": handle.get("decision")},
                    step_recovery.STATE_KEY: state, "note": note},
            event=step_recovery.RESUMED_EVENT,
            data={"why": why_resumed if updated else "backoff_over", "sentence": note,
                  **({"self_fix_id": self_fix_id} if self_fix_id else {}),
                  **({"retire_projects": handle["retire_projects"]} if retiring else {})})
        return {"run_id": run_id, "step_id": step_id, "outcome": "running_again",
                "awaited": "self_fix" if self_fix_id else "recovery",
                "why": why_resumed if updated else "backoff_over"}
    build_run_id = str(handle.get("build_run_id") or "")
    new_id, why_not = _start_recovery_build(
        connection, ctx, project_id=project_id, run_id=run_id, step_id=step_id,
        build_run_id=build_run_id,
        note=("BOSS was updated since this build failed for a cause BOSS placed on itself "
              f"({handle.get('signature') or 'BOSS-side'}). Build it again as it was.")) \
        if build_run_id else ("", "no build to start again")
    if not new_id:
        workflows.force_fail_waiting_step(
            connection, project_id=project_id, step_run_id=step["id"],
            error=f"BOSS was updated, but the build could not be started again: {why_not}"[:500],
            output={step_recovery.STATE_KEY: state})
        return {"run_id": run_id, "step_id": step_id, "outcome": "failed", "because": why_not}
    state["build_ids"] = [*state.get("build_ids", []), new_id]
    _park_for_recovery(connection, project_id, step["id"],
                       {"kind": "code_task", "id": new_id,
                        **({"then": deploy_repair.THEN_REDEPLOY} if kind == "deploy_repair"
                           else {})},
                       {"code_task_run_id": new_id, "note": note,
                        step_recovery.STATE_KEY: state})
    emit_event(connection, project_id, step_recovery.RESUMED_EVENT, "boss", "workflow_run",
               run_id, {"step_id": step_id, "why": why_resumed, "build_run_id": new_id,
                        "sentence": note,
                        **({"self_fix_id": self_fix_id} if self_fix_id else {})})
    return {"run_id": run_id, "step_id": step_id, "outcome": "building_again",
            "awaited": "self_fix" if self_fix_id else "recovery", "now": new_id}


def _revive_after_boss_update(connection: Connection, *, project_id: str, run_id: str,
                              step: Any, output: dict[str, Any]) -> list[dict[str, Any]]:
    """A FAILED step whose agent asked a person about a cause BOSS placed on itself or on the
    host: when BOSS's build changes, the step is tried again by itself (retry_failed_step, the
    one Try again) — a BOSS fix may be all it needed."""
    marked = str(output.get("recovery_resume_on_update") or "")
    if not marked or marked == step_recovery.boss_build():
        return []
    state = step_recovery.state_of(output)
    point = connection.begin_nested()
    try:
        connection.execute(
            text("UPDATE workflow_step_runs SET output = output - 'recovery_resume_on_update' "
                 "WHERE id = :i"), {"i": step["id"]})
        workflows.retry_failed_step(
            connection, project_id=project_id, run_id=run_id, step_id=str(step["step_id"]),
            actor=str(state.get("role") or "boss"),
            note="BOSS was updated since this step failed; trying it again by itself.")
        point.commit()
    except Exception as error:  # noqa: BLE001 — a person's Retry still works
        point.rollback()
        log.info("step %s/%s not revived after the update: %s", run_id, step["step_id"], error)
        return []
    emit_event(connection, project_id, step_recovery.RESUMED_EVENT, "boss", "workflow_run",
               run_id, {"step_id": step["step_id"], "why": "boss_updated",
                        "sentence": f"{state.get('agent') or 'BOSS'}: BOSS was updated — "
                                    "trying the step again"})
    return [{"run_id": run_id, "step_id": step["step_id"],
             "outcome": "revived_after_update"}]


def _security_step(connection: Connection, ctx: Any, project_id: str, key: str,
                   context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    """A security review step with no tool. A refusal is SETTLED, not retried: the same two scans
    give the same verdict, and the operator decides what happens next (Retry after another fix,
    or stop the run)."""
    try:
        return security_review_steps.perform(connection, getattr(ctx, "settings", None),
                                             project_id, key, context, inputs)
    except security_review_steps.StepRefused as refused:
        raise SettledCapabilityError(str(refused)) from refused


def _security_rereview(connection: Connection, ctx: Any, project_id: str,
                       context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    return _security_step(connection, ctx, project_id, "security_rereview", context, inputs)


def _security_report(connection: Connection, ctx: Any, project_id: str,
                     context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    return _security_step(connection, ctx, project_id, "security_report", context, inputs)


#: An async tool reporting that the work is already over. Rare but real — a capability can find
#: the thing it was asked to start already finished — and it is the one case where an async step
#: legitimately completes without anything to wait on. Everything else is a defect.
#:
#: "running", "accepted", "queued" are deliberately NOT here: those say the work has begun, which
#: is exactly the state that must be waited on rather than celebrated.
_DONE_STATUSES: frozenset[str] = frozenset({"completed", "succeeded", "observed", "skipped"})


#: What an async capability is waiting on, and where its tool puts the id. The FIRST key that is
#: present wins, so a tool that grows a more specific field later does not need this touched.
_AWAIT_KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "research": ("discovery", ("discovery_id", "run_id", "id")),
    "meeting": ("meeting", ("meeting_run_id", "run_id", "id")),
    "build": ("code_task", ("code_task_run_id", "run_id", "id")),
    # The architecture review a person answers: approved, sent back, or rejected.
    "architecture": ("architecture", ("architecture_review_id", "id")),
    # STARTED IS NOT SERVING. start_preview returns as soon as the request is accepted, so the
    # step completed while the app was still booting — and the workflow walked on to `deploy`
    # with nothing anybody could look at. "Without a preview, do not move to deploy" is the
    # whole reason this step sits where it does.
    "preview": ("preview", ("preview_id", "id")),
    # PLRR closes the gaps it finds by WRITING CODE, so when it finds any it hands back a code
    # task and waits for it exactly as `build` does. A review that only files findings leaves
    # the operator to do the work the review exists to prevent.
    "plrr": ("code_task", ("code_task_run_id", "run_id", "id")),
    "more_tests": ("code_task", ("code_task_run_id", "run_id", "id")),
    # PLANNED IS NOT SHIPPED. `deploy_app` answers `{"status": "planned", "deployment_run_id"}`
    # and `redeploy` starts a new deployment run; both went green on that answer, so a
    # deployment that then failed its health check — or was never applied — never reached the
    # step, and the workflow walked on to monitoring an app that was not there. The step waits
    # on the deployment run, like a build waits on its code task.
    "deploy": ("deployment", ("deployment_id", "deployment_run_id", "id")),
    "redeploy": ("deployment", ("deployment_id", "deployment_run_id", "id")),
    # RETRIED IS NOT REBUILT. `retry_code_task` answers `{"status": "retried", "result":
    # {"run_id": <the new attempt>}}` the moment the attempt is queued; the step waits on that
    # attempt as `build` waits on the first.
    "retry_build": ("code_task", ("code_task_run_id", "run_id", "id")),
    # THE SECURITY REVIEW: a scan is done when the worker has run every tool; the panel is a
    # meeting; the fix is a build of the app.
    "security_scan": ("security_scan", ("scan_id", "id")),
    "security_panel": ("meeting", ("meeting_run_id", "run_id", "id")),
    "security_fix": ("code_task", ("code_task_run_id", "run_id", "id")),
    # WAITING FOR CI IS WAITING. The step records a ci_waits row and parks on it; every tick
    # takes one look at GitHub (with backoff) until the checks finish or the wait times out.
    "wait_for_ci": ("ci_wait", ("ci_wait_id", "id")),
    # VERIFYING RECOVERY IS WAITING TOO: on an incident_recovery_checks row that every tick
    # judges from the app's own scrapes (incident_response.look_at_recovery).
    "verify_recovery": ("recovery_check", ("recovery_check_id", "id")),
}

#: Envelopes a capability's tool nests its handle in, beyond the ones named after the kind and
#: the capability. Named per capability rather than searched for, for the reason
#: `_awaited_handle` gives: a blind search finds the wrong id as happily as the right one.
_AWAIT_ENVELOPES: dict[str, tuple[str, ...]] = {
    "retry_build": ("result",),
}


#: The name downstream steps use for the thing a kind produced — the FIRST field of its await
#: spec, which is also what the capability registry declares it `produces`.
#:
#: `preview` and `deploy` bind {"from_step": "build", "field": "code_task_run_id"}, but
#: create_code_task returns `run_id` and an adopted build calls no tool at all — so that field
#: existed in the catalogue and in nothing else, and every standard workflow would have failed
#: at preview after a successful build. Derived rather than written out a second time, because
#: two hand-maintained lists of the same names is how they came to disagree in the first place.
_CANONICAL_FIELD: dict[str, str] = {kind: fields[0] for kind, fields in _AWAIT_KINDS.values()}


#: Statuses that mean THE WORK DID NOT HAPPEN, however politely the capability says so.
#:
#: The runner failed a step only on "failed". `deploy` returns {"status": "blocked",
#: "problems": [...]} when its preflight refuses — and blocked fell through to SUCCEEDED. A
#: workflow therefore showed Deploy ✓ succeeded 0s, and marched on to monitoring, for a
#: deployment that never ran: the Infra panel two clicks away showed the same release BLOCKED,
#: and the app's URL answered nothing. Asked which screen was right, the honest answer was
#: neither — Infra was right about the deployment and the workflow was lying about it.
#:
#: A step that reports it did not do the thing must not be recorded as having done it. What
#: happens next is a policy question; claiming success is not an answer to it.
_DID_NOT_SHIP = workflow_outcomes.DID_NOT_SHIP


def _settle_started_work(capability: Any, key: str, result: dict[str, Any]) -> dict[str, Any]:
    """A tool that has accepted work has not finished it.

    An async capability's tool returns as soon as the work is ACCEPTED: create_code_task hands
    back a run id in milliseconds for a build that takes twenty minutes. Completing the step
    there made the runner race past `build` while the build was still cloning.

    Marked as awaiting instead. The caller parks the step against this id, and `resume_awaited`
    completes it when the work reaches a terminal state.

    `plrr` has to come through here too. It returns from the `tool is None` branch, and
    `extend_code_task` reports `status: extending` with the new run id nested under `result`.
    Left as-is, the runner records the step succeeded and the workflow previews and opens the
    deploy gate while that repair is still editing the branch.
    """
    if key in ("plrr", "more_tests", "build") and not str(
        result.get("run_id") or result.get("code_task_run_id") or ""
    ).strip():
        inner = result.get("result")
        if isinstance(inner, dict):
            nested = str(inner.get("code_task_run_id") or inner.get("run_id") or "").strip()
            if nested:
                result["run_id"] = nested

    # A TOOL THAT SAID IT DID NOT DO THE WORK IS NOT PARKED. `deploy_build` answers a policy
    # block as `{"status": "blocked", "deployment_run_id": ...}` — there is an id, but nothing
    # will ever run under it, and overwriting `blocked` with `awaiting` would hide the refusal
    # behind a wait. The verdict (`workflow_outcomes`) fails it now, with the tool's reason.
    if str(result.get("status") or "").lower() in _DID_NOT_SHIP:
        return result

    awaited = _awaited_handle(key, result)
    if awaited:
        result["status"] = "awaiting"
        result["await"] = awaited
        # create_code_task returns `run_id`; the catalogue reads `code_task_run_id`. Writing the
        # canonical name here is what makes "deploy the thing you just built" resolve.
        field = _CANONICAL_FIELD.get(awaited["kind"])
        if field:
            result.setdefault(field, awaited["id"])
        return result

    # AN ASYNC STEP THAT CANNOT BE CORRELATED HAS NOT SUCCEEDED.
    #
    # This used to fall through and complete, on the reasoning that parking a step with no id
    # would hang the run. That reasoning was wrong in the direction that costs the most: the
    # meeting capability returned its id one level down, no handle was found, and the step
    # reported success nought seconds after convening a panel that was still arguing. The run
    # walked on to the spec gate with no PRD behind it.
    #
    # A run that stops with a reason can be fixed. A run that claims work happened cannot even
    # be noticed. Adoption returns earlier, so reaching here means a tool really was invoked.
    # ONLY FOR WORK THIS RUNTIME KNOWS HOW TO WAIT ON.
    #
    # `key in _AWAIT_KINDS` is not decoration, it is the bug this line already caused once.
    # `deploy` was declared is_async with no await table, and the first version indexed
    # _AWAIT_KINDS[key] while building the error message: KeyError('deploy') on every deploy
    # step, which escaped the handler that releases leases, so five deploy steps sat `leased`
    # for fifteen hours showing a running timer, and the log read `(non-fatal): 'deploy'`.
    #
    # Every async capability now HAS an await spec — deploy waits on its deployment run — and
    # the catalogue test refuses one without. The guard stays, because a guard must not assume.
    if (capability.is_async and key in _AWAIT_KINDS
            and str(result.get("status") or "") not in _DONE_STATUSES):
        tool_name = result.get("tool") or capability.tool or key
        result["status"] = "failed"
        result["error"] = (
            f"{tool_name} started work but returned no id to wait on, so this step "
            f"cannot tell when it finished. Looked for "
            f"{', '.join(_AWAIT_KINDS[key][1])} in the result"
            f"{f' and in its {key!r} envelope' if key in _AWAIT_KINDS else ''}."
        )
    return result


def _awaited_handle(key: str, output: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The work this step started, or None when there is nothing to wait for.

    Looked for one level down as well as at the top, because the tools do not agree on shape:
    create_discovery returns {"run_id": ...} but request_meeting returns
    {"meeting": {"run_id": ...}}. Reading only the top level found the discovery and missed the
    meeting, so a panel that had just been convened was recorded as one that had finished —
    and the spec gate asked for approval of a PRD that did not exist yet.

    Only the envelope named after the kind or the capability is opened. A blind search for any
    nested `run_id` would have found request_meeting's `task_id` sibling just as happily, and
    parked the step against the wrong object.
    """
    if key in ("build", "retry_build") and isinstance(output.get("change_set_id"), str) \
            and output["change_set_id"].strip():
        # A build of an app with several repositories is ONE change set (multirepo).
        return {"kind": "change_set", "id": output["change_set_id"].strip()}
    if key == "deploy" and isinstance(output.get("stack_deployment_id"), str) \
            and output["stack_deployment_id"].strip():
        # A DEPLOY THAT SHIPS FROM THE APP'S OWN MANIFEST is a stack deployment, and the step
        # waits on that — never "no deployment id, so it must be done".
        return {"kind": "stack_deployment", "id": output["stack_deployment_id"].strip()}
    spec = _AWAIT_KINDS.get(key)
    if not spec:
        return None
    kind, fields = spec
    envelopes = tuple(output.get(name) for name in _AWAIT_ENVELOPES.get(key, ()))
    for scope in (output, output.get(kind), output.get(key), *envelopes):
        if not isinstance(scope, dict):
            continue
        for field in fields:
            value = scope.get(field)
            if isinstance(value, str) and value.strip():
                return {"kind": kind, "id": value.strip()}
    return None


def _preview_not_permitted(key: str, output: dict[str, Any],
                           actor: Optional[str]) -> Optional[dict[str, Any]]:
    """A preview the step's agent may not start is SKIPPED, in words — not a deadletter.

    The Captain's improve passes ran a workflow whose preview step runs as the QA agent, whose
    Passport does not hold preview:manage. Every pass deadlettered with the raw
    "'start_preview' needs preview:manage", and the next pass did the same. A preview is for a
    person to look at; the build and its tests already stand without it. Any other refusal still
    fails the step.
    """
    if key != "preview" or str(output.get("error") or "") != "forbidden":
        return None
    permission = str(output.get("permission") or "preview:manage")
    who = str(actor or "the step's agent")
    reason = (f"Preview skipped: {who} is not allowed to start previews ({permission}). "
              "Grant it on that agent's Passport to get a preview here, or start one from the "
              "build's card.")
    return {**output, "ok": True, "status": "skipped", "skipped": True, "error": None,
            "reason": reason, "message": reason, "summary": reason,
            # Said on the step as what it is: a whole step that did not run.
            "note": f"skipped: {reason}"}


def _tool_failed_to_start(key: str, output: dict[str, Any]) -> Optional[str]:
    """A sentence when the tool reported failure and there is nothing to wait on.

    start_preview returns `{preview: null, error: preview_unavailable_no_pr}` when the build
    has no PR. Completing that step as success lets deploy start with nothing to look at.
    """
    if key != "preview":
        return None
    if _awaited_handle(key, output):
        return None
    # Explicit failure only. A payload that merely omitted the nested record is the old
    # flat shape (preview_url, preview_id at the top level) and is not a failed start.
    if output.get("error") or ("preview" in output and output.get("preview") is None):
        return str(
            output.get("message") or output.get("error") or "preview did not start"
        )
    return None


def _build_waits_on_a_person(connection: Any, run_id: str) -> bool:
    """A blocked build that is waiting on a person (plan approval, a gate waiver, a budget
    refusal), not stuck: the Factory's attention kinds for it, factory spec §6.5."""
    try:
        row = connection.execute(
            text("SELECT phase, finalize_ctx->>'reason_code' AS reason FROM code_task_runs "
                 "WHERE id = :id"), {"id": run_id}).mappings().first()
    except Exception:  # noqa: BLE001
        return False
    if not row:
        return False
    if str(row["phase"] or "") == "awaiting_gate_override":
        # A failed TEMPLATE gate cannot be waived: nobody can answer that wait, so the build is
        # blocked, not waiting (templates.gate.held_run), and the step above it fails with why.
        from ..templates.gate import held_run

        try:
            with connection.begin_nested():
                if held_run(connection, run_id) is not None:
                    return False
        except Exception:  # noqa: BLE001
            pass
    return (str(row["phase"] or "") in ("awaiting_plan_approval", "awaiting_gate_override")
            or str(row["reason"] or "") == "budget_denied")


#: The await kind of a meeting step held for its panel's PRD decision (`_hold_for_spec`).
SPEC_DECISION_KIND = "spec_decision"

#: How to tell whether awaited work is done, per kind: the table, and which statuses are
#: terminal. Anything not listed is still in flight.
_AWAIT_TABLES: dict[str, tuple[str, frozenset[str], frozenset[str]]] = {
    # table, succeeded-statuses, failed-statuses
    "code_task": ("code_task_runs",
                  frozenset({"completed"}),
                  frozenset({"failed", "blocked", "cancelled"})),
    "discovery": ("discovery_runs",
                  frozenset({"completed", "succeeded"}),
                  frozenset({"failed", "cancelled"})),
    "meeting": ("meeting_runs",
                frozenset({"completed", "succeeded"}),
                frozenset({"failed", "cancelled"})),
    # A PANEL'S PRD DECISION, which a meeting step that carries the spec ask waits on once the
    # panel has finished (`_hold_for_spec`). Approved releases the step with the panel's PRD;
    # a person's No fails it. Pending, delegated or an expired hold-default are still waiting.
    SPEC_DECISION_KIND: ("decisions",
                         frozenset({"approved", "executed", "completed"}),
                         frozenset({"rejected"})),
    "preview": ("app_previews",
                frozenset({"running", "verified"}),
                frozenset({"failed", "stopped", "expired"})),
    # `awaiting_approval` and `running` are deliberately absent. Those are the states
    # where a person has not answered yet, or the approved run is still working. Treating
    # either as finished is the bug: the step went green and the next step ran.
    "agent_run": ("agent_runs",
                  frozenset({"completed"}),
                  frozenset({"failed", "cancelled", "denied", "error"})),
    # A deployment has SUCCEEDED when its health check passed (`succeeded`, or `healthy` from
    # Resource Plane's verify). `deployed` is not here: it means the release was placed and has
    # not been health-checked yet. `blocked` is a policy or preflight refusal.
    "deployment": ("deployment_runs",
                   frozenset({"succeeded", "healthy"}),
                   frozenset({"failed", "blocked", "cancelled", "rolled_back",
                              "rollback_pending", "unhealthy"})),
    # A stack deployment (boss.deploy.yaml) is done when its health checks passed on the host.
    # `awaiting_approval` and `running` are in flight: a person has not answered, or the apply
    # is still going.
    "stack_deployment": ("stack_deployments",
                         frozenset({"healthy"}),
                         frozenset({"failed", "rejected"})),
    # A scan with every tool run is `done`; one whose tools all failed (or that could not run)
    # is `failed`, with why in counts.error.
    "security_scan": ("security_scans", frozenset({"done"}), frozenset({"failed"})),
    # An architecture is done when a PERSON approved it. `awaiting_approval`,
    # `revision_requested` and `drafting` are in flight: nobody has answered, or the architect
    # is writing the version a person asked for.
    "architecture": ("architecture_reviews",
                     frozenset({"approved"}),
                     frozenset({"rejected", "cancelled", "failed"})),
    # CI (services/ci_gate.py): passed, or no CI configured at all, lets the run go on; a failed
    # check or a timeout fails the step with the words and the failing check's link.
    "ci_wait": ("ci_waits",
                frozenset({"passed", "no_ci"}),
                frozenset({"failed", "timed_out", "unreadable", "cancelled"})),
    # Recovered means BOSS's monitor saw the error rate back at baseline for the stability
    # window; timing out is a fix that did not hold (or traffic that never came back).
    "recovery_check": ("incident_recovery_checks",
                       frozenset({"recovered"}),
                       frozenset({"timed_out", "failed"})),
    # A multi-repo change set (multirepo/change_sets.py) is done when every repository merged
    # and every component is live (or ships with the merge). `blocked` is not failed: a lane is
    # being retried or a person is deciding; `merged` waits for its deploy.
    "change_set": ("repo_change_sets",
                   frozenset({"live"}),
                   frozenset({"failed", "rejected", "cancelled", "rolled_back"})),
}

#: The column a table keeps its status in, when it is not `status`.
_STATUS_COLUMN: dict[str, str] = {"repo_change_sets": "state"}

#: Where a table keeps the reason its run ended, when it is not a column called `error`.
_REASON_COLUMN: dict[str, str] = {"decisions": "resolution_note", "ci_waits": "summary", "incident_recovery_checks": "summary",
                                  "deployment_runs": "failure_reason",
                                  "stack_deployments": "failure_reason",
                                  "repo_change_sets": "COALESCE(error, blocked_reason)",
                                  "security_scans": "counts->>'error'"}


def _terminal_evidence(
    connection: Connection, kind: str, awaited_id: str
) -> dict[str, Any]:
    """What the finished work produced, for the step that was waiting on it.

    A build's pull request is both its outcome and the input every later step needs, so it goes
    onto the step's output rather than being looked up again by preview and deploy.
    """
    try:
        if kind == "architecture":
            # THE APPROVED DOCUMENT, so every later step's brief can carry it.
            from . import architecture_docs

            return architecture_docs.evidence(connection, awaited_id)
        if kind == "change_set":
            rows = connection.execute(
                text("SELECT full_name, pr_number, pr_url, merged_sha FROM change_set_prs "
                     "WHERE change_set_id = :c AND pr_number IS NOT NULL ORDER BY wave"),
                {"c": awaited_id}).mappings().all()
            return {"change_set_id": awaited_id,
                    "pull_requests": [dict(r) for r in rows]}
        if kind == "code_task":
            row = connection.execute(
                text("SELECT pull_request_url, pull_request_number, "
                     "finalize_ctx->'checks_failing' AS checks_failing FROM code_task_runs "
                     "WHERE id = :r"),
                {"r": awaited_id},
            ).mappings().first()
            if not row or not row["pull_request_url"]:
                return {}
            out = {"pull_request_url": row["pull_request_url"],
                   "pull_request_number": row["pull_request_number"]}
            checks = row["checks_failing"]
            checks = json.loads(checks) if isinstance(checks, str) else checks
            if isinstance(checks, dict) and checks.get("gates"):
                # THE PR OPENED WITH ITS CHECKS FAILING (checks_failing.py): the step is done
                # and the run goes on to the merge decision, saying so — never a waiver wait.
                from .checks_failing import gate_names, headline

                out["checks_failing"] = {"gates": gate_names(checks),
                                         "security": bool(checks.get("security")),
                                         "followup_run_id": checks.get("followup_run_id")}
                out["note"] = ("PR open · checks failing: " + headline(checks)
                               + (" (security finding open)" if checks.get("security") else "")
                               + ". The merge is a person's decision.")
            return out
        if kind == "discovery":
            # WHAT THE RESEARCH FOUND, not just that it finished. Without this the research
            # step completes carrying nothing, and the panel that follows it debates the
            # original one-line directive as though no research had happened — which is
            # exactly what research was for.
            row = connection.execute(
                text("SELECT title, state FROM discovery_runs WHERE id = :r"),
                {"r": awaited_id},
            ).mappings().first()
            if not row:
                return {}
            return {"discovery_id": awaited_id,
                    "dossier": _dossier_brief(row["title"], row["state"] or {})}
        if kind == SPEC_DECISION_KIND:
            # THE PANEL'S PRD, now approved: the same evidence the meeting gives, plus who
            # approved it, so the build is briefed from the spec a person signed.
            row = connection.execute(
                text("SELECT m.id AS meeting_run_id, d.approved_by FROM decisions d "
                     "LEFT JOIN meeting_runs m ON m.decision_id = d.id WHERE d.id = :d "
                     "ORDER BY m.created_at DESC LIMIT 1"),
                {"d": awaited_id},
            ).mappings().first()
            if not row:
                return {}
            evidence = (_terminal_evidence(connection, "meeting", row["meeting_run_id"])
                        if row["meeting_run_id"] else {})
            return {**evidence, "spec_decision_id": awaited_id,
                    "spec_approved_by": row["approved_by"]}
        if kind == "meeting":
            # WHAT THE PANEL DECIDED, not just that it finished.
            #
            # The meeting capability declares it produces `prd_markdown`, and nothing ever put
            # one on the step: a finished meeting contributed `meeting_run_id` and a status,
            # so `build`'s binding to meeting.prd_markdown resolved to nothing at all. An
            # 18,979-character PRD — behavioural anchors, a visible rubric, per-interviewer
            # submission status, all argued out over 22 minutes — sat in meeting_runs.state
            # and reached no one, while the engineering agent built from a one-line directive.
            #
            # Exactly the gap the `discovery` branch above was written to close, one step
            # later. Binding the input without producing the output would have looked fixed
            # and changed nothing.
            row = connection.execute(
                text("SELECT state, task_id FROM meeting_runs WHERE id = :r"),
                {"r": awaited_id},
            ).mappings().first()
            if not row:
                return {}
            state = row["state"] or {}
            # JSONB decodes to a dict; some drivers and paths hand back the raw string.
            # resume_awaited already guards the same way for the same reason.
            if isinstance(state, str):
                try:
                    state = json.loads(state)
                except ValueError:
                    state = {}
            prd = str((state or {}).get("prd_markdown") or "").strip()
            evidence: dict[str, Any] = {"meeting_run_id": awaited_id}
            if row["task_id"]:
                evidence["task_id"] = row["task_id"]
            if prd:
                # THE PRD IS THE BRIEF, so it travels far more fully than a dossier does. A
                # dossier is evidence a panel argues FROM and is capped to leave room for the
                # directive; this is the specification being implemented, and a requirement
                # dropped by truncation is a requirement the build silently will not meet.
                evidence["prd_markdown"] = prd[:_PRD_CHARS]
            return evidence
        if kind == "deployment":
            # Where it went, so `smoke_test` can bind {"from_step": "deploy", "field":
            # "deployed_url"} instead of an operator pasting the address in.
            row = connection.execute(
                text("SELECT status, deployment_target_id FROM deployment_runs WHERE id = :r"),
                {"r": awaited_id},
            ).mappings().first()
            if not row:
                return {}
            evidence = {"deployment_id": awaited_id, "deployment_status": row["status"]}
            domains = connection.execute(
                text("SELECT domains FROM deployment_targets WHERE id = :t"),
                {"t": row["deployment_target_id"]},
            ).scalar()
            if isinstance(domains, str):
                try:
                    domains = json.loads(domains)
                except ValueError:
                    domains = [domains]
            first = next((str(d) for d in (domains or []) if str(d).strip()), "")
            if first:
                evidence["deployed_url"] = first if "://" in first else f"http://{first}"
            return evidence
        if kind == "ci_wait":
            row = connection.execute(
                text("SELECT status, summary, failing_url, pull_request_url FROM ci_waits "
                     "WHERE id = :r"), {"r": awaited_id}).mappings().first()
            if not row:
                return {}
            return {"ci_wait_id": awaited_id, "ci_status": row["status"],
                    "ci_summary": row["summary"], "failing_check_url": row["failing_url"],
                    "note": row["summary"]}
        if kind == "stack_deployment":
            row = connection.execute(
                text("SELECT status, open_url, commit_sha FROM stack_deployments WHERE id = :r"),
                {"r": awaited_id},
            ).mappings().first()
            if not row:
                return {}
            evidence = {"stack_deployment_id": awaited_id, "deployment_status": row["status"],
                        "commit": row["commit_sha"]}
            if row["open_url"]:
                evidence["deployed_url"] = row["open_url"]
            # WHO PASSED THIS STEP'S GATE. The deployment's decision is the deploy step's only
            # approval, so the step records it exactly as an answered gate would.
            decided = connection.execute(
                text("SELECT d.id, d.status, d.approved_by, "
                     "       d.recommendation->>'approver_role' AS approver_role "
                     "FROM stack_deployments s JOIN decisions d ON d.id = s.decision_id "
                     "WHERE s.id = :r"),
                {"r": awaited_id},
            ).mappings().first()
            if decided:
                evidence["approval"] = {
                    "decision_id": decided["id"], "status": decided["status"],
                    "approved_by": decided["approved_by"],
                    "approver_role": decided["approver_role"],
                }
            return evidence
        if kind == "security_scan":
            # WHAT THE SCAN FOUND, so the run card reads "12 findings" and the panel, the
            # re-scan and the re-review find the scan by its id.
            row = connection.execute(
                text("SELECT counts, commit_sha FROM security_scans WHERE id = :r"),
                {"r": awaited_id},
            ).mappings().first()
            if not row:
                return {}
            counts = row["counts"] if isinstance(row["counts"], dict) else {}
            return {"scan_id": awaited_id, "counts": counts, "commit_sha": row["commit_sha"],
                    "note": f"{int(counts.get('total') or 0)} findings"
                            + (f"; tools that failed: {', '.join(counts['failed_tools'])}"
                               if counts.get("failed_tools") else "")}
        if kind == "agent_run":
            # WHAT THE AGENT ACTUALLY PRODUCED, read after the approval, not the snapshot
            # taken when it stopped. The snapshot is missing the skill that needed a person
            # — that call had not run yet — and a later step wiring `media_artifact_id`
            # would bind the artifact from before the render.
            from .agent_steps import _used_skills, _what_the_skills_returned

            produced = _what_the_skills_returned(connection, awaited_id)
            produced.pop("await", None)
            produced.pop("status", None)
            row = connection.execute(
                text("SELECT output_text FROM agent_runs WHERE id = :r"),
                {"r": awaited_id},
            ).mappings().first()
            return {
                **produced,
                "summary": str((row or {}).get("output_text") or ""),
                "skills_used": _used_skills(connection, awaited_id),
                "agent_run_id": awaited_id,
            }
    except Exception as error:  # noqa: BLE001 — never fail a step over its own evidence
        log.warning("cannot read the result of %s: %s", awaited_id, error)
    return {}


#: How much of a dossier travels into the next step's prompt. Enough to change what a panel
#: argues about; not so much that it crowds out the directive itself.
_DOSSIER_CHARS = 4000

#: How much of a PRD travels into the build. Far more than a dossier, because this is not
#: evidence to argue from — it is the specification being implemented, and a requirement lost
#: to truncation is one the build will silently not meet. Real panels have produced 19k and
#: 25k characters; this leaves headroom above both without being unbounded.
_PRD_CHARS = 40000


#: The sections a dossier actually stores, in the order a reader wants them, with the heading
#: each becomes. Taken from a real completed run (drun_e615b07aa693491a8aba) rather than
#: guessed: the first version of this list read `brief`/`findings`/`analysis`/`outcomes`, none
#: of which exist, so the panel would have been handed a critique and a gate verdict while the
#: twelve actual findings and the market analysis were silently dropped.
_DOSSIER_SECTIONS: tuple[tuple[str, str], ...] = (
    ("research_brief", "What was researched"),
    ("signal_findings", "What the research found"),
    ("market_analysis", "Market analysis"),
    ("critique", "Where this is weak"),
    ("candidate_outcomes", "Outcomes worth considering"),
    ("gate", "Evidence gate"),
)


def _dossier_brief(title: Any, state: Any) -> str:
    """The research, as something a panel can read.

    The dossier is a nested document. Handing the panel the whole JSON would bury the
    directive; handing it nothing is what happened before this existed. These are the sections
    that change a product decision.
    """
    if not isinstance(state, dict):
        return ""
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
    for key, heading in _DOSSIER_SECTIONS:
        value = state.get(key)
        if not value:
            continue
        if key == "research_brief" and isinstance(value, dict):
            # `hypothesis` is the directive, restated and truncated mid-word by the research
            # service ("- Show the spread and flag where interviewers disagree most.\n- Prod").
            # The panel is already reading the directive; a broken copy of it is only noise.
            value = {k: v for k, v in value.items() if k != "hypothesis"}
            if not value:
                continue
        # RENDERED, NOT DUMPED — for the panel as much as for the reader.
        #
        # This was `json.dumps`, so the brief handed to the agent panel contained
        # `{"questions": [...], "hypothesis": "Build an interview scorecard app.\nWho it is
        # for: ...", "sources_consulted": [...]}` — truncated at 1200 characters, mid-URL. That
        # is a poor prompt AND, because the change record fell back to this text when the
        # structured dossier could not be read, it is what reached the PDF.
        if isinstance(value, str):
            body = value.strip()
        elif isinstance(value, list):
            body = "\n".join(
                f"- {change_record.render_value(v)}" for v in value[:12]
            )
        else:
            body = change_record.render_value(value)[:2400]
        if body:
            parts.append(f"## {heading}\n{body}")
    return "\n\n".join(parts)[:_DOSSIER_CHARS]


#: Steps whose failure never stops the run (operator, 2026-10-13: "pass 2 never blocks pass
#: 1"). The ONE exception to "failed is terminal for the step": a failed More tests pass
#: completes its step with the failure said, and the run carries on with pass 1's build.
_ADVISORY_CAPABILITIES = frozenset({"more_tests"})


def _complete_advisory_failure(connection: Connection, *, project_id: str, run_id: str,
                               step: Any, output: dict, status: str, why: str,
                               done: list) -> bool:
    """Complete an advisory step whose work failed; True when it did."""
    if str((output or {}).get("capability") or "") not in _ADVISORY_CAPABILITIES:
        if not (output or {}).get("advisory"):
            return False
        # AN AUTOMATIC PREVIEW (two_pass) THAT DID NOT COME UP says so and the run goes on.
        workflows.force_complete_waiting_step(
            connection, project_id=project_id, step_run_id=step["id"],
            output={**output, "status": "not_applicable", "advisory_failed": True,
                    "awaited_status": status,
                    "note": (f"this app could not be previewed: the preview ended as "
                             f"'{status}'" + (f" ({why})" if why else "")
                             + ". The run goes on without one.")[:600]},
        )
        done.append({"run_id": run_id, "step_id": step["step_id"],
                     "outcome": "advisory_failed", "awaited_status": status})
        return True
    base = str(output.get("base_run_id") or "")
    workflows.force_complete_waiting_step(
        connection, project_id=project_id, step_run_id=step["id"],
        output={**output, "status": "succeeded", "advisory_failed": True,
                "awaited_status": status, "code_task_run_id": base,
                "note": (f"the More tests pass ended as '{status}'"
                         + (f" ({why})" if why else "")
                         + "; the run carries on with the first pass's build.")[:600]},
    )
    done.append({"run_id": run_id, "step_id": step["step_id"], "outcome": "advisory_failed",
                 "awaited_status": status})
    return True


def resume_awaited(
    connection: Connection, *, project_id: str, run_id: str, worker_id: str,
    ctx: Any = None,
) -> list[dict[str, Any]]:
    """Finish steps whose awaited work has reached a terminal state.

    Without this a parked step waits forever: nothing else in the system knows a workflow step
    is watching a code task. Runs every tick, before claiming new work, so a build that
    finished a moment ago releases the next step in the same pass rather than a tick later.

    A step whose work FAILED fails the step, with the underlying status in the message, and the
    run stops there until an operator retries or skips it. No kind of work is exempt. Silently
    succeeding would let a workflow report a shipped product on top of a build that did not.

    FIX AND REDEPLOY (deploy_repair), with `ctx`: a deploy step whose stack deployment failed
    for the APP's reason waits on a repair build instead of failing; when that build completes
    the step is queued to deploy again, and when it fails the step fails with both causes.
    Without `ctx` (a revival sweep) nothing is started.
    """
    parked = connection.execute(
        text(
            """
            SELECT id, step_id, output, status FROM workflow_step_runs
            WHERE workflow_run_id = :run_id AND project_id = :project_id
              -- FAILED STEPS TOO, and that is the case that actually happens.
              --
              -- The order of events is: the build dies, the step fails, and THEN the operator
              -- retries. Sweeping only `waiting` steps meant the recovery could never fire for
              -- the real sequence — the operator watched a new build run to completion while
              -- the workflow above it stayed "failed — the code_task it started finished as
              -- 'cancelled'", stuck at 3 of 8.
              --
              -- A failed step is only reconsidered when a LIVE later attempt at the same work
              -- exists (see `_successor_attempt`). Nothing else about it is revisited.
              AND status IN ('waiting', 'failed')
            """
        ),
        {"run_id": run_id, "project_id": project_id},
    ).mappings().all()

    done: list[dict[str, Any]] = []
    # THE VERDICT OF A RESUMED SELF-FIX (Stage 12): the step it was for passed, or failed for a
    # new cause (verified), or for the same one (rolled back).
    try:
        with connection.begin_nested():
            for settled in self_fix.settle_run(
                    connection, getattr(ctx, "settings", None) or get_settings(), run_id=run_id):
                done.append({"run_id": run_id, "outcome": "self_fix_settled", **settled})
    except Exception as error:  # noqa: BLE001 — a verdict never stops the sweep
        log.warning("self-fix verdicts for %s not settled: %s", run_id, error)
    for step in parked:
        output = step["output"] or {}
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except Exception:  # noqa: BLE001
                continue
        handle = (output or {}).get("await") or {}
        kind, awaited_id = str(handle.get("kind") or ""), str(handle.get("id") or "")

        # A FAILED STEP IS ONLY HERE TO BE RESCUED BY A RETRY.
        #
        # Everything below this point decides the fate of work in flight, and a failed step has
        # already had its verdict. The one thing that legitimately reopens it is a later attempt
        # at the same work now running — so that is the only question asked of it.
        if str(step["status"]) == "failed":
            successor = (_successor_attempt(connection, project_id, awaited_id, run_id)
                         if kind == "code_task" and awaited_id else None)
            if successor:
                workflows.reopen_failed_step(
                    connection, project_id=project_id, step_run_id=step["id"],
                    handle={**handle, "id": successor},
                    note=(f"the build was retried; this step now follows {successor} "
                          f"(the earlier attempt {awaited_id} did not finish)"),
                )
                done.append({"run_id": run_id, "step_id": step["step_id"],
                             "outcome": "following_retry", "awaited": kind,
                             "was": awaited_id, "now": successor})
            elif output.get("recovery_resume_on_update"):  # retried after a BOSS update
                done.extend(_revive_after_boss_update(connection, project_id=project_id,
                                                      run_id=run_id, step=step, output=output))
            continue

        if kind == self_fix.AWAIT_KIND:
            # WAITING ON A BOSS SELF-FIX (services/self_fix.py): resumes when the fix is
            # installed and running; falls back to a BOSS-update wait when it did not hold.
            followed = _follow_self_fix(connection, ctx, project_id=project_id, run_id=run_id,
                                        step=step, output=output, handle=handle)
            if followed:
                done.append(followed)
            continue
        if kind == step_recovery.AWAIT_KIND:
            # PARKED BY THE RECOVERY LOOP: a backoff, or a BOSS update. Nothing moves without a
            # worker context (a revival sweep), and nothing before its wait is over.
            if ctx is not None and step_recovery.due(handle):
                resumed = _resume_recovery(connection, ctx, project_id=project_id,
                                           run_id=run_id, step=step, output=output,
                                           handle=handle)
                if resumed:
                    done.append(resumed)
            continue
        table_spec = _AWAIT_TABLES.get(kind)
        if not table_spec or not awaited_id:
            # A step waiting for a timer, a gate or a subworkflow. Those have their own
            # resumption paths and must not be touched here.
            continue
        table, ok_states, bad_states = table_spec
        if kind == "ci_wait":
            _look_at_ci(connection, project_id, awaited_id)
        if kind == "recovery_check":
            _look_at_recovery(connection, awaited_id)
        try:
            row = connection.execute(
                text(f"SELECT {_STATUS_COLUMN.get(table, 'status')} AS status FROM {table} "  # noqa: S608
                     "WHERE id = :id"),  # fixed set
                {"id": awaited_id},
            ).mappings().first()
        except Exception as error:  # noqa: BLE001 — a missing optional table is not fatal
            log.warning("cannot check awaited %s %s: %s", kind, awaited_id, error)
            continue
        if not row:
            continue
        status = str(row["status"] or "")
        if kind == "code_task" and not output.get(_SPEC_GATE_CHECKED):
            _mark_skipped_spec_gate(connection, run_id=run_id, step_run_id=str(step["id"]))
        if kind == "code_task" and status == "blocked" and _build_waits_on_a_person(
                connection, awaited_id):
            # A BUILD WAITING ON THE OPERATOR HAS NOT FAILED (factory walk). A plan to approve,
            # checks to waive or a budget to raise is the build's own card; failing the step
            # above it added a second card for the same wait ("Build, review and test failed").
            # UNLESS THE PERSON ALREADY ANSWERED IT BY RETRYING (Factory › Retry on a build
            # parked for a waiver, HelpDesk AI wfr_f383649a8b024d97a80c): the retry is a new
            # attempt of the lineage, and the old one stays parked forever — the step must
            # follow the attempt the person started, or it waits on an answered question.
            successor = _successor_attempt(connection, project_id, awaited_id, run_id)
            if successor:
                workflows.repoint_waiting_step(
                    connection, project_id=project_id, step_run_id=step["id"],
                    handle={**handle, "id": successor},
                    note=(f"a person retried the build that was waiting on them; this step "
                          f"now follows {successor} (the earlier attempt {awaited_id})"),
                )
                done.append({"run_id": run_id, "step_id": step["step_id"],
                             "outcome": "following_retry", "awaited": kind,
                             "was": awaited_id, "now": successor})
                continue
            # OPEN REVIEW FINDINGS ARE THE STEP'S AGENT'S FIRST (step_recovery): it fixes them
            # in a repair build before anybody is asked to waive them.
            answered = _recover_open_findings(
                connection, ctx, project_id=project_id, run_id=run_id, step=step,
                output=output, handle=handle, build_run_id=awaited_id)
            if answered:
                done.append(answered)
            continue
        if kind == "code_task" and (status == "repairing" or status in ok_states):
            # A REPAIRED ATTEMPT IS NOT THE RESULT, AND A FINISHED BUILD IS NOT A PASSED ONE.
            # `repairing` means the attempt failed its checks and handed its work to a repair
            # round: follow that attempt. A `completed` build a template gate stopped (or an
            # attempt superseded by its repair) did not pass: fail the step with why, so the
            # deploy after it never offers a release for it.
            from .run_fencing import build_not_passed

            not_passed = None if status == "repairing" else build_not_passed(
                connection, awaited_id)
            if status == "repairing" or not_passed:
                successor = _successor_attempt(connection, project_id, awaited_id, run_id)
                if successor:
                    workflows.repoint_waiting_step(
                        connection, project_id=project_id, step_run_id=step["id"],
                        handle={**handle, "id": successor},
                        note=(f"the build failed its checks and is being repaired; this step "
                              f"now follows {successor} (the earlier attempt {awaited_id})"),
                    )
                    done.append({"run_id": run_id, "step_id": step["step_id"],
                                 "outcome": "following_retry", "awaited": kind,
                                 "was": awaited_id, "now": successor})
                    continue
                if status == "repairing":
                    continue  # the repair round is being started; the shepherd ends a dead one
                if _complete_advisory_failure(connection, project_id=project_id, run_id=run_id,
                                              step=step, output=output, status=status,
                                              why=str(not_passed), done=done):
                    continue
                recovered = _recover_build_step(
                    connection, ctx, project_id=project_id, run_id=run_id, step=step,
                    output=output, handle=handle, build_run_id=awaited_id, status=status,
                    why=str(not_passed))
                if recovered and recovered.get("done"):
                    done.append(recovered["done"])
                    continue
                said = _both_causes(output, handle, awaited_id, str(not_passed))
                if recovered:
                    said = f"{recovered['ask']} — {said}"
                workflows.force_fail_waiting_step(
                    connection, project_id=project_id, step_run_id=step["id"],
                    error=said[:500],
                    output={**output, "status": "failed", "awaited_status": status,
                            "reached_before_failing": _evidence_quietly(
                                connection, kind, awaited_id),
                            **((recovered or {}).get("output") or {})},
                )
                done.append({"run_id": run_id, "step_id": step["step_id"],
                             "outcome": "failed", "awaited": kind, "awaited_status": status,
                             "because": not_passed})
                continue
        if kind == "code_task" and status in ok_states \
                and handle.get("then") == deploy_repair.THEN_REDEPLOY:
            # THE APP WAS FIXED FOR ITS DEPLOY: the step deploys again, from the repair build.
            fixes = _deploy_fixes(output)
            note = (f"{output.get('note') or 'Deploy failed'} — fixed by build {awaited_id}; "
                    "deploying it again")
            workflows.requeue_waiting_step(
                connection, project_id=project_id, step_run_id=step["id"],
                output={deploy_repair.REDEPLOY_KEY: {
                            "build_run_id": awaited_id, "attempt": handle.get("attempt"),
                            "at": now_utc().isoformat()},
                        deploy_repair.FIXES_KEY: fixes, "note": note},
                event=deploy_repair.REDEPLOY_EVENT,
                data={"build_run_id": awaited_id, "attempt": handle.get("attempt")})
            done.append({"run_id": run_id, "step_id": step["step_id"],
                         "outcome": "redeploying", "awaited": kind, "build": awaited_id})
            continue
        if kind == "meeting" and status in ok_states:
            held = _hold_for_spec(connection, project_id=project_id, run_id=run_id,
                                  step=step, output=output, meeting_run_id=awaited_id)
            if held:
                done.append(held)
                continue
        if status in ok_states:
            # `completed` IS the inner pipeline's success. A missing pull request is not a
            # refusal — reviewer refusal lands as `blocked` / `failed`, already handled below.
            # Non-final decomposition slices and `open_pr=false` eval runs finish as
            # `completed_no_pr` on purpose. Treating a missing URL as "reviewers refused"
            # deadlettered the outer workflow while later slices were still building.
            evidence = _terminal_evidence(connection, kind, awaited_id)
            finished = {**output, "status": "succeeded", "awaited_status": status, **evidence}
            if kind == "agent_run":
                # AN AGENT THAT FINISHED AFTER AN APPROVAL IS JUDGED LIKE ONE THAT DID NOT STOP:
                # the run reaching `completed` is the loop ending, not the work being done.
                finished = _finished_agent_step(connection, project_id, run_id,
                                                str(step["step_id"]), awaited_id, finished)
            if str(finished.get("status") or "") in _DID_NOT_SHIP:
                workflows.force_fail_waiting_step(
                    connection, project_id=project_id, step_run_id=step["id"],
                    error=str(finished.get("error") or "")[:500], output=finished,
                )
                done.append({"run_id": run_id, "step_id": step["step_id"],
                             "outcome": "failed", "awaited": kind,
                             "because": finished.get("error")})
                continue
            workflows.force_complete_waiting_step(
                connection, project_id=project_id, step_run_id=step["id"],
                output=finished,
            )
            done.append({"run_id": run_id, "step_id": step["step_id"],
                         "outcome": "succeeded", "awaited": kind})
        elif status in bad_states:
            # A RETRY IS THE SAME STEP, NOT A NEW ONE.
            #
            # The step parks against ONE code-task id. Retrying a build starts a fresh attempt
            # with a new id, so the step kept waiting on the corpse: the operator watched a new
            # build run to completion while the workflow above it read
            # "Build, review and test — failed — the code_task it started finished as
            # 'cancelled'", stuck at 3 of 8 forever. Reported as "the build is running but the
            # workflow is not updated", which is exactly what it was.
            #
            # Following the successor is the whole fix. It is the same work by construction —
            # `retry_code_task` starts a new attempt in the same lineage — so the step re-parks
            # on the live attempt rather than failing on the dead one.
            # BOTH KINDS, NOT ONE. The gate here used to read `kind == "code_task"`, which
            # is why a retried panel was never followed — see `_meeting_successor`.
            successor = (
                _successor_attempt(connection, project_id, awaited_id, run_id)
                if kind == "code_task"
                else _meeting_successor(connection, project_id, awaited_id, run_id)
                if kind == "meeting" else None
            )
            if successor:
                noun = "build" if kind == "code_task" else "panel"
                workflows.repoint_waiting_step(
                    connection, project_id=project_id, step_run_id=step["id"],
                    handle={**handle, "id": successor},
                    note=(f"the {noun} was retried; this step now follows {successor} "
                          f"(the earlier attempt {awaited_id} ended as '{status}')"),
                )
                done.append({"run_id": run_id, "step_id": step["step_id"],
                             "outcome": "following_retry", "awaited": kind,
                             "was": awaited_id, "now": successor})
                continue
            # SAY WHY, NOT JUST WHAT. See `_why_it_ended`: the cause was already recorded
            # one or two tables away and this line used to discard it, leaving the operator
            # with a sentence that only restates the failure.
            why = _why_it_ended(connection, table, awaited_id)
            if _complete_advisory_failure(connection, project_id=project_id, run_id=run_id,
                                          step=step, output=output, status=status,
                                          why=str(why or ""), done=done):
                continue
            if kind == SPEC_DECISION_KIND:
                why = ("the spec was not approved — the panel's PRD decision was answered no, "
                       "so nothing was built" + (f": {why}" if why else ""))
            if kind == "stack_deployment" and status == "rejected":
                # The deployment's decision IS this step's gate (see `_gate_subject`), so a
                # deployment that was not approved is a release that was not approved.
                why = ("the release was not approved — the stack deployment's decision is this "
                       "step's approval, and it was answered no. Nothing was deployed.")
            refused_fix = ""
            recovered = None
            if kind == "stack_deployment" and status == "failed":
                # THE DEPLOY STEP'S AGENT DECIDES (step_recovery): retry, fix the app, clean this
                # app's earlier release, wait for a BOSS fix, or ask a person ONE question. A
                # person's "no" is `rejected`, never here.
                failure = deploy_repair.failure_of_deployment(connection, awaited_id)
                if failure:
                    settings_ = getattr(ctx, "settings", None) or get_settings()
                    recovered = _recover_awaited(
                        connection, ctx, project_id=project_id, run_id=run_id, step=step,
                        output=output, handle=handle, kind="deploy", failure=failure,
                        evidence=step_recovery.deploy_evidence(connection, settings_, failure)
                    ) if ctx is not None else None
                    if recovered and recovered.get("done"):
                        done.append(recovered["done"])
                        continue
            elif kind == "code_task":
                recovered = _recover_build_step(
                    connection, ctx, project_id=project_id, run_id=run_id, step=step,
                    output=output, handle=handle, build_run_id=awaited_id, status=status,
                    why=str(why or ""))
                if recovered and recovered.get("done"):
                    done.append(recovered["done"])
                    continue
            if kind == "stack_deployment" and status == "failed" and recovered is None:
                # FIX AND REDEPLOY with the recovery agent OFF: failed for the APP's reason → a
                # repair build of the app, and this step waits on it (deploy_repair).
                failure = deploy_repair.failure_of_deployment(connection, awaited_id)
                if deploy_repair.is_app_side(failure) and ctx is not None:
                    fixes = _deploy_fixes(output)
                    fix, refused_fix = _start_deploy_fix(
                        connection, ctx, project_id=project_id, run_id=run_id,
                        step_id=str(step["step_id"]), failure=failure, fixes=fixes)
                    if fix is not None:
                        waiting = _deploy_fix_awaiting(failure, fix, fixes, base=output)
                        workflows.repoint_waiting_step(
                            connection, project_id=project_id, step_run_id=step["id"],
                            handle=waiting["await"], note=waiting["note"])
                        connection.execute(
                            text("UPDATE workflow_step_runs SET output = output "
                                 "|| CAST(:o AS JSONB), updated_at = now() WHERE id = :i"),
                            {"i": step["id"], "o": json.dumps(
                                {k: waiting[k] for k in (deploy_repair.FIXES_KEY, "note",
                                                         "deploy_failure")})})
                        done.append({"run_id": run_id, "step_id": step["step_id"],
                                     "outcome": "fixing_the_app", "awaited": kind,
                                     "was": awaited_id, "now": fix["build_run_id"]})
                        continue
                fixes = _deploy_fixes(output)
                if fixes:
                    why = (f"{why or 'it failed'} (after {len(fixes)} automatic fix"
                           f"{'es' if len(fixes) != 1 else ''}: "
                           f"{deploy_repair.fixes_sentence(fixes)})")
                if refused_fix:
                    why = f"{why or 'it failed'}. BOSS did not fix it by itself: {refused_fix}"
            if kind == "stack_deployment" and status == "failed" and recovered:
                fixes = _deploy_fixes(output)
                if fixes:
                    why = (f"{why or 'it failed'} (after {len(fixes)} automatic fix"
                           f"{'es' if len(fixes) != 1 else ''}: "
                           f"{deploy_repair.fixes_sentence(fixes)})")
            if recovered and recovered.get("ask"):
                # THE QUESTION FIRST: it is what the person reading the failure must answer.
                why = f"{recovered['ask']} — {(why or 'it failed').rstrip('.')}"
            if kind == "code_task" and handle.get("then") == deploy_repair.THEN_REDEPLOY:
                why = _both_causes(output, handle, awaited_id,
                                   f"finished as '{status}'" + (f" — {why}" if why else ""))
            # FAILED IS TERMINAL FOR THE STEP, FOR EVERY KIND OF WORK — RESEARCH INCLUDED.
            #
            # Research used to be the exception: a discovery that died was force-COMPLETED with
            # status `failed_continued`, on the reasoning that research is only enrichment. On
            # boss-1 (2026-10-02) that is exactly what happened — research failed, the step went
            # green, and the workflow walked on to a panel, an auto-approved spec and a build,
            # none of which anybody had decided should go ahead without the research. Whether a
            # run carries on without a step is the OPERATOR'S decision ("Skip and proceed",
            # `workflows.skip_failed_step`), never the runner's.
            #
            # What the work reached before it died is kept on the failed step, where the
            # operator weighing retry against skip can read it. It does not travel downstream:
            # the run context reads succeeded steps only.
            workflows.force_fail_waiting_step(
                connection, project_id=project_id, step_run_id=step["id"],
                error=(why if (kind == SPEC_DECISION_KIND or (
                               kind == "code_task"
                               and handle.get("then") == deploy_repair.THEN_REDEPLOY)) else
                       f"the {kind} it started finished as '{status}' — {why}"
                       if why else f"the {kind} it started finished as '{status}'"),
                output={"status": "failed", "awaited_status": status,
                        "reached_before_failing": _evidence_quietly(
                            connection, kind, awaited_id),
                        # A person said no. `workflow_run_actions` reads this and never offers
                        # "Skip and proceed" on it: skipping would turn the refusal into a yes.
                        **({REJECTED_BY_APPROVER: True}
                           if kind in ("stack_deployment", "architecture", SPEC_DECISION_KIND)
                           and status == "rejected" else {}),
                        **((recovered or {}).get("output") or {})},
            )
            done.append({"run_id": run_id, "step_id": step["step_id"],
                         "outcome": "failed", "awaited": kind, "awaited_status": status,
                         "because": why})
    return done


#: Set on a build step's output once its run's open PRD decisions have been checked.
_SPEC_GATE_CHECKED = "spec_gate_checked"


def _mark_skipped_spec_gate(connection: Connection, *, run_id: str, step_run_id: str) -> None:
    """A BUILD RUNNING WHILE ITS PANEL'S PRD DECISION IS STILL OPEN went past the spec gate (a
    run started before `_hold_for_spec`). Its card is marked so it says so and offers only
    Acknowledge or Reject (spec_gate.mark_gate_skipped). Once per build step."""
    from . import spec_gate

    try:
        with connection.begin_nested():
            spec_gate.mark_gate_skipped(connection, run_id=run_id)
            connection.execute(
                text("UPDATE workflow_step_runs SET output = COALESCE(output, '{}'::jsonb) "
                     "|| CAST(:o AS JSONB) WHERE id = :i"),
                {"i": step_run_id, "o": json.dumps({_SPEC_GATE_CHECKED: True})})
    except Exception as error:  # noqa: BLE001 — a card's wording never stops the sweep
        log.warning("spec gate check for %s skipped: %s", run_id, error)


def _hold_for_spec(connection: Connection, *, project_id: str, run_id: str,
                   step: Any, output: dict[str, Any],
                   meeting_run_id: str) -> Optional[dict[str, Any]]:
    """Keep a finished panel's step WAITING on its PRD decision, when the step carries the
    workflow's spec ask and that decision is not approved yet. None lets it complete.

    THE PROMISE AND THE BEHAVIOUR FROM ONE SOURCE (workflow_asks.spec_ask_step_ids). A
    workflow with no `spec_review` step lists its meeting as "Approve the spec — you read the
    spec before any code", and the meeting step went green the moment the panel finished, with
    its PRD decision still pending: Minor Feature v6 on the Order API started build
    crun_e18a3a67 while dec_eb911c56 waited (wfr_723213d8, 2026-10-19). The step now waits on
    that decision as its work (`SPEC_DECISION_KIND`), so the build after it starts only once the
    spec is approved — by a person, or by BOSS where the PRD is auto-approved, which approves
    this same decision before the panel's run is even marked completed.

    A panel that raised no PRD decision (a security review's) has nothing to hold on.
    """
    from .workflow_asks import holds_for_spec

    try:
        steps = workflows._run_spec(connection, run_id).get("steps") or []
    except Exception:  # noqa: BLE001 — an unreadable spec keeps the old behaviour
        return None
    if not holds_for_spec(steps, str(step["step_id"])):
        return None
    row = connection.execute(
        text("SELECT d.id, d.status FROM meeting_runs m JOIN decisions d ON d.id = m.decision_id "
             "WHERE m.id = :m"), {"m": meeting_run_id}).mappings().first()
    if not row:
        return None
    ok_states = _AWAIT_TABLES[SPEC_DECISION_KIND][1]
    if str(row["status"] or "") in ok_states:
        return None
    decision_id = str(row["id"])
    handle = {"kind": SPEC_DECISION_KIND, "id": decision_id, "meeting_run_id": meeting_run_id}
    connection.execute(
        text("UPDATE workflow_step_runs SET output = COALESCE(output, '{}'::jsonb) "
             "|| CAST(:o AS JSONB), updated_at = now() WHERE id = :i AND status = 'waiting'"),
        {"i": step["id"], "o": json.dumps({
            "await": handle, "meeting_run_id": meeting_run_id, "spec_decision_id": decision_id,
            "awaited_note": ("The panel finished. Nothing is built until its spec (the PRD) "
                             f"is approved: decision {decision_id}.")})})
    return {"run_id": run_id, "step_id": step["step_id"], "outcome": "holding_for_spec",
            "awaited": SPEC_DECISION_KIND, "decision_id": decision_id,
            "meeting_run_id": meeting_run_id}


def _finished_agent_step(connection: Connection, project_id: str, run_id: str, step_id: str,
                         agent_run_id: str, finished: dict[str, Any]) -> dict[str, Any]:
    """The output of an agent step whose run finished after an approval, with what it decided
    (`produces`) read from its final reply and its verdict applied — the same two things a step
    that never stopped gets in `agent_steps.run` and `execute_step`."""
    from . import agent_steps

    spec_steps = {s["id"]: s for s in (workflows._run_spec(connection, run_id).get("steps")
                                        or []) if isinstance(s, dict) and s.get("id")}
    spec_step = spec_steps.get(step_id) or {"id": step_id}
    wanted = [str(n).strip() for n in ((spec_step.get("binding") or {}).get("produces") or [])
              if str(n).strip()]
    summary = str(finished.get("summary") or "")
    finished = {**finished,
                **agent_steps.products_of(connection, agent_run_id, summary, wanted),
                "agent_outcome": agent_steps.declared_outcome(summary)}
    return _judged_agent_step(connection, project_id, spec_step, spec_steps, finished)


def _evidence_quietly(connection: Connection, kind: str, awaited_id: str) -> dict[str, Any]:
    """`_terminal_evidence` inside a savepoint, for a step that is being FAILED.

    Reading what a dead run left behind is a courtesy to the operator; it must never be the
    thing that aborts the transaction recording the failure itself.
    """
    point = connection.begin_nested()
    try:
        evidence = _terminal_evidence(connection, kind, awaited_id)
    except Exception as error:  # noqa: BLE001
        point.rollback()
        log.warning("cannot read what %s %s reached: %s", kind, awaited_id, error)
        return {}
    point.commit()
    return evidence


def _why_it_ended(connection: Connection, table: str, awaited_id: str) -> str:
    """The REASON the awaited run ended, not merely the state it ended in.

    FOUR HOPS, AND EVERY ONE THREW THE REASON AWAY.
    -----------------------------------------------
    An operator's panel step read "the meeting it started finished as 'cancelled'" — a
    sentence that says nothing except that something went wrong somewhere. Tracing it by hand
    through the database took four queries:

        1. `POST /v1/meetings/step` raised
           `IntegrationConfigurationError: OPENAI_API_KEY is not configured` and returned 500.
        2. The `meeting.step` job retried, then DEADLETTERED, keeping that message in
           `jobs.last_error`.
        3. The shepherd saw a run with no job in ('queued','running','leased') and closed it:
           `cancelled — "abandoned: no job remains that could advance this run"`.
        4. This function's caller reported the STATUS of that run and nothing else.

    The true cause — a missing credential, which the operator can fix in a minute — survived
    at step 2 and was discarded by steps 3 and 4. So the console offered "Try Convene a panel
    again", a button guaranteed to fail identically forever, and BOSS itself could not say
    why, because the system had destroyed the evidence before the agent ever looked.

    This walks back down: the run's own error first, and — when that error is the shepherd's
    tautology about missing jobs — one further hop into the job that actually failed.
    """
    reason = ""
    # EACH READ IN ITS OWN SAVEPOINT. `agent_runs` has no `error` column, and on Postgres a
    # failed statement poisons the whole transaction: the `except` below swallowed the Python
    # error, the very next statement — the one failing the step — then died on
    # InFailedSqlTransaction, and the tick logged "skipped run (non-fatal)". So an agent step
    # whose approved run was denied stayed `waiting` for ever, never stopping its run. Found by
    # the every-await-kind test in test_a_failed_step_stops_the_run.py.
    point = connection.begin_nested()
    try:
        column = _REASON_COLUMN.get(table, "error")
        row = connection.execute(
            text(f"SELECT {column} AS error FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": awaited_id},
        ).mappings().first()
        reason = str((row or {}).get("error") or "").strip()
        point.commit()
    except Exception:  # noqa: BLE001 — a table without an `error` column is not fatal
        point.rollback()
        reason = ""

    # "No job remains" describes the SWEEP, not the failure. The job it is talking about
    # recorded why it died before it was swept.
    if not reason or "no job remains" in reason:
        point = connection.begin_nested()
        try:
            job = connection.execute(
                text("SELECT last_error FROM jobs "
                     " WHERE payload->>'run_id' = :r "
                     "   AND status IN ('deadlettered', 'failed') "
                     " ORDER BY updated_at DESC LIMIT 1"),
                {"r": awaited_id},
            ).mappings().first()
            raw = (job or {}).get("last_error")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = {"message": raw}
            if isinstance(raw, dict):
                detail = str(raw.get("message") or raw.get("reason") or "").strip()
                kind_of = str(raw.get("type") or "").strip()
                if detail:
                    reason = f"{kind_of}: {detail}" if kind_of else detail
            point.commit()
        except Exception:  # noqa: BLE001 — the status is still reportable without this
            point.rollback()
    return reason[:400]


def _meeting_successor(
    connection: Connection, project_id: str, awaited_id: str,
    workflow_run_id: Optional[str] = None,
) -> Optional[str]:
    """A later attempt at the same panel, if one is under way.

    THE HALF OF THE RETRY FIX THAT WAS NEVER WRITTEN.
    -------------------------------------------------
    `_successor_attempt` reads `code_task_runs` and its caller gated on
    `kind == "code_task"`, so a retried MEETING was never followed: the step stayed parked on
    the dead `mrun_`, and an operator who pressed "Try Convene a panel again" watched a new
    panel run to completion while the workflow above it still read "the meeting it started
    finished as 'cancelled'". Exactly the defect that was fixed for builds, left open for
    panels — and exactly what Kiro Crew's Stage 4 critique predicts of a pipeline that cannot
    re-plan when a result invalidates the plan.

    The lineage key is `finalize_ctx->>'meeting_fingerprint'`: two runs of the same panel over
    the same directive carry the same fingerprint, which is what makes "the same work, tried
    again" decidable rather than guessed.

    A successor that itself failed or was cancelled is not progress — following one would only
    move the stuck point — so only live or completed runs count.

    TWO GUARDS THAT THE FIRST VERSION LACKED, both found by an independent review:

    * **It must be LATER than the run that failed.** Without `created_at >`, ordering the
      fingerprint class by recency and taking the first live row adopts a run that PRE-DATES
      the failure whenever the real retry is itself dead. The step then force-completes on a
      stale panel's minutes — a silent wrong answer, which is worse than the stuck step it
      replaced.
    * **It must belong to THIS workflow run.** The fingerprint is (project, directive, url,
      type), so every run of the same panel across every workflow shares one. Four rows in a
      real database shared a fingerprint across four days and four different workflow runs,
      and this would have adopted a panel another run commissioned a day later. The code-task
      twin scopes by `workflow_run_id` for exactly this reason; this now does too, and falls
      back to lineage only when the awaited run records no workflow.
    """
    try:
        return connection.execute(
            text(
                """
                WITH target AS (
                    SELECT finalize_ctx->>'meeting_fingerprint' AS fp,
                           finalize_ctx->>'workflow_run_id'     AS wfr,
                           created_at                           AS born
                      FROM meeting_runs WHERE id = :awaited
                )
                SELECT r.id
                  FROM meeting_runs r, target t
                 WHERE r.project_id = :p
                   AND r.id <> :awaited
                   AND t.fp IS NOT NULL
                   AND r.finalize_ctx->>'meeting_fingerprint' = t.fp
                   AND r.status IN ('queued', 'running', 'completed')
                   -- A retry happens AFTER the thing it retries.
                   AND r.created_at > t.born
                   -- And belongs to the same run, when the awaited one says which run it is.
                   AND (
                        t.wfr IS NULL
                     OR r.finalize_ctx->>'workflow_run_id' = t.wfr
                     OR (:wfr <> '' AND r.finalize_ctx->>'workflow_run_id' = :wfr)
                   )
                 ORDER BY r.created_at ASC
                 LIMIT 1
                """
            ),
            {"awaited": awaited_id, "p": project_id, "wfr": workflow_run_id or ""},
        ).scalar()
    except Exception:  # noqa: BLE001 — a missing optional column must not stop the sweep
        return None


def _successor_attempt(
    connection: Connection, project_id: str, awaited_id: str,
    workflow_run_id: Optional[str] = None,
) -> Optional[str]:
    """A later attempt at the work this step was waiting on, if one is under way.

    TWO SIGNALS, AND THE SECOND ONE IS WHY THIS IS NOT JUST LINEAGE. The obvious rule — same
    `root_run_id`, so the same lineage — covers `retry_code_task`, which continues an attempt.
    It does NOT cover the retry an operator actually presses on a stuck workflow: "Try Build,
    review and test again" re-runs the STEP, which calls `create_code_task` and starts a fresh
    build with a root of its own.

    Checked against the live rows from the incident:

        crun_33a0…  cancelled  root=crun_2b7b…  workflow_run_id=wfr_666b…
        crun_7f38…  running    root=crun_ae46…  workflow_run_id=wfr_666b…

    Different roots, same workflow run. A lineage-only rule would have missed the exact case it
    was written for. What both runs agree on is which workflow asked for the work, and that is
    the stronger claim anyway: this run commissioned a build, and a build it commissioned is
    live.

    Never matched on title. Two builds named "Interview Scorecard App" can be different work,
    and a workflow following a stranger because the names matched is worse than one that stays
    stuck.

    A COMPLETED SUCCESSOR COUNTS TOO, and the first version of this got that wrong. It accepted
    only `running`/`queued`, on the reasoning that a finished attempt should reach its verdict
    the ordinary way — but a FAILED step has no ordinary way; it is terminal. So by the time the
    operator looked, the retry had completed, the step was still failed, and the recovery
    declined to act on the single most valuable case: the build worked, and the workflow should
    move on to preview and deploy.
    Following it re-parks the step, and the very next sweep sees a completed run and finishes
    the step properly — including the "did it open a PR" check, which is not skipped.

    Dead ends are still dead ends: a successor that was itself cancelled or failed is not
    progress, and chasing one would only move the stuck point.
    """
    try:
        row = connection.execute(
            text(
                """
                WITH target AS (
                    SELECT COALESCE(NULLIF(finalize_ctx->>'root_run_id', ''), id) AS root,
                           created_at AS born
                    FROM code_task_runs WHERE id = :awaited
                )
                SELECT r.id
                FROM code_task_runs r, target t
                WHERE r.project_id = :p
                  AND r.id <> :awaited
                  -- A later attempt, never an earlier one: a blocked repair must not send
                  -- the step back to the attempt it repaired.
                  AND r.created_at > t.born
                  AND r.status IN ('running', 'queued', 'completed')
                  AND (
                        COALESCE(NULLIF(r.finalize_ctx->>'root_run_id', ''), r.id) = t.root
                     OR (:wfr <> '' AND r.finalize_ctx->>'workflow_run_id' = :wfr)
                  )
                ORDER BY r.created_at DESC
                LIMIT 1
                """
            ),
            {"awaited": awaited_id, "p": project_id, "wfr": workflow_run_id or ""},
        ).mappings().first()
    except Exception as error:  # noqa: BLE001 — failing the step is still the safe fallback
        log.warning("cannot look for a successor to %s: %s", awaited_id, error)
        return None
    return str(row["id"]) if row else None

class StaleApprovalError(SettledCapabilityError):
    """The step's inputs no longer match the content a person approved.

    Settled: retrying the same approval would only refuse again. The tick clears the step's
    decision when it records this, so Retry opens a fresh gate showing the new content.
    """


def _gated_inputs(
    connection: Connection, project_id: str, spec_step: dict[str, Any],
    context: dict[str, Any],
) -> tuple[Optional[str], dict[str, Any]]:
    """The skill a gated step calls and EXACTLY the inputs it will be given.

    ONE resolution for the preview shown when the gate opens, the refresh while it waits, and
    the check when it finally runs — three copies would drift, and a hash computed two
    different ways refuses every approval.
    """
    binding = spec_step.get("binding") or {}
    context = dict(context)
    context.pop(SKIPPED_INPUTS, None)
    raw = binding.get("inputs") or {}
    if binding.get("agent_work"):
        return "agent_work", _resolve_inputs(
            raw, context, frozenset(k for k in raw if k != "instructions"))
    custom_key = str(binding.get("custom_step") or "")
    if custom_key:
        definition = custom_steps.get(connection, project_id, custom_key) or {}
        skill_id = definition.get("skill_id") or (
            f"mcp:{definition.get('mcp_tool_name')}" if definition.get("mcp_tool_name")
            else None)
        return skill_id, _resolve_inputs(
            raw, context, custom_steps.optional_inputs(connection, project_id, custom_key))
    key = str(binding.get("capability") or "")
    capability = get_capability(key)
    inputs = _resolve_inputs(raw, context,
                             optional=frozenset(capability.optional_inputs or ()))
    return f"capability:{key}", _fill_optional_from_run_input(capability, inputs, context)


def _action_preview_for(
    connection: Connection, project_id: str, spec_step: dict[str, Any],
    context: dict[str, Any], label: str,
) -> dict[str, Any]:
    """What approving this step will do, from the inputs it will really be given."""
    from . import action_preview

    try:
        skill_id, inputs = _gated_inputs(connection, project_id, spec_step, context)
    except Exception as error:  # noqa: BLE001 — a preview must never stop a gate opening
        binding = spec_step.get("binding") or {}
        return action_preview.unresolved(
            binding.get("custom_step") or binding.get("capability"), str(error),
            step_label=label)
    return action_preview.build(skill_id, inputs, step_label=label,
                                connection=connection, project_id=project_id)


def _approved_content_refusal(
    connection: Connection, project_id: str, step: dict[str, Any], context: dict[str, Any],
) -> Optional[str]:
    """Why this released step must not run: its content is not what was approved."""
    from . import action_preview

    decision_id = context.get("approval_decision_id")
    if not decision_id:
        return None
    row = connection.execute(
        text("SELECT recommendation FROM decisions WHERE id=:i"), {"i": decision_id},
    ).mappings().first()
    rec = (row or {}).get("recommendation") if row else None
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = None
    if not isinstance(rec, dict):
        return None
    preview = rec.get("action_preview") if isinstance(rec.get("action_preview"), dict) else {}
    expected = str(rec.get("approved_content_hash") or preview.get("content_hash") or "")
    if not expected:
        return None
    try:
        _skill, inputs = _gated_inputs(connection, project_id, step, context)
    except Exception:  # noqa: BLE001 — the step itself will fail saying what is missing
        return None
    current = action_preview.content_hash(inputs)
    if current == expected:
        return None
    return action_preview.stale_message(
        str(rec.get("step_label") or preview.get("step_label") or step.get("id") or ""),
        expected, current)


def _refresh_waiting_previews(
    connection: Connection, project_id: str, run_id: str, spec_steps: dict[str, Any],
) -> int:
    """Keep an open gate's preview equal to what the step would send NOW.

    If an upstream step is re-run while the gate waits, the approver must be looking at the new
    content — and the hash the approval binds to moves with it.
    """
    rows = connection.execute(
        text(
            """
            SELECT s.id, s.step_id, d.id AS decision_id, d.recommendation
            FROM workflow_step_runs s
            JOIN decisions d ON d.id = s.decision_id
            WHERE s.workflow_run_id = :r AND s.status = 'waiting'
              AND s.step_kind = 'capability'
              AND d.status IN ('pending', 'delegated')
              AND d.recommendation->'action_preview' IS NOT NULL
            """
        ),
        {"r": run_id},
    ).mappings().all()
    if not rows:
        return 0
    context = _run_context(connection, run_id)
    refreshed = 0
    for row in rows:
        rec = row["recommendation"] if isinstance(row["recommendation"], dict) else {}
        old = rec.get("action_preview") or {}
        spec_step = spec_steps.get(row["step_id"]) or {}
        if not spec_step:
            continue
        preview = _action_preview_for(connection, project_id, spec_step, context,
                                      str(rec.get("step_label") or old.get("step_label") or ""))
        if preview.get("content_hash") == old.get("content_hash"):
            continue
        patch = {"action_preview": preview, "preview_refreshed_at": now_utc().isoformat()}
        connection.execute(
            text("UPDATE decisions SET recommendation = COALESCE(recommendation,'{}'::jsonb) "
                 "|| CAST(:patch AS JSONB), updated_at = now() WHERE id = :i"),
            {"i": row["decision_id"], "patch": json.dumps(patch)},
        )
        connection.execute(
            text("UPDATE workflow_step_runs SET binding_snapshot = "
                 "COALESCE(binding_snapshot,'{}'::jsonb) || CAST(:patch AS JSONB), "
                 "updated_at = now() WHERE id = :i"),
            {"i": row["id"], "patch": json.dumps({"action_preview": preview})},
        )
        emit_event(connection, project_id, "workflow.gate_preview_refreshed", "workflow",
                   "workflow_step_run", row["id"],
                   {"step_id": row["step_id"], "decision_id": row["decision_id"],
                    "was": old.get("content_hash"), "now": preview.get("content_hash")})
        refreshed += 1
    return refreshed


def _step_word(spec_step: dict[str, Any], fallback: str) -> str:
    """The STEP's own name for a gate's sentence — "Approve before 'Go live' runs" — not the
    capability's: every Notify step in a workflow said "Approve before 'Notify' runs", so an
    operator with three gates open could not tell them apart (Command Center re-test, 3)."""
    for key in ("label", "name", "title"):
        value = spec_step.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _gate_subject(
    connection: Connection,
    project_id: str,
    spec_step: dict[str, Any],
    context: Optional[dict[str, Any]] = None,
    *,
    default_owner: str = "",
    waived: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """What a person has to approve before this step, or None if it just runs.

    `waived`, when given, receives `{STACK_GATE_TARGET: id}` when the gate is waived because
    the step ships to a stack target whose deployment decision is the approval.

    TWO DIFFERENT THINGS END UP WAITING HERE, and the difference is `completes_step`:

      * A gate capability — `approve_release` and its siblings. The approval IS the work, so
        answering it finishes the step.
      * An ordinary step whose approval mode is manual, including every custom step above the
        risk floor. The approval only unblocks it; the step still has to run afterwards.

    Collapsing the two would either finish real work nobody did, or leave a gate waiting for a
    tool call that does not exist.
    """
    binding = spec_step.get("binding") or {}
    approval = spec_step.get("approval") or {}
    requested = str(approval.get("mode") or "").lower() or None

    if _owned_by_change_set(str(binding.get("capability") or ""), context):
        # The change set's ONE approval covers it (approve and ship all); no second gate.
        return None

    if binding.get("agent_work"):
        # MANUAL MEANS A PERSON SAYS GO FIRST, on an agent step as on any other. The builder
        # offered "manual" here and the card said "needs you", and this returned None because
        # the step has no capability key — so the agent ran without anybody being asked.
        if requested != "manual":
            return None
        named = str(spec_step.get("captain") or binding.get("agent") or "").strip()
        return {
            "key": "agent_work",
            "label": "Let an agent work it out",
            "summary": (f"Approve before {named or 'the agent BOSS picks'} starts on this "
                        "step's instructions."),
            # An agent's own risky skill calls still stop for a person on their own; this is
            # the step-level "ask me first" the workflow's author chose.
            "risk_tier": 1,
            "completes_step": False,
        }

    custom_key = str(binding.get("custom_step") or "")
    if custom_key:
        definition = custom_steps.get(connection, project_id, custom_key)
        if definition is None or not definition["usable"] or not definition["enabled"]:
            # Not gated, so it reaches execute_step and fails there with the sentence naming
            # what is actually wrong. Gating it instead would ask a person to approve a step
            # that cannot run, and an unusable step defaults to the risk floor — so a deleted
            # skill would have produced an approval request rather than an error.
            return None
        # The whole resolved definition, not just its tier: a step on an unreviewed MCP server
        # needs a person at ANY tier (requires_person), and passing the tier alone is how
        # round 2 found a tier-0 step on such a server running with nobody asked.
        if custom_steps.approval_for(definition, requested) != "manual":
            return None
        return {
            "key": custom_key,
            "label": definition["label"],
            "summary": (
                f"Approve before '{_step_word(spec_step, definition['label'])}' runs. It calls "
                f"{definition['risk_source']} at risk tier {definition['risk_tier']}."
                + (f" {definition['requires_person']}"
                   if definition.get("requires_person") else "")
            ),
            "risk_tier": definition["risk_tier"],
            "completes_step": False,
            # Why only a PERSON may answer this gate — carried to _open_gate, which writes
            # on_timeout=block into the snapshot so the timeout sweep cannot answer it.
            "requires_person": definition.get("requires_person"),
        }

    key = str(binding.get("capability") or "")
    if key not in CAPABILITY_KEYS:
        return None
    stack_target = (stack_target_for_step(connection, project_id, spec_step, context,
                                          default_owner=default_owner)
                     if key == "deploy" else None)
    if stack_target:
        if waived is not None:
            waived[STACK_GATE_TARGET] = stack_target.get("id") or FIRST_STACK_TARGET
        # ONE APPROVAL FOR A STACK DEPLOY: THE DEPLOYMENT'S OWN.
        #
        # Observed on boss-1 (wfr_79c63a78b34546a3b927): the deploy step of a repository with
        # an active stack target was held at its release_manager gate BEFORE it ran, and the
        # console rendered that gate as the per-app container flow — "Where it ships: needs
        # you first / Set target", then "Approve the release" — a path Community has paused.
        # Only after that approval did the step plan the stack deployment, which raised its
        # own decision: two approvals for one release, the first through the wrong screen.
        #
        # So the step runs at once and plans the deployment from the build's pull-request
        # head; the deployment's decision — raised for this step's approver_role, showing the
        # plan, the host and the manifest — is the gate. Approving it releases the apply;
        # rejecting it rejects the step. Every other deploy keeps its gate exactly as before.
        return None
    if key == "deploy" and context is not None:
        # A DEPLOY THAT WILL BE REFUSED ASKS NOBODY: an app that was told to declare how it
        # runs and did not, or a Postgres app with no manifest to bring its database, is refused
        # by the step in words (stack_deploy.undeclared_deploy_refusal) — never after a person
        # approved a deploy that could not work.
        from .stack_deploy import undeclared_deploy_refusal

        ready = deploy_step_inputs(connection, spec_step, context) or {}
        if not ((binding.get("inputs") or {}).get("pass_2_of")) and ready.get("run_id"):
            point = connection.begin_nested()
            try:
                undeclared = undeclared_deploy_refusal(connection, str(ready["run_id"]))
            except Exception:  # noqa: BLE001 — unknown: the gate opens as it always did
                point.rollback()
                undeclared = None
            else:
                point.commit()
            if undeclared:
                return None
        # "DEPLOY AGAIN" THAT WILL SHIP NOTHING ASKS NOBODY: it runs, and says why (two_pass).
        pass_2_of = str(((binding.get("inputs") or {}).get("pass_2_of")) or "")
        if pass_2_of and pass_two_redeploy_refusal(
                connection, str(context.get("run_id") or ""), spec_step, pass_2_of):
            return None
        # A RELEASE WITHOUT ITS REQUIRED MONITORING PACK ASKS NOBODY: it is refused, in words.
        from . import monitoring_pack

        if monitoring_pack.release_refusal(
                connection, str(context.get("run_id") or ""), spec_step,
                monitoring_pack.run_gate(connection, str(context.get("run_id") or ""))):
            return None
        # THE ENVIRONMENT'S POLICY, ON THE STEP AS ON THE DEPLOYMENT — FOR A STEP THAT LET IT.
        # A step with `approval.follows_policy` is not asked where a person set the environment
        # to deploy without asking; the deployment then runs on that policy and records whose
        # it was. A step that PROMISED a person (every other Deploy step) is asked, whatever
        # the policy says (D2, 2026-10-18: "Ships only when you say yes", shipped unasked).
        if policy_answers_for_step(connection, project_id, spec_step, context.get("input")):
            return None
    capability = get_capability(key)
    if capability.is_gate:
        if _no_prd_to_approve(key, context):
            return None  # runs as ordinary work below and records "no PRD"
        if key == "security_decide" and context is not None and not \
                security_review_steps.has_choices(connection, project_id, context):
            return None  # nothing to choose from: runs as ordinary work and records why
        if context is not None and _adopted_gate(connection, key, context):
            # Already answered. Returning None sends it down the ordinary path, where
            # execute_step records the approval it was started with.
            return None
        if key == "spec_review" and context is not None:
            existing = _decision_from_the_panel(connection, project_id, context)
            if existing:
                # WAIT ON THE PANEL'S OWN DECISION. Raising a second "approve the spec" would
                # ask the same person about the same document twice, one dialog after the other.
                return {
                    "key": key, "label": capability.label,
                    "summary": capability.summary,
                    "risk_tier": max(2, capability.risk_tier), "completes_step": True,
                    "decision_id": existing,
                }
        return {
            "key": key, "label": capability.label, "summary": capability.summary,
            "risk_tier": max(2, capability.risk_tier), "completes_step": True,
        }
    if str(approval.get("mode") or "").lower() == "manual":
        # The floor said manual and the runner has to honour it. Before this, `forced_manual`
        # was stored on the step and then ignored at run time, which made the whole risk floor
        # advisory — a tier-3 capability marked "requires a person" ran unattended.
        return {
            "key": key, "label": capability.label,
            "summary": f"Approve before '{_step_word(spec_step, capability.label)}' runs.",
            "risk_tier": capability.risk_tier, "completes_step": False,
        }
    return None


def _assignment_refusal(
    connection: Connection, project_id: str, run_input: Any, approver_role: str,
    run_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Why this approver may not answer for this run's app, or None when they may.

    ONE CHECK FOR EVERY KIND OF GATE: `_open_gate` asks it before raising a gate, and a stack
    deploy — whose gate is its deployment's decision — asks it before planning, so waiving the
    workflow gate never waives the assignment rule with it.
    """
    run_input = run_input if isinstance(run_input, dict) else {}
    approver = str(approver_role or "").strip()
    app_name = str(run_input.get("title") or run_input.get("directive") or "").strip()
    if not approver or not app_name:
        return None
    try:
        from . import agent_assignment
        verdict = agent_assignment.may_act(
            connection, project_id=project_id, app_name=app_name, role_key=approver)
    except Exception as error:  # noqa: BLE001 — an unreadable table must not stop a gate
        log.warning("assignment check failed for %s: %s", approver, error)
        return None
    if not verdict.get("allowed") and run_id:
        # A TEAM NOBODY CHOSE GAINS THE AGENTS THIS RUN'S WORKFLOW NAMES (agent_assignment.
        # ensure_defaults), here as well as at the run's start: a run started before that fix,
        # or on an app an older run seeded, would otherwise stay refused on every retry.
        try:
            row = connection.execute(
                text("SELECT v.spec FROM workflow_runs r JOIN workflow_versions v "
                     "ON v.id = r.workflow_version_id WHERE r.id = :r"), {"r": run_id},
            ).mappings().first()
            spec = (row or {}).get("spec")
            if isinstance(spec, dict) and approver in agent_assignment.workflow_named(spec):
                agent_assignment.ensure_defaults(connection, project_id=project_id,
                                                 app_name=app_name, actor="boss", spec=spec)
                verdict = agent_assignment.may_act(
                    connection, project_id=project_id, app_name=app_name, role_key=approver)
        except Exception as error:  # noqa: BLE001 — the first verdict stands
            log.warning("could not top up the team for %s: %s", approver, error)
    if verdict.get("allowed"):
        return None
    return {"reason": verdict.get("reason") or f"{approver} is not assigned to this app.",
            "approver_role": approver, "app": app_name,
            "assigned": verdict.get("assigned") or []}


def _open_gate(
    connection: Connection, project_id: str, run: dict[str, Any], step_row: dict[str, Any]
) -> None:
    """Turn a step that needs approval into a decision a person can answer, and leave it
    waiting."""
    spec_step = step_row.get("_spec") or {}
    subject = step_row["_subject"]
    approval = spec_step.get("approval") or {}
    # A GATE ONLY A PERSON MAY ANSWER NEVER TIMES OUT INTO AN APPROVAL.
    #
    # Round-3 review: a custom step published before the review rule existed could carry
    # on_timeout=approve with an sla. Its gate is forced manual at run time (requires_person),
    # but the snapshot kept on_timeout=approve, so the timeout sweep approved the gate BY
    # POLICY and an unreviewed server's tool ran with nobody asked. The snapshot is what the
    # sweep reads, so it is corrected here, where the gate opens, whatever the version says.
    requires_person = str(subject.get("requires_person") or "").strip() or None
    if requires_person and str(approval.get("on_timeout") or "block").lower() == "approve":
        approval = {**approval, "on_timeout": "block"}

    # ASSIGNMENT, ENFORCED WHERE IT MATTERS: at the gate.
    #
    # A gate names an `approver_role` and BOSS has never checked whether that agent is on this
    # piece of work. Assignment that changes nothing is decoration, and this codebase has
    # shipped decoration before — the template tags that rendered as labels and never filtered.
    #
    # It refuses only when the project has MADE a decision that this contradicts. An app with no
    # assignment at all passes: enforcement should stop work that goes against somebody's
    # choice, not work on something nobody has chosen about yet, or every app that predates the
    # table becomes unrunnable.
    refusal = _assignment_refusal(connection, project_id, run.get("input") or {},
                                  str(approval.get("approver_role") or ""),
                                  run_id=str(run.get("id") or "") or None)
    if refusal:
        connection.execute(
            text("UPDATE workflow_step_runs SET status='failed', "
                 "last_error=:e, updated_at=now() WHERE id=:i"),
            {"e": refusal["reason"], "i": step_row["id"]},
        )
        emit_event(
            connection, project_id, "workflow.assignment_refused",
            run.get("actor") or "workflow", "workflow_step_run", step_row["id"],
            {"approver_role": refusal["approver_role"], "app": refusal["app"],
             "assigned": refusal["assigned"]},
        )
        return
    # Matches core.create_blocking_decision exactly: it takes a question and a recommendation,
    # returns the decision id, and defaults to HOLD — which is the behaviour a gate wants. A
    # gate that defaulted to "proceed" would be a gate in name only.
    # An existing decision the subject named — the panel's own PRD approval — is used as this
    # gate rather than a new one raised beside it.
    # ONE RUN, ONE DECISION. THIS QUESTION MUST NOT COLLIDE.
    #
    # create_blocking_decision deduplicates on (project_id, question, task_id) while pending —
    # right for a repeated alert, because a repeat is evidence the same cause is ongoing.
    # Catastrophic for a release gate: the question was "Ship it: standard_product_delivery",
    # identical for every run of that workflow, so five runs across five different apps
    # attached to ONE decision row. Approving one of them released all five deploys at the
    # same second, which the operator caught — they approved a single release and watched five
    # start.
    #
    # The run id makes it unique, and the title makes the Approvals page readable: an approver
    # is being asked about a specific piece of work, not about a workflow in the abstract.
    #
    # IN WORDS, NOT KEYS. "Email the newsletter: weekly_newsletter · run 4715a965" named a
    # workflow key and a run id; the question now reads "<step label> — <run title>", and a
    # second open gate with the same words is told apart by run_decisions.step_gate_question.
    # The ids stay in the recommendation, which is the evidence.
    #
    # ONE GATE, ONE RUN: a gate this run raises goes through run_decisions.open_step_decision,
    # the one way a step's decision is opened, so it belongs to exactly this run and step.
    from . import action_preview

    run_context = _run_context(connection, run["id"])
    # The tick's run row carries no input; the run's context does.
    run_input = run.get("input") or run_context.get("input") or {}
    subject_name = action_preview.run_title(run_input, run_context.get("workflow_name") or "")
    capability_key = str(subject["key"])
    # WHAT APPROVING WILL DO, from the inputs the step will really be given. Only for a step
    # the approval RELEASES — on a gate capability the approval is the work itself.
    preview = (None if subject["completes_step"] or subject.get("decision_id")
               else _action_preview_for(connection, project_id, spec_step, run_context,
                                        str(subject["label"])))
    # THE STEP AS THE WORKFLOW NAMES IT (agents-home D9): "Publish — Release notes 2.9", the
    # words the Command Center and Agents › Live use for the same gate, not the capability's.
    from .command_center import step_label as _named_step_label

    question = action_preview.gate_question(
        _named_step_label(spec_step, str(step_row.get("step_id") or "")) if spec_step.get(
            "label") or spec_step.get("name") or spec_step.get("title") else str(subject["label"]),
        subject_name)
    recommendation_extra: dict[str, Any] = {}
    if preview is not None:
        recommendation_extra["action_preview"] = preview
    # AN ENVIRONMENT ONLY WHERE THERE IS ONE. "staging" on an email step was noise that read as
    # a fact about where the email went.
    if capability_key in {"deploy", "redeploy", "monitor"}:
        recommendation_extra["environment"] = str(run_input.get("environment") or "staging")
    pass_2_of = str((((spec_step.get("binding") or {}).get("inputs") or {}).get("pass_2_of"))
                    or "")
    if capability_key == "deploy" and pass_2_of:
        # WHAT THIS SECOND DEPLOY SHIPS: what the More tests pass added or fixed (two_pass).
        wired = ((spec_step.get("binding") or {}).get("inputs") or {}).get("run_id")
        found = pass_two_summary(connection, run["id"], str(
            wired.get("from_step") if isinstance(wired, dict) else ""))
        recommendation_extra["more_tests"] = {**found, "sentence": pass_two_sentence(found)}
        subject = {**subject, "summary": (f"{subject['summary']} Ships the More tests pass on "
                                          f"top of what is deployed. "
                                          f"{pass_two_sentence(found)}")}
    if capability_key == "deploy":
        # THE PACK IS APPROVED WITH THE RELEASE (monitoring_pack): rules, PromQL, SEV,
        # thresholds, the last hour's preview and the runbooks, on this card.
        from .monitoring_pack import card_for

        pack = card_for(connection, run["id"], spec_step)
        if pack:
            recommendation_extra["monitoring_pack"] = pack
    if capability_key in {"release_approval", "deploy"}:
        # WHAT THE RELEASE WOULD SHIP beyond the serving commit (release_scope): every commit,
        # its run, that run's approval and whether it was reviewed, bound by its hash. The D2
        # card showed one line of a fifteen-commit release.
        card = _release_card(connection, project_id, run["id"], run_context)
        if card:
            from . import release_scope

            recommendation_extra = release_scope.attach(
                recommendation_extra, card, step_label=str(subject["label"]))
            subject = {**subject, "summary": f"{subject['summary']} {card['sentence']}"}
    if capability_key == "security_decide":
        # Where the checklist is: App › Security draws the recommendations as the choice this
        # approval records (approving without opening it takes the P0/P1 pre-ticks).
        recommendation_extra["security_review"] = {
            "run_id": run["id"], "app_key": str(run_input.get("app_key") or ""),
            "recommendations": len(security_review_steps.review.list_recommendations(
                connection, run["id"]))}
    decision_id = subject.get("decision_id") or open_step_decision(
        connection,
        project_id,
        run.get("task_id"),
        question,
        {
            "capability": subject["key"],
            "approver_role": approval.get("approver_role"),
            # WHAT THE APPROVER IS ACTUALLY DECIDING ABOUT.
            #
            # The recommendation carried a capability key, a step id and a run id — three
            # identifiers and not one fact. So the Approvals page, having no `action` it
            # recognised, fell through to its default and told the approver "this type has no
            # automated follow-on (no project is started)" above a Deploy gate whose approval
            # ships a container onto a host. That sentence is the opposite of true, and it is
            # the one an approver reads before pressing the green button.
            #
            # The label and the app name are what make the sentence sayable: "approving builds
            # a container from Onboarding Path Builder and runs it on the bound host" rather
            # than "approving records your decision".
            "step_label": subject["label"],
            "workflow_key": run.get("workflow_key"),
            "app_name": str(run_input.get("app_name")
                            or run_input.get("title") or "").strip()[:120],
            "run_title": subject_name,
            # Whether answering this finishes the step or merely lets it run. The two have
            # different consequences and the page has never been able to tell them apart.
            "gate_completes_step": bool(subject["completes_step"]),
            **recommendation_extra,
        },
        {"estimated_usd": 0.0},
        subject["summary"],
        run_id=run["id"],
        step_id=step_row["step_id"],
        risk_tier=max(2, int(subject["risk_tier"])),
    )
    if not subject.get("decision_id"):
        # A FIRST APPROVAL IS "Approve" / "Reject". The shared default, "Approve and retry" /
        # "Keep blocked", is right for an alert about something that failed; nothing has
        # failed here and approving retries nothing.
        connection.execute(
            text("UPDATE decisions SET options = CAST(:o AS JSONB) WHERE id = :i"),
            {"i": decision_id, "o": json.dumps([
                {"action": "approve", "label": "Approve"},
                {"action": "reject", "label": "Reject"},
            ])},
        )
    # The deadline goes on the step, not in a scheduler somewhere else. A timeout you cannot
    # see from the row that is waiting is a timeout nobody can explain when it fires.
    sla_hours = approval.get("sla_hours")
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='waiting',
                decision_id=:decision_id,
                binding_snapshot = COALESCE(binding_snapshot, '{}'::jsonb)
                                   || CAST(:snapshot AS JSONB),
                updated_at=now()
            WHERE id=:id
            """
        ),
        {
            "id": step_row["id"],
            "decision_id": decision_id,
            "snapshot": json.dumps({
                "approval": approval,
                # Recorded, not recomputed on resume: the answer to "did approving this finish
                # it?" must not change if the workflow is edited while somebody is deciding.
                "gate_completes_step": bool(subject["completes_step"]),
                "requires_person": requires_person,
                # The same preview the decision carries, so the run's step can show it.
                **({"action_preview": preview} if preview is not None else {}),
                "opened_at": now_utc().isoformat(),
                "due_at": (
                    (now_utc() + timedelta(hours=float(sla_hours))).isoformat()
                    if sla_hours else None
                ),
            }),
        },
    )
    emit_event(
        connection, project_id, "workflow.gate_opened", run.get("actor") or "workflow",
        "workflow_step_run", step_row["id"],
        {"capability": subject["key"], "step_id": step_row["step_id"]},
    )

def _release_card(connection: Connection, project_id: str, run_id: str,
                  context: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The release card for this run's build, or None (no build, nothing serving yet)."""
    from . import release_scope

    build = _build_run_id(context)
    if not build:
        return None
    point = connection.begin_nested()
    try:
        serving = release_scope.serving_for_run(connection, context.get("input"), build)
        card = release_scope.card_for_build(
            connection, _settings(), project_id, build_run_id=build, serving_sha=serving,
            workflow_run_id=run_id)
    except Exception as error:  # noqa: BLE001 — logged; the card then carries no list
        point.rollback()
        log.warning("release card for %s: %s", run_id, error)
        return None
    point.commit()
    return card


def _park_for_decision(
    connection: Connection, project_id: str, run: dict[str, Any], step_row: dict[str, Any],
    output: dict[str, Any],
) -> str:
    """Turn a step that hit a blocking finding into a question a person can answer.

    A finding that is meant to stop the line is a decision, not a dead end. Deadlettering it
    answered the question for the operator — "no, do not continue" — with no way to say "ship it
    anyway", and left preview, deploy and monitor pending forever on work that had already been
    built.

    The lease is released along with it: a parked step holds no lease, or the reclaimer treats
    the worker as crashed and re-runs a step that is waiting on a person.
    """
    # The question is the finding's ("Continue this workflow?" for every run), so the decision
    # is opened as this run's step's own — another run parked on the same finding gets its own.
    decision_id = open_step_decision(
        connection,
        project_id,
        run.get("task_id"),
        str(output.get("question") or "Continue this workflow?"),
        {
            "capability": output.get("capability"),
            "findings": output.get("findings") or [],
            "code_task_run_id": output.get("code_task_run_id"),
        },
        {"estimated_usd": 0.0},
        str(output.get("note") or ""),
        run_id=run["id"],
        step_id=step_row["step_id"],
        risk_tier=3,
    )
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='waiting',
                decision_id=:decision_id,
                output = CAST(:output AS JSONB),
                lease_owner=NULL, lease_expires_at=NULL,
                binding_snapshot = COALESCE(binding_snapshot, '{}'::jsonb)
                                   || CAST(:snapshot AS JSONB),
                updated_at=now()
            WHERE id=:id
            """
        ),
        {
            "id": step_row["id"], "decision_id": decision_id,
            "output": json.dumps(output),
            # Approving means "carry on past this review", so the approval completes the step.
            "snapshot": json.dumps({"gate_completes_step": True,
                                    "approval": {"mode": "manual"}}),
        },
    )
    return decision_id


def _resume_answered_gates(
    connection: Connection, project_id: str, run: dict[str, Any]
) -> list[dict[str, Any]]:
    """Act on gates a person has now answered.

    Without this the gate was a dead end: it opened, somebody approved it, and the step stayed
    `waiting` forever because the runtime's own resume logic only knows about `decision`-kind
    steps. A gate that cannot be passed is worse than no gate — the work never happens and
    nothing says why.
    """
    rows = connection.execute(
        text(
            """
            SELECT s.id, s.step_id, s.binding_snapshot, s.iteration, s.decision_id,
                   d.status AS decision_status, d.approved_by AS decision_approved_by
            FROM workflow_step_runs s
            JOIN decisions d ON d.id = s.decision_id
            WHERE s.workflow_run_id = :run_id
              AND s.status = 'waiting'
              AND s.step_kind = 'capability'
              AND d.status IN ('approved', 'rejected', 'expired')
              -- ONCE PER ANSWER. A released step goes on to wait for its WORK (a deploy
              -- awaiting its own approval), still `waiting` with the same approved decision.
              -- Read as a fresh answer, it was released again every tick: HelpDesk's deploy
              -- step re-sent its deploy 1,039 times in six hours.
              AND COALESCE(s.binding_snapshot->>'released_for', '') <> s.decision_id
            """
        ),
        {"run_id": run["id"]},
    ).mappings().all()

    acted: list[dict[str, Any]] = []
    for row in rows:
        snapshot = row["binding_snapshot"] or {}
        if row["decision_status"] != "approved":
            # A NO FAILS THE STEP and stops the run there, offering Retry and Skip — the one
            # path every refused gate takes (workflows.reject_gate).
            workflows.reject_gate(
                connection, run["id"], str(row["step_id"]), int(row["iteration"]),
                decision_id=str(row.get("decision_id") or ""),
                outcome=str(row["decision_status"]),
            )
            acted.append({"run_id": run["id"], "step_id": row["step_id"],
                          "outcome": f"gate_{row['decision_status']}"})
            continue

        # WHO ACTUALLY APPROVED IT — read, never assumed.
        #
        # This wrote `"person"` unconditionally, for every approved decision, including the
        # ones no person touched. BOSS auto-approves the PRD gate when
        # BOSS_MEETING_PRD_AUTO_APPROVE is on, and the code that does it promises:
        #
        #     "the audit trail records `auto_approval` as the actor, so a reader can always
        #      tell which approvals a human made."
        #
        # The decision row kept that promise. This function then broke it, by stamping
        # `decided_by: "person"` onto the step evidence -- which is the record the run card
        # shows and the one an auditor reads. Observed on wfr_a1ac0748e42742d38256, where
        # dec_1b70bd99cca141488f8b was approved by `auto_approval` and the step said a person
        # decided it.
        #
        # For a product whose claim is that every change can be traced to whoever approved it,
        # an approval record naming the wrong kind of approver is the worst defect available.
        approved_by = str(row["decision_approved_by"] or "").strip() or "unknown"
        decided_automatically = approved_by == DECISION_SYSTEM_ACTOR

        if snapshot.get("gate_completes_step", True):
            # The approval was the work. Same direct UPDATE the timeout sweep uses, and for the
            # same reason: this step is leased by nobody, so `complete_step` would refuse it.
            done = connection.execute(
                text(
                    """
                    UPDATE workflow_step_runs
                    SET status='succeeded',
                        output = CAST(:output AS JSONB),
                        evidence = COALESCE(evidence,'[]'::jsonb) || CAST(:evidence AS JSONB),
                        completed_at = now(), updated_at = now()
                    WHERE id = :id AND status = 'waiting'
                    """
                ),
                {
                    "id": row["id"],
                    "output": json.dumps({
                        "approved_by": approved_by,
                        "outcome": "approved",
                    }),
                    "evidence": json.dumps([{
                        "kind": "auto_approval" if decided_automatically else "approval",
                        "decided_by": approved_by,
                        "automatic": decided_automatically,
                    }]),
                },
            ).rowcount

            # FOLLOW THE EDGES OUT OF THE STEP WE JUST PASSED.
            #
            # `advance_run` alone was not enough, and the way it failed was silent. It looks
            # only at steps that are still `waiting`; the UPDATE above had just moved this one
            # to `succeeded`, so it matched nothing, reported no progress, and fell through to
            # the finish check — which found no active step, marked `build`, `preview` and
            # `deploy` as `path_not_reached`, and called the run COMPLETED.
            #
            # So a person read the PRD, pressed Approve, and BOSS recorded a finished delivery
            # that had built nothing. `_after_success` is what unlocks successors; every other
            # path pairs it with the write that succeeds a step, and this one did not.
            #
            # Guarded on rowcount so two ticks racing on the same gate cannot advance twice.
            if done:
                workflows._after_success(
                    connection, run["id"], row["step_id"], int(row["iteration"]),
                )
            workflows.advance_run(connection, project_id, run["id"])
            outcome = "gate_passed"
        else:
            # The approval only unblocked it. Back to `ready` so the ordinary claim-and-execute
            # path runs it — with decision_id still set, which is what stops it being gated a
            # second time on the next tick.
            connection.execute(
                text(
                    """
                    UPDATE workflow_step_runs
                    SET status='ready', updated_at=now(),
                        binding_snapshot = COALESCE(binding_snapshot,'{}'::jsonb)
                                           || CAST(:patch AS JSONB)
                    WHERE id=:id AND status='waiting'
                    """
                ),
                {"id": row["id"],
                 "patch": json.dumps({"approved_at": now_utc().isoformat(),
                                      "released_for": str(row["decision_id"] or "")})},
            )
            connection.execute(
                text("UPDATE workflow_runs SET status='running', updated_at=now() "
                     "WHERE id=:id AND status='waiting'"),
                {"id": run["id"]},
            )
            outcome = "approved_released"

        emit_event(
            connection, project_id, "workflow.gate_answered", run.get("actor") or "workflow",
            "workflow_step_run", row["id"],
            {"step_id": row["step_id"], "outcome": outcome},
        )
        acted.append({"run_id": run["id"], "step_id": row["step_id"], "outcome": outcome})
    return acted


def projects_with_work(connection: Connection, asked: str) -> list[str]:
    """`asked` first, then every other project with a product workflow run to advance.

    The worker ticks the project it is configured for. A template LAB is a project of its own
    (templates.lab), so its workflow sat at `build: ready` for ever: nothing ever ticked that
    project. Every project with live work is advanced, whichever one the worker names. Not an
    archived one (projects_archive): its runs wait, as they are, until it is restored.
    """
    from . import projects_archive

    others = [str(r[0]) for r in connection.execute(
        text(
            """
            SELECT DISTINCT r.project_id
            FROM workflow_runs r
            JOIN workflow_definitions d ON d.id = r.workflow_definition_id
            WHERE r.project_id <> :asked
              AND COALESCE(d.namespace, 'system') = 'product'
              AND (r.status IN ('running', 'waiting')
                   OR (r.status = 'failed' AND EXISTS (
                         SELECT 1 FROM workflow_step_runs s
                         WHERE s.workflow_run_id = r.id AND s.status = 'failed'
                           AND (s.output->'await'->>'kind' = 'code_task'
                                OR s.output ? 'recovery_resume_on_update'))))
            ORDER BY r.project_id
            LIMIT 50
            """
        ),
        {"asked": asked},
    ).all()]
    return projects_archive.live_ids(connection, [asked, *others])


def tick(
    connection: Connection,
    ctx: Any,
    *,
    project_id: str,
    worker_id: str = "capability-runner",
    limit: int = MAX_STEPS_PER_TICK,
) -> dict[str, Any]:
    """Advance every product workflow run that has work ready.

    Returns what it did, per step, so a caller can log something more useful than a count.
    Never raises for a step-level failure: one broken workflow must not stop the others.
    """
    from . import projects_archive

    if projects_archive.is_archived(connection, project_id):
        return {"performed": [], "count": 0, **projects_archive.skipped(project_id)}
    runs = connection.execute(
        text(
            """
            SELECT r.id, r.actor, r.status, d.workflow_key, d.namespace
            FROM workflow_runs r
            JOIN workflow_definitions d ON d.id = r.workflow_definition_id
            WHERE r.project_id = :project_id
              AND r.status IN ('running', 'waiting')
              AND COALESCE(d.namespace, 'system') = 'product'
            ORDER BY r.updated_at
            LIMIT 50
            """
        ),
        {"project_id": project_id},
    ).mappings().all()

    # A FAILED RUN IS NEVER TICKED, SO IT COULD NEVER RECOVER.
    #
    # The scan above takes `running` and `waiting` only, which is right for doing work. But a
    # run whose build failed is `failed`, and the operator's next move is to retry that build —
    # after which nothing looked at the run again. Chicken and egg: failed, therefore never
    # swept, therefore failed for ever, while a new build ran to completion underneath it.
    #
    # These runs are handed to `resume_awaited` and nothing else. On a failed run it asks its
    # failed steps one question — is there a live later attempt at this work — and reopens only
    # those. No claiming, no gates, no new work: reviving a run is not the same as running it,
    # and once revived it is `waiting` and the ordinary loop takes it from there.
    revivable = connection.execute(
        text(
            """
            SELECT r.id, r.actor, r.status, d.workflow_key, d.namespace
            FROM workflow_runs r
            JOIN workflow_definitions d ON d.id = r.workflow_definition_id
            WHERE r.project_id = :project_id
              AND r.status = 'failed'
              AND COALESCE(d.namespace, 'system') = 'product'
              AND EXISTS (
                    SELECT 1 FROM workflow_step_runs s
                    WHERE s.workflow_run_id = r.id AND s.status = 'failed'
                      AND (s.output->'await'->>'kind' = 'code_task'
                           -- asked a person about a cause BOSS placed on itself or the host:
                           -- revived when BOSS's build changes (step_recovery)
                           OR s.output ? 'recovery_resume_on_update')
              )
            ORDER BY r.updated_at DESC
            LIMIT 20
            """
        ),
        {"project_id": project_id},
    ).mappings().all()

    performed: list[dict[str, Any]] = []
    for run in revivable:
        # SAME SAVEPOINT, SAME REASON as the main loop below: "must never break the tick" is not
        # something a bare `except` can deliver once a statement has aborted the transaction.
        savepoint = connection.begin_nested()
        try:
            performed.extend(resume_awaited(
                connection, project_id=project_id, run_id=run["id"], worker_id=worker_id
            ))
        except Exception as error:  # noqa: BLE001 — a revival must never break the tick
            try:
                savepoint.rollback()
            except Exception:  # noqa: BLE001
                pass
            log.warning("could not revive %s: %s", run["id"], error)
        else:
            savepoint.commit()

    # Work that ended elsewhere — a build, a panel, a run finished from another screen — stops
    # showing its agents as busy. It never waits on a lock: this whole tick is one transaction.
    agent_acts.quietly(connection, "close finished work", agent_acts.reconcile, project_id)

    for run in runs:
        if len(performed) >= limit:
            break
        run_id = run["id"]
        # ONE RUN MUST NOT STOP THE REST, AND CATCHING THE EXCEPTION IS NOT ENOUGH.
        #
        # That is what the handler below was written to do, and against a DATABASE error it
        # cannot. Postgres aborts the whole transaction on the first failed statement; every
        # statement after it raises InFailedSqlTransaction regardless of what Python caught. So
        # one bad run poisoned the connection and every later run in the same tick "failed" on a
        # query that was fine — and the caller saw the innocent query named in the error.
        #
        # Observed on 2026-09-27: every tick returned HTTP 500 naming a SELECT in
        # sweep_gate_timeouts, no delivery workflow advanced at all, and the runs sat at `ready`
        # for ever with nothing recorded against them. A SAVEPOINT per run makes the isolation
        # real: a failure rolls back that run's work and leaves the connection usable.
        savepoint = connection.begin_nested()
        # WHICH PHASE FAILED, NOT JUST THAT ONE DID.
        #
        # A run does four distinct things per tick, and a database error in any of them leaves
        # every statement afterwards raising InFailedSqlTransaction — so the exception that
        # escapes names whichever query happened to come next, not the one that broke. Chasing
        # the live failure meant reading a poisoned-transaction message about a SELECT that was
        # perfectly correct, twice. The label travels with the error so the report says where.
        phase = "read the spec"
        try:
            spec = workflows._run_spec(connection, run_id)
            spec_steps = {s["id"]: s for s in spec.get("steps", [])}

            # Work that FINISHED while we were away, first of all: a build that completed a
            # moment ago should release the next step in this pass rather than a tick later.
            # AN ARCHITECTURE SENT BACK IS REWRITTEN NOW, before its step is looked at: the
            # architect writes the version the person asked for and a fresh approval opens.
            phase = "revise architectures sent back"
            from . import architecture_docs

            performed.extend(architecture_docs.revise_sent_back(
                connection, getattr(ctx, "settings", None), project_id=project_id,
                run_id=run_id))

            phase = "resume awaited work"
            performed.extend(resume_awaited(
                connection, project_id=project_id, run_id=run_id, worker_id=worker_id,
                ctx=ctx,
            ))

            # A WAIT THAT IS OVER, ENDED — whatever kind of wait it was. Timers, approval gates
            # (`decision` steps) and "Run another workflow" steps are released by `advance_run`
            # alone, and for a product run nothing called it but this tick. It used to ask about
            # timers only (Deprecation / Sunset's grace period never ended), so an Approval gate
            # placed from the editor's palette sat `waiting` after a person approved it, and a
            # parent never left a child run that had finished. `workflows.a_wait_is_over` asks
            # `workflows._wait_outcome`, the same rule `advance_run` acts on.
            phase = "release waits that are over"
            if workflows.a_wait_is_over(connection, run_id):
                workflows.advance_run(connection, project_id, run_id)

            # Answered gates next, so a step approved a moment ago is released in the same
            # tick rather than waiting for the next one.
            # An open gate's preview follows its content, so nobody approves a stale one.
            phase = "refresh waiting previews"
            _refresh_waiting_previews(connection, project_id, run_id, spec_steps)

            phase = "resume answered gates"
            performed.extend(_resume_answered_gates(
                connection, project_id, {**dict(run), "id": run_id}
            ))
            phase = "open gates and claim work"

            # Then gates to open: they are not claimable work, and leaving them until after the
            # claim would let the runner treat a waiting approval as an idle step forever.
            gates = connection.execute(
                text(
                    """
                    SELECT * FROM workflow_step_runs
                    WHERE workflow_run_id=:run_id AND status='ready' AND step_kind='capability'
                      AND decision_id IS NULL
                    """
                ),
                {"run_id": run_id},
            ).mappings().all()
            for row in gates:
                spec_step = spec_steps.get(row["step_id"]) or {}
                # PER GATE, so the one that breaks is named and the others still open. Deciding
                # a gate reads the step definition and the run's context; opening one WRITES a
                # decision. Both can fail, they fail differently, and without this the error
                # surfaced later as a poisoned transaction on somebody else's SELECT.
                phase = f"decide whether {row['step_id']} needs approval"
                gate_point = connection.begin_nested()
                try:
                    waived: dict[str, Any] = {}
                    subject = _gate_subject(
                        connection, project_id, spec_step, _run_context(connection, run_id),
                        default_owner=_github_owner(ctx), waived=waived,
                    )
                    if subject is None and (
                            (spec_step.get("binding") or {}).get("capability") == "deploy"):
                        # WHAT THE GATE WAS WAIVED FOR, written where execution reads it: the
                        # step may ship to this stack target without a gate, and nowhere else.
                        # Cleared when no gate was waived, so a stale pin cannot linger.
                        connection.execute(
                            text("UPDATE workflow_step_runs SET binding_snapshot = "
                                 "(COALESCE(binding_snapshot, '{}'::jsonb) - :k) "
                                 "|| CAST(:patch AS JSONB), updated_at = now() WHERE id = :i"),
                            {"i": row["id"], "k": STACK_GATE_TARGET,
                             "patch": json.dumps(waived)},
                        )
                    if subject is not None:
                        phase = f"open the approval gate for {row['step_id']}"
                        _open_gate(
                            connection, project_id, {**dict(run), "id": run_id},
                            {**dict(row), "_spec": spec_step, "_subject": subject},
                        )
                        performed.append(
                            {"run_id": run_id, "step_id": row["step_id"],
                             "capability": subject["key"], "outcome": "gate_opened"}
                        )
                except Exception as error:  # noqa: BLE001 — one gate must not stop the run
                    try:
                        gate_point.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    log.warning("could not %s: %s: %s",
                                phase, type(error).__name__, error)
                    performed.append({
                        "run_id": run_id, "step_id": row["step_id"],
                        "outcome": "gate_failed", "phase": phase,
                        "error": f"{type(error).__name__}: {error}",
                    })
                else:
                    gate_point.commit()

            # SIGNED AS IT MOVES. Gates opened, work resumed, answers given since the last tick
            # all show in the run's chain before the next step is claimed. Never waits: a run
            # another tick holds is that tick's to sign, and its lock would be held to the end
            # of this one — through every step it runs.
            phase = "sign the run's work"
            agent_acts.quietly(connection, "sign a run's work", agent_acts.sync_run, run_id,
                               wait=False)

            phase = "claim a step to run"
            claimed = workflows.claim_step(
                connection, project_id=project_id, run_id=run_id,
                worker_id=worker_id, lease_seconds=LEASE_SECONDS,
            )
            if claimed is None:
                continue
            if claimed.get("step_kind") != "capability":
                # Somebody else's step kind. Release it by letting the lease lapse rather than
                # failing it — this runner is not the only possible claimant.
                continue

            spec_step = spec_steps.get(claimed["step_id"]) or {}
            # A STEP THAT THINKS IN THE CALL HOLDS ITS LEASE FOR AS LONG AS IT THINKS. Set up
            # monitoring asks a model, previews every rule and may retune: minutes, in the
            # call. With the two-minute lease its completion was refused ("lease has expired")
            # and it ran again from the start, three times (Order API, 2026-10-19).
            lease = lease_for(spec_step)
            if lease > LEASE_SECONDS:
                phase = f"hold the lease for {claimed['step_id']}"
                claimed = {**claimed, **workflows.heartbeat_step(
                    connection, project_id=project_id, step_run_id=claimed["id"],
                    worker_id=worker_id, lease_seconds=lease)}
            phase = f"execute {claimed['step_id']}"
            # A FAILED STEP MUST LEAVE THE CONNECTION USABLE, OR ITS FAILURE CANNOT BE RECORDED.
            #
            # `fail_step` below opens with SELECT ... FOR UPDATE. When a step's own work aborts
            # the transaction — a constraint violation anywhere inside a skill invocation, an
            # artifact insert, a receipt — that SELECT raises InFailedSqlTransaction and takes
            # the real exception with it. The step is then left `running` with no error against
            # it, the tick reports a message about a query that was fine, and the operator sees
            # a run that has simply stopped. That is precisely how the video workflow sat at
            # step one for half an hour saying nothing at all.
            #
            # Rolling back to the savepoint first restores a working connection, so the handlers
            # below record the ORIGINAL failure instead of dying on the attempt.
            step_point = connection.begin_nested()
            step_agents.forget_resolved()
            # WHO STARTED WHAT THIS STEP STARTS. A build or a panel is created by a tool in its
            # own transaction, before this step's act is committed; this is how it records the
            # step (and the run's person) as its origin, so its stages and seats are signed
            # under this step rather than a root of their own. The step, not its run: a run id
            # in a build's origin would mark the build as governed by this run.
            try:
                try:
                    with agent_acts.origin(step_run_id=claimed["id"],
                                           actor=run.get("actor") or None):
                        output = execute_step(
                            connection, ctx, project_id=project_id,
                            actor=run.get("actor") or "workflow",
                            step=spec_step,
                            spec_steps=spec_steps,
                            context={
                                **_run_context(connection, run_id),
                                # The item, when a for_each started this step run.
                                **item_context(claimed),
                                "step_attempt": int(claimed.get("attempt") or 1),
                                "worker_id": worker_id,
                                # Set only on a step a gate released (_resume_answered_gates
                                # keeps decision_id on the row); read by a custom step's MCP
                                # effect.
                                "approval_decision_id": claimed.get("decision_id"),
                                # The stack target this step's gate was waived for, if any.
                                STACK_GATE_TARGET: (claimed.get("binding_snapshot") or {}).get(
                                    STACK_GATE_TARGET),
                            },
                        )
                except BaseException:
                    try:
                        step_point.rollback()
                    except Exception:  # noqa: BLE001 — already gone; the raise below matters
                        pass
                    raise
                else:
                    step_point.commit()
                if str(output.get("status") or "") in _DID_NOT_SHIP:
                    # A FAILURE THAT IS RETURNED IS STILL A FAILURE.
                    #
                    # The runner failed a step only when execute_step RAISED. A capability that
                    # returned {"status": "failed", "error": ...} was recorded as succeeded —
                    # so every guard written as a `return` silently passed.
                    #
                    # Observed on five runs at once: the preview gate correctly refused
                    # ("the preview at http://127.0.0.1:39917 does not answer, so it cannot have
                    # been reviewed"), the step was marked succeeded, the console showed the
                    # work as deployed, and Infra showed nothing — because nothing had been
                    # deployed and nothing said so. That is the exact failure mode the gate was
                    # written to prevent, reintroduced one layer up.
                    # `problems` is where the deploy connector puts the ACTUAL cause, and it
                    # was going nowhere: the step rendered the dict's shape — literally
                    # "{status, problems}" — as its detail, so the one sentence that explains
                    # the failure ("no ssh_key bound to this target and no global ssh key
                    # configured") never reached the person who had to act on it.
                    problems = output.get("problems")
                    if isinstance(problems, (list, tuple)) and problems:
                        detail = "; ".join(
                            str(p.get("detail") or p.get("message") or p.get("reason") or p)
                            if isinstance(p, dict) else str(p)
                            for p in problems)
                    else:
                        detail = ""
                    reason = str(output.get("error") or detail or output.get("note") or "")
                    if not reason.strip():
                        # "the capability reported failure without a reason" is what an operator
                        # actually saw on a deploy gate, and it is not an error message — it is
                        # the absence of one. It told them nothing, and it told the next engineer
                        # less, because the step's stored output was `{}` as well.
                        #
                        # A failure with nothing to say should be impossible: whatever the
                        # capability DID return is evidence, so it is reported rather than
                        # summarised away. Where it returned nothing at all, say that plainly and
                        # name the thing to look at next.
                        reason = _reason_of_last_resort(output, claimed)
                    workflows.fail_step(
                        connection, project_id=project_id, step_run_id=claimed["id"],
                        worker_id=worker_id, error=reason[:500],
                        # A SETTLED failure stops once, for Retry / Skip: an agent that said it
                        # could not do the work would say so again, at the price of a whole run.
                        retryable=not output.get(SETTLED_FAILURE),
                        # THE OUTPUT SURVIVES THE FAILURE. It was dropped, so a step that failed
                        # stored `{}` and the only record of what happened was the one sentence
                        # above — which in this case said nothing.
                        output=output,
                    )
                    performed.append({
                        "run_id": run_id, "step_id": claimed["step_id"],
                        "capability": output.get("capability"),
                        "outcome": "failed", "error": reason,
                    })
                elif output.get("status") == "needs_decision":
                    # A finding that is meant to stop the line asks the operator, in place,
                    # rather than deadlettering the run and answering for them.
                    decision_id = _park_for_decision(
                        connection, project_id, {**dict(run), "id": run_id},
                        claimed, output,
                    )
                    performed.append({
                        "run_id": run_id, "step_id": claimed["step_id"],
                        "capability": output.get("capability"), "outcome": "needs_decision",
                        "decision_id": decision_id,
                    })
                elif output.get("status") == "awaiting":
                    # Started, not finished. Park it against the id it is waiting on;
                    # `resume_awaited` finishes it when that work reaches a terminal state.
                    workflows.park_step_awaiting(
                        connection, project_id=project_id, step_run_id=claimed["id"],
                        worker_id=worker_id, output=output,
                    )
                    performed.append({
                        "run_id": run_id, "step_id": claimed["step_id"],
                        "capability": output.get("capability"), "outcome": "awaiting",
                        "await": output.get("await"),
                    })
                else:
                    workflows.complete_step(
                        connection, project_id=project_id, step_run_id=claimed["id"],
                        worker_id=worker_id, output=output,
                        evidence=[{
                            "kind": "capability",
                            "capability": output.get("capability"),
                            "tool": output.get("tool"),
                        }],
                    )
                    performed.append({
                        "run_id": run_id, "step_id": claimed["step_id"],
                        "capability": output.get("capability"), "outcome": "succeeded",
                    })
            except CapabilityExecutionError as error:
                agent_acts.note_ran_as(claimed["id"], step_agents.last_resolved())
                wait = _as_wait(error)
                if wait is not None:
                    # NOT YET, NOT FAILED. No `continue` here: it would skip the run's savepoint
                    # commit in the `else` below.
                    waited = workflows.wait_step(
                        connection, project_id=project_id, step_run_id=claimed["id"],
                        worker_id=worker_id, reason=str(wait),
                        wait_seconds=wait.wait_seconds, kind=wait.kind,
                    )
                    performed.append({
                        "run_id": run_id, "step_id": claimed["step_id"],
                        "outcome": "waiting" if waited["status"] == "ready" else "failed",
                        "waiting_for": wait.kind, "error": str(wait),
                    })
                else:
                    error = _as_settled(error)
                    workflows.fail_step(
                        connection, project_id=project_id, step_run_id=claimed["id"],
                        worker_id=worker_id, error=str(error),
                        # A settled verdict fails once and says so. Retrying it spends ticks and
                        # minutes to arrive at the same sentence.
                        retryable=not isinstance(error, SettledCapabilityError),
                    )
                    if isinstance(error, StaleApprovalError):
                        # The approval covered other content, so it no longer releases this
                        # step: Retry opens a fresh gate showing what would be sent now.
                        connection.execute(
                            text("UPDATE workflow_step_runs SET decision_id = NULL, "
                                 "updated_at = now() WHERE id = :i"),
                            {"i": claimed["id"]},
                        )
                    performed.append({
                        "run_id": run_id, "step_id": claimed["step_id"],
                        "outcome": "failed", "error": str(error),
                    })
            except Exception as error:  # noqa: BLE001
                # A CLAIMED STEP MUST NOT BE STRANDED BY A SURPRISE.
                #
                # Only CapabilityExecutionError released the lease. Anything else — a KeyError,
                # a bad row, a library raising something new — escaped to the handler below,
                # which logs and moves on while the step stays `leased` with nobody serving it.
                # Five deploy steps sat exactly like that for fifteen hours, showing a running
                # timer, and the log said `(non-fatal): 'deploy'` — a stringified KeyError,
                # which names neither the type nor where it came from.
                #
                # Failing it retryably is strictly better: the lease is released, the reason is
                # on the step where the operator is looking, and the runtime can try again.
                log.exception(
                    "capability step %s of run %s raised %s",
                    claimed["step_id"], run_id, type(error).__name__,
                )
                agent_acts.note_ran_as(claimed["id"], step_agents.last_resolved())
                workflows.fail_step(
                    connection, project_id=project_id, step_run_id=claimed["id"],
                    worker_id=worker_id,
                    error=f"{type(error).__name__}: {error}"[:500],
                    retryable=True,
                )
                performed.append({
                    "run_id": run_id, "step_id": claimed["step_id"],
                    "outcome": "failed", "error": f"{type(error).__name__}: {error}",
                })
            # The step just settled (succeeded, failed, parked, waiting on a person): signed now.
            agent_acts.quietly(connection, "sign a step", agent_acts.sync_run, run_id,
                               wait=False)
        except Exception as error:                 # noqa: BLE001 - one run must not stop the rest
            # ROLL BACK TO THE SAVEPOINT FIRST. Until that happens the connection is unusable,
            # so even the logging below would fail if it touched the database.
            try:
                savepoint.rollback()
            except Exception:  # noqa: BLE001 — already rolled back, or the connection is gone
                pass
            # The TYPE, always. `(non-fatal): 'deploy'` is a KeyError wearing a disguise: it
            # names neither what went wrong nor where, and it cost a day of guessing.
            log.warning(
                "capability runner skipped run %s while it tried to %s (non-fatal): %s: %s",
                run_id, phase, type(error).__name__, error,
            )
            performed.append({
                "run_id": run_id, "outcome": "skipped",
                "phase": phase,
                "error": f"{type(error).__name__}: {error}",
            })
        else:
            savepoint.commit()

    return {"project_id": project_id, "performed": performed, "count": len(performed)}


def sweep_gate_timeouts(
    connection: Connection, *, project_id: str, actor: str = "workflow-policy"
) -> dict[str, Any]:
    """Apply each waiting gate's timeout policy once its deadline has passed.

    Three outcomes, and the difference between them matters more than the mechanism:

      * `block`   — the default. It stays waiting and the run is marked blocked, so somebody
                    has to look. A gate whose unanswered outcome is "proceed" is not a gate.
      * `escalate`— still waiting, but now loud: an event a human process can pick up.
      * `approve` — allowed only below the risk ceiling, and recorded as a decision made BY
                    POLICY. An unattended approval that leaves no record is how an audit fails.

    Never raises for one bad step. Runs on the worker loop beside the capability tick.
    """
    rows = connection.execute(
        text(
            """
            SELECT s.id, s.step_id, s.workflow_run_id, s.binding_snapshot, s.decision_id,
                   r.actor, d.workflow_key
            FROM workflow_step_runs s
            JOIN workflow_runs r ON r.id = s.workflow_run_id
            JOIN workflow_definitions d ON d.id = r.workflow_definition_id
            WHERE s.project_id = :project_id
              AND s.status = 'waiting'
              AND COALESCE(d.namespace, 'system') = 'product'
              AND s.binding_snapshot ? 'due_at'
              AND s.binding_snapshot->>'due_at' IS NOT NULL
              AND (s.binding_snapshot->>'due_at')::timestamptz < now()
              AND COALESCE(s.binding_snapshot->>'timeout_applied', 'no') = 'no'
            LIMIT 50
            """
        ),
        {"project_id": project_id},
    ).mappings().all()

    applied: list[dict[str, Any]] = []
    for row in rows:
        snapshot = row["binding_snapshot"] or {}
        approval = snapshot.get("approval") or {}
        action = str(approval.get("on_timeout") or "block").lower()
        if action == "approve" and snapshot.get("requires_person"):
            # Belt to _open_gate's braces: a gate opened because only a person may answer it
            # is never approved by policy, even from a snapshot written before that rule.
            action = "block"
        try:
            if action == "approve":
                evidence = [{
                    "kind": "auto_approval",
                    "decided_by": "policy",
                    "sla_hours": approval.get("sla_hours"),
                    "approver_role": approval.get("approver_role"),
                    "due_at": snapshot.get("due_at"),
                }]
                # Same split `_resume_answered_gates` already makes: a gate's approval IS the
                # work, so it finishes here. A work step's approval only unblocks it. Treating
                # both as "succeeded" marked builds and notifies complete without ever calling
                # the tool — and on_timeout=approve is only legal below the risk ceiling, which
                # is exactly the work-step side of that split.
                if snapshot.get("gate_completes_step", True):
                    # NOT `complete_step`. That is the worker path and requires the step to be
                    # leased by the caller — this step is WAITING on a person, leased by nobody.
                    connection.execute(
                        text(
                            """
                            UPDATE workflow_step_runs
                            SET status='succeeded',
                                output = CAST(:output AS JSONB),
                                evidence = COALESCE(evidence, '[]'::jsonb)
                                           || CAST(:evidence AS JSONB),
                                completed_at = now(),
                                updated_at = now()
                            WHERE id = :id AND status = 'waiting'
                            """
                        ),
                        {
                            "id": row["id"],
                            "output": json.dumps({
                                "approved_by": "policy",
                                "reason": "nobody answered within the agreed time",
                                "sla_hours": approval.get("sla_hours"),
                            }),
                            "evidence": json.dumps(evidence),
                        },
                    )
                    # The run has to move on, or a policy approval leaves the workflow parked
                    # at a step that has already been decided.
                    workflows.advance_run(connection, project_id, row["workflow_run_id"])
                else:
                    connection.execute(
                        text(
                            """
                            UPDATE workflow_step_runs
                            SET status='ready',
                                evidence = COALESCE(evidence, '[]'::jsonb)
                                           || CAST(:evidence AS JSONB),
                                binding_snapshot = COALESCE(binding_snapshot, '{}'::jsonb)
                                                   || CAST(:patch AS JSONB),
                                updated_at = now()
                            WHERE id = :id AND status = 'waiting'
                            """
                        ),
                        {
                            "id": row["id"],
                            "evidence": json.dumps(evidence),
                            "patch": json.dumps({
                                "approved_at": now_utc().isoformat(),
                                "approved_by": "policy",
                            }),
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE workflow_runs SET status='running', updated_at=now() "
                            "WHERE id=:id AND status='waiting'"
                        ),
                        {"id": row["workflow_run_id"]},
                    )
                emit_event(
                    connection, project_id, "workflow.gate_auto_approved", actor,
                    "workflow_step_run", row["id"],
                    {"step_id": row["step_id"], "workflow_key": row["workflow_key"],
                     "sla_hours": approval.get("sla_hours")},
                )
                outcome = "auto_approved"
            elif action == "escalate":
                emit_event(
                    connection, project_id, "workflow.gate_escalated", actor,
                    "workflow_step_run", row["id"],
                    {"step_id": row["step_id"], "workflow_key": row["workflow_key"],
                     "approver_role": approval.get("approver_role"),
                     "due_at": snapshot.get("due_at")},
                )
                outcome = "escalated"
            else:
                emit_event(
                    connection, project_id, "workflow.gate_overdue", actor,
                    "workflow_step_run", row["id"],
                    {"step_id": row["step_id"], "workflow_key": row["workflow_key"],
                     "due_at": snapshot.get("due_at")},
                )
                outcome = "blocked"

            # Marked so a gate is not escalated once a minute forever. Escalating repeatedly is
            # how people learn to ignore escalations.
            connection.execute(
                text(
                    "UPDATE workflow_step_runs "
                    "SET binding_snapshot = COALESCE(binding_snapshot,'{}'::jsonb) "
                    "    || CAST(:patch AS JSONB), updated_at=now() "
                    "WHERE id=:id"
                ),
                {"id": row["id"],
                 "patch": json.dumps({"timeout_applied": outcome,
                                      "timeout_applied_at": now_utc().isoformat()})},
            )
            applied.append({"step_run_id": row["id"], "step_id": row["step_id"],
                            "outcome": outcome})
        except Exception as error:                 # noqa: BLE001
            log.warning("gate timeout sweep skipped %s (non-fatal): %s", row["id"], error)

    return {"project_id": project_id, "applied": applied, "count": len(applied)}
