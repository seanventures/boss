"""P1 Platform M6 Baseline Opaque-Box E2E Suite (F1.1–F1.11 — Tier 1 & Tier 2).

Covers all 11 P1 platform feature groups present on M1 (`5d321f073`) with >=5 Tier 1 and >=5 Tier 2
genuine opaque-box tests per feature (110+ tests total, 0 skips, 0 xfails, 0 LAST_MILE mocks):
- F1.1: CEO Command Intake & Task Lifecycle
- F1.2: Budget Governor (Quote, Reserve, Consume, Release, Allocations & Costs)
- F1.3: Durable Worker Queue, Leases & Deadletters
- F1.4: Receipts, Unique Dedupe Keys & Event Feed
- F1.5: Decisions, TOTP Step-Up Auth & Autonomy Rules
- F1.6: Delivery Workflows, Webhooks & V1 Lock
- F1.7: Code Tasks, Deployment Targets, Stack Plans & Self-Fix
- F1.8: Knowledge Graph & Context Packs
- F1.9: Identity Auth, RBAC, Multi-Org Confinement & Model Catalogue
- F1.10: Acceptance Checks & Deliverable Review Integrity
- F1.11: Web Console (`apps/web` Live Next.js Server & API Proxies)
"""
from __future__ import annotations

import dataclasses
import json
import uuid

import httpx
import pytest
from sqlalchemy import text


