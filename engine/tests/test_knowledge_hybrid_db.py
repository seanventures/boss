"""Hybrid retrieval by default: full text, the graph and the vector index, fused by RRF.

Operator decision 2026-10-22. Breaks covered: search that is full text only (PR #15), so a fact
about a symbol the query names, or one that says the same thing in other words, is never
found; embeddings that do not use the HNSW index when pgvector is there; a missing embedding
model that silently narrows search instead of saying why in status; the vector list read
outside the stage's scope.
"""
from __future__ import annotations

import hashlib
import math
import os

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import scopes
from boss_engine.services.knowledge.code_index import index_files
from boss_engine.services.knowledge.embeddings import NO_MODEL, embed_pending
from boss_engine.services.knowledge.service import KnowledgeService
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
SERVICE = KnowledgeService(STORE)
# A tiny "meaning": words of one concept land on the same dimension.
CONCEPTS = {"money": 0, "cash": 0, "invoice": 0, "currency": 0, "payment": 0,
            "rounding": 1, "rounds": 1, "half-even": 1, "bankers": 1,
            "deploy": 2, "release": 2, "ship": 2}


class FakeEmbedder:
    model_id = "fake-concepts-8"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for value in texts:
            vector = [0.0] * 8
            for word in value.lower().replace(",", " ").replace(".", " ").split():
                slot = CONCEPTS.get(word)
                if slot is None:
                    slot = 3 + int(hashlib.sha256(word.encode()).hexdigest(), 16) % 5
                vector[slot] += 1.0
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            out.append([x / norm for x in vector])
        return out


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
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_hy', 'HY')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_hy', 'org_hy', 'HY')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('proj_hy', 'ws_hy', 'HY', 'hy')"))
            yield conn
        finally:
            transaction.rollback()


def _fact(connection, key: str, body: str) -> str:
    return STORE.upsert_node(connection, project_id="proj_hy", kind="fact", node_key=f"fact:{key}",
                             name=key, body=body, trust_tier="observed", actor_kind="system")


def _hit(result, node_id):
    return next((h for h in result["hits"] if h["node_id"] == node_id), None)


def test_the_graph_finds_a_fact_about_the_symbol_the_query_names(connection):
    index_files(connection, STORE, project_id="proj_hy",
                files=[{"path": "stock.py", "language": "python", "source": "def reserve():\n    return 1\n"}])
    symbol = STORE.find_by_key(connection, project_id="proj_hy", kind="symbol", node_key="symbol:stock.py:reserve")
    fact = _fact(connection, "hold", "Holds expire after fifteen minutes")
    STORE.upsert_edge(connection, project_id="proj_hy", src_id=fact, dst_id=symbol["id"], kind="about",
                      actor_kind="system")
    found = SERVICE.search(connection, project_id="proj_hy", query="reserve")
    assert _hit(found, fact)["via"] == ["graph"]                  # no word of the query is in the fact
    assert found["retrieval"] == {"mode": "text+graph", "lists": {"text": 1, "graph": 1}, "reason": NO_MODEL}


def test_with_an_embedder_a_fact_in_other_words_is_found_through_the_vector_index(connection):
    said_otherwise = _fact(connection, "bankers", "Invoice totals use bankers rounding")
    _fact(connection, "ship", "Release only after a sign-off")
    embedder = FakeEmbedder()
    assert embed_pending(connection, STORE, project_id="proj_hy", embedder=embedder) == 2
    found = SERVICE.search(connection, project_id="proj_hy", query="currency half-even", embedder=embedder)
    assert found["retrieval"]["mode"] == "hybrid"
    assert _hit(found, said_otherwise)["via"] == ["vector"]       # no shared word, the same meaning
    if STORE.vector_extension_version(connection):
        assert STORE.vector_index_kind(connection) == "hnsw"
        connection.execute(text("SET LOCAL enable_seqscan = off"))     # two rows: force the index
        plan = "\n".join(connection.execute(text(
            "EXPLAIN SELECT embedding_id FROM kg_vec_8 ORDER BY embedding <=> CAST(:q AS vector) LIMIT 5"),
            {"q": "[" + ",".join(["0.1"] * 8) + "]"}).scalars().all())
        assert "kg_vec_8_hnsw" in plan


def test_the_vector_list_stays_inside_the_stage_scope(connection):
    index_files(connection, STORE, project_id="proj_hy",
                files=[{"path": "money.py", "language": "python", "source": "def currency_rounding():\n    return 1\n"}])
    embedder = FakeEmbedder()
    symbol = STORE.find_by_key(connection, project_id="proj_hy", kind="symbol",
                               node_key="symbol:money.py:currency_rounding")
    STORE.upsert_embedding(connection, project_id="proj_hy", node_id=symbol["id"], model_id=embedder.model_id,
                           embedding=embedder.embed(["currency rounding"])[0],
                           content_hash=STORE.get_node(connection, project_id="proj_hy",
                                                       node_id=symbol["id"])["content_hash"])
    triage = scopes.policy_for(stage="triage")
    found = SERVICE.search(connection, project_id="proj_hy", query="currency rounding", embedder=embedder,
                           policy=triage)
    assert _hit(found, symbol["id"]) is None


def test_status_says_how_retrieval_runs(connection):
    status = SERVICE.status(connection, project_id="proj_hy")
    assert status["retrieval"] == {"mode": "text+graph", "reason": NO_MODEL}
    assert "vector_extension" in status
