"""R5, dynamically (IDENTITY.md §15.4): every GET is read-only — the DB-diff version that
test_identity_authz_read_only.py's docstring says was missing.

A Viewer bound org-wide in the install's own org (the seeded one, so handlers have real rows to
read) calls EVERY GET operation of the real app, through the real enforcement point in enforce
mode. Path parameters are filled from real rows where the seeded database has one, else a dummy
id; project-scoped GETs name the seeded project. Every table of the run's private test schema gets a
row-level write trigger (and a TRUNCATE trigger) that notes the table, so each write is attributed
to the GET that made it; and every table is fingerprinted (row count + a hash of every row) before
and after the whole sweep, as a cross-check that nothing changed that the triggers did not see.
The triggers are dropped again at the end. Then:

  * a GET that wrote and is not declared `writes_on_get` (GETS_THAT_WRITE, the reviewed list the
    static R5 keeps) fails the test, naming the route and the tables;
  * the listed writing GETs are called too and may write (they are CSRF-guarded instead,
    test_identity_authz_writing_gets.py).

Tables excluded from the diff, each a legitimate read-side log (EXCLUDED below, with its reason).
Streaming GETs (SSE / chunked streams that never end) are skipped: they are listed in STREAMS.
"""
from __future__ import annotations

import inspect
import re
from typing import Any, Optional

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests import identity_matrix
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.test_identity_authz_read_only import GETS_THAT_WRITE
from boss_engine.identity.declarations import access_of

#: Tables a GET may legitimately write, and why. Everything else must not change.
EXCLUDED = {
    "identity_security_events": "the security audit log: authz.denied for the routes a Viewer is "
                                "refused, and authz.allowed_critical, are appended by design (§6.6)",
    "identity_authz_shadow": "the enforcement ledger's per-route decision counters (shadow_report.flush)",
    "identity_rate_limits": "request rate-limit counters",
}

STREAM_MARKERS = re.compile(r"StreamingResponse|EventSourceResponse|text/event-stream")


def _gets() -> tuple[list[tuple[str, str, Any]], list[str]]:
    ops, streams = [], []
    for route in app.router.routes:
        if not isinstance(route, APIRoute) or "GET" not in (route.methods or ()):
            continue
        try:
            source = inspect.getsource(route.endpoint)
        except (OSError, TypeError):
            source = ""
        if STREAM_MARKERS.search(source):
            streams.append(f"GET {route.path}")
            continue
        ops.append(("GET", route.path, access_of(route.endpoint)))
    return sorted(ops, key=lambda o: o[1]), sorted(streams)


GETS, STREAMS = _gets()

#: (param name) -> candidate (table, column) holding a real value in the seeded database.
REAL_VALUES = {
    "role_key": [("roles", "key"), ("roles", "role_key")],
    "workflow_key": [("delivery_workflows", "workflow_key"), ("delivery_workflows", "key")],
    "run_id": [("code_task_runs", "id"), ("workflow_runs", "id")],
    "meeting_id": [("meetings", "id")],
    "incident_id": [("incidents", "id")],
    "artifact_id": [("artifacts", "id")],
    "thread_id": [("agent_threads", "id")],
    "skill_id": [("skills", "id")],
    "pack_id": [("packs", "id")],
    "policy_id": [("policies", "id")],
    "provider": [("model_providers", "provider")],
    "process_key": [("agent_processes", "process_key")],
}


@pytest.fixture(scope="module")
def install(engine):
    """The seeded install's own org and project, with a Viewer bound org-wide."""
    with engine.connect() as db:
        org = db.execute(text("SELECT id FROM orgs ORDER BY created_at, id LIMIT 1")).scalar()
        project = db.execute(text(
            "SELECT p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id WHERE w.org_id = :o "
            "ORDER BY p.created_at, p.id LIMIT 1"), {"o": org}).scalar()
    assert org and project, "the seeded database has an org and a project"
    world = IdentityWorld(engine, org, "ws-unused", project, project)
    viewer = world.person("r5-viewer")
    world.bind(viewer, "viewer")
    return world, viewer


