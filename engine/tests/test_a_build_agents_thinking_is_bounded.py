"""A build agent's silent thinking is bounded, so a step is never quarters of an hour of silence.

A HelpDesk AI build spent 19 of its first 52 minutes in two silent stretches (7 and 12 minutes)
in which the model thought before acting: no tool call, no output, no error. The CLI honours
MAX_THINKING_TOKENS; BOSS sets it for every build agent unless the operator chose otherwise.
"""
from __future__ import annotations

from boss_engine.services import code_agent


def test_the_default_cap_is_minutes_not_quarters_of_an_hour(monkeypatch):
    monkeypatch.delenv("BOSS_CODE_AGENT_MAX_THINKING_TOKENS", raising=False)
    assert 0 < code_agent.thinking_token_cap() <= 16000


def test_the_operator_can_raise_or_lift_it(monkeypatch):
    monkeypatch.setenv("BOSS_CODE_AGENT_MAX_THINKING_TOKENS", "32000")
    assert code_agent.thinking_token_cap() == 32000
    monkeypatch.setenv("BOSS_CODE_AGENT_MAX_THINKING_TOKENS", "0")
    assert code_agent.thinking_token_cap() == 0
    monkeypatch.setenv("BOSS_CODE_AGENT_MAX_THINKING_TOKENS", "lots")
    assert code_agent.thinking_token_cap() == code_agent.THINKING_TOKEN_CAP_DEFAULT


def test_the_install_forwards_the_setting_to_the_containers():
    from pathlib import Path
    compose = Path("scripts/install/community-compose.yml").read_text()
    assert compose.count("BOSS_CODE_AGENT_MAX_THINKING_TOKENS: ${BOSS_CODE_AGENT_MAX_THINKING_TOKENS") == 2  # api + worker


def test_the_cli_child_gets_the_cap():
    import inspect
    src = inspect.getsource(code_agent.ClaudeCodeAgent._child_env) if hasattr(code_agent, "ClaudeCodeAgent") else inspect.getsource(code_agent)
    assert 'env["MAX_THINKING_TOKENS"]' in src


def test_the_fail_closed_list_never_replaces_the_repair_list():
    """Both lists must exist side by side: one decides when a repair re-authors tests, the other
    which failing gates can never ship. A merge that reused one name replaced the first."""
    from boss_engine.services import governed_code_task_service as g
    assert {"tests_strong_enough", "tests_written_first", "existing_tests_intact"} <= set(g._TEST_GATES)
    assert set(g._FAIL_CLOSED_TEST_GATES) == {"written_tests_pass", "nothing_broke", "coder_did_not_edit_tests"}
    assert set(g._FAIL_CLOSED_TEST_GATES) <= g._NEVER_SHIP_PAST
