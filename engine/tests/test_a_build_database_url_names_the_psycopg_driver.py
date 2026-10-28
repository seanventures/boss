"""A build's database URL is plain postgresql:// (build_services.py), which SQLAlchemy reads as
psycopg2, a driver BOSS does not ship. The engine suite names psycopg 3 once, in conftest, so DB
tests in a code-task build don't all error "No module named 'psycopg2'" (2026-10-28)."""
from __future__ import annotations

from .conftest import name_the_driver


def test_a_plain_postgres_url_gets_the_psycopg_driver():
    env = {"DATABASE_URL": "postgresql://u:p@build-services:5433/boss_run_ab",
           "BOSS_TEST_DATABASE_URL": "postgres://u:p@h/db"}
    name_the_driver(env)
    assert env["DATABASE_URL"] == "postgresql+psycopg://u:p@build-services:5433/boss_run_ab"
    assert env["BOSS_TEST_DATABASE_URL"] == "postgresql+psycopg://u:p@h/db"


def test_a_url_that_names_a_driver_or_is_not_postgres_is_left_alone():
    env = {"DATABASE_URL": "postgresql+psycopg://u:p@h/db", "BOSS_TEST_DATABASE_URL": "sqlite://"}
    name_the_driver(env)
    assert env == {"DATABASE_URL": "postgresql+psycopg://u:p@h/db", "BOSS_TEST_DATABASE_URL": "sqlite://"}
