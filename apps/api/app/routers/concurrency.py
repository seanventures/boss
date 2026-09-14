"""HTTP surface for concurrency & scheduling governance (BOSS-100).

Prefix: /v1/concurrency
  GET /status  — queue depth, lease contention, rate-limit windows, VM density
  GET /policy  — policy knobs (settings + documented defaults)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.services.concurrency_governance import (
    ConcurrencyGovernanceService,
    go_scale_track,
)

router = APIRouter(prefix="/v1/concurrency", tags=["concurrency"])


def _service() -> ConcurrencyGovernanceService:
    return ConcurrencyGovernanceService(get_settings())


@router.get("/policy")
def get_policy() -> dict[str, Any]:
    """Documented concurrency / backpressure / rate-limit policy knobs."""
    svc = _service()
    policy = svc.policy()
    return {
        "policy": policy,
        "go_scale_track": go_scale_track(settings=get_settings()),
        "env_aliases": {
            "max_concurrent_vms_per_host": "BOSS_CONCURRENCY_MAX_VMS_PER_HOST",
            "max_in_flight": "BOSS_CONCURRENCY_MAX_IN_FLIGHT",
            "max_queue_depth_per_worker": "BOSS_CONCURRENCY_MAX_QUEUE_DEPTH_PER_WORKER",
            "backpressure_queue_depth_threshold": "BOSS_CONCURRENCY_BACKPRESSURE_QUEUE_DEPTH",
            "lease_contention_ratio_threshold": "BOSS_CONCURRENCY_LEASE_CONTENTION_RATIO",
            "model_max_concurrency": "BOSS_MODEL_MAX_CONCURRENCY",
            "model_burst_mode": "BOSS_MODEL_BURST_MODE",
            "model_min_interval_ms": "BOSS_MODEL_MIN_INTERVAL_MS",
            "model_rate_backoff_seconds": "BOSS_MODEL_RATE_BACKOFF_SECONDS",
        },
        "specpack": "packages/specpack/policies/concurrency_governance_policy.yaml",
        "message": (
            "Provider rate limits bind first when prefer_provider_rate_limits is true. "
            "Budget governor overrides scale when budget_governor_overrides_scale is true."
        ),
    }


@router.get("/status")
def get_status(
    project_id: str = Query(..., description="Project scope for queue/lease metrics"),
    include_sandbox_host: bool = Query(
        default=True,
        description="Probe sandbox host capacity (may be slow if host is down)",
    ),
) -> dict[str, Any]:
    """Aggregate concurrency / backpressure status for a project."""
    if not (project_id or "").strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    svc = _service()
    try:
        with begin(get_engine()) as connection:
            return svc.status(
                connection,
                project_id.strip(),
                include_sandbox_host=include_sandbox_host,
            )
    except Exception as error:  # noqa: BLE001
        # Surface DB/config failures visibly — never silent empty success.
        raise HTTPException(
            status_code=503,
            detail=f"concurrency status unavailable: {error}",
        ) from error
