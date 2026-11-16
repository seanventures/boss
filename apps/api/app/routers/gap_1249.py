"""Save a SIEM URL and forward new security-audit rows to it (BOSSAPP-1249).

The reconciler mounts ``router``. The hash chain is not written here.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit
from boss_engine.identity import siem_forward
from boss_engine.identity.principal import AuthContext

router = APIRouter(tags=["security"])


def _context(request: Request) -> Optional[AuthContext]:
    """The verified caller. Never taken from the body."""
    from apps.api.app import identity_guard
    return identity_guard.request_auth(request)


def _refuse(status: int, error: str, detail: Optional[str] = None) -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=status, content=body)


@router.put("/v1/security/siem")
@permit(P.AUDIT_EXPORT, scope=Scope.org())
def save_siem(request: Request, payload: Optional[dict] = Body(default=None)):
    ctx = _context(request)
    if ctx is None:
        return _refuse(401, "unauthenticated")
    url = payload.get("url") if isinstance(payload, dict) else None
    if not isinstance(url, str) or not url.strip():
        return _refuse(422, "url_required")
    try:
        with begin() as db:
            return siem_forward.save_destination(ctx.principal.org_id, url, db=db)
    except siem_forward.DestinationRefused as exc:
        return _refuse(422, "invalid_url", str(exc))


@router.delete("/v1/security/siem")
@permit(P.AUDIT_EXPORT, scope=Scope.org())
def clear_siem(request: Request):
    ctx = _context(request)
    if ctx is None:
        return _refuse(401, "unauthenticated")
    with begin() as db:
        siem_forward.clear_destination(ctx.principal.org_id, db=db)
    return {"destination": None}


@router.get("/v1/security/siem")
@permit(P.AUDIT_READ, scope=Scope.org())
def show_siem(request: Request):
    ctx = _context(request)
    if ctx is None:
        return _refuse(401, "unauthenticated")
    with begin() as db:
        org_id = ctx.principal.org_id
        return {
            "destination": siem_forward.get_destination(org_id, db=db),
            "failures": siem_forward.failures(org_id, db=db),
        }


@router.post("/v1/security/siem/forward")
@permit(P.AUDIT_EXPORT, scope=Scope.org())
def forward_siem(request: Request):
    ctx = _context(request)
    if ctx is None:
        return _refuse(401, "unauthenticated")
    with begin() as db:
        return siem_forward.forward_new(ctx.principal.org_id, db=db)
