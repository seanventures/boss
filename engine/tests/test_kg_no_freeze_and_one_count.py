"""The knowledge graph never freezes the console, and every surface counts rules one way.

Tester findings, 2026-11-01 (NorthPeak Lending):
1. ~14 min after boot an API connection sat "idle in transaction" holding ACCESS EXCLUSIVE on
   kg_nodes / kg_edges / kg_usage: the store's lazy ensure_schema ran ALTER TABLE … ADD COLUMN
   IF NOT EXISTS (which locks even when the column exists) on the request path, once per store
   instance — and stores are built per request and per tick.
2. The embedding sync copied EVERY kg_embeddings row into kg_vec_<dim> once per embedding, in
   one transaction held open across the embedder's network call: searches queued ~60 s.
3. "0 of 96 DQ rules in the graph", "96 waiting", "Rules 50 proposed" and "146" about the same
   rules: four surfaces, four definitions.
4. Domain / Context bubbles led with whichever kind had most rows, not the DQ story.
Also: the API booted before the database and skipped every boot step without retrying.
"""
from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine import db as boss_db
from boss_engine.services.knowledge import embeddings, store as store_mod

ENGINE_ROOT = Path(__file__).resolve().parents[1] / "boss_engine"


# ── 1. a request never runs DDL ─────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, scalar=None, rows=()):
        self._scalar, self._rows = scalar, list(rows)

    def scalar(self):
        return self._scalar

    def all(self):
        return self._rows


class _CatalogConnection:
    """Answers catalog reads as a fully migrated database would; records every statement."""

    def __init__(self, schema_sql: str, missing_column: tuple[str, str] | None = None):
        self.statements: list[str] = []
        self.columns = [(t.lower(), c.lower()) for t, c in boss_db._DECL_COLUMN.findall(schema_sql)
                        if (t.lower(), c.lower()) != missing_column]

    def execute(self, clause, params=None):
        sql = str(clause)
        self.statements.append(sql)
        if "information_schema.columns" in sql:
            return _Result(rows=self.columns)
        return _Result(scalar=True)


def _ddl(statements: list[str]) -> list[str]:
    return [s for s in statements if re.match(r"\s*(ALTER|CREATE|DROP|SET LOCAL lock_timeout)", s, re.I)]


@pytest.mark.parametrize("module, attr", [
    ("boss_engine.services.knowledge.store", "_SCHEMA_SQL"),
    ("boss_engine.services.knowledge_graph", "_SCHEMA_EXTRAS_SQL"),
    ("boss_engine.services.knowledge.document_upload", "_SCHEMA_SQL"),
])
def test_a_migrated_database_gets_no_ddl_from_a_request(module, attr):
    import importlib

    schema_sql = getattr(importlib.import_module(module), attr)
    boss_db.reset_declared_schemas()
    connection = _CatalogConnection(schema_sql)
    assert boss_db.ensure_declared_schema(connection, schema_sql, key=module) is False
    assert _ddl(connection.statements) == [], "a request ran DDL on a migrated database"
    # And after the first look, nothing at all: not even the catalog read.
    connection.statements.clear()
    boss_db.ensure_declared_schema(connection, schema_sql, key=module)
    assert connection.statements == []
    # Boot is where it is applied.
    assert f"{module}:{attr}" in boss_db.SERVICE_SCHEMAS


def test_a_missing_column_is_added_under_a_lock_timeout():
    boss_db.reset_declared_schemas()
    connection = _CatalogConnection(store_mod._SCHEMA_SQL, missing_column=("kg_nodes", "repo"))
    assert boss_db.ensure_declared_schema(connection, store_mod._SCHEMA_SQL, key="t") is True
    ddl = _ddl(connection.statements)
    assert ddl[0].startswith("SET LOCAL lock_timeout"), "fallback DDL must never wait unbounded"
    boss_db.reset_declared_schemas()


