"""Honesty tests for POST /v1/anomalies/scan — no invented scores.

Scores/details must come from measured job/spend data. An empty window
returns an empty list, never a fabricated anomaly.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.db import begin

PROJECT_ID = "boss-dogfood"


def test_anomalies_scan_empty_does_not_invent(client):
    r = client.post(
        "/v1/anomalies/scan",
        json={"project_id": PROJECT_ID, "auto_lite": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["anomalies"] == []
    assert body["auto_lite_applied"] is False
    assert "score" not in body
    for item in body["anomalies"]:
        assert "score" not in item or item.get("score") is None


def test_anomalies_scan_retry_storm_uses_measured_count(client, engine):
    """Five deadlettered jobs in 15m is a retry_storm — count is measured, not scored."""
    with begin(engine) as connection:
        for i in range(5):
            connection.execute(
                text(
                    "INSERT INTO jobs (id, project_id, task_id, kind, status, payload) "
                    "VALUES (:id, :p, NULL, 'test.anomaly', 'deadlettered', '{}'::jsonb)"
                ),
                {"id": f"job_anom_{i}", "p": PROJECT_ID},
            )

    r = client.post(
        "/v1/anomalies/scan",
        json={"project_id": PROJECT_ID, "auto_lite": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    storms = [a for a in body["anomalies"] if a.get("type") == "retry_storm"]
    assert storms, body
    detail = storms[0]["detail"]
    assert "5" in detail or "deadlettered" in detail
    # No invented numeric score field — the detector reports measured counts.
    assert "score" not in storms[0]
