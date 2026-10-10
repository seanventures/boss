"""An app that declares `metrics: {path: /metrics}` is scraped, and its traffic is shown.

The gap (D1 helpdesk, gap 5): monitoring was an outside HTTP probe only — uptime, probe latency,
an incident after three failed probes. No request count, no error rate, no app-side latency.
Now a deployed app's own Prometheus page is scraped on the probe's cadence, through the route
the probe uses, windowed into request rate / 5xx error rate / p50 / p95, stored per app, shown
in Observability, and alerted on through the incidents ledger.

A REAL HTTP server plays the app here: its page changes between scrapes, restarts (a counter
reset), answers 404, serves garbage and hangs — every outcome must come back as numbers or as
words, never as an exception.
"""
from __future__ import annotations

import http.server
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.config import get_settings
from boss_engine.services import app_metrics, deployment_health, infra_view, obs_view
from boss_engine.services.app_deploy_manifest import ManifestError, load_manifest
from boss_engine.services.app_metrics import (
    MetricsFormatError, aggregate, extract, histogram_quantile, judge, parse_exposition,
    window_delta,
)


# ── the fake app ─────────────────────────────────────────────────────────────────────────────
def page(ok: int, err: int, b005: int, b01: int, b05: int) -> str:
    total = ok + err
    return (
        "# HELP http_requests_total HTTP requests\n"
        "# TYPE http_requests_total counter\n"
        f'http_requests_total{{method="GET",route="/tickets/:id",status="200"}} {ok}\n'
        f'http_requests_total{{method="GET",route="/tickets/:id",status="500"}} {err}\n'
        "# TYPE http_request_duration_seconds histogram\n"
        f'http_request_duration_seconds_bucket{{method="GET",route="/tickets/:id",le="0.05"}} {b005}\n'
        f'http_request_duration_seconds_bucket{{method="GET",route="/tickets/:id",le="0.1"}} {b01}\n'
        f'http_request_duration_seconds_bucket{{method="GET",route="/tickets/:id",le="0.5"}} {b05}\n'
        f'http_request_duration_seconds_bucket{{method="GET",route="/tickets/:id",le="+Inf"}} {total}\n'
        f'http_request_duration_seconds_count{{method="GET",route="/tickets/:id"}} {total}\n'
        f'http_request_duration_seconds_sum{{method="GET",route="/tickets/:id"}} {total * 0.08}\n')


class FakeApp:
    def __init__(self):
        self.status = 200
        self.body = page(0, 0, 0, 0, 0)
        self.delay = 0.0
        self.hits: list[str] = []
        app = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                app.hits.append(self.path)
                if app.delay:
                    time.sleep(app.delay)
                data = app.body.encode()
                self.send_response(app.status if self.path == "/metrics" else 200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.srv.server_address[1]}"


@pytest.fixture(scope="module")
def app():
    a = FakeApp()
    yield a
    a.srv.shutdown()


def _seed(c, pid: str, *, url: str | None, metrics: dict | None, health_url: str = "",
          host: bool = False) -> str:
    tid = f"t_{uuid4().hex[:10]}"
    app_name = f"helpdesk-{tid[-4:]}"
    host_id = None
    if host:
        host_id = f"h_{uuid4().hex[:8]}"
        c.execute(text("INSERT INTO deployment_hosts (id, project_id, name, address) "
                       "VALUES (:h, :p, :n, '203.0.113.7')"),
                  {"h": host_id, "p": pid, "n": host_id})
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains, public_url, host_id) VALUES (:t, :p, :a, "
        " 'acme/helpdesk', 'prod', 'stack', :a, 'active', 'test', '[]'::jsonb, :u, :h)"),
        {"t": tid, "p": pid, "a": app_name, "u": url, "h": host_id})
    manifest = {"name": app_name, "health": [{"url": health_url, "expect": 200}] if health_url
                else [], "metrics": metrics}
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, host_id, repository, ref, "
        " commit_sha, manifest, status, created_by, finished_at) VALUES (:id, :p, :t, :h, "
        " 'acme/helpdesk', 'main', 'abc123', CAST(:m AS jsonb), 'healthy', 'test', now())"),
        {"id": f"sd_{uuid4().hex[:10]}", "p": pid, "t": tid, "h": host_id,
         "m": json.dumps(manifest)})
    return tid


