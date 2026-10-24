"""A self-fix can test apps/api on its own disposable database, created on first use, and never on
a database that does not read as a test one (the laptop's first self-fix refused with api_suite)."""
import pytest

from boss_engine.services import self_fix_build


def test_no_url_means_no_api_suite(monkeypatch):
    monkeypatch.delenv("BOSS_SELF_FIX_TEST_DATABASE_URL", raising=False)
    assert self_fix_build._api_suite() is False


def test_a_live_looking_database_is_refused_before_connecting():
    with pytest.raises(ValueError):
        self_fix_build._ensure_test_database("postgresql+psycopg://boss:x@db:5432/boss")


def test_an_unreachable_server_disables_the_suite_quietly(monkeypatch):
    monkeypatch.setenv("BOSS_SELF_FIX_TEST_DATABASE_URL", "postgresql+psycopg://boss:x@127.0.0.1:1/boss_selffix_test")
    assert self_fix_build._api_suite() is False