def _real_value(engine, name: str) -> Optional[str]:
    for table, column in REAL_VALUES.get(name, []):
        try:
            with engine.connect() as db:
                value = db.execute(text(f'SELECT "{column}" FROM "{table}" ORDER BY 1 LIMIT 1')).scalar()
        except Exception:  # noqa: BLE001 — the candidate table/column is not in this schema
            continue
        if value is not None:
            return str(value)
    return None


def _request(engine, world, viewer, method, path, access) -> dict:
    req = identity_matrix.request_for(method, path, access, world.project_a)
    url = path.replace("{path:path}", "x")
    for name in re.findall(r"{(\w+)(?::\w+)?}", path):
        value = {"project_id": world.project_a, "user_id": viewer.id, "principal_id": viewer.id,
                 "role_id": "arl_viewer", "number": "1", "org_id": world.org}.get(name) \
            or _real_value(engine, name) or "obj_1"
        url = re.sub(r"{" + name + r"(?::\w+)?}", value, url)
    req["url"] = url
    return req


def _fingerprint(engine) -> dict[str, tuple[int, str]]:
    with engine.connect() as db:
        parts = [f"SELECT '{t}' AS t, count(*) AS n, md5(coalesce(string_agg(md5(x::text), '' "
                 f"ORDER BY md5(x::text)), '')) AS h FROM \"{t}\" x" for t in _tables(db)]
        rows = db.execute(text(" UNION ALL ".join(parts))).all()
    return {r[0]: (int(r[1]), r[2]) for r in rows}


LOG = "fxd_r5_writes"


def _tables(db) -> list[str]:
    """Every table of the schema; a partitioned table once (its partitions inherit its triggers
    and are read through it)."""
    return [r[0] for r in db.execute(text(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
        "ORDER BY 1")) if r[0] not in EXCLUDED and r[0] != LOG]


