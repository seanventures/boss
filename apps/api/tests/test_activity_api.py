"""HTTP tests for GET /v1/activity — BOSS-379 slice 5.

Read-only ops timeline. Never POST/spend. Skips if the activity router
is not mounted (sibling slice owns apps/api/app/routers/activity.py).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

_ROUTER_PATH = Path(__file__).resolve().parents[1] / "app" / "routers" / "activity.py"

PROJECT_ID = "boss-dogfood"

ITEM_FIELDS = (
    "id",
    "source",
    "kind",
    "status",
    "title",
    "actor",
    "entity_type",
    "entity_id",
    "stage",
    "created_at",
    "data",
)
ALLOWED_SOURCES = {"event", "process_step", "job", "deadletter", "receipt"}
ALLOWED_STATUSES = {"ok", "failed", "waiting", "stuck", "info"}

# Distinctive secret-looking literals used only as redaction probes.
SECRET_SK = "sk-test-secret-value-1234567890abcd"
SECRET_AWS = "AKIAIOSFODNN7EXAMPLE"
SECRET_BEARER = "Bearer supersecrettokenvalue99"

SECRET_PATTERNS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[baprs])[-_][A-Za-z0-9]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
)


def _router_reason() -> str:
    return (
        "GET /v1/activity router not mounted "
        "(apps/api/app/routers/activity.py missing or not included in platform.py)"
    )


def _router_file_exists() -> bool:
    return _ROUTER_PATH.is_file()


# Sibling slice owns the router. Do not fail the matrix when it has not landed.
pytestmark = pytest.mark.skipif(not _router_file_exists(), reason=_router_reason())


def _activity_router_available() -> bool:
    try:
        from apps.api.app.routers import activity as _activity  # noqa: F401
    except ImportError:
        return False
    from apps.api.app.main import app

    return any(getattr(route, "path", "") == "/v1/activity" for route in app.routes)


def _skip_if_router_missing() -> None:
    if not _activity_router_available():
        pytest.skip(_router_reason())


def _get_activity(client, **params):
    query = {"project_id": PROJECT_ID}
    query.update({key: value for key, value in params.items() if value is not None})
    return client.get("/v1/activity", params=query)


def _require_live_activity(client):
    """Skip when the route is absent, or when the sibling service is not loaded."""
    _skip_if_router_missing()
    probe = _get_activity(client, limit=1)
    if probe.status_code == 503:
        pytest.skip("activity service unavailable (sibling slice not loaded)")
    return probe


def _assert_item_shape(item: dict) -> None:
    assert isinstance(item, dict)
    for field in ITEM_FIELDS:
        assert field in item, f"missing activity item field: {field}"
    assert item["source"] in ALLOWED_SOURCES
    assert item["status"] in ALLOWED_STATUSES
    assert item["data"] is None or isinstance(item["data"], dict)
    assert item["id"]
    assert item["created_at"]


def _insert_event(
    engine,
    *,
    event_type: str,
    actor: str,
    entity_type: str = "task",
    entity_id: str | None = None,
    data: dict | None = None,
) -> str:
    event_id = f"evt_act_{uuid4().hex[:16]}"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO events (
                    id, project_id, event_type, actor, entity_type, entity_id, data
                )
                VALUES (
                    :id, :project_id, :event_type, :actor, :entity_type, :entity_id,
                    CAST(:data AS jsonb)
                )
                """
            ),
            {
                "id": event_id,
                "project_id": PROJECT_ID,
                "event_type": event_type,
                "actor": actor,
                "entity_type": entity_type,
                "entity_id": entity_id or f"ent_{uuid4().hex[:12]}",
                "data": json.dumps(data or {}),
            },
        )
    return event_id


def _count(engine, table: str) -> int:
    with engine.begin() as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def test_activity_list_200_items_shape(client, engine):
    _require_live_activity(client)
    event_id = _insert_event(
        engine,
        event_type="artifact.created",
        actor="ceo",
        entity_type="artifact",
        entity_id="art_activity_shape",
        data={"message": "activity shape probe"},
    )

    response = _get_activity(client, limit=50)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body.get("items"), list)
    items = body["items"]
    assert items, "expected at least the inserted event in the activity feed"
    for item in items:
        _assert_item_shape(item)
    ids = {item["id"] for item in items}
    kinds = {item["kind"] for item in items}
    assert event_id in ids or any("artifact" in str(kind) for kind in kinds)


