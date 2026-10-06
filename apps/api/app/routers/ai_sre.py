"""HTTP surface for the AI SRE capability ledger."""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from fastapi import APIRouter, HTTPException

from boss_engine.services.ai_sre import capability_by_id, capability_ledger

router = APIRouter(prefix="/v1/ai-sre", tags=["ai-sre"])


@router.get("/capabilities")
@permit(P.RELIABILITY_READ, scope=Scope.org())
def ai_sre_capabilities():
    """Read-only map of awesome-ai-sre categories onto BOSS. Never invents coverage."""
    return capability_ledger()


@router.get("/capabilities/{capability_id}")
@permit(P.RELIABILITY_READ, scope=Scope.org())
def ai_sre_capability(capability_id: str):
    item = capability_by_id(capability_id)
    if item is None:
        raise HTTPException(status_code=404, detail="unknown AI SRE capability")
    return item