@pytest.fixture(scope="module")
def write_log(engine):
    with engine.begin() as db:
        db.execute(text(f"CREATE TABLE IF NOT EXISTS {LOG} (tbl text NOT NULL, op text NOT NULL)"))
        db.execute(text(
            f"CREATE OR REPLACE FUNCTION fxd_r5_note() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"INSERT INTO {LOG} (tbl, op) VALUES (TG_TABLE_NAME, TG_OP); RETURN NULL; END $$"))
        tables = _tables(db)
        for t in tables:
            db.execute(text(f'CREATE TRIGGER fxd_r5_row AFTER INSERT OR UPDATE OR DELETE ON "{t}" '
                            f"FOR EACH ROW EXECUTE FUNCTION fxd_r5_note()"))
            db.execute(text(f'CREATE TRIGGER fxd_r5_trunc AFTER TRUNCATE ON "{t}" '
                            f"FOR EACH STATEMENT EXECUTE FUNCTION fxd_r5_note()"))
    try:
        yield tables
    finally:
        with engine.begin() as db:
            for t in tables:
                db.execute(text(f'DROP TRIGGER IF EXISTS fxd_r5_row ON "{t}"'))
                db.execute(text(f'DROP TRIGGER IF EXISTS fxd_r5_trunc ON "{t}"'))
            db.execute(text("DROP FUNCTION IF EXISTS fxd_r5_note()"))
            db.execute(text(f"DROP TABLE IF EXISTS {LOG}"))


def _drain(engine) -> list[str]:
    with engine.begin() as db:
        rows = db.execute(text(f"DELETE FROM {LOG} RETURNING tbl, op")).all()
    return sorted({f"{r[0]}:{r[1]}" for r in rows})


def _table_names(engine) -> set[str]:
    with engine.connect() as db:
        return {r[0] for r in db.execute(text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')"))}


#: Undeclared writing GETs (none since R2 #7: /v1/auth/options and /v1/auth/setup/status no
#: longer mint the claim link — main.lifespan keeps it printed; /v1/capabilities reads the recorded
#: MCP state; the tracker reads no longer seed; the secret store provisions its key on the first
#: write). A NEW one fails test_every_get_that_writes_rows_is_declared.
KNOWN_UNDECLARED_WRITERS: dict[str, str] = {}
#: GETs that CREATE TABLE (none since R2 #7: db.SERVICE_SCHEMAS applies the service-owned tables
#: at boot, and the lazy ensure runs DDL only when a table is really missing).
KNOWN_DDL_ON_GET: dict[str, str] = {}


@pytest.fixture(scope="module")
def sweep_result(engine, install, write_log):
    world, viewer = install
    # The identity background loop (shadow_report's flusher: ledger flush, legacy retirement, the
    # audit anchor) writes on its own clock; paused, so a write is attributed to the GET that made
    # it and never to whichever GET happened to be in flight.
    from boss_engine.identity import shadow_report

    flusher = shadow_report._flusher  # noqa: SLF001
    shadow_report.stop_flusher()
    if flusher is not None:
        flusher.join(timeout=30)
    client = TestClient(app, raise_server_exceptions=False)
    writers: dict[str, list[str]] = {}
    creators: dict[str, list[str]] = {}
    before, names = _fingerprint(engine), _table_names(engine)
    _drain(engine)
    with world.acting_as(world.ctx(viewer)):
        for method, path, access in GETS:
            client.request(**_request(engine, world, viewer, method, path, access))
            wrote = _drain(engine)
            if wrote:
                writers[f"{method} {path}"] = wrote
            now = _table_names(engine)
            if now - names:
                creators[f"{method} {path}"] = sorted(now - names)
            names = now
    after = _fingerprint(engine)
    created = {t for tables in creators.values() for t in tables}
    seen = {w.split(":", 1)[0] for tables in writers.values() for w in tables}
    unseen = sorted(t for t in set(before) | set(after)
                    if before.get(t) != after.get(t) and t not in seen and t not in created)
    undeclared = {label: tables for label, tables in writers.items() if label not in GETS_THAT_WRITE}
    ddl = {label: tables for label, tables in creators.items() if label not in GETS_THAT_WRITE}
    if flusher is not None:
        shadow_report.start_flusher()
    return {"writers": writers, "undeclared": undeclared, "ddl": ddl, "unseen": unseen}


def test_the_sweep_called_every_get_and_saw_every_change(sweep_result):
    assert len(GETS) > 400
    assert not sweep_result["unseen"], f"tables changed that no trigger saw: {sweep_result['unseen']}"
    # The triggers do see writes: the declared writing GETs are caught writing.
    assert set(sweep_result["writers"]) & GETS_THAT_WRITE


def test_no_new_get_writes_rows_without_being_declared(sweep_result):
    new = {k: v for k, v in sweep_result["undeclared"].items() if k not in KNOWN_UNDECLARED_WRITERS}
    assert not new, "GETs that wrote without being declared writes_on_get:\n" + "\n".join(
        f"{label}: {tables}" for label, tables in sorted(new.items()))


def test_every_get_that_writes_rows_is_declared(sweep_result):
    assert not sweep_result["undeclared"], sweep_result["undeclared"]


def test_no_new_get_creates_tables(sweep_result):
    new = {k: v for k, v in sweep_result["ddl"].items() if k not in KNOWN_DDL_ON_GET}
    assert not new, "GETs that ran CREATE TABLE:\n" + "\n".join(f"{k}: {v}" for k, v in sorted(new.items()))


def test_the_streams_skipped_are_few_and_none_is_a_declared_writer():
    """A stream cannot be fingerprinted at its end, so it is skipped: the skipped ones are listed."""
    assert len(STREAMS) < 15, STREAMS
    assert not set(STREAMS) & GETS_THAT_WRITE, STREAMS


def test_no_get_creates_tables(sweep_result):
    assert not sweep_result["ddl"], sweep_result["ddl"]
