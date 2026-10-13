"""Each build stage runs the CLI at its own --effort.

Measured on a HelpDesk-size build: about 89% of output tokens were reasoning and narration and
only about 11% became file content. The writing stages carry out a plan that design and plan
review already decided, so they run at the project's writing effort (build_effort_writing,
default medium). Design, plan review and the reviewers judge, so they run at high. Anything that
is not a build stage (a chat, research) keeps the CLI's own default: no flag.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.services import code_agent, governed_code_task_service as g
from boss_engine.services import project_settings as ps
from boss_engine.services.code_agent import EFFORT_LEVELS, effort_map, stage_effort
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context
from tests.test_a_build_fans_out_inside_its_sandbox import _flag, _launch


@pytest.fixture(autouse=True)
def _clean_context():
    clear_llm_context()
    yield
    clear_llm_context()


def test_writing_stages_default_to_medium_and_reasoning_stages_run_high():
    for st in ("write_tests", "implement", "solo", "repair"):
        assert stage_effort(st) == "medium"
    for st in ("design", "plan", "review_plan", "review_code", "security_check",
               "consistency_check"):
        assert stage_effort(st, "low") == "high"
    assert stage_effort(None) is None and stage_effort("chat") is None


def test_the_project_setting_moves_only_the_writing_stages_and_junk_falls_back():
    assert stage_effort("implement", "low") == "low"
    assert stage_effort("implement", "bogus") == "medium"
    assert set(effort_map("high").values()) <= set(EFFORT_LEVELS)
    knob = ps.KNOBS["build_effort_writing"]
    assert knob["kind"] == "choice" and tuple(knob["choices"]) == EFFORT_LEVELS


@pytest.mark.parametrize("stage,writing,expected", [
    ("implement", None, "medium"), ("write_tests", "low", "low"),
    ("review_code", "low", "high"), ("design", None, "high"),
])
def test_the_cli_gets_the_stage_effort(monkeypatch, tmp_path, stage, writing, expected):
    set_llm_context(stage=stage, effort_writing=writing)
    seen = _launch(monkeypatch, tmp_path, read_only=stage.startswith("review"))
    assert _flag(seen["cmd"], "--effort") == expected


def test_outside_a_build_stage_there_is_no_flag(monkeypatch, tmp_path):
    seen = _launch(monkeypatch, tmp_path)
    assert "--effort" not in seen["cmd"]


def test_the_run_records_each_stage_effort():
    src = inspect.getsource(g._record_effort)
    assert "effort_writing" in src and "'{effort}'" in src and "code_task.effort_chosen" in src
    assert "_record_effort(engine, project_id, run_id, settings)" in inspect.getsource(
        g._run_governed_code_task)
    assert "_current_effort()" in inspect.getsource(code_agent.ClaudeCodeAgent._invoke)
