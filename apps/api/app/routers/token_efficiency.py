"""Token efficiency / consumption control API — ``/v1/token-efficiency``.

Thin routes over ``boss_engine.services.token_efficiency``. Analytics are
read-only over ``llm_calls``; policies are the only writes. No external model calls.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError

from boss_engine.db import begin, get_engine
from boss_engine.services import token_efficiency as te

router = APIRouter(prefix="/v1/token-efficiency", tags=["token-efficiency"])


class MessageIn(BaseModel):
    role: str = "user"
    content: str = ""


class EstimateRequest(BaseModel):
    messages: list[MessageIn] = Field(default_factory=list)
    max_context_tokens: Optional[int] = Field(default=None, ge=1, le=2_000_000)


class PolicyIn(BaseModel):
    stage: str = te.PROJECT_DEFAULT_STAGE
    max_prompt_tokens: Optional[int] = Field(default=None, ge=0)
    max_completion_tokens: Optional[int] = Field(default=None, ge=0)
    max_total_tokens_per_call: Optional[int] = Field(default=None, ge=0)
    max_tokens_per_day: Optional[int] = Field(default=None, ge=0)
    max_cost_usd_micros_per_day: Optional[int] = Field(default=None, ge=0)
    alert_at_pct: Optional[float] = Field(default=None, gt=0.0, le=1.0)
    preferred_model_class: Optional[str] = None
    enabled: bool = True


class PoliciesPutRequest(BaseModel):
    project_id: str
    policies: list[PolicyIn] = Field(min_length=1)


class RecommendationsRequest(BaseModel):
    project_id: str
    profile_name: Optional[str] = None


@router.get("/scorecard")
def get_scorecard(
    project_id: str = Query(...),
    window_hours: Optional[int] = Query(None, ge=1, le=24 * 90),
) -> dict[str, Any]:
    """Efficiency scorecard from llm_calls (cache ratio, failed burn, avgs)."""
    try:
        with begin(get_engine()) as connection:
            te.ensure_tables(connection)
            return te.build_scorecard(
                connection, project_id, window_hours=window_hours
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ProgrammingError as error:
        if te.is_missing_relation(error):
            return te.empty_scorecard(project_id, window_hours, schema_ready=False)
        raise


@router.get("/policies")
def get_policies(project_id: str = Query(...)) -> dict[str, Any]:
    """Token budget policies per project/stage (defaults + stored overrides)."""
    try:
        with begin(get_engine()) as connection:
            te.ensure_tables(connection)
            return te.list_policies(connection, project_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ProgrammingError as error:
        if te.is_missing_relation(error):
            return te.unconfigured_policies(project_id)
        raise


@router.put("/policies")
def put_policies(payload: PoliciesPutRequest) -> dict[str, Any]:
    """Upsert token budget policies for a project."""
    try:
        with begin(get_engine()) as connection:
            te.ensure_tables(connection)
            return te.upsert_policies(
                connection,
                project_id=payload.project_id,
                # exclude_unset: a key the caller did not send must reach the engine as absent,
                # not as None, or the merge in upsert_policies reads it as "clear this cap"
                # (the Govern cap form wiped the daily caps on '*' that way, 2026-10-02).
                policies=[
                    {"stage": p.stage, **p.model_dump(exclude_unset=True)}
                    for p in payload.policies
                ],
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/estimate")
def post_estimate(payload: EstimateRequest) -> dict[str, Any]:
    """Estimate tokens for messages (chars/4) + compress suggestions. No model call."""
    try:
        return te.estimate_messages(
            [m.model_dump() for m in payload.messages],
            max_context_tokens=payload.max_context_tokens,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/recommendations")
def post_recommendations(payload: RecommendationsRequest) -> dict[str, Any]:
    """Deterministic recommendations extending cost_insights with token rules."""
    try:
        with begin(get_engine()) as connection:
            te.ensure_tables(connection)
            return te.build_recommendations(
                connection,
                payload.project_id,
                profile_name=payload.profile_name,
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ProgrammingError as error:
        if te.is_missing_relation(error):
            return {
                "project_id": payload.project_id,
                "total_spend_usd_micros": 0,
                "scorecard_grades": {
                    "cache_ratio": "n/a",
                    "failed_burn": "n/a",
                    "avg_tokens": "n/a",
                },
                "profile": payload.profile_name or "balanced",
                "model_routing": [],
                "recommendations": [],
                "count": 0,
                "schema_ready": False,
            }
        raise


@router.get("/consumption")
def get_consumption(
    project_id: str = Query(...),
    window_hours: int = Query(24, ge=1, le=24 * 90),
) -> dict[str, Any]:
    """Rolling-window token/cost consumption + policy pressure alerts."""
    try:
        with begin(get_engine()) as connection:
            te.ensure_tables(connection)
            return te.build_consumption(
                connection, project_id, window_hours=window_hours
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ProgrammingError as error:
        if te.is_missing_relation(error):
            empty = te.empty_scorecard(project_id, window_hours, schema_ready=False)
            return {
                "project_id": project_id,
                "window_hours": window_hours,
                "calls": 0,
                "total_tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_read_tokens": 0,
                "cost_usd_micros": 0,
                "failed_burn_usd_micros": 0,
                "cache_ratio": None,
                "policy": {
                    "stage": te.PROJECT_DEFAULT_STAGE,
                    "token_utilization": None,
                    "cost_utilization": None,
                    "enabled": True,
                },
                "alerts": [],
                "by_stage": [],
                "series": [],
                "schema_ready": False,
                "configured": False,
                "grades": empty["grades"],
            }
        raise


@router.get("/routing")
def get_routing(
    profile_name: Optional[str] = Query(None),
) -> dict[str, Any]:
    """Stage → preferred model class (low_cost|strong) from pipeline profiles."""
    return te.suggest_model_routing(profile_name=profile_name)


__all__ = ["router"]
