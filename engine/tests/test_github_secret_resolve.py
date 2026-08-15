"""GitHub connection SecretRef resolution is backend-first, env last-resort."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.services.github_connections import (
    GitHubConnectionService,
    GitHubConnectionStateError,
)
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
        return _Result(None)


def _svc():
    return GitHubConnectionService(SimpleNamespace())


def test_env_pointer_without_id_reads_environment(monkeypatch):
    monkeypatch.setenv("BOSS_TEST_GITHUB_TOKEN", "ghp-from-env-never-log")
    svc = _svc()
    got = svc._resolve_secret({"ref_kind": "env", "ref_key": "BOSS_TEST_GITHUB_TOKEN"})
    assert got == "ghp-from-env-never-log"


def test_bound_ref_id_uses_backend(monkeypatch):
    monkeypatch.setenv("BOSS_TEST_GITHUB_TOKEN", "ghp-from-secret-ref-never-log")
    svc = _svc()
    svc._active_connection = FakeConnection(
        [
            {
                "id": "sref_gh",
                "ref_kind": "env",
                "ref_key": "BOSS_TEST_GITHUB_TOKEN",
                "storage_backend": "environment",
                "status": "active",
            }
        ]
    )
    got = svc._resolve_secret({"id": "sref_gh", "ref_kind": "local_vault", "ref_key": "ignored"})
    assert got == "ghp-from-secret-ref-never-log"


def test_bound_unresolvable_ref_does_not_fall_back_to_env(monkeypatch):
    monkeypatch.setenv("BOSS_TEST_GITHUB_TOKEN", "ghp-from-env-never-log")
    svc = _svc()
    svc._active_connection = FakeConnection(
        [
            {
                "id": "sref_gh",
                "ref_kind": "env",
                "ref_key": "BOSS_TEST_MISSING_GITHUB_TOKEN",
                "storage_backend": "environment",
                "status": "active",
            }
        ]
    )
    with pytest.raises(SecretUnavailable):
        svc._resolve_secret(
            {
                "id": "sref_gh",
                "ref_kind": "env",
                "ref_key": "BOSS_TEST_GITHUB_TOKEN",
            }
        )


def test_non_env_without_connection_fails_closed():
    svc = _svc()
    with pytest.raises(GitHubConnectionStateError):
        svc._resolve_secret({"ref_kind": "local_vault", "ref_key": "github/token"})
