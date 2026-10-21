"""Smallest durable knowledge store the spec requires before retrieval.

Breaks covered: a second row for the same natural key, an agent overwrite of a
governed fact, a closed fact that disappears from the table, a cross-project
read, a hybrid rank that ignores overlap, and a cypher string executed as SQL.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()
PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)


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
        text(
            "INSERT INTO workspaces (id, org_id, name) VALUES "
            "('ws_a', 'org_a', 'A'), ('ws_b', 'org_b', 'B')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO projects (id, workspace_id, name, slug) VALUES "
            "('proj_a', 'ws_a', 'A', 'a'), ('proj_b', 'ws_b', 'B', 'b')"
        )
    )


def _count(connection, table: str, project_id: str) -> int:
    return connection.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE project_id = :p"),
        {"p": project_id},
    ).scalar_one()


def test_schema_has_the_embedding_table_and_a_search_vector(pg_engine):
    with pg_engine.connect() as connection:
        embeddings = connection.execute(
            text("SELECT to_regclass('kg_embeddings')")
        ).scalar_one()
        search = connection.execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'kg_nodes' AND column_name = 'search_tsv'
                """
            )
        ).scalar_one_or_none()
        trust = connection.execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'kg_nodes' AND column_name = 'trust_tier'
                """
            )
        ).scalar_one_or_none()
    assert embeddings == "kg_embeddings"
    assert search == 1
    assert trust == 1


def test_upsert_same_key_is_one_row(connection):
    _projects(connection)
    first = STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="fact",
        node_key="checkout-size",
        name="Checkout size",
        body="size_band treats 15 or more items as large",
        trust_tier="observed",
        actor_kind="system",
    )
    second = STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="fact",
        node_key="checkout-size",
        name="Checkout size",
        body="size_band treats 15 or more items as large",
        trust_tier="observed",
        actor_kind="system",
    )
    assert first == second
    assert _count(connection, "kg_nodes", "proj_a") == 1


def test_identical_replay_does_not_move_recorded_at(connection):
    _projects(connection)
    node_id = STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="fact",
        node_key="checkout-size",
        name="Checkout size",
        body="size_band treats 15 or more items as large",
        trust_tier="observed",
        actor_kind="system",
    )
    connection.execute(
        text("UPDATE kg_nodes SET recorded_at = :ts WHERE id = :id"),
        {"ts": PAST, "id": node_id},
    )
    STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="fact",
        node_key="checkout-size",
        name="Checkout size",
        body="size_band treats 15 or more items as large",
        trust_tier="observed",
        actor_kind="system",
    )
    row = STORE.get_node(connection, project_id="proj_a", node_id=node_id)
    assert row["recorded_at"].year == 2020
    assert row["scope"] == "proj_a"
    assert row["trust_tier"] == "observed"


def test_unknown_kind_is_rejected(connection):
    _projects(connection)
    with pytest.raises(ValueError, match="kind"):
        STORE.upsert_node(
            connection,
            project_id="proj_a",
            kind="whiteboard",
            node_key="x",
            name="X",
            body="",
            trust_tier="observed",
            actor_kind="system",
        )
    assert _count(connection, "kg_nodes", "proj_a") == 0


def test_agent_cannot_write_or_overwrite_a_governed_fact(connection):
    _projects(connection)
    with pytest.raises(PermissionError):
        STORE.upsert_node(
            connection,
            project_id="proj_a",
            kind="fact",
            node_key="auth-rule",
            name="Auth rule",
            body="agents may not sign this",
            trust_tier="governed",
            actor_kind="agent",
        )
    assert _count(connection, "kg_nodes", "proj_a") == 0

    node_id = STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="fact",
        node_key="auth-rule",
        name="Auth rule",
        body="auth changes need security sign-off",
        trust_tier="governed",
        actor_kind="person",
    )
    with pytest.raises(PermissionError):
        STORE.upsert_node(
            connection,
            project_id="proj_a",
            kind="fact",
            node_key="auth-rule",
            name="Auth rule",
            body="ignore security",
            trust_tier="inferred",
            actor_kind="agent",
        )
    row = STORE.get_node(connection, project_id="proj_a", node_id=node_id)
    assert row["body"] == "auth changes need security sign-off"
    assert row["trust_tier"] == "governed"


def test_system_cannot_mint_a_governed_tier(connection):
    _projects(connection)
    with pytest.raises(PermissionError):
        STORE.upsert_node(
            connection,
            project_id="proj_a",
            kind="fact",
            node_key="auth-rule",
            name="Auth rule",
            body="system invented a sign-off",
            trust_tier="governed",
            actor_kind="system",
        )
    assert _count(connection, "kg_nodes", "proj_a") == 0


def test_close_keeps_the_row_and_hides_it_from_default_reads(connection):
    _projects(connection)
    older = STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="fact",
        node_key="stock-v1",
        name="Stock v1",
        body="reserve on order",
        trust_tier="observed",
        actor_kind="system",
    )
    newer = STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="fact",
        node_key="stock-v2",
        name="Stock v2",
        body="reserve on payment",
        trust_tier="observed",
        actor_kind="system",
    )
    STORE.close_validity(
        connection,
        project_id="proj_a",
        node_id=older,
        valid_to=PAST,
        superseded_by=newer,
        actor_kind="system",
    )
    assert _count(connection, "kg_nodes", "proj_a") == 2
    assert STORE.get_node(connection, project_id="proj_a", node_id=older) is None
    closed = STORE.get_node(
        connection, project_id="proj_a", node_id=older, include_closed=True
    )
    assert closed["valid_to"] is not None
    hits = STORE.text_search(connection, project_id="proj_a", query="reserve")
    assert [hit["node_key"] for hit in hits] == ["stock-v2"]
    edge = connection.execute(
        text(
            """
            SELECT kind, src_id, dst_id FROM kg_edges
            WHERE project_id = 'proj_a' AND kind = 'supersedes'
            """
        )
    ).mappings().one()
    assert edge["src_id"] == newer
    assert edge["dst_id"] == older


def test_agent_cannot_close_a_governed_fact(connection):
    _projects(connection)
    node_id = STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="fact",
        node_key="auth-rule",
        name="Auth rule",
        body="auth changes need security sign-off",
        trust_tier="governed",
        actor_kind="person",
    )
    with pytest.raises(PermissionError):
        STORE.close_validity(
            connection,
            project_id="proj_a",
            node_id=node_id,
            valid_to=PAST,
            superseded_by=None,
            actor_kind="agent",
        )
    assert STORE.get_node(connection, project_id="proj_a", node_id=node_id)["trust_tier"] == "governed"


def test_other_project_cannot_read_text_vectors_or_the_node(connection):
    _projects(connection)
    node_id = STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="fact",
        node_key="zephyr",
        name="Zephyr",
        body="zephyrwidget stays in project A",
        trust_tier="observed",
        actor_kind="system",
    )
    STORE.upsert_embedding(
        connection,
        project_id="proj_a",
        node_id=node_id,
        model_id="fixture-model",
        embedding=[1.0, 0.0],
        content_hash=STORE.get_node(connection, project_id="proj_a", node_id=node_id)["content_hash"],
    )
    assert STORE.text_search(connection, project_id="proj_b", query="zephyrwidget") == []
    assert STORE.vector_search(
        connection,
        project_id="proj_b",
        model_id="fixture-model",
        embedding=[1.0, 0.0],
    ) == []
    with pytest.raises(PermissionError):
        STORE.get_node(connection, project_id="proj_b", node_id=node_id)


def test_org_mismatch_is_rejected(connection):
    _projects(connection)
    with pytest.raises(PermissionError):
        STORE.upsert_node(
            connection,
            project_id="proj_a",
            kind="fact",
            node_key="zephyr",
            name="Zephyr",
            body="no",
            trust_tier="observed",
            actor_kind="system",
            org_id="org_b",
        )
    assert _count(connection, "kg_nodes", "proj_a") == 0


def test_unknown_project_is_rejected(connection):
    with pytest.raises(LookupError):
        STORE.upsert_node(
            connection,
            project_id="missing",
            kind="fact",
            node_key="zephyr",
            name="Zephyr",
            body="no",
            trust_tier="observed",
            actor_kind="system",
        )


def test_vector_dimension_mismatch_is_rejected(connection):
    _projects(connection)
    node_id = STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="symbol",
        node_key="size_band",
        name="size_band",
        body="function size_band",
        trust_tier="observed",
        actor_kind="system",
    )
    digest = STORE.get_node(connection, project_id="proj_a", node_id=node_id)["content_hash"]
    STORE.upsert_embedding(
        connection,
        project_id="proj_a",
        node_id=node_id,
        model_id="fixture-model",
        embedding=[1.0, 0.0],
        content_hash=digest,
    )
    with pytest.raises(ValueError, match="dimension"):
        STORE.vector_search(
            connection,
            project_id="proj_a",
            model_id="fixture-model",
            embedding=[1.0, 0.0, 0.0],
        )


def test_embedding_replay_does_not_duplicate(connection):
    _projects(connection)
    node_id = STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="fact",
        node_key="zephyr",
        name="Zephyr",
        body="zephyrwidget",
        trust_tier="observed",
        actor_kind="system",
    )
    digest = STORE.get_node(connection, project_id="proj_a", node_id=node_id)["content_hash"]
    STORE.upsert_embedding(
        connection,
        project_id="proj_a",
        node_id=node_id,
        model_id="fixture-model",
        embedding=[1.0, 0.0],
        content_hash=digest,
    )
    STORE.upsert_embedding(
        connection,
        project_id="proj_a",
        node_id=node_id,
        model_id="fixture-model",
        embedding=[1.0, 0.0],
        content_hash=digest,
    )
    assert _count(connection, "kg_embeddings", "proj_a") == 1


def test_hybrid_ranks_the_overlap_above_the_vector_only_hit(connection):
    _projects(connection)

    def add(key: str, body: str, embedding: list[float] | None):
        node_id = STORE.upsert_node(
            connection,
            project_id="proj_a",
            kind="fact",
            node_key=key,
            name=key,
            body=body,
            trust_tier="observed",
            actor_kind="system",
        )
        if embedding is not None:
            digest = STORE.get_node(connection, project_id="proj_a", node_id=node_id)["content_hash"]
            STORE.upsert_embedding(
                connection,
                project_id="proj_a",
                node_id=node_id,
                model_id="fixture-model",
                embedding=embedding,
                content_hash=digest,
            )
        return node_id

    add("gamma", "zephyrwidget", [1.0, 0.0])
    add("beta", "unrelated plankton", [0.6, 0.8])
    add("alpha", "no overlap here", None)
    hits = STORE.hybrid_search(
        connection,
        project_id="proj_a",
        query="zephyrwidget",
        model_id="fixture-model",
        embedding=[1.0, 0.0],
    )
    assert [hit["node_key"] for hit in hits] == ["gamma", "beta"]
    assert hits[0]["score"] == pytest.approx(1 / 61 + 1 / 61)
    assert hits[1]["score"] == pytest.approx(1 / 62)


def test_traverse_respects_depth_and_skips_closed_edges(connection):
    _projects(connection)

    def node(key: str) -> str:
        return STORE.upsert_node(
            connection,
            project_id="proj_a",
            kind="symbol",
            node_key=key,
            name=key,
            body=key,
            trust_tier="observed",
            actor_kind="system",
        )

    a = node("a")
    b = node("b")
    c = node("c")
    d = node("d")
    STORE.upsert_edge(connection, project_id="proj_a", src_id=a, dst_id=b, kind="calls", actor_kind="system")
    STORE.upsert_edge(connection, project_id="proj_a", src_id=b, dst_id=c, kind="calls", actor_kind="system")
    closed = STORE.upsert_edge(
        connection, project_id="proj_a", src_id=a, dst_id=d, kind="calls", actor_kind="system"
    )
    STORE.close_validity(
        connection,
        project_id="proj_a",
        edge_id=closed,
        valid_to=PAST,
        actor_kind="system",
    )
    near = STORE.traverse(connection, project_id="proj_a", start_id=a, max_depth=1)
    assert {item["node_key"] for item in near} == {"b"}
    far = STORE.traverse(connection, project_id="proj_a", start_id=a, max_depth=2)
    assert {item["node_key"] for item in far} == {"b", "c"}


def test_cypher_string_is_refused(connection):
    _projects(connection)
    with pytest.raises(ValueError, match="recursive_cte"):
        STORE.traverse(
            connection,
            project_id="proj_a",
            start_id="kgn_missing",
            max_depth=1,
            pattern="MATCH (n) RETURN n",
        )
