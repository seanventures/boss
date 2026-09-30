"""The owner account must not be the worker identity by default."""

from boss_engine.config import Settings


def test_worker_principal_defaults_to_system(monkeypatch):
    monkeypatch.delenv("BOSS_WORKER_PRINCIPAL", raising=False)
    assert Settings(_env_file=None).worker_principal == "system"