def test_the_store_and_the_kg_service_never_run_their_ddl_directly():
    """The class guard: no knowledge module loops over its own DDL on a request path."""
    offenders = []
    for path in [*sorted((ENGINE_ROOT / "services" / "knowledge").glob("*.py")),
                 ENGINE_ROOT / "services" / "knowledge_graph.py"]:
        source = path.read_text(encoding="utf-8")
        for name in ("_SCHEMA_SQL", "_SCHEMA_EXTRAS_SQL"):
            if re.search(rf"\b{name}\.split\(", source):
                offenders.append(f"{path.name}: {name}.split(...)")
    assert offenders == [], f"run DDL through db.ensure_declared_schema, not inline: {offenders}"
    ensure = inspect.getsource(store_mod.PostgresKnowledgeStore.ensure_schema)
    assert "ensure_declared_schema" in ensure


class _Cursor:
    def __init__(self, exists: bool):
        self.exists = exists

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return (self.exists,)


def test_boot_skips_ddl_that_is_already_in_place_without_a_lock():
    columns = {("kg_nodes", "repo")}
    assert boss_db.ddl_already_applied(_Cursor(True), "ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS repo TEXT",
                                       columns)
    assert not boss_db.ddl_already_applied(_Cursor(True), "ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS path TEXT",
                                           columns)
    assert boss_db.ddl_already_applied(_Cursor(True), "CREATE INDEX IF NOT EXISTS idx_x ON kg_nodes(id)", columns)
    assert not boss_db.ddl_already_applied(_Cursor(False), "CREATE INDEX IF NOT EXISTS idx_x ON kg_nodes(id)",
                                           columns)
    assert not boss_db.ddl_already_applied(_Cursor(True), "CREATE TABLE IF NOT EXISTS t (id int)", columns)
    source = inspect.getsource(boss_db.ensure_schema)
    assert "SET lock_timeout" in source and "ddl_already_applied" in source


def test_boot_waits_for_a_database_that_is_starting_up(monkeypatch):
    answers = iter([False, False, True])
    monkeypatch.setattr(boss_db, "database_is_reachable", lambda _e: next(answers))
    slept: list[float] = []
    assert boss_db.wait_for_database(object(), timeout_s=60, interval_s=1, sleep=slept.append) is True
    assert slept == [1, 1]
    monkeypatch.setattr(boss_db, "database_is_reachable", lambda _e: False)
    assert boss_db.wait_for_database(object(), timeout_s=0, interval_s=1, sleep=slept.append) is False
    main = (ENGINE_ROOT.parents[1] / "apps" / "api" / "app" / "main.py").read_text(encoding="utf-8")
    lifespan = main[main.index("async def lifespan"):]
    assert lifespan.index("wait_for_database") < lifespan.index("ensure_schema(engine, settings)")


# ── 2. the embedding sync never holds search up ─────────────────────────────────────────────

class _Engine:
    def __init__(self):
        self.open = 0

    def begin(self):
        engine = self

        class _Tx:
            def __enter__(self):
                engine.open += 1
                return object()

            def __exit__(self, *exc):
                engine.open -= 1
                return False

        return _Tx()


def test_the_model_embeds_on_its_own_live_connection_and_no_graph_write_shares_it(monkeypatch):
    """The governed embedder reserves and settles budget on the connection it was built with.
    Called after that transaction closed, every call failed silently and left its reservation to
    expire into a "Reconcile expired budget reservation" card (2026-11-02). The model call gets
    its own short transaction, open while it embeds, holding no graph write."""
    from boss_engine.services import projects_archive
    from boss_engine.services.knowledge import embedding_slot

    engine = _Engine()
    rows = [{"id": f"n{i}", "name": "x", "body": "y", "content_hash": "h"} for i in range(40)]
    batches: list[int] = []

    class _Embedder:
        model_id = "m"

        def __init__(self, depth):
            self.depth = depth

        def embed(self, texts):
            assert engine.open == self.depth == 1, "the embedder's own transaction must be open while it embeds"
            assert not batches, "a graph write ran before or alongside the model call"
            return [[0.1, 0.2]] * len(texts)

    monkeypatch.setattr(projects_archive, "live_ids", lambda _c, ids: ids)
    monkeypatch.setattr(embedding_slot, "active_embedder", lambda *_a: (_Embedder(engine.open), ""))
    monkeypatch.setattr(embeddings, "drop_other_spaces", lambda *_a, **_k: 0)
    monkeypatch.setattr(embeddings, "pending_rows", lambda *_a, **_k: rows)

    def _write(_c, _s, *, rows, vectors, **_k):
        assert engine.open == 1
        batches.append(len(rows))
        return len(rows)

    monkeypatch.setattr(embeddings, "write_vectors", _write)
    out = embeddings.embed_due(engine, project_id="p", limit=64)
    assert out["embedded"] == 40 and out["failed"] == 0
    assert batches and max(batches) <= embeddings.WRITE_BATCH, "vectors are written in short transactions"


