"""Domain E2E: core control plane against live Postgres via FastAPI TestClient.

Exercises real handlers (no unit mocks) for:
  - POST /v1/commands/propose
  - GET/POST /v1/tasks, GET/POST /v1/jobs
  - GET /v1/decisions/inbox
  - POST /v1/budget/quote, /v1/budget/reserve, GET /v1/budget/status
  - GET /v1/events, GET /v1/receipts

Policy denials and validation errors must be 4xx, never 5xx.
"""

from __future__ import annotations

import uuid


PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"

# Acceptable non-5xx outcomes for control-plane paths
OK = {200, 201}
CLIENT = {400, 401, 403, 404, 409, 422, 429}
ALLOW = OK | CLIENT


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def test_budget_status_quote_and_reserve(client):
    """Budget status uses scope_type/scope_id; quote/reserve are spend gates."""
    status = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": PROJECT_ID},
    )
    assert status.status_code in ALLOW, status.text
    if status.status_code == 200:
        body = status.json()
        assert body.get("scope_type") == "project"
        assert body.get("scope_id") == PROJECT_ID
        assert "budget_state" in body

    # Wrong query shape must fail closed (422), not 500
    wrong = client.get("/v1/budget/status", params={"project_id": PROJECT_ID})
    assert wrong.status_code in CLIENT, wrong.text
    assert wrong.status_code < 500

    uid = _uid()
    quote = client.post(
        "/v1/budget/quote",
        json={
            "project_id": PROJECT_ID,
            "category": "tool",
            "amount_usd_micros": 40_000,
            "reason": f"e2e-core quote {uid}",
        },
    )
    assert quote.status_code in ALLOW, quote.text
    assert quote.status_code < 500
    if quote.status_code == 200:
        q = quote.json()
        assert "allowed" in q
        assert q.get("category") == "tool"
        assert "budget_state" in q

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": PROJECT_ID,
            "category": "tool",
            "amount_usd_micros": 40_000,
            "reason": f"e2e-core reserve {uid}",
        },
    )
    assert reserve.status_code in ALLOW, reserve.text
    assert reserve.status_code < 500
    if reserve.status_code in OK:
        r = reserve.json()
        assert r.get("status") in ("reserved", "pending_approval", "denied")
        # When reserved, reservation_id is required for later consume/release
        if r.get("status") == "reserved":
            assert r.get("reservation_id")
            assert r["reservation_id"].startswith("res_")


def test_commands_propose_creates_task_and_job(client):
    uid = _uid()
    event_id = f"evt-core-cmd-{uid}"
    response = client.post(
        "/v1/commands/propose",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "command": f"E2E core control plane command {uid} — list tasks only",
            "constraints": {
                "source": "test_e2e_core_control_plane",
                "event_id": event_id,
                "dedupe_key": f"cmd-dedupe-{uid}",
            },
        },
    )
    assert response.status_code in ALLOW, response.text
    assert response.status_code < 500
    if response.status_code in OK:
        body = response.json()
        assert body.get("status") == "accepted"
        assert body.get("task_id")
        assert body.get("job_id")
        # Proposed work should be visible on task and job lists
        tasks = client.get("/v1/tasks", params={"project_id": PROJECT_ID, "limit": 50})
        assert tasks.status_code == 200, tasks.text
        task_ids = {item.get("id") for item in tasks.json().get("items") or []}
        assert body["task_id"] in task_ids

        jobs = client.get("/v1/jobs", params={"project_id": PROJECT_ID, "limit": 50})
        assert jobs.status_code == 200, jobs.text
        job_ids = {item.get("id") for item in jobs.json().get("items") or []}
        assert body["job_id"] in job_ids


