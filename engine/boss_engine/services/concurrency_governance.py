"""Concurrency & scheduling governance (BOSS-100 / Plane #100).

Aggregates queue depth, lease contention, model rate-limit windows, elastic
fabric / VM-pool density, and deterministic policy knobs into one operator-
facing status surface. Python-side governance that the Go sandbox substrate
and workers honor; does not re-implement Firecracker.

Fail-visible backpressure signals:
  - queue pressure vs workers
  - lease contention / reclaim candidates
  - model-lane AIMD backoff / floor concurrency
  - VM host capacity saturation (when sandbox host reports capacity)
  - WIP / budget concurrent job caps
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .model_rate_limiter import rate_limiter_observability

GO_SCALE_TRACK_DOC = "docs/operations/GO_SCALE_TRACK.md"


def go_scale_track(*, settings: Optional[Settings] = None) -> dict[str, Any]:
    """Honesty block for the Go scale track. Scaffold ≠ live Firecracker.

    ``s1_go_sandbox_service`` stays false until a live probe of a real
    Firecracker pool exists. The in-repo Go binary is a fail-closed scaffold.
    """
    frozen = True
    if settings is not None:
        frozen = bool(getattr(settings, "microvm_claims_frozen", True))
    return {
        "community_ga_required": False,
        "go_runtime_shipped": False,
        "s1_go_sandbox_service": False,
        "s1_in_repo_scaffold": True,
        "s2_go_tenant_gateway": False,
        "s3_go_event_dispatcher": False,
        "microvm_claims_frozen": frozen,
        "doc": GO_SCALE_TRACK_DOC,
    }


# Documented defaults — mirrored in packages/specpack/policies/concurrency_governance_policy.yaml
DEFAULT_POLICY: dict[str, Any] = {
    "max_concurrent_vms_per_host": 8,
    "max_queue_depth_per_worker": 5,
    "backpressure_queue_depth_threshold": 20,
    "lease_contention_ratio_threshold": 0.75,
    "lease_expiry_soon_seconds": 30,
    "prefer_provider_rate_limits": True,
    "budget_governor_overrides_scale": True,
    "wip_limit": 5,
    "max_concurrent_jobs_default": 3,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def policy_from_settings(settings: Optional[Settings] = None) -> dict[str, Any]:
    """Resolve policy knobs from Settings with documented defaults."""
    s = settings
    policy = dict(DEFAULT_POLICY)
    if s is None:
        return policy
    policy["max_concurrent_vms_per_host"] = max(
        1, int(getattr(s, "concurrency_max_vms_per_host", policy["max_concurrent_vms_per_host"]) or 8)
    )
    policy["max_queue_depth_per_worker"] = max(
        1,
        int(
            getattr(
                s,
                "concurrency_max_queue_depth_per_worker",
                policy["max_queue_depth_per_worker"],
            )
            or 5
        ),
    )
    policy["backpressure_queue_depth_threshold"] = max(
        0,
        int(
            getattr(
                s,
                "concurrency_backpressure_queue_depth",
                policy["backpressure_queue_depth_threshold"],
            )
            or 20
        ),
    )
    policy["lease_contention_ratio_threshold"] = min(
        1.0,
        max(
            0.0,
            float(
                getattr(
                    s,
                    "concurrency_lease_contention_ratio",
                    policy["lease_contention_ratio_threshold"],
                )
                or 0.75
            ),
        ),
    )
    policy["lease_expiry_soon_seconds"] = max(
        1,
        int(
            getattr(
                s,
                "concurrency_lease_expiry_soon_seconds",
                policy["lease_expiry_soon_seconds"],
            )
            or 30
        ),
    )
    policy["prefer_provider_rate_limits"] = bool(
        getattr(s, "concurrency_prefer_provider_rate_limits", True)
    )
    policy["budget_governor_overrides_scale"] = bool(
        getattr(s, "concurrency_budget_governor_overrides_scale", True)
    )
    policy["wip_limit"] = max(
        1, int(getattr(s, "concurrency_wip_limit", policy["wip_limit"]) or 5)
    )
    # Model-lane knobs (existing settings)
    policy["model"] = {
        "burst_mode": bool(getattr(s, "model_burst_mode", False)),
        "max_concurrency": int(getattr(s, "model_max_concurrency", 3) or 3),
        "min_interval_ms": int(getattr(s, "model_min_interval_ms", 0) or 0),
        "rate_backoff_seconds": int(getattr(s, "model_rate_backoff_seconds", 30) or 30),
        "code_agent_max_concurrency": int(
            getattr(s, "code_agent_max_concurrency", 3) or 3
        ),
    }
    return policy


def evaluate_backpressure(
    *,
    queue: dict[str, int],
    lease: dict[str, Any],
    rate_limits: dict[str, Any],
    vm_pool: dict[str, Any],
    policy: dict[str, Any],
    actual_workers: int = 0,
) -> dict[str, Any]:
    """Pure deterministic backpressure evaluation (unit-testable)."""
    signals: list[dict[str, Any]] = []
    queue_depth = int(queue.get("queue_depth") or 0)
    threshold = int(policy.get("backpressure_queue_depth_threshold") or 20)
    per_worker = max(int(policy.get("max_queue_depth_per_worker") or 5), 1)
    workers = max(int(actual_workers), 0)

    if queue_depth >= threshold:
        signals.append(
            {
                "code": "queue_depth_high",
                "severity": "high" if queue_depth >= threshold * 2 else "medium",
                "detail": f"queue_depth={queue_depth} threshold={threshold}",
            }
        )

    if workers > 0 and queue_depth > workers * per_worker:
        signals.append(
            {
                "code": "queue_exceeds_worker_absorb",
                "severity": "high",
                "detail": (
                    f"queue_depth={queue_depth} workers={workers} "
                    f"max_per_worker={per_worker}"
                ),
            }
        )
    elif workers == 0 and queue_depth > 0:
        signals.append(
            {
                "code": "no_workers_with_queue",
                "severity": "high",
                "detail": f"queue_depth={queue_depth} actual_workers=0",
            }
        )

    contention_ratio = float(lease.get("contention_ratio") or 0.0)
    ratio_threshold = float(policy.get("lease_contention_ratio_threshold") or 0.75)
    if contention_ratio >= ratio_threshold and int(lease.get("active_leases") or 0) > 0:
        signals.append(
            {
                "code": "lease_contention",
                "severity": "medium",
                "detail": (
                    f"contention_ratio={contention_ratio:.2f} "
                    f"threshold={ratio_threshold:.2f} "
                    f"reclaim_candidates={lease.get('reclaim_candidates', 0)}"
                ),
            }
        )
    if int(lease.get("reclaim_candidates") or 0) > 0:
        signals.append(
            {
                "code": "expired_leases_pending_reclaim",
                "severity": "medium",
                "detail": f"reclaim_candidates={lease.get('reclaim_candidates')}",
            }
        )

    # Rate-limit windows: any lane in backoff or at floor is provider-bound
    for lane in rate_limits.get("lanes") or []:
        if float(lane.get("backoff_remaining_seconds") or 0) > 0:
            signals.append(
                {
                    "code": "model_rate_limit_backoff",
                    "severity": "high",
                    "detail": (
                        f"provider={lane.get('provider')} "
                        f"backoff_s={lane.get('backoff_remaining_seconds')} "
                        f"limit={lane.get('limit')}/{lane.get('max_concurrency')}"
                    ),
                }
            )
        elif (
            int(lane.get("limit") or 0) <= int(lane.get("floor_concurrency") or 1)
            and int(lane.get("in_flight") or 0) >= int(lane.get("limit") or 0) > 0
        ):
            signals.append(
                {
                    "code": "model_lane_at_floor",
                    "severity": "medium",
                    "detail": f"provider={lane.get('provider')} limit={lane.get('limit')}",
                }
            )

    if rate_limits.get("mode") == "burst" and policy.get("prefer_provider_rate_limits"):
        signals.append(
            {
                "code": "model_burst_mode_disables_pacing",
                "severity": "low",
                "detail": "BOSS_MODEL_BURST_MODE=true — provider limits may bind without BOSS pacing",
            }
        )

    # VM pool density
    capacity = vm_pool.get("capacity")
    running = vm_pool.get("running")
    ceiling = int(policy.get("max_concurrent_vms_per_host") or 8)
    if capacity is not None:
        try:
            cap_i = int(capacity)
            run_i = int(running or 0)
            if cap_i > 0 and run_i >= cap_i:
                signals.append(
                    {
                        "code": "vm_host_saturated",
                        "severity": "high",
                        "detail": f"running={run_i} capacity={cap_i}",
                    }
                )
            elif cap_i > ceiling:
                # Host reports more than policy ceiling — governance still binds to ceiling
                signals.append(
                    {
                        "code": "vm_capacity_above_policy_ceiling",
                        "severity": "low",
                        "detail": f"host_capacity={cap_i} policy_ceiling={ceiling}",
                    }
                )
        except (TypeError, ValueError):
            pass
    documented_ceiling = ceiling
    if running is not None:
        try:
            if int(running) >= documented_ceiling:
                signals.append(
                    {
                        "code": "vm_policy_ceiling_reached",
                        "severity": "high",
                        "detail": f"running={running} ceiling={documented_ceiling}",
                    }
                )
        except (TypeError, ValueError):
            pass

    # Severity rollup
    order = {"high": 3, "medium": 2, "low": 1}
    max_sev = 0
    for sig in signals:
        max_sev = max(max_sev, order.get(sig.get("severity") or "low", 0))
    if max_sev >= 3:
        level = "high"
        admit = False
        reason = "backpressure_active"
    elif max_sev >= 2:
        level = "medium"
        admit = True
        reason = "backpressure_elevated"
    elif max_sev >= 1:
        level = "low"
        admit = True
        reason = "backpressure_advisory"
    else:
        level = "none"
        admit = True
        reason = "healthy"

    needed_workers = (
        0 if queue_depth <= 0 else int(math.ceil(queue_depth / per_worker))
    )

    return {
        "level": level,
        "admit_new_work": admit,
        "reason": reason,
        "signals": signals,
        "needed_workers_estimate": needed_workers,
        "provider_limits_bind_first": bool(policy.get("prefer_provider_rate_limits", True)),
        "budget_governor_overrides_scale": bool(
            policy.get("budget_governor_overrides_scale", True)
        ),
    }


class ConcurrencyGovernanceService:
    """Project-scoped concurrency / backpressure status aggregation."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def policy(self) -> dict[str, Any]:
        return policy_from_settings(self.settings)

    def queue_snapshot(
        self, connection: Connection, project_id: str
    ) -> dict[str, int]:
        row = connection.execute(
            text(
                """
                SELECT
                  COUNT(*) FILTER (
                    WHERE status IN ('queued', 'failed_retryable') AND run_after <= now()
                  ) AS queue_depth,
                  COUNT(*) FILTER (WHERE status = 'running') AS running_jobs,
                  COUNT(*) FILTER (WHERE status = 'leased') AS leased_jobs,
                  COUNT(*) FILTER (WHERE status = 'failed_retryable') AS retryable_jobs,
                  COUNT(*) FILTER (WHERE status = 'queued') AS queued_jobs,
                  COUNT(*) FILTER (
                    WHERE status IN ('queued', 'failed_retryable') AND run_after > now()
                  ) AS delayed_jobs
                FROM jobs
                WHERE project_id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().one()
        return {
            "queue_depth": int(row["queue_depth"] or 0),
            "running_jobs": int(row["running_jobs"] or 0),
            "leased_jobs": int(row["leased_jobs"] or 0),
            "retryable_jobs": int(row["retryable_jobs"] or 0),
            "queued_jobs": int(row["queued_jobs"] or 0),
            "delayed_jobs": int(row["delayed_jobs"] or 0),
        }

    def lease_contention(
        self,
        connection: Connection,
        project_id: str,
        *,
        expiry_soon_seconds: int = 30,
    ) -> dict[str, Any]:
        """Lease pressure: active leases, near-expiry, reclaim candidates, holders."""
        row = connection.execute(
            text(
                """
                SELECT
                  COUNT(*) FILTER (
                    WHERE status IN ('leased', 'running')
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > now()
                  ) AS active_leases,
                  COUNT(*) FILTER (
                    WHERE status IN ('leased', 'running')
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= now()
                  ) AS reclaim_candidates,
                  COUNT(*) FILTER (
                    WHERE status IN ('leased', 'running')
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > now()
                      AND lease_expires_at <= now() + make_interval(secs => :soon)
                  ) AS expiring_soon,
                  COUNT(DISTINCT lease_owner) FILTER (
                    WHERE status IN ('leased', 'running')
                      AND lease_owner IS NOT NULL
                  ) AS distinct_holders
                FROM jobs
                WHERE project_id = :project_id
                """
            ),
            {"project_id": project_id, "soon": int(expiry_soon_seconds)},
        ).mappings().one()
        active = int(row["active_leases"] or 0)
        reclaim = int(row["reclaim_candidates"] or 0)
        expiring = int(row["expiring_soon"] or 0)
        holders = int(row["distinct_holders"] or 0)
        # Contention ratio: fraction of lease-bearing jobs that are reclaimable or near expiry
        lease_bearing = active + reclaim
        hot = reclaim + expiring
        ratio = (hot / lease_bearing) if lease_bearing > 0 else 0.0
        return {
            "active_leases": active,
            "reclaim_candidates": reclaim,
            "expiring_soon": expiring,
            "distinct_holders": holders,
            "lease_bearing": lease_bearing,
            "contention_ratio": round(ratio, 4),
            "expiry_soon_seconds": int(expiry_soon_seconds),
        }

    def worker_pool_summary(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        rows = connection.execute(
            text(
                """
                SELECT id, pool_key, kind, current_workers, desired_workers,
                       min_workers, max_workers, max_queue_depth_per_worker
                FROM worker_pools
                WHERE project_id = :project_id
                ORDER BY pool_key ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        pools = []
        total_current = 0
        total_desired = 0
        for r in rows:
            cur = int(r["current_workers"] or 0)
            des = int(r["desired_workers"] or 0)
            total_current += cur
            total_desired += des
            pools.append(
                {
                    "id": r["id"],
                    "pool_key": r["pool_key"],
                    "kind": r["kind"],
                    "current_workers": cur,
                    "desired_workers": des,
                    "min_workers": int(r["min_workers"] or 0),
                    "max_workers": int(r["max_workers"] or 0),
                    "max_queue_depth_per_worker": int(
                        r["max_queue_depth_per_worker"] or 5
                    ),
                }
            )
        return {
            "pools": pools,
            "total_current_workers": total_current,
            "total_desired_workers": total_desired,
            "pool_count": len(pools),
        }

    def elastic_summary(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        try:
            from .elastic_fabric import ElasticFabricService
            from .resource_plane import ResourcePlaneService

            fabric = ElasticFabricService(
                self.settings, resource_plane=ResourcePlaneService(self.settings)
            )
            status = fabric.fabric_status(connection, project_id)
            return {
                "instance_counts": status.get("instance_counts") or {},
                "infra_real_provision_allowed": status.get(
                    "infra_real_provision_allowed"
                ),
                "pool_count": len(status.get("pools") or []),
            }
        except Exception as error:  # noqa: BLE001
            return {
                "instance_counts": {},
                "unavailable": str(error)[:200],
            }

    def vm_pool_density(self) -> dict[str, Any]:
        """Sandbox host capacity + policy ceiling (no silent invent when host down)."""
        policy = self.policy()
        ceiling = int(policy["max_concurrent_vms_per_host"])
        host: dict[str, Any] = {"connected": False}
        try:
            from .sandbox_console import list_sandboxes

            report = list_sandboxes(self.settings)
            host = report.get("host") or {"connected": False}
            if report.get("unavailable"):
                host["unavailable"] = report["unavailable"]
        except Exception as error:  # noqa: BLE001
            host = {"connected": False, "unavailable": str(error)[:200]}

        capacity = host.get("capacity")
        available = host.get("available")
        running = host.get("running")
        return {
            "policy_ceiling": ceiling,
            "documented_ceiling": ceiling,
            "host_connected": bool(host.get("connected")),
            "host_kind": host.get("kind"),
            "capacity": capacity,
            "available": available,
            "running": running,
            "effective_ceiling": (
                min(int(capacity), ceiling)
                if capacity is not None
                else ceiling
            ),
            "density_note": (
                f"At most {ceiling} concurrent VMs per host under BOSS policy; "
                "host capacity may be lower. Provider rate limits bind before "
                "host density when prefer_provider_rate_limits is true."
            ),
            "host_unavailable": host.get("unavailable"),
        }

    def budget_caps(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT max_concurrent_jobs, max_workers_total
                FROM budget_policies
                WHERE scope_type = 'project' AND scope_id = :project_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            return {
                "max_concurrent_jobs": int(
                    self.policy().get("max_concurrent_jobs_default") or 3
                ),
                "max_workers_total": None,
                "source": "default",
            }
        return {
            "max_concurrent_jobs": int(row["max_concurrent_jobs"] or 1),
            "max_workers_total": (
                int(row["max_workers_total"])
                if row["max_workers_total"] is not None
                else None
            ),
            "source": "budget_policies",
        }

    def status(
        self,
        connection: Connection,
        project_id: str,
        *,
        include_sandbox_host: bool = True,
    ) -> dict[str, Any]:
        policy = self.policy()
        queue = self.queue_snapshot(connection, project_id)
        lease = self.lease_contention(
            connection,
            project_id,
            expiry_soon_seconds=int(policy["lease_expiry_soon_seconds"]),
        )
        workers = self.worker_pool_summary(connection, project_id)
        elastic = self.elastic_summary(connection, project_id)
        rate_limits = rate_limiter_observability(
            self.settings, project_id=project_id
        )
        vm_pool = (
            self.vm_pool_density()
            if include_sandbox_host
            else {
                "policy_ceiling": policy["max_concurrent_vms_per_host"],
                "documented_ceiling": policy["max_concurrent_vms_per_host"],
                "host_connected": False,
                "density_note": "sandbox host probe skipped",
            }
        )
        budget = self.budget_caps(connection, project_id)
        backpressure = evaluate_backpressure(
            queue=queue,
            lease=lease,
            rate_limits=rate_limits,
            vm_pool=vm_pool,
            policy=policy,
            actual_workers=int(workers.get("total_current_workers") or 0),
        )
        return {
            "project_id": project_id,
            "as_of": _iso(_utcnow()),
            "queue": queue,
            "lease_contention": lease,
            "rate_limit_windows": rate_limits,
            "worker_pools": workers,
            "elastic_fabric": elastic,
            "vm_pool_density": vm_pool,
            "budget_caps": budget,
            "policy": policy,
            "backpressure": backpressure,
            "related_apis": {
                "worker_autoscaling": "/v1/worker-autoscaling/status",
                "elastic_fabric": "/v1/elastic/status",
                "model_rate_limits": "/v1/model-gateway/rate-limits",
                "factory_wip": "/v1/factory/wip",
            },
            "message": (
                "Concurrency governance aggregates queue depth, lease contention, "
                "model rate-limit windows, and VM-pool density. Provider rate limits "
                "bind first; budget governor overrides scale when enabled."
            ),
        }
