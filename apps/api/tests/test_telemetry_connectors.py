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


# --- Splunk / Datadog / CloudWatch / PagerDuty (RB-9 completion) --------------
def test_splunk_normalizes_export_rows(monkeypatch):
    rows = [{"result": {"_time": "1500.5", "_raw": "db timeout on order 9",
                        "source": "orders-app", "log_level": "ERROR"}},
            {"preview": True},                                    # non-result rows skipped
            {"result": {"_time": "bad", "_raw": "fallback ts line", "host": "h1"}}]
    captured = {}
    def fake(url, form, *, headers=None):
        captured.update(url=url, form=form)
        return rows
    monkeypatch.setattr(tc, "_http_ndjson_post", fake)
    events = tc.SplunkSource("https://splunk:8089", index="prod").query_logs("error", WIN)
    assert len(events) == 2
    assert events[0].service == "orders-app" and events[0].level == "error"
    assert events[0].ts == 1500.5
    assert events[1].ts == WIN.start                              # bad _time -> window floor
    assert captured["form"]["search"].startswith("search error")  # SPL prefix added
    assert "output_mode=json" in captured["url"]


def test_datadog_logs_and_metrics(monkeypatch):
    def fake_post(url, body, *, headers=None):
        assert headers["DD-API-KEY"] == "k1"                      # env-name indirection resolved
        return {"data": [{"attributes": {"timestamp_epoch": 1500, "message": "oom",
                                         "service": "worker", "status": "error"}}]}
    def fake_get(url, *, headers=None):
        return {"series": [{"metric": "sys.cpu", "scope": "host:a",
                            "pointlist": [[1500000, 90.0], [1560000, None], [1620000, 95.0]]}]}
    monkeypatch.setenv("DD_API_KEY", "k1")
    monkeypatch.setenv("DD_APP_KEY", "k2")
    monkeypatch.setattr(tc, "_http_json_post", fake_post)
    monkeypatch.setattr(tc, "_http_json", fake_get)
    src = tc.DatadogSource()
    logs = src.query_logs("service:worker", WIN)
    assert logs[0].message == "oom" and logs[0].level == "error"
    series = src.query_metrics("avg:sys.cpu{*}", WIN)
    assert series[0].name == "sys.cpu"
    assert [p.value for p in series[0].points] == [90.0, 95.0]    # None points dropped


def test_cloudwatch_logs_via_injected_client():
    class FakeLogs:
        def filter_log_events(self, **kw):
            assert kw["logGroupName"] == "/app/prod" and kw["startTime"] == 1000000
            return {"events": [{"timestamp": 1500000, "message": "task crashed",
                                "logStreamName": "svc-a"}]}
    src = tc.CloudWatchSource("/app/prod", client_factory=lambda svc: FakeLogs())
    events = src.query_logs("", WIN)
    assert events[0].message == "task crashed" and events[0].service == "svc-a"
    assert events[0].ts == 1500.0


def test_pagerduty_alerts(monkeypatch):
    def fake(url, *, headers=None):
        assert "since=" in url and "until=" in url
        return {"incidents": [{"id": "PD1", "title": "DB down",
                               "priority": {"summary": "P1"},
                               "service": {"summary": "orders"}}]}
    monkeypatch.setattr(tc, "_http_json", fake)
    alerts = tc.PagerDutySource().list_alerts(WIN)
    assert alerts[0].title == "DB down" and alerts[0].severity == "critical"
    assert alerts[0].service == "orders"


def test_build_source_new_kinds():
    assert build_source({"kind": "splunk", "base_url": "http://s"}).kind == "splunk"
    assert build_source({"kind": "datadog"}).kind == "datadog"
    assert build_source({"kind": "cloudwatch", "log_group": "/g"}).kind == "cloudwatch"
    assert build_source({"kind": "pagerduty"}).kind == "pagerduty"