def test_activity_q_filter(client, engine):
    _require_live_activity(client)
    needle = f"zzq-activity-unique-{uuid4().hex[:10]}"
    hit_id = _insert_event(
        engine,
        event_type=f"task.{needle}",
        actor=f"actor-{needle}",
        entity_id=f"task_{needle}",
        data={"message": f"searchable {needle}"},
    )
    _insert_event(
        engine,
        event_type="job.completed",
        actor="worker",
        entity_id="job_other_activity",
        data={"message": "unrelated activity row"},
    )

    matched = _get_activity(client, q=needle, limit=50)
    assert matched.status_code == 200, matched.text
    matched_items = matched.json()["items"]
    assert matched_items, f"q={needle} should return the inserted row"
    blob = json.dumps(matched_items)
    assert needle in blob or hit_id in {item["id"] for item in matched_items}
    for item in matched_items:
        _assert_item_shape(item)
        packed = json.dumps(item)
        assert needle in packed or item["id"] == hit_id

    missed = _get_activity(client, q="no-such-activity-token-zzz-379", limit=50)
    assert missed.status_code == 200, missed.text
    missed_ids = {item["id"] for item in missed.json()["items"]}
    assert hit_id not in missed_ids
    assert needle not in json.dumps(missed.json()["items"])


def test_activity_bad_limit_422(client):
    _skip_if_router_missing()
    for bad_limit in (0, -1, 9999, "nope"):
        response = _get_activity(client, limit=bad_limit)
        assert response.status_code == 422, response.text
        assert response.status_code != 500


def test_activity_503_when_service_missing(client, monkeypatch):
    _skip_if_router_missing()
    import apps.api.app.routers.activity as activity_mod

    patched = False
    for name in (
        "list_activity",
        "query_activity",
        "get_activity",
        "activity_log",
        "ACTIVITY_SERVICE",
        "_list_activity",
        "_service",
    ):
        if hasattr(activity_mod, name):
            monkeypatch.setattr(activity_mod, name, None)
            patched = True

    if not patched:
        monkeypatch.setitem(sys.modules, "boss_engine.services.activity_log", None)

    response = _get_activity(client, limit=10)
    assert response.status_code == 503, response.text
    assert response.status_code != 500


def test_activity_never_includes_raw_secret_values(client, engine):
    _require_live_activity(client)
    _insert_event(
        engine,
        event_type="secret.probe.activity",
        actor="ceo",
        entity_id="ent_secret_probe",
        data={
            "token": SECRET_SK,
            "aws_key": SECRET_AWS,
            "authorization": SECRET_BEARER,
            "note": "redact these secret-looking values",
        },
    )

    response = _get_activity(client, limit=200)
    assert response.status_code == 200, response.text
    blob = response.text
    assert SECRET_SK not in blob
    assert SECRET_AWS not in blob
    assert SECRET_BEARER not in blob
    for pattern in SECRET_PATTERNS:
        assert pattern.search(blob) is None, f"raw secret-looking value leaked: {pattern.pattern}"
    for item in response.json()["items"]:
        _assert_item_shape(item)


def test_activity_does_not_post_or_spend(client, engine):
    _require_live_activity(client)
    posted = client.post("/v1/activity", json={"project_id": PROJECT_ID, "actor": "ceo"})
    assert posted.status_code in {404, 405, 422}, posted.text
    assert posted.status_code != 200
    assert posted.status_code != 201

    before = {
        "budget_reservations": _count(engine, "budget_reservations"),
        "cost_receipts": _count(engine, "cost_receipts"),
        "receipts": _count(engine, "receipts"),
        "jobs": _count(engine, "jobs"),
    }
    response = _get_activity(client, limit=10)
    assert response.status_code == 200, response.text
    after = {
        "budget_reservations": _count(engine, "budget_reservations"),
        "cost_receipts": _count(engine, "cost_receipts"),
        "receipts": _count(engine, "receipts"),
        "jobs": _count(engine, "jobs"),
    }
    assert after == before
