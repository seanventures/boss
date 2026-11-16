"""BOSSAPP-1033: one install has one secret store.

`pick_default_backend` asked each process which store looked healthy. A
container with the installer's database choice and a host bench that could see
Infisical each picked their own, and a rotation healed only the one that
process could see. The install's recorded choice is authoritative. A host
bench that would otherwise point somewhere else is refused. Fake stores only —
nothing here reads a real secret.
"""
from __future__ import annotations

import pytest

from boss_engine.config import Settings
from boss_engine.services import secret_backends as sb


class _Healthy:
    def health(self):
        return {"ok": True, "durable": True}


def _everything_answers(monkeypatch):
    monkeypatch.setattr(sb, "backend_for", lambda kind, settings: _Healthy())


def test_the_install_record_wins_over_a_host_that_can_see_infisical(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "BOSS_COMMUNITY_SECRET_STORE=database\n"
        "BOSS_INFISICAL_TOKEN=not-a-real-token\n"
    )
    monkeypatch.setenv("BOSS_INSTALL_DIR", str(tmp_path))
    monkeypatch.delenv("BOSS_COMMUNITY_SECRET_STORE", raising=False)
    monkeypatch.setenv("BOSS_BENCH_DATABASE_URL", "postgresql://example/boss_test_store")
    _everything_answers(monkeypatch)
    assert sb.pick_default_backend(Settings())[0] == "database"
    assert sb.durable_secret_backend(Settings()) == "database"
    assert sb.default_secret_backend(Settings()) == "database"


def test_a_host_bench_refuses_to_guess_a_store_the_install_did_not_choose(monkeypatch):
    monkeypatch.delenv("BOSS_COMMUNITY_SECRET_STORE", raising=False)
    monkeypatch.delenv("BOSS_INSTALL_DIR", raising=False)
    monkeypatch.setenv("BOSS_HOST_BENCH", "1")
    _everything_answers(monkeypatch)
    with pytest.raises(sb.SecretUnavailable, match="host bench must not"):
        sb.pick_default_backend(Settings())


def test_an_explicit_process_choice_still_beats_the_install_file(monkeypatch, tmp_path):
    """The environment the process was started with is the operator's answer."""
    (tmp_path / ".env").write_text("BOSS_COMMUNITY_SECRET_STORE=database\n")
    monkeypatch.setenv("BOSS_INSTALL_DIR", str(tmp_path))
    monkeypatch.setenv("BOSS_COMMUNITY_SECRET_STORE", "infisical")
    _everything_answers(monkeypatch)
    assert sb.pick_default_backend(Settings())[0] == "infisical"


def test_no_host_bench_flag_keeps_the_preference_walk(monkeypatch):
    monkeypatch.delenv("BOSS_COMMUNITY_SECRET_STORE", raising=False)
    monkeypatch.delenv("BOSS_INSTALL_DIR", raising=False)
    monkeypatch.delenv("BOSS_HOST_BENCH", raising=False)
    monkeypatch.delenv("BOSS_BENCH_DATABASE_URL", raising=False)
    _everything_answers(monkeypatch)
    assert sb.pick_default_backend(Settings())[0] == "infisical"
