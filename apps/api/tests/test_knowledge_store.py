"""Knowledge Graph Phase 0 — the KnowledgeStore interface and its Postgres skeleton (spec §3.1, §5, §7).

Scope enforcement in the store, temporal close (supersede, never delete), idempotent upserts over
the full §4 kind set, bounded time-aware traversal, full text, and model-free hybrid fusion.
Runs against the isolated boss_test DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from boss_engine.services.knowledge import (
    KnowledgeStore,
    NotImplementedInPhase,
    Pattern,
    PostgresKnowledgeStore,
    Scope,
    reciprocal_rank_fusion,
)
from boss_engine.services.knowledge_graph import KnowledgeGraphService
from sqlalchemy import text

from apps.api.app.db import begin

STORE = PostgresKnowledgeStore()


def _scopes(c):
    pid = c.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar_one()
    c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                   "SELECT 'proj-kstore-other', workspace_id, 'KStore other', 'kstore-other' "
                   "FROM projects WHERE id = :p ON CONFLICT (id) DO NOTHING"), {"p": pid})
    return Scope(pid), Scope("proj-kstore-other")


def test_the_postgres_store_implements_the_protocol():
    assert isinstance(STORE, KnowledgeStore)
    assert isinstance(KnowledgeGraphService().store, KnowledgeStore)


def test_upserts_accept_the_code_and_lifecycle_kinds_and_stay_idempotent(engine):
    with begin(engine) as c:
        mine, _ = _scopes(c)
        fn = STORE.upsert_node(c, mine, "symbol", "saleor/checkout.py::complete", "complete")
        again = STORE.upsert_node(c, mine, "symbol", "saleor/checkout.py::complete", attrs={"lang": "python"})
        pr = STORE.upsert_node(c, mine, "pull_request", "saleor#42")
        assert fn == again
        e1 = STORE.upsert_edge(c, mine, pr, fn, "changed")
        assert STORE.upsert_edge(c, mine, pr, fn, "changed", confidence=0.5) == e1
        # The SRE facade keeps its narrower kinds: a code kind is still refused there.
        with pytest.raises(ValueError, match="unknown node kind"):
            KnowledgeGraphService().upsert_node(c, mine.project_id, "symbol", "x")


def test_scope_is_enforced_by_the_store_on_every_path(engine):
    with begin(engine) as c:
        mine, theirs = _scopes(c)
        a = STORE.upsert_node(c, mine, "service", "ks-scope-a")
        b = STORE.upsert_node(c, theirs, "service", "ks-scope-b")
        with pytest.raises(PermissionError):
            STORE.upsert_edge(c, mine, a, b, "depends_on")
        with pytest.raises(PermissionError):
            STORE.traverse(c, mine, Pattern(seeds=[b]))
        with pytest.raises(PermissionError):
            STORE.close_validity(c, mine, "node", b)
        with pytest.raises(LookupError):
            STORE.text_search(c, Scope("no-such-project"), "anything")
        with pytest.raises(ValueError):
            STORE.upsert_node(c, Scope(""), "service", "x")
        assert [h.id for h in STORE.text_search(c, mine, "ks-scope-b")] == []


def test_close_validity_supersedes_without_deleting_and_traversal_respects_it(engine):
    with begin(engine) as c:
        mine, _ = _scopes(c)
        svc = STORE.upsert_node(c, mine, "service", "ks-close-svc")
        old_db = STORE.upsert_node(c, mine, "datastore", "ks-close-old")
        new_db = STORE.upsert_node(c, mine, "datastore", "ks-close-new")
        old_edge = STORE.upsert_edge(c, mine, svc, old_db, "runs_on")
        STORE.upsert_edge(c, mine, svc, new_db, "runs_on")
        before = datetime.now(timezone.utc) - timedelta(seconds=1)

        assert STORE.close_validity(c, mine, "edge", old_edge, superseded_by=new_db) is True
        assert STORE.close_validity(c, mine, "edge", old_edge) is False   # already closed
        row = c.execute(text("SELECT valid_to, attrs FROM kg_edges WHERE id = :i"), {"i": old_edge}).mappings().one()
        assert row["valid_to"] is not None and row["attrs"]["superseded_by"] == new_db

        now_keys = {n["node_key"] for n in STORE.traverse(c, mine, Pattern(seeds=[svc], depth=1))["nodes"]}
        assert now_keys == {"ks-close-svc", "ks-close-new"}
        then_keys = {n["node_key"] for n in STORE.traverse(c, mine, Pattern(seeds=[svc], depth=1, valid_at=before))["nodes"]}
        assert "ks-close-old" in then_keys


def test_traverse_is_bounded_cycle_safe_directional_and_filters_kinds(engine):
    with begin(engine) as c:
        mine, _ = _scopes(c)
        ids = [STORE.upsert_node(c, mine, "symbol", f"ks-walk-{i}") for i in range(5)]
        for a, b in zip(ids, ids[1:]):
            STORE.upsert_edge(c, mine, a, b, "calls")
        STORE.upsert_edge(c, mine, ids[4], ids[0], "calls")           # a cycle
        STORE.upsert_edge(c, mine, ids[0], ids[3], "imports")

        out = STORE.traverse(c, mine, Pattern(seeds=[ids[0]], depth=2, edge_kinds=["calls"]))["nodes"]
        assert {n["node_key"]: n["depth"] for n in out} == {"ks-walk-0": 0, "ks-walk-1": 1, "ks-walk-2": 2}
        shortest = STORE.traverse(c, mine, Pattern(seeds=[ids[0]], depth=4))["nodes"]
        assert {n["node_key"]: n["depth"] for n in shortest}["ks-walk-3"] == 1   # via imports, not 3 calls
        callers = STORE.traverse(c, mine, Pattern(seeds=[ids[0]], depth=1, direction="in"))["nodes"]
        assert {n["node_key"] for n in callers} == {"ks-walk-0", "ks-walk-4"}
        whole_cycle = STORE.traverse(c, mine, Pattern(seeds=[ids[0]], depth=50, edge_kinds=["calls"]))
        assert len(whole_cycle["nodes"]) == 5 and whole_cycle["depth"] == 6        # depth is capped
        with pytest.raises(ValueError):
            STORE.traverse(c, mine, Pattern(seeds=[ids[0]], edge_kinds=["teleports"]))
        with pytest.raises(NotImplementedInPhase):
            STORE.traverse(c, mine, "MATCH (a)-[:calls]->(b) RETURN b")


def test_text_and_hybrid_search_fuse_text_and_graph_candidates(engine):
    with begin(engine) as c:
        mine, _ = _scopes(c)
        checkout = STORE.upsert_node(c, mine, "module", "saleor.checkout", "checkout complete mutation")
        payment = STORE.upsert_node(c, mine, "module", "saleor.payment", "payment gateway")
        STORE.upsert_node(c, mine, "module", "saleor.warehouse", "warehouse stock")
        STORE.upsert_edge(c, mine, checkout, payment, "calls")

        assert [h.id for h in STORE.text_search(c, mine, "checkout")] == [checkout]
        hits = STORE.hybrid_search(c, mine, "payment", seeds=[checkout])
        assert hits[0].id == payment and sorted(hits[0].sources) == ["graph", "text"]
        with pytest.raises(NotImplementedInPhase):
            STORE.hybrid_search(c, mine, "payment", embedding=[0.1, 0.2], model_id="any")


def test_reciprocal_rank_fusion_rewards_agreement():
    fused = reciprocal_rank_fusion({"text": ["a", "b", "c"], "graph": ["c", "a"]})
    assert [h.id for h in fused] == ["a", "c", "b"]
    assert fused[0].sources == ["text", "graph"]
