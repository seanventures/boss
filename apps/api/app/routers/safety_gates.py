"""Safety gates for live demos and GA process (BOSS-324).

Prefix: /v1/safety
  GET  /branch-protection          — verify default-branch protection / live_demo_ready
  POST /branch-protection/verify   — same with body overrides
  POST /branch-protection/simulate — store simulated checklist (tests/rehearsal)
  GET  /branch-protection/checklist — dry-run static checklist
"""

from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.config import get_settings
from boss_engine.services.branch_protection_gate import (
    BranchProtectionGateService,
    REQUIRED_CHECKS,
    all_required_true_checks,
    dry_run_checklist,
    store_simulated_checklist,
)

router = APIRouter(prefix="/v1/safety", tags=["safety-gates"])


def _svc() -> BranchProtectionGateService:
    return BranchProtectionGateService(get_settings())


class VerifyRequest(BaseModel):
    owner: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = "main"
    accept_simulated: bool = True


class SimulateRequest(BaseModel):
    owner: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = "main"
    actor: str = "system"
    note: Optional[str] = None
    # When true, mark every required check satisfied (tests only).
    all_required: bool = False
    checks: dict[str, bool] = Field(default_factory=dict)


@router.get("/branch-protection/checklist")
@permit(P.POLICY_READ, scope=Scope.org())
def get_checklist() -> dict[str, Any]:
    return {
        "required_checks": REQUIRED_CHECKS,
        "checklist": dry_run_checklist(),
        "message": (
            "Operator dry-run checklist for boss-sandbox main. Live demo is blocked "
            "until GET /v1/safety/branch-protection reports live_demo_ready=true."
        ),
    }


@router.get("/branch-protection")
@permit(P.POLICY_READ, scope=Scope.org())
def get_branch_protection(
    owner: Optional[str] = Query(default=None),
    repo: Optional[str] = Query(default=None),
    branch: Optional[str] = Query(default="main"),
    accept_simulated: bool = Query(default=True),
) -> dict[str, Any]:
    return _svc().verify(
        owner=owner,
        repo=repo,
        branch=branch,
        accept_simulated=accept_simulated,
    )


@router.post("/branch-protection/verify")
@permit(P.POLICY_READ, scope=Scope.org(), readonly_post=True)
def post_verify(payload: VerifyRequest) -> dict[str, Any]:
    return _svc().verify(
        owner=payload.owner,
        repo=payload.repo,
        branch=payload.branch,
        accept_simulated=payload.accept_simulated,
    )


@router.post("/branch-protection/simulate")
@permit(P.POLICY_MANAGE, scope=Scope.org())
def post_simulate(payload: SimulateRequest) -> dict[str, Any]:
    """Store a simulated checklist. Fail closed: live_demo_ready only if all required pass."""
    target = _svc().default_target()
    owner = (payload.owner or target["owner"]).strip()
    repo = (payload.repo or target["repo"]).strip()
    branch = (payload.branch or target["branch"] or "main").strip()
    if payload.all_required:
        checks = all_required_true_checks()
    else:
        checks = dict(payload.checks or {})
        if not checks:
            raise HTTPException(
                status_code=422,
                detail="checks map required unless all_required=true",
            )
    record = store_simulated_checklist(
        owner=owner,
        repo=repo,
        branch=branch,
        checks=checks,
        actor=payload.actor,
        note=payload.note,
    )
    verification = _svc().verify(
        owner=owner, repo=repo, branch=branch, accept_simulated=True
    )
    return {
        "simulated": record,
        "verification": verification,
        "live_demo_ready": verification["live_demo_ready"],
    }
