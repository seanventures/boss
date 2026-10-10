"""Real PostgreSQL ledger recovery after a fresh cleanup process starts.

The explicit test URL must identify a disposable owned server. The ledger lives
in a unique schema; build databases/roles use a unique prefix and are removed.
No application or provider work executes.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import build_services as bs
from boss_engine.services import owned_process, resource_ledger


@pytest.fixture
def durable_cleanup(monkeypatch):
    url = os.environ.get("BOSS_CLEANUP_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("requires an explicitly owned cleanup PostgreSQL server")
    schema = "cleanup_" + secrets.token_hex(6)
    pre = "boss_cleanup_" + secrets.token_hex(5) + "_"
    writable = create_engine(url, connect_args={"options": f"-c search_path={schema}"})
    readonly = create_engine(url, connect_args={
        "options": f"-c search_path={schema} -c default_transaction_read_only=on",
    })
    with writable.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
        # Use the shipped table contract, including owner/chain and timestamps.
        ddl = (Path(__file__).resolve().parents[2] / "db/schema.sql").read_text()
        table = ddl.split("CREATE TABLE IF NOT EXISTS owned_resources (", 1)[1].split(";", 1)[0]
        c.execute(text("CREATE TABLE owned_resources (" + table))
    monkeypatch.setenv(bs.SERVER_URL_ENV, url.replace("postgresql+psycopg://", "postgresql://"))
    monkeypatch.setenv(bs.PREFIX_ENV, pre)
    monkeypatch.delenv(bs.PASSWORD_FILE_ENV, raising=False)
    monkeypatch.delenv(bs.RUN_HOST_ENV, raising=False)
    monkeypatch.setattr(bs, "_LEASES", {})
    monkeypatch.setattr(bs, "_PENDING_CLEANUP", {})
    monkeypatch.setattr(bs, "_SECRETS", {})
    monkeypatch.setattr(bs, "_forget_secret", lambda _: None)
    monkeypatch.setattr(owned_process, "_LEDGER", resource_ledger.DbLedger(writable))
    state = {"engine": writable, "readonly": readonly, "schema": schema, "prefix": pre}
    yield state
    from psycopg import sql

    with contextlib.closing(bs._connect(bs.server())) as c:
        for (name,) in c.execute("SELECT datname FROM pg_database WHERE starts_with(datname,%s)",
                                 (pre,)).fetchall():
            c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        for (name,) in c.execute("SELECT rolname FROM pg_roles WHERE starts_with(rolname,%s)",
                                 (pre,)).fetchall():
            c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
    readonly.dispose()
    with writable.begin() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    writable.dispose()


def exists(name):
    with contextlib.closing(bs._connect(bs.server())) as c:
        return c.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=%s), "
            "EXISTS(SELECT 1 FROM pg_roles WHERE rolname=%s)", (name, name),
        ).fetchone()


def provision(run_id="crun_cleanup_restart"):
    return bs.provision(run_id, install="inst_owned_cleanup", owner=owned_process.Owner(
        "code_run", run_id, "owned_cleanup_project",
    ))


def row(state, handle):
    with state["engine"].connect() as c:
        return c.execute(text("SELECT * FROM owned_resources WHERE handle=:h"),
                         {"h": handle}).mappings().one()


def age_rows(state):
    # Set the lifecycle state explicitly; don't depend on container/host clock
    # skew or a sleep crossing the reaper's grace boundary.
    with state["engine"].begin() as c:
        c.execute(text("UPDATE owned_resources SET created_at=now()-interval '1 hour'"))


_FRESH_REAPER = """
import json, os
from sqlalchemy import create_engine
from boss_engine.services import build_services as bs, owned_process, resource_ledger
e=create_engine(os.environ['BOSS_CLEANUP_TEST_DATABASE_URL'], connect_args={
    'options':'-c search_path='+os.environ['BOSS_CLEANUP_TEST_SCHEMA']})
owned_process.set_ledger(resource_ledger.DbLedger(e))
assert not bs._LEASES and not bs._PENDING_CLEANUP
# Durable rows are evidence for absence reconciliation, never DROP authority.
def forbidden_drop(*a,**k):
    raise AssertionError('ledger-only reconciliation attempted DROP')
bs._drop=forbidden_drop
try:
    print(json.dumps(bs.reap(set(),install='inst_owned_cleanup',min_age=0)))
finally:
    e.dispose()
