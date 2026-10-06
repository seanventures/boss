"""HTTP surface for the deterministic AI SRE bench.

Mounted at GET /v1/ai-sre/bench. No vendors. Not public GA.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from boss_engine.services.sre_bench import run_sre_bench
from fastapi import APIRouter

router = APIRouter(prefix="/v1/ai-sre", tags=["ai-sre"])


@router.get("/bench")
@permit(P.RELIABILITY_READ, scope=Scope.org())
def ai_sre_bench():
    """Run InvestigationEngine against fixed fixtures. Never invents confidence."""
    return run_sre_bench()
