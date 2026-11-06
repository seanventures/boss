"""A repo with no CI never reports a check, so waiting for one ends in "timed_out".

That is absence of evidence, not a failure: when BOSS's own gates passed, an approved merge
goes ahead (helpdesk-ai PR #2 was blocked after approval with every BOSS gate green).
"""
from __future__ import annotations

import pytest

from boss_engine.services import decisions

_ALL_GREEN = {"passed": ["written_tests_pass", "existing_tests_intact", "nothing_broke"],
              "failed": [], "run_id": "crun_1"}


class _Connection:
    def __init__(self, content):
        self.content = content

    def execute(self, *_args, **_kwargs):
        content = self.content

        class _Result:
            def mappings(self):
                return self

            def first(self):
                return {"content": content}
        return _Result()


def _gate(monkeypatch, status, own):
    service = object.__new__(decisions.DecisionService)
    service._boss_own_verdict = lambda *_args: own
    monkeypatch.setattr(decisions, "emit_event", lambda *_args: None)
    row = {"id": "dec_1", "project_id": "p",
           "recommendation": {"test_report_artifact_id": "art_1"}}
    return service._pull_request_ci_gate(
        _Connection({"status": status, "summary": "Timed out while waiting"}), row)


def test_a_timed_out_wait_for_ci_that_never_came_merges_on_boss_own_checks(monkeypatch):
    assert _gate(monkeypatch, "timed_out", _ALL_GREEN) is None


def test_a_timed_out_wait_still_blocks_when_boss_own_checks_failed(monkeypatch):
    own = {**_ALL_GREEN, "failed": ["written_tests_pass"]}
    assert _gate(monkeypatch, "timed_out", own)["status"] == "blocked_boss_validation_failed"


@pytest.mark.parametrize("status", ["failed"])
def test_a_ci_failure_still_blocks(monkeypatch, status):
    assert _gate(monkeypatch, status, _ALL_GREEN)["status"] == "blocked_ci_validation_failed"