"""


@pytest.mark.parametrize("via_reaper", [False, True])
def test_fresh_process_reconciles_failed_ledger_write_after_real_drop(
    durable_cleanup, monkeypatch, caplog, via_reaper,
):
    state = durable_cleanup
    lease = provision()
    age_rows(state)
    assert row(state, lease.ledger_handle)["released_at"] is None
    monkeypatch.setattr(owned_process, "_LEDGER", resource_ledger.DbLedger(state["readonly"]))
    if via_reaper:
        bs._LEASES.clear()
        report = bs.reap(set(), install="inst_owned_cleanup", min_age=0)
        assert report["pending"] == [lease.name] and report["dropped"] == []
    else:
        assert bs.teardown(lease.run_id) is False
    assert exists(lease.name) == (False, False)
    assert bs._PENDING_CLEANUP
    assert row(state, lease.ledger_handle)["released_at"] is None
    assert "ledger cleanup pending" in caplog.text or "cleanup pending" in caplog.text
    assert lease.password not in caplog.text
    env = dict(os.environ, BOSS_CLEANUP_TEST_SCHEMA=state["schema"])
    proc = subprocess.run([sys.executable, "-c", _FRESH_REAPER], env=env,
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert row(state, lease.ledger_handle)["released_at"] is not None
    assert report["reconciled"] == [lease.name]
    assert report["dropped"] == [] and report["pending"] == []
    again = subprocess.run([sys.executable, "-c", _FRESH_REAPER], env=env,
                           capture_output=True, text=True, timeout=20)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout) == {"dropped": [], "pending": []}


@pytest.mark.parametrize("guard", [
    "live_owner", "live_chain", "current_lease", "foreign_server", "foreign_prefix",
    "foreign_install", "young", "unknown_owner", "unknown_liveness", "database", "role",
])
def test_durable_row_is_held_without_sufficient_absence_and_owner_evidence(
    durable_cleanup, monkeypatch, guard,
):
    from psycopg import sql

    state = durable_cleanup
    lease = provision()
    if guard != "young":
        age_rows(state)
    with contextlib.closing(bs._connect(bs.server())) as c:
        bs._drop(c, lease.name)
        if guard == "database":
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(lease.name)))
        elif guard == "role":
            c.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(lease.name)))
    if guard != "current_lease":
        bs._LEASES.clear()
    row_id = row(state, lease.ledger_handle)["id"]
    with state["engine"].begin() as c:
        if guard == "live_chain":
            c.execute(text("UPDATE owned_resources SET owner_kind='review_seat', "
                           "owner_id='seat_ended', owner_chain=ARRAY['crun_parent_live']"))
        elif guard == "foreign_server":
            c.execute(text("UPDATE owned_resources SET handle=:h"),
                      {"h": "pg:foreign.example.test:5432/" + lease.name})
        elif guard == "foreign_prefix":
            c.execute(text("UPDATE owned_resources SET handle=replace(handle,:old,:new)"),
                      {"old": state["prefix"], "new": "foreign_cleanup_"})
        elif guard == "foreign_install":
            c.execute(text("UPDATE owned_resources SET meta=meta || "
                           "'{\"install\":\"inst_foreign\"}'::jsonb"))
        elif guard == "unknown_owner":
            c.execute(text("UPDATE owned_resources SET owner_kind='api', owner_id='api'"))
    live = {"crun_parent_live"} if guard == "live_chain" else (
        {lease.run_id} if guard == "live_owner" else ({"*"} if guard == "unknown_liveness" else set())
    )

    def forbidden_drop(*_a, **_k):
        pytest.fail("an unqualified durable row attempted DROP")

    monkeypatch.setattr(bs, "_drop", forbidden_drop)
    report = bs.reap(live, install="inst_owned_cleanup", min_age=300 if guard == "young" else 0)
    assert report["dropped"] == [] and not report.get("reconciled")
    with state["engine"].connect() as c:
        assert c.execute(text("SELECT released_at FROM owned_resources WHERE id=:id"),
                         {"id": row_id}).scalar() is None
    if guard in ("database", "role"):
        assert exists(lease.name) == (guard == "database", guard == "role")


@pytest.mark.parametrize("when", ["before_read", "after_read"])
def test_row_release_does_not_close_active_duplicate_or_concurrently_added_row(
    durable_cleanup, monkeypatch, when,
):
    state = durable_cleanup
    lease = provision()
    age_rows(state)
    with contextlib.closing(bs._connect(bs.server())) as c:
        bs._drop(c, lease.name)
    bs._LEASES.clear()
    ledger = owned_process.ledger()
    old = row(state, lease.ledger_handle)
    original = ledger.open_resources
    added = []

    def add_live_row():
        added.append(ledger.open(
            owner=owned_process.Owner("code_run", "crun_new_live"),
            resource_kind=bs.RESOURCE_KIND, handle=lease.ledger_handle, meta=old["meta"],
        ))

    def snapshot_then_add(*args):
        rows = original(*args)
        add_live_row()
        return rows

    if when == "before_read":
        add_live_row()
        age_rows(state)  # The owner's liveness, not the grace period, must protect it.
    else:
        monkeypatch.setattr(ledger, "open_resources", snapshot_then_add)
    report = bs.reap({"crun_new_live"}, install="inst_owned_cleanup", min_age=0)
    assert report.get("reconciled") == [lease.name] and len(added) == 1, report
    with state["engine"].connect() as c:
        rows = {r["id"]: r for r in c.execute(text("SELECT * FROM owned_resources")).mappings()}
    assert rows[old["id"]]["released_at"] is not None
    assert rows[added[0]]["released_at"] is None


def test_reconciliation_write_failure_is_pending_then_retryable(durable_cleanup, monkeypatch, caplog):
    state = durable_cleanup
    lease = provision()
    age_rows(state)
    with contextlib.closing(bs._connect(bs.server())) as c:
        bs._drop(c, lease.name)
    bs._LEASES.clear()
    assert not bs._PENDING_CLEANUP
    monkeypatch.setattr(owned_process, "_LEDGER", resource_ledger.DbLedger(state["readonly"]))
    report = bs.reap(set(), install="inst_owned_cleanup", min_age=0)
    assert report["pending"] == [lease.name] and not report.get("reconciled")
    assert row(state, lease.ledger_handle)["released_at"] is None
    assert "ledger cleanup pending" in caplog.text and lease.password not in caplog.text
    monkeypatch.setattr(owned_process, "_LEDGER", resource_ledger.DbLedger(state["engine"]))
    report = bs.reap(set(), install="inst_owned_cleanup", min_age=0)
    assert report["reconciled"] == [lease.name] and report["pending"] == []
    assert row(state, lease.ledger_handle)["release_reason"] == "reconciled_absent"


def test_ledger_read_and_catalog_probe_failures_remain_visible_and_open(
    durable_cleanup, monkeypatch, caplog,
):
    state = durable_cleanup
    lease = provision()
    age_rows(state)
    with contextlib.closing(bs._connect(bs.server())) as c:
        bs._drop(c, lease.name)
    bs._LEASES.clear()
    with state["engine"].begin() as c:
        c.execute(text("ALTER TABLE owned_resources RENAME TO owned_resources_unavailable"))
    report = bs.reap(set(), install="inst_owned_cleanup", min_age=0)
    assert report["ledger_cleanup_error"] == "ProgrammingError"
    assert not report.get("reconciled")
    with state["engine"].begin() as c:
        c.execute(text("ALTER TABLE owned_resources_unavailable RENAME TO owned_resources"))
    connect = bs._connect

    class ProbeFailure:
        def __init__(self, srv):
            self.conn = connect(srv)

        def execute(self, query, *args):
            if str(query).startswith("SELECT EXISTS"):
                return self.conn.execute("SELECT 1/0")  # actual PostgreSQL probe failure
            return self.conn.execute(query, *args)

        def close(self):
            self.conn.close()

    monkeypatch.setattr(bs, "_connect", ProbeFailure)
    report = bs.reap(set(), install="inst_owned_cleanup", min_age=0)
    assert report["pending"] == [lease.name] and not report.get("reconciled")
    assert row(state, lease.ledger_handle)["released_at"] is None
    assert "ledger reconciliation pending" in caplog.text
    assert "ledger cleanup pending" in caplog.text
    assert lease.password not in caplog.text


def test_strict_specific_row_release_checks_kind_handle_and_keeps_write_failure_visible(
    durable_cleanup,
):
    from sqlalchemy.exc import DBAPIError

    state = durable_cleanup
    lease = provision()
    row_id = row(state, lease.ledger_handle)["id"]
    ledger = owned_process.ledger()
    assert ledger.release_owned_row(row_id, "foreign_kind", lease.ledger_handle, "test") == 0
    assert ledger.release_owned_row(row_id, bs.RESOURCE_KIND, "foreign_handle", "test") == 0
    readonly = resource_ledger.DbLedger(state["readonly"])
    with pytest.raises(DBAPIError) as err:
        readonly.release_owned_row(row_id, bs.RESOURCE_KIND, lease.ledger_handle, "test")
    assert err.value.orig.sqlstate == "25006"
    assert row(state, lease.ledger_handle)["released_at"] is None