def _target(eng, tid: str) -> dict:
    with eng.begin() as c:
        return dict(c.execute(text(
            "SELECT t.*, " + deployment_health.STACK_OPEN_URL_COLUMN.format(alias="t")
            + " FROM deployment_targets t WHERE t.id = :t"), {"t": tid}).mappings().one())


def _scrape(eng, tid: str, at: datetime, **kw) -> dict:
    t = _target(eng, tid)
    with eng.begin() as c:
        decl = app_metrics.declarations(c, [tid])[tid]
    return app_metrics.scrape_target(eng, get_settings(), t, decl, now=at, **kw)


# ── pure: the format ─────────────────────────────────────────────────────────────────────────
def test_the_parser_reads_labels_escapes_specials_and_timestamps():
    got = parse_exposition(
        '# HELP x y\n'
        'a_total{path="/a\\"b\\\\c",x="1",} 3 1700000000000\n'
        'b NaN\nc +Inf\nd -1.5e3\n# EOF\n')
    assert got[0].name == "a_total" and got[0].labels == {"path": '/a"b\\c', "x": "1"}
    assert got[0].value == 3 and got[2].value == float("inf") and got[3].value == -1500.0


@pytest.mark.parametrize("bad, said", [
    ('http_requests_total{status="200" 5', "line 1"),
    ("http_requests_total", "has no value"),
    ("http_requests_total 5 6 7", "unexpected text"),
    ("http_requests_total abc", "not a number"),
    ("<html><body>login</body></html>", "HTML"),
    ("# only comments\n", "no samples"),
])
def test_a_malformed_page_is_refused_in_words(bad, said):
    with pytest.raises(MetricsFormatError) as e:
        parse_exposition(bad)
    assert said in str(e.value)


def test_naming_variants_are_all_read():
    instrumentator = extract(parse_exposition(
        'http_requests_total{handler="/t",method="GET",status="2xx"} 95\n'
        'http_requests_total{handler="/t",method="GET",status="5xx"} 5\n'
        'http_request_duration_seconds_bucket{handler="/t",le="1.0"} 100\n'
        'http_request_duration_seconds_bucket{handler="/t",le="+Inf"} 100\n'
        'http_request_duration_highr_seconds_bucket{le="0.01"} 50\n'
        'http_request_duration_highr_seconds_bucket{le="+Inf"} 100\n'
        'http_request_duration_highr_seconds_count 100\n'))
    assert instrumentator["requests"] == 100 and instrumentator["errors"] == 5
    assert instrumentator["histogram_family"] == "http_request_duration_highr_seconds"
    assert instrumentator["naming"] == "fastapi-instrumentator"

    starlette = extract(parse_exposition(
        'starlette_requests_total{method="GET",path="/",status_code="503"} 3\n'
        'starlette_requests_total{method="GET",path="/",status_code="200"} 7\n'
        'starlette_request_duration_seconds_bucket{method="GET",path="/",le="0.1"} 10\n'
        'starlette_request_duration_seconds_bucket{method="GET",path="/",le="+Inf"} 10\n'))
    assert (starlette["requests"], starlette["errors"]) == (10, 3)
    assert starlette["naming"] == "starlette-exporter"

    code_label = extract(parse_exposition(
        'http_requests_total{code="502"} 2\nhttp_requests_total{code="200"} 8\n'))
    assert code_label["errors"] == 2

    no_status = extract(parse_exposition("http_requests_total 8\n"))
    assert no_status["requests"] == 8 and no_status["errors"] is None

    histogram_only = extract(parse_exposition(
        'http_request_duration_seconds_bucket{le="0.1"} 4\n'
        'http_request_duration_seconds_bucket{le="+Inf"} 6\n'))
    assert histogram_only["requests"] == 6

    with pytest.raises(MetricsFormatError, match="no request metrics"):
        extract(parse_exposition("process_cpu_seconds_total 4\n"))


def test_window_math_count_errors_quantiles_and_a_counter_reset():
    s1 = extract(parse_exposition(page(90, 10, 40, 80, 95)))
    s2 = extract(parse_exposition(page(270, 30, 120, 240, 290)))
    d = window_delta(s1, s2)
    assert (d["requests"], d["errors"], d["reset"]) == (200, 20, False)
    # rank 100 lands in (0.05, 0.1] holding 80 of them, 20 in: 0.05 + 0.05 * 20/80
    assert histogram_quantile(0.5, d["buckets"]) == pytest.approx(0.0625)
    # rank 190 lands in (0.1, 0.5] holding 35, 30 in: 0.1 + 0.4 * 30/35
    assert histogram_quantile(0.95, d["buckets"]) == pytest.approx(0.1 + 0.4 * 30 / 35)

    restarted = extract(parse_exposition(page(18, 2, 10, 15, 19)))
    r = window_delta(s2, restarted)
    assert r["reset"] is True and (r["requests"], r["errors"]) == (20, 2), \
        "after a restart the new counter IS the delta, never a negative one"

    assert window_delta(None, s1)["requests"] is None, "one scrape is not a window"


