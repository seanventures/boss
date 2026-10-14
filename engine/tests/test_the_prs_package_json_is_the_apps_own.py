"""BOSS's package hardening never reaches the pull request of an app that is not a bundled front end.

Pre-mortem finding 10: REQUIRED_WEB_DEV_DEPS (React types, vite, Playwright, TypeScript, the
eslint-8 plugins) and a `preview` script were written into every web build's package.json, so
they shipped in the PR: reviewers on #224, #225 and #227 flagged them in Express apps, beside a
lockfile that no longer matched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_engine.services import build_scope
from boss_engine.services import verification_target as vt

EXPRESS = {"name": "helpdesk", "type": "module",
           "scripts": {"start": "node src/server.js", "test": "node --test"},
           "dependencies": {"express": "5.2.1"}}
REACT = {"name": "web", "scripts": {"build": "vite build", "test": "vitest run"},
         "dependencies": {"react": "^18.3.1"}, "devDependencies": {"vite": "^5.3.1"}}


def _app(tmp_path: Path, payload: dict) -> Path:
    (tmp_path / "package.json").write_text(json.dumps(payload, indent=2) + "\n")
    return tmp_path


def _prepare(app: Path, scope):
    token = build_scope.set_scope(scope)
    try:
        vt.ensure_verify_script(app)
        return vt.prepare_web_verify_env(app, scenarios=None, app_id=None)
    finally:
        build_scope._SCOPE.reset(token)


def test_an_express_build_keeps_its_own_package_json_byte_for_byte(tmp_path):
    app = _app(tmp_path, EXPRESS)
    before = (app / "package.json").read_bytes()
    prep = _prepare(app, build_scope.decide("HelpDesk", "an Express API with login"))
    assert (app / "package.json").read_bytes() == before
    assert prep["changed"] == [] and "not a bundled front end" in prep["skipped"]


@pytest.mark.parametrize("payload,brief", [
    (REACT, "add a search box"),                 # the app declares a bundler
    (EXPRESS, "a React SPA built with vite"),     # the brief asks for one
])
def test_a_bundled_front_end_is_still_hardened(tmp_path, payload, brief):
    app = _app(tmp_path, payload)
    prep = _prepare(app, build_scope.decide("Web", brief))
    assert prep["changed"], prep
    assert "preview" in json.loads((app / "package.json").read_text())["scripts"]


def test_outside_a_build_hardening_is_unchanged(tmp_path):
    app = _app(tmp_path, EXPRESS)
    assert vt.prepare_web_verify_env(app, scenarios=None, app_id=None)["changed"]
