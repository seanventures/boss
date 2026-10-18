"""Every promql_lite feature Phase 1a adds agrees with Prometheus (BOSSAPP-598, spec §4.2, §13).

`on(...)` / `ignoring(...)` one-to-one matching, `offset`, `absent_over_time`, the formatter (a
scoped query parses back to the same tree) and the built-in rules rendered in the OpenTelemetry
dialects are evaluated by BOSS and by `promtool test rules` on the same series; promtool fails
on any value BOSS got wrong. The v1 agreement test is unchanged beside it.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from boss_engine.services import app_alerts as aa
from boss_engine.services import promql_lite as pq

from .test_alert_rules_mean_the_same_to_boss_and_prometheus import MIN, _promtool

APP, ENV = "checkout", "production"
SEL = {"app": APP, "environment": ENV}

SERIES: dict[tuple, list] = {
    # OTel stable semconv, as Prometheus' OTLP translation names it
    ("http_server_request_duration_seconds_count", ("http_response_status_code", "200")):
        [0, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100],
    ("http_server_request_duration_seconds_count", ("http_response_status_code", "503")):
        [0, 0, 0, 0, 20, 40, 60, 80, 80, 80, 80, 80],
    ("http_server_request_duration_seconds_bucket", ("le", "0.5")):
        [0, 99, 198, 297, 380, 420, 460, 500, 599, 698, 797, 896],
    ("http_server_request_duration_seconds_bucket", ("le", "+Inf")):
        [0, 100, 200, 300, 420, 540, 660, 780, 880, 980, 1080, 1180],
    # older semconv, milliseconds
    ("http_server_duration_milliseconds_count", ("http_status_code", "500")):
        [0, 0, 0, 10, 30, 50, 70, 70, 70, 70, 70, 70],
    ("http_server_duration_milliseconds_count", ("http_status_code", "200")):
        [0, 100, 200, 290, 370, 450, 530, 630, 730, 830, 930, 1030],
    # a ratio by route, and its denominator, for on()/ignoring()
    ("good_total", ("route", "/a")): [0, 10, 20, 30, 35, 40, 45, 55, 65, 75, 85, 95],
    ("all_total", ("route", "/a"), ("kind", "x")): [0, 10, 20, 30, 40, 50, 60, 70, 80, 90,
                                                     100, 110],
    # a heartbeat that stops at minute 5
    ("boss_heartbeat", ("source_id", "src_1")): [1, 1, 1, 1, 1, 1, None, None, None, None, None,
                                                 None],
}


def _store() -> pq.Store:
    s = pq.Store()
    for key, values in SERIES.items():
        name, *labels = key
        s.add({"__name__": name, **SEL, **dict(labels)},
              [(i * MIN, float(v)) for i, v in enumerate(values) if v is not None])
    return s


EXPRS = [
    'sum(rate(good_total{app="checkout"}[3m])) by (route) / on(route) '
    'sum(rate(all_total{app="checkout"}[3m])) by (route)',
    'sum by (route) (rate(good_total[3m])) / ignoring(kind) sum by (route, kind) '
    '(rate(all_total[3m]))',
    'sum(increase(http_server_request_duration_seconds_count[3m] offset 2m))',
    'absent_over_time(boss_heartbeat{source_id="src_1"}[3m])',
]


def test_new_constructs_and_otel_dialect_builtins_mean_what_prometheus_means():
    _promtool()
    s = _store()
    tests = []
    measures = list(EXPRS)
    for dialect in ("otel", "otel_legacy"):
        for kind in ("error_rate", "latency_p95", "no_traffic"):
            if dialect == "otel_legacy" and kind == "latency_p95":
                continue
            r = aa.render(kind, {"window_minutes": 3, "min_requests": 20, "expected_hours": 1},
                          APP, ENV, dialect)
            measures.append(r["value_expr"])
    # what BOSS sends a shared store is the scoped, formatted query: it must mean the same
    measures.append(pq.inject_scope(EXPRS[0], {"environment": ENV}))
    got_any = set()
    for expr in measures:
        for m in range(1, 12):
            measure = f"round({expr}, 0.000001) > -Inf"
            if expr.startswith("absent_over_time"):
                measure = expr
            vals = pq.evaluate(measure, s, m * MIN).samples
            if vals:
                got_any.add(expr)
            tests.append({"expr": measure, "eval_time": f"{m}m",
                          "exp_samples": [{"labels": "{" + ",".join(
                              f'{k}="{v}"' for k, v in sorted(x.labels.items())) + "}",
                              "value": x.value} for x in vals]})
    assert len(got_any) == len(measures), "every expression has a value somewhere in the run"
    series = []
    for key, values in SERIES.items():
        name, *labels = key
        lab = ",".join(f'{k}="{v}"' for k, v in [*SEL.items(), *labels])
        series.append({"series": f"{name}{{{lab}}}",
                       "values": " ".join("_" if v is None else str(v) for v in values)})
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "rules.yml").write_text(yaml.safe_dump({"groups": []}))
        (d / "test.yml").write_text(yaml.safe_dump({
            "rule_files": ["rules.yml"], "evaluation_interval": "1m",
            "tests": [{"interval": "1m", "input_series": series,
                       "promql_expr_test": tests}]}))
        out = subprocess.run(["docker", "run", "--rm", "-v", f"{d}:/w", "-w", "/w",
                              "--entrypoint", "promtool", "prom/prometheus:latest",
                              "test", "rules", "test.yml"],
                             capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, (out.stdout + out.stderr)[-4000:]


def test_a_scoped_query_parses_back_to_the_same_meaning():
    for expr in EXPRS:
        once = pq.inject_scope(expr, {"project": "p1"})
        assert pq.format_expr(once) == once
        assert all(("project", "=", "p1") in sel.matchers for sel in _selectors(pq.parse(once)))


def _selectors(node):
    out = []
    if isinstance(node, pq.Selector):
        return [node]
    for k in ("expr", "lhs", "rhs"):
        if hasattr(node, k):
            out += _selectors(getattr(node, k))
    for a in getattr(node, "args", []) or []:
        out += _selectors(a)
    return out
