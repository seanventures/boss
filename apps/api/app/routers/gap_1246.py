"""Builder turn meter (BOSSAPP-1246).

GET  /v1/builder/turns/{turn_id}                 elapsed work and settled receipts
POST /v1/builder/turns                           open a turn, no spend
POST /v1/builder/turns/{turn_id}/credit          pause for a credit check-in
POST /v1/builder/turns/{turn_id}/credit/answer   approve reserves once; decline does not

The reconciler mounts this router. A refused reservation is 409. A retryable
refusal is 429. Both bodies are the meter, including the error.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["builder-turn-meter"])


class OpenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    turn_id: Optional[str] = Field(default=None, max_length=200)


class CreditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    request_key: str = Field(min_length=1, max_length=120)
    amount_usd_micros: int = Field(gt=0)


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    request_key: str = Field(min_length=1, max_length=120)
    decision: str = Field(pattern="^(approve|decline)$")


def _engine() -> Any:
    from boss_engine.config import get_settings
    from boss_engine.db import get_engine

    return get_engine(get_settings())


def _run(fn) -> Any:
    from boss_engine.db import begin
    from boss_engine.services.turn_meter import TurnMeterError

    try:
        with begin(_engine()) as connection:
            return fn(connection)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TurnMeterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _respond(view: dict[str, Any], *, created: bool = False) -> Any:
    code = 201 if created else 200
    if view.get("retry"):
        code = 429
    elif (view.get("checkin") or {}).get("status") == "failed":
        code = 409
    if code in (200, 201):
        return JSONResponse(status_code=code, content=jsonable_encoder(view))
    return JSONResponse(status_code=code, content=jsonable_encoder(view))


@router.get("/v1/builder/turns/{turn_id}")
@permit(P.BUDGET_READ, scope=Scope.project(query="project_id"))
def get_turn_meter(turn_id: str, project_id: str) -> Any:
    from boss_engine.services.turn_meter import read_meter

    return _respond(_run(lambda c: read_meter(c, project_id, turn_id)))


@router.post("/v1/builder/turns")
@permit(P.BUDGET_MANAGE, scope=Scope.project(body="project_id"))
def post_turn(body: OpenBody) -> Any:
    from boss_engine.services.turn_meter import open_turn

    return _respond(
        _run(lambda c: open_turn(c, body.project_id, turn_id=body.turn_id)),
        created=True,
    )


@router.post("/v1/builder/turns/{turn_id}/credit")
@permit(P.BUDGET_MANAGE, scope=Scope.project(body="project_id"))
def post_credit(turn_id: str, body: CreditBody) -> Any:
    from boss_engine.services.turn_meter import request_credit

    return _respond(_run(lambda c: request_credit(
        c, body.project_id, turn_id, body.request_key, body.amount_usd_micros)))


@router.post("/v1/builder/turns/{turn_id}/credit/answer")
@permit(P.BUDGET_MANAGE, scope=Scope.project(body="project_id"))
def post_answer(turn_id: str, body: AnswerBody) -> Any:
    from boss_engine.services.turn_meter import answer_credit

    return _respond(_run(lambda c: answer_credit(
        c, body.project_id, turn_id, body.request_key, body.decision)))
