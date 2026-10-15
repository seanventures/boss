"""HTTP surface for test runs with pinned data and re-running a run from a step (#512).

Thin: every rule lives in ``boss_engine.services.workflow_test_runs``.

* ``POST /v1/delivery-workflows/test-runs`` — start a TEST run of a saved workflow version, with
  pins (``{step_id: output}``) and the steps allowed to run live (``allow_live``).
* ``GET  /v1/delivery-workflows/test-runs`` — the project's test runs (they are left out of the
  ordinary run lists).
* ``POST /v1/delivery-workflows/runs/{run_id}/rerun`` — start a new run from one step of a
  finished or failed run, every earlier step pinned to what that run produced.

Tenant scope: each route is permitted at the project named in the request, the actor must be a
member allowed to execute workflows there, and the engine only ever reads a version or run
inside that project — another project's run or version is a 404.
"""
from __future__ import annotations

from typing import Any, Optional

from boss_engine.identity import P, Scope, permit
from boss_engine.services import workflow_test_runs as test_runs
from boss_engine.services import workflows as workflow_runtime
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..db import begin, get_engine

router = APIRouter(tags=["workflows"])


class TestRunBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    #: The saved version to test. Or name the workflow by `workflow_key` (and `version`;
    #: without one, the version that runs now).
    workflow_version_id: Optional[str] = None
    workflow_key: Optional[str] = None
    version: Optional[int] = None
    input: dict[str, Any] = Field(default_factory=dict)
    #: {step_id: output object}. A pinned step does not run; it is settled with this output.
    pins: dict[str, Any] = Field(default_factory=dict)
    #: Steps that may run FOR REAL — with every approval, budget reservation and receipt a
    #: normal run needs. Any other step that could spend or reach outside BOSS is refused.
    allow_live: list[str] = Field(default_factory=list)
    dedupe_key: Optional[str] = None


class RerunBody(BaseModel):
    project_id: str
    from_step: str
    actor: Optional[str] = None
    dedupe_key: Optional[str] = None
    #: The start door's own answers, as on POST /v1/delivery-workflows/start: what is missing
    #: and started anyway, the pre-check findings accepted or the steps skipped (with a reason),
    #: and the unsupported-stack override. A normal re-run is checked exactly as a start is.
    start_anyway: list[str] = Field(default_factory=list)
    accept: list[str] = Field(default_factory=list)
    skip_steps: list[Any] = Field(default_factory=list)
    precheck_reason: Optional[str] = None
    allow_unsupported_stack: Optional[bool] = None


def _actor(connection, project_id: str, claimed: Optional[str]) -> str:
    from apps.api.app.main import _delivery_workflow_actor, require_project_actor

    actor = _delivery_workflow_actor(actor=claimed)
    require_project_actor(connection, project_id, actor, "workflow_execute")
    return actor


def _raise(error: workflow_runtime.WorkflowError) -> None:
    if isinstance(error, workflow_runtime.WorkflowNotFoundError):
        status = 404
    elif isinstance(error, (workflow_runtime.WorkflowConflictError,
                            workflow_runtime.WorkflowBindingBlockedError)):
        status = 409
    else:
        status = 422
    raise HTTPException(status_code=status, detail=str(error)) from error


def _version_id(connection, payload: TestRunBody) -> str:
    """The product workflow version to test, read inside the request's project only."""
    row = connection.execute(
        text(
            """
            SELECT v.id FROM workflow_versions v
            JOIN workflow_definitions d ON d.id = v.workflow_definition_id
            WHERE d.project_id = :p AND COALESCE(d.namespace, 'system') = 'product'
              AND (
                (CAST(:vid AS TEXT) IS NOT NULL AND v.id = CAST(:vid AS TEXT))
                OR (CAST(:vid AS TEXT) IS NULL AND d.workflow_key = :key AND (
                      (CAST(:ver AS INTEGER) IS NOT NULL AND v.version = CAST(:ver AS INTEGER))
                   OR (CAST(:ver AS INTEGER) IS NULL AND v.id = d.active_version_id)))
              )
            """
        ),
        {"p": payload.project_id, "vid": payload.workflow_version_id or None,
         "key": payload.workflow_key or "", "ver": payload.version},
    ).scalar()
    if not row:
        raise HTTPException(status_code=404, detail="no saved version of that workflow here")
    return str(row)


