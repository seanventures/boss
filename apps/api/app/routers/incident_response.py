"""HTTP surface for incident response (engine services/incident_response.py, incident_signals.py).

* ``POST /v1/apps/{target_id}/signals`` — an app reports a suspected incident (PUSH). Public by
  route, authenticated per app: HMAC over ``<timestamp>.<raw body>`` with the app's own
  BOSS_SIGNALS_TOKEN. Creates or links an incident; never starts a change.
* ``GET /v1/reliability/incidents/{id}/report?format=json|md`` — the timeline, root cause and
  cost of a response, exportable.
* ``GET /v1/reliability/incidents/{id}/response`` — what the console's incident card shows: the
  app, the serving release, the evidence, the reports, the response run and its verification.
* ``POST /v1/reliability/incidents/{id}/tell-reporters`` — a person sends the incident's state
  (the root cause and fix, once resolved) to the apps that reported it, again.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import text

from boss_engine.identity import P, PublicReason, Scope, permit, public
from boss_engine.services import incident_response, incident_signals

from ..db import begin, get_engine

router = APIRouter(tags=["reliability"])

_REFUSAL_STATUS = {"not_declared": 403, "bad_signature": 401, "body_too_large": 413,
                   "invalid_json": 400, "invalid_shape": 400, "rate_limited": 429}


@router.post("/v1/apps/{target_id}/signals", status_code=202)
@public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=incident_signals.receive)
async def receive_app_signal(target_id: str, request: Request):
    """One suspected incident from the app itself. 202 accepted (with the incident it created
    or joined), 200 duplicate, 404 unknown app; every refusal is said."""
    from boss_engine.config import get_settings

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > incident_signals.MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"status": "refused",
                                                      "reason": "body_too_large"})
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    settings = get_settings()
    with begin(get_engine()) as connection:
        result = incident_signals.receive(connection, settings, target_id, raw_body, headers)
    if result["status"] == "not_found":
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    if result["status"] == "refused":
        return JSONResponse(status_code=_REFUSAL_STATUS.get(result["reason"], 400),
                            content={"status": "refused", "reason": result["reason"],
                                     "detail": result.get("detail")})
    if result.get("action") in ("linked", "opened"):
        # Told after the commit, so the app is never told about a link that rolled back.
        with begin(get_engine()) as connection:
            target = connection.execute(
                text(
                    "SELECT project_id FROM deployment_targets WHERE id = :t"),
                {"t": target_id}).scalar()
            incident_signals.notify_reporters(connection, settings, str(target),
                                              result["incident_id"], None,
                                              only_target=target_id)
    return JSONResponse(status_code=200 if result.get("action") == "duplicate" else 202,
                        content={"status": result.get("action"),
                                 "incident_id": result.get("incident_id")})


@router.get("/v1/reliability/incidents/{incident_id}/report")
@permit(P.RELIABILITY_READ, scope=Scope.project(query="project_id"))
def incident_report(incident_id: str, project_id: str = Query(...),
                    format: str = Query("json")) -> Any:
    """The report as stored by the response's last step, else assembled now (not stored)."""
    with begin(get_engine()) as connection:
        stored = connection.execute(
            text(
                "SELECT payload FROM sre_postmortems WHERE incident_id = :i AND project_id = :p "
                "AND payload->>'kind' = 'incident_report'"),
            {"i": incident_id, "p": project_id}).scalar()
        if stored is None:
            try:
                stored = incident_response.report(connection, project_id, incident_id,
                                                  persist=False)
            except LookupError:
                raise HTTPException(status_code=404, detail="incident not found") from None
    if format == "md":
        return PlainTextResponse(stored.get("markdown") or "", media_type="text/markdown")
    return JSONResponse(content=jsonable_encoder(stored))


@router.get("/v1/reliability/incidents/{incident_id}/response")
@permit(P.RELIABILITY_READ, scope=Scope.project(query="project_id"))
def incident_response_view(incident_id: str, project_id: str = Query(...)) -> Any:
    with begin(get_engine()) as connection:
        view = incident_response.response_view(connection, project_id, incident_id)
    if view is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return JSONResponse(content=jsonable_encoder(view))


class TellReportersRequest(BaseModel):
    project_id: str


@router.post("/v1/reliability/incidents/{incident_id}/tell-reporters")
@permit(P.RELIABILITY_RESPOND, scope=Scope.project(body="project_id"))
def tell_reporters(incident_id: str, payload: TellReportersRequest) -> Any:
    """Send the incident's state to its reporters once more; answers what each one said."""
    from boss_engine.config import get_settings

    with begin(get_engine()) as connection:
        found = connection.execute(
            text("SELECT 1 FROM incidents WHERE id = :i AND project_id = :p"),
            {"i": incident_id, "p": payload.project_id}).first()
        if not found:
            raise HTTPException(status_code=404, detail="incident not found")
        told = incident_signals.tell_reporters_again(connection, get_settings(),
                                                     payload.project_id, incident_id)
    if not told:
        return JSONResponse(status_code=409, content={
            "detail": "No app that reported this incident declares a callback BOSS can reach."})
    return JSONResponse(content=jsonable_encoder({"told": told}))


__all__ = ["router"]
