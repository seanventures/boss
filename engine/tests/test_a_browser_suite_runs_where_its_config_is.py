"""A pass that asks for the browser suite runs it where its Playwright config is.

helpdesk-ai crun_13587ba1 (2026-11-06): the More tests pass asked for e2e, the gate added
`npm run test:e2e` at the root, npm said `Missing script`, and a passing pass became "checks
failing" plus an 11-minute follow-up build. Its suite lived in web/playwright.config.mjs.
"""
from __future__ import annotations

import json

from boss_engine.services import build_scope, verification_target as vt


def _app(tmp_path, scripts, config_in=None):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))
    if config_in is not None:
        where = tmp_path / config_in if config_in else tmp_path
        where.mkdir(parents=True, exist_ok=True)
        (where / "playwright.config.mjs").write_text("export default {}")
    return tmp_path


def _asked_for_e2e(monkeypatch):
    monkeypatch.setattr(build_scope, "gate_runs_e2e", lambda: True)
    monkeypatch.setattr(build_scope, "current", lambda: {"e2e": True})


def test_a_suite_in_a_subfolder_runs_there_and_never_as_a_missing_script(tmp_path, monkeypatch):
    _asked_for_e2e(monkeypatch)
    commands, finding = vt.web_gate_for(_app(tmp_path, {"test": "node --test"}, "web"))
    assert vt.E2E_STEP not in commands
    assert f"cd web && {vt.E2E_STEP_DIRECT}" in commands and finding is None


def test_a_named_script_still_runs_by_name(tmp_path, monkeypatch):
    _asked_for_e2e(monkeypatch)
    commands, _ = vt.web_gate_for(_app(tmp_path, {"test": "node --test",
                                                  "test:e2e": "playwright test"}, "web"))
    assert vt.E2E_STEP in commands


def test_no_config_means_no_browser_step(tmp_path, monkeypatch):
    _asked_for_e2e(monkeypatch)
    commands, _ = vt.web_gate_for(_app(tmp_path, {"test": "node --test"}))
    assert not any("playwright" in c or "test:e2e" in c for c in commands)
