"""Deterministic activity-row classifier (BOSS-379 slice 1).

Pure unit tests: no database, no network, no spend.
"""
from __future__ import annotations

from boss_engine.services.activity_classify import classify_activity, redact_activity_data


def _assert_shape(result: dict) -> None:
    assert set(result) == {"status", "stage", "severity", "title"}
    assert result["status"] in {"ok", "failed", "waiting", "stuck", "info"}
    assert result["severity"] in {"info", "warn", "error"}
    assert isinstance(result["stage"], str)
    assert isinstance(result["title"], str)


def test_classify_failed_from_event_type():
    result = classify_activity({"event_type": "job.failed", "status": "running"})
    _assert_shape(result)
    assert result["status"] == "failed"
    assert result["severity"] == "error"
    assert result["title"] == "Job Failed"


def test_classify_failed_from_status_keywords():
    for status in ("error", "deadlettered", "rejected", "blocked"):
        result = classify_activity({"event_type": "job.updated", "status": status})
        _assert_shape(result)
        assert result["status"] == "failed", status
        assert result["severity"] == "error"


def test_classify_success_completed():
    result = classify_activity({"event_type": "code_task.completed", "status": "succeeded"})
    _assert_shape(result)
    assert result["status"] == "ok"
    assert result["severity"] == "info"
    assert result["title"] == "Code Task Completed"
    assert "score" not in result["title"].lower()


def test_classify_success_handoff_succeeded():
    result = classify_activity({"action": "handoff succeeded", "status": "accepted"})
    _assert_shape(result)
    assert result["status"] == "ok"
    assert result["title"] == "Handoff Succeeded"


def test_never_ok_unless_name_or_status_says_success():
    result = classify_activity({"event_type": "budget.reserved", "status": "open"})
    _assert_shape(result)
    assert result["status"] == "info"
    assert result["severity"] == "info"


def test_classify_waiting():
    result = classify_activity({"event_type": "job.leased", "status": "queued"})
    _assert_shape(result)
    assert result["status"] == "waiting"
    assert result["severity"] == "info"
    assert result["title"] == "Job Leased"


def test_classify_stuck_expired_still_open():
    result = classify_activity(
        {"event_type": "job.lease_expired", "status": "expired", "stage": "implement"}
    )
    _assert_shape(result)
    assert result["status"] == "stuck"
    assert result["severity"] == "warn"
    assert result["stage"] == "implement"


def test_classify_stuck_not_ok_when_timeout_still_open():
    result = classify_activity({"event_type": "worker.timeout", "status": "running"})
    _assert_shape(result)
    assert result["status"] == "stuck"
    assert result["status"] != "ok"


def test_classify_info_default_and_empty_stage():
    result = classify_activity({"event_type": "event.emitted"})
    _assert_shape(result)
    assert result["status"] == "info"
    assert result["severity"] == "info"
    assert result["stage"] == ""
    assert result["title"] == "Event Emitted"


def test_failed_wins_over_success_words():
    result = classify_activity(
        {"event_type": "handoff.failed", "status": "completed", "action": "handoff succeeded"}
    )
    _assert_shape(result)
    assert result["status"] == "failed"
    assert result["severity"] == "error"


def test_redact_drops_secret_keys_and_masks_string_values():
    redacted = redact_activity_data(
        {
            "token": "tok_live_should_drop",
            "secret": "super-secret",
            "password": "hunter2",
            "api_key": "AKIAIOSFODNN7EXAMPLE",
            "authorization": "Bearer 0123456789abcdef0123",
            "note": "ping ops@example.com",
            "attempt": 2,
        }
    )
    assert "token" not in redacted
    assert "secret" not in redacted
    assert "password" not in redacted
    assert "api_key" not in redacted
    assert "authorization" not in redacted
    assert redacted["note"] == "ping [EMAIL]"
    assert redacted["attempt"] == 2


def test_redact_empty_data():
    assert redact_activity_data({}) == {}
    assert redact_activity_data(None) == {}
