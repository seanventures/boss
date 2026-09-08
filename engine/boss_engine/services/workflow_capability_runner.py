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

import json
import logging
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import change_record, workflow_custom_steps as custom_steps, workflows
from .core import create_blocking_decision, emit_event, now_utc
from .workflow_capabilities import CAPABILITY_KEYS, get as get_capability

log = logging.getLogger(__name__)

#: Long enough for a tool call that talks to GitHub or a sandbox, short enough that a runner
#: which dies does not strand the step for long. The runtime reclaims an expired lease.
LEASE_SECONDS = 120

#: Per tick. The point is steady progress across many runs, not draining one to completion
#: while the others wait.
MAX_STEPS_PER_TICK = 10


class CapabilityExecutionError(RuntimeError):
    """The capability could not be performed. Carries the reason the step will record."""


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
        if isinstance(value, dict) and "from_input" in value:
            # What the run was STARTED with. A catalogue workflow cannot hard-code the thing
            # being built — "Standard Product Delivery" has to work for any directive — so its
            # first steps read from the run's input instead of a literal.
            field = str(value["from_input"])
            run_input = context.get("input") or {}
            if field not in run_input:
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
            resolved[name] = run_input[field]
        elif isinstance(value, dict) and "from_step" in value:
            step_id = str(value["from_step"])
            field = str(value.get("field") or "")
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
    rows = connection.execute(
        text(
            """
            SELECT step_id, output FROM workflow_step_runs
            WHERE workflow_run_id = :run_id AND status = 'succeeded'
            ORDER BY completed_at
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    steps: dict[str, Any] = {}
    for row in rows:
        output = row["output"]
        if isinstance(output, dict):
            steps[row["step_id"]] = output
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
        "input": run_input if isinstance(run_input, dict) else {},
        "run_id": run_id,
        "step_index": len(rows),
        "objective": str(spec.get("objective") or ""),
        "workflow_name": str(spec.get("workflow_name") or (row or {}).get("workflow_name") or ""),
    }


def _tool_registry():
    """The same registry the chat agent and MCP use. Imported lazily so this module can be
    read (and unit-tested) without constructing the whole tool layer."""
    from ..tools.builtin import build_default_registry

    return build_default_registry()


def _invoke_tool(
    ctx: Any, tool_name: str, project_id: str, actor: str, args: dict[str, Any]
) -> dict[str, Any]:
    registry = _tool_registry()
    spec = {s.name: s for s in registry.specs()}.get(tool_name)
    if spec is None:
        raise CapabilityExecutionError(
            f"this BOSS does not have the '{tool_name}' tool installed."
        )
    result = spec.handler(ctx, project_id=project_id, actor=actor, args=args)
    if isinstance(result, dict) and result.get("ok") is False:
        raise CapabilityExecutionError(str(result.get("error") or f"{tool_name} failed."))
    return result if isinstance(result, dict) else {"result": result}


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
    for name in getattr(capability, "optional_inputs", ()) or ():
        if name in filled:
            continue
        value = run_input.get(name)
        if value not in (None, "", [], {}):
            filled[name] = value
    return filled


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
    return {
        "capability": "monitor",
        "tool": None,
        "status": "observed",
        "targets": watched,
        "note": (
            f"{len(watched)} target(s) under health probing"
            + (f", {len(probed)} probed so far." if probed else ", none probed yet.")
        ),
    }


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
    title = (str(inputs.get("title") or "").strip()
             or str(run_input.get("title") or "").strip()
             or str(run_input.get("directive") or "").strip().split("\n")[0].strip()[:80]
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
    result = _write_the_record(connection, project_id, context,
                               {"title": row["title"]})
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
    return {"preview_id": row["id"], "preview_url": row["url"], "adopted": True}


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
               context: dict[str, Any]) -> dict[str, Any]:
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

    started = _invoke_tool(ctx, "extend_code_task", project_id, actor, {
        "run_id": build_run,
        "instructions": _plrr_brief(connection, project_id, context),
        "title": "Production launch readiness",
    })
    return {"capability": "plrr", "tool": "extend_code_task", **started}


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
    if row["status"] != "succeeded":
        return (f"the preview step is '{row['status']}', so there is nothing to look at yet. "
                "Run the preview, then deploy.")
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


def _live_preview_for_run(connection: Connection, run_id: str) -> Optional[str]:
    """The id of a preview RUNNING NOW for this workflow's app, or None.

    Matched through the code task the run built, widened to every build sharing that app's
    identity — `finalize_ctx->>'app_id'`, which hashes the project in, so it can never reach
    another project's app.
    """
    try:
        task = connection.execute(
            text("SELECT COALESCE("
                 "  (SELECT input->>'adopt_code_task_run_id' FROM workflow_runs WHERE id = :r),"
                 "  (SELECT output->>'code_task_run_id' FROM workflow_step_runs "
                 "     WHERE workflow_run_id = :r AND output ? 'code_task_run_id' "
                 "     ORDER BY created_at DESC LIMIT 1)"
                 ") AS task_id"),
            {"r": run_id},
        ).scalar()
        if not task:
            return None
        return connection.execute(
            text("""
                SELECT p.id FROM app_previews p
                WHERE p.status = 'running'
                  AND (p.run_id = :t
                       OR p.run_id IN (
                            SELECT c.id FROM code_task_runs c
                            WHERE NULLIF(c.finalize_ctx->>'app_id', '') = (
                                SELECT NULLIF(finalize_ctx->>'app_id', '')
                                FROM code_task_runs WHERE id = :t)
                              AND NULLIF(c.finalize_ctx->>'app_id', '') IS NOT NULL))
                ORDER BY p.created_at DESC LIMIT 1
            """),
            {"t": task},
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


def execute_step(
    connection: Connection,
    ctx: Any,
    *,
    project_id: str,
    actor: str,
    step: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Perform one capability. Returns the step's output.

    Raises CapabilityExecutionError with a sentence an operator can act on — the step records
    it, and "step failed" with no reason is the thing this exists to avoid.
    """
    binding = step.get("binding") or {}

    # A step the customer defined. Checked first because it is the only kind that has no
    # capability key at all — falling through to the capability lookup would report it as an
    # unknown capability, which is true and useless.
    if binding.get("custom_step"):
        try:
            return custom_steps.execute(
                connection, getattr(ctx, "settings", None),
                project_id=project_id, actor=actor, step=step,
                inputs=_resolve_inputs(binding.get("inputs") or {}, context),
                run_id=str(context.get("run_id") or ""),
                step_index=int(context.get("step_index") or 0),
            )
        except custom_steps.CustomStepError as error:
            # Translated, not wrapped: the runner's failure path records
            # CapabilityExecutionError, and the customer's sentence is the part worth keeping.
            raise CapabilityExecutionError(str(error)) from error

    key = str(binding.get("capability") or "")
    if key not in CAPABILITY_KEYS:
        raise CapabilityExecutionError(f"unknown capability '{key}'.")
    capability = get_capability(key)

    if capability.is_gate:
        # Except when the run already HAS the answer — `_gate_subject` declined to open a gate,
        # so the step arrived here as ordinary work. Record the approval that was given and move
        # on; the audit trail names the decision, so "who approved this" is still answerable.
        adopted_gate = _adopted_gate(connection, key, context)
        if adopted_gate:
            return {
                "capability": key,
                "tool": None,
                "status": "adopted",
                "note": (
                    f"'{capability.label}' was answered before this run started "
                    f"({adopted_gate['decision_status']})."
                ),
                **adopted_gate,
            }
        # Otherwise: gates are turned into decisions before they are claimable.
        raise CapabilityExecutionError(
            f"'{capability.label}' waits for a person and is not executable work."
        )

    inputs = _resolve_inputs(binding.get("inputs") or {}, context,
                             optional=frozenset(capability.optional_inputs or ()))
    inputs = _fill_optional_from_run_input(capability, inputs, context)

    if capability.tool is None:
        # `document` has no tool because it commissions nothing — it WRITES, from what the run
        # already produced. Falling through to the no-op below made the one step whose entire
        # job is to leave an artefact behind the only step that left nothing.
        if key == "document":
            return _write_the_record(connection, project_id, context, inputs)
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
    if key == "deploy":
        missing = _preview_missing_for_run(connection, context)
        if missing:
            return {"capability": key, "tool": None, "status": "failed",
                    "error": missing,
                    "note": "this workflow previews before it ships; nothing was previewable."}

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
    if key == "plrr":
        # Has no single tool: it reads the run's own spec, then starts the repair that closes
        # what the app is missing. Handled before _invoke_tool for that reason.
        return _plrr_step(ctx, connection, project_id, actor, context)

    if key == "build":
        inputs = {**inputs, "workflow_run_id": str(context.get("run_id") or "")}

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
        if objective:
            inputs = {**inputs, "objective": objective,
                      "workflow_name": str(context.get("workflow_name") or "").strip()}

    output = _invoke_tool(ctx, capability.tool, project_id, actor, inputs)
    result = {"capability": key, "tool": capability.tool, **output}

    # STARTED IS NOT FINISHED.
    #
    # An async capability's tool returns as soon as the work is ACCEPTED: create_code_task hands
    # back a run id in milliseconds for a build that takes twenty minutes. Returning here made
    # the runner complete the step immediately, so a workflow raced past `build` into `review`
    # and `test` while the build was still cloning — every run finishing in seconds, in a shape
    # that looked like success.
    #
    # Marked as awaiting instead. The caller parks the step against this id, and `resume_awaited`
    # completes it when the work reaches a terminal state.
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
    # `deploy` is declared is_async — it does take a while — but there is no await table for a
    # deployment, so it has no entry here. The first version indexed _AWAIT_KINDS[key] while
    # building the error message and raised KeyError('deploy') on every deploy step. That
    # escaped the handler that releases leases, so five deploy steps sat `leased` for fifteen
    # hours showing a running timer, and the log read `(non-fatal): 'deploy'`.
    #
    # A capability with no await spec cannot be parked against anything, so demanding a handle
    # from it is asking for something that does not exist.
    if (capability.is_async and key in _AWAIT_KINDS
            and str(result.get("status") or "") not in _DONE_STATUSES):
        result["status"] = "failed"
        result["error"] = (
            f"{capability.tool} started work but returned no id to wait on, so this step "
            f"cannot tell when it finished. Looked for "
            f"{', '.join(_AWAIT_KINDS[key][1])} in the result"
            f"{f' and in its {key!r} envelope' if key in _AWAIT_KINDS else ''}."
        )
    return result


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
    # STARTED IS NOT SERVING. start_preview returns as soon as the request is accepted, so the
    # step completed while the app was still booting — and the workflow walked on to `deploy`
    # with nothing anybody could look at. "Without a preview, do not move to deploy" is the
    # whole reason this step sits where it does.
    "preview": ("preview", ("preview_id", "id")),
    # PLRR closes the gaps it finds by WRITING CODE, so when it finds any it hands back a code
    # task and waits for it exactly as `build` does. A review that only files findings leaves
    # the operator to do the work the review exists to prevent.
    "plrr": ("code_task", ("code_task_run_id", "run_id", "id")),
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
_DID_NOT_SHIP = frozenset({"failed", "blocked", "refused", "error"})


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
    spec = _AWAIT_KINDS.get(key)
    if not spec:
        return None
    kind, fields = spec
    for scope in (output, output.get(kind), output.get(key)):
        if not isinstance(scope, dict):
            continue
        for field in fields:
            value = scope.get(field)
            if isinstance(value, str) and value.strip():
                return {"kind": kind, "id": value.strip()}
    return None


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
    "preview": ("app_previews",
                frozenset({"running", "verified"}),
                frozenset({"failed", "stopped", "expired"})),
}


def _terminal_evidence(
    connection: Connection, kind: str, awaited_id: str
) -> dict[str, Any]:
    """What the finished work produced, for the step that was waiting on it.

    A build's pull request is both its outcome and the input every later step needs, so it goes
    onto the step's output rather than being looked up again by preview and deploy.
    """
    try:
        if kind == "code_task":
            row = connection.execute(
                text("SELECT pull_request_url, pull_request_number FROM code_task_runs "
                     "WHERE id = :r"),
                {"r": awaited_id},
            ).mappings().first()
            if not row or not row["pull_request_url"]:
                return {}
            return {"pull_request_url": row["pull_request_url"],
                    "pull_request_number": row["pull_request_number"]}
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
    except Exception as error:  # noqa: BLE001 — never fail a step over its own evidence
        log.warning("cannot read the result of %s: %s", awaited_id, error)
    return {}


#: How much of a dossier travels into the next step's prompt. Enough to change what a panel
#: argues about; not so much that it crowds out the directive itself.
_DOSSIER_CHARS = 4000


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


def resume_awaited(
    connection: Connection, *, project_id: str, run_id: str, worker_id: str
) -> list[dict[str, Any]]:
    """Finish steps whose awaited work has reached a terminal state.

    Without this a parked step waits forever: nothing else in the system knows a workflow step
    is watching a code task. Runs every tick, before claiming new work, so a build that
    finished a moment ago releases the next step in the same pass rather than a tick later.

    A step whose work FAILED fails the step, with the underlying status in the message. Silently
    succeeding would let a workflow report a shipped product on top of a build that did not.
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
            continue

        table_spec = _AWAIT_TABLES.get(kind)
        if not table_spec or not awaited_id:
            # A step waiting for a timer, a gate or a subworkflow. Those have their own
            # resumption paths and must not be touched here.
            continue
        table, ok_states, bad_states = table_spec
        try:
            row = connection.execute(
                text(f"SELECT status FROM {table} WHERE id = :id"),  # noqa: S608 - fixed set
                {"id": awaited_id},
            ).mappings().first()
        except Exception as error:  # noqa: BLE001 — a missing optional table is not fatal
            log.warning("cannot check awaited %s %s: %s", kind, awaited_id, error)
            continue
        if not row:
            continue
        status = str(row["status"] or "")
        if status in ok_states:
            # DID IT PRODUCE A PULL REQUEST?
            #
            # That is the whole question the outer loop asks of a build. The inner pipeline —
            # implement, adversarial review, security check, mutation check, tests — owns every
            # judgement inside it, and reports its verdict through its RESULT: a refuted change
            # opens no PR. Reading its individual gates from out here was the outer loop doing
            # the inner loop's job, and it stopped a run whose app was already previewable.
            evidence = _terminal_evidence(connection, kind, awaited_id)
            if kind == "code_task" and not evidence.get("pull_request_url"):
                workflows.force_fail_waiting_step(
                    connection, project_id=project_id, step_run_id=step["id"],
                    error=("the build finished without opening a pull request — its reviewers "
                           "refused the change, so there is nothing to preview or deploy."),
                )
                done.append({"run_id": run_id, "step_id": step["step_id"],
                             "outcome": "failed", "awaited": kind, "reason": "no pull request"})
                continue
            workflows.force_complete_waiting_step(
                connection, project_id=project_id, step_run_id=step["id"],
                output={**output, "status": "succeeded", "awaited_status": status, **evidence},
            )
            done.append({"run_id": run_id, "step_id": step["step_id"],
                         "outcome": "succeeded", "awaited": kind})
        elif status in bad_states and kind == "discovery":
            # RESEARCH THAT DID NOT FINISH FAILED — AND THE PROJECT CARRIES ON.
            #
            # Two facts, and the step has to hold both. It FAILED: saying "not run" about work
            # that ran and died hides a thing worth fixing, and this one has been failing for
            # reasons of its own. But it must not sink the run — research is enrichment, the
            # binding into `meeting` is optional, and a cancelled discovery took a whole build
            # down with it before a line of code was written (wfr_9782313e30cf4826a0).
            #
            # AND WHATEVER IT FOUND STILL GOES TO THE PANEL. A run that dies partway has
            # usually written its brief and some findings already; throwing those away wastes
            # the search that was actually done and sends the panel in with nothing. Partial
            # evidence argued over beats no evidence.
            partial = _terminal_evidence(connection, kind, awaited_id)
            salvaged = str(partial.get("dossier") or "")
            workflows.force_complete_waiting_step(
                connection, project_id=project_id, step_run_id=step["id"],
                output={**output, **partial,
                        "status": "failed_continued",
                        "awaited_status": status,
                        "note": (
                            f"research {status} before it finished"
                            + (" — the panel gets what it had reached by then."
                               if salvaged else
                               " and had produced nothing, so the panel argues from the "
                               "directive alone.")
                        )},
            )
            done.append({"run_id": run_id, "step_id": step["step_id"],
                         "outcome": "failed_continued", "awaited": kind,
                         "awaited_status": status, "salvaged": bool(salvaged)})
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
            successor = _successor_attempt(connection, project_id, awaited_id, run_id)
            if kind == "code_task" and successor:
                workflows.repoint_waiting_step(
                    connection, project_id=project_id, step_run_id=step["id"],
                    handle={**handle, "id": successor},
                    note=(f"the build was retried; this step now follows {successor} "
                          f"(the earlier attempt {awaited_id} ended as '{status}')"),
                )
                done.append({"run_id": run_id, "step_id": step["step_id"],
                             "outcome": "following_retry", "awaited": kind,
                             "was": awaited_id, "now": successor})
                continue
            workflows.force_fail_waiting_step(
                connection, project_id=project_id, step_run_id=step["id"],
                error=f"the {kind} it started finished as '{status}'",
            )
            done.append({"run_id": run_id, "step_id": step["step_id"],
                         "outcome": "failed", "awaited": kind, "awaited_status": status})
    return done


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
                    SELECT COALESCE(NULLIF(finalize_ctx->>'root_run_id', ''), id) AS root
                    FROM code_task_runs WHERE id = :awaited
                )
                SELECT r.id
                FROM code_task_runs r, target t
                WHERE r.project_id = :p
                  AND r.id <> :awaited
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

def _gate_subject(
    connection: Connection,
    project_id: str,
    spec_step: dict[str, Any],
    context: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """What a person has to approve before this step, or None if it just runs.

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

    custom_key = str(binding.get("custom_step") or "")
    if custom_key:
        definition = custom_steps.get(connection, project_id, custom_key)
        if definition is None or not definition["usable"] or not definition["enabled"]:
            # Not gated, so it reaches execute_step and fails there with the sentence naming
            # what is actually wrong. Gating it instead would ask a person to approve a step
            # that cannot run, and an unusable step defaults to the risk floor — so a deleted
            # skill would have produced an approval request rather than an error.
            return None
        if custom_steps.approval_for(
            {"risk_tier": definition["risk_tier"]}, requested
        ) != "manual":
            return None
        return {
            "key": custom_key,
            "label": definition["label"],
            "summary": (
                f"Approve before '{definition['label']}' runs. It calls "
                f"{definition['risk_source']} at risk tier {definition['risk_tier']}."
            ),
            "risk_tier": definition["risk_tier"],
            "completes_step": False,
        }

    key = str(binding.get("capability") or "")
    if key not in CAPABILITY_KEYS:
        return None
    capability = get_capability(key)
    if capability.is_gate:
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
            "summary": f"Approve before '{capability.label}' runs.",
            "risk_tier": capability.risk_tier, "completes_step": False,
        }
    return None


