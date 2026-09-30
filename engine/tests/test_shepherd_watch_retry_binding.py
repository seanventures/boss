"""Shepherd watch is a function, not a class — retry must not crash on `self`."""
from pathlib import Path


def test_watch_retry_uses_function_settings_not_self():
    src = Path("engine/boss_engine/services/shepherd_service.py").read_text(encoding="utf-8")
    assert "self.settings" not in src
    assert "self.jobs" not in src
    assert "_retry_run(engine, settings, jobs," in src
    assert "jobs=jobs" in src
