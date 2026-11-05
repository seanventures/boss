"""The operator's feedback on the installed monitoring build (BOSSAPP-598):

1. Grafana is bundled and has the apps' series: BOSS forwards each scrape (raw), probe and open
   alert to the bundled Prometheus, writes a per-app dashboard, and a link opens where the
   browser can reach it — only after BOSS asked and Grafana answered.
2. SAMPLE rules, clearly labelled and removable; their alerts come only from evaluation.
3. Every rule shows itself as a query: PromQL, interval, window, for, keep_firing_for,
   threshold, min requests, severity, routing.
4. The builder's metric picker reads the app's ACTUAL series; the preview replays on the
   rule's datasource (Prometheus when it is the one), scoped to the project.
5. SEV1-SEV4, one vocabulary: rules, alerts, incidents, routing and messages.
6. One status chip per alert and incident, and "needs attention" = firing and nobody has it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.services import alert_samples
from boss_engine.services import app_alerts as aa
from boss_engine.services import app_alerts_view as view
from boss_engine.services import bundled_monitoring as bundled
from boss_engine.services import monitoring as mon
from boss_engine.services import notify
from boss_engine.services import promql_lite as pq
from boss_engine.services import rule_preview
from boss_engine.services import severity as sev
from boss_engine.services.app_metrics import parse_exposition

T0 = datetime(2026, 10, 18, 7, 0, tzinfo=timezone.utc)

EXPOSITION = """# HELP http_requests_total Requests.
# TYPE http_requests_total counter
http_requests_total{method="GET",route="/orders",status="200"} 120
http_requests_total{method="GET",route="/orders",status="500"} 3
http_requests_created{method="GET",route="/orders",status="200"} 1.7e9
# HELP http_request_duration_seconds Latency.
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{method="GET",route="/orders",le="0.5"} 100
http_request_duration_seconds_bucket{method="GET",route="/orders",le="+Inf"} 123
http_request_duration_seconds_sum{method="GET",route="/orders"} 40
http_request_duration_seconds_count{method="GET",route="/orders"} 123
# HELP orders_created_total Orders created.
# TYPE orders_created_total counter
orders_created_total{result="ok"} 40
"""


@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _app(c, pid, name="Order API") -> dict:
    tid = f"t_fb_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains) VALUES (:t, :p, :a, 'acme/shop', 'staging', "
        " 'stack', 'order-api', 'active', 'test', '[]'::jsonb)"), {"t": tid, "p": pid, "a": name})
    mon.ensure_target_services(c, pid)
    return dict(c.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                          {"t": tid}).mappings().one())


def _scrape(c, t, at, requests, errors):
    snap = {"requests": requests, "errors": errors, "routes": {}, "buckets": {},
            "hist_count": None, "hist_sum": None}
    c.execute(text(
        "INSERT INTO app_metrics_scrapes (id, project_id, deployment_target_id, scraped_at, ok, "
        " word, snapshot) VALUES (:i, :p, :t, :at, true, 'Scraped', CAST(:s AS jsonb))"),
        {"i": f"ams_{uuid4().hex}", "p": t["project_id"], "t": t["id"], "at": at,
         "s": json.dumps(snap)})


# ── 5. severity ───────────────────────────────────────────────────────────────────────────
def test_one_severity_vocabulary_maps_the_older_words():
    assert [sev.level(w) for w in ("critical", "warning", "info", "SEV2", "sev4", None)] == \
        ["sev1", "sev3", "sev4", "sev2", "sev4", "sev3"]
    assert sev.urgency("sev2") == "high" and sev.urgency("sev3") == "low"
    assert sev.legacy("sev2") == "critical" and sev.legacy("sev4") == "info"
    assert sev.worst(["warning", "sev2", "info"]) == "sev2"
    assert [c["label"] for c in sev.catalog()] == ["SEV1", "SEV2", "SEV3", "SEV4"]
    assert "Pages" in sev.tip("critical")
    # routing matches the normalised level whatever word the rule used
    labels = mon.alert_labels({"labels": {"severity": "critical"}, "alertname": "X"})
    assert labels["sev"] == "sev1"


def test_a_rule_takes_sev1_to_sev4_and_refuses_anything_else(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        aa.ensure_rules(c, t)
        r = aa.update_rule(c, t, None, {"alert": "Slow", "expr": "up == 0",
                                        "labels": {"severity": "SEV2"}}, actor="usr_t")
        assert r["labels"]["severity"] == "sev2"
        er = next(x for x in aa.rules(c, t["id"]) if x["kind"] == "error_rate")
        assert aa.update_rule(c, t, er["id"], {"severity": "sev4"},
                              actor="usr_t")["labels"]["severity"] == "sev4"
        with pytest.raises(aa.RuleRefused):
            aa.update_rule(c, t, er["id"], {"severity": "loud"}, actor="usr_t")


def test_a_notification_leads_with_the_worst_level():
    alerts = [{"state": "firing", "alertname": "A", "labels": {"severity": "sev3"},
               "annotations": {"summary": "slow"}, "subject_id": "s"},
              {"state": "firing", "alertname": "B", "labels": {"severity": "critical"},
               "annotations": {"summary": "down"}, "subject_id": "s"}]
    subject, body = notify._render("firing", alerts, {"s": "Order API"}, None, ack_url=None,
                                   console_url=None)
    assert subject.startswith("[SEV1 FIRING:2]")
    assert "• SEV3 Order API: slow" in body and "• SEV1 Order API: down" in body


# ── 6. status ─────────────────────────────────────────────────────────────────────────────
def test_every_alert_says_plainly_whether_it_is_still_open():
    now = T0
    base = {"active_at": now - timedelta(minutes=12), "fired_at": now - timedelta(minutes=10),
            "cleared_at": None, "suppressed_by": [], "silenced_by": None, "inhibited_by": None}
    firing = view.alert_status({**base, "state": "firing"}, None, now)
    assert (firing["key"], firing["tone"]) == ("firing", "red")
    acked = view.alert_status({**base, "state": "firing"}, "Param", now)
    assert acked["key"] == "acknowledged" and "Param" in acked["word"]
    assert view.alert_status({**base, "state": "pending"}, None, now)["key"] == "pending"
    done = view.alert_status({**base, "state": "resolved", "cleared_at": now}, None, now)
    assert done["key"] == "resolved" and "10 min" in done["tip"]
    assert view.duration_s({**base, "cleared_at": now}) == 600
    muted = view.alert_status({**base, "state": "firing",
                               "suppressed_by": [{"kind": "maintenance"}]}, None, now)
    assert (muted["key"], muted["tone"]) == ("maintenance", "muted")


def test_needs_attention_is_firing_and_nobody_has_it(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        for name, acked in (("Open", False), ("Taken", True)):
            c.execute(text(
                "INSERT INTO app_alerts (id, project_id, target_id, rule_kind, "
                " alertname, fingerprint, labels, state, active_at, fired_at, acked_at, acked_by, "
                " ack_expires_at) VALUES (:i, :p, :t, 'custom', :n, :f, "
                " '{\"severity\": \"sev2\"}', 'firing', now(), now(), :at, :by, :exp)"),
                {"i": f"al_{uuid4().hex[:8]}", "p": pid, "t": t["id"], "n": name,
                 "f": uuid4().hex[:16], "at": datetime.now(timezone.utc) if acked else None,
                 "by": "usr_param" if acked else None,
                 "exp": datetime.now(timezone.utc) + timedelta(hours=4) if acked else None})
        everything = view.alerts(c, pid, state="firing")
        assert everything["counts"]["needs_attention"] == 1
        assert {a["alertname"]: a["needs_attention"] for a in everything["alerts"]} == \
            {"Open": True, "Taken": False}
        assert next(a for a in everything["alerts"] if a["alertname"] == "Taken")["status"][
            "key"] == "acknowledged"
        only = view.alerts(c, pid, attention=True)
        assert [a["alertname"] for a in only["alerts"]] == ["Open"]
        assert all(a["sev"] == "sev2" for a in everything["alerts"])
        assert [a["alertname"] for a in view.alerts(c, pid, state="firing",
                                                    severity="sev2")["alerts"]]
        assert view.alerts(c, pid, state="firing", severity="sev1")["alerts"] == []


# ── 3. rules as queries ───────────────────────────────────────────────────────────────────
def test_every_rule_shows_itself_as_a_query(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        tab = view.app_alerts_tab(c, None, pid, t["id"])
        er = next(r for r in tab["rules"] if r["kind"] == "error_rate")
        f = er["facts"]
        assert f["expr"] == er["expr"] and "[5m]" in f["expr"]
        assert (f["window"], f["for_s"], f["threshold"], f["min_requests"]) == \
            ("5m", 0, "> 5", 20)
        assert f["interval_s"] == 60 and f["routing"].startswith("Console")
        assert er["sev"] == "sev1" and tab["severities"][0]["label"] == "SEV1"
        hb = aa.update_rule(c, t, None, {"alert": "Gone", "expr": "absent_over_time(up[3m])",
                                         "keep_firing_for": "2m"}, actor="usr_t")
        f2 = view.rule_facts(c, hb, t, 30)
        assert (f2["threshold"], f2["window"], f2["keep_firing_for_s"], f2["interval_s"]) == \
            ("absent", "3m", 120, 30)


# ── 2. samples ────────────────────────────────────────────────────────────────────────────
def test_samples_are_labelled_idempotent_and_removable(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        aa.ensure_rules(c, t)
        got = alert_samples.seed(c, t, actor="usr_t", now=T0)
        assert "SampleErrorBudgetBurn" in got["created"] and "SampleNoDeletes" in got["created"]
        assert alert_samples.seed(c, t, actor="usr_t", now=T0)["created"] == []
        samples = [r for r in aa.rules(c, t["id"]) if view.is_sample(r)]
        assert len(samples) == len(got["created"])
        assert all(r["annotations"]["summary"].startswith("SAMPLE: ") for r in samples)
        assert {sev.level(r["labels"]) for r in samples} >= {"sev1", "sev2", "sev3", "sev4"}
        for r in samples:                       # every one is PromQL BOSS can evaluate
            pq.parse(r["expr"])
        assert c.execute(text("SELECT count(*) FROM app_alerts WHERE target_id = :t"),
                         {"t": t["id"]}).scalar() == 0      # seeding writes no alert
        assert view.app_alerts_tab(c, None, pid, t["id"])["has_samples"] is True
        gone = alert_samples.remove(c, t, actor="usr_t", now=T0)
        assert gone["removed"] == len(samples)
        assert not [r for r in aa.rules(c, t["id"]) if view.is_sample(r)]
        assert len(aa.rules(c, t["id"])) == 5         # the built-ins stay


def test_a_sample_fires_and_resolves_from_real_evaluation(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        aa.ensure_rules(c, t)
        alert_samples.seed(c, t, actor="usr_t", now=T0)
        req = err = 0.0
        for i in range(16):                       # 30 req/min, 3 of them 5xx: 10% > 0.5%
            _scrape(c, t, T0 + timedelta(minutes=i), req, err)
            req, err = req + 30, err + 3
    for i in (10, 12, 13):
        with eng.begin() as c:
            aa.evaluate_target(c, t, T0 + timedelta(minutes=i))
    with eng.begin() as c:
        row = c.execute(text("SELECT state, labels FROM app_alerts WHERE target_id = :t AND "
                             "alertname = 'SampleErrorBudgetBurn'"), {"t": t["id"]}).one()
        assert row.state == "firing" and row.labels["sample"] == "true"
        for i in range(16, 40):                   # the errors stop
            _scrape(c, t, T0 + timedelta(minutes=i), req, err)
            req += 30
    with eng.begin() as c:
        aa.evaluate_target(c, t, T0 + timedelta(minutes=39))
        assert c.execute(text("SELECT state FROM app_alerts WHERE target_id = :t AND "
                              "alertname = 'SampleErrorBudgetBurn'"),
                         {"t": t["id"]}).scalar() == "resolved"


# ── 4. the builder's metric names and the preview ─────────────────────────────────────────
def test_the_builder_offers_the_series_the_app_actually_exports(pg):
    eng, pid = pg
    fams = alert_samples.exposition_families(EXPOSITION, parse_exposition(EXPOSITION))
    by = {f["name"]: f for f in fams}
    assert set(by) == {"http_requests_total", "http_request_duration_seconds",
                       "orders_created_total"}       # histogram by base, _created left out
    assert by["http_request_duration_seconds"]["type"] == "histogram"
    assert by["http_requests_total"]["labels"] == ["method", "route", "status"]
    with eng.begin() as c:
        t = _app(c, pid)
        alert_samples.record_families(c, t["id"], EXPOSITION)
        got = alert_samples.metric_names(c, t)
        names = [m["name"] for m in got["metrics"]]
        assert "orders_created_total" in names and "probe_success" in names
        assert got["selector"] == 'app="Order API",environment="staging"'


class _FakeProm:
    def __init__(self, rows):
        self.rows, self.asked = rows, []

    def query_range(self, expr, start, end, step_s):
        self.asked.append(expr)
        return self.rows


def test_the_preview_replays_on_prometheus_scoped_to_the_project(pg):
    eng, pid = pg
    now = T0
    ms = int(now.timestamp()) // 60 * 60 * 1000
    # true for minutes -30..-20 (11 steps), then false: with for=5m it fired once
    rows = [({"app": "Order API"}, [(ms - k * 60_000, 1.0) for k in range(20, 31)])]
    prom = _FakeProm(rows)
    with eng.begin() as c:
        t = _app(c, pid)
        got = rule_preview.preview(c, t, "sum(rate(orders_created_total[5m])) > 1", "5m",
                                   now=now, prometheus=prom, scope={"project": pid})
    assert got["fired"] == 1 and got["evaluated_by"] == "Prometheus"
    assert f'project="{pid}"' in prom.asked[0]


# ── 1. the bundled Prometheus and Grafana ─────────────────────────────────────────────────
def test_a_scrape_reaches_prometheus_raw_and_stamped():
    t = {"id": "t1", "project_id": "p1", "app_name": "Order API", "environment": "staging"}
    series = bundled.scrape_series(t, parse_exposition(EXPOSITION), T0)
    lab = next(lab for lab, _ in series if lab["__name__"] == "orders_created_total")
    assert lab == {"__name__": "orders_created_total", "result": "ok", "app": "Order API",
                   "environment": "staging", "project": "p1", "target_id": "t1",
                   "job": "boss", "instance": "t1"}
    alerts = bundled.alert_series([{"alertname": "SampleLatencyP95", "state": "firing",
                                    "labels": {"severity": "sev3", "sample": "true"},
                                    "project_id": "p1", "app": "Order API",
                                    "environment": "staging"}], T0)
    assert alerts[0][0]["__name__"] == "ALERTS" and alerts[0][0]["sev"] == "sev3"


def test_a_restart_that_drops_the_5xx_series_reads_as_zero_error_rate():
    """BOSSAPP-621: after a restart the 5xx counter is gone while 2xx continues, so
    `100 * sum(rate(5xx)) / sum(rate(all))` is empty. The series ends and lastNotNull keeps
    the old error rate. A missing 5xx numerator is 0; the window stays 5m."""
    d = json.loads(bundled.render_dashboard("Order API", "staging"))
    exprs = {p["title"]: p["targets"][0]["expr"] for p in d["panels"]}
    ratio = exprs["Error rate"]
    assert exprs["Error rate (5xx %)"] == ratio
    assert "or vector(0)" in ratio and "[5m]" in ratio
    pq.parse(ratio)
    t0 = 1_700_000_000_000

    def series(status: str, step: float) -> list[tuple[int, float]]:
        return [(t0 + i * 60_000, float(i) * step) for i in range(8)]

    def at(samples: list[tuple[str, float]]) -> float:
        store = pq.Store()
        for status, step in samples:
            store.add({"__name__": "http_requests_total", "app": "Order API",
                       "environment": "staging", "status": status}, series(status, step))
        got = pq.instant_vector(ratio, store, t0 + 7 * 60_000)
        assert got, "traffic is still arriving, so the ratio is a number"
        return got[0].value

    assert at([("200", 100.0)]) == 0.0
    assert 40.0 < at([("200", 50.0), ("500", 50.0)]) < 60.0


def test_the_stack_dashboard_5xx_series_is_zero_when_that_series_is_gone():
    """BOSSAPP-621: the stack deploy writes the same Order API dashboard. Its 5xx rate ends
    when the series is gone. Request rate stays empty with no traffic — a flat zero there
    would read as a healthy app that was never instrumented."""
    from boss_engine.services.observability_stack import render_dashboard

    d = json.loads(render_dashboard({
        "app": "Order API", "environment": "staging", "project": "p", "repo": "o/r",
        "port": 1, "scrape_url": "h:1", "probe_url": "http://h/"}))
    expr = next(p["targets"][0]["expr"] for p in d["panels"] if p["title"] == "Error rate (5xx/s)")
    req = next(p["targets"][0]["expr"] for p in d["panels"] if p["title"] == "Request rate (req/s)")
    assert "or vector(0)" in expr and "[5m]" in expr
    assert "or vector(0)" not in req
    pq.parse(expr)
    t0 = 1_700_000_000_000
    store = pq.Store()
    store.add({"__name__": "http_requests_total", "app": "Order API", "status": "200"},
              [(t0 + i * 60_000, float(i) * 100) for i in range(8)])
    got = pq.instant_vector(expr, store, t0 + 7 * 60_000)
    assert got and got[0].value == 0.0
    bare = pq.instant_vector(expr, pq.Store(), t0 + 7 * 60_000)
    # No traffic at all: the default is 0, which is what a missing counter means.
    assert bare and bare[0].value == 0.0


def test_each_app_gets_a_dashboard_with_the_golden_signals_and_its_alerts():
    d = json.loads(bundled.render_dashboard("Order API", "staging"))
    assert d["uid"] == "boss-order-api"
    titles = [p["title"] for p in d["panels"]]
    for want in ("Health (BOSS probe)", "Requests / s", "Error rate", "p95 latency",
                 "Latency p50 / p95"):
        assert want in titles
    exprs = " ".join(t["expr"] for p in d["panels"] for t in p["targets"])
    assert "histogram_quantile(0.5" in exprs and "histogram_quantile(0.95" in exprs
    for e in [t["expr"] for p in d["panels"] for t in p["targets"]]:
        pq.parse(e)
    ann = [a for a in d["annotations"]["list"] if "expr" in a]
    assert ann and 'ALERTS{app="Order API"' in ann[0]["expr"]


def test_the_link_opens_where_the_browser_reaches_grafana_and_only_when_it_answered(
        pg, monkeypatch):
    from boss_engine.config import get_settings
    from boss_engine.services import observability_stack as stack

    eng, pid = pg
    s = get_settings()
    monkeypatch.setattr(s, "monitoring_grafana_url", "http://grafana:3000")
    monkeypatch.setattr(s, "monitoring_grafana_public_url", "http://localhost:3300")
    monkeypatch.setattr(s, "monitoring_prometheus_url", "http://prometheus:9090")
    with eng.begin() as c:
        _app(c, pid)
        assert stack.grafana_base(c, pid) == "http://grafana:3000"
        assert stack.endpoint_state(c, pid, "grafana")["url"] is None        # never asked
        stack.record_check(c, pid, "grafana", "http://grafana:3000",
                           {"state": "answering", "detail": {"dashboards": ["boss-order-api"]}})
        state = stack.endpoint_state(c, pid, "grafana")
        assert state["url"] == "http://localhost:3300"
        assert stack.dashboard_link(state, "Order API")[0] == \
            "http://localhost:3300/d/boss-order-api"
        assert stack.dashboard_link(state, "HelpDesk")[0] is None             # no dashboard yet
        stack.record_check(c, pid, "prometheus", "http://prometheus:9090",
                           {"state": "answering", "detail": {}})
        prom = aa.prometheus_for(c, pid)
        assert prom.scope == {"project": pid}                     # never asked unscoped
        assert f'project="{pid}"' in prom._scoped("sum(up)")


def test_dashboards_are_written_once_per_change(pg, tmp_path):
    eng, pid = pg

    class S:
        monitoring_dashboards_dir = str(tmp_path)

    with eng.begin() as c:
        _app(c, pid, name=f"Shop {uuid4().hex[:4]}")
        first = bundled.write_dashboards(c, S())
        assert first and (tmp_path / f"{first[-1]}.json").exists()
        assert bundled.write_dashboards(c, S()) == []
