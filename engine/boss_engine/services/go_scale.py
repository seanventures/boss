"""BOSS-97 parent seam — honesty, metric names, dry-run load harness.

No Go runtime is shipped. These names and the stub harness exist so children
(98/101/102) and operators share one vocabulary. The dry-run path spends
nothing and does not require Firecracker.
"""
from __future__ import annotations

from typing import Any

GO_SCALE_TRACK: dict[str, Any] = {
    "community_ga_required": False,
    "go_runtime_shipped": False,
    "s1_go_sandbox_service": False,
    "s2_go_tenant_gateway": False,
    "s3_go_event_dispatcher": False,
    "engine_requires_go": False,
    "doc": "docs/operations/GO_SCALE_TRACK.md",
}

METRIC_NAMES: tuple[str, ...] = (
    "boss_go_queue_depth",
    "boss_go_vm_count",
    "boss_go_exec_latency_seconds",
    "boss_go_in_flight",
    "boss_go_overflow_rejected_total",
    "boss_go_job_enqueue_total",
    "boss_go_job_status_total",
    "boss_go_job_cancel_total",
    "boss_go_job_heartbeat_total",
    "boss_go_job_timeout_total",
)


def go_scale_track() -> dict[str, Any]:
    """Frozen honesty snapshot. Do not flip shipped flags without a live Go binary."""
    return dict(GO_SCALE_TRACK)


def dry_run_report(
    *, n: int = 20, concurrency: int = 4, duration_s: float = 0.0
) -> dict[str, Any]:
    """In-process stub soak. Zero model cost. Zero Firecracker."""
    from .concurrency_harness import run_load, stub_task

    def task_fn(index: int) -> dict[str, Any]:
        return stub_task(index, duration_s=duration_s)

    report = run_load(task_fn, n, concurrency)
    report["runner"] = "python_stub"
    report["spent_usd_micros"] = 0
    report["firecracker_required"] = False
    report["go_scale_track"] = go_scale_track()
    report["metrics"] = list(METRIC_NAMES)
    return report
