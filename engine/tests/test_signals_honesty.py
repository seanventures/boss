"""Honesty tests for telemetry_mining + reliability scores — no invented numbers.

Pure (no Postgres). Scores are measured z-scores / error rates, or None for ∞.
"""
from __future__ import annotations

from boss_engine.services.telemetry_mining import (
    error_surge_signal,
    mine,
    template_of,
)
from boss_engine.services.telemetry_source import LogEvent


def _log(msg: str, level: str = "info", ts: float = 0.0) -> LogEvent:
    return LogEvent(ts=ts, message=msg, level=level)


def test_template_is_deterministic():
    assert template_of("user 42 from 10.0.0.1 failed") == "user <NUM> from <IP> failed"


def test_mine_calm_logs_do_not_invent_anomaly_scores():
    events = [_log(f"cache miss for key {i}") for i in range(40)]
    rep = mine(events)
    assert rep["events"] == 40
    assert rep["signal_count"] == 0
    assert rep["signals"] == []


def test_mine_serializes_inf_as_none_not_a_fake_score():
    events = [_log(f"db connection refused {i}", "error") for i in range(5)]
    rep = mine(events)
    new_templates = [s for s in rep["signals"] if s["kind"] == "new_template"]
    assert new_templates
    for sig in new_templates:
        assert sig["score"] is None
        assert sig["count"] == 5


def test_error_surge_score_is_the_measured_rate():
    events = [_log("boom", "error") for _ in range(8)] + [_log("ok") for _ in range(12)]
    sig = error_surge_signal(events, baseline_error_rate=None)
    assert sig is not None
    assert sig.score == 0.4
    assert sig.meta["error_rate"] == 0.4


def test_anomaly_classifier_uses_measured_counts_no_score():
    from boss_engine.services.anomaly_detector import classify_operational_anomalies

    empty = classify_operational_anomalies(
        deadlettered=0, recent_spend_micros=0, hour_spend_micros=0
    )
    assert empty == []

    below = classify_operational_anomalies(
        deadlettered=4, recent_spend_micros=0, hour_spend_micros=0
    )
    assert below == []

    storm = classify_operational_anomalies(
        deadlettered=5, recent_spend_micros=0, hour_spend_micros=0
    )
    assert storm == [
        {"type": "retry_storm", "detail": "5 deadlettered jobs in 15m"}
    ]
    assert "score" not in storm[0]

    spike = classify_operational_anomalies(
        deadlettered=0,
        recent_spend_micros=400,
        hour_spend_micros=120,  # baseline 10/5m; 400 > 30
    )
    assert spike[0]["type"] == "spend_spike"
    assert "400" in spike[0]["detail"]
    assert "score" not in spike[0]
