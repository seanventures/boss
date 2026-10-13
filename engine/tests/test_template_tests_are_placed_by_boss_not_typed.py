"""BOSS copies an applied template's test files into the app before any agent runs.

A HelpDesk AI build spent a 407-second stretch re-typing the login-oauth tests and diffing them
against the reference, because the brief asked the agent to "copy them as they are". BOSS holds
the files (templates.run_ref); it places them, and tells the agents they are there and frozen.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from boss_engine.services import governed_code_task_service as g
from boss_engine.templates import apply, run_ref


def test_the_app_folder_comes_from_the_named_app_or_the_brief():
    brief = "Build **HelpDesk AI** in the `helpdesk-ai/` folder of this repository."
    assert g._template_tests_home({}, {}, brief) == "helpdesk-ai"
    assert g._template_tests_home({"app_root": "shop/"}, {}, brief) == "shop"
    assert g._template_tests_home({}, {}, "Build a thing.") is None


def test_the_template_tests_are_copied_once_and_left_alone(tmp_path, monkeypatch):
    stack = tmp_path / "template/stacks/node"
    (stack / "tests/support").mkdir(parents=True)
    (stack / "tests/login.test.mjs").write_text("real test")
    (stack / "tests/support/fake-idp.mjs").write_text("fake idp")
    (stack / "auth/config.mjs").parent.mkdir(parents=True)
    (stack / "auth/config.mjs").write_text("not a test: adapted by the agent, never placed")
    monkeypatch.setattr(run_ref, "current", lambda: {run_ref.STACK_DIR_ENV: str(stack)})
    work = tmp_path / "work"
    work.mkdir()
    (work / "helpdesk-ai/tests").mkdir(parents=True)
    (work / "helpdesk-ai/tests/login.test.mjs").write_text("already here (a repair)")

    placed = g.place_template_tests(work, "helpdesk-ai")
    assert placed == ["helpdesk-ai/tests/support/fake-idp.mjs"]
    assert (work / "helpdesk-ai/tests/login.test.mjs").read_text() == "already here (a repair)"
    assert (work / "helpdesk-ai/tests/support/fake-idp.mjs").read_text() == "fake idp"
    assert not (work / "helpdesk-ai/auth/config.mjs").exists()


def test_nothing_is_placed_without_a_template_or_a_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(run_ref, "current", lambda: {})
    assert g.place_template_tests(tmp_path, "app") == []
    assert g.place_template_tests(tmp_path, None) == []


def test_the_agents_are_told_and_qa_still_has_to_write_its_own():
    note = g._placed_tests_note(["app/tests/login.test.mjs"])
    assert "do NOT re-create, re-type, edit, move or diff them" in note
    assert g._placed_tests_note([]) == ""
    src = inspect.getsource(g)
    assert "+ _placed_tests_note(_placed_tpl_tests)" in src            # the QA prompt
    assert "fc.path not in _placed_tpl_tests" in src                     # QA wrote its OWN tests
    assert "list(test_files) + [p for p in _placed_tpl_tests" in src     # and both are frozen


def test_the_brief_no_longer_asks_the_agent_to_copy_them():
    src = inspect.getsource(apply.build_directive)
    assert "BOSS places them there" in src and "never re-type or diff them" in src
