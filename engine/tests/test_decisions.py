"""Decision follow-on regressions that do not need a database."""
from __future__ import annotations

from types import SimpleNamespace

from boss_engine.services import decisions


def test_forced_merge_records_the_operator_note(monkeypatch):
    service = object.__new__(decisions.DecisionService)
    service.settings = SimpleNamespace(enable_sandbox_merge=False)
    service._pull_request_ci_gate = lambda *_args: {
        "status": "failed", "reason": "CI failed"
    }
    queued: list[tuple] = []
    service._queue_merge_job_from_review = lambda *args: queued.append(args)
    events: list[dict] = []
    monkeypatch.setattr(decisions, "emit_event", lambda *_args: events.append(_args[-1]))
    row = {
        "id": "dec_1",
        "project_id": "project_1",
        "task_id": None,
        "recommendation": {
            "action": "review_pull_request",
            "pull_request_url": "https://example.test/pull/7",
        },
    }

    service._handle_approved_follow_on(
        object(), row, "ceo", note="CI is waived for this emergency fix.", force=True
    )

    assert events == [{
        "reason": "CI is waived for this emergency fix.",
        "overridden_status": "failed",
        "overridden_reason": "CI failed",
        "pull_request_url": "https://example.test/pull/7",
        "note": "operator override — BOSS's automated merge gate did not pass",
    }]
    assert len(queued) == 1
