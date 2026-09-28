"""Worker autoscaling control plane — queue → desired → elastic fabric apply."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from boss_engine.services.worker_autoscaling import WorkerAutoscalingService

PROJECT_ID = "boss-dogfood"


def test_compute_desired_workers_deterministic():
    # 11 queued jobs, 5 per worker → need 3; clamp max 8 min 0
    result = WorkerAutoscalingService.compute_desired_workers(
        queue_depth=11,
        min_workers=0,
        max_workers=8,
        max_queue_depth_per_worker=5,
        scale_up_threshold=1,
        current_desired=0,
        cooldown_seconds=0,
        last_applied_at=None,
    )
    assert result["needed_workers"] == 3
    assert result["desired_workers"] == 3
    assert result["action"] == "scale_up"

    # Below scale_up_threshold holds
    held = WorkerAutoscalingService.compute_desired_workers(
        queue_depth=2,
        min_workers=0,
        max_workers=8,
        max_queue_depth_per_worker=5,
        scale_up_threshold=5,
        current_desired=0,
        cooldown_seconds=0,
        last_applied_at=None,
    )
    assert held["raw_target"] == 1
    assert held["desired_workers"] == 0
    assert held["action"] == "held_threshold"

    # Cooldown holds scale-up
    last = datetime.now(timezone.utc) - timedelta(seconds=5)
    cool = WorkerAutoscalingService.compute_desired_workers(
        queue_depth=20,
        min_workers=0,
        max_workers=8,
        max_queue_depth_per_worker=5,
        scale_up_threshold=1,
        current_desired=1,
        cooldown_seconds=60,
        last_applied_at=last,
    )
    assert cool["action"] == "held_cooldown"
    assert cool["desired_workers"] == 1

    # Empty queue → scale toward min
    empty = WorkerAutoscalingService.compute_desired_workers(
        queue_depth=0,
        min_workers=1,
        max_workers=8,
        max_queue_depth_per_worker=5,
        scale_up_threshold=1,
        current_desired=4,
        cooldown_seconds=0,
        last_applied_at=None,
    )
    assert empty["desired_workers"] == 1
    assert empty["action"] == "scale_down"


def test_policy_upsert_evaluate_apply_status_history(client):
    # Upsert policy for a dedicated pool
    policy = client.put(
        "/v1/worker-autoscaling/policies",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "as-pool-a",
            "enabled": True,
            "min_workers": 0,
            "max_workers": 4,
            "scale_up_threshold": 1,
            "cooldown_seconds": 0,
            "max_queue_depth_per_worker": 2,
            "kind": "local",
        },
    )
    assert policy.status_code == 200, policy.text
    body = policy.json()
    assert body["pool_key"] == "as-pool-a"
    assert body["max_workers"] == 4
    assert body["max_queue_depth_per_worker"] == 2

    listed = client.get(
        "/v1/worker-autoscaling/policies",
        params={"project_id": PROJECT_ID},
    )
    assert listed.status_code == 200
    keys = {item["pool_key"] for item in listed.json()["items"]}
    assert "as-pool-a" in keys

    # Queue 5 jobs → need ceil(5/2)=3 workers
    for index in range(5):
        created = client.post(
            "/v1/jobs",
            json={
                "project_id": PROJECT_ID,
                "kind": "plan.command",
                "payload": {"as_index": index},
                "priority": 50,
            },
        )
        assert created.status_code == 201, created.text

    evaluated = client.post(
        "/v1/worker-autoscaling/evaluate",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "as-pool-a",
        },
    )
    assert evaluated.status_code == 200, evaluated.text
    ev = evaluated.json()
    assert ev["queue"]["queue_depth"] >= 5
    assert len(ev["evaluations"]) == 1
    one = ev["evaluations"][0]
    assert one["pool_key"] == "as-pool-a"
    assert one["needed_workers"] == 3
    assert one["desired_workers"] == 3
    assert one["action"] == "scale_up"

    applied = client.post(
        "/v1/worker-autoscaling/apply",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "as-pool-a",
            "auto_reconcile": True,
            "reason": "test scale-out",
        },
    )
    assert applied.status_code == 200, applied.text
    result = applied.json()["results"][0]
    assert result["status"] == "applied"
    assert result["desired_workers"] == 3
    assert result["previous_desired"] == 0
    assert result["receipt_id"]
    assert result["elastic"] is not None
    # Local reconcile should materialize instances
    recon = result["elastic"].get("reconcile") or {}
    assert recon.get("active_instances") == 3

    status = client.get(
        "/v1/worker-autoscaling/status",
        params={"project_id": PROJECT_ID},
    )
    assert status.status_code == 200, status.text
    st = status.json()
    assert st["queue_depth"] >= 5
    assert st["leased_jobs"] >= 0
    pool_st = next(p for p in st["pools"] if p["pool_key"] == "as-pool-a")
    assert pool_st["desired_workers"] == 3
    assert pool_st["actual_workers"] == 3

    history = client.get(
        "/v1/worker-autoscaling/history",
        params={"project_id": PROJECT_ID, "pool_key": "as-pool-a"},
    )
    assert history.status_code == 200, history.text
    items = history.json()["items"]
    assert len(items) >= 1
    assert any(item["status"] == "applied" for item in items)

    # Events + receipts visible
    events = client.get("/v1/events", params={"project_id": PROJECT_ID})
    types = {item["event_type"] for item in events.json()["items"]}
    assert "worker_autoscaling.policy_upserted" in types
    assert "worker_autoscaling.applied" in types
    assert "elastic.capacity_requested" in types


def test_multi_pool_and_cooldown(client):
    for pool_key, max_workers in (("as-pool-b", 2), ("as-pool-c", 6)):
        resp = client.put(
            "/v1/worker-autoscaling/policies",
            json={
                "project_id": PROJECT_ID,
                "actor": "ceo",
                "pool_key": pool_key,
                "min_workers": 0,
                "max_workers": max_workers,
                "scale_up_threshold": 1,
                "cooldown_seconds": 3600,
                "max_queue_depth_per_worker": 1,
                "kind": "local",
            },
        )
        assert resp.status_code == 200, resp.text

    for index in range(3):
        created = client.post(
            "/v1/jobs",
            json={
                "project_id": PROJECT_ID,
                "kind": "plan.command",
                "payload": {"multi": index},
            },
        )
        assert created.status_code == 201

    # First apply for pool-b succeeds (clamped by max=2)
    first = client.post(
        "/v1/worker-autoscaling/apply",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "as-pool-b",
            "auto_reconcile": True,
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["results"][0]["status"] == "applied"
    assert first.json()["results"][0]["desired_workers"] == 2  # clamped by max

    # Raise max so raw target would increase, but cooldown must hold scale-up
    raised = client.put(
        "/v1/worker-autoscaling/policies",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "as-pool-b",
            "min_workers": 0,
            "max_workers": 8,
            "scale_up_threshold": 1,
            "cooldown_seconds": 3600,
            "max_queue_depth_per_worker": 1,
            "kind": "local",
        },
    )
    assert raised.status_code == 200, raised.text

    second = client.post(
        "/v1/worker-autoscaling/apply",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "as-pool-b",
            "auto_reconcile": True,
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["results"][0]["status"] == "held_cooldown"
    assert second.json()["results"][0]["desired_workers"] == 2

    # force bypasses cooldown
    forced = client.post(
        "/v1/worker-autoscaling/apply",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "as-pool-c",
            "force": True,
            "auto_reconcile": True,
        },
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["results"][0]["status"] == "applied"
    assert forced.json()["results"][0]["desired_workers"] == 3

    # Multi-pool evaluate without pool_key
    multi = client.post(
        "/v1/worker-autoscaling/evaluate",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert multi.status_code == 200, multi.text
    keys = {e["pool_key"] for e in multi.json()["evaluations"]}
    assert "as-pool-b" in keys
    assert "as-pool-c" in keys


def test_apply_is_idempotent_when_no_change(client):
    client.put(
        "/v1/worker-autoscaling/policies",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "as-idle",
            "min_workers": 0,
            "max_workers": 2,
            "cooldown_seconds": 0,
            "max_queue_depth_per_worker": 5,
            "kind": "local",
        },
    )
    # No jobs → desired 0, no-op apply
    applied = client.post(
        "/v1/worker-autoscaling/apply",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "as-idle",
        },
    )
    assert applied.status_code == 200, applied.text
    result = applied.json()["results"][0]
    assert result["status"] == "no_change"
    assert result["desired_workers"] == 0
    assert result["receipt_id"] is None


def test_ensure_tables_and_unconfigured_status_are_honest(client, engine):
    from boss_engine.config import get_settings
    from boss_engine.services.worker_autoscaling import WorkerAutoscalingService
    from sqlalchemy import text

    from apps.api.app.db import begin

    svc = WorkerAutoscalingService(get_settings())
    with begin(engine) as connection:
        assert svc.ensure_tables(connection) is True
        assert svc.ensure_tables(connection) is True
        present = connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name IN "
                "('worker_autoscaling_policies', 'worker_autoscaling_history')"
            )
        ).fetchall()
        names = {row[0] for row in present}
        assert "worker_autoscaling_policies" in names
        assert "worker_autoscaling_history" in names

    status = client.get(
        "/v1/worker-autoscaling/status",
        params={"project_id": PROJECT_ID},
    )
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["configured"] is False
    assert body["queue_depth"] == 0
    assert isinstance(body["pools"], list)
    history = client.get(
        "/v1/worker-autoscaling/history",
        params={"project_id": PROJECT_ID},
    )
    assert history.status_code == 200, history.text
    assert history.json()["items"] == []
    policies = client.get(
        "/v1/worker-autoscaling/policies",
        params={"project_id": PROJECT_ID},
    )
    assert policies.status_code == 200, policies.text
    assert policies.json()["items"] == []
    assert policies.json()["count"] == 0
