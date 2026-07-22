"""RB-9 — Prometheus/Loki/Grafana connectors: request shaping + normalization into canonical shapes,
auth indirection, and a full scan through a stubbed Grafana. No network — the wire fn is stubbed."""
import pytest

from apps.api.app.services import telemetry_connectors as tc
from apps.api.app.services.reliability import ReliabilityService, build_source
from apps.api.app.services.telemetry_source import TimeWindow

RS = ReliabilityService()
WIN = TimeWindow(1000, 2000)


@pytest.fixture
def wire(monkeypatch):
    """Capture requested URLs; serve canned payloads per API path."""
    calls = []

    def fake(url, *, headers=None):
        calls.append({"url": url, "headers": headers or {}})
        # NB: check the Loki path FIRST — it contains "/api/v1/query_range" as a substring
        if "/loki/api/v1/query_range" in url:
            return {"data": {"result": [
                {"stream": {"service": "checkout", "level": "error"},
                 "values": [[str(int(1.5e12)), "db connection refused"],
                            [str(int(1.6e12)), "db connection refused"],
                            [str(int(1.7e12)), "db connection refused"]]}]}}
        if "/api/v1/query_range" in url:
            return {"data": {"result": [
                {"metric": {"__name__": "http_errors", "service": "checkout"},
                 "values": [[1000, "1"], [1060, "2"], [1120, "40"]]}]}}
        if "/api/v1/alerts" in url:
            return {"data": {"alerts": [
                {"labels": {"alertname": "HighErrorRate", "service": "checkout",
                            "severity": "critical"}}]}}
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(tc, "_http_json", fake)
    return calls


# --- Prometheus ---------------------------------------------------------------
def test_prometheus_metrics_normalize(wire):
    src = tc.PrometheusSource("http://prom:9090", step_seconds=60)
    series = src.query_metrics("rate(http_errors[5m])", WIN)
    assert len(series) == 1
    s = series[0]
    assert s.name == "http_errors" and s.labels == {"service": "checkout"}
    assert [p.value for p in s.points] == [1.0, 2.0, 40.0]
    assert "query=rate%28http_errors%5B5m%5D%29" in wire[0]["url"]
    assert "step=60" in wire[0]["url"]


def test_prometheus_alerts(wire):
    alerts = tc.PrometheusSource("http://prom:9090").list_alerts(WIN)
    assert alerts[0].title == "HighErrorRate" and alerts[0].severity == "critical"
    assert alerts[0].service == "checkout"


def test_bearer_env_indirection(wire, monkeypatch):
    monkeypatch.setenv("TEST_PROM_TOKEN", "sekret")
    tc.PrometheusSource("http://prom:9090", bearer_env="TEST_PROM_TOKEN").query_metrics("up", WIN)
    assert wire[-1]["headers"].get("Authorization") == "Bearer sekret"
    # unset env -> no auth header, never a crash
    tc.PrometheusSource("http://prom:9090", bearer_env="MISSING_VAR").query_metrics("up", WIN)
    assert "Authorization" not in wire[-1]["headers"]


# --- Loki ---------------------------------------------------------------------
def test_loki_logs_normalize(wire):
    events = tc.LokiSource("http://loki:3100").query_logs('{service="checkout"}', WIN)
    assert len(events) == 3
    e = events[0]
    assert e.service == "checkout" and e.level == "error" and "refused" in e.message
    assert e.ts == 1.5e12 / 1e9                       # ns -> s
    # window converted to nanoseconds in the request
    assert f"start={int(1000 * 1e9)}" in wire[0]["url"]


# --- Grafana proxy ------------------------------------------------------------
def test_grafana_routes_through_datasource_proxy(wire):
    src = tc.GrafanaSource("http://grafana:3000", prometheus_uid="prom-uid", loki_uid="loki-uid",
                           bearer_env="")
    src.query_metrics("up", WIN)
    assert "/api/datasources/proxy/uid/prom-uid/api/v1/query_range" in wire[0]["url"]
    src.query_logs("", WIN)
    assert "/api/datasources/proxy/uid/loki-uid/loki/api/v1/query_range" in wire[1]["url"]


# --- build_source + end-to-end scan through the connector ---------------------
def test_build_source_kinds():
    assert build_source({"kind": "prometheus", "base_url": "http://p"}).kind == "prometheus"
    assert build_source({"kind": "loki", "base_url": "http://l"}).kind == "loki"
    assert build_source({"kind": "grafana", "base_url": "http://g"}).kind == "grafana"


def test_scan_through_grafana_connector(wire, engine):
    """The point of the seam: a Grafana-backed scan mines Loki logs + flags the Prometheus spike,
    identically to an in-memory source."""
    from apps.api.app.db import begin
    with begin(engine) as c:
        rep = RS.scan(c, "boss-dogfood",
                      {"kind": "grafana", "base_url": "http://grafana:3000",
                       "prometheus_uid": "prom-uid", "loki_uid": "loki-uid"},
                      {"start": 1000, "end": 2000}, query="", persist=False)
        assert rep["events"] == 3                              # Loki lines mined
        assert rep["distinct_templates"] == 1
        # error lines -> a signal (novel error template on cold start)
        assert any(s["kind"] in ("new_template", "error_surge") for s in rep["signals"])
