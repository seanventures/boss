"""System health: the host BOSS runs on, as BOSS sees it (stability spec §4c-§4e).

Thin routes over engine services: deploy readiness (drain), and — with the system doctor — the
gauges, the findings that need a person, what BOSS did on its own, and the operator's choices.
"""
from __future__ import annotations

from typing import Any

from typing import Optional, Union

from fastapi import APIRouter
from pydantic import BaseModel, Field

from boss_engine.db import begin, get_engine

router = APIRouter(tags=["system-health"])


class BuildsAtOnceBody(BaseModel):
    """"auto" (derived from this machine) or a number of builds to run at once."""

    value: Optional[Union[int, str]] = Field(default="auto",
                                             description='"auto" or 1-8 builds at once')


@router.get("/v1/system/deploy-readiness")
def get_deploy_readiness() -> dict[str, Any]:
    """Would an update now interrupt builds, and did the operator ask to hold updates while
    builds run? Deploy scripts (remote.sh update) wait on this before recreating containers."""
    from boss_engine.services.drain import deploy_readiness

    with begin(get_engine()) as connection:
        return deploy_readiness(connection)


@router.get("/v1/system/health")
def get_system_health() -> dict[str, Any]:
    """Gauges with their scope ("This server" / "Docker Desktop VM, not your Mac"), the
    degradation level, and admission ("Builds at once: 2 (auto) · 1 queued, waiting for
    0.6 GB"). The worker reads `admission.builds_at_once.value` to size its lease slots."""
    from boss_engine.services import admission

    cap = admission.sample()
    with begin(get_engine()) as connection:
        state = admission.snapshot(connection, cap=cap)
    scope_words = {"machine": "This server", "docker_vm": "Docker Desktop VM, not your Mac",
                   "container": "This BOSS container's memory cap"}
    level = int(state.get("level") or 0)
    return {
        "scope": cap.scope, "scope_label": scope_words.get(cap.scope, cap.scope),
        "level": level, "level_label": admission.LEVEL_WORDS.get(level, str(level)),
        "gauges": {
            "memory_total_mb": round(cap.total_mb), "memory_available_mb": round(cap.available_mb),
            "memory_reserved_for_boss_mb": round(cap.reserve_mb),
            "memory_pressure_full_avg10": cap.psi_full_avg10,
            "memory_pressure_full_avg60": cap.psi_full_avg60,
            "swap_used_mb": round(cap.swap_used_mb), "disk_free_gb": cap.disk_free_gb,
            "disk_used_pct": cap.disk_used_pct, "load_per_core": cap.load_per_core,
            "cores": cap.cores,
        },
        "admission": state,
    }


@router.put("/v1/projects/{project_id}/settings/builds_at_once")
def put_builds_at_once(project_id: str, payload: BuildsAtOnceBody) -> dict[str, Any]:
    """`{"value": "auto"}` or `{"value": N}`. Install-wide: builds share one machine."""
    from fastapi import HTTPException

    from boss_engine.services import admission

    raw = payload.value
    if raw in (None, "", "auto"):
        value: Any = None
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="value must be 'auto' or a number")
        if not 1 <= value <= admission.MAX_SLOTS:
            raise HTTPException(status_code=422,
                                detail=f"value must be between 1 and {admission.MAX_SLOTS}")
    cap = admission.sample()
    with begin(get_engine()) as connection:
        admission.set_state(connection, "builds_at_once",
                            {"value": value, "set_for_project": project_id})
        state = admission.snapshot(connection, cap=cap)
    return {"builds_at_once": state["builds_at_once"], "label": state["label"]}
