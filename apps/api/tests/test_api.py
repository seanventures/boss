import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text


def test_health_returns_service_status(client):
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database_connected"] is True


def test_status_returns_seeded_project(client):
    response = client.get("/v1/status", params={"project_id": "boss-dogfood"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == "boss-dogfood"
    assert payload["project_name"] == "BOSS dogfood bootstrap"


def test_command_proposal_creates_task_job_artifact_and_task_lookup(client, engine):
    response = client.post(
        "/v1/commands/propose",
        json={
            "project_id": "boss-dogfood",
            "command": "Plan the next sandbox verification pass.",
            "constraints": {"deadline": "soon"},
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["task_id"]
    assert payload["job_id"]
    assert payload["artifact_id"]

    task_response = client.get(f"/v1/tasks/{payload['task_id']}")
    assert task_response.status_code == 200
    task_payload = task_response.json()
    assert task_payload["id"] == payload["task_id"]
    assert task_payload["status"] == "open"
    assert task_payload["priority"] == "p1"

    list_response = client.get("/v1/tasks", params={"project_id": "boss-dogfood"})
    assert list_response.status_code == 200
    listed_ids = [item["id"] for item in list_response.json()["items"]]
    assert payload["task_id"] in listed_ids

    with engine.begin() as connection:
        job_kind = connection.execute(
            text("SELECT kind FROM jobs WHERE id = :job_id"),
            {"job_id": payload["job_id"]},
        ).scalar_one()
        artifact_type = connection.execute(
            text("SELECT artifact_type FROM artifacts WHERE id = :artifact_id"),
            {"artifact_id": payload["artifact_id"]},
        ).scalar_one()
    assert job_kind == "plan.command"
    assert artifact_type == "command_brief"


def test_budget_denial_creates_decision(client):
    response = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "denied"
    assert payload["decision_id"]


def test_budget_status_returns_project_snapshot(client):
    response = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope_type"] == "project"
    assert payload["scope_id"] == "boss-dogfood"
    assert payload["budget_state"] == "green"
    assert payload["cap_usd_micros"] > 0
    assert payload["available_usd_micros"] == payload["cap_usd_micros"]
    assert payload["committed_usd_micros"] == 0
    assert payload["active_reservations_count"] == 0
    assert payload["top_cost_drivers"] == []


def test_budget_quote_returns_allowed_projection(client):
    response = client.post(
        "/v1/budget/quote",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 250_000,
            "reason": "quote a model call",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is True
    assert payload["requested_usd_micros"] == 250_000
    assert payload["budget_state"] == "green"
    assert payload["post_reservation_available_usd_micros"] < payload["available_usd_micros"]


def test_budget_release_frees_active_reservation(client):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 250_000,
            "reason": "reserve then release",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    release = client.post(
        "/v1/budget/release",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "reason": "No external effect was executed.",
        },
    )
    assert release.status_code == 200
    payload = release.json()
    assert payload["status"] == "released"
    assert payload["reservation_id"] == reservation_id

    status = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )
    assert status.status_code == 200
    status_payload = status.json()
    assert status_payload["reserved_usd_micros"] == 0
    assert status_payload["active_reservations_count"] == 0


def test_budget_denial_is_visible_in_decision_inbox_and_event_feed(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    inbox = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert inbox.status_code == 200
    inbox_payload = inbox.json()
    assert inbox_payload["items"]
    assert inbox_payload["items"][0]["id"] == decision_id
    assert inbox_payload["items"][0]["status"] == "pending"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.created" in event_types
    assert "budget.reservation_denied" in event_types


def test_decision_can_be_approved_and_emits_audit_event(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo", "note": "Budget increase is acceptable."},
    )
    assert approve.status_code == 200
    payload = approve.json()
    assert payload["id"] == decision_id
    assert payload["status"] == "approved"
    assert payload["approved_by"] == "ceo"
    assert payload["approved_at"]

    status = client.get("/v1/status", params={"project_id": "boss-dogfood"})
    assert status.status_code == 200
    assert status.json()["pending_decisions"] == 0

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.approved" in event_types


def test_decision_can_be_rejected_and_cannot_be_reapproved(client):
    denial = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 100_000_000,
            "reason": "force denial",
        },
    )
    assert denial.status_code == 201
    decision_id = denial.json()["decision_id"]

    reject = client.post(
        f"/v1/decisions/{decision_id}/reject",
        json={"actor": "ceo", "note": "Keep the guardrail in place."},
    )
    assert reject.status_code == 200
    payload = reject.json()
    assert payload["status"] == "rejected"
    assert payload["approved_by"] is None
    assert payload["approved_at"] is None

    duplicate_approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo"},
    )
    assert duplicate_approve.status_code == 409

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.rejected" in event_types


def test_overdue_pending_decisions_expire_to_safe_default(client, engine):
    deadline_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, deadline_at, default_action, status
                )
                VALUES (
                    'dec_expired_for_inbox', 'boss-dogfood', NULL, 'Expired decision',
                    '{}'::jsonb, '[]'::jsonb, 2, '{}'::jsonb, :deadline_at, 'hold', 'pending'
                )
                """
            ),
            {"deadline_at": deadline_at},
        )

    inbox = client.get("/v1/decisions/inbox", params={"project_id": "boss-dogfood"})
    assert inbox.status_code == 200
    payload = inbox.json()
    assert payload["items"][0]["id"] == "dec_expired_for_inbox"
    assert payload["items"][0]["status"] == "expired"
    assert payload["items"][0]["default_action"] == "hold"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "decision.expired" in [item["event_type"] for item in events.json()["items"]]


def test_expired_reservations_are_reclassified_before_budget_status(client, engine):
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO budget_reservations (
                    id, allocation_id, project_id, job_id, category, amount_usd_micros,
                    status, reason, expires_at
                )
                VALUES (
                    'res_expired_budget_status', NULL, 'boss-dogfood', NULL, 'model', 12345,
                    'active', 'expired for test', :expires_at
                )
                """
            ),
            {"expires_at": expired_at},
        )

    status_response = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )
    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["reserved_usd_micros"] == 0

    with engine.begin() as connection:
        status = connection.execute(
            text(
                """
                SELECT status FROM budget_reservations
                WHERE id = 'res_expired_budget_status'
                """
            )
        ).scalar_one()
    assert status == "expired"

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "budget.reservation_expired" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_openai_plan_blocks_without_reservation(client):
    response = client.post(
        "/v1/openai/plans",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": "missing",
            "dedupe_key": "test-plan-no-res",
            "prompt": "Create a plan.",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]


