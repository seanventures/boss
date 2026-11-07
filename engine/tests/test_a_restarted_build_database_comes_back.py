"""A RUN'S TEST DATABASE STILL OPENS WHEN ITS TESTS RUN, even after the build-services server
restarted empty (crun_de96bf12, helpdesk-ai-demo, 2026-11-06).

The server keeps its data on tmpfs and is the host's preferred OOM victim; it restarted between
the run's provisioning (18:22:45) and its suite (18:32), came back with no roles, and every
connection with the lease BOSS still handed out failed "password authentication failed for user
boss_test_run_…" (SQLSTATE 28P01) — 119 of 190 tests "failed" in setup. A restart is reproduced
here by dropping the run's role and database as the server's superuser (what an empty tmpfs
leaves). Real Postgres (the test server stands in for build-services); skipped without one.
"""
from __future__ import annotations

import contextlib
import logging
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from boss_engine.services import build_services as bs  # noqa: E402
from boss_engine.services import owned_process  # noqa: E402


def _server_url() -> str:
    explicit = os.environ.get("BOSS_TEST_BUILD_SERVICES_PG_URL", "").strip()
    if explicit:
        return explicit
    raw = (os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not raw.startswith("postgresql"):
        return ""
    parts = urlsplit(raw.replace("+psycopg2://", "://").replace("+psycopg://", "://"))
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))


@pytest.fixture()
def pg_server(monkeypatch):
    url = _server_url()
    if not url:
        pytest.skip("no Postgres server for the build-services cases")
    import psycopg

    try:
        psycopg.connect(url, connect_timeout=3).close()
    except Exception:  # noqa: BLE001
        pytest.skip("the build-services test server is not reachable")
    pre = f"boss_test_bsvc_{secrets.token_hex(3)}_"
    monkeypatch.setenv(bs.SERVER_URL_ENV, url)
    monkeypatch.setenv(bs.PREFIX_ENV, pre)
    monkeypatch.delenv(bs.RUN_HOST_ENV, raising=False)
    monkeypatch.delenv(bs.PASSWORD_FILE_ENV, raising=False)
    yield url, pre
    for run_id in [r for r, lease in list(bs._LEASES.items()) if lease.name.startswith(pre)]:
        bs.teardown(run_id)
    with contextlib.closing(psycopg.connect(url, autocommit=True)) as c:
        for (name,) in c.execute("SELECT datname FROM pg_database WHERE starts_with(datname, %s)",
                                 (pre,)).fetchall():
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        for (name,) in c.execute("SELECT rolname FROM pg_roles WHERE starts_with(rolname, %s)",
                                 (pre,)).fetchall():
            c.execute(f'DROP ROLE IF EXISTS "{name}"')


def _restart_empty(url: str, name: str) -> None:
    """What a build-services restart on tmpfs leaves: neither the database nor the role."""
    import psycopg

    with contextlib.closing(psycopg.connect(url, autocommit=True)) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'DROP ROLE IF EXISTS "{name}"')


def _login(env: dict) -> str:
    """Log in exactly as the app's suite does: with the DATABASE_URL it was handed."""
    import psycopg

    try:
        with contextlib.closing(psycopg.connect(env["DATABASE_URL"], connect_timeout=5)) as c:
            c.execute("CREATE TABLE IF NOT EXISTS t (id int)")
            c.commit()
        return "ok"
    except psycopg.Error as exc:
        # A refused login has no sqlstate on the client side at connect time: its words.
        said = str(exc)
        if "password authentication failed" in said:
            return "28P01"
        if "does not exist" in said:
            return "role or database does not exist"
        return getattr(exc, "sqlstate", None) or type(exc).__name__


def test_the_failure_reproduces_a_vanished_role_refuses_the_login_28P01(pg_server):
    url, _ = pg_server
    lease = bs.provision("crun_restart_repro")
    env = lease.env()
    assert _login(env) == "ok"
    _restart_empty(url, lease.name)
    # Exactly the build's error under build-services' pg_hba (scram-sha-256): Postgres says
    # "password authentication failed" for a role that is not there (the password was right).
    # A server that trusts local logins names the missing role instead; either way the lease
    # BOSS kept handing out no longer opens anything.
    assert _login(env) in ("28P01", "role or database does not exist")


def test_the_runs_next_process_gets_a_database_that_opens_again(pg_server, caplog):
    url, _ = pg_server
    caplog.set_level(logging.INFO, logger="boss.build_services")
    lease = bs.provision("crun_restart_heal")
    _restart_empty(url, lease.name)
    bs._ENSURED.pop(lease.name, None)            # the throttle's window has passed
    owner = owned_process.Owner("code_run", "crun_restart_heal")
    env = bs.env_for_owner(owner)
    # The same credentials (the run's processes already hold them), and they open again.
    assert env["DATABASE_URL"] == lease.env()["DATABASE_URL"]
    assert _login(env) == "ok"
    assert lease.restored == 1
    assert "had vanished" in caplog.text and lease.password not in caplog.text
    # Still the run's own, isolated database: BOSS's comment, CONNECT not public.
    import psycopg

    with contextlib.closing(psycopg.connect(url, autocommit=True)) as c:
        comment = c.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                            "WHERE datname = %s", (lease.name,)).fetchone()[0]
        public = c.execute("SELECT has_database_privilege('public', %s, 'CONNECT')",
                           (lease.name,)).fetchone()[0]
    assert '"boss": "build-services"' in comment and bs.run_tag("crun_restart_heal") in comment
    assert public is False


def test_a_healthy_lease_is_checked_cheaply_and_an_ended_one_never_comes_back(pg_server):
    url, _ = pg_server
    lease = bs.provision("crun_restart_cheap")
    # Just made: the first spawn does not even ask the server.
    assert bs.ensure(lease) == "skipped"
    assert bs.ensure(lease, force=True) == "ok"
    assert bs.ensure(lease) == "skipped", "at most one check per ENSURE_EVERY seconds"
    assert bs.teardown("crun_restart_cheap") is True
    assert bs.ensure(lease, force=True) == "skipped"
    import psycopg

    with contextlib.closing(psycopg.connect(url, autocommit=True)) as c:
        gone = c.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s",
                         (lease.name,)).fetchone()[0]
    assert gone == 0, "a torn-down run's database is never re-created"


def test_an_unreachable_server_never_blocks_the_spawn(monkeypatch):
    lease = bs.PostgresLease(run_id="crun_restart_down", name="boss_test_run_00", host="h",
                             port=5433, password="x" * 48)
    monkeypatch.setitem(bs._LEASES, "crun_restart_down", lease)
    monkeypatch.setenv(bs.SERVER_URL_ENV, "postgresql://postgres:pw@127.0.0.1:1/postgres")
    monkeypatch.setattr(bs, "_ENSURED", {})
    env = bs.env_for_owner(owned_process.Owner("code_run", "crun_restart_down"))
    assert env["PGUSER"] == "boss_test_run_00"
    assert bs.ensure(lease, force=True) == "unreachable"