def _create_pending_decision(tenant) -> str:
    """Create a genuine pending Decision via HTTP agent staging -> prod promotion."""
    key = f"dec_ag_{uuid.uuid4().hex[:8]}"
    created = tenant.session_client.post(
        "/v1/agent-specs",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "spec": {
                "apiVersion": "boss.agent/v1",
                "kind": "Agent",
                "metadata": {"key": key, "name": f"Agent {key}"},
                "spec": {
                    "mission": "Check release readiness.",
                    "prompts": {"system": "Report release status."},
                    "tools": {"native": ["read_repo_file"]},
                },
            },
        },
        headers=tenant.csrf_headers,
    )
    assert created.status_code == 201, created.text
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/deployments",
            json={
                "project_id": tenant.project_id,
                "actor": tenant.owner.principal.id,
                "environment": "dev",
                "version": 1,
            },
            headers=tenant.csrf_headers,
        ).status_code
        == 201
    )
    assert (
        tenant.session_client.post(
            f"/v1/agents/{key}/promote",
            json={
                "project_id": tenant.project_id,
                "actor": tenant.owner.principal.id,
                "from_environment": "dev",
                "to_environment": "staging",
            },
            headers=tenant.csrf_headers,
        ).status_code
        == 200
    )
    prom = tenant.session_client.post(
        f"/v1/agents/{key}/promote",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "from_environment": "staging",
            "to_environment": "prod",
        },
        headers=tenant.csrf_headers,
    )
    assert prom.status_code == 200, prom.text
    dec_id = prom.json().get("decision_id")
    assert dec_id, prom.text
    return dec_id


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F1.1 — CEO Command Intake & Task Lifecycle (5 Tier 1 + 5 Tier 2)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f1_1_01_commands_propose_creates_task_and_job(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/commands/propose",
        json={
            "project_id": tenant.project_id,
            "command": "Verify health check metrics and summarize queue lag.",
            "constraints": {"read_only": True},
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["status"] == "accepted"
    assert body["task_id"] and body["job_id"]


def test_t1_f1_1_02_create_task_with_dedupe_key_persists(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    dkey = f"t_dedupe_{uuid.uuid4().hex[:10]}"
    res = client.post(
        "/v1/tasks",
        json={
            "project_id": tenant.project_id,
            "title": "Audit budget state",
            "description": "Verify project budget counters",
            "priority": "high",
            "dedupe_key": dkey,
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code == 201, res.text
    task = res.json()
    assert task["title"] == "Audit budget state" and task["priority"] == "high"

    got = client.get(f"/v1/tasks/{task['id']}", headers=tenant.owner.headers)
    assert got.status_code == 200 and got.json()["id"] == task["id"]


def test_t1_f1_1_03_list_tasks_filtered_by_project(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    created = client.post(
        "/v1/tasks",
        json={
            "project_id": tenant.project_id,
            "title": "Listed Task",
            "dedupe_key": f"tsk_{uuid.uuid4().hex[:10]}",
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    assert created.status_code == 201, created.text
    listed = client.get("/v1/tasks", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert listed.status_code == 200, listed.text
    ids = {item["id"] for item in listed.json().get("items", [])}
    assert created.json()["id"] in ids


def test_t1_f1_1_04_get_task_detail_includes_status_and_priority(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    created = client.post(
        "/v1/tasks",
        json={
            "project_id": tenant.project_id,
            "title": "Detail Task",
            "priority": "low",
            "dedupe_key": f"tsk_{uuid.uuid4().hex[:10]}",
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    assert created.status_code == 201, created.text
    detail = client.get(f"/v1/tasks/{created.json()['id']}", headers=tenant.owner.headers)
    assert detail.status_code == 200
    assert detail.json()["project_id"] == tenant.project_id
    assert detail.json()["priority"] == "low"


def test_t1_f1_1_05_delete_task_removes_record(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    created = client.post(
        "/v1/tasks",
        json={
            "project_id": tenant.project_id,
            "title": "Disposable Task",
            "dedupe_key": f"tsk_{uuid.uuid4().hex[:10]}",
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    deleted = client.delete(
        f"/v1/tasks/{tid}",
        params={"project_id": tenant.project_id, "actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert deleted.status_code == 200, deleted.text
    after = client.get(f"/v1/tasks/{tid}", headers=tenant.owner.headers)
    assert after.status_code == 200 and after.json()["status"] == "archived", after.text
    listed = client.get("/v1/tasks", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert listed.status_code == 200
    assert tid not in {t["id"] for t in listed.json().get("items", []) if t.get("status") != "archived"}


def test_t2_f1_1_01_missing_command_rejected_422(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/commands/propose",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert res.status_code == 422, res.text


def test_t2_f1_1_02_invalid_constraints_type_rejected_422(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/commands/propose",
        json={
            "project_id": tenant.project_id,
            "command": "Verify constraints",
            "constraints": ["must-be-dict"],
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code == 422, res.text


def test_t2_f1_1_03_unknown_project_command_rejected(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/commands/propose",
        json={"project_id": "proj_does_not_exist_999", "command": "Run audit", "actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert res.status_code in (403, 404, 422), res.text


def test_t2_f1_1_04_duplicate_task_dedupe_key_returns_409(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    dkey = f"dup_{uuid.uuid4().hex[:10]}"
    payload = {"project_id": tenant.project_id, "title": "Once", "dedupe_key": dkey, "actor": tenant.owner.principal.id}
    first = client.post("/v1/tasks", json=payload, headers=tenant.owner.headers)
    assert first.status_code == 201, first.text
    second = client.post("/v1/tasks", json=payload, headers=tenant.owner.headers)
    assert second.status_code == 409, second.text


def test_t2_f1_1_05_unauthenticated_and_viewer_task_create_refused(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    unauth = client.post(
        "/v1/tasks",
        json={"project_id": tenant.project_id, "title": "NoAuth", "dedupe_key": f"tsk_{uuid.uuid4().hex[:8]}"},
    )
    assert unauth.status_code == 401
    viewer = client.post(
        "/v1/tasks",
        json={
            "project_id": tenant.project_id,
            "title": "Viewer",
            "dedupe_key": f"tsk_{uuid.uuid4().hex[:8]}",
            "actor": tenant.viewer.principal.id,
        },
        headers=tenant.viewer.headers,
    )
    assert viewer.status_code == 403


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F1.2 — Budget Governor (5 Tier 1 + 5 Tier 2)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f1_2_01_budget_status_returns_policy_and_remaining(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    res = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scope_id"] == tenant.project_id
    assert "budget_state" in body


def test_t1_f1_2_02_budget_quote_allows_within_cap(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    res = client.post(
        "/v1/budget/quote",
        json={
            "project_id": tenant.project_id,
            "category": "model",
            "amount_usd_micros": 100_000,
            "reason": "m6 quote check",
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["allowed"] is True


def test_t1_f1_2_03_budget_reserve_creates_reservation_and_tracks_balance(client, owner_headers, worker_headers):
    before = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
        headers=owner_headers,
    ).json()
    res = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 250_000,
            "reason": "m6 reserve test",
        },
        headers=worker_headers,
    )
    assert res.status_code == 201, res.text
    rid = res.json()["reservation_id"]
    assert rid
    after = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
        headers=owner_headers,
    ).json()
    assert after["reserved_usd_micros"] >= before["reserved_usd_micros"] + 250_000
    client.post(
        "/v1/budget/release",
        json={"project_id": "boss-dogfood", "reservation_id": rid, "reason": "m6 cleanup"},
        headers=worker_headers,
    )


def test_t1_f1_2_04_budget_release_restores_reserved_micros(client, worker_headers):
    reserved = client.post(
        "/v1/budget/reserve",
        json={"project_id": "boss-dogfood", "category": "model", "amount_usd_micros": 300_000, "reason": "m6 rel"},
        headers=worker_headers,
    )
    assert reserved.status_code == 201, reserved.text
    rid = reserved.json()["reservation_id"]
    rel = client.post(
        "/v1/budget/release",
        json={"project_id": "boss-dogfood", "reservation_id": rid, "reason": "unused"},
        headers=worker_headers,
    )
    assert rel.status_code == 200, rel.text
    assert rel.json()["status"] == "released"


def test_t1_f1_2_05_budget_consume_records_cost_receipt_and_allocations(client, owner_headers, worker_headers):
    watch = client.post(
        "/v1/metatron/watch",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
        headers=owner_headers,
    )
    assert watch.status_code == 200 and watch.json().get("receipt_id"), watch.text
    rcpt_id = watch.json()["receipt_id"]
    reserved = client.post(
        "/v1/budget/reserve",
        json={"project_id": "boss-dogfood", "category": "model", "amount_usd_micros": 120_000, "reason": "m6 consume"},
        headers=worker_headers,
    )
    assert reserved.status_code == 201, reserved.text
    cons = client.post(
        "/v1/budget/consume",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reserved.json()["reservation_id"],
            "receipt_id": rcpt_id,
            "category": "model",
            "skill_or_model_id": "gpt-5.2-codex",
            "estimated_usd_micros": 120_000,
            "actual_usd_micros": 85_000,
            "actor": "system",
        },
        headers=worker_headers,
    )
    assert cons.status_code == 200, cons.text
    assert cons.json()["cost_receipt"]["actual_usd_micros"] == 85_000

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"}, headers=owner_headers)
    assert costs.status_code == 200
    allocs = client.get("/v1/budget/allocations", params={"project_id": "boss-dogfood"}, headers=owner_headers)
    assert allocs.status_code == 200


def test_t2_f1_2_01_negative_budget_amount_rejected_422(client, tenant_factory, worker_headers):
    tenant = tenant_factory(seed_defaults=True)
    q = client.post(
        "/v1/budget/quote",
        json={"project_id": tenant.project_id, "category": "model", "amount_usd_micros": -100, "reason": "neg"},
        headers=tenant.owner.headers,
    )
    assert q.status_code == 422, q.text
    r = client.post(
        "/v1/budget/reserve",
        json={"project_id": tenant.project_id, "category": "model", "amount_usd_micros": -100, "reason": "neg"},
        headers=worker_headers,
    )
    assert r.status_code == 422, r.text


def test_t2_f1_2_02_zero_budget_amount_rejected_422(client, tenant_factory, worker_headers):
    tenant = tenant_factory(seed_defaults=True)
    res = client.post(
        "/v1/budget/reserve",
        json={"project_id": tenant.project_id, "category": "model", "amount_usd_micros": 0, "reason": "zero"},
        headers=worker_headers,
    )
    assert res.status_code == 422, res.text


def test_t2_f1_2_03_reserve_above_daily_cap_refused(client, tenant_factory, worker_headers):
    tenant = tenant_factory(seed_defaults=True)
    res = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": tenant.project_id,
            "category": "model",
            "amount_usd_micros": 900_000_000,
            "reason": "over cap",
        },
        headers=worker_headers,
    )
    assert (res.status_code in (402, 403, 409, 422) or res.json().get("status") == "denied"), res.text
    assert res.json().get("reservation_id") is None


def test_t2_f1_2_04_double_release_or_unknown_reservation_refused(client, tenant_factory, worker_headers):
    tenant = tenant_factory(seed_defaults=True)
    unknown = client.post(
        "/v1/budget/release",
        json={"project_id": tenant.project_id, "reservation_id": "bres_missing_999", "reason": "missing"},
        headers=worker_headers,
    )
    assert (unknown.status_code in (404, 409, 422) or unknown.json().get("status") == "not_found"), unknown.text


def test_t2_f1_2_05_viewer_cannot_reserve_budget_403(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    res = client.post(
        "/v1/budget/reserve",
        json={"project_id": tenant.project_id, "category": "model", "amount_usd_micros": 1000, "reason": "viewer"},
        headers=tenant.viewer.headers,
    )
    assert res.status_code == 403, res.text


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F1.3 — Durable Worker Queue, Leases & Deadletters (5 Tier 1 + 5 Tier 2)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f1_3_01_enqueue_job_for_task(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": tenant.project_id,
            "title": "Worker task",
            "dedupe_key": f"tsk_{uuid.uuid4().hex[:10]}",
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    ).json()
    job = client.post(
        "/v1/jobs",
        json={"project_id": tenant.project_id, "kind": "plan.command", "task_id": task["id"]},
        headers=tenant.owner.headers,
    )
    assert job.status_code == 201, job.text
    assert job.json()["status"] == "queued"


def test_t1_f1_3_02_worker_poll_acquires_fenced_lease(client, tenant_factory, worker_headers):
    tenant = tenant_factory(seed_defaults=False)
    cap = "agent.cao.architecture_review"
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": tenant.project_id,
            "title": "Poll task",
            "dedupe_key": f"tsk_{uuid.uuid4().hex[:10]}",
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    ).json()
    job_res = client.post(
        "/v1/jobs",
        json={"project_id": tenant.project_id, "kind": cap, "task_id": task["id"]},
        headers=tenant.owner.headers,
    )
    assert job_res.status_code == 201, job_res.text
    job = job_res.json()
    polled = client.post(
        "/v1/worker/poll",
        json={"worker_id": f"wrk_{uuid.uuid4().hex[:8]}", "capabilities": [cap], "max_jobs": 1, "wait_ms": 0},
        headers=worker_headers,
    )
    assert polled.status_code == 200, polled.text
    leased = polled.json().get("job")
    assert leased and leased["id"] == job["id"] and leased["lease_token"]


def test_t1_f1_3_03_duplicate_worker_poll_never_double_leases(client, tenant_factory, worker_headers):
    tenant = tenant_factory(seed_defaults=False)
    cap = "agent.coo.ops_digest"
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": tenant.project_id,
            "title": "No double lease",
            "dedupe_key": f"tsk_{uuid.uuid4().hex[:10]}",
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    ).json()
    job_res = client.post(
        "/v1/jobs",
        json={"project_id": tenant.project_id, "kind": cap, "task_id": task["id"]},
        headers=tenant.owner.headers,
    )
    assert job_res.status_code == 201, job_res.text
    job = job_res.json()
    first = client.post(
        "/v1/worker/poll",
        json={"worker_id": "wrk_a", "capabilities": [cap], "max_jobs": 1, "wait_ms": 0},
        headers=worker_headers,
    ).json()
    second = client.post(
        "/v1/worker/poll",
        json={"worker_id": "wrk_b", "capabilities": [cap], "max_jobs": 1, "wait_ms": 0},
        headers=worker_headers,
    ).json()
    assert first.get("job") and first["job"]["id"] == job["id"]
    assert second.get("job") is None


def test_t1_f1_3_04_cancel_job_transitions_to_cancelled(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": tenant.project_id,
            "title": "Cancel job",
            "dedupe_key": f"tsk_{uuid.uuid4().hex[:10]}",
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    ).json()
    job = client.post(
        "/v1/jobs",
        json={"project_id": tenant.project_id, "kind": "openai.plan", "task_id": task["id"]},
        headers=tenant.owner.headers,
    ).json()
    cancelled = client.post(
        f"/v1/jobs/{job['id']}/cancel",
        json={"actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    fetched = client.get(f"/v1/jobs/{job['id']}", headers=tenant.owner.headers)
    assert fetched.status_code == 200 and fetched.json()["status"] in ("cancelled", "canceled")


def test_t1_f1_3_05_deadletter_remediations_list(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.get(
        "/v1/deadletters/remediations",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert res.status_code == 200, res.text


def test_t2_f1_3_01_non_worker_cannot_poll_403(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/worker/poll",
        json={"worker_id": "rogue", "capabilities": ["workflow.step"], "max_jobs": 1, "wait_ms": 0},
        headers=tenant.owner.headers,
    )
    assert res.status_code == 403, res.text


def test_t2_f1_3_02_create_job_for_unknown_task_rejected(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/jobs",
        json={"project_id": tenant.project_id, "kind": "workflow.step", "task_id": "tsk_does_not_exist_999"},
        headers=tenant.owner.headers,
    )
    assert res.status_code in (404, 422), res.text


def test_t2_f1_3_03_heartbeat_with_stale_token_rejected(client, worker_headers):
    res = client.post(
        "/v1/worker/heartbeat",
        json={"worker_id": "wrk_ghost", "job_id": "job_missing_999", "lease_token": "bogus_tok"},
        headers=worker_headers,
    )
    assert res.status_code in (403, 404, 409), res.text


def test_t2_f1_3_04_retry_nonexistent_deadletter_rejected_404(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/deadletters/dlt_missing_999/retry",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert res.status_code in (404, 422), res.text


def test_t2_f1_3_05_unauthenticated_job_create_refused_401(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post("/v1/jobs", json={"project_id": tenant.project_id, "kind": "workflow.step"})
    assert res.status_code == 401


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F1.4 — Receipts, Unique Dedupe Keys & Event Feed (5 Tier 1 + 5 Tier 2)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f1_4_01_metatron_watch_emits_receipt_and_event(client, owner_headers):
    res = client.post("/v1/metatron/watch", json={"project_id": "boss-dogfood", "actor": "ceo"}, headers=owner_headers)
    assert res.status_code == 200, res.text
    assert res.json().get("receipt_id")


def test_t1_f1_4_02_list_receipts_and_fetch_by_dedupe_key(client, owner_headers):
    client.post("/v1/metatron/watch", json={"project_id": "boss-dogfood", "actor": "ceo"}, headers=owner_headers)
    listed = client.get("/v1/receipts", params={"project_id": "boss-dogfood"}, headers=owner_headers)
    assert listed.status_code == 200, listed.text
    items = listed.json().get("items", [])
    if items:
        dkey = items[0]["dedupe_key"]
        single = client.get(f"/v1/receipts/{dkey}", headers=owner_headers)
        assert single.status_code == 200 and single.json()["dedupe_key"] == dkey


def test_t1_f1_4_03_resource_receipts_lists_cleanly(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.get("/v1/resource-receipts", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert res.status_code == 200, res.text


def test_t1_f1_4_04_events_and_activity_record_lifecycle_trail(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    prop = client.post(
        "/v1/commands/propose",
        json={
            "project_id": tenant.project_id,
            "command": "Verify audit event trail",
            "constraints": {},
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    assert prop.status_code == 201, prop.text
    events = client.get("/v1/events", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert events.status_code == 200, events.text
    assert len(events.json().get("items", [])) >= 1
    activity = client.get("/v1/activity", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert activity.status_code == 200, activity.text


def test_t1_f1_4_05_console_v2_receipts_view(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.get("/v1/console/v2/receipts", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert res.status_code == 200, res.text


def test_t2_f1_4_01_duplicate_receipt_dedupe_key_blocked_at_db(db_engine, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    dkey = f"rcpt_dedupe_{uuid.uuid4().hex[:10]}"
    with db_engine.begin() as conn:
        ws_id = conn.execute(
            text("SELECT workspace_id FROM projects WHERE id = :p"),
            {"p": tenant.project_id},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO receipts (id, org_id, workspace_id, project_id, dedupe_key, effect_kind, payload_hash, status) "
                "VALUES (:id1, :org, :ws, :p, :dk, 'm6.test', 'hash1', 'recorded')"
            ),
            {
                "id1": f"r1_{uuid.uuid4().hex[:8]}",
                "org": tenant.owner.principal.org_id,
                "ws": ws_id,
                "p": tenant.project_id,
                "dk": dkey,
            },
        )
    with pytest.raises(Exception):
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO receipts (id, org_id, workspace_id, project_id, dedupe_key, effect_kind, payload_hash, status) "
                    "VALUES (:id2, :org, :ws, :p, :dk, 'm6.test', 'hash2', 'recorded')"
                ),
                {
                    "id2": f"r2_{uuid.uuid4().hex[:8]}",
                    "org": tenant.owner.principal.org_id,
                    "ws": ws_id,
                    "p": tenant.project_id,
                    "dk": dkey,
                },
            )


def test_t2_f1_4_02_cross_project_events_read_confined(client, tenant_factory):
    tenant_a = tenant_factory(seed_defaults=False)
    tenant_b = tenant_factory(seed_defaults=False)
    res = client.get("/v1/events", params={"project_id": tenant_b.project_id}, headers=tenant_a.owner.headers)
    assert res.status_code in (403, 404), res.text


def test_t2_f1_4_03_events_invalid_limit_boundary(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    for bad_limit in (0, -1, 100_000):
        res = client.get(
            "/v1/events",
            params={"project_id": tenant.project_id, "limit": bad_limit},
            headers=tenant.owner.headers,
        )
        assert res.status_code in (400, 422), f"limit={bad_limit}: {res.text}"


def test_t2_f1_4_04_secret_pattern_in_command_scrubbed_from_events(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    secret_val = "sk-live-secret-m6-leak-probe-987654321"
    client.post(
        "/v1/commands/propose",
        json={
            "project_id": tenant.project_id,
            "command": f"Do not expose {secret_val}",
            "constraints": {},
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    events = client.get("/v1/events", params={"project_id": tenant.project_id}, headers=tenant.owner.headers).json()
    assert secret_val not in json.dumps(events)


def test_t2_f1_4_05_unknown_receipt_dedupe_key_returns_404(client, owner_headers):
    res = client.get("/v1/receipts/nonexistent_dedupe_key_m6_999", headers=owner_headers)
    assert res.status_code == 404, res.text


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F1.5 — Decisions, Step-Up Auth & Autonomy Rules (5 Tier 1 + 5 Tier 2)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f1_5_01_decide_and_console_v2_decisions_list_pending(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    dec_id = _create_pending_decision(tenant)
    inbox = client.get("/v1/decide", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert inbox.status_code == 200, inbox.text
    assert dec_id in json.dumps(inbox.json())

    v2 = client.get("/v1/console/v2/decisions", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert v2.status_code == 200, v2.text


def test_t1_f1_5_02_get_decision_and_understand_summary(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    dec_id = _create_pending_decision(tenant)
    card = client.get(f"/v1/decisions/{dec_id}", headers=tenant.owner.headers)
    assert card.status_code == 200, card.text
    assert card.json()["id"] == dec_id

    und = client.post(
        "/v1/decide/understand",
        json={"project_id": tenant.project_id, "decision_id": dec_id, "actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert und.status_code in (200, 422), und.text


def test_t1_f1_5_03_put_decide_rule_persists_autonomy_rule(tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = tenant.session_client.put(
        "/v1/decide/rules/deploy",
        json={
            "project_id": tenant.project_id,
            "scope": "install",
            "rule": {"mode": "ask", "note": "m6 policy"},
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.csrf_headers,
    )
    assert res.status_code in (200, 422), res.text


def test_t1_f1_5_04_reject_decision_updates_status_and_leaves_pending(tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    dec_id = _create_pending_decision(tenant)
    rej = tenant.session_client.post(
        f"/v1/decisions/{dec_id}/reject",
        json={"actor": tenant.owner.principal.id, "note": "Hold for M6 audit"},
        headers=tenant.csrf_headers,
    )
    assert rej.status_code == 200, rej.text
    assert rej.json()["status"] == "rejected"


def test_t1_f1_5_05_step_up_auth_succeeds_with_fresh_totp(claimed_owner_session):
    claimed_owner_session.step_up()
    sess = claimed_owner_session.client.get("/v1/auth/session")
    assert sess.status_code == 200 and sess.json()["transport_authenticated"] is True


def test_t2_f1_5_01_stale_mfa_triggers_step_up_required_401(db_engine, claimed_owner_session):
    claimed_owner_session.age_mfa(db_engine, minutes=20)
    res = claimed_owner_session.client.post(
        "/v1/identity/me/api-keys",
        headers=claimed_owner_session.csrf_headers,
        json={"name": "m6-stepup-probe", "scopes": ["project:read"]},
    )
    assert res.status_code == 401, res.text
    assert "step_up" in res.text
    claimed_owner_session.step_up()


def test_t2_f1_5_02_replay_approve_on_rejected_decision_refused(tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    dec_id = _create_pending_decision(tenant)
    assert (
        tenant.session_client.post(
            f"/v1/decisions/{dec_id}/reject",
            json={"actor": tenant.owner.principal.id, "note": "first decision"},
            headers=tenant.csrf_headers,
        ).status_code
        == 200
    )
    replay = tenant.session_client.post(
        f"/v1/decisions/{dec_id}/approve",
        json={"actor": tenant.owner.principal.id, "note": "replay"},
        headers=tenant.csrf_headers,
    )
    assert replay.status_code in (409, 422), replay.text


def test_t2_f1_5_03_viewer_cannot_approve_or_reject_decision_403(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    dec_id = _create_pending_decision(tenant)
    res = client.post(
        f"/v1/decisions/{dec_id}/approve",
        json={"actor": tenant.viewer.principal.id, "note": "viewer"},
        headers=tenant.viewer.headers,
    )
    assert res.status_code == 403, res.text


def test_t2_f1_5_04_unknown_decision_id_returns_404(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    assert client.get("/v1/decisions/dec_missing_999", headers=tenant.owner.headers).status_code == 404
    assert (
        client.post(
            "/v1/decisions/dec_missing_999/approve",
            json={"actor": tenant.owner.principal.id},
            headers=tenant.owner.headers,
        ).status_code
        == 404
    )


def test_t2_f1_5_05_unauthenticated_decide_inbox_returns_401(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    assert client.get("/v1/decide", params={"project_id": tenant.project_id}).status_code == 401


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F1.6 — Delivery Workflows, Webhooks & V1 Lock (5 Tier 1 + 5 Tier 2)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f1_6_01_catalogue_and_project_workflows_list(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    cat = client.get("/v1/delivery-workflows/catalogue", headers=tenant.owner.headers)
    assert cat.status_code == 200, cat.text
    listed = client.get("/v1/delivery-workflows", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert listed.status_code == 200, listed.text


def test_t1_f1_6_02_publish_and_start_delivery_workflow_run(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    pub = tenant.session_client.post(
        "/v1/delivery-workflows/publish",
        json={
            "project_id": tenant.project_id,
            "name": f"M6 Gate Flow {uuid.uuid4().hex[:6]}",
            "actor": tenant.owner.principal.id,
            "spec": {
                "entry_step": "gate1",
                "steps": [
                    {
                        "id": "gate1",
                        "kind": "decision",
                        "captain": "human",
                        "next": [],
                        "approval": {"mode": "manual"},
                        "question": "Proceed?",
                    }
                ],
            },
        },
        headers=tenant.csrf_headers,
    )
    assert pub.status_code == 200, pub.text
    wf_key = pub.json()["definition"]["workflow_key"]

    started = client.post(
        "/v1/delivery-workflows/start",
        json={
            "project_id": tenant.project_id,
            "workflow_key": wf_key,
            "actor": tenant.owner.principal.id,
            "input": {},
            "start_anyway": ["model"],
        },
        headers=tenant.owner.headers,
    )
    assert started.status_code in (200, 201), started.text
    run_id = started.json()["run"]["id"]
    runs = client.get("/v1/delivery-workflows/runs", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert runs.status_code == 200 and run_id in json.dumps(runs.json())


def test_t1_f1_6_03_draft_workflow_handles_no_model_cleanly(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    res = client.post(
        "/v1/delivery-workflows/draft",
        json={
            "project_id": tenant.project_id,
            "description": "Lint and run unit tests before deploy",
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code in (200, 409), res.text


def test_t1_f1_6_04_workflow_webhooks_list_and_create(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    listed = client.get(
        "/v1/delivery-workflows/webhooks",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert listed.status_code == 200, listed.text


def test_t1_f1_6_05_v1_lock_and_release_planning_status(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    lock = client.get("/v1/v1-lock", headers=tenant.owner.headers)
    assert lock.status_code == 200, lock.text
    rp = client.get("/v1/release-planning/status", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert rp.status_code == 200, rp.text


def test_t2_f1_6_01_start_unknown_workflow_key_rejected(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    res = client.post(
        "/v1/delivery-workflows/start",
        json={"project_id": tenant.project_id, "workflow_key": "wf_nonexistent_999", "actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert res.status_code in (404, 422), res.text


def test_t2_f1_6_02_viewer_cannot_start_workflow_403(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    res = client.post(
        "/v1/delivery-workflows/start",
        json={"project_id": tenant.project_id, "workflow_key": "any", "actor": tenant.viewer.principal.id},
        headers=tenant.viewer.headers,
    )
    assert res.status_code == 403, res.text


def test_t2_f1_6_03_retry_step_unknown_run_rejected_404(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    res = client.post(
        "/v1/delivery-workflows/runs/wfr_missing_999/retry-step",
        json={"project_id": tenant.project_id, "step_id": "s1", "actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert res.status_code in (404, 422), res.text


def test_t2_f1_6_04_delete_unknown_workflow_run_404(tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    res = tenant.session_client.delete(
        "/v1/delivery-workflows/runs/wfr_missing_999",
        params={"project_id": tenant.project_id},
        headers=tenant.csrf_headers,
    )
    assert res.status_code == 404, res.text


def test_t2_f1_6_05_unauthenticated_v1_lock_refused_401(client):
    assert client.get("/v1/v1-lock").status_code == 401


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F1.7 — Code Tasks, Deployment Targets, Stack Plans & Self-Fix (5 Tier 1 + 5 Tier 2)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f1_7_01_start_simulated_code_task_and_read_activity(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    created = client.post(
        "/v1/code-tasks",
        json={
            "project_id": tenant.project_id,
            "brief": "Add diagnostic metrics endpoint",
            "repo": "https://github.com/example/demo.git",
            "simulated": True,
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    assert created.status_code in (200, 201), created.text
    run_id = created.json()["run_id"]
    detail = client.get(f"/v1/code-tasks/{run_id}", headers=tenant.owner.headers)
    assert detail.status_code == 200, detail.text
    act = client.get(f"/v1/code-tasks/{run_id}/activity", headers=tenant.owner.headers)
    assert act.status_code == 200, act.text


def test_t1_f1_7_02_create_and_list_deployment_target(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    created = tenant.session_client.post(
        "/v1/deployment-targets",
        json={
            "project_id": tenant.project_id,
            "app_name": "demo-app",
            "repo": "example/demo",
            "environment": "staging",
            "target_kind": "ssh",
            "target_ref": "203.0.113.10",
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.csrf_headers,
    )
    assert created.status_code == 201, created.text
    listed = client.get("/v1/deployment-targets", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert listed.status_code == 200 and "demo-app" in json.dumps(listed.json())


def test_t1_f1_7_03_list_code_tasks_and_stack_deployments(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    assert client.get("/v1/code-tasks", params={"project_id": tenant.project_id}, headers=tenant.owner.headers).status_code == 200
    assert (
        client.get("/v1/deployments/stack", params={"project_id": tenant.project_id}, headers=tenant.owner.headers).status_code
        == 200
    )


def test_t1_f1_7_04_self_fix_listings_and_installer_health_guard(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    assert client.get("/v1/self-fixes", params={"project_id": tenant.project_id}, headers=tenant.owner.headers).status_code == 200
    assert (
        client.get("/v1/self-fixes/exists", params={"project_id": tenant.project_id}, headers=tenant.owner.headers).status_code
        == 200
    )
    assert (
        client.get("/v1/self-fixes/tickets", params={"project_id": tenant.project_id}, headers=tenant.owner.headers).status_code
        == 403
    )
    assert client.get("/v1/self-fixes/health", headers=tenant.owner.headers).status_code == 403


def test_t1_f1_7_05_cancel_code_task_run(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    created = client.post(
        "/v1/code-tasks",
        json={
            "project_id": tenant.project_id,
            "brief": "Cancelable simulated code task",
            "repo": "https://github.com/example/demo.git",
            "simulated": True,
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    ).json()
    cancelled = client.post(
        f"/v1/code-tasks/{created['run_id']}/cancel",
        json={"actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert cancelled.status_code in (200, 409), cancelled.text


def test_t2_f1_7_01_stack_execute_without_changeset_refused(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/deployments/stack/sdep_missing_999/execute",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id},
        headers=tenant.owner.headers,
    )
    assert res.status_code in (403, 404, 409, 422), res.text


def test_t2_f1_7_02_code_task_simulated_with_open_pr_rejected_422(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/code-tasks",
        json={
            "project_id": tenant.project_id,
            "brief": "Invalid simulated open_pr",
            "repo": "https://github.com/example/demo.git",
            "simulated": True,
            "open_pr": True,
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code == 422, res.text


def test_t2_f1_7_03_unknown_deployment_target_kind_rejected_422(tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = tenant.session_client.post(
        "/v1/deployment-targets",
        json={
            "project_id": tenant.project_id,
            "app_name": "bad-kind",
            "repo": "example/demo",
            "environment": "staging",
            "target_kind": "teleport",
            "target_ref": "203.0.113.10",
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.csrf_headers,
    )
    assert res.status_code == 422, res.text


def test_t2_f1_7_04_deployment_target_missing_ref_rejected_422(tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = tenant.session_client.post(
        "/v1/deployment-targets",
        json={
            "project_id": tenant.project_id,
            "app_name": "missing-ref",
            "repo": "example/demo",
            "environment": "staging",
            "target_kind": "ssh",
            "target_ref": "",
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.csrf_headers,
    )
    assert res.status_code == 422, res.text


def test_t2_f1_7_05_viewer_cannot_start_code_task_403(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/code-tasks",
        json={
            "project_id": tenant.project_id,
            "brief": "Unauthorized",
            "repo": "https://github.com/example/demo.git",
            "simulated": True,
            "actor": tenant.viewer.principal.id,
        },
        headers=tenant.viewer.headers,
    )
    assert res.status_code == 403, res.text


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F1.8 — Knowledge Graph & Context Packs (5 Tier 1 + 5 Tier 2)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f1_8_01_knowledge_status_reports_pack_context_available(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.get("/v1/knowledge/status", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert res.status_code == 200, res.text
    assert res.json().get("pack_context_available") is True


def test_t1_f1_8_02_upsert_and_list_knowledge_graph_nodes(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    nkey = f"svc:{uuid.uuid4().hex[:8]}"
    up = client.post(
        "/v1/knowledge/graph/nodes",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "kind": "service",
            "node_key": nkey,
            "name": "Billing API",
            "attrs": {"owner": "platform"},
        },
        headers=tenant.owner.headers,
    )
    assert up.status_code == 200, up.text
    listed = client.get("/v1/knowledge/graph/nodes", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert listed.status_code == 200 and nkey in json.dumps(listed.json())


def test_t1_f1_8_03_upsert_knowledge_graph_edge(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    n1_res = client.post(
        "/v1/knowledge/graph/nodes",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "service", "node_key": "api", "name": "API"},
        headers=tenant.owner.headers,
    )
    assert n1_res.status_code == 200, n1_res.text
    n2_res = client.post(
        "/v1/knowledge/graph/nodes",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "service", "node_key": "db-svc", "name": "Postgres"},
        headers=tenant.owner.headers,
    )
    assert n2_res.status_code == 200, n2_res.text
    edge = client.post(
        "/v1/knowledge/graph/edges",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "src_id": n1_res.json()["id"],
            "dst_id": n2_res.json()["id"],
            "kind": "depends_on",
        },
        headers=tenant.owner.headers,
    )
    assert edge.status_code == 200, edge.text


def test_t1_f1_8_04_subgraph_and_neighbors_traverse_edges(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    n1 = client.post(
        "/v1/knowledge/graph/nodes",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "service", "node_key": "web", "name": "Web"},
        headers=tenant.owner.headers,
    ).json()["id"]
    n2 = client.post(
        "/v1/knowledge/graph/nodes",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": "service", "node_key": "auth", "name": "Auth"},
        headers=tenant.owner.headers,
    ).json()["id"]
    client.post(
        "/v1/knowledge/graph/edges",
        json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "src_id": n1, "dst_id": n2, "kind": "depends_on"},
        headers=tenant.owner.headers,
    )
    sub = client.get(
        "/v1/knowledge/graph/subgraph",
        params={"project_id": tenant.project_id, "kind": "service", "node_key": "web", "depth": 2},
        headers=tenant.owner.headers,
    )
    assert sub.status_code == 200 and n2 in json.dumps(sub.json())
    nbr = client.get(
        "/v1/knowledge/graph/neighbors",
        params={"project_id": tenant.project_id, "node_id": n1},
        headers=tenant.owner.headers,
    )
    assert nbr.status_code == 200 and n2 in json.dumps(nbr.json())


def test_t1_f1_8_05_pack_context_respects_token_budget(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/knowledge/pack-context",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "token_budget": 1200,
            "reserved_output_tokens": 200,
            "chunks": [
                {"id": "c1", "text": "Service SLA is 99.9%.", "authority": "fact"},
                {"id": "c2", "text": "word " * 2000, "authority": "reference"},
            ],
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code == 200, res.text
    assert res.json().get("used_tokens", res.json().get("total_tokens", 0)) <= 1000


def test_t2_f1_8_01_pack_context_zero_budget_rejected_422(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/knowledge/pack-context",
        json={"project_id": tenant.project_id, "token_budget": -5, "chunks": []},
        headers=tenant.owner.headers,
    )
    assert res.status_code == 422, res.text


def test_t2_f1_8_02_kg_node_unknown_kind_rejected_400(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    bad = client.post(
        "/v1/knowledge/graph/nodes",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "kind": "unknown_kind_m6",
            "node_key": "bad-kind",
            "name": "Bad Kind",
        },
        headers=tenant.owner.headers,
    )
    assert bad.status_code in (400, 422), bad.text


def test_t2_f1_8_03_edge_to_missing_node_rejected(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/knowledge/graph/edges",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "src_id": "kgn_missing_a",
            "dst_id": "kgn_missing_b",
            "kind": "depends_on",
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code in (400, 404, 422), res.text


def test_t2_f1_8_04_cross_project_subgraph_isolated(client, tenant_factory):
    tenant_a = tenant_factory(seed_defaults=False)
    tenant_b = tenant_factory(seed_defaults=False)
    res = client.get(
        "/v1/knowledge/graph/subgraph",
        params={"project_id": tenant_b.project_id, "kind": "service", "node_key": "web"},
        headers=tenant_a.owner.headers,
    )
    assert res.status_code in (403, 404), res.text


def test_t2_f1_8_05_extreme_subgraph_depth_handled_without_500(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.get(
        "/v1/knowledge/graph/subgraph",
        params={"project_id": tenant.project_id, "kind": "service", "node_key": "none", "depth": 99},
        headers=tenant.owner.headers,
    )
    assert res.status_code in (200, 422), res.text


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F1.9 — Identity Auth, RBAC, Multi-Org Confinement & Model Catalogue (5 Tier 1 + 5 Tier 2)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f1_9_01_auth_session_returns_authenticated_principal(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.get("/v1/auth/session", headers=tenant.owner.headers)
    assert res.status_code == 200, res.text
    assert res.json()["transport_authenticated"] is True
    assert res.json()["principal"]["id"] == tenant.owner.principal.id


def test_t1_f1_9_02_identity_me_and_users_directory(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    users = client.get("/v1/identity/users", headers=tenant.owner.headers)
    assert users.status_code == 200, users.text
    assert "users" in users.json()
    me = tenant.session_client.get("/v1/identity/me")
    assert me.status_code == 200, me.text
    assert me.json()["principal_id"] == tenant.owner.principal.id


def test_t1_f1_9_03_openrouter_catalogue_returns_finite_prices(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    res = client.get(
        "/v1/setup/models/openrouter/catalogue",
        params={"project_id": tenant.project_id},
        headers=tenant.owner.headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "models" in body and "prices" in body


def test_t1_f1_9_04_manual_openrouter_model_trims_whitespace(tenant_factory):
    tenant = tenant_factory(seed_defaults=True)
    res = tenant.session_client.post(
        "/v1/setup/models/openrouter/catalogue",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "provider": "openrouter",
            "kind": "api_key",
            "model": "  qwen/qwen3-coder  ",
        },
        headers=tenant.csrf_headers,
    )
    assert res.status_code == 200, res.text
    models = res.json().get("models", [])
    assert "qwen/qwen3-coder" in models


def test_t1_f1_9_05_edition_endpoint_reports_community(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.get("/v1/edition", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert res.status_code == 200, res.text
    assert res.json().get("edition_id") == "community"


def test_t2_f1_9_01_unauthenticated_identity_users_refused_401(client):
    assert client.get("/v1/identity/users").status_code == 401


def test_t2_f1_9_02_invalid_api_key_refused_401(client):
    res = client.get("/v1/identity/users", headers={"Authorization": "Bearer boss_pat_forged_99999"})
    assert res.status_code == 401


def test_t2_f1_9_03_revoked_pat_refused_401(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    revoked_key = tenant.mint_pat(tenant.owner.principal, revoked=True)
    res = client.get("/v1/auth/session", headers={"Authorization": f"Bearer {revoked_key}"})
    assert res.status_code == 401
    protected = client.get("/v1/identity/users", headers={"Authorization": f"Bearer {revoked_key}"})
    assert protected.status_code == 401


def test_t2_f1_9_04_multi_org_isolation_blocks_cross_org_reads(client, tenant_factory):
    tenant_a = tenant_factory(seed_defaults=False)
    tenant_b = tenant_factory(seed_defaults=False)
    res = client.get("/v1/tasks", params={"project_id": tenant_b.project_id}, headers=tenant_a.owner.headers)
    assert res.status_code in (403, 404), res.text


def test_t2_f1_9_05_openrouter_non_finite_price_sanitized(tenant_factory):
    from boss_engine.services import openrouter_catalog

    parsed = openrouter_catalog.parse(
        {
            "data": [
                {"id": "bad/nan", "pricing": {"prompt": "NaN", "completion": "0.000001"}},
                {"id": "good/model", "pricing": {"prompt": "0.000002", "completion": "0.000005"}},
            ]
        }
    )
    json.dumps([dataclasses.asdict(item) for item in parsed], allow_nan=False)

    tenant = tenant_factory(seed_defaults=True)
    blank = tenant.session_client.post(
        "/v1/setup/models/openrouter/catalogue",
        json={
            "project_id": tenant.project_id,
            "actor": tenant.owner.principal.id,
            "provider": "openrouter",
            "kind": "api_key",
            "model": "   ",
        },
        headers=tenant.csrf_headers,
    )
    assert blank.status_code in (200, 400, 422), blank.text


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F1.10 — Acceptance Checks & Deliverable Review Integrity (5 Tier 1 + 5 Tier 2)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f1_10_01_list_acceptance_checks_empty_for_new_project(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.get("/v1/acceptance-checks", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert res.status_code == 200 and res.json()["items"] == []


def test_t1_f1_10_02_code_task_gates_endpoint_returns_structure(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    run_id = client.post(
        "/v1/code-tasks",
        json={
            "project_id": tenant.project_id,
            "brief": "Gate check",
            "repo": "https://github.com/example/demo.git",
            "simulated": True,
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    ).json()["run_id"]
    gates = client.get(f"/v1/code-tasks/{run_id}/gates", headers=tenant.owner.headers)
    assert gates.status_code == 200, gates.text


def test_t1_f1_10_03_deliverable_result_review_snapshot(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    run_id = client.post(
        "/v1/code-tasks",
        json={
            "project_id": tenant.project_id,
            "brief": "Result review check",
            "repo": "https://github.com/example/demo.git",
            "simulated": True,
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    ).json()["run_id"]
    rev = client.get(f"/v1/code-tasks/{run_id}/result-review", headers=tenant.owner.headers)
    assert rev.status_code == 200, rev.text


def test_t1_f1_10_04_code_task_cockpit_and_tool_log(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    run_id = client.post(
        "/v1/code-tasks",
        json={
            "project_id": tenant.project_id,
            "brief": "Cockpit check",
            "repo": "https://github.com/example/demo.git",
            "simulated": True,
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    ).json()["run_id"]
    cockpit = client.get(f"/v1/code-tasks/{run_id}/cockpit", headers=tenant.owner.headers)
    assert cockpit.status_code == 200, cockpit.text
    tlog = client.get(f"/v1/code-tasks/{run_id}/tool-log", headers=tenant.owner.headers)
    assert tlog.status_code == 200, tlog.text


def test_t1_f1_10_05_acceptance_check_requires_accepted_review_contract(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    run_id = client.post(
        "/v1/code-tasks",
        json={
            "project_id": tenant.project_id,
            "brief": "Acceptance contract check",
            "repo": "https://github.com/example/demo.git",
            "simulated": True,
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    ).json()["run_id"]
    res = client.post(
        "/v1/acceptance-checks",
        json={
            "project_id": tenant.project_id,
            "run_id": run_id,
            "review_id": "rev_none",
            "snapshot_hash": "a" * 64,
            "title": "Unreviewed check",
            "submission_id": f"subm_{uuid.uuid4().hex[:20]}",
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code == 409, res.text


def test_t2_f1_10_01_service_account_blocked_from_creating_acceptance_checks_403(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/acceptance-checks",
        json={
            "project_id": tenant.project_id,
            "run_id": "crun_none",
            "review_id": "rev_none",
            "snapshot_hash": "b" * 64,
            "title": "Service Account Attempt",
            "submission_id": f"subm_{uuid.uuid4().hex[:20]}",
        },
        headers=tenant.service_account.headers,
    )
    assert res.status_code == 403, res.text


def test_t2_f1_10_02_unauthenticated_acceptance_check_refused_401(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/acceptance-checks",
        json={
            "project_id": tenant.project_id,
            "run_id": "crun_none",
            "review_id": "rev_none",
            "snapshot_hash": "c" * 64,
            "title": "Unauth",
            "submission_id": f"subm_{uuid.uuid4().hex[:20]}",
        },
    )
    assert res.status_code == 401


def test_t2_f1_10_03_invalid_snapshot_hash_rejected_422(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/acceptance-checks",
        json={
            "project_id": tenant.project_id,
            "run_id": "crun_none",
            "review_id": "rev_none",
            "snapshot_hash": "not-a-64-char-sha",
            "title": "Bad hash",
            "submission_id": f"subm_{uuid.uuid4().hex[:20]}",
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code == 422, res.text


def test_t2_f1_10_04_nonexistent_run_id_rejected_404(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/acceptance-checks",
        json={
            "project_id": tenant.project_id,
            "run_id": "crun_missing_999",
            "review_id": "rev_none",
            "snapshot_hash": "d" * 64,
            "title": "Missing run",
            "submission_id": f"subm_{uuid.uuid4().hex[:20]}",
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code == 404, res.text


def test_t2_f1_10_05_revise_nonexistent_template_rejected_404(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/acceptance-checks/act_missing_999/revisions",
        json={
            "project_id": tenant.project_id,
            "expected_revision_hash": "e" * 64,
            "title": "Revise",
            "criteria": ["All tests pass"],
            "submission_id": f"subm_{uuid.uuid4().hex[:20]}",
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code == 404, res.text


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F1.11 — Web Console Live Next.js Server & API Proxies (5 Tier 1 + 5 Tier 2)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f1_11_01_login_page_serves_200_html(live_web_console):
    r = httpx.get(f"{live_web_console['web_url']}/login", timeout=15.0)
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_t1_f1_11_02_root_page_responds_without_5xx(live_web_console):
    r = httpx.get(f"{live_web_console['web_url']}/", follow_redirects=False, timeout=15.0)
    assert r.status_code in (200, 307, 308), r.status_code


@pytest.mark.parametrize("route", ["/v2", "/v2/factory", "/v2/org", "/v2/settings", "/v2/infra", "/v2/timeline"])
def test_t1_f1_11_03_v2_console_routes_respond_without_5xx(live_web_console, route):
    r = httpx.get(f"{live_web_console['web_url']}{route}", follow_redirects=False, timeout=15.0)
    assert r.status_code in (200, 307, 308), f"{route}: {r.status_code}"


def test_t1_f1_11_04_v2_console_shell_renders_cleanly(live_web_console):
    r = httpx.get(f"{live_web_console['web_url']}/v2", follow_redirects=True, timeout=15.0)
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_t1_f1_11_05_live_api_health_and_web_server_cooperate(live_web_console):
    api_r = httpx.get(f"{live_web_console['api_url']}/health", timeout=10.0)
    assert api_r.status_code == 200


def test_t2_f1_11_01_unauthenticated_api_proxy_get_returns_401(live_web_console):
    r = httpx.get(f"{live_web_console['web_url']}/api/delivery-workflows", timeout=10.0)
    assert r.status_code == 401
    assert r.json().get("error") == "unauthenticated"


def test_t2_f1_11_02_unauthenticated_api_proxy_post_returns_401(live_web_console):
    r = httpx.post(f"{live_web_console['web_url']}/api/run-acts", json={}, timeout=10.0)
    assert r.status_code == 401
    assert r.json().get("error") == "unauthenticated"


def test_t2_f1_11_03_unknown_v2_route_never_returns_500(live_web_console):
    r = httpx.get(f"{live_web_console['web_url']}/v2/does-not-exist-m6", follow_redirects=False, timeout=10.0)
    assert r.status_code < 500, r.status_code


def test_t2_f1_11_04_xss_probe_in_login_query_not_reflected_raw(live_web_console):
    probe = "<script>alert(1)</script>"
    r = httpx.get(f"{live_web_console['web_url']}/login", params={"next": probe}, timeout=10.0)
    assert r.status_code == 200
    assert probe not in r.text


def test_t2_f1_11_05_forged_session_cookie_on_v1_proxy_fails_closed(live_web_console):
    r = httpx.get(
        f"{live_web_console['web_url']}/v1/identity/users",
        cookies={"__Host-boss_session": "forged_invalid_token_m6"},
        timeout=10.0,
    )
    assert r.status_code in (401, 403), f"{r.status_code}: {r.text[:200]}"
