"""Provider terms before a prompt runs (BOSSAPP-1264).

Region, log retention, and whether the provider may train are shown first.
A fact that was not recorded is unknown. The run route refuses until shown.
"""
from __future__ import annotations

from typing import Any, NoReturn, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from boss_engine.services.provider_terms import (
    DisclosureMissing,
    TermsNotShown,
    get_terms,
)

router = APIRouter(tags=["provider-terms"])


class RecordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    provider: str
    region: Optional[str] = None
    log_retention: Optional[str] = None
    may_train: Optional[str] = None


class DiscloseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    provider: str
    prompt: str


class DisclosureBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    disclosure_id: str


def _fail(error: Exception) -> NoReturn:
    if isinstance(error, TermsNotShown):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, DisclosureMissing):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, ValueError):
        raise HTTPException(status_code=422, detail=str(error)) from error
    raise error


@router.get("/v1/provider-terms")
@permit(P.MODEL_PROVIDER_READ, scope=Scope.project(query="project_id"))
def read_provider_terms(
    project_id: str = Query(...),
    provider: str = Query(...),
) -> dict[str, Any]:
    try:
        facts = get_terms().facts(project_id, provider)
    except ValueError as error:
        _fail(error)
    return {"project_id": project_id, "provider": provider, **facts}


@router.post("/v1/provider-terms/record")
@permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
def record_provider_terms(body: RecordBody) -> dict[str, Any]:
    passed = body.model_fields_set
    kwargs: dict[str, Any] = {}
    for name in ("region", "log_retention", "may_train"):
        if name in passed:
            kwargs[name] = getattr(body, name)
    try:
        facts = get_terms().record(body.project_id, body.provider, **kwargs)
    except ValueError as error:
        _fail(error)
    return {"project_id": body.project_id, "provider": body.provider, **facts}


@router.post("/v1/provider-terms/disclose")
@permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
def disclose_provider_terms(body: DiscloseBody) -> dict[str, Any]:
    try:
        return get_terms().disclose(body.project_id, body.provider, body.prompt)
    except ValueError as error:
        _fail(error)


@router.post("/v1/provider-terms/shown")
@permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
def mark_provider_terms_shown(body: DisclosureBody) -> dict[str, Any]:
    try:
        return get_terms().mark_shown(body.project_id, body.disclosure_id)
    except (TermsNotShown, DisclosureMissing, ValueError) as error:
        _fail(error)


@router.post("/v1/provider-terms/run")
@permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
def start_provider_terms_run(body: DisclosureBody) -> dict[str, Any]:
    try:
        return get_terms().start_run(body.project_id, body.disclosure_id)
    except (TermsNotShown, DisclosureMissing, ValueError) as error:
        _fail(error)
