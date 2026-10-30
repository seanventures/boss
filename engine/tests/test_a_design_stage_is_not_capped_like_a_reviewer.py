"""A read-only PLANNING stage gets the design cap; a reviewer keeps the review cap.

crun_23d665a9 (2026-10-26): the design stage on the BOSS repository timed out at 420 s. Design,
standards, spec, plan and review_plan run read-only, and every read-only invocation was capped at
`code_agent_review_timeout_seconds` -- a budget sized for a reviewer reading a diff, not a planner
reading the whole repository. Planning stages now have `code_agent_design_timeout_seconds`.
"""
from __future__ import annotations

import os
import stat
import subprocess

import pytest

from boss_engine.config import Settings
from boss_engine.services import code_agent
from boss_engine.services.code_agent import ClaudeCodeAgent
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context


@pytest.fixture
def agent(tmp_path, monkeypatch):
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setattr(code_agent, "capture_enabled", lambda: False, raising=False)
    a = ClaudeCodeAgent(Settings(BOSS_CLAUDE_CLI_PATH=str(cli), CLAUDE_CODE_OAUTH_TOKEN="",
                                 BOSS_CODE_AGENT_TIMEOUT_SECONDS=2700,
                                 BOSS_CODE_AGENT_REVIEW_TIMEOUT_SECONDS=420,
                                 BOSS_CODE_AGENT_DESIGN_TIMEOUT_SECONDS=1200))
    monkeypatch.setattr(a, "_child_env", lambda: {"PATH": os.environ.get("PATH", "")})
    seen: list[int] = []

    def fake_drain(child, *, timeout, **_kw):
        seen.append(timeout)
        child.kill()
        raise subprocess.TimeoutExpired("claude", timeout)

    monkeypatch.setattr(code_agent, "_drain", fake_drain)
    a._seen = seen
    yield a
    clear_llm_context()


def _timeout_for(agent, tmp_path, stage: str, *, read_only: bool = True) -> int:
    set_llm_context(stage=stage)
    with pytest.raises(RuntimeError, match="timed out"):
        agent._invoke("p", tmp_path, read_only=read_only)
    return agent._seen[-1]


@pytest.mark.parametrize("stage", sorted(code_agent.PLANNING_STAGES))
def test_a_planning_stage_gets_the_design_cap(agent, tmp_path, stage):
    assert _timeout_for(agent, tmp_path, stage) == 1200


@pytest.mark.parametrize("stage", ["review_code", "security_check", "consistency_check", ""])
def test_a_review_stage_keeps_the_review_cap(agent, tmp_path, stage):
    assert _timeout_for(agent, tmp_path, stage) == 420


def test_the_design_cap_never_lengthens_past_the_session_timeout():
    s = Settings(BOSS_CODE_AGENT_DESIGN_TIMEOUT_SECONDS=5000,
                 BOSS_CODE_AGENT_REVIEW_TIMEOUT_SECONDS=420)
    assert code_agent.read_only_timeout(s, 900, "design") == 900
    assert code_agent.read_only_timeout(s, 900, "review_code") == 420


def test_a_reviewer_gets_fifteen_minutes_by_default():
    """Every build is reviewed now; a reviewer reading an ~870-line change timed out at 420 s
    twice (KG P6a) and the PR said "BOSS review — NOT DONE"."""
    assert Settings().code_agent_review_timeout_seconds == 900


def test_the_design_cap_defaults_to_twenty_minutes():
    assert Settings().code_agent_design_timeout_seconds == 1200
