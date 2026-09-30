"""Guest factory lanes resolve SecretRef keys when a project is bound."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.services.governed_code_task_service import _guest_engine_fields
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
        openai_api_key="sk-platform",
        openai_base_url="",
        openai_model="gpt-4o-mini",
        openrouter_api_key="sk-or",
        openrouter_base_url="https://openrouter.ai/api/v1",
        groq_api_key="gsk-groq",
        groq_base_url="https://api.groq.com/openai/v1",
        deepseek_api_key="sk-ds",
        deepseek_base_url="https://api.deepseek.com/v1",
        claude_code_oauth_token="oauth-tok",
        ollama_api_key="ollama",
        ollama_base_url="http://127.0.0.1:11434/v1",
        vllm_api_key="vllm",
        vllm_base_url="http://127.0.0.1:8000/v1",
        azure_openai_key="az-key",
        azure_openai_endpoint="https://x.openai.azure.com",
        azure_openai_deployment="gpt-4o",
        azure_openai_api_version="2024-06-01",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _ref(provider="openai", **over):
    row = {
        "id": f"sref_{provider}",
        "project_id": "proj_a",
        "provider": provider,
        "status": "active",
        "ref_kind": "env",
        "ref_key": "BOSS_TEST_OPENAI_KEY",
        "storage_backend": "environment",
    }
    row.update(over)
    return row


def test_unbound_guest_still_uses_settings():
    fields = _guest_engine_fields(_settings(), "openrouter", "some/model")
    assert fields["openai_api_key"] == "sk-or"
    assert fields["openai_api_key"] != "sk-platform"


def test_bound_openai_ref_wins_on_native_lane(monkeypatch):
    monkeypatch.setenv("BOSS_TEST_OPENAI_KEY", "sk-from-secret-ref-never-log")
    fields = _guest_engine_fields(
        _settings(),
        "native",
        None,
        connection=FakeConnection([_ref()]),
        project_id="proj_a",
    )
    assert fields["openai_api_key"] == "sk-from-secret-ref-never-log"


def test_bound_unresolvable_ref_fails_closed():
    with pytest.raises(SecretUnavailable):
        _guest_engine_fields(
            _settings(),
            "native",
            None,
            connection=FakeConnection([_ref(ref_key="BOSS_TEST_MISSING_MODEL_KEY")]),
            project_id="proj_a",
        )


def test_echo_lane_stays_credential_free():
    fields = _guest_engine_fields(
        _settings(),
        "echo",
        None,
        connection=FakeConnection([_ref()]),
        project_id="proj_a",
    )
    assert fields == {"engine": "echo"}