def _open_gate(
    connection: Connection, project_id: str, run: dict[str, Any], step_row: dict[str, Any]
) -> None:
    """Turn a step that needs approval into a decision a person can answer, and leave it
    waiting."""
    spec_step = step_row.get("_spec") or {}
    subject = step_row["_subject"]
    approval = spec_step.get("approval") or {}
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
    subject_name = str((run.get("input") or {}).get("title")
                       or run.get("workflow_key") or "workflow").strip()[:80]
    decision_id = subject.get("decision_id") or create_blocking_decision(
        connection,
        project_id,
        run.get("task_id"),
        f"{subject['label']}: {subject_name} · run {run['id'][-8:]}",
        {
            "capability": subject["key"],
            "step_id": step_row["step_id"],
            "workflow_run_id": run["id"],
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
            "app_name": str((run.get("input") or {}).get("app_name")
                            or (run.get("input") or {}).get("title") or "").strip()[:120],
            "environment": str((run.get("input") or {}).get("environment") or "staging"),
            # Whether answering this finishes the step or merely lets it run. The two have
            # different consequences and the page has never been able to tell them apart.
            "gate_completes_step": bool(subject["completes_step"]),
        },
        {"estimated_usd": 0.0},
        subject["summary"],
        risk_tier=max(2, int(subject["risk_tier"])),
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
    decision_id = create_blocking_decision(
        connection,
        project_id,
        run.get("task_id"),
        str(output.get("question") or "Continue this workflow?"),
        {
            "capability": output.get("capability"),
            "step_id": step_row["step_id"],
            "workflow_run_id": run["id"],
            "findings": output.get("findings") or [],
            "code_task_run_id": output.get("code_task_run_id"),
        },
        {"estimated_usd": 0.0},
        str(output.get("note") or ""),
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
            SELECT s.id, s.step_id, s.binding_snapshot, s.iteration, d.status AS decision_status
            FROM workflow_step_runs s
            JOIN decisions d ON d.id = s.decision_id
            WHERE s.workflow_run_id = :run_id
              AND s.status = 'waiting'
              AND s.step_kind = 'capability'
              AND d.status IN ('approved', 'rejected', 'expired')
            """
        ),
        {"run_id": run["id"]},
    ).mappings().all()

    acted: list[dict[str, Any]] = []
    for row in rows:
        snapshot = row["binding_snapshot"] or {}
        if row["decision_status"] != "approved":
            workflows._block_run(
                connection, run["id"],
                f"step '{row['step_id']}' was {row['decision_status']}",
            )
            acted.append({"run_id": run["id"], "step_id": row["step_id"],
                          "outcome": f"gate_{row['decision_status']}"})
            continue

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
                    "output": json.dumps({"approved_by": "person", "outcome": "approved"}),
                    "evidence": json.dumps([{"kind": "approval", "decided_by": "person"}]),
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
                 "patch": json.dumps({"approved_at": now_utc().isoformat()})},
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
                      AND s.output->'await'->>'kind' = 'code_task'
              )
            ORDER BY r.updated_at DESC
            LIMIT 20
            """
        ),
        {"project_id": project_id},
    ).mappings().all()

    performed: list[dict[str, Any]] = []
    for run in revivable:
        try:
            performed.extend(resume_awaited(
                connection, project_id=project_id, run_id=run["id"], worker_id=worker_id
            ))
        except Exception as error:  # noqa: BLE001 — a revival must never break the tick
            log.warning("could not revive %s: %s", run["id"], error)

    for run in runs:
        if len(performed) >= limit:
            break
        run_id = run["id"]
        try:
            spec = workflows._run_spec(connection, run_id)
            spec_steps = {s["id"]: s for s in spec.get("steps", [])}

            # Work that FINISHED while we were away, first of all: a build that completed a
            # moment ago should release the next step in this pass rather than a tick later.
            performed.extend(resume_awaited(
                connection, project_id=project_id, run_id=run_id, worker_id=worker_id
            ))

            # Answered gates next, so a step approved a moment ago is released in the same
            # tick rather than waiting for the next one.
            performed.extend(_resume_answered_gates(
                connection, project_id, {**dict(run), "id": run_id}
            ))

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
                subject = _gate_subject(
                    connection, project_id, spec_step, _run_context(connection, run_id)
                )
                if subject is not None:
                    _open_gate(
                        connection, project_id, {**dict(run), "id": run_id},
                        {**dict(row), "_spec": spec_step, "_subject": subject},
                    )
                    performed.append(
                        {"run_id": run_id, "step_id": row["step_id"],
                         "capability": subject["key"], "outcome": "gate_opened"}
                    )

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
            try:
                output = execute_step(
                    connection, ctx, project_id=project_id,
                    actor=run.get("actor") or "workflow",
                    step=spec_step, context=_run_context(connection, run_id),
                )
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
                        worker_id=worker_id, error=reason[:500], retryable=True,
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
                workflows.fail_step(
                    connection, project_id=project_id, step_run_id=claimed["id"],
                    worker_id=worker_id, error=str(error),
                    # A settled verdict fails once and says so. Retrying it spends ticks and
                    # minutes to arrive at the same sentence.
                    retryable=not isinstance(error, SettledCapabilityError),
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
        except Exception as error:                 # noqa: BLE001 - one run must not stop the rest
            # The TYPE, always. `(non-fatal): 'deploy'` is a KeyError wearing a disguise: it
            # names neither what went wrong nor where, and it cost a day of guessing.
            log.warning(
                "capability runner skipped run %s (non-fatal): %s: %s",
                run_id, type(error).__name__, error,
            )
            performed.append({
                "run_id": run_id, "outcome": "skipped",
                "error": f"{type(error).__name__}: {error}",
            })

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
        try:
            if action == "approve":
                # NOT `complete_step`. That is the worker path and requires the step to be
                # leased by the caller — this step is WAITING on a person, leased by nobody.
                # Approving by policy is a different act from finishing work, and borrowing
                # the worker's function to do it would have meant lying about who did what.
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
                        # The evidence IS the record. "Approved by policy" beside the SLA it
                        # was approved under is what an auditor needs; "approved" alone is not.
                        "evidence": json.dumps([{
                            "kind": "auto_approval",
                            "decided_by": "policy",
                            "sla_hours": approval.get("sla_hours"),
                            "approver_role": approval.get("approver_role"),
                            "due_at": snapshot.get("due_at"),
                        }]),
                    },
                )
                emit_event(
                    connection, project_id, "workflow.gate_auto_approved", actor,
                    "workflow_step_run", row["id"],
                    {"step_id": row["step_id"], "workflow_key": row["workflow_key"],
                     "sla_hours": approval.get("sla_hours")},
                )
                # The run has to move on, or a policy approval leaves the workflow parked at a
                # step that has already been decided.
                workflows.advance_run(connection, project_id, row["workflow_run_id"])
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