def test_quantile_edges_follow_prometheus():
    assert histogram_quantile(0.5, {}) is None
    assert histogram_quantile(0.5, {"0.1": 5.0}) is None, "no +Inf bucket, no answer"
    assert histogram_quantile(0.5, {"0.1": 0.0, "+Inf": 0.0}) is None, "nothing observed"
    assert histogram_quantile(0.99, {"0.1": 1.0, "+Inf": 10.0}) == 0.1, \
        "a rank in +Inf answers the highest finite bound"
    assert histogram_quantile(0.5, {"0.2": 10.0, "+Inf": 10.0}) == pytest.approx(0.1), \
        "the first bucket starts at 0"


def test_the_alert_judges_only_on_enough_traffic():
    rule = dict(app_metrics.DEFAULT_ALERT)
    assert judge({"requests": 10, "error_rate": 0.5}, rule) == "quiet"
    assert judge({"requests": 100, "error_rate": 0.06}, rule) == "firing"
    assert judge({"requests": 100, "error_rate": 0.05}, rule) == "ok", "at the line is not over"
    assert judge({"requests": 100, "error_rate": 0.9}, {**rule, "enabled": False}) == "off"
    assert aggregate([])["error_rate"] is None


# ── the declaration ──────────────────────────────────────────────────────────────────────────
BASE = "version: 1\nname: helpdesk\ninstall: [make up]\n"


def test_the_manifest_declares_metrics_and_the_summary_carries_it():
    m = load_manifest(BASE + "metrics: {path: /metrics, interval: 30, via: host, "
                             "alert: {error_rate_percent: 2, window_minutes: 10}}\n")
    assert m.summary()["metrics"] == {"path": "/metrics", "interval_seconds": 30, "via": "host",
                                      "alert": {"error_rate_percent": 2.0, "window_minutes": 10}}
    assert load_manifest(BASE + "metrics: /metrics\n").metrics == {"path": "/metrics",
                                                                    "via": "auto"}
    assert load_manifest(BASE).summary()["metrics"] is None


@pytest.mark.parametrize("decl, said", [
    ("{path: 'http://evil.example/metrics'}", "no scheme or host"),
    ("{path: //evil.example/metrics}", "no scheme or host"),
    ("{path: metrics}", "starting with '/'"),
    ("{path: '/metrics?x=1'}", "no query"),
    ("{path: /a/../metrics}", "'..'"),
    ("{path: /metrics, interval: 5}", "between 15 and 3600"),
    ("{path: /metrics, interval: 99999}", "between 15 and 3600"),
    ("{path: /metrics, via: tunnel}", "metrics.via"),
    ("{path: /metrics, scheme: https}", "unknown field"),
    ("{path: /metrics, alert: {error_rate_percent: 0}}", "between"),
    ("{path: /metrics, alert: {page: true}}", "unknown field"),
])
def test_a_bad_metrics_declaration_is_refused_naming_the_field(decl, said):
    with pytest.raises(ManifestError) as e:
        load_manifest(BASE + f"metrics: {decl}\n")
    assert said in str(e.value) and "metrics" in str(e.value)


