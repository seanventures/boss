"""BOSS-11 — persistent preview sandboxes: durable records, TTL heartbeat, fail-closed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.config import get_settings
from boss_engine.services.preview_sandbox_service import PreviewSandboxService

PID = "boss-dogfood"
ACTOR = "ceo"


@pytest.fixture
def authenticated_preview_keys(monkeypatch):
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "ceo-key,coo-key,ops-key")
    monkeypatch.setattr(
        api_main.settings,
        "api_key_principals",
        "ceo-key:ceo,coo-key:coo,ops-key:preview_operator",
    )


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


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
        params={"project_id": PID, "actor": ACTOR},
    )
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert any(item["id"] == preview_id for item in items)

    status = client.get(
        f"/v1/preview-sandboxes/{preview_id}",
        params={"project_id": PID, "actor": ACTOR},
    )
    assert status.status_code == 200
    assert status.json()["status"] == "running"

    # Heartbeat extends TTL.
    before_exp = status.json()["expires_at"]
    hb = client.post(
        f"/v1/preview-sandboxes/{preview_id}/heartbeat",
        json={"project_id": PID, "actor": ACTOR, "ttl_seconds": 1200},
    )
    assert hb.status_code == 200, hb.text
    assert hb.json()["status"] == "running"
    assert hb.json()["ttl_seconds"] == 1200
    assert hb.json()["expires_at"] >= before_exp

    released = client.post(
        f"/v1/preview-sandboxes/{preview_id}/release",
        json={"project_id": PID, "actor": ACTOR},
    )
    assert released.status_code == 200
    assert released.json()["status"] == "released"

    # Heartbeat after release fails closed.
    dead = client.post(
        f"/v1/preview-sandboxes/{preview_id}/heartbeat",
        json={"project_id": PID, "actor": ACTOR},
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
            actor="chief_operator",  # trusted internal service caller
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
        params={"project_id": PID, "actor": ACTOR, "include_expired": True},
    )
    assert listed.status_code == 200
    by_id = {item["id"]: item for item in listed.json()["items"]}
    assert by_id[preview_id]["status"] == "expired"


def test_host_reserve_requires_project_grant_before_command_or_env_forwarding(
    client, engine, monkeypatch, authenticated_preview_keys,
):
    calls = []

    def fake_host_request(_self, method, path, body=None, **_kwargs):
        calls.append((method, path, body))
        return {"host_ref": "qa-preview", "status": "running"}

    monkeypatch.setattr(PreviewSandboxService, "_host_request", fake_host_request)
    other_project = f"preview-other-{uuid4().hex[:8]}"
    with engine.begin() as connection:
        workspace_id = connection.execute(text(
            "SELECT workspace_id FROM projects WHERE id = :project_id"
        ), {"project_id": PID}).scalar_one()
        connection.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "VALUES (:id, :workspace_id, 'Preview other', :id)"
        ), {"id": other_project, "workspace_id": workspace_id})
        api_main.members.upsert_member(
            connection, project_id=PID, member_id="preview_operator",
            display_name="Preview operator", member_type="human", role_keys=[],
            permissions=["providerops_manage"], actor="ceo",
        )

    payload = {
        "project_id": PID, "app_id": f"qa-preview-{uuid4().hex[:8]}",
        "actor": "coo", "run_cmd": "python -m http.server",
        "env": {"PREVIEW_TOKEN": "test-secret-value"}, "simulate": False,
    }
    assert client.post(
        "/v1/preview-sandboxes", headers=_auth("coo-key"), json=payload
    ).status_code == 403
    assert client.post(
        "/v1/preview-sandboxes", headers=_auth("coo-key"),
        json={**payload, "actor": "ceo"},
    ).status_code == 403
    assert client.post(
        "/v1/preview-sandboxes", headers=_auth("ops-key"),
        json={**payload, "actor": "preview_operator"},
    ).status_code == 403  # providerops_manage alone cannot run commands
    assert client.post(
        "/v1/preview-sandboxes", headers=_auth("ops-key"),
        json={**payload, "project_id": other_project,
              "actor": "preview_operator"},
    ).status_code == 403
    assert calls == []

    allowed = client.post(
        "/v1/preview-sandboxes", headers=_auth("ceo-key"),
        json={**payload, "actor": "ceo"},
    )
    assert allowed.status_code == 200, allowed.text
    assert calls == [("POST", "/v1/sandbox/apps", {
        "app_id": payload["app_id"], "image_or_build": "",
        "run_cmd": payload["run_cmd"], "env": payload["env"],
        "port": None, "ttl_seconds": 3600,
    })]
    assert "env" not in allowed.json()
    assert "env_json" not in allowed.json()
    assert "test-secret-value" not in allowed.text


def test_read_and_lifecycle_are_bound_to_actual_sandbox_project(
    client, engine, monkeypatch, authenticated_preview_keys,
):
    calls = []

    def fake_host_request(_self, method, path, body=None, **_kwargs):
        calls.append((method, path))
        return {"host_ref": "qa-preview", "status": "running"}

    monkeypatch.setattr(PreviewSandboxService, "_host_request", fake_host_request)
    app_id = f"qa-read-{uuid4().hex[:8]}"
    created = client.post(
        "/v1/preview-sandboxes", headers=_auth("ceo-key"),
        json={"project_id": PID, "app_id": app_id, "actor": "ceo",
              "env": {"PREVIEW_TOKEN": "test-secret-value"}},
    )
    assert created.status_code == 200, created.text
    preview_id = created.json()["id"]
    path = f"/v1/preview-sandboxes/{preview_id}"
    calls.clear()

    assert client.get(
        "/v1/preview-sandboxes", headers=_auth("coo-key"),
        params={"project_id": PID, "actor": "coo"},
    ).status_code == 403
    assert client.get(
        path, headers=_auth("coo-key"),
        params={"project_id": PID, "actor": "coo"},
    ).status_code == 403
    assert client.get(
        path, headers=_auth("coo-key"),
        params={"project_id": PID, "actor": "ceo"},
    ).status_code == 403
    assert client.post(
        f"{path}/heartbeat", headers=_auth("coo-key"),
        json={"project_id": PID, "actor": "coo"},
    ).status_code == 403
    assert client.post(
        f"{path}/release", headers=_auth("coo-key"),
        json={"project_id": PID, "actor": "ceo"},
    ).status_code == 403
    assert calls == []

    foreign = f"preview-foreign-{uuid4().hex[:8]}"
    with engine.begin() as connection:
        workspace_id = connection.execute(text(
            "SELECT workspace_id FROM projects WHERE id = :project_id"
        ), {"project_id": PID}).scalar_one()
        connection.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "VALUES (:id, :workspace_id, 'Preview foreign', :id)"
        ), {"id": foreign, "workspace_id": workspace_id})

    # Even a grant in the claimed project cannot operate on another project's id.
    assert client.post(
        f"{path}/heartbeat", headers=_auth("ceo-key"),
        json={"project_id": foreign, "actor": "ceo"},
    ).status_code == 404
    assert client.post(
        f"{path}/release", headers=_auth("ceo-key"),
        json={"project_id": foreign, "actor": "ceo"},
    ).status_code == 404
    assert client.get(
        path, headers=_auth("ceo-key"),
        params={"project_id": foreign, "actor": "ceo"},
    ).status_code == 404
    assert calls == []

    listed = client.get(
        "/v1/preview-sandboxes", headers=_auth("ceo-key"),
        params={"project_id": PID, "actor": "ceo"},
    )
    assert listed.status_code == 200, listed.text
    assert "test-secret-value" not in listed.text
    assert "env" not in next(item for item in listed.json()["items"]
                             if item["id"] == preview_id)
    status = client.get(
        path, headers=_auth("ceo-key"),
        params={"project_id": PID, "actor": "ceo"},
    )
    assert status.status_code == 200, status.text
    assert "env" not in status.json()

    heartbeat = client.post(
        f"{path}/heartbeat", headers=_auth("ceo-key"),
        json={"project_id": PID, "actor": "ceo", "ttl_seconds": 1200},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    released = client.post(
        f"{path}/release", headers=_auth("ceo-key"),
        json={"project_id": PID, "actor": "ceo"},
    )
    assert released.status_code == 200, released.text
    assert calls == [
        ("POST", f"/v1/sandbox/apps/{app_id}/heartbeat"),
        ("DELETE", f"/v1/sandbox/apps/{app_id}"),
    ]
    assert all("test-secret-value" not in response.text
               for response in (heartbeat, released))