def test_an_embedding_write_copies_its_own_row_not_the_table():
    source = inspect.getsource(store_mod.PostgresKnowledgeStore.upsert_embedding)
    assert "sync_vector_index" not in source
    copy = inspect.getsource(store_mod.PostgresKnowledgeStore._copy_embedding_to_index)
    assert "WHERE e.id = :id" in copy


# ── 3 + 4. one rule count; the DQ story leads (database) ────────────────────────────────────

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
    boss_db.ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


def _node(connection, nid, kind, key, status, tier, attrs_extra=""):
    connection.execute(text(
        "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, attrs, trust_tier) VALUES "
        f"(:id, 'proj_rc', :k, :key, :key, CAST(:a AS JSONB), :t)"),
        {"id": nid, "k": kind, "key": key, "t": tier,
         "a": '{"status": "%s"%s}' % (status, attrs_extra)})


def _project(connection):
    connection.execute(text("INSERT INTO orgs (id, name) VALUES ('org_rc', 'RC')"))
    connection.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_rc', 'org_rc', 'RC')"))
    connection.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                            "VALUES ('proj_rc', 'ws_rc', 'RC', 'rc')"))


def test_every_surface_counts_rules_the_same_way(connection):
    from boss_engine.services.knowledge import dq_platform, project_setup, rules, view

    _project(connection)
    _node(connection, "kgn_rc1", "rule", "rule:a", "proposed", "inferred")
    _node(connection, "kgn_rc2", "rule", "rule:b", "approved", "approved")
    _node(connection, "kgn_rc3", "data_quality_rule", "NPB-DQ-LN-001", "approved", "approved",
          ', "dq": {"rule_id": "NPB-DQ-LN-001"}')
    _node(connection, "kgn_rc4", "data_quality_rule", "NPB-DQ-LN-002", "proposed", "inferred",
          ', "dq": {"rule_id": "NPB-DQ-LN-002"}')

    one = rules.rule_counts(connection, "proj_rc")
    assert (one["approved"], one["proposed"]) == (2, 2)
    assert rules.list_rules(connection, project_id="proj_rc")["counts"] == one
    setup = project_setup._rules(connection, "proj_rc")
    assert all(setup[k] == one[k] for k in setup)
    bubble = next(b for b in view.overview(connection, "proj_rc", layers={"rules"})["layers"]
                  if b["id"] == "layer:rules")
    assert all(bubble["counts"][k] == one[k] for k in one)
    assert bubble["sub"] == rules.rule_count_words(one) == "2 approved · 2 waiting for review"
    # "In the graph" is approved DQ rules, found however the rulebook keyed them.
    assert dq_platform.rules_in_graph(connection, "proj_rc", ["NPB-DQ-LN-001", "NPB-DQ-LN-002"]) == 1
    assert dq_platform.resolve_rule(connection, "proj_rc", "NPB-DQ-LN-002")["id"] == "kgn_rc4"


def test_a_dq_project_sees_its_rules_and_incidents_first(connection, monkeypatch):
    from boss_engine.services.knowledge import ontology_packs, view

    _project(connection)
    for i in range(5):
        _node(connection, f"kgn_th{i}", "dq_threshold", f"th{i}", "approved", "observed")
    _node(connection, "kgn_dr", "data_quality_rule", "NPB-DQ-LN-009", "approved", "approved")
    _node(connection, "kgn_in", "incident", "dq:incident:INC-1", "open", "observed")
    _node(connection, "kgn_ex", "exception_group", "eg1", "open", "observed")
    pack = ontology_packs.load_pack("banking-dq")
    monkeypatch.setattr(ontology_packs, "get_project_pack", lambda *_a, **_k: pack)
    layers = {b["id"]: b for b in view.overview(connection, "proj_rc", layers={"domain", "context"})["layers"]}
    assert layers["layer:domain"]["sub"].startswith("1 data quality rule")
    assert layers["layer:context"]["sub"].startswith("1 incident")
