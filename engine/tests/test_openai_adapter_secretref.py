"""OpenAIAdapter resolves project credentials SecretRef-first."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.services.openai_adapter import OpenAIAdapter
from boss_engine.services.secret_backends import SecretUnavailable


class _Mappings:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return _Mappings(self._row)


class FakeConnection:
    def __init__(self, rows):
        self.rows = list(rows)

    def execute(self, _stmt, params=None):
        params = params or {}
        if "i" in params:
            row = next((r for r in self.rows if r.get("id") == params["i"]), None)
            return _Result(row)
        matches = [
            r
            for r in self.rows
            if r.get("project_id") == params.get("project_id")
            and r.get("provider") == params.get("provider")
            and r.get("status") in ("active", "rotating", "rotation_due")
        ]
        return _Result(matches[0] if matches else None)


def _settings(**over):
    base = dict(
        openai_api_key="sk-from-env-fallback-never-log",
        openai_base_url="",
        openai_model="gpt-4o-mini",
        azure_openai_key=None,
        azure_openai_endpoint=None,
        azure_openai_deployment=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _ref(**over):
    row = {
        "id": "sref_openai",
        "project_id": "proj_a",
        "provider": "openai",
        "status": "active",
        "ref_kind": "env",
        "ref_key": "BOSS_TEST_OPENAI_KEY",
        "storage_backend": "environment",
    }
    row.update(over)
    return row


def test_override_api_key_wins():
    adapter = OpenAIAdapter(_settings(), api_key="sk-override")
    assert adapter._resolved_api_key() == "sk-override"


def test_settings_key_used_without_project_binding():
    adapter = OpenAIAdapter(_settings())
    assert adapter._resolved_api_key() == "sk-from-env-fallback-never-log"


def test_bound_secret_ref_wins_over_settings(monkeypatch):
    monkeypatch.setenv("BOSS_TEST_OPENAI_KEY", "sk-from-secret-ref-never-log")
    adapter = OpenAIAdapter(
        _settings(),
        connection=FakeConnection([_ref()]),
        project_id="proj_a",
    )
    assert adapter._resolved_api_key() == "sk-from-secret-ref-never-log"


def test_bound_unresolvable_ref_does_not_fall_back_to_env():
    adapter = OpenAIAdapter(
        _settings(),
        connection=FakeConnection([_ref(ref_key="BOSS_TEST_MISSING_MODEL_KEY")]),
        project_id="proj_a",
    )
    with pytest.raises(SecretUnavailable):
        adapter._resolved_api_key()
