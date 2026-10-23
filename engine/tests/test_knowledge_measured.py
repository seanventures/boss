"""Measured knowledge paths against a disposable Postgres.

Alpine has neither pgvector nor AGE. The opt-in image has both. Each test
asserts the branch that this database can actually run.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services.knowledge.age import (
    age_neighbors,
    extension_version,
    project_graph,
)
from boss_engine.services.knowledge.archivist import distil
from boss_engine.services.knowledge.bench import measure_retrieval
from boss_engine.services.knowledge.capture import capture_lifecycle
from boss_engine.services.knowledge.patterns import LineExtractor, conformance_corpus, score_extractor
from boss_engine.services.knowledge.scrub import contains_secret_marker
from boss_engine.services.knowledge.service import KnowledgeService
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()
SERVICE = KnowledgeService(STORE)
SECRETS = (
    "AKIAIOSFODNN7EXAMPLE",
    "sk-abcdefghijklmnopqrstuvwxyz",
    "ada@example.com",
    "Bearer abcdefghijklmnop1234",
)


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
            yield conn
        finally:
            transaction.rollback()


def _projects(connection):
    connection.execute(text("INSERT INTO orgs (id, name) VALUES ('org_a', 'A'), ('org_b', 'B')"))
    connection.execute(
        text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_a', 'org_a', 'A'), ('ws_b', 'org_b', 'B')")
    )
    connection.execute(
        text(
            "INSERT INTO projects (id, workspace_id, name, slug) VALUES "
            "('proj_a', 'ws_a', 'A', 'a'), ('proj_b', 'ws_b', 'B', 'b')"
        )
    )


def _available(pg_engine) -> set[str]:
    with pg_engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM pg_available_extensions WHERE name IN ('vector', 'age')")
        ).all()
    return {row[0] for row in rows}


def _install(pg_engine, names: set[str]) -> None:
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if "vector" in names:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        if "age" in names:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS age"))
            connection.execute(text("LOAD 'age'"))
            connection.execute(text('SET search_path = ag_catalog, "$user", public'))
            found = connection.execute(
                text("SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'boss_kg'")
            ).first()
            if found is None:
                connection.execute(text("SELECT create_graph('boss_kg')"))


def _node(connection, key: str, body: str) -> str:
    return STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="symbol",
        node_key=key,
        name=key,
        body=body,
        trust_tier="observed",
        actor_kind="system",
    )


def test_vector_index_follows_the_installed_extension(pg_engine, connection):
    _projects(connection)
    names = _available(pg_engine)
    if "vector" in names:
        _install(pg_engine, names)
    near = _node(connection, "near", "near")
    mid = _node(connection, "mid", "mid")
    for key, node_id, embedding in (
        ("near", near, [1.0, 0.0]),
        ("mid", mid, [0.6, 0.8]),
    ):
        digest = STORE.get_node(connection, project_id="proj_a", node_id=node_id)["content_hash"]
        STORE.upsert_embedding(
            connection,
            project_id="proj_a",
            node_id=node_id,
            model_id="fixture-model",
            embedding=embedding,
            content_hash=digest,
        )
    hits = STORE.vector_search(
        connection,
        project_id="proj_a",
        model_id="fixture-model",
        embedding=[1.0, 0.0],
    )
    assert [hit["node_key"] for hit in hits] == ["near", "mid"]
    status = SERVICE.status(connection, project_id="proj_a")
    if "vector" in names:
        assert STORE.vector_index_kind(connection) == "hnsw"
        assert status["vector_index"] == "hnsw"
        assert connection.execute(text("SELECT to_regclass('public.kg_vec_2')")).scalar() == "kg_vec_2"
    else:
        assert STORE.vector_index_kind(connection) == "not_created"
        assert status["vector_index"] == "not_created"
        assert connection.execute(text("SELECT to_regclass('public.kg_vec_2')")).scalar() is None


def test_age_neighbors_match_the_cte_when_age_is_installed(pg_engine, connection):
    _projects(connection)
    names = _available(pg_engine)
    version = extension_version(connection)
    status = SERVICE.status(connection, project_id="proj_a")
    assert status["traversal"] == "recursive_cte"
    assert status["ship_gate"] == "case_study_primary_not_run"
    if "age" not in names:
        assert version == "not_installed"
        assert status["age_extension"] == "not_installed"
        with pytest.raises(RuntimeError, match="not installed"):
            project_graph(connection, STORE, project_id="proj_a")
        with pytest.raises(ValueError, match="recursive_cte"):
            STORE.traverse(
                connection,
                project_id="proj_a",
                start_id="missing",
                max_depth=1,
                pattern="(a)-[:CALLS]->(b)",
            )
        return
    _install(pg_engine, names)
    assert extension_version(connection) == "1.6.0"
    alpha = _node(connection, "alpha", "alpha")
    beta = _node(connection, "beta", "beta")
    gamma = _node(connection, "gamma", "gamma")
    STORE.upsert_edge(
        connection, project_id="proj_a", src_id=alpha, dst_id=beta, kind="calls", actor_kind="system"
    )
    STORE.upsert_edge(
        connection, project_id="proj_a", src_id=beta, dst_id=gamma, kind="calls", actor_kind="system"
    )
    projected = project_graph(connection, STORE, project_id="proj_a")
    assert projected["nodes"] >= 3
    assert projected["edges"] >= 2
    walked = STORE.traverse(connection, project_id="proj_a", start_id=alpha, max_depth=2)
    aged = age_neighbors(connection, project_id="proj_a", start_id=alpha, max_depth=2)
    assert [(row["node_id"], row["depth"]) for row in walked] == [
        (row["node_id"], row["depth"]) for row in aged
    ]
    with pytest.raises(ValueError, match="recursive_cte"):
        STORE.traverse(
            connection,
            project_id="proj_a",
            start_id=alpha,
            max_depth=1,
            pattern="(a)-[:CALLS]->(b)",
        )


def test_one_hundred_facts_score_and_store_zero_secrets(connection):
    _projects(connection)
    corpus = conformance_corpus()
    scored = score_extractor(LineExtractor(), corpus)
    extractor = LineExtractor()
    for index, item in enumerate(corpus):
        body = item["text"]
        if index < 4:
            body = f"{body}\n{SECRETS[index]}"
        capture_lifecycle(
            connection,
            STORE,
            project_id="proj_a",
            record={
                "kind": "runbook",
                "source_id": f"conf-{index}",
                "title": f"conformance {index}",
                "body": body,
            },
        )
        distil(
            connection,
            STORE,
            project_id="proj_a",
            capture_id=f"cap-{index}",
            text=body,
            extractor=extractor,
        )
    stored = connection.execute(
        text(
            """
            SELECT body FROM kg_nodes WHERE project_id = 'proj_a'
            UNION ALL
            SELECT body FROM kg_captures WHERE project_id = 'proj_a'
            """
        )
    ).scalars().all()
    blob = "\n".join(stored)
    for secret in SECRETS:
        assert secret not in blob
    assert contains_secret_marker(blob) is False
    assert "[EMAIL]" in blob
    fact_count = connection.execute(
        text("SELECT COUNT(*) FROM kg_nodes WHERE project_id = 'proj_a' AND kind = 'fact'")
    ).scalar_one()
    assert fact_count == scored["true_positive"]
    assert scored["labelled_facts"] == 100
    assert scored["precision"] >= 0.85


def test_retrieval_p95_records_the_sample_size(connection):
    _projects(connection)
    measured = measure_retrieval(
        connection,
        STORE,
        project_id="proj_a",
        nodes=24,
        edges=24,
        queries=5,
    )
    assert measured["nodes"] == 24
    assert measured["facts"] == 24
    assert 1 <= measured["edges"] <= 24
    assert measured["queries"] == 5
    assert measured["hybrid_p95_ms"] >= 0
    assert measured["scale_nodes"] == 10_000_000
    assert measured["scale_target_met"] is False
