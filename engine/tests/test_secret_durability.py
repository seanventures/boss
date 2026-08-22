"""The database secret backend must survive a redeploy.

Regression guard for a defect that made BOSS-generated host keys unusable: both `file` and
`local_vault` write under ~/.config/boss/secrets, which on a containerised install is the
container's own filesystem. Every redeploy wiped it, so `deployment_hosts.key_secret_ref`
pointed at material that no longer existed and host verification failed with
"secret 'ssh_key' is bound but unusable".
"""

import base64
import os

import pytest

from boss_engine.services.secret_backends import (
    DatabaseBackend,
    SecretUnavailable,
    backend_for,
)


class FakeSettings:
    secret_backend = "database"


@pytest.fixture
def clean_env(monkeypatch):
    for var in (
        "BOSS_KMS_MASTER_KEY",
        "BOSS_KMS_MASTER_KEY_FILE",
        "BOSS_KMS_ALLOW_EPHEMERAL_MASTER",
    ):
        monkeypatch.delenv(var, raising=False)


def _engine():
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine("sqlite://", poolclass=sqlalchemy.pool.StaticPool)
    with engine.begin() as c:
        c.execute(sqlalchemy.text(
            "CREATE TABLE secret_material ("
            "  ref_key TEXT PRIMARY KEY,"
            "  ciphertext BLOB NOT NULL,"
            "  created_at TIMESTAMP,"
            "  updated_at TIMESTAMP)"))
    return engine


def test_backend_for_resolves_database_aliases():
    for alias in ("database", "db", "postgres"):
        assert backend_for(alias, FakeSettings()).kind == "database"


def test_usable_without_a_configured_master_key(clean_env):
    """A fresh install must work before it is tuned — the previous default 500'd."""
    backend = DatabaseBackend(engine=_engine())
    backend.put("hosts/ci-01/ssh_key", "PRIVATE-KEY-BODY")
    assert backend.get("hosts/ci-01/ssh_key") == "PRIVATE-KEY-BODY"


def test_material_is_encrypted_at_rest(clean_env):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = _engine()
    DatabaseBackend(engine=engine).put("hosts/ci-01/ssh_key", "PRIVATE-KEY-BODY")
    with engine.begin() as c:
        blob = c.execute(sqlalchemy.text(
            "SELECT ciphertext FROM secret_material WHERE ref_key = 'hosts/ci-01/ssh_key'"
        )).scalar()
    assert b"PRIVATE-KEY-BODY" not in bytes(blob)


def test_secret_survives_a_process_restart(clean_env):
    """The actual bug: the key vanished when the container was replaced."""
    engine = _engine()
    DatabaseBackend(engine=engine).put("hosts/ci-01/ssh_key", "PRIVATE-KEY-BODY")
    # A new process, new backend instance — only the database persists.
    assert DatabaseBackend(engine=engine).get("hosts/ci-01/ssh_key") == "PRIVATE-KEY-BODY"


def test_health_reports_the_weaker_key_mode_honestly(clean_env):
    health = DatabaseBackend(engine=_engine()).health()
    assert health["ok"] is True
    assert health["durable"] is True
    assert health["key_mode"] == "auto_provisioned"
    assert "BOSS_KMS_MASTER_KEY" in health["note"]


def test_health_reports_configured_mode_when_a_key_is_supplied(clean_env, monkeypatch):
    monkeypatch.setenv("BOSS_KMS_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    assert DatabaseBackend(engine=_engine()).health()["key_mode"] == "configured"


def test_configuring_a_master_key_later_does_not_orphan_material(clean_env, monkeypatch):
    engine = _engine()
    DatabaseBackend(engine=engine).put("hosts/ci-01/ssh_key", "PRIVATE-KEY-BODY")

    monkeypatch.setenv("BOSS_KMS_MASTER_KEY", base64.urlsafe_b64encode(b"z" * 32).decode())
    upgraded = DatabaseBackend(engine=engine)
    assert upgraded.get("hosts/ci-01/ssh_key") == "PRIVATE-KEY-BODY"

    upgraded.put("hosts/ci-01/ssh_key", "PRIVATE-KEY-BODY")
    assert upgraded.get("hosts/ci-01/ssh_key") == "PRIVATE-KEY-BODY"


def test_master_key_is_not_exposed_or_deletable_as_a_secret(clean_env):
    backend = DatabaseBackend(engine=_engine())
    backend.put("hosts/ci-01/ssh_key", "PRIVATE-KEY-BODY")
    assert backend.list_keys() == ["hosts/ci-01/ssh_key"]
    with pytest.raises(SecretUnavailable):
        backend.delete("__boss_master_key__")


def test_missing_key_raises_the_error_the_ui_matches_on(clean_env):
    """delivery-manage.tsx sends the host wizard back to the credential stage on this text."""
    with pytest.raises(SecretUnavailable, match="does not exist"):
        DatabaseBackend(engine=_engine()).get("hosts/ci-01/ssh_key")
