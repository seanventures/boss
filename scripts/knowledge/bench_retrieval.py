"""Phase 3 latency benchmark: p95 of retrieval.search and knowledge_pack on a synthetic graph.

    DATABASE_URL=postgresql+psycopg://... python scripts/knowledge/bench_retrieval.py --nodes 1000000

Builds (once) a synthetic project `kg-bench` with N symbol nodes, 5N edges (calls), N/2 facts
over a 2,000-word vocabulary, then times 200 searches and 200 packs with 3 touched symbols each.
Prints JSON. No model calls.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time

from sqlalchemy import create_engine, text

from boss_engine.services.knowledge import Scope
from boss_engine.services.knowledge import retrieval as rt

P = "kg-bench"


def build(c, n: int) -> None:
    if c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p"), {"p": P}).scalar() >= n:
        return
    c.execute(text("INSERT INTO orgs (id, name) VALUES ('kg-bench-org', 'bench') ON CONFLICT DO NOTHING"))
    c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('kg-bench-ws', 'kg-bench-org', 'bench') "
                   "ON CONFLICT DO NOTHING"))
    c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'kg-bench-ws', :p, :p) "
                   "ON CONFLICT DO NOTHING"), {"p": P})
    c.execute(text("""INSERT INTO kg_nodes (id, project_id, kind, node_key, name, path, repo)
        SELECT 'bn' || i, :p, 'symbol', 'bench/m' || (i / 50) || '.py::f' || i, 'f' || i, 'bench/m' || (i / 50) || '.py',
               'acme/bench' FROM generate_series(1, :n) i ON CONFLICT DO NOTHING"""), {"p": P, "n": n})
    c.execute(text("""INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind)
        SELECT 'be' || i || '_' || j, :p, 'bn' || i, 'bn' || (1 + (hashtext(i || ':' || j) & 2147483647) % :n), 'calls'
        FROM generate_series(1, :n) i, generate_series(1, 5) j ON CONFLICT DO NOTHING"""), {"p": P, "n": n})
    c.execute(text("""INSERT INTO kg_facts (id, project_id, subject, subject_node_id, predicate, statement, kind, tier,
                                            content_hash, read_permissions)
        SELECT 'bf' || i, :p, 'f' || (2 * i), 'bn' || (2 * i), 'is',
               'w' || (hashtext('a' || i) & 2047) || ' w' || (hashtext('b' || i) & 2047) || ' w' ||
               (hashtext('c' || i) & 2047) || ' must hold for f' || (2 * i),
               (ARRAY['invariant','decision','lesson','pattern','coupling','risk'])[1 + i % 6],
               (ARRAY['inferred','observed','governed','approved'])[1 + i % 4], md5('bf' || i),
               ARRAY['repository:read'] FROM generate_series(1, :m) i ON CONFLICT DO NOTHING"""),
              {"p": P, "m": n // 2})
    c.execute(text("ANALYZE kg_nodes; ANALYZE kg_edges; ANALYZE kg_facts"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=1_000_000)
    ap.add_argument("--queries", type=int, default=200)
    a = ap.parse_args()
    eng = create_engine(os.environ["DATABASE_URL"])
    t0 = time.time()
    with eng.begin() as c:
        build(c, a.nodes)
    built = time.time() - t0
    rnd = random.Random(7)
    reader = rt.Reader(Scope(P), permissions=frozenset({"repository:read", "code_task:read", "decision:read",
                                                        "reliability:read"}))
    search_ms, pack_ms, over = [], [], 0
    with eng.connect() as c:
        for _ in range(a.queries):
            touched = [f"f{rnd.randint(1, a.nodes)}" for _ in range(3)]
            q = " ".join(f"w{rnd.randint(0, 2047)}" for _ in range(6)) + " fix the totals"
            s = time.perf_counter()
            rt.search(c, reader, q, touched=touched)
            search_ms.append((time.perf_counter() - s) * 1000)
            s = time.perf_counter()
            p = rt.knowledge_pack(c, reader, "software_engineer", "implement", {"text": q, "touched": touched}, 1200,
                                  log=False)
            pack_ms.append((time.perf_counter() - s) * 1000)
            over += p.tokens > 1200
        sizes = dict(c.execute(text(
            "SELECT 'nodes', count(*) FROM kg_nodes WHERE project_id = :p UNION ALL "
            "SELECT 'edges', count(*) FROM kg_edges WHERE project_id = :p UNION ALL "
            "SELECT 'facts', count(*) FROM kg_facts WHERE project_id = :p"), {"p": P}).all())
    print(json.dumps({"sizes": sizes, "build_seconds": round(built, 1), "queries": a.queries,
                      "search_p50_ms": round(sorted(search_ms)[len(search_ms) // 2], 1),
                      "search_p95_ms": round(rt.p95(search_ms), 1),
                      "pack_p50_ms": round(sorted(pack_ms)[len(pack_ms) // 2], 1),
                      "pack_p95_ms": round(rt.p95(pack_ms), 1), "packs_over_budget": over}, indent=2))


if __name__ == "__main__":
    main()
