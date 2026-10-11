"""A typed, deterministic preview for the first planning/delivery workflow."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from boss_engine.db import begin
from boss_engine.identity import P, Scope, lineage, permit
from boss_engine.services import work_agreement
from .. import identity_guard


class AgreementBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    directive: str = Field(min_length=1, max_length=20_000)
    actor: str | None = None
    start_anyway: list[str] = Field(default_factory=list, max_length=50)


class AgreementInput(BaseModel):
    title: str
    directive: str


class AgreementRepository(BaseModel):
    full_name: str
    base_branch: str | None
    base_revision: str | None
    source: str | None
    binding_id: str | None
    selection: Literal["supplied", "current_project_target"]


class AgreementStep(BaseModel):
    id: str
    label: str
    does: str | None
    capability: str | None
    runs: str | None = None
    agent: str | None
    approval: str | None
    approval_note: str | None
    produces: list[str] | None
    next: list[str]


class CostHistory(BaseModel):
    runs: int
    low_usd: float
    high_usd: float
    metered: bool


class AgreementFinancial(BaseModel):
    estimate_usd_micros: int | None
    reservation_usd_micros: int | None
    authorized_limit_usd_micros: int | None
    history: CostHistory | None
    history_basis: str


class AgreementResponse(BaseModel):
    schema_version: Literal[1]
    action: Literal["delivery_workflow.start"]
    project_id: str
    content_hash: str
    binding_token: str
    start_intent_key: str
    expires_at_unix: int
    workflow_key: str
    workflow_name: str
    workflow_version_id: str
    workflow_version: int
    entry_step: str
    input: AgreementInput
    start_anyway: list[str]
    repository: AgreementRepository | None
    approved_spec: None
    acceptance_criteria: list[str]
    test_commands: list[str]
    selected_provider: None
    selected_account: None
    steps: list[AgreementStep]
    results: list[str]
    stop_conditions: list[str]
    financial: AgreementFinancial
    bounds: list[str]
    unknowns: list[str]


def principal_binding(actor: str) -> str:
    ctx = identity_guard.REQUEST_IDENTITY.get() or lineage.current_context()
    return ctx.principal.id if ctx is not None else f"legacy:{actor}"


def build_work_agreement_router(*, engine, settings, require_reader, actor_of) -> APIRouter:
    router = APIRouter(tags=["workflows"])

    @router.post("/v1/delivery-workflows/agreement", response_model=AgreementResponse)
    @permit(P.WORKFLOW_READ, scope=Scope.project(body="project_id"))
    async def preview_agreement(payload: AgreementBody, request: Request):
        actor = actor_of(payload.model_dump(exclude_none=True))
        ctx = getattr(request.state, "identity", None) or identity_guard.REQUEST_IDENTITY.get()
        # The central guard replaces claimed actors in parsed JSON for legacy attribution.
        # This new confirmation boundary must also refuse an explicit spoof in the wire body.
        supplied_actor = json.loads(await request.body()).get("actor")
        if ctx is not None and supplied_actor and supplied_actor != identity_guard.actor_for(ctx):
            raise HTTPException(403, "The agreement must be attributed to the authenticated principal.")
        principal = ctx.principal.id if ctx is not None else principal_binding(actor)

        def read():
            with begin(engine) as connection:
                require_reader(connection, payload.project_id)
                try:
                    return work_agreement.preview(connection, settings(), project_id=payload.project_id,
                                                  principal=principal, directive=payload.directive,
                                                  start_anyway=payload.start_anyway)
                except work_agreement.AgreementConflict as exc:
                    raise HTTPException(409, str(exc)) from exc
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc

        return await run_in_threadpool(read)

    return router
