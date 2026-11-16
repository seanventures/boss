"""A person's primary agent in one company. The reconciler mounts `router`."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from boss_engine.db import begin
from boss_engine.identity import authenticated
from boss_engine.identity.authn import AuthnError
from boss_engine.services.primary_agent import (
    PrimaryAgentError,
    primary_agent,
    resolve_member,
    set_primary,
)

from .identity_auth import current_context, refusal

router = APIRouter(tags=["primary-agent"])

_STATUS = {
    "missing": 422,
    "not_a_member": 404,
    "agent_not_in_company": 404,
    "authority_changed": 500,
    "more_than_one": 500,
}


class SetPrimaryBody(BaseModel):
    agent_id: str


def _failure(exc: PrimaryAgentError) -> JSONResponse:
    return JSONResponse(
        status_code=_STATUS.get(exc.code, 400),
        content={"error": exc.code},
        headers={"Cache-Control": "no-store"},
    )


def _caller(connection, ctx, org_id: str) -> str:
    if org_id != ctx.principal.org_id:
        raise PrimaryAgentError("not_a_member")
    legacy = tuple(getattr(ctx.principal, "legacy_member_ids", ()) or ())
    return resolve_member(
        connection, org_id=org_id, candidates=[*legacy, ctx.principal.id]
    )


@router.get("/v1/orgs/{org_id}/primary-agent")
@authenticated(
    self_only=True,
    people_only=True,
    note="the caller's own primary agent in this company",
)
def read_primary(org_id: str, request: Request) -> JSONResponse:
    from .identity_me import need_person_session

    try:
        with begin() as connection:
            ctx = current_context(request, connection)
            refused = need_person_session(ctx)
            if refused is not None:
                return refused
            person_id = _caller(connection, ctx, org_id)
            agent_id: Optional[str] = primary_agent(
                connection, org_id=org_id, person_id=person_id
            )
    except AuthnError as exc:
        return refusal(exc)
    except PrimaryAgentError as exc:
        return _failure(exc)
    return JSONResponse(
        {"org_id": org_id, "person_id": person_id, "agent_id": agent_id},
        headers={"Cache-Control": "no-store"},
    )


@router.put("/v1/orgs/{org_id}/primary-agent")
@authenticated(
    self_only=True,
    people_only=True,
    note="sets only the caller's primary agent; authority is untouched",
)
def put_primary(org_id: str, body: SetPrimaryBody, request: Request) -> JSONResponse:
    from .identity_me import need_person_session

    try:
        with begin() as connection:
            ctx = current_context(request, connection)
            refused = need_person_session(ctx)
            if refused is not None:
                return refused
            person_id = _caller(connection, ctx, org_id)
            saved = set_primary(
                connection, org_id=org_id, person_id=person_id, agent_id=body.agent_id
            )
    except AuthnError as exc:
        return refusal(exc)
    except PrimaryAgentError as exc:
        return _failure(exc)
    return JSONResponse(saved, headers={"Cache-Control": "no-store"})
