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
