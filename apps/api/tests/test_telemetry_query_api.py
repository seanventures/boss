"""HTTP surface for telemetry connectors + change-correlation.

Covers:
  GET  /v1/telemetry/connectors/catalog
  POST /v1/telemetry/query
  POST /v1/change-correlation/correlate

No live Prometheus/Loki — the wire helpers are stubbed; missing creds fail closed.
"""
from datetime import timedelta

from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.app.services import telemetry_connectors as tc
from apps.api.app.services.core import make_id, now_utc
from apps.api.app.services.reliability import ReliabilityService

PID = "boss-dogfood"
ACTOR = "executive_console"
RS = ReliabilityService()


def test_connectors_catalog_lists_known_kinds(client):
    r = client.get("/v1/telemetry/connectors/catalog")
    assert r.status_code == 200, r.text
    body = r.json()
    kinds = {item["kind"] for item in body["items"]}
    assert {"prometheus", "loki", "grafana", "datadog", "pagerduty", "memory"} <= kinds
    assert body["count"] == len(body["items"]) >= 7
    prom = next(i for i in body["items"] if i["kind"] == "prometheus")
    assert "metrics" in prom["capabilities"] and "base_url" in prom["required_config"]


def test_telemetry_query_memory_logs(client):
    r = client.post("/v1/telemetry/query", json={
        "project_id": PID,
        "actor": ACTOR,
        "kind": "logs",
        "query": "refused",
        "window": {"start": 0, "end": 100},
        "source": {
            "kind": "memory",
            "logs": [
                {"ts": 10, "message": "db connection refused", "service": "checkout", "level": "error"},
                {"ts": 20, "message": "ok", "service": "checkout", "level": "info"},
            ],
        },
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "logs" and body["source_kind"] == "memory"
    assert body["count"] == 1
    assert body["items"][0]["service"] == "checkout"
    assert "refused" in body["items"][0]["message"]


def test_telemetry_query_prometheus_stubbed_wire(client, monkeypatch):
    def fake(url, *, headers=None):
        assert "/api/v1/query_range" in url
        return {"data": {"result": [
            {"metric": {"__name__": "up", "job": "api"},
             "values": [[1000, "1"], [1060, "1"]]},
        ]}}

    monkeypatch.setattr(tc, "_http_json", fake)
    r = client.post("/v1/telemetry/query", json={
        "project_id": PID,
        "actor": ACTOR,
        "kind": "metrics",
        "query": "up",
        "window": {"start": 1000, "end": 2000},
        "source": {"kind": "prometheus", "base_url": "http://prom:9090"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["name"] == "up"
    assert [p["value"] for p in body["items"][0]["points"]] == [1.0, 1.0]


def test_telemetry_query_via_registered_source_id(client, monkeypatch, engine):
    def fake(url, *, headers=None):
        return {"data": {"result": [
            {"metric": {"__name__": "http_errors"}, "values": [[1500, "3"]]},
        ]}}

    monkeypatch.setattr(tc, "_http_json", fake)
    with begin(engine) as c:
        row = RS.register_source(
            c, PID, "prometheus", "prom-query-api",
            config={"base_url": "http://prom:9090"},
        )
        source_id = row["id"]

    r = client.post("/v1/telemetry/query", json={
        "project_id": PID,
        "actor": ACTOR,
        "kind": "metrics",
        "query": "http_errors",
        "source_id": source_id,
        "window": {"start": 1000, "end": 2000},
    })
    assert r.status_code == 200, r.text
    assert r.json()["source_id"] == source_id
    assert r.json()["items"][0]["name"] == "http_errors"


def test_telemetry_query_missing_source_fails_422(client):
    r = client.post("/v1/telemetry/query", json={
        "project_id": PID,
        "actor": ACTOR,
        "kind": "metrics",
        "query": "up",
        "window": {"start": 0, "end": 1},
    })
    assert r.status_code == 422


def test_telemetry_query_missing_base_url_fails_closed(client):
    r = client.post("/v1/telemetry/query", json={
        "project_id": PID,
        "actor": ACTOR,
        "kind": "metrics",
        "query": "up",
        "window": {"start": 0, "end": 1},
        "source": {"kind": "prometheus"},  # no base_url
    })
    assert r.status_code == 422, r.text
    assert "base_url" in r.json()["detail"]


def test_telemetry_query_named_bearer_env_missing_fails_closed(client):
    r = client.post("/v1/telemetry/query", json={
        "project_id": PID,
        "actor": ACTOR,
        "kind": "metrics",
        "query": "up",
        "window": {"start": 0, "end": 1},
        "source": {
            "kind": "prometheus",
            "base_url": "http://prom:9090",
            "bearer_env": "PROM_TOKEN_THAT_IS_NOT_SET",
        },
    })
    assert r.status_code == 422, r.text
    assert "PROM_TOKEN_THAT_IS_NOT_SET" in r.json()["detail"]


def test_telemetry_query_datadog_missing_keys_fails_closed(client, monkeypatch):
    monkeypatch.delenv("DD_API_KEY", raising=False)
    monkeypatch.delenv("DD_APP_KEY", raising=False)
    r = client.post("/v1/telemetry/query", json={
        "project_id": PID,
        "actor": ACTOR,
        "kind": "logs",
        "query": "*",
        "window": {"start": 0, "end": 1},
        "source": {"kind": "datadog"},
    })
    assert r.status_code == 422, r.text
    assert "datadog" in r.json()["detail"].lower()


def test_telemetry_query_unreachable_backend_is_503(client, monkeypatch):
    def boom(url, *, headers=None):
        raise OSError("connection refused")

    monkeypatch.setattr(tc, "_http_json", boom)
    r = client.post("/v1/telemetry/query", json={
        "project_id": PID,
        "actor": ACTOR,
        "kind": "metrics",
        "query": "up",
        "window": {"start": 1000, "end": 2000},
        "source": {"kind": "prometheus", "base_url": "http://prom:9090"},
    })
    assert r.status_code == 503, r.text
    assert "failed" in r.json()["detail"].lower() or "refused" in r.json()["detail"].lower()


def test_telemetry_query_unknown_source_id_404(client):
    r = client.post("/v1/telemetry/query", json={
        "project_id": PID,
        "actor": ACTOR,
        "kind": "logs",
        "query": "",
        "source_id": "tsrc_does_not_exist",
        "window": {"start": 0, "end": 1},
    })
    assert r.status_code == 404


def test_change_correlation_by_service_window(client, engine):
    onset = now_utc()
    with begin(engine) as c:
        c.execute(
            text(
                """
                INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, finalize_ctx,
                                            pull_request_number, created_at)
                VALUES (:id, :p, 'completed', 'done', :title, '{}'::jsonb, '{}'::jsonb, :pr, :ts)
                """
            ),
            {
                "id": make_id("crun"),
                "p": PID,
                "title": "risky ship",
                "pr": 4242,
                "ts": onset - timedelta(minutes=4),
            },
        )

    r = client.post("/v1/change-correlation/correlate", json={
        "project_id": PID,
        "actor": ACTOR,
        "service": "checkout",
        "window": {
            "start": (onset - timedelta(minutes=60)).timestamp(),
            "end": onset.timestamp(),
        },
        "lookback_minutes": 60,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["service"] == "checkout"
    assert body["count"] >= 1
    assert any(ch["key"] == "pr-4242" for ch in body["changes"])


def test_change_correlation_by_incident_ref(client, engine):
    with begin(engine) as c:
        rep = RS.scan(
            c, PID,
            {"kind": "memory", "logs": [
                {"ts": i, "message": f"boom {i}", "service": "pay", "level": "error"}
                for i in range(12)
            ]},
            {"start": 0, "end": 9e9},
            persist=True,
        )
        inc_id = rep["incidents"][0]["id"]
        onset = c.execute(
            text("SELECT first_seen FROM incidents WHERE id = :id"),
            {"id": inc_id},
        ).scalar_one()
        c.execute(
            text(
                """
                INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, finalize_ctx,
                                            pull_request_number, created_at)
                VALUES (:id, :p, 'completed', 'done', :title, '{}'::jsonb, '{}'::jsonb, :pr, :ts)
                """
            ),
            {
                "id": make_id("crun"),
                "p": PID,
                "title": "bad deploy",
                "pr": 9991,
                "ts": onset - timedelta(minutes=2),
            },
        )

    r = client.post("/v1/change-correlation/correlate", json={
        "project_id": PID,
        "actor": ACTOR,
        "incident_ref": inc_id,
        "lookback_minutes": 30,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["incident_ref"] == inc_id
    assert body["changes"][0]["key"] == "pr-9991"


def test_change_correlation_requires_incident_or_service(client):
    r = client.post("/v1/change-correlation/correlate", json={
        "project_id": PID,
        "actor": ACTOR,
        "window": {"start": 0, "end": 100},
    })
    assert r.status_code == 422


def test_change_correlation_unknown_incident_404(client):
    r = client.post("/v1/change-correlation/correlate", json={
        "project_id": PID,
        "actor": ACTOR,
        "incident_ref": "inc_missing",
        "window": {"start": 0, "end": 100},
    })
    assert r.status_code == 404
