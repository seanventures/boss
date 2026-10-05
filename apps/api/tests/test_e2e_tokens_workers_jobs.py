"""Isolated-Postgres HTTP E2E for token efficacy, worker autoscaling, jobs.

These tests must not run against the shared dogfood `boss` database. Set
DATABASE_URL to a dedicated database (this session uses
boss_e2e_tokens_workers on a throwaway Postgres container).

Seams:
  GET/PUT /v1/token-efficiency/{scorecard,policies,consumption}
  GET/PUT /v1/worker-autoscaling/{status,policies,history} + POST /evaluate
  POST /v1/jobs + /v1/worker/{poll,heartbeat}
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4

from boss_engine.services.token_efficiency import ensure_tables as ensure_token_tables
from boss_engine.services.worker_autoscaling import WorkerAutoscalingService
from sqlalchemy import text

from apps.api.app.db import begin

SHARED_DB_NAMES = frozenset({"boss", "postgres", ""})
# jobs / worker_autoscaling_policies FK to projects — use the seeded demo org
SEEDED_PROJECT = "boss-dogfood"


def _database_name() -> str:
    raw = os.environ.get("DATABASE_URL") or ""
    parsed = urlparse(raw)
    return (parsed.path or "/").lstrip("/").split("/")[0]


def test_e2e_uses_isolated_postgres_not_shared_boss_db():
    name = _database_name()
    assert name not in SHARED_DB_NAMES, (
        f"Refusing E2E against shared database {name!r}. "
        "Point DATABASE_URL at an isolated Postgres database."
    )
    assert "e2e" in name or "test" in name, (
        f"Isolated E2E database name {name!r} should contain 'e2e' or 'test'."
    )


def _drop_if_exists(connection, *tables: str) -> None:
    for table in tables:
        connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))


def _count(connection, table: str, project_id: str) -> int:
    present = connection.execute(
        text("SELECT to_regclass(:name)"), {"name": table}
    ).scalar_one()
    if present is None:
        return 0
    return int(
        connection.execute(
            text(f"SELECT count(*) FROM {table} WHERE project_id = :p"),
            {"p": project_id},
        ).scalar_one()
    )


def test_token_efficiency_http_never_500_when_tables_missing(client, engine):
    """ensure_tables recovers missing relations; GETs stay 200 empty/defaults."""
    pid = f"proj_e2e_te_missing_{uuid4().hex[:10]}"
    with begin(engine) as connection:
        _drop_if_exists(connection, "token_budget_policies")

    scorecard = client.get(
        "/v1/token-efficiency/scorecard", params={"project_id": pid}
    )
    assert scorecard.status_code == 200, scorecard.text
    sc = scorecard.json()
    assert sc["project_id"] == pid
    assert sc["calls"] == 0
    assert sc["total_tokens"] == 0
    assert sc["cost_usd_micros"] == 0
    assert sc["cache_ratio"] is None
    assert sc["grades"]["cache_ratio"] == "n/a"
    assert sc["by_stage"] == []

    policies = client.get(
        "/v1/token-efficiency/policies", params={"project_id": pid}
    )
    assert policies.status_code == 200, policies.text
    pb = policies.json()
    assert pb["configured"] is False
    assert pb["count"] >= 1
    assert all(item["source"] == "default" for item in pb["policies"])
    assert all(item["id"] is None for item in pb["policies"])

    consumption = client.get(
        "/v1/token-efficiency/consumption",
        params={"project_id": pid, "window_hours": 24},
    )
    assert consumption.status_code == 200, consumption.text
    cb = consumption.json()
    assert cb["calls"] == 0
    assert cb["total_tokens"] == 0
    assert cb["prompt_tokens"] == 0
    assert cb["completion_tokens"] == 0
    assert cb["cost_usd_micros"] == 0
    assert cb["series"] == []
    assert cb["alerts"] == []
    assert cb["policy"]["token_utilization"] in (None, 0.0)
    assert cb["policy"]["cost_utilization"] in (None, 0.0)

    with begin(engine) as connection:
        assert ensure_token_tables(connection) is True
        present = connection.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'token_budget_policies'"
            )
        ).first()
        assert present is not None


def test_token_efficiency_put_policy_then_consumption_is_real_zeros(client):
    """Stored policy must not invent token usage when llm_calls is empty."""
    # A real project: writing token guides needs a member with `budget_manage` now
    # (activity-govern §10 Q3). Its llm_calls are emptied before every test.
    pid = SEEDED_PROJECT
    put = client.put(
        "/v1/token-efficiency/policies",
        json={
            "project_id": pid,
            "policies": [
                {
                    "stage": "*",
                    "max_tokens_per_day": 1_000_000,
                    "max_cost_usd_micros_per_day": 10_000_000,
                    "alert_at_pct": 0.75,
                    "preferred_model_class": "low_cost",
                    "enabled": True,
                }
            ],
        },
    )
    assert put.status_code == 200, put.text
    stored = next(p for p in put.json()["policies"] if p["stage"] == "*")
    assert stored["source"] == "stored"
    assert stored["max_tokens_per_day"] == 1_000_000
    assert stored["max_cost_usd_micros_per_day"] == 10_000_000

    cons = client.get(
        "/v1/token-efficiency/consumption",
        params={"project_id": pid, "window_hours": 24},
    )
    assert cons.status_code == 200, cons.text
    body = cons.json()
    assert body["calls"] == 0
    assert body["total_tokens"] == 0
    assert body["prompt_tokens"] == 0
    assert body["completion_tokens"] == 0
    assert body["cache_read_tokens"] == 0
    assert body["cost_usd_micros"] == 0
    assert body["failed_burn_usd_micros"] == 0
    assert body["series"] == []
    assert body["by_stage"] == []
    assert body["alerts"] == []
    assert body["policy"]["max_tokens_per_day"] == 1_000_000
    assert body["policy"]["max_cost_usd_micros_per_day"] == 10_000_000
    # Real zero usage against a real cap — not a fabricated token count.
    assert body["policy"]["token_utilization"] == 0.0
    assert body["policy"]["cost_utilization"] == 0.0


def test_worker_autoscaling_http_empty_then_evaluate_does_not_provision(
    client, engine, monkeypatch
):
    """Unconfigured reads are 200-empty; evaluate must not provision workers."""
    monkeypatch.delenv("BOSS_INFRA_REAL_PROVISION_ALLOWED", raising=False)
    monkeypatch.setenv("BOSS_INFRA_REAL_PROVISION_ALLOWED", "false")
    from boss_engine.config import get_settings

    get_settings.cache_clear()

    empty_pid = SEEDED_PROJECT
    status = client.get(
        "/v1/worker-autoscaling/status", params={"project_id": empty_pid}
    )
    assert status.status_code == 200, status.text
    st = status.json()
    assert st["configured"] is False
    assert st["queue_depth"] == 0
    assert st["pools"] == [] or all(
        p.get("actual_workers") in (0, None) for p in st["pools"]
    )

    policies = client.get(
        "/v1/worker-autoscaling/policies", params={"project_id": empty_pid}
    )
    assert policies.status_code == 200, policies.text
    assert policies.json()["items"] == []
    assert policies.json()["count"] == 0

    history = client.get(
        "/v1/worker-autoscaling/history", params={"project_id": empty_pid}
    )
    assert history.status_code == 200, history.text
    assert history.json()["items"] == []

    pid = SEEDED_PROJECT
    pool_key = f"e2e-no-provision-{uuid4().hex[:8]}"
    upsert = client.put(
        "/v1/worker-autoscaling/policies",
        json={
            "project_id": pid,
            "actor": "ceo",
            "pool_key": pool_key,
            "enabled": True,
            "min_workers": 0,
            "max_workers": 6,
            "scale_up_threshold": 1,
            "cooldown_seconds": 0,
            "max_queue_depth_per_worker": 2,
            # Evaluate must not provision even for the local/container path.
            # Cloud/VM apply is separately gated by BOSS_INFRA_REAL_PROVISION_ALLOWED.
            "kind": "local",
        },
    )
    assert upsert.status_code == 200, upsert.text

    for index in range(5):
        created = client.post(
            "/v1/jobs",
            json={
                "project_id": pid,
                "kind": "plan.command",
                "payload": {"e2e_as": index},
                "priority": 50,
            },
        )
        assert created.status_code == 201, created.text

    evaluated = client.post(
        "/v1/worker-autoscaling/evaluate",
        json={"project_id": pid, "actor": "ceo", "pool_key": pool_key},
    )
    assert evaluated.status_code == 200, evaluated.text
    ev = evaluated.json()
    assert ev["infra_real_provision_allowed"] is False
    assert ev["queue"]["queue_depth"] >= 5
    one = ev["evaluations"][0]
    assert one["pool_key"] == pool_key
    assert one["needed_workers"] == 3
    assert one["desired_workers"] == 3
    assert one["action"] == "scale_up"
    assert one["actual_workers"] == 0

    with begin(engine) as connection:
        assert _count(connection, "worker_instances", pid) == 0

    events = client.get("/v1/events", params={"project_id": pid, "limit": 100})
    assert events.status_code == 200, events.text
    types = {item["event_type"] for item in events.json()["items"]}
    assert "elastic.capacity_requested" not in types
    assert "worker_autoscaling.applied" not in types

    # Missing-table GETs stay 200 empty after a drop; recreate for later tests.
    with begin(engine) as connection:
        _drop_if_exists(
            connection,
            "worker_autoscaling_history",
            "worker_autoscaling_policies",
        )
    dropped_status = client.get(
        "/v1/worker-autoscaling/status", params={"project_id": empty_pid}
    )
    assert dropped_status.status_code == 200, dropped_status.text
    dropped_policies = client.get(
        "/v1/worker-autoscaling/policies", params={"project_id": empty_pid}
    )
    assert dropped_policies.status_code == 200, dropped_policies.text
    dropped_history = client.get(
        "/v1/worker-autoscaling/history", params={"project_id": empty_pid}
    )
    assert dropped_history.status_code == 200, dropped_history.text
    with begin(engine) as connection:
        svc = WorkerAutoscalingService(get_settings())
        assert svc.ensure_tables(connection) is True


def test_jobs_http_dedupe_lease_heartbeat_and_reclaim(client, engine):
    """One dedupe_key → one job; exclusive lease; heartbeat; expired reclaim."""
    pid = SEEDED_PROJECT
    key = f"e2e-job-dedupe:{uuid4().hex}"

    first = client.post(
        "/v1/jobs",
        json={
            "project_id": pid,
            "kind": "plan.generate",
            "payload": {"step": 1},
            "dedupe_key": key,
        },
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/v1/jobs",
        json={
            "project_id": pid,
            "kind": "plan.generate",
            "payload": {"step": 2},
            "dedupe_key": key,
        },
    )
    assert second.status_code == 201, second.text
    job_id = first.json()["id"]
    assert second.json()["id"] == job_id

    listed = client.get("/v1/jobs", params={"project_id": pid})
    assert listed.status_code == 200, listed.text
    matching = [item for item in listed.json()["items"] if item["id"] == job_id]
    assert len(matching) == 1
    with begin(engine) as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM jobs "
                "WHERE project_id = :p AND dedupe_key = :k"
            ),
            {"p": pid, "k": key},
        ).scalar_one()
        assert int(count) == 1

    worker_a = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "e2e-worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert worker_a.status_code == 200, worker_a.text
    leased = worker_a.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id
    token = leased["job"]["lease_token"]
    first_expiry = leased["job"]["lease_expires_at"]

    worker_b = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "e2e-worker-b",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert worker_b.status_code == 200, worker_b.text
    assert worker_b.json()["status"] == "idle"
    assert worker_b.json()["job"] is None

    heartbeat = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "e2e-worker-a",
            "job_id": job_id,
            "lease_token": token,
            "lease_ttl_seconds": 90,
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["lease_expires_at"] != first_expiry

    stale = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "e2e-worker-a",
            "job_id": job_id,
            "lease_token": "not-the-lease-token",
            "lease_ttl_seconds": 60,
        },
    )
    assert stale.status_code == 409, stale.text

    with begin(engine) as connection:
        connection.execute(
            text(
                """
                UPDATE jobs
                SET lease_expires_at = :expires
                WHERE id = :job_id
                """
            ),
            {
                "job_id": job_id,
                "expires": datetime.now(timezone.utc) - timedelta(seconds=5),
            },
        )

    reclaim = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "e2e-worker-b",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert reclaim.status_code == 200, reclaim.text
    recovered = reclaim.json()
    assert recovered["status"] == "leased"
    assert recovered["job"]["id"] == job_id
    assert recovered["job"]["attempt"] == 2
    assert recovered["job"]["lease_token"] != token

    old_hb = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "e2e-worker-a",
            "job_id": job_id,
            "lease_token": token,
            "lease_ttl_seconds": 60,
        },
    )
    assert old_hb.status_code == 409, old_hb.text

    events = client.get("/v1/events", params={"project_id": pid, "limit": 100})
    assert events.status_code == 200, events.text
    types = {item["event_type"] for item in events.json()["items"]}
    assert "job.lease_reclaimed" in types
    assert "job.heartbeat" in types