def test_tasks_list_create_and_jobs_enqueue(client):
    uid = _uid()
    dedupe_key = f"e2e-core-task-{uid}"

    listed = client.get(
        "/v1/tasks", params={"project_id": PROJECT_ID, "limit": 20}
    )
    assert listed.status_code in ALLOW, listed.text
    assert listed.status_code < 500
    if listed.status_code == 200:
        assert "items" in listed.json()

    task = client.post(
        "/v1/tasks",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "title": f"e2e core task {uid}",
            "priority": "p2",
            "dedupe_key": dedupe_key,
            "inputs": {"event_id": f"evt-task-{uid}"},
        },
    )
    assert task.status_code in ALLOW, task.text
    assert task.status_code < 500
    if task.status_code not in OK:
        return

    task_id = task.json()["id"]
    assert task_id.startswith("tsk_")

    # Duplicate dedupe_key must fail closed with 4xx (409 conflict), never 500
    again = client.post(
        "/v1/tasks",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "title": f"e2e core task {uid} again",
            "priority": "p2",
            "dedupe_key": dedupe_key,
        },
    )
    assert again.status_code in (400, 409, 422), again.text
    assert again.status_code < 500

    job = client.post(
        "/v1/jobs",
        json={
            "project_id": PROJECT_ID,
            "task_id": task_id,
            "kind": "agent_run",
            "payload": {
                "source": "test_e2e_core_control_plane",
                "event_id": f"evt-job-{uid}",
            },
        },
    )
    assert job.status_code in OK, job.text
    body = job.json()
    assert body.get("id")
    assert body.get("status") in ("queued", "leased", "running")
    assert body.get("task_id") == task_id

    jobs = client.get(
        "/v1/jobs",
        params={"project_id": PROJECT_ID, "task_id": task_id, "limit": 20},
    )
    assert jobs.status_code == 200, jobs.text
    ids = {item.get("id") for item in jobs.json().get("items") or []}
    assert body["id"] in ids

    # Unknown task must fail closed with 4xx
    bad = client.post(
        "/v1/jobs",
        json={
            "project_id": PROJECT_ID,
            "task_id": f"tsk_does_not_exist_{uid}",
            "kind": "agent_run",
            "payload": {"event_id": f"evt-bad-{uid}"},
        },
    )
    assert bad.status_code in CLIENT, bad.text
    assert bad.status_code < 500


def test_decisions_inbox_events_and_receipts(client):
    inbox = client.get(
        "/v1/decisions/inbox", params={"project_id": PROJECT_ID}
    )
    assert inbox.status_code in ALLOW, inbox.text
    assert inbox.status_code < 500
    if inbox.status_code == 200:
        body = inbox.json()
        assert body.get("project_id") == PROJECT_ID
        assert isinstance(body.get("items"), list)

    events = client.get(
        "/v1/events",
        params={"project_id": PROJECT_ID, "limit": 10},
    )
    assert events.status_code in ALLOW, events.text
    assert events.status_code < 500
    if events.status_code == 200:
        body = events.json()
        assert body.get("project_id") == PROJECT_ID
        assert isinstance(body.get("items"), list)

    receipts = client.get(
        "/v1/receipts",
        params={"project_id": PROJECT_ID, "limit": 10},
    )
    assert receipts.status_code in ALLOW, receipts.text
    assert receipts.status_code < 500
    if receipts.status_code == 200:
        body = receipts.json()
        assert body.get("project_id") == PROJECT_ID
        assert isinstance(body.get("items"), list)

    # Missing receipt by dedupe_key is a clean 404
    missing = client.get(f"/v1/receipts/e2e-missing-dedupe-{_uid()}")
    assert missing.status_code in (404, 422), missing.text
    assert missing.status_code < 500


def test_core_control_plane_vertical_slice(client):
    """Single happy-path slice: propose → list tasks/jobs → budget → events/receipts."""
    uid = _uid()
    cmd = client.post(
        "/v1/commands/propose",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "command": f"Vertical slice e2e {uid}",
            "constraints": {
                "source": "test_e2e_core_control_plane",
                "event_id": f"evt-vertical-{uid}",
            },
        },
    )
    assert cmd.status_code in ALLOW, cmd.text
    assert cmd.status_code < 500

    for path, params in (
        ("/v1/tasks", {"project_id": PROJECT_ID, "limit": 5}),
        ("/v1/jobs", {"project_id": PROJECT_ID, "limit": 5}),
        ("/v1/decisions/inbox", {"project_id": PROJECT_ID}),
        ("/v1/events", {"project_id": PROJECT_ID, "limit": 5}),
        ("/v1/receipts", {"project_id": PROJECT_ID, "limit": 5}),
        (
            "/v1/budget/status",
            {"scope_type": "project", "scope_id": PROJECT_ID},
        ),
    ):
        response = client.get(path, params=params)
        assert response.status_code in ALLOW, f"{path} -> {response.status_code} {response.text[:200]}"
        assert response.status_code < 500, f"5xx on {path}: {response.text[:300]}"
