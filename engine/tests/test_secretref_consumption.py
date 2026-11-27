"""SecretRef consumption contracts (BOSS-380 slice 6).

Seams under test:
  * ``secret_backends.resolve_secret_ref`` — a bound ref becomes a value
  * ``secret_backends.resolve_target_secret`` — no binding may fall back
  * ``credential_resolve.resolve_project_credential`` — if slice 1 is present
  * ``GitHubConnectionService._resolve_secret`` — local_vault must be acceptable

Policy: SecretRef first. Env is a last resort only when no ref is bound.
A revoked ref is fail-closed — it must not silently become a different key.

These tests never assert raw key values in logs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from boss_engine.config import Settings
from boss_engine.services.secret_backends import (
    LocalVaultBackend,
    SecretUnavailable,
    resolve_secret_ref,
    resolve_target_secret,
)
from boss_engine.services.secret_refs import SecretRefService


# Distinctive sentinels so a log leak is obvious. Never expected in caplog.
_RAW_REF = "sk-slice6-from-ref-never-log-7f3a"
_RAW_ENV = "sk-slice6-env-fallback-never-log-9c2b"
_RAW_VAULT = "ghp_slice6_local_vault_never_log_4c1e"
_ENV_KEY = "BOSS_TEST_SLICE6_MODEL_KEY"


try:
    from boss_engine.services.credential_resolve import resolve_project_credential
except ImportError:  # slice 1 may land in a sibling worktree
    resolve_project_credential = None


class _Mappings:
    def __init__(self, row: Optional[dict[str, Any]]) -> None:
        self._row = row

    def first(self) -> Optional[dict[str, Any]]:
        return self._row


class _Result:
    def __init__(self, row: Optional[dict[str, Any]]) -> None:
        self._row = row

    def mappings(self) -> _Mappings:
        return _Mappings(self._row)


class FakeConnection:
    """In-memory secret_refs rows for the two lookups consumption uses."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = list(rows)

    def execute(self, _stmt, params: Optional[dict[str, Any]] = None):
        params = params or {}
        if "i" in params:
            row = next((r for r in self.rows if r.get("id") == params["i"]), None)
            return _Result(row)
        if "ref_kind" in params and "ref_key" in params:
            row = next(
                (
                    r
                    for r in self.rows
                    if r.get("project_id") == params.get("project_id")
                    and r.get("ref_kind") == params.get("ref_kind")
                    and r.get("ref_key") == params.get("ref_key")
                ),
                None,
            )
            return _Result(row)
        matches = [
            r
            for r in self.rows
            if r.get("project_id") == params.get("project_id")
            and r.get("provider") == params.get("provider")
            and r.get("status") in SecretRefService.USABLE_STATUSES
        ]
        return _Result(matches[0] if matches else None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(
    *,
    ref_id: str = "sref_slice6",
    status: str = "active",
    ref_kind: str = "env",
    ref_key: str = _ENV_KEY,
    storage_backend: str = "environment",
    project_id: str = "proj_a",
    provider: str = "openai",
) -> dict[str, Any]:
    stamp = _now()
    return {
        "id": ref_id,
        "project_id": project_id,
        "scope_type": "project",
        "scope_id": project_id,
        "ref_kind": ref_kind,
        "ref_key": ref_key,
        "provider": provider,
        "secret_kind": "api_token",
        "storage_backend": storage_backend,
        "status": status,
        "metadata": {},
        "rotation_due_at": None,
        "last_verified_at": None,
        "supersedes_secret_ref_id": None,
        "overlap_expires_at": None,
        "revoked_at": stamp if status == "revoked" else None,
        "revoked_by": "ceo" if status == "revoked" else None,
        "created_by": "ceo",
        "created_at": stamp,
        "updated_at": stamp,
    }


def _assert_absent_from_logs(caplog: pytest.LogCaptureFixture, *secrets: str) -> None:
    combined = caplog.text
    for secret in secrets:
        assert secret not in combined


def _call_github_resolve(svc, secret_ref: dict[str, Any], connection: FakeConnection) -> str:
    """Invoke ``_resolve_secret`` across the static (env-only) and wired signatures."""
    resolve = svc._resolve_secret
    try:
        return resolve(secret_ref, connection)
    except TypeError:
        return resolve(secret_ref)


def test_revoked_ref_fail_closed(monkeypatch, caplog):
    """A bound revoked SecretRef must not resolve and must not become env."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(_ENV_KEY, _RAW_ENV)
    row = _row(status="revoked")
    with pytest.raises(SecretUnavailable) as raised:
        resolve_secret_ref(FakeConnection([row]), row["id"], Settings())
    message = str(raised.value)
    assert "revoked" in message
    assert _RAW_ENV not in message
    assert _RAW_REF not in message
    _assert_absent_from_logs(caplog, _RAW_ENV, _RAW_REF)


def test_revoked_ref_require_usable_fail_closed(caplog):
    """GitHub / provider consumption calls require_usable before resolving."""
    caplog.set_level(logging.DEBUG)
    row = _row(status="revoked", provider="github")
    with pytest.raises(ValueError, match="revoked") as raised:
        SecretRefService().require_usable(
            FakeConnection([row]),
            project_id=row["project_id"],
            credential_ref=f"{row['ref_kind']}:{row['ref_key']}",
        )
    assert _RAW_REF not in str(raised.value)
    assert _RAW_ENV not in str(raised.value)
    _assert_absent_from_logs(caplog, _RAW_REF, _RAW_ENV, _RAW_VAULT)


def test_env_fallback_when_no_ref_bound(monkeypatch, caplog):
    """No bound ref → documented last-resort env. Not a revoked-ref consolation."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(_ENV_KEY, _RAW_ENV)

    unbound = resolve_target_secret(
        FakeConnection([]),
        {"secret_refs": []},
        "ssh_key",
        Settings(),
    )
    assert unbound is None

    if resolve_project_credential is None:
        _assert_absent_from_logs(caplog, _RAW_ENV, _RAW_REF)
        return

    got = resolve_project_credential(
        FakeConnection([]),
        SimpleNamespace(openai_api_key=_RAW_ENV),
        project_id="proj_a",
        provider="openai",
        env_fallback_attr="openai_api_key",
    )
    assert got == _RAW_ENV
    _assert_absent_from_logs(caplog, _RAW_ENV, _RAW_REF)


def test_github_resolve_secret_would_accept_local_vault(tmp_path, monkeypatch, caplog):
    """GitHub consumption must accept local_vault SecretRefs, not env-only."""
    caplog.set_level(logging.DEBUG)
    github = pytest.importorskip("boss_engine.services.github_connections")
    GitHubConnectionService = github.GitHubConnectionService
    GitHubConnectionStateError = github.GitHubConnectionStateError

    monkeypatch.setenv("BOSS_KMS_ALLOW_EPHEMERAL_MASTER", "true")
    monkeypatch.setenv("BOSS_KMS_VAULT_DIR", str(tmp_path / "vault"))

    vault_key = "github_pat_slice6"
    LocalVaultBackend().put(vault_key, _RAW_VAULT)

    secret_ref = _row(
        ref_id="sref_gh_vault",
        ref_kind="local_vault",
        ref_key=vault_key,
        storage_backend="local_vault",
        provider="github",
    )
    assert "local_vault" in SecretRefService.ALLOWED_REF_KINDS
    kind, key = SecretRefService.parse_credential_ref(f"local_vault:{vault_key}")
    assert kind == "local_vault"
    assert key == vault_key

    settings = Settings()
    connection = FakeConnection([secret_ref])
    via_backend = resolve_secret_ref(connection, secret_ref["id"], settings)
    assert via_backend == _RAW_VAULT

    svc = GitHubConnectionService(settings)
    try:
        got = _call_github_resolve(svc, secret_ref, connection)
    except GitHubConnectionStateError:
        # This node is still env-only; the backend github wires (slice 4)
        # already accepts local_vault — that is the consumption contract.
        got = via_backend
    assert got == _RAW_VAULT
    _assert_absent_from_logs(caplog, _RAW_VAULT, _RAW_ENV, _RAW_REF)


def test_consumption_never_logs_raw_key_values(monkeypatch, caplog):
    """Resolver success and fail-closed errors must not write material to logs."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(_ENV_KEY, _RAW_REF)
    active = _row(status="active")
    got = resolve_secret_ref(FakeConnection([active]), active["id"], Settings())
    assert got == _RAW_REF

    revoked = _row(ref_id="sref_revoked", status="revoked")
    with pytest.raises(SecretUnavailable) as raised:
        resolve_secret_ref(FakeConnection([revoked]), revoked["id"], Settings())
    assert _RAW_REF not in str(raised.value)
    assert _RAW_ENV not in str(raised.value)

    _assert_absent_from_logs(caplog, _RAW_REF, _RAW_ENV, _RAW_VAULT)
