"""A build's implement stage has a wall-time budget, and fails fast past it.

A HelpDesk AI build ran 120 minutes into the job's 2-hour limit and ended with nothing but a
timeout. The implement stage now has a budget (build_implement_budget_minutes, default 25), fix
round and continuations included: past it BOSS starts no new session, a running session's
timeout is capped at the deadline, and the build fails with its failing tests listed. The job
timeout stays as the backstop.
"""
from __future__ import annotations

import inspect
import time

import pytest

from boss_engine.services import code_agent, governed_code_task_service as g
from boss_engine.services import project_settings as ps
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context


@pytest.fixture(autouse=True)
def _clean():
    clear_llm_context()
    yield
    clear_llm_context()


def test_the_budget_is_a_project_setting_defaulting_to_25_minutes():
    knob = ps.KNOBS["build_implement_budget_minutes"]
    assert knob["kind"] == "int" and knob["min"] == 0
    from boss_engine.config import Settings
    assert Settings.model_fields["build_implement_budget_minutes"].default == 25


def test_a_writing_session_never_runs_past_the_stage_deadline():
    set_llm_context(stage_deadline=time.time() + 300)
    assert 290 <= code_agent._cap_to_deadline(2400, read_only=False) <= 300
    set_llm_context(stage_deadline=time.time() - 5)
    assert code_agent._cap_to_deadline(2400, read_only=False) == 60   # never zero: a last minute
    assert code_agent._cap_to_deadline(420, read_only=True) == 420    # a reviewer is not cut
    clear_llm_context()
    assert code_agent._cap_to_deadline(2400, read_only=False) == 2400
    assert "_cap_to_deadline(timeout, read_only=read_only)" in inspect.getsource(
        code_agent.ClaudeCodeAgent._invoke)


def test_failing_tests_are_named():
    out = ("not ok 3 - creates a ticket\n  ✗ agent can reply\nFAILED tests/test_x.py::test_roles\n"
           "ok 4 - lists tickets\n")
    assert g.failing_tests(out) == ["creates a ticket", "agent can reply", "tests/test_x.py::test_roles"]


def test_the_pipeline_stops_at_the_budget_and_records_it():
    src = inspect.getsource(g)
    assert "set_llm_context(stage_deadline=_impl_deadline)" in src
    assert "set_llm_context(stage_deadline=None)" in src
    assert "and not _impl_over_budget()" in src                     # no continuation past it
    assert "code_task.stage_budget_exhausted" in src and "'{stage_budget}'" in src
    assert "_within_budget(lambda: a.run_role(" in src