# ── scrape → store → alert → view, against the real fake app ─────────────────────────────────
@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def test_scrapes_become_traffic_an_alert_fires_and_resolves(pg, app):
    eng, pid = pg
    with eng.begin() as c:
        tid = _seed(c, pid, url=app.origin + "/", metrics={"path": "/metrics", "via": "public"})
    t0 = datetime.now(timezone.utc).replace(microsecond=0)

    app.status, app.body = 200, page(90, 10, 40, 80, 95)
    first = _scrape(eng, tid, t0)
    assert first["ok"] and first["requests"] is None and "/metrics" in app.hits
    assert first["route"] == "public" and first["url"] == app.origin + "/metrics"

    app.body = page(270, 30, 120, 240, 290)
    second = _scrape(eng, tid, t0 + timedelta(seconds=60))
    assert (second["requests"], second["errors"]) == (200, 20)
    assert second["alert"]["verdict"] == "firing" and second["alert"]["action"] == "opened"

    app.body = page(18, 2, 10, 15, 19)                      # the app restarted
    third = _scrape(eng, tid, t0 + timedelta(seconds=120))
    assert third["reset"] is True and third["requests"] == 20
    assert third["alert"]["action"] == "still_firing"

    with eng.begin() as c:
        inc = c.execute(text("SELECT status, title, severity FROM incidents WHERE project_id = :p "
                             "AND dedupe_key = :k"),
                        {"p": pid, "k": app_metrics.INCIDENT_PREFIX + tid}).mappings().one()
        sigs = c.execute(text("SELECT count(*) FROM anomaly_signals s JOIN incidents i ON "
                              "i.id = s.incident_id WHERE i.dedupe_key = :k AND "
                              "s.kind = 'error_surge'"),
                         {"k": app_metrics.INCIDENT_PREFIX + tid}).scalar()
        view = app_metrics.metrics_view(c, {**_target(eng, tid)},
                                        now=t0 + timedelta(seconds=120))
        cards = [i for i in infra_view.attention(c, get_settings(), pid)
                 if i["infra_kind"] == "app_errors"]
    assert inc["status"] == "open" and "10.0% of requests failing" in inc["title"]
    assert sigs == 2
    assert view["declared"] and view["state"] == "ok" and view["word"] == "Scraped"
    assert view["requests"] == 220 and view["errors"] == 22 and view["error_rate"] == 0.1
    assert view["rate_per_min"] == 110.0, "220 requests over the two 60 s windows"
    # p50/p95 over the SUMMED bucket deltas of both windows: 90/175/214/220
    summed = {"0.05": 90.0, "0.1": 175.0, "0.5": 214.0, "+Inf": 220.0}
    assert view["p50_ms"] == pytest.approx(round(histogram_quantile(0.5, summed) * 1000, 1))
    assert view["p95_ms"] == pytest.approx(round(histogram_quantile(0.95, summed) * 1000, 1))
    assert view["alert"]["state"] == "firing" and view["alert"]["word"] == "Alerting"
    assert len(view["series"]) == 3 and view["series"][1]["rpm"] == 200.0
    assert view["series"][1]["err"] == 0.1 and view["series"][1]["p50"] == 62.5
    assert len(cards) == 1 and cards[0]["area"] == "observability"

    app.body = page(418, 2, 300, 400, 415)                  # recovered: 400 more, 0 errors
    fourth = _scrape(eng, tid, t0 + timedelta(seconds=480))
    assert fourth["alert"]["verdict"] == "ok" and fourth["alert"]["action"] == "resolved"
    with eng.begin() as c:
        status = c.execute(text("SELECT status FROM incidents WHERE dedupe_key = :k"),
                           {"k": app_metrics.INCIDENT_PREFIX + tid}).scalar()
        assert not [i for i in infra_view.attention(c, get_settings(), pid)
                    if i["infra_kind"] == "app_errors"]
    assert status == "resolved"

    app.body = page(418, 100, 300, 400, 500)                # fails again: the same row reopens
    again = _scrape(eng, tid, t0 + timedelta(seconds=540))
    assert again["alert"]["action"] == "reopened"


def test_failures_are_said_in_words_and_never_raise(pg, app, monkeypatch):
    eng, pid = pg
    with eng.begin() as c:
        tid = _seed(c, pid, url=app.origin, metrics={"path": "/metrics", "via": "public"})
    t0 = datetime.now(timezone.utc)

    app.status, app.body = 404, "not found"
    assert _scrape(eng, tid, t0)["word"] == "Metrics endpoint returned 404"

    app.status, app.body = 200, 'http_requests_total{status="200" 5\n'
    said = _scrape(eng, tid, t0 + timedelta(seconds=1))["word"]
    assert said.startswith("Metrics page is not usable: line 1")

    monkeypatch.setattr(app_metrics, "FETCH_TIMEOUT_SECONDS", 1)
    app.delay, app.body = 2.5, page(1, 0, 1, 1, 1)
    try:
        said = _scrape(eng, tid, t0 + timedelta(seconds=2))["word"]
    finally:
        app.delay = 0.0
    assert said == "Metrics endpoint timed out after 1s"

    with eng.begin() as c:
        view = app_metrics.metrics_view(c, _target(eng, tid), now=t0 + timedelta(seconds=3))
    assert view["state"] == "failing" and view["word"] == said and view["requests"] is None

    with eng.begin() as c:
        gone = _seed(c, pid, url="http://127.0.0.1:9", metrics={"path": "/metrics"})
    assert _scrape(eng, gone, t0)["word"] == "Metrics endpoint refused the connection"


