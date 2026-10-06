"""Freeze-safe Firecracker echo exercise (no LLM, no coding claim)."""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any

from fastapi import APIRouter, HTTPException

from boss_engine.services.sandbox_provider import (
    SandboxPlaneError,
    run_echo_exercise,
)

from ..config import get_settings


router = APIRouter(tags=["sandbox"])


@router.post("/v1/diag/sandbox-echo")
@permit(P.SANDBOX_EXEC, scope=Scope.org())
def sandbox_echo_exercise() -> dict[str, Any]:
    """Boot a throwaway echo VM (test_mode + engine=echo).

    Works while ``BOSS_MICROVM_CLAIMS_FROZEN=true`` — echo is not a coding
    claim and never submits ``claude_code``. Missing host → 503. Never 500.
    Frozen coding claims do **not** produce 422 on this route.
    """
    try:
        return run_echo_exercise(get_settings())
    except SandboxPlaneError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
