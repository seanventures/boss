"""RB-1/RB-2 — telemetry mining (templating + detector cascade) + the TelemetrySource seam. Pure."""
import json

from apps.api.app.services import telemetry_mining as tm
from apps.api.app.services.telemetry_source import (
    InMemorySource,
    LogEvent,
    LogExportSource,
    MetricPoint,
    MetricSeries,
    TimeWindow,
)


# --- templating --------------------------------------------------------------
def test_template_masks_variable_tokens():
    assert tm.template_of("user 42 from 10.0.0.1 failed") == "user <NUM> from <IP> failed"
    assert tm.template_of("req 7 from 10.0.0.9 failed") == tm.template_of("user 42 from 10.0.0.1 failed").replace("user", "req")
    # different ids collapse to ONE template (the whole point)
    a = tm.template_of("order 123 total 45.6 shipped")
    b = tm.template_of("order 999 total 12.0 shipped")
    assert a == b == "order <NUM> total <NUM> shipped"


def test_template_masks_uuid_ts_path():
    t = tm.template_of("2026-07-21T10:00:00Z GET /api/v1/users/55c1-uuid ok")
    assert "<TS>" in t and "<PATH>" in t and "GET" in t


# --- template signals --------------------------------------------------------
def _log(msg, level="info", ts=0.0):
    return LogEvent(ts=ts, message=msg, level=level)


def test_cold_start_only_surfaces_novel_errors():
    # cold start (no baseline): benign INFO templates are NOT anomalies (would drown the report)...
    benign = [_log(f"cache miss for key {i}") for i in range(5)] + [_log(f"served page {i}") for i in range(5)]
    assert tm.template_signals(benign, baseline=None, min_count=3) == []
    # ...but a novel ERROR template IS surfaced
    witherr = benign + [_log(f"db connection refused {i}", "error") for i in range(4)]
    sigs = tm.template_signals(witherr, baseline=None, min_count=3)
    assert len(sigs) == 1 and sigs[0].kind == "new_template" and "db connection refused" in sigs[0].key


def test_new_templates_vs_baseline_any_level():
    # WITH a baseline, a genuinely-new template is a signal at any level
    events = [_log(f"feature flag {i} enabled") for i in range(5)]
    sigs = tm.template_signals(events, baseline={"other <NUM>": 3.0}, min_count=3)
    assert len(sigs) == 1 and sigs[0].kind == "new_template"


def test_template_spike_vs_baseline():
    events = [_log(f"payment declined {i}") for i in range(30)] + [_log(f"ok {i}") for i in range(3)]
    baseline = {"payment declined <NUM>": 2.0, "ok <NUM>": 3.0}  # payment usually rare
    sigs = tm.template_signals(events, baseline=baseline, z_threshold=2.0, min_count=3)
    spikes = [s for s in sigs if s.kind == "template_spike"]
    assert any("payment declined" in s.key for s in spikes)


# --- error surge -------------------------------------------------------------
def test_error_surge_cold_start():
    events = [_log("boom", "error") for _ in range(8)] + [_log("ok") for _ in range(12)]
    sig = tm.error_surge_signal(events, baseline_error_rate=None)
    assert sig and sig.kind == "error_surge" and sig.meta["error_rate"] == 0.4
    # below the threshold -> no signal
    calm = [_log("boom", "error") for _ in range(2)] + [_log("ok") for _ in range(98)]
    assert tm.error_surge_signal(calm, baseline_error_rate=None) is None


def test_error_surge_vs_baseline_ratio():
    events = [_log("err", "error") for _ in range(10)] + [_log("ok") for _ in range(10)]  # 0.5 rate
    assert tm.error_surge_signal(events, baseline_error_rate=0.05, ratio_threshold=2.0).score == 10.0


# --- metric signals ----------------------------------------------------------
def test_metric_zscore_spike():
    pts = [MetricPoint(ts=i, value=100.0) for i in range(10)] + [MetricPoint(ts=10, value=500.0)]
    sigs = tm.metric_signals([MetricSeries("latency_p99", pts)], z_threshold=2.5)
    assert sigs and sigs[0].kind == "metric_spike" and sigs[0].key == "latency_p99"


# --- mine() report -----------------------------------------------------------
def test_mine_report_distills_batch():
    events = [_log(f"GET /users/{i} 200") for i in range(100)] + [_log("db connection refused", "error") for _ in range(20)]
    rep = tm.mine(events)
    assert rep["events"] == 120
    assert rep["distinct_templates"] <= 4 and rep["reduction_ratio"] >= 30   # huge cardinality drop
    assert rep["top_templates"][0]["count"] >= 20
    assert any(s["kind"] == "error_surge" for s in rep["signals"])
    assert rep["signal_count"] >= 1


# --- source seam -------------------------------------------------------------
def test_in_memory_source_window_and_filter():
    src = InMemorySource(logs=[_log("a", ts=1), _log("b", ts=5), _log("target here", ts=8)])
    assert len(src.query_logs("", TimeWindow(0, 6))) == 2          # window filter
    hits = src.query_logs("target", TimeWindow(0, 10))
    assert len(hits) == 1 and "target" in hits[0].message


def test_log_export_source_parses_ndjson_and_plain(tmp_path):
    p = tmp_path / "export.log"
    p.write_text(
        json.dumps({"ts": 100, "message": "json line ok", "level": "info", "service": "api"}) + "\n"
        + "2026-07-21 ERROR something broke\n"
        + "\n"                                    # blank line ignored
        + "plain info line\n")
    src = LogExportSource(str(p))
    events = src.query_logs("", TimeWindow(0, 1e12))
    assert len(events) == 3
    assert events[0].service == "api" and events[0].message == "json line ok"
    assert events[1].level == "error"            # sniffed from the plain line
    # feeds mining unchanged (source-agnostic)
    assert tm.mine(events)["events"] == 3
