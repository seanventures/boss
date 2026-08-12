"""BOSS-11 — persistent preview sandboxes: durable records, TTL heartbeat, fail-closed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services.preview_sandbox_service import PreviewSandboxService

PID = "boss-dogfood"
ACTOR = "chief_operator"


def test_reserve_list_status_heartbeat_release_simulated(client, engine):
    """Simulated path: durable ledger without a sandbox host."""
    reserve = client.post(
        "/v1/preview-sandboxes",
        json={
            "project_id": PID,
            "app_id": "demo-nqueens",
            "actor": ACTOR,
            "run_cmd": "python3 -m http.server",
            "ttl_seconds": 600,
            "simulate": True,
        },
    )
    assert reserve.status_code == 200, reserve.text
    body = reserve.json()
    assert body["status"] == "running"
    assert body["simulate"] is True
    assert body["app_id"] == "demo-nqueens"
    assert body["expires_at"]
    assert body["last_heartbeat_at"]
    preview_id = body["id"]

    listed = client.get(
        "/v1/preview-sandboxes",
        params={"project_id": PID},
    )
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert any(item["id"] == preview_id for item in items)

    status = client.get(f"/v1/preview-sandboxes/{preview_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "running"

    # Heartbeat extends TTL.
    before_exp = status.json()["expires_at"]
    hb = client.post(
        f"/v1/preview-sandboxes/{preview_id}/heartbeat",
        json={"actor": ACTOR, "ttl_seconds": 1200},
    )
    assert hb.status_code == 200, hb.text
    assert hb.json()["status"] == "running"
    assert hb.json()["ttl_seconds"] == 1200
    assert hb.json()["expires_at"] >= before_exp

    released = client.post(
        f"/v1/preview-sandboxes/{preview_id}/release",
        json={"actor": ACTOR},
    )
    assert released.status_code == 200
    assert released.json()["status"] == "released"

    # Heartbeat after release fails closed.
    dead = client.post(
        f"/v1/preview-sandboxes/{preview_id}/heartbeat",
        json={"actor": ACTOR},
    )
    assert dead.status_code == 422
    assert "heartbeat" in dead.json()["detail"].lower() or "status" in dead.json()[
        "detail"
    ].lower()


def test_reserve_without_host_fail_closed(client, monkeypatch):
    """Without simulate and without a host URL, reserve is 503 fail-closed."""
    settings = get_settings()
    monkeypatch.setattr(settings, "sandbox_agent_url", "", raising=False)
    # Ensure env simulate is off.
    monkeypatch.delenv("BOSS_PREVIEW_SANDBOX_SIMULATE", raising=False)

    response = client.post(
        "/v1/preview-sandboxes",
        json={
            "project_id": PID,
            "app_id": "no-host-app",
            "actor": ACTOR,
            "simulate": False,
        },
    )
    assert response.status_code == 503, response.text
    assert "sandbox host" in response.json()["detail"].lower()


def test_expired_rows_reaped_on_list(client, engine):
    settings = get_settings()
    service = PreviewSandboxService(settings)
    with engine.begin() as connection:
        row = service.reserve(
            connection,
            project_id=PID,
            app_id="ttl-expiry-app",
            actor=ACTOR,
            ttl_seconds=60,
            simulate=True,
        )
        preview_id = row["id"]
        # Force expiry in the past.
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        connection.execute(
            text(
                """
                UPDATE preview_sandboxes
                SET expires_at = :past, last_heartbeat_at = :past
                WHERE id = :id
                """
            ),
            {"past": past, "id": preview_id},
        )

    listed = client.get(
        "/v1/preview-sandboxes",
        params={"project_id": PID, "include_expired": True},
    )
    assert listed.status_code == 200
    by_id = {item["id"]: item for item in listed.json()["items"]}
    assert by_id[preview_id]["status"] == "expired"
