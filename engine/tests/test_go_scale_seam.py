"""BOSS-97 parent seam: backpressure overflow, rate-limit hooks, load-harness stub.

No Postgres, no Firecracker, no model spend.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from boss_engine.services.concurrency_governance import (
    DEFAULT_POLICY,
    admit_or_reject,
    evaluate_backpressure,
    policy_from_settings,
)
from boss_engine.services.go_scale import (
    GO_SCALE_TRACK,
    METRIC_NAMES,
    dry_run_report,
    go_scale_track,
)


ROOT = Path(__file__).resolve().parents[2]


def test_go_scale_track_honesty_is_fail_closed():
    track = go_scale_track()
    assert track["community_ga_required"] is False
    assert track["go_runtime_shipped"] is False
    assert track["s1_go_sandbox_service"] is False
    assert track["s2_go_tenant_gateway"] is False
    assert track["s3_go_event_dispatcher"] is False
    assert track["engine_requires_go"] is False
    assert track["doc"] == "docs/operations/GO_SCALE_TRACK.md"
    # Frozen snapshot so a later flip cannot be silent.
    assert track == GO_SCALE_TRACK


def test_metric_names_cover_queue_vm_latency_and_job_ops():
    names = set(METRIC_NAMES)
    for required in (
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
    ):
        assert required in names


def test_admit_or_reject_overflow_is_honest():
    overflow = admit_or_reject(in_flight=8, max_in_flight=8)
    assert overflow["admitted"] is False
    assert overflow["code"] == "max_in_flight_overflow"
    assert overflow["http_status"] == 429
    assert overflow["retryable"] is True
    assert "in_flight=8" in overflow["reason"]
    assert overflow["dropped_silently"] is False

    ok = admit_or_reject(in_flight=2, max_in_flight=8)
    assert ok["admitted"] is True
    assert ok["code"] == "ok"
    assert ok["http_status"] == 202


def test_admit_or_reject_backpressure_and_rate_limit():
    bp = evaluate_backpressure(
        queue={"queue_depth": 40},
        lease={"contention_ratio": 0.0, "active_leases": 0, "reclaim_candidates": 0},
        rate_limits={"mode": "paced", "lanes": []},
        vm_pool={},
        policy=DEFAULT_POLICY,
        actual_workers=0,
    )
    rejected = admit_or_reject(in_flight=1, max_in_flight=16, backpressure=bp)
    assert rejected["admitted"] is False
    assert rejected["code"] == "backpressure_reject"
    assert rejected["dropped_silently"] is False

    rate = admit_or_reject(in_flight=1, max_in_flight=16, rate_limited=True)
    assert rate["admitted"] is False
    assert rate["code"] == "rate_limit_reject"


def test_evaluate_backpressure_signals_max_in_flight_overflow():
    policy = dict(DEFAULT_POLICY)
    policy["max_in_flight"] = 4
    result = evaluate_backpressure(
        queue={"queue_depth": 0, "running_jobs": 3, "leased_jobs": 1},
        lease={"contention_ratio": 0.0, "active_leases": 4, "reclaim_candidates": 0},
        rate_limits={"mode": "paced", "lanes": []},
        vm_pool={},
        policy=policy,
        actual_workers=2,
    )
    codes = {s["code"] for s in result["signals"]}
    assert "max_in_flight_overflow" in codes
    assert result["admit_new_work"] is False
    assert result["level"] == "high"


def test_policy_includes_max_in_flight():
    policy = policy_from_settings(None)
    assert policy["max_in_flight"] >= 1


def test_dry_run_harness_does_not_spend_or_need_firecracker():
    report = dry_run_report(n=12, concurrency=3, duration_s=0.0)
    assert report["runner"] == "python_stub"
    assert report["spent_usd_micros"] == 0
    assert report["firecracker_required"] is False
    assert report["tasks"] == 12
    assert report["peak_concurrency"] <= 3
    assert report["go_scale_track"]["go_runtime_shipped"] is False
    assert "boss_go_queue_depth" in report["metrics"]


def test_dry_run_script_refuses_live_and_succeeds_stub():
    script = ROOT / "scripts" / "go_scale_dry_run.py"
    live = subprocess.run(
        [sys.executable, str(script), "--live"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert live.returncode != 0
    assert "refuse" in (live.stderr + live.stdout).lower() or "not spend" in (
        live.stderr + live.stdout
    ).lower()

    fc = subprocess.run(
        [sys.executable, str(script), "--firecracker"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert fc.returncode != 0

    ok = subprocess.run(
        [sys.executable, str(script), "--n", "8", "--concurrency", "2", "--duration-ms", "0"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "python_stub" in ok.stdout
    assert "spent_usd_micros" in ok.stdout
