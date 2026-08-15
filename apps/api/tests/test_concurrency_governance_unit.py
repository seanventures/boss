"""Unit tests for BOSS-100 concurrency governance (no Postgres required)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.app.routers.concurrency import router
from boss_engine.services.concurrency_governance import (
    DEFAULT_POLICY,
    evaluate_backpressure,
    policy_from_settings,
)


def test_policy_defaults():
    policy = policy_from_settings(None)
    assert policy["max_concurrent_vms_per_host"] == 8
    assert policy["prefer_provider_rate_limits"] is True
    assert policy["max_queue_depth_per_worker"] == 5


def test_policy_from_settings_overrides():
    settings = SimpleNamespace(
        concurrency_max_vms_per_host=4,
        concurrency_max_queue_depth_per_worker=3,
        concurrency_backpressure_queue_depth=10,
        concurrency_lease_contention_ratio=0.5,
        concurrency_lease_expiry_soon_seconds=15,
        concurrency_prefer_provider_rate_limits=True,
        concurrency_budget_governor_overrides_scale=True,
        concurrency_wip_limit=2,
        model_burst_mode=False,
        model_max_concurrency=2,
        model_min_interval_ms=100,
        model_rate_backoff_seconds=45,
        code_agent_max_concurrency=2,
    )
    policy = policy_from_settings(settings)
    assert policy["max_concurrent_vms_per_host"] == 4
    assert policy["max_queue_depth_per_worker"] == 3
    assert policy["backpressure_queue_depth_threshold"] == 10
    assert policy["model"]["max_concurrency"] == 2
    assert policy["model"]["min_interval_ms"] == 100


def test_evaluate_backpressure_healthy():
    result = evaluate_backpressure(
        queue={"queue_depth": 0, "running_jobs": 0, "leased_jobs": 0},
        lease={"contention_ratio": 0.0, "active_leases": 0, "reclaim_candidates": 0},
        rate_limits={"mode": "paced", "lanes": []},
        vm_pool={"capacity": 8, "running": 1, "policy_ceiling": 8},
        policy=DEFAULT_POLICY,
        actual_workers=2,
    )
    assert result["level"] == "none"
    assert result["admit_new_work"] is True
    assert result["signals"] == []


def test_evaluate_backpressure_queue_high_and_no_workers():
    result = evaluate_backpressure(
        queue={"queue_depth": 25},
        lease={"contention_ratio": 0.0, "active_leases": 0, "reclaim_candidates": 0},
        rate_limits={"mode": "paced", "lanes": []},
        vm_pool={},
        policy=DEFAULT_POLICY,
        actual_workers=0,
    )
    codes = {s["code"] for s in result["signals"]}
    assert "queue_depth_high" in codes
    assert "no_workers_with_queue" in codes
    assert result["level"] == "high"
    assert result["admit_new_work"] is False
    assert result["needed_workers_estimate"] == 5  # ceil(25/5)


def test_evaluate_backpressure_rate_limit_backoff():
    result = evaluate_backpressure(
        queue={"queue_depth": 1},
        lease={"contention_ratio": 0.0, "active_leases": 1, "reclaim_candidates": 0},
        rate_limits={
            "mode": "paced",
            "lanes": [
                {
                    "provider": "anthropic",
                    "backoff_remaining_seconds": 12.5,
                    "limit": 1,
                    "max_concurrency": 3,
                    "floor_concurrency": 1,
                    "in_flight": 1,
                }
            ],
        },
        vm_pool={},
        policy=DEFAULT_POLICY,
        actual_workers=2,
    )
    codes = {s["code"] for s in result["signals"]}
    assert "model_rate_limit_backoff" in codes
    assert result["level"] == "high"
    assert result["provider_limits_bind_first"] is True


def test_evaluate_vm_policy_ceiling():
    result = evaluate_backpressure(
        queue={"queue_depth": 0},
        lease={"contention_ratio": 0.0, "active_leases": 0, "reclaim_candidates": 0},
        rate_limits={"mode": "paced", "lanes": []},
        vm_pool={"running": 8, "capacity": 8},
        policy=DEFAULT_POLICY,
        actual_workers=4,
    )
    codes = {s["code"] for s in result["signals"]}
    assert "vm_host_saturated" in codes or "vm_policy_ceiling_reached" in codes
    assert result["level"] == "high"


def test_evaluate_lease_contention():
    result = evaluate_backpressure(
        queue={"queue_depth": 2},
        lease={
            "contention_ratio": 0.9,
            "active_leases": 4,
            "reclaim_candidates": 2,
        },
        rate_limits={"mode": "paced", "lanes": []},
        vm_pool={},
        policy=DEFAULT_POLICY,
        actual_workers=2,
    )
    codes = {s["code"] for s in result["signals"]}
    assert "lease_contention" in codes
    assert "expired_leases_pending_reclaim" in codes
    assert result["level"] in {"medium", "high"}


@pytest.fixture
def policy_client():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_get_policy_endpoint(policy_client):
    r = policy_client.get("/v1/concurrency/policy")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "policy" in body
    assert body["policy"]["max_concurrent_vms_per_host"] >= 1
    assert "BOSS_CONCURRENCY_MAX_VMS_PER_HOST" in body["env_aliases"].values()
    assert "concurrency_governance_policy.yaml" in body["specpack"]
    track = body["go_scale_track"]
    assert track["go_runtime_shipped"] is False
    assert track["s1_go_sandbox_service"] is False
    assert track["s2_go_tenant_gateway"] is False
    assert track["s3_go_event_dispatcher"] is False
    assert track["engine_requires_go"] is False
    assert track["s1_in_repo_scaffold"] is True
    assert track["community_ga_required"] is False
