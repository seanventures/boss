"""TEST RUNS WITH PINNED DATA, AND RE-RUNNING A RUN FROM ONE OF ITS STEPS (#512).

A TEST RUN is an ordinary workflow run with `mode = 'test'`. Two things are different about it:

* A step can be PINNED: given an output (a JSON object) up front. A pinned step does not execute.
  When it becomes due it is recorded `succeeded` with that output, `output.pinned = true` and the
  pin's provenance under `output.pin`, and a `workflow.step_pinned` event says so. Its output is
  merged into the run's context exactly as a real output would be, so the steps after it read it.
* A step that could SPEND or have an EXTERNAL EFFECT is REFUSED unless it is pinned or the person
  starting the test named it in `allow_live`. Refused means: the step is failed with reason
  `test_run_refused`, a `workflow.step_test_refused` event is written and the run stops there —
  visibly, never silently. "Could spend or have an effect" is decided by `effect_of`, from the
  capability registry's own `risk_tier` / `needs_model` (0 = reads), so the test gate and the
  approval floor read one declaration. Anything the registry cannot classify is refused.

  A step named in `allow_live` runs FOR REAL through the ordinary runner: the same approval gates,
  budget reservations, receipts and dedupe keys a normal run gets. A test run never has a cheaper
  path to an effect than a normal run does — it only has more ways to NOT have one.

A RE-RUN FROM A STEP starts a new run of the same pinned workflow version, with every step before
the chosen one pinned to what it produced in the source run (provenance: the source run and step
run ids), recorded with `rerun_of = {run_id, from_step}`. It is refused when a step the chosen
step certainly depends on (`workflows._runs_before`) did not complete in the source run. A re-run
keeps the source run's mode; a re-run of a test run keeps that run's own pins on later steps and
its `allow_live` list.

Control steps (condition, parallel, join, loop, decision, timer, sub-workflow) are never pinned:
they are re-decided from the run's context, so a condition takes the same branch it took before,
and an approval is asked again rather than copied from another run.

Test runs are excluded from the run lists and the scorecards/cost readers by default
(`include_test` to see them); every run carries `mode` so a reader can always tell.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event

NORMAL_MODE = "normal"
TEST_MODE = "test"
RUN_MODES = (NORMAL_MODE, TEST_MODE)

PIN_EVENT = "workflow.step_pinned"
REFUSED_EVENT = "workflow.step_test_refused"
LIVE_EVENT = "workflow.step_test_live"
RERUN_EVENT = "workflow.rerun_started"
REFUSED_REASON = "test_run_refused"

#: One pinned output, serialised. A pin stands in for a step's output, not for a data store.
MAX_PIN_BYTES = 64_000
MAX_PINS = 200

#: Keys a pinned step's output carries about the pin itself. Never merged into the run context,
#: and stripped when a re-run copies an output that was itself pinned.
PIN_META_KEYS = ("pinned", "pin")


def _workflows():
    from . import workflows

    return workflows


def is_test(run: Any) -> bool:
    return str((dict(run) if run is not None else {}).get("mode") or NORMAL_MODE) == TEST_MODE


def _json(value: Any, default: Any) -> Any:
    return _workflows()._json(value, default)


def effect_of(step: Mapping[str, Any]) -> Optional[str]:
    """Why running this step for real could spend money or reach outside BOSS — or None.

    Read from the capability registry's own risk declaration, so the test gate cannot disagree
    with the approval floor about what a step does. Anything not provably free is refused.
    """
    wf = _workflows()
    kind = str(step.get("kind") or "")
    if kind in wf.CONTROL_KINDS:
        if kind == "subworkflow":
            return "starts another workflow run, which would run for real"
        return None
    if kind == "capability":
        from .workflow_capabilities import CAPABILITIES

        key = str((step.get("binding") or {}).get("capability") or "")
        capability = CAPABILITIES.get(key)
        if capability is None:
            return "is a step BOSS cannot classify, so it is treated as one that spends"
        if capability.is_gate:
            return None   # a gate asks a person; it neither spends nor changes anything
        if capability.needs_model:
            return f"uses a model ({capability.label}), which spends"
        if capability.risk_tier >= 1:
            return (f"is risk tier {capability.risk_tier} ({capability.label}): it spends or "
                    "changes something outside BOSS")
        return None
    return f"is a '{kind}' step, which may spend or change something outside BOSS"


def pinnable(step: Mapping[str, Any]) -> bool:
    return str(step.get("kind") or "") not in _workflows().CONTROL_KINDS


def normalise_pins(spec: Mapping[str, Any], pins: Any, *, actor: str) -> dict[str, Any]:
    """`{step_id: output}` as given by a person -> `{step_id: {output, source}}`, validated."""
    wf = _workflows()
    if pins in (None, ""):
        return {}
    if not isinstance(pins, dict):
        raise wf.WorkflowValidationError("pins must be an object of step id -> output object")
    if len(pins) > MAX_PINS:
        raise wf.WorkflowValidationError(f"at most {MAX_PINS} pins")
    steps = wf._step_map(dict(spec))
    out: dict[str, Any] = {}
    for step_id, output in pins.items():
        step_id = str(step_id)
        step = steps.get(step_id)
        if step is None:
            raise wf.WorkflowValidationError(f"pin for step '{step_id}': no such step")
        if not pinnable(step):
            raise wf.WorkflowValidationError(
                f"pin for step '{step_id}': a {step.get('kind')} step decides from the run "
                "itself and cannot be pinned")
        if not isinstance(output, dict):
            raise wf.WorkflowValidationError(
                f"pin for step '{step_id}': the output must be a JSON object")
        if any(k in output for k in PIN_META_KEYS):
            output = {k: v for k, v in output.items() if k not in PIN_META_KEYS}
        size = len(json.dumps(output))
        if size > MAX_PIN_BYTES:
            raise wf.WorkflowValidationError(
                f"pin for step '{step_id}' is {size} bytes; keep it under {MAX_PIN_BYTES}")
        out[step_id] = {"output": output, "source": {"kind": "person", "actor": actor}}
    return out


def normalise_allow_live(spec: Mapping[str, Any], allow: Any) -> list[str]:
    wf = _workflows()
    if allow in (None, ""):
        return []
    if not isinstance(allow, (list, tuple)):
        raise wf.WorkflowValidationError("allow_live must be a list of step ids")
    steps = wf._step_map(dict(spec))
    named = []
    for step_id in allow:
        step_id = str(step_id)
        if step_id not in steps:
            raise wf.WorkflowValidationError(f"allow_live: no step '{step_id}'")
        if step_id not in named:
            named.append(step_id)
    return named


def _version_spec(connection: Connection, project_id: str, version_id: str) -> dict[str, Any]:
    wf = _workflows()
    version = wf._version_row(connection, project_id, version_id)
    return wf._published_spec(version["spec"], wf.project_roster(connection, project_id))


def start_test_run(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    workflow_version_id: str,
    input_data: Optional[dict[str, Any]] = None,
    pins: Optional[dict[str, Any]] = None,
    allow_live: Optional[Iterable[str]] = None,
    dedupe_key: Optional[str] = None,
) -> dict[str, Any]:
    """Start a run of `workflow_version_id` in test mode with its pins recorded."""
    wf = _workflows()
    if not str(workflow_version_id or "").strip():
        raise wf.WorkflowValidationError("workflow_version_id is required")
    spec = _version_spec(connection, project_id, str(workflow_version_id))
    pinned = normalise_pins(spec, pins, actor=actor)
    allowed = normalise_allow_live(spec, list(allow_live or []))
    return wf.start_run(
        connection, project_id=project_id, actor=actor, input_data=dict(input_data or {}),
        dedupe_key=dedupe_key or f"test:{uuid.uuid4().hex}",
        workflow_version_id=str(workflow_version_id),
        mode=TEST_MODE, pins=pinned, allow_live=allowed)


def _upstream(spec: Mapping[str, Any], step_id: str) -> set[str]:
    """Every step from which `step_id` can be reached along a success edge."""
    wf = _workflows()
    steps = wf._step_map(dict(spec))
    preds: dict[str, set[str]] = {sid: set() for sid in steps}
    for sid, step in steps.items():
        for target in wf._success_targets(step):
            if target in preds:
                preds[target].add(sid)
    seen: set[str] = set()
    frontier = [step_id]
    while frontier:
        for pred in preds.get(frontier.pop(), ()):
            if pred not in seen and pred != step_id:
                seen.add(pred)
                frontier.append(pred)
    return seen


def _latest_rows(connection: Connection, run_id: str) -> dict[str, Any]:
    rows = connection.execute(
        text("SELECT * FROM workflow_step_runs WHERE workflow_run_id=:r "
             "ORDER BY step_id, iteration"),
        {"r": run_id},
    ).mappings().all()
    latest: dict[str, Any] = {}
    for row in rows:
        latest[str(row["step_id"])] = row   # ordered by iteration: the last one wins
    return latest


def rerun_from_step(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    from_step: str,
    actor: str,
    dedupe_key: Optional[str] = None,
    door: Optional[Callable[[dict[str, Any]], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Start a new run that copies what the source run produced before `from_step`.

    A NORMAL RE-RUN GOES THROUGH THE START DOOR. Everything after `from_step` runs for real, so
    a re-run must meet every check a fresh start of the same version would: `door` is that
    check (the API's readiness and pre-check), called with the plan — version, input, mode —
    BEFORE anything is written. It raises to refuse, in its own words, and returns the
    pre-check record to keep on the run. A normal re-run without a door is refused: no caller
    can start one past the checks by forgetting them. A test re-run keeps the test path: every
    step that could spend is pinned, refused, or one its test explicitly allowed live.
    """
    wf = _workflows()
    from_step = str(from_step or "").strip()
    if not from_step:
        raise wf.WorkflowValidationError("from_step is required")
    source = connection.execute(
        text("SELECT * FROM workflow_runs WHERE id=:id AND project_id=:p"),
        {"id": run_id, "p": project_id},
    ).mappings().first()
    if source is None:
        raise wf.WorkflowNotFoundError("Workflow run not found.")
    if source["status"] not in wf.TERMINAL_RUN_STATES:
        raise wf.WorkflowConflictError(
            f"run {run_id} is {source['status']}; run it again from a step once it has "
            "finished or failed")
    spec = wf._run_spec(connection, run_id)
    steps = wf._step_map(spec)
    if from_step not in steps:
        raise wf.WorkflowValidationError(f"no step '{from_step}' in this run's workflow")
    latest = _latest_rows(connection, run_id)
    certain = wf._runs_before(spec).get(from_step, set())
    missing = []
    for step_id in sorted(certain):
        if not pinnable(steps[step_id]):
            continue
        row = latest.get(step_id)
        if row is None or row["status"] not in ("succeeded", "skipped"):
            missing.append(f"{step_id} ({row['status'] if row is not None else 'never ran'})")
    if missing:
        raise wf.WorkflowConflictError(
            f"cannot run again from '{from_step}': it depends on step(s) that did not complete "
            f"in run {run_id}: {', '.join(missing)}")

    pins: dict[str, Any] = {}
    for step_id in sorted(_upstream(spec, from_step)):
        row = latest.get(step_id)
        if not pinnable(steps[step_id]) or row is None or row["status"] != "succeeded":
            continue
        output = _json(row["output"], {}) or {}
        pins[step_id] = {
            "output": {k: v for k, v in output.items() if k not in PIN_META_KEYS},
            "source": {"kind": "rerun", "run_id": run_id, "step_run_id": row["id"],
                       "iteration": int(row["iteration"])},
        }
    mode = str(dict(source).get("mode") or NORMAL_MODE)
    allow_live: list[str] = []
    if mode == TEST_MODE:
        # The test's own pins on steps at and after the chosen one carry over, so re-running a
        # test from a step does not suddenly refuse what the first test had pinned.
        for step_id, pin in (_json(dict(source).get("pins"), {}) or {}).items():
            if step_id not in pins and step_id in steps:
                pins[step_id] = pin
        allow_live = list(_json(dict(source).get("allow_live"), []) or [])
    input_data = {k: v for k, v in (_json(source["input"], {}) or {}).items()
                  if k != wf.PRECHECK_KEY}
    rerun_of = {"run_id": run_id, "from_step": from_step}
    precheck_record: Optional[dict[str, Any]] = None
    if mode != TEST_MODE:
        if door is None:
            raise wf.WorkflowConflictError(
                "a re-run that runs for real must pass the start door's checks; none was given")
        precheck_record = door({"version_id": str(source["workflow_version_id"]),
                                "input": dict(input_data), "mode": mode,
                                "source_run_id": run_id, "from_step": from_step})
    run = wf.start_run(
        connection, project_id=project_id, actor=actor, input_data=input_data,
        dedupe_key=dedupe_key or f"rerun:{run_id}:{from_step}:{uuid.uuid4().hex[:12]}",
        workflow_version_id=str(source["workflow_version_id"]),
        mode=mode, pins=pins, allow_live=allow_live, rerun_of=rerun_of,
        precheck_record=precheck_record)
    if not run.get("duplicate"):
        emit_event(connection, project_id, RERUN_EVENT, actor, "workflow_run", run_id,
                   {"new_run_id": run["id"], "from_step": from_step,
                    "pinned_steps": sorted(pins)})
    return run


