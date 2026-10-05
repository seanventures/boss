"""System health: the host BOSS runs on, as BOSS sees it (stability spec §4c-§4e).

Thin routes over engine services: deploy readiness (drain), and — with the system doctor — the
gauges, the findings that need a person, what BOSS did on its own, and the operator's choices.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from boss_engine.db import begin, get_engine

router = APIRouter(tags=["system-health"])


@router.get("/v1/system/deploy-readiness")
def get_deploy_readiness() -> dict[str, Any]:
    """Would an update now interrupt builds, and did the operator ask to hold updates while
    builds run? Deploy scripts (remote.sh update) wait on this before recreating containers."""
    from boss_engine.services.drain import deploy_readiness

    with begin(get_engine()) as connection:
        return deploy_readiness(connection)
