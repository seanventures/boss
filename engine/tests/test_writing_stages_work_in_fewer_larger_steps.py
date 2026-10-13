"""A writing stage works in fewer, larger steps.

A HelpDesk build's implement stage took 1,337 turns: 227 Reads, 268 Bash runs (build, test,
lint and typecheck after each small change), 48 Edits, and about 89% of its output tokens were
reasoning and narration. Every writing stage now gets the same short working rules from the one
place every CLI call goes through; a reviewer does not (it reads, it does not write). No
writing prompt asks for a test run after every change.
"""
from __future__ import annotations

import inspect

from boss_engine.services import code_agent
from boss_engine.services import governed_code_task_service as g
from tests.test_a_build_fans_out_inside_its_sandbox import _launch

RULE = code_agent._WRITING_RULE


def test_the_rule_says_each_thing_once_and_briefly():
    for words in ("No narration", "in ONE turn", "WHOLE with one Write",
                  "Do not re-read a file you just wrote", "when a chunk of work is finished"):
        assert words in RULE
    assert len(RULE) < 900


def test_a_writing_stage_gets_the_rule_and_a_reviewer_does_not(monkeypatch, tmp_path):
    assert RULE in _launch(monkeypatch, tmp_path)["cmd"][-1]
    assert RULE not in _launch(monkeypatch, tmp_path, read_only=True)["cmd"][-1]


def test_no_writing_prompt_asks_for_a_run_after_every_change():
    for fn in (g._impl_prompt, g._solo_prompt):
        src = inspect.getsource(fn)
        assert "iterate until" in src and "when a chunk of work is finished" in src
        assert "from the app directory and iterate" not in src