def test_two_workers_cannot_claim_same_job(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    worker_a = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert worker_a.status_code == 200
    leased = worker_a.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id

    worker_b = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-b",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert worker_b.status_code == 200
    assert worker_b.json()["status"] == "idle"
    assert worker_b.json()["job"] is None


def test_get_job_returns_created_job(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    fetched = client.get(f"/v1/jobs/{job_id}")
    assert fetched.status_code == 200
    payload = fetched.json()
    assert payload["id"] == job_id
    assert payload["kind"] == "plan.generate"
    assert payload["status"] == "queued"


def test_expired_lease_can_be_reclaimed(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    first = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert first.status_code == 200
    assert first.json()["job"]["attempt"] == 1

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE jobs
                SET lease_expires_at = :lease_expires_at
                WHERE id = :job_id
                """
            ),
            {
                "job_id": job_id,
                "lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=5),
            },
        )

    second = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-b",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    )
    assert second.status_code == 200
    payload = second.json()
    assert payload["status"] == "leased"
    assert payload["job"]["id"] == job_id
    assert payload["job"]["attempt"] == 2

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    assert "job.lease_reclaimed" in [
        item["event_type"] for item in events.json()["items"]
    ]


def test_failed_job_retries_then_deadletters(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
            "max_attempts": 2,
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    first_lease = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    heartbeat = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": first_lease["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    assert heartbeat.status_code == 200

    first_fail = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": first_lease["lease_token"],
            "error": {"message": "transient failure"},
            "retryable": True,
            "retry_delay_seconds": 0,
        },
    )
    assert first_fail.status_code == 200
    assert first_fail.json()["status"] == "failed_retryable"

    second_lease = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-b",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    second_heartbeat = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-b",
            "job_id": job_id,
            "lease_token": second_lease["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    assert second_heartbeat.status_code == 200

    second_fail = client.post(
        "/v1/worker/fail",
        json={
            "worker_id": "worker-b",
            "job_id": job_id,
            "lease_token": second_lease["lease_token"],
            "error": {"message": "still failing"},
            "retryable": True,
            "retry_delay_seconds": 0,
        },
    )
    assert second_fail.status_code == 200
    payload = second_fail.json()
    assert payload["status"] == "deadlettered"
    assert payload["deadletter_id"]

    with engine.begin() as connection:
        status = connection.execute(
            text("SELECT status FROM jobs WHERE id = :job_id"),
            {"job_id": job_id},
        ).scalar_one()
        deadletters = connection.execute(
            text(
                "SELECT COUNT(*) FROM deadletters WHERE source_kind = 'job' AND source_id = :job_id"
            ),
            {"job_id": job_id},
        ).scalar_one()
    assert status == "deadlettered"
    assert deadletters == 1


def test_worker_complete_creates_plan_artifact_and_decision(client):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Create a draft plan."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "result": {
                "summary": "Create a draft plan.",
                "objectives": ["Create a draft plan."],
                "steps": ["Do the work."],
                "risks": ["None"],
                "done_when": ["Artifact exists."],
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["artifact_id"]
    assert payload["decision_id"]


def test_approving_plan_decision_queues_follow_on_pr_job(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"command": "Prepare the next sandbox proposal."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-a",
            "capabilities": ["plan.generate"],
            "lease_ttl_seconds": 60,
        },
    ).json()["job"]
    client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-a",
            "job_id": job_id,
            "lease_token": leased["lease_token"],
            "result": {
                "summary": "Prepare the next sandbox proposal.",
                "objectives": ["Prepare the next sandbox proposal."],
                "steps": ["Draft the proposal markdown."],
                "risks": ["Budget could block execution."],
                "done_when": ["A sandbox PR proposal job is queued."],
            },
        },
    )
    assert complete.status_code == 200
    decision_id = complete.json()["decision_id"]

    approve = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo", "note": "Proceed to sandbox proposal."},
    )
    assert approve.status_code == 200

    with engine.begin() as connection:
        reservation = connection.execute(
            text(
                """
                SELECT id, status, category, amount_usd_micros
                FROM budget_reservations
                WHERE project_id = 'boss-dogfood'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()
        next_job = connection.execute(
            text(
                """
                SELECT kind, status, payload
                FROM jobs
                WHERE project_id = 'boss-dogfood'
                  AND kind = 'software_factory.propose_pr'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()

    assert reservation["status"] == "active"
    assert reservation["category"] == "tool"
    assert int(reservation["amount_usd_micros"]) == 500_000
    assert next_job["status"] == "queued"
    payload = next_job["payload"]
    assert payload["reservation_id"] == reservation["id"]
    assert payload["title"].startswith("BOSS proposal:")
    assert payload["branch_name"].startswith("boss/proposal-")
    assert payload["files"][0]["path"].startswith("proposals/")

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.follow_on_job_queued" in event_types


def test_approving_pr_review_decision_creates_merge_gate_block_artifact(client, engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, project_id, task_id, artifact_type, title, uri, content, content_hash, created_by_job_id
                )
                VALUES (
                    'art_review_packet', 'boss-dogfood', NULL, 'review_packet', 'Review packet',
                    NULL, '{}'::jsonb, 'hash', NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, default_action, status
                )
                VALUES (
                    'dec_review_pr', 'boss-dogfood', NULL, 'Review and approve sandbox PR: Demo',
                    CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold', 'pending'
                )
                """
            ),
            {
                "recommendation": json.dumps(
                    {
                        "action": "review_pull_request",
                        "artifact_id": "art_review_packet",
                        "receipt_id": "rcpt_pr_demo",
                        "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
                        "branch_name": "boss/demo-pr",
                        "title": "Demo",
                    }
                )
            },
        )

    approve = client.post(
        "/v1/decisions/dec_review_pr/approve",
        json={"actor": "ceo", "note": "Manual merge is acceptable."},
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"

    with engine.begin() as connection:
        merge_gate = connection.execute(
            text(
                """
                SELECT artifact_type, title, content
                FROM artifacts
                WHERE project_id = 'boss-dogfood' AND artifact_type = 'merge_gate'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()
        queued_jobs = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM jobs
                WHERE project_id = 'boss-dogfood' AND kind = 'github.pull_request.merge'
                """
            )
        ).scalar_one()

    assert merge_gate["artifact_type"] == "merge_gate"
    assert "Merge gate for Demo" == merge_gate["title"]
    assert merge_gate["content"]["status"] == "blocked_manual_merge_required"
    assert (
        merge_gate["content"]["pull_request_url"]
        == "https://github.com/seanheiney/boss-sandbox/pull/42"
    )
    assert merge_gate["content"]["receipt_id"] == "rcpt_pr_demo"
    assert queued_jobs == 0

    events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "decision.follow_on_blocked" in event_types


def test_spend_gated_job_without_reservation_is_not_leased(client, engine):
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "openai.plan",
            "requires_reservation": True,
            "payload": {"prompt": "Plan a task."},
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-budget",
            "capabilities": ["openai.plan"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    assert poll.json()["status"] == "idle"

    with engine.begin() as connection:
        job_status = connection.execute(
            text("SELECT status FROM jobs WHERE id = :job_id"),
            {"job_id": job_id},
        ).scalar_one()
        decision_count = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM decisions
                WHERE project_id = 'boss-dogfood'
                  AND question = 'Budget blocked spend-gated job: openai.plan'
                """
            )
        ).scalar_one()
    assert job_status == "cancelled"
    assert decision_count == 1


def test_spend_gated_job_with_reservation_can_be_leased(client):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "model",
            "amount_usd_micros": 250_000,
            "reason": "Lease an OpenAI-backed job.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    created = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "openai.plan",
            "requires_reservation": True,
            "reservation_id": reservation_id,
            "payload": {"prompt": "Plan a task."},
        },
    )
    assert created.status_code == 201

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-budget",
            "capabilities": ["openai.plan"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    payload = poll.json()
    assert payload["status"] == "leased"
    assert payload["job"]["kind"] == "openai.plan"


def test_queue_pr_proposal_job_is_leasable_and_completion_passes_through(client):
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "tool",
            "amount_usd_micros": 500_000,
            "reason": "Queue a PR proposal job.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    queued = client.post(
        "/v1/software-factory/proposals/queue",
        json={
            "project_id": "boss-dogfood",
            "reservation_id": reservation_id,
            "dedupe_key": "queued-pr-proposal",
            "title": "Sandbox PR",
            "branch_name": "boss/test-queued-pr",
            "summary": "Create a queued sandbox PR proposal.",
            "files": [{"path": "README.md", "content": "hello"}],
        },
    )
    assert queued.status_code == 201
    job_id = queued.json()["id"]

    fetched = client.get(f"/v1/jobs/{job_id}")
    assert fetched.status_code == 200
    assert fetched.json()["kind"] == "software_factory.propose_pr"

    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "worker-pr",
            "capabilities": ["software_factory.propose_pr"],
            "lease_ttl_seconds": 60,
        },
    )
    assert poll.status_code == 200
    leased = poll.json()
    assert leased["status"] == "leased"
    assert leased["job"]["id"] == job_id

    complete = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "worker-pr",
            "job_id": job_id,
            "lease_token": leased["job"]["lease_token"],
            "result": {
                "status": "created",
                "artifact_id": "art_review",
                "diff_artifact_id": "art_diff",
                "test_report_artifact_id": "art_test_report",
                "receipt_id": "rcpt_pr",
                "decision_id": "dec_pr",
                "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/1",
                "branch_name": "boss/test-queued-pr",
            },
        },
    )
    assert complete.status_code == 200
    payload = complete.json()
    assert payload["status"] == "succeeded"
    assert payload["artifact_id"] == "art_review"
    assert payload["diff_artifact_id"] == "art_diff"
    assert payload["test_report_artifact_id"] == "art_test_report"
    assert payload["receipt_id"] == "rcpt_pr"
    assert payload["decision_id"] == "dec_pr"
    assert payload["pull_request_url"].endswith("/pull/1")
