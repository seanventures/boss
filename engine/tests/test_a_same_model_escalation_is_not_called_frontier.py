"""BOSSAPP-1032: do not claim a frontier model was tried when none was.

Escalation that resolves to the same engine and model that just failed has no
stronger lane. The operator-facing error may say that, or that the same engine
was retried. It must not say "escalating to a frontier model" unless a
different lane actually ran.
"""
from __future__ import annotations

import inspect

from boss_engine.services import governed_code_task_service as g


def test_the_same_engine_and_model_is_not_a_stronger_lane():
    failed = {"engine": "ollama", "model": "llama3.1:8b"}
    assert g.escalation_reached_a_stronger_lane(failed, dict(failed)) is False
    assert g.escalation_reached_a_stronger_lane(
        {"engine": "ollama", "model": None},
        {"engine": "ollama", "model": None}) is False


def test_a_different_engine_or_model_is_a_stronger_lane():
    failed = {"engine": "ollama", "model": "llama3.1:8b"}
    assert g.escalation_reached_a_stronger_lane(
        failed, {"engine": "claude_code", "model": "opus"}) is True
    assert g.escalation_reached_a_stronger_lane(
        {"engine": "claude_code", "model": "haiku"},
        {"engine": "claude_code", "model": "opus"}) is True


def test_no_stronger_lane_is_said_without_claiming_a_frontier_model():
    message = g.write_tests_exhausted_message(stronger_lane=False)
    assert "escalating to a frontier model" not in message
    assert "no stronger lane" in message.lower()


def test_a_stronger_lane_may_say_it_escalated_to_a_frontier_model():
    message = g.write_tests_exhausted_message(stronger_lane=True)
    assert "escalating to a frontier model" in message


def test_the_write_tests_failure_uses_that_message():
    """The sentence the operator reads is chosen here, not hardcoded."""
    source = inspect.getsource(g._run_governed_code_task)
    assert "write_tests_exhausted_message(" in source
    assert "escalation_reached_a_stronger_lane(" in source
    assert 'reason": "no_stronger_lane"' in source or "no_stronger_lane" in source
