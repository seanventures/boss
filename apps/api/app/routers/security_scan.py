"""HTTP surface for OSS security vulnerability scanning.

Prefix: ``/v1/security-scan``

  GET  /scanners              — catalog (stars_note / website / availability)
  POST /run                   — gated until project workspace and receipt preflight exist
  GET  /runs?project_id=      — list recent runs for a project
  GET  /runs/{run_id}         — fetch one run with findings
  POST /runs/{run_id}/gate    — evaluate go/no-go (critical findings fail by default)

Domain logic lives in ``boss_engine.services.security_scan``.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.services import security_scan as svc

from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(prefix="/v1/security-scan", tags=["security-scan"])


class SecurityScanRunRequest(BaseModel):
    project_id: str
    actor: str = "system"
    path: Optional[str] = Field(
        default=None,
        description="Reserved for a future project-scoped workspace scan.",
    )
    scanners: Optional[list[str]] = Field(
        default=None,
        description=(
            "Scanner ids to run. Defaults to semgrep, trivy, osv-scanner, gitleaks. "
            "checkov is optional. Unknown ids fail closed. snyk is never runnable."
        ),
    )
    simulated: bool = Field(
        default=False,
        description="Reserved fixture mode; API execution remains gated.",
    )
    timeout_s: int = Field(default=300, ge=5, le=3600)
    fail_on_critical: bool = Field(
        default=True,
        description="When true, critical findings mark the run status failed and gate no-go.",
    )


class SecurityScanGateRequest(BaseModel):
    fail_on: Optional[list[str]] = Field(
        default=None,
        description="Severities that block go. Default: ['critical'].",
    )


@router.get("/scanners")
def list_scanners(include_planned: bool = Query(default=True)) -> dict:
    """Catalog of market-validated OSS scanners (+ Snyk BYO note)."""
    items = svc.list_scanners(include_planned=include_planned)
    return {
        "items": items,
        "count": len(items),
        "supported": sorted(svc.SUPPORTED_SCANNERS),
        "default_scanners": list(svc.DEFAULT_SCANNERS),
        "notes": {
            "snyk": (
                "Snyk commercial API is never called. Listed as planned bring-your-own only."
            ),
            "simulated": (
                "Fixture findings are available to internal tests; API execution is gated."
            ),
            "execution": (
                "Run is unavailable until project workspace confinement and a durable "
                "preflight receipt are implemented."
            ),
            "checkov": (
                "Also used by the governed IaC verify lane; available here as an optional "
                "project-wide scanner kind."
            ),
        },
        "execution_available": False,
    }


@router.post("/run")
def run_scan(payload: SecurityScanRunRequest) -> dict:
    """Authorize the project request, then fail closed before any scanner effect.

    A caller-controlled path (including an omitted path that defaults to the API's
    process CWD) is not a project workspace. The service currently records its
    receipt after invoking binaries and tolerates receipt failure. Neither is a
    safe boundary for an API-triggered host scan.
    """
    # Import at request time because main mounts this router after defining the
    # request principal context and project permission guard.
    from ..main import (
        current_api_principal,
        current_transport_authenticated,
        require_project_actor,
    )

    if not current_transport_authenticated.get() or not current_api_principal.get():
        raise HTTPException(status_code=401, detail="Authenticated project member required.")
    settings = get_settings()
    with begin(get_engine(settings)) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "providerops_manage"
        )
    raise HTTPException(
        status_code=409,
        detail=(
            "Security scan execution is unavailable until scans are confined to a "
            "configured project workspace and a durable receipt is reserved before "
            "any scanner starts."
        ),
    )


@router.get("/latest")
def latest_scan(project_id: str = Query(..., description="Project scope")) -> dict:
    """Most recent scan for a project. Empty is honest 200 not_configured, not 404."""
    return svc.latest_run(project_id)


@router.get("/runs")
def list_runs(
    project_id: str = Query(..., description="Project scope"),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    items = svc.list_runs(project_id, limit=limit)
    last = svc.last_scan_summary(project_id)
    return {"items": items, "count": len(items), "last_summary": last}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    rec = svc.get_run(run_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"scan run not found: {run_id}")
    return rec


@router.post("/runs/{run_id}/gate")
def evaluate_gate(run_id: str, payload: Optional[SecurityScanGateRequest] = None) -> dict:
    """Evaluate go/no-go for a prior run. Critical findings fail closed by default."""
    body = payload or SecurityScanGateRequest()
    try:
        return svc.evaluate_gate(run_id, fail_on=body.fail_on)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