@router.post("/v1/delivery-workflows/test-runs", status_code=201)
@permit(P.WORKFLOW_RUN_START, scope=Scope.project(body="project_id"))
def start_test_run(payload: TestRunBody) -> dict:
    """Run a saved workflow as a TEST: pinned steps return their pinned output, and a step that
    could spend or reach outside BOSS is refused unless named in `allow_live`."""
    if not payload.workflow_version_id and not payload.workflow_key:
        raise HTTPException(status_code=400,
                            detail="workflow_version_id or workflow_key is required")
    with begin(get_engine()) as connection:
        actor = _actor(connection, payload.project_id, payload.actor)
        version_id = _version_id(connection, payload)
        try:
            return test_runs.start_test_run(
                connection, project_id=payload.project_id, actor=actor,
                workflow_version_id=version_id, input_data=payload.input, pins=payload.pins,
                allow_live=payload.allow_live, dedupe_key=payload.dedupe_key)
        except workflow_runtime.WorkflowError as error:
            _raise(error)


@router.get("/v1/delivery-workflows/test-runs")
@permit(P.WORKFLOW_RUN_READ, scope=Scope.project(query="project_id"))
def list_test_runs(project_id: str = Query(...), limit: int = 25) -> dict:
    """The project's test runs, newest first — the ordinary lists leave them out."""
    with begin(get_engine()) as connection:
        try:
            items = workflow_runtime.list_runs(connection, project_id,
                                               limit=max(1, min(200, limit)), only_test=True)
        except workflow_runtime.WorkflowError as error:
            _raise(error)
    return {"project_id": project_id, "items": items}


@router.post("/v1/delivery-workflows/runs/{run_id}/rerun", status_code=201)
@permit(P.WORKFLOW_RUN_START, scope=Scope.project(body="project_id"))
def rerun_from_step(run_id: str, payload: RerunBody) -> dict:
    """Run again from one step: a new run, every earlier step pinned to what this run produced
    (with provenance), recorded with `rerun_of`. Refused when the step depends on one that did
    not complete here."""
    with begin(get_engine()) as connection:
        actor = _actor(connection, payload.project_id, payload.actor)
        checked: dict[str, Any] = {}
        try:
            run = test_runs.rerun_from_step(
                connection, project_id=payload.project_id, run_id=run_id,
                from_step=payload.from_step, actor=actor, dedupe_key=payload.dedupe_key,
                door=lambda plan: _start_door(connection, payload, actor, plan, checked))
        except workflow_runtime.WorkflowError as error:
            _raise(error)
        if checked and not run.get("duplicate"):
            from boss_engine.services.workflow_precheck import record_on_run
            from boss_engine.services.workflow_readiness import record_started_anyway

            # As a start records them: the pre-check first, then what was started anyway.
            record_on_run(connection, payload.project_id, actor, run["id"],
                          checked.get("precheck"))
            record_started_anyway(connection, payload.project_id, actor, run["id"],
                                  checked.get("readiness") or {})
        return run


def _start_door(connection, payload: RerunBody, actor: str, plan: dict,
                checked: dict) -> Optional[dict]:
    """THE START DOOR, for a re-run that runs for real: the same checks, in the same order, as
    POST /v1/delivery-workflows/start makes for a fresh start of this version and input — a
    deployment target this actor may use, a stack BOSS can build, readiness, the pre-check.
    Each refuses with its own HTTP status and words, before any run is written. What the
    source run was started anyway past carries over, as it does for "Run this workflow again".
    """
    from apps.api.app.main import (
        _authorize_delivery_target_input, _delivery_readiness_or_409, _precheck_or_409,
    )
    from boss_engine.services import stacks as _stacks
    from boss_engine.services.workflow_readiness import accepted_by_run

    project_id, version_id = payload.project_id, plan["version_id"]
    run_input = dict(plan["input"])
    _authorize_delivery_target_input(connection, project_id, actor, run_input)
    accepted = sorted({*payload.start_anyway,
                       *accepted_by_run(connection, project_id, plan["source_run_id"])})
    try:
        _stacks.refuse_unsupported_start(connection, version_id=version_id, run_input=run_input,
                                         allow=bool(payload.allow_unsupported_stack))
    except _stacks.StackUnsupported as refused:
        raise HTTPException(status_code=409, detail=refused.detail()) from refused
    checked["readiness"] = _delivery_readiness_or_409(
        connection, project_id, version_id=version_id, work_class="", run_input=run_input,
        start_anyway=accepted)
    checked["precheck"] = _precheck_or_409(
        connection, project_id, version_id=version_id, work_class="", run_input=run_input,
        actor=actor, accept=payload.accept, skip_steps=payload.skip_steps,
        reason=payload.precheck_reason, start_anyway=accepted)
    return checked["precheck"]
