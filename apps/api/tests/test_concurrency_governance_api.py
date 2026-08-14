"""API integration tests for /v1/concurrency (requires Postgres)."""

from __future__ import annotations

PROJECT_ID = "boss-dogfood"


def test_concurrency_policy_via_app(client):
    r = client.get("/v1/concurrency/policy")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy"]["max_concurrent_vms_per_host"] >= 1
    assert body["policy"]["prefer_provider_rate_limits"] is True


def test_concurrency_status_aggregates(client):
    # Seed a few queued jobs for queue_depth
    for i in range(3):
        created = client.post(
            "/v1/jobs",
            json={
                "project_id": PROJECT_ID,
                "kind": "plan.command",
                "payload": {"concurrency_gov": i},
                "priority": 50,
            },
        )
        assert created.status_code == 201, created.text

    r = client.get(
        "/v1/concurrency/status",
        params={"project_id": PROJECT_ID, "include_sandbox_host": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == PROJECT_ID
    assert body["queue"]["queue_depth"] >= 3
    assert "lease_contention" in body
    assert "rate_limit_windows" in body
    assert body["rate_limit_windows"]["scope"] == "process_local"
    assert "backpressure" in body
    assert body["backpressure"]["level"] in {"none", "low", "medium", "high"}
    assert body["vm_pool_density"]["policy_ceiling"] >= 1
    assert "policy" in body
    assert body["related_apis"]["worker_autoscaling"].startswith("/v1/")


def test_safety_branch_protection_mounted(client):
    r = client.get("/v1/safety/branch-protection/checklist")
    assert r.status_code == 200, r.text
    assert len(r.json()["required_checks"]) >= 5

    v = client.get(
        "/v1/safety/branch-protection",
        params={
            "owner": "seanheiney",
            "repo": "boss-sandbox",
            "branch": "main",
            "accept_simulated": False,
        },
    )
    assert v.status_code == 200, v.text
    # Fail closed without real protection / simulated accept
    assert v.json()["live_demo_ready"] is False