# ── the runtime's hooks ─────────────────────────────────────────────────────────────────────


def _pin_of(run: Any, step_id: str) -> Optional[dict[str, Any]]:
    pins = _json(dict(run).get("pins"), {}) or {}
    pin = pins.get(str(step_id))
    return pin if isinstance(pin, dict) and isinstance(pin.get("output"), dict) else None


def _allowed_live(run: Any, step_id: str) -> bool:
    return str(step_id) in set(_json(dict(run).get("allow_live"), []) or [])


def refusal_for(run: Any, template: Mapping[str, Any]) -> Optional[str]:
    """Why this test run may not run `template` for real — None when it may (or is not a test)."""
    if not is_test(run):
        return None
    step_id = str(template.get("id") or "")
    if _pin_of(run, step_id) is not None or _allowed_live(run, step_id):
        return None
    return effect_of(template)


def refuse(connection: Connection, run: Any, row: Any, why: str) -> None:
    """Fail a step a test run will not run for real, and stop the run there, loudly."""
    wf = _workflows()
    sentence = (f"a test run does not run this step for real: it {why}. Pin its output, or "
                "allow it to run live")
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs
            SET status='failed', last_error=:why, completed_at=clock_timestamp(),
                lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                output = COALESCE(output, '{}'::jsonb) || CAST(:out AS JSONB), updated_at=now()
            WHERE id=:id
            """
        ),
        {"id": row["id"], "why": sentence,
         "out": json.dumps({"reason": REFUSED_REASON, REFUSED_REASON: True, "effect": why})},
    )
    refused = wf._step_row(connection, row["workflow_run_id"], row["step_id"],
                           int(row["iteration"]))
    wf._emit_step_event(connection, run, REFUSED_EVENT, refused,
                        {"reason": REFUSED_REASON, "effect": why})
    wf._stop_run_at(connection, row["workflow_run_id"], refused)


def on_activate(connection: Connection, run: Any, template: Mapping[str, Any],
                row: Any) -> bool:
    """Called as a pending step becomes due. True when this module settled it (pinned or
    refused) and the ordinary activation must not happen."""
    wf = _workflows()
    pin = _pin_of(run, str(template["id"])) if pinnable(template) else None
    if pin is not None:
        output = dict(pin["output"])
        stored = {**output, "pinned": True, "pin": pin.get("source") or {}}
        connection.execute(
            text(
                """
                UPDATE workflow_step_runs
                SET status='succeeded', output=CAST(:output AS JSONB),
                    started_at=COALESCE(started_at, clock_timestamp()),
                    completed_at=clock_timestamp(), updated_at=now()
                WHERE id=:id AND status='pending'
                """
            ),
            {"id": row["id"], "output": json.dumps(stored)},
        )
        if output:
            connection.execute(
                text("UPDATE workflow_runs SET context=context || CAST(:o AS JSONB), "
                     "updated_at=now() WHERE id=:r"),
                {"r": run["id"], "o": json.dumps(output)},
            )
        settled = wf._step_row(connection, run["id"], row["step_id"], int(row["iteration"]))
        wf._emit_step_event(connection, run, PIN_EVENT, settled,
                            {"source": pin.get("source") or {}, "output": output})
        wf._after_success(connection, run["id"], str(row["step_id"]), int(row["iteration"]))
        return True
    if not is_test(run):
        return False
    why = refusal_for(run, template)
    if why:
        refuse(connection, run, row, why)
        return True
    if _allowed_live(run, str(template["id"])) and effect_of(template):
        wf._emit_step_event(connection, run, LIVE_EVENT, row,
                            {"effect": effect_of(template)})
    return False
