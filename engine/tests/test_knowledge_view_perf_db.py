"""The Graph area's latency budgets on a Fineract-size graph (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §3).

Opt-in: ``BOSS_KG_PERF=1``. The database is ``BOSS_KG_PERF_DATABASE_URL`` (a disposable one), and
the project ``BOSS_KG_PERF_PROJECT`` (default ``proj_fin``). When that project has no code graph
yet and ``BOSS_KG_PERF_ROOT`` names a checkout (Apache Fineract 1.15.0 gives 51,319 nodes and
234,187 edges, the size of northpeak-bank-a), it is indexed first. p95 over warm runs; a route
over its budget fails, never waived:

overview < 300 ms · neighbourhood (depth <= 3, limit 80) < 200 ms · node detail < 150 ms ·
tree children < 100 ms · search < 150 ms · rollup rebuild < 3 s.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import view, view_rollup
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.skipif(os.environ.get("BOSS_KG_PERF") != "1", reason="opt-in: BOSS_KG_PERF=1")

PROJECT = os.environ.get("BOSS_KG_PERF_PROJECT", "proj_fin")
RUNS = int(os.environ.get("BOSS_KG_PERF_RUNS", "20"))


@pytest.fixture(scope="module")
def engine():
    url = os.environ.get("BOSS_KG_PERF_DATABASE_URL") or os.environ.get("BOSS_KG_TEST_DATABASE_URL") or ""
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp", "perf")):
        pytest.skip("BOSS_KG_PERF_DATABASE_URL must name a disposable database")
    eng = create_engine(url, future=True)
    ensure_schema(eng)
    with eng.begin() as c:
        symbols = c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = 'symbol' "
                                 "AND valid_to IS NULL"), {"p": PROJECT}).scalar()
    if not symbols:
        root = os.environ.get("BOSS_KG_PERF_ROOT")
        if not root:
            pytest.skip(f"{PROJECT} has no code graph and BOSS_KG_PERF_ROOT names no checkout")
        from boss_engine.services.knowledge.code_index import index_repository
        from boss_engine.services.knowledge.store import PostgresKnowledgeStore

        store = PostgresKnowledgeStore()
        with eng.begin() as c:
            store.ensure_schema(c)
        with eng.begin() as c:
            c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_perf', 'Perf') ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_perf', 'org_perf', 'Perf') "
                           "ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_perf', :p, :p) "
                           "ON CONFLICT DO NOTHING"), {"p": PROJECT})
            index_repository(c, store, project_id=PROJECT, root=Path(root), repo="apache/fineract")
    return eng


@pytest.fixture(scope="module")
def rebuilt(engine):
    """A first build (no rollup rows: the end of a project's first index), then a rebuild that
    finds nothing changed (the reconcile's case)."""
    with engine.begin() as c:
        for table in ("kg_view_tree", "kg_view_edges", "kg_view_sync"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": PROJECT})
    out = {}
    for label in ("first", "unchanged"):
        started = time.perf_counter()
        with engine.begin() as c:
            out["row"] = view_rollup.rebuild(c, PROJECT)
        out[label] = time.perf_counter() - started
    return out


def _p95(engine, fn) -> float:
    for _ in range(3):
        with engine.connect() as c:
            fn(c)
    samples = []
    for _ in range(RUNS):
        with engine.connect() as c:
            started = time.perf_counter()
            fn(c)
            samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return samples[max(0, int(round(0.95 * len(samples))) - 1)]


def _symbol(engine, name: str) -> str:
    with engine.connect() as c:
        found = c.execute(text(
            "SELECT n.id FROM kg_nodes n JOIN kg_view_tree t ON t.project_id = n.project_id AND t.node_id = n.id "
            "WHERE n.project_id = :p AND n.kind = 'symbol' AND n.name = :n AND n.valid_to IS NULL "
            "ORDER BY t.child_count DESC LIMIT 1"), {"p": PROJECT, "n": name}).scalar()
    if not found:
        pytest.skip(f"{name} is not in {PROJECT}")
    return found


def test_the_rollup_rebuilds_inside_three_seconds(rebuilt):
    print(f"\nrebuild: first {rebuilt['first']:.2f} s, unchanged {rebuilt['unchanged']:.2f} s, {rebuilt['row']}")
    assert rebuilt["first"] < 3.0 and rebuilt["unchanged"] < 3.0


def test_overview_inside_300_ms(engine, rebuilt):
    ms = _p95(engine, lambda c: view.overview(c, PROJECT))
    drill = _p95(engine, lambda c: view.overview(c, PROJECT, area="fineract-provider"))
    print(f"\noverview p95 {ms:.1f} ms; area drill-down p95 {drill:.1f} ms")
    assert ms < 300 and drill < 300


@pytest.mark.parametrize("name,depth", [
    ("LoanWritePlatformServiceJpaRepositoryImpl", 1), ("LoanWritePlatformServiceJpaRepositoryImpl", 3),
    ("Loan", 3), ("LoanWritePlatformServiceJpaRepositoryImpl.disburseLoan", 2),
    ("LoanWritePlatformServiceJpaRepositoryImpl.disburseLoan", 3)])
def test_neighbourhood_inside_200_ms(engine, rebuilt, name, depth):
    node = _symbol(engine, name)
    ms = _p95(engine, lambda c: view.neighbourhood(c, PROJECT, node, depth=depth, limit=80))
    print(f"\nneighbourhood {name} depth {depth} p95 {ms:.1f} ms")
    assert ms < 200


def test_node_detail_inside_150_ms(engine, rebuilt):
    for name in ("LoanWritePlatformServiceJpaRepositoryImpl.disburseLoan", "Loan"):
        node = _symbol(engine, name)
        ms = _p95(engine, lambda c: view.node_detail(c, PROJECT, node))
        print(f"\nnode detail {name} p95 {ms:.1f} ms")
        assert ms < 150


def test_tree_children_inside_100_ms(engine, rebuilt):
    with engine.connect() as c:
        area = view.tree_children(c, PROJECT)["rows"][0]["key"]
    for label, kwargs in (("areas", {}), ("packages", {"parent": area}), ("search", {"q": "disburse"})):
        ms = _p95(engine, lambda c: view.tree_children(c, PROJECT, **kwargs))
        print(f"\ntree {label} p95 {ms:.1f} ms")
        assert ms < 100


def test_search_inside_150_ms(engine, rebuilt):
    for q in ("disburse", "loan", "repayment schedule"):
        ms = _p95(engine, lambda c: view.search(c, PROJECT, q))
        print(f"\nsearch {q!r} p95 {ms:.1f} ms")
        assert ms < 150
