"""A BUILD DATABASE ARRIVES WITH THE EXTENSIONS ITS TESTS NEED (vector, age) and a role that
can create its own roles — without ever turning a genuine provisioning failure into silence.

Mirrors the fixture style of test_builds_get_their_own_postgres.py (its own ``pg_server`` is
not imported across files, so this module defines an equivalent). Real-server cases skip when
no Postgres is reachable; the fake-admin-connection cases always run.
"""
from __future__ import annotations

import contextlib
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from boss_engine.services import build_services as bs  # noqa: E402


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
    pre = f"boss_test_bsvcx_{secrets.token_hex(3)}_"
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


def _connect_as(lease, dbname=None):
    import psycopg

    return psycopg.connect(host=lease.host, port=lease.port, user=lease.name,
                           password=lease.password, dbname=dbname or lease.name,
                           connect_timeout=5, autocommit=True)


def _available(url, names):
    import psycopg

    with contextlib.closing(psycopg.connect(url, autocommit=True)) as c:
        rows = c.execute("SELECT name FROM pg_available_extensions WHERE name = ANY(%s)",
                         (list(names),)).fetchall()
    return {r[0] for r in rows}


# ------------------------------------------------------------------------------------------ #
# P0: a provisioned run database gets the extensions this server offers
# ------------------------------------------------------------------------------------------ #

def test_p0_provisioned_database_installs_available_extensions(pg_server):
    url, _pre = pg_server
    present = _available(url, bs.EXTENSIONS)
    if present != set(bs.EXTENSIONS):
        pytest.skip(f"this test server lacks {set(bs.EXTENSIONS) - present}; "
                    "run against the build-services image to cover the full install")
    lease = bs.provision("crun_ext_full")
    assert lease.missing_extensions == ()
    assert "BOSS_BUILD_SERVICES_MISSING_EXTENSIONS" not in lease.env()
    import psycopg

    with contextlib.closing(psycopg.connect(url, dbname=lease.name, autocommit=True)) as admin:
        installed = {r[0] for r in admin.execute("SELECT extname FROM pg_extension").fetchall()}
    assert {"vector", "age"} <= installed


# ------------------------------------------------------------------------------------------ #
# P1: the run role itself can create roles and use its graph catalog
# ------------------------------------------------------------------------------------------ #

def test_p1_role_can_create_roles_and_reach_ag_catalog(pg_server):
    url, _pre = pg_server
    present = _available(url, bs.EXTENSIONS)
    if present != set(bs.EXTENSIONS):
        pytest.skip(f"this test server lacks {set(bs.EXTENSIONS) - present}")
    lease = bs.provision("crun_ext_role")
    with contextlib.closing(_connect_as(lease)) as c:
        c.execute(f'CREATE ROLE "{lease.name}_child" LOGIN')
        c.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE false")  # readable, even if empty


# ------------------------------------------------------------------------------------------ #
# P1: unavailable extensions are reported, never attempted, never an exception
# ------------------------------------------------------------------------------------------ #

class _RecordingAdmin:
    """Stands in for the admin connection on a server lacking vector/age."""

    def __init__(self, available):
        self.available = set(available)
        self.statements = []

    def execute(self, query, params=None):
        text = str(getattr(query, "as_string", lambda *_: query)(None)) if hasattr(query, "as_string") else str(query)
        self.statements.append(text)

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._rows[0] if self._rows else None

        if "pg_available_extensions" in text or (params and "pg_available_extensions" in str(params)):
            return _Result([(n,) for n in self.available])
        return _Result([])

    def close(self):
        pass


def test_p1_missing_extensions_are_reported_and_never_attempted():
    conn = _RecordingAdmin(available=set())
    missing = bs._install_extensions(conn, "some_db", "some_role")
    assert set(missing) == set(bs.EXTENSIONS)
    assert not any("CREATE EXTENSION" in s for s in conn.statements)


# ------------------------------------------------------------------------------------------ #
# P1: a role the run created is dropped with the run
# ------------------------------------------------------------------------------------------ #

def test_p1_self_created_role_is_dropped_on_teardown(pg_server):
    url, _pre = pg_server
    import psycopg

    lease = bs.provision("crun_ext_teardown")
    with contextlib.closing(_connect_as(lease)) as c:
        c.execute(f'CREATE ROLE "{lease.name}_kid" LOGIN')
    assert bs.teardown("crun_ext_teardown") is True
    with contextlib.closing(psycopg.connect(url, autocommit=True)) as admin:
        left = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                             (f"{lease.name}_kid",)).fetchone()
    assert left is None
