"""Project+provider credential resolution: SecretRef first, env last resort.

These tests pin the lookup order later slices (openai, factory, github, plane)
must share. A bound usable SecretRef always wins. Process settings are a
documented last resort only when no usable ref exists for that project+provider.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from boss_engine.services.credential_resolve import resolve_project_credential
from boss_engine.services.secret_backends import SecretUnavailable


USABLE = ("active", "rotating", "rotation_due")
UNUSABLE = ("disabled", "revoked")
SECRET_FROM_REF = "sk-from-secret-ref-never-log"
SECRET_FROM_ENV = "sk-from-env-fallback-never-log"


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
    """In-memory secret_refs table. Understands the two lookups this helper uses."""

    def __init__(self, rows: list[dict]):
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
            and r.get("status") in USABLE
        ]
        return _Result(matches[0] if matches else None)


def _env_ref(*, status: str = "active", project_id: str = "proj_a", provider: str = "openai",
             ref_id: str = "sref_openai", env_key: str = "BOSS_TEST_OPENAI_KEY") -> dict:
    return {
        "id": ref_id,
        "project_id": project_id,
        "provider": provider,
        "status": status,
        "ref_kind": "env",
        "ref_key": env_key,
        "storage_backend": "environment",
    }


def _settings(**attrs):
    return SimpleNamespace(**attrs)


def test_usable_secret_ref_wins_over_env_fallback(monkeypatch):
    monkeypatch.setenv("BOSS_TEST_OPENAI_KEY", SECRET_FROM_REF)
    got = resolve_project_credential(
        FakeConnection([_env_ref()]),
        _settings(openai_api_key=SECRET_FROM_ENV),
        project_id="proj_a",
        provider="openai",
        env_fallback_attr="openai_api_key",
    )
    assert got == SECRET_FROM_REF


@pytest.mark.parametrize("status", USABLE)
def test_active_rotating_and_rotation_due_refs_are_usable(monkeypatch, status):
    monkeypatch.setenv("BOSS_TEST_OPENAI_KEY", SECRET_FROM_REF)
    got = resolve_project_credential(
        FakeConnection([_env_ref(status=status)]),
        _settings(openai_api_key=SECRET_FROM_ENV),
        project_id="proj_a",
        provider="openai",
        env_fallback_attr="openai_api_key",
    )
    assert got == SECRET_FROM_REF


@pytest.mark.parametrize("status", UNUSABLE)
def test_unusable_ref_falls_back_to_env(status):
    got = resolve_project_credential(
        FakeConnection([_env_ref(status=status)]),
        _settings(openai_api_key=SECRET_FROM_ENV),
        project_id="proj_a",
        provider="openai",
        env_fallback_attr="openai_api_key",
    )
    assert got == SECRET_FROM_ENV


def test_env_fallback_used_when_no_ref_bound():
    got = resolve_project_credential(
        FakeConnection([]),
        _settings(openai_api_key=SECRET_FROM_ENV),
        project_id="proj_a",
        provider="openai",
        env_fallback_attr="openai_api_key",
    )
    assert got == SECRET_FROM_ENV


def test_returns_none_when_no_ref_and_no_env():
    got = resolve_project_credential(
        FakeConnection([]),
        _settings(),
        project_id="proj_a",
        provider="openai",
        env_fallback_attr="openai_api_key",
    )
    assert got is None


def test_blank_env_fallback_is_treated_as_unset():
    got = resolve_project_credential(
        FakeConnection([]),
        _settings(openai_api_key="   "),
        project_id="proj_a",
        provider="openai",
        env_fallback_attr="openai_api_key",
    )
    assert got is None


def test_other_project_ref_is_ignored():
    got = resolve_project_credential(
        FakeConnection([_env_ref(project_id="proj_other")]),
        _settings(openai_api_key=SECRET_FROM_ENV),
        project_id="proj_a",
        provider="openai",
        env_fallback_attr="openai_api_key",
    )
    assert got == SECRET_FROM_ENV


def test_other_provider_ref_is_ignored():
    got = resolve_project_credential(
        FakeConnection([_env_ref(provider="anthropic")]),
        _settings(openai_api_key=SECRET_FROM_ENV),
        project_id="proj_a",
        provider="openai",
        env_fallback_attr="openai_api_key",
    )
    assert got == SECRET_FROM_ENV


def test_bound_unresolvable_ref_does_not_fall_back_to_env():
    """Fail-closed: a usable ref that cannot resolve must not become a different key."""
    with pytest.raises(SecretUnavailable):
        resolve_project_credential(
            FakeConnection([_env_ref(env_key="BOSS_TEST_MISSING_MODEL_KEY")]),
            _settings(openai_api_key=SECRET_FROM_ENV),
            project_id="proj_a",
            provider="openai",
            env_fallback_attr="openai_api_key",
        )


def test_resolved_value_is_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("BOSS_TEST_OPENAI_KEY", SECRET_FROM_REF)
    caplog.set_level(logging.DEBUG)
    got = resolve_project_credential(
        FakeConnection([_env_ref()]),
        _settings(openai_api_key=SECRET_FROM_ENV),
        project_id="proj_a",
        provider="openai",
        env_fallback_attr="openai_api_key",
    )
    assert got == SECRET_FROM_REF
    combined = caplog.text
    assert SECRET_FROM_REF not in combined
    assert SECRET_FROM_ENV not in combined
