"""Every stage of a build shares one cached prompt prefix.

The prompt cache is keyed on the request prefix (tools, system prompt, messages). The CLI's
default system prompt carries the cwd, env and git status, which change between stages, and the
task context was in the user message after it, so each of a HelpDesk build's 14 sessions wrote
its own cache from scratch (2.08M cache-write tokens, no reuse across sessions). Now:
- the dynamic sections move out of the system prompt (--exclude-dynamic-system-prompt-sections);
- the pipeline's byte-identical TASK CONTEXT block is appended to the system prompt;
- a resumed session sends the same block it started with;
- the block no longer differs by stage (the reviewers passed service=False, QA True).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.config import get_settings
from boss_engine.services import code_agent
from boss_engine.services import governed_code_task_service as g
from boss_engine.services.code_agent import (SHARED_CONTEXT_END, ClaudeCodeAgent,
                                             split_shared_context)
from tests.test_a_build_fans_out_inside_its_sandbox import _Spawned, _flag

CRITERIA = ["Customers file tickets", "Agents reply"]


def _launch(monkeypatch, tmp_path: Path, prompt: str, **kwargs) -> list[str]:
    seen: dict = {}

    def fake_popen(cmd, **kw):
        seen["cmd"] = list(cmd)
        raise _Spawned()

    monkeypatch.setattr(code_agent.owned_process, "popen", fake_popen)
    monkeypatch.setenv("BOSS_CAPTURE_AGENT_TURNS", "0")
    # No Anthropic credential: the one resolution every `claude` child uses (claude_credential).
    from boss_engine.services import claude_credential
    monkeypatch.setattr(claude_credential, "resolve_claude_credential",
                        lambda settings, **_k: claude_credential.ClaudeCredential("none"))
    with pytest.raises(_Spawned):
        ClaudeCodeAgent(get_settings())._invoke(prompt, tmp_path, **kwargs)
    return seen["cmd"]


def test_the_block_is_split_off_only_when_the_prompt_starts_with_it():
    block = g._ctx_block("HelpDesk", "Build it.", CRITERIA)
    shared, own = split_shared_context(block + "You are a QA engineer.")
    assert shared == block and shared.endswith(SHARED_CONTEXT_END) and own == "You are a QA engineer."
    assert split_shared_context("Hello " + block) == ("", "Hello " + block)


def test_the_cli_gets_the_block_as_system_prompt_and_a_static_default(monkeypatch, tmp_path):
    block = g._ctx_block("HelpDesk", "Build it.", CRITERIA)
    cmd = _launch(monkeypatch, tmp_path, block + "Write the tests.")
    assert "--exclude-dynamic-system-prompt-sections" in cmd
    assert _flag(cmd, "--append-system-prompt") == block
    assert cmd[-1].startswith("Write the tests.") and "TASK CONTEXT" not in cmd[-1]


def test_a_prompt_without_the_block_is_sent_as_before(monkeypatch, tmp_path):
    cmd = _launch(monkeypatch, tmp_path, "Summarise this.")
    assert "--append-system-prompt" not in cmd and cmd[-1].startswith("Summarise this.")


def test_a_resumed_session_sends_the_block_it_started_with(monkeypatch, tmp_path):
    block = g._ctx_block("HelpDesk", "Build it.", CRITERIA)
    code_agent._remember_shared("sess-1", block)
    cmd = _launch(monkeypatch, tmp_path, "Fix the failing test.", resume_session_id="sess-1")
    assert _flag(cmd, "--append-system-prompt") == block
    assert cmd[-1].startswith("Fix the failing test.")


def test_every_stage_of_one_task_renders_the_same_block():
    title, instr = "Shared prefix check", "Customers file tickets; agents answer them."
    qa = g._qa_prompt(title, instr, CRITERIA, "npm test", web=True, app_root="helpdesk-lite")
    impl = g._impl_prompt(title, instr, ["t.test.js"], "npm test", criteria=CRITERIA,
                          app_root="helpdesk-lite")
    review = g._review_prompt(title, CRITERIA, instructions=instr)
    blocks = {split_shared_context(p)[0] for p in (qa, impl, review)}
    assert len(blocks) == 1 and next(iter(blocks))
