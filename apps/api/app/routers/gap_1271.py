"""Console and published-app traffic against the approved IP list (BOSSAPP-1271).

An empty list allows the request. A configured list returns 403 for any other
address and writes one audit row.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.identity.authn_support import resolve_client_ip
from boss_engine.services import ip_allowlist

from ..config import get_settings

router = APIRouter(prefix="/v1/ip-allowlist", tags=["ip-allowlist"])

_SURFACES = {"console": "console", "published-app": "published_app"}


class AllowlistBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[str] = Field(default_factory=list, max_length=256)
    actor: str = Field(default="operator", min_length=1, max_length=200)


def _engine():
    return get_engine(get_settings())


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for")
    return resolve_client_ip(peer, forwarded)


@router.get("")
@permit(P.SECURITY_POLICY_READ, scope=Scope.org())
def read_allowlist() -> dict:
    with begin(_engine()) as connection:
        entries = ip_allowlist.listed(connection)
    return {"entries": entries, "restricted": bool(entries)}


@router.put("")
@permit(P.SECURITY_POLICY_MANAGE, scope=Scope.org())
def replace_allowlist(body: AllowlistBody) -> dict:
    try:
        with begin(_engine()) as connection:
            entries = ip_allowlist.replace(
                connection, body.entries, actor=body.actor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"entries": entries, "restricted": bool(entries)}


@router.get("/gate/{surface}")
@permit(P.SECURITY_POLICY_READ, scope=Scope.org())
def gate(surface: str, request: Request) -> JSONResponse:
    mapped = _SURFACES.get(surface)
    if mapped is None:
        raise HTTPException(status_code=404, detail=f"Unknown surface '{surface}'.")
    with begin(_engine()) as connection:
        decision = ip_allowlist.admit(
            connection, surface=mapped, client_ip=_client_ip(request))
    status = int(decision.pop("status_code"))
    return JSONResponse(status_code=status, content=decision)
