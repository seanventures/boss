"""An existing repository decides its own stack, not the words in a brief (2026-10-31).

KG P7d changes BOSS's own Python repository; its brief describes a bank's Java code. The build was
refused "needs Java, which is not turned on" three times before anything ran. The brief's words are
a request only when there is no code yet."""
from __future__ import annotations

from boss_engine.services import stacks
from boss_engine.services.governed_code_task_service import repository_stack_verdict


def _java_off(monkeypatch):
    monkeypatch.setattr(stacks, "language_ready", lambda lang: lang != "java")


def test_a_python_repository_builds_though_its_brief_mentions_java(tmp_path, monkeypatch):
    _java_off(monkeypatch)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'app'\n")
    (tmp_path / "main.py").write_text("print('hi')\n")
    assert not stacks.check_build("Fix how the lane reads the bank's Java code.").ok
    verdict = repository_stack_verdict(tmp_path)
    assert verdict is not None and verdict.ok


def test_a_java_repository_with_java_off_is_still_refused(tmp_path, monkeypatch):
    _java_off(monkeypatch)
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "src/main/java").mkdir(parents=True)
    (tmp_path / "src/main/java/App.java").write_text("class App {}")
    verdict = repository_stack_verdict(tmp_path)
    assert verdict is not None and not verdict.ok


def test_an_empty_repository_leaves_it_to_the_brief(tmp_path):
    assert repository_stack_verdict(tmp_path) is None
    assert repository_stack_verdict(None) is None
