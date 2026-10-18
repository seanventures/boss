"""An app alert rule means the same thing to BOSS's monitor and to Prometheus (BOSSAPP-598).

The rules are PromQL. BOSS evaluates them itself (promql_lite, over its own scrapes) whenever the
deployed Prometheus is not reachable, so the two evaluators must agree on every value and on
when an alert fires — otherwise "the rule" would be two rules. These tests pin the semantics
BOSS implements (Prometheus' extrapolated rate, its histogram quantile, filtering comparisons,
one-to-one matching, the pending/firing lifecycle) and then hand the SAME series and the SAME
rule file to `promtool test rules` with BOSS's answers as the expected ones: promtool fails the
test if real Prometheus disagrees with BOSS on any value or any alert.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from boss_engine.services import app_alerts as aa
from boss_engine.services import promql_lite as pq

APP, ENV = "Order API", "staging"
SEL = {"app": APP, "environment": ENV}
MIN = 60_000


def store(series: dict[tuple, list]) -> pq.Store:
    """{(name, (label, value), ...): [value per minute or None]} -> a Store, one sample/min."""
    s = pq.Store()
    for key, values in series.items():
        name, *labels = key
        s.add({"__name__": name, **SEL, **dict(labels)},
              [(i * MIN, float(v)) for i, v in enumerate(values) if v is not None])
    return s


# ── the semantics, pure ─────────────────────────────────────────────────────────────────────
def test_increase_extrapolates_like_prometheus():
    s = store({("c",): [0, 10, 20, 30, 40, 50, 60]})
    # samples at 2..6 min are inside (1m, 6m]: 40 counted over 4 min, extrapolated to 5 min
    got = pq.evaluate("increase(c[5m])", s, 6 * MIN).samples[0].value
    assert got == pytest.approx(50.0)
    # a counter that restarts is counted across the reset
    r = store({("c",): [100, 110, 5, 15]})
    assert pq.evaluate("increase(c[10m])", r, 3 * MIN).samples[0].value > 0
    # one sample in the window: no rate, never a zero
    assert pq.evaluate("increase(c[1m])", s, 6 * MIN).samples == []


def test_histogram_quantile_and_comparisons_filter():
    s = store({("h_bucket", ("le", "0.1")): [0, 50], ("h_bucket", ("le", "1")): [0, 90],
               ("h_bucket", ("le", "+Inf")): [0, 100]})
    q = pq.evaluate("histogram_quantile(0.95, sum by (le) (increase(h_bucket[5m])))", s, MIN)
    # rank 95 of 100 falls in (1, +Inf]: the highest finite bound
    assert q.samples[0].value == pytest.approx(1.0)
    assert pq.evaluate("vector(3) > 2", s, MIN).samples[0].value == 3.0
    assert pq.evaluate("vector(1) > 2", s, MIN).samples == []
    assert pq.evaluate("vector(1) > bool 2", s, MIN).samples[0].value == 0.0
    assert pq.evaluate("vector(1) and vector(2)", s, MIN).samples[0].value == 1.0


def test_what_boss_cannot_evaluate_is_said_never_guessed():
    for expr in ("sum(rate(c[5m])) / on(app) sum(rate(d[5m]))", "c offset 5m",
                 "max_over_time(c[10m:1m])", "topk(3, c)", "predict_linear(c[1h], 60)"):
        with pytest.raises(pq.PromQLUnsupported):
            pq.parse(expr)
    with pytest.raises(pq.PromQLError):
        pq.parse("sum(rate(c[5m])")


def test_the_lifecycle_is_prometheus_alerts():
    hold = 2 * MIN
    assert aa.lifecycle(None, None, True, 0, hold) == "pending"
    assert aa.lifecycle("pending", 0, True, MIN, hold) == "pending"
    assert aa.lifecycle("pending", 0, True, 2 * MIN, hold) == "firing"
    assert aa.lifecycle("pending", 0, False, MIN, hold) is None, "a pending alert never fired"
    assert aa.lifecycle("firing", 0, False, 3 * MIN, hold) == "resolved"
    assert aa.lifecycle(None, None, True, 0, 0) == "firing"


def test_the_built_in_rules_are_promql_over_the_platform_metrics():
    er = aa.render("error_rate", {"threshold": 5, "window_minutes": 5, "min_requests": 20},
                   APP, ENV)
    assert er["expr"] == (
        '100 * sum(increase(http_requests_total{app="Order API",environment="staging",'
        'status=~"5.."}[5m])) / sum(increase(http_requests_total{app="Order API",'
        'environment="staging"}[5m])) > 5 and sum(increase(http_requests_total{'
        'app="Order API",environment="staging"}[5m])) >= 20')
    for kind in aa.KINDS:
        r = aa.render(kind, {}, APP, ENV)
        pq.parse(r["expr"])            # every built-in is PromQL BOSS can evaluate
        assert 'app="Order API"' in r["expr"]
    assert aa.boss_only(aa.render("deploy_health", {}, APP, ENV)["expr"])
    assert not aa.boss_only(er["expr"])
    exported = aa.export_rules([{"alert": "AppErrorRate", "expr": er["expr"], "for_seconds": 0,
                                 "labels": {"severity": "critical"},
                                 "annotations": {"summary": "x", "runbook_url": "y"}}])
    rule = exported["groups"][0]["rules"][0]
    assert set(rule) == {"alert", "expr", "for", "labels", "annotations"}


# ── the agreement, through promtool ─────────────────────────────────────────────────────────
def _promtool() -> None:
    if not shutil.which("docker"):
        pytest.skip("needs docker for promtool")
    probe = subprocess.run(["docker", "image", "inspect", "prom/prometheus:latest"],
                           capture_output=True)
    if probe.returncode != 0:
        pytest.skip("needs the prom/prometheus image for promtool")


SCENARIO_MIN = 16
SERIES: dict[tuple, list] = {
    # errors burst at minute 4 and stop at 9: the error-rate rule fires, then clears
    ("http_requests_total", ("status", "500"), ("route", "/orders")):
        [0, 0, 0, 0, 10, 30, 50, 70, 90, 90, 90, 90, 90, 90, 90, 90],
    ("http_requests_total", ("status", "200"), ("route", "/orders")):
        [0, 100, 200, 300, 390, 470, 550, 630, 710, 810, 910, 1010, 1110, 1110, 1110, 1110],
    # requests slow down from minute 5 to 9 (p95 above 800 ms), then speed up again
    ("http_request_duration_seconds_bucket", ("le", "0.1")):
        [0, 90, 180, 270, 360, 380, 400, 420, 440, 530, 620, 710, 800, 800, 800, 800],
    ("http_request_duration_seconds_bucket", ("le", "1")):
        [0, 99, 198, 297, 396, 420, 444, 468, 492, 591, 690, 789, 888, 888, 888, 888],
    ("http_request_duration_seconds_bucket", ("le", "+Inf")):
        [0, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1200, 1200, 1200],
    ("http_request_duration_seconds_count",):
        [0, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1200, 1200, 1200],
    # the health probe fails from minute 6 to 10
    ("probe_success",): [1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
    ("boss_deploy_health_ok",): [1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 1, 1, 1, 1],
}


def _rules() -> list[dict]:
    rows = []
    params = {"error_rate": {"window_minutes": 3, "min_requests": 20},
              "latency_p95": {"window_minutes": 3, "min_requests": 20},
              "health_check": {"window_minutes": 4, "min_failures": 3},
              "no_traffic": {"window_minutes": 3, "expected_hours": 1},
              "deploy_health": {}}
    for kind in aa.KINDS:
        b = aa.BUILTIN[kind]
        rows.append({"kind": kind, "alert": b["alert"],
                     "expr": aa.render(kind, params[kind], APP, ENV)["expr"],
                     "value_expr": aa.render(kind, params[kind], APP, ENV)["value_expr"],
                     "for_seconds": 0, "labels": {"severity": b["severity"],
                                                  "layer": b["layer"]},
                     "annotations": {"summary": f"{b['label']} on {APP}",
                                     "runbook_url": b["runbook"]}})
    # a person's own rule, with a `for`: pending for two minutes before it fires
    rows.append({"kind": "custom", "alert": "CheckoutErrors",
                 "expr": 'sum(rate(http_requests_total{app="Order API",environment="staging",'
                         'status=~"5.."}[2m])) > 0.1',
                 "value_expr": 'sum(rate(http_requests_total{app="Order API",'
                               'environment="staging",status=~"5.."}[2m]))',
                 "for_seconds": 120, "labels": {"severity": "warning"},
                 "annotations": {"summary": "Checkout errors"}})
    return rows




def test_alert_rules_mean_the_same_to_boss_and_prometheus():
    _promtool()
    s = store(SERIES)
    rules = _rules()
    evals = list(range(1, SCENARIO_MIN))
    expr_tests, alert_tests = [], []
    fired_somewhere = set()
    for r in rules:
        state, active = None, None
        for m in evals:
            t = m * MIN
            # Compared to a millionth (both sides round with PromQL's own round(): the last
            # bits of a float differ between two implementations of one formula), and a NaN —
            # nothing to measure — is no sample on either side.
            measure = f"round({r['value_expr']}, 0.000001) > -Inf"
            got = pq.evaluate(measure, s, t).samples
            expr_tests.append({"expr": measure, "eval_time": f"{m}m",
                               "exp_samples": [{"labels": "{}", "value": x.value}
                                               for x in got]})
            present = bool(pq.instant_vector(r["expr"], s, t))
            nxt = aa.lifecycle(state, active, present, t, r["for_seconds"] * 1000)
            if nxt == "pending" and state != "pending":
                active = t
            state = nxt
            firing = state == "firing"
            if firing:
                fired_somewhere.add(r["alert"])
            alert_tests.append({"eval_time": f"{m}m", "alertname": r["alert"],
                                "exp_alerts": ([{"exp_labels": r["labels"],
                                                 "exp_annotations": r["annotations"]}]
                                               if firing else [])})
            if state == "resolved":
                state, active = None, None
    # the scenario is worth running only if every rule fires and clears in it
    assert fired_somewhere >= {"AppErrorRate", "AppLatencyP95", "AppHealthCheckFailing",
                               "AppNoTraffic", "AppDeployUnhealthy", "CheckoutErrors"}, \
        fired_somewhere
    series = []
    for key, values in SERIES.items():
        name, *labels = key
        lab = ",".join(f'{k}="{v}"' for k, v in [*SEL.items(), *labels])
        series.append({"series": f"{name}{{{lab}}}",
                       "values": " ".join("_" if v is None else str(v) for v in values)})
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "rules.yml").write_text(yaml.safe_dump(aa.export_rules(rules)))
        (d / "test.yml").write_text(yaml.safe_dump({
            "rule_files": ["rules.yml"], "evaluation_interval": "1m",
            "tests": [{"interval": "1m", "input_series": series,
                       "promql_expr_test": expr_tests, "alert_rule_test": alert_tests}]}))
        out = subprocess.run(["docker", "run", "--rm", "-v", f"{d}:/w", "-w", "/w",
                              "--entrypoint", "promtool", "prom/prometheus:latest",
                              "test", "rules", "test.yml"],
                             capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, (out.stdout + out.stderr)[-4000:]
