"""A pull request's changed-symbol links are durable (BOSSAPP-622; the class PR #12 fixed).

Breaks covered: a PR linked to the first symbol of the same name anywhere in the project instead
of the one its files define; a link lost when a re-index closes, replaces or forgets the symbol
node (it was only a kg_edges row, cascaded with the node); a rewrite of the PR record dropping it.
"""
from __future__ import annotations

import os

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge.capture import capture_lifecycle
from boss_engine.services.knowledge.code_index import index_files, symbol_key
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
A = "def reserve():\n    return 1\n"
B = "def reserve():\n    return 2\n\ndef release():\n    return reserve()\n"


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp")):
        pytest.skip("BOSS_KG_TEST_DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"disposable postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_cl', 'CL')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_cl', 'org_cl', 'CL')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('proj_cl', 'ws_cl', 'CL', 'cl')"))
            yield conn
        finally:
            transaction.rollback()


def _py(path: str, source: str) -> dict:
    return {"path": path, "language": "python", "source": source}


def _changed(connection) -> set[str]:
    node = STORE.find_by_key(connection, project_id="proj_cl", kind="pull_request", node_key="pull_request:pr-7")
    return {row["node_key"] for row in STORE.neighbors(
        connection, project_id="proj_cl", node_id=node["id"], kind="changed", direction="out")}


def _pr(connection, body: str = "Release returns what reserve held") -> dict:
    return capture_lifecycle(connection, STORE, project_id="proj_cl", record={
        "kind": "pull_request", "source_id": "pr-7", "title": "release", "body": body,
        "files": [_py("b.py", B)], "symbol_names": ["reserve", "release"],
    })


def test_a_pull_request_links_the_symbols_its_own_files_define(connection):
    index_files(connection, STORE, project_id="proj_cl", files=[_py("a.py", A)])     # a same-named symbol
    _pr(connection)
    assert _changed(connection) == {symbol_key(None, "b.py", "reserve"), symbol_key(None, "b.py", "release")}


def test_the_link_survives_a_reindex_that_closes_the_symbol_and_returns_with_it(connection):
    _pr(connection)
    index_files(connection, STORE, project_id="proj_cl", files=[_py("b.py", "def release():\n    return 0\n")])
    assert _changed(connection) == {symbol_key(None, "b.py", "release")}              # reserve went away
    index_files(connection, STORE, project_id="proj_cl", files=[_py("b.py", B)])
    assert symbol_key(None, "b.py", "reserve") in _changed(connection)                # and came back


def test_the_link_survives_a_forgotten_symbol_node_and_a_rewritten_record(connection):
    _pr(connection)
    reserve = STORE.find_by_key(connection, project_id="proj_cl", kind="symbol",
                                node_key=symbol_key(None, "b.py", "reserve"))
    STORE.delete_node(connection, project_id="proj_cl", node_id=reserve["id"], actor_kind="person")
    assert symbol_key(None, "b.py", "reserve") not in _changed(connection)
    connection.execute(text("UPDATE kg_nodes SET body = '' WHERE project_id = 'proj_cl' AND kind = 'file'"))
    index_files(connection, STORE, project_id="proj_cl",
                files=[_py("b.py", B + "\n# touched\n")])                             # re-creates the node
    _pr(connection, body="Rewritten description")                                      # a rewrite of the record
    assert _changed(connection) == {symbol_key(None, "b.py", "reserve"), symbol_key(None, "b.py", "release")}
    stored = connection.execute(text(
        "SELECT symbol_key FROM kg_change_links WHERE project_id = 'proj_cl' ORDER BY 1")).scalars().all()
    assert stored == [symbol_key(None, "b.py", "release"), symbol_key(None, "b.py", "reserve")]
