"""HTTP surface for the Community v1 feature lock summary.

GET /v1/v1-lock returns in / out / cloud counts only. Not a shipment claim.
Cloud / Enterprise stay ✦. Not public GA.
"""

from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any

from fastapi import APIRouter

from boss_engine.config import get_settings
from boss_engine.services.release_planning import ReleasePlanningService


router = APIRouter(prefix="/v1", tags=["v1-lock"])

_settings = get_settings()
_service = ReleasePlanningService(_settings)


@router.get("/v1-lock")
@permit(P.SYSTEM_READ, scope=Scope.org())
def get_v1_lock() -> dict[str, Any]:
    """Community v1 lock counts (in / out / cloud). Not a shipment claim."""
    return _service.v1_lock_summary()