class LocalHost:
    """The host channel, played by this machine: the command runs in a local shell, so its curl
    reaches the fake app the way a host's curl reaches 127.0.0.1:<port>."""

    def __init__(self):
        self.commands: list[tuple[str, bool]] = []

    def run(self, command, *, timeout=120, write=False, **kw):
        self.commands.append((command, write))
        p = subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                           timeout=timeout)
        return type("R", (), {"returncode": p.returncode, "stdout": p.stdout,
                              "stderr": p.stderr})()


def test_an_app_reachable_only_from_its_host_is_scraped_over_the_host_channel(pg, app):
    eng, pid = pg
    with eng.begin() as c:
        # The public URL refuses; the deploy's health check ran on the host at the app's origin.
        tid = _seed(c, pid, url="http://127.0.0.1:9", metrics={"path": "/metrics"},
                    health_url=app.origin + "/health", host=True)
    app.status, app.body = 200, page(5, 0, 5, 5, 5)
    host = LocalHost()
    got = _scrape(eng, tid, datetime.now(timezone.utc), host_access=host)
    assert got["ok"] and got["route"] == "host" and got["url"] == app.origin + "/metrics"
    assert host.commands and host.commands[0][1] is False, "a scrape is a READ on the host"

    app.status = 404
    said = _scrape(eng, tid, datetime.now(timezone.utc) + timedelta(seconds=1),
                   host_access=host)["word"]
    assert said == "Metrics endpoint returned 404"


def test_the_health_sweep_scrapes_on_its_cadence_and_observability_shows_it(pg, app):
    eng, pid = pg
    with eng.begin() as c:
        tid = _seed(c, pid, url=app.origin + "/", metrics={"path": "/metrics", "via": "public",
                                                           "interval_seconds": 60})
        quiet = _seed(c, pid, url=app.origin + "/", metrics=None)
    app.status, app.body = 200, page(10, 0, 10, 10, 10)
    t0 = datetime.now(timezone.utc)
    s = get_settings()
    first = deployment_health.sweep(eng, s, project_id=pid, now=t0)
    assert first["scraped"] == 1, first
    assert deployment_health.sweep(eng, s, project_id=pid,
                                   now=t0 + timedelta(seconds=30))["scraped"] == 0, "not due"
    assert deployment_health.sweep(eng, s, project_id=pid,
                                   now=t0 + timedelta(seconds=61))["scraped"] == 1

    with eng.begin() as c:
        body = obs_view.app_detail(c, s, pid, tid)
        none = obs_view.app_detail(c, s, pid, quiet)
    m = body["metrics"]
    assert m["declared"] and m["path"] == "/metrics" and m["last_scrape_at"]
    assert m["alert"]["rule"]["error_rate_percent"] == 5.0
    assert none["metrics"]["declared"] is False and "boss.deploy.yaml" in none["metrics"]["tip"]

    hits = len(app.hits)
    obs_view.probe_app(eng, s, pid, tid)
    assert len(app.hits) > hits, "Probe now scrapes the declared metrics too"


# ── the builder is told ──────────────────────────────────────────────────────────────────────
def test_the_build_brief_asks_for_metrics_and_their_declaration():
    """A web service a build agent writes must expose /metrics AND declare it, or the monitor
    above never sees it. The brief carries both, with a FastAPI example."""
    from boss_engine.services.platform_standards import (
        METRICS_TEST_REQUIREMENT, platform_standards,
    )

    brief = platform_standards("a FastAPI helpdesk api")
    assert "metrics: {path: /metrics}" in brief and "boss.deploy.yaml" in brief
    assert "from prometheus_client import" in brief and '@app.middleware("http")' in brief
    assert "Counter(" in brief and "Histogram(" in brief
    assert "metrics: {path: /metrics}" in METRICS_TEST_REQUIREMENT
    assert platform_standards("a pure library for parsing dates") == "" or \
        "boss.deploy.yaml" not in platform_standards("a pure library for parsing dates")
