#!/usr/bin/env python3
"""Knowledge Graph Phase 0: Apache AGE vs recursive CTEs for 2–4 hop traversals (spec §3.1, §9).

Builds a synthetic graph in its own schema (default 1M nodes / 5M edges, kg_nodes/kg_edges
shaped, text ids, skewed in-degree so a few "utility" nodes are hubs, as in code graphs). It
projects the same graph into an AGE graph with AGE's bulk loaders, then times:

* dependencies (out, 2–4 hops) from random seeds: the store's own recursive CTE
  (services/knowledge/store.py) against AGE variable-length MATCH;
* blast radius (in, 2–3 hops) from mid-popularity nodes.

Every AGE answer is checked against the CTE answer (same reachable node set size), so a fast
wrong answer cannot win. Synthetic data only; needs the BOSS DB image (AGE + superuser for the
server-side CSV the loaders read). Prints a JSON report.

    python3 scripts/knowledge/bench_traversal.py --dsn postgresql://boss:boss@127.0.0.1:55432/kgbench
"""
from __future__ import annotations

import argparse
import json
import statistics
import time

import psycopg

SCHEMA = "kgbench"
GRAPH = "kgbench_graph"

CTE = """
WITH RECURSIVE walk(id, depth) AS (
    SELECT unnest(CAST(%(seeds)s AS TEXT[])), 0
    UNION
    SELECT nxt.id, w.depth + 1
    FROM walk w
    CROSS JOIN LATERAL ({step} AND e.project_id = 'p1'
        AND (e.valid_from IS NULL OR e.valid_from <= now())
        AND (e.valid_to IS NULL OR e.valid_to >= now())
        AND e.kind = 'calls') nxt
    WHERE w.depth < %(depth)s
)
SELECT count(DISTINCT id) FILTER (WHERE depth > 0) FROM walk
"""
STEP = {"out": "SELECT e.dst_id AS id FROM kgbench.kg_edges e WHERE e.src_id = w.id",
        "in": "SELECT e.src_id AS id FROM kgbench.kg_edges e WHERE e.dst_id = w.id"}
AGE = {"out": "MATCH (a:N {{id: '{seed}'}})-[:E*1..{depth}]->(b) RETURN DISTINCT id(b)",
       "in": "MATCH (a:N {{id: '{seed}'}})<-[:E*1..{depth}]-(b) RETURN DISTINCT id(b)"}


def age_fixed(direction: str, seed: int, depth: int) -> str:
    """The same question as fixed-length patterns, one per hop count, UNIONed (AGE plans joins)."""
    hop = "-[:E]->" if direction == "out" else "<-[:E]-"
    parts = [f"MATCH (a:N {{id: '{seed}'}})" + hop + "".join(f"(x{i}){hop}" for i in range(k - 1)) + "(b) RETURN id(b)"
             for k in range(1, depth + 1)]
    return " UNION ".join(parts)


def timed(cur, sql, params=None):
    t = time.perf_counter()
    try:
        cur.execute(sql, params)
        rows = cur.fetchall()
        return (time.perf_counter() - t) * 1000, rows
    except psycopg.errors.QueryCanceled:
        cur.connection.rollback()
        return None, None


def build(conn, nodes: int, out_degree: int, skew: float) -> dict:
    cur = conn.cursor()
    t0 = time.perf_counter()
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA}")
    cur.execute(f"""
        CREATE UNLOGGED TABLE {SCHEMA}.kg_nodes (id text PRIMARY KEY, project_id text NOT NULL,
            kind text NOT NULL, node_key text NOT NULL, n bigint NOT NULL);
        CREATE UNLOGGED TABLE {SCHEMA}.kg_edges (id text PRIMARY KEY, project_id text NOT NULL,
            src_id text NOT NULL, dst_id text NOT NULL, kind text NOT NULL,
            valid_from timestamptz, valid_to timestamptz);
        SELECT setseed(0.42);
        INSERT INTO {SCHEMA}.kg_nodes SELECT 'kgn_' || i, 'p1', 'symbol', 'sym' || i, i
            FROM generate_series(1, {nodes}) i;
        INSERT INTO {SCHEMA}.kg_edges
            SELECT 'kge_' || s || '_' || j, 'p1', 'kgn_' || s,
                   'kgn_' || (1 + floor({nodes} * power(random(), {skew})))::bigint, 'calls', NULL, NULL
            FROM generate_series(1, {nodes}) s, generate_series(1, {out_degree}) j;
        CREATE INDEX ON {SCHEMA}.kg_edges (src_id, kind);
        CREATE INDEX ON {SCHEMA}.kg_edges (dst_id, kind);
        ANALYZE {SCHEMA}.kg_nodes; ANALYZE {SCHEMA}.kg_edges;
    """)
    conn.commit()
    t1 = time.perf_counter()
    cur.execute("LOAD 'age'; SET search_path = ag_catalog, \"$user\", public")
    cur.execute("CREATE EXTENSION IF NOT EXISTS age")
    cur.execute("SELECT count(*) FROM ag_graph WHERE name = %s", (GRAPH,))
    if cur.fetchone()[0]:
        cur.execute("SELECT drop_graph(%s, true)", (GRAPH,))
    cur.execute(f"""
        COPY (SELECT n AS id FROM {SCHEMA}.kg_nodes ORDER BY n) TO '/tmp/kgbench_nodes.csv' CSV HEADER;
        COPY (SELECT substr(src_id, 5)::bigint AS start_id, 'N' AS start_vertex_type,
                     substr(dst_id, 5)::bigint AS end_id, 'N' AS end_vertex_type
              FROM {SCHEMA}.kg_edges) TO '/tmp/kgbench_edges.csv' CSV HEADER;
    """)
    cur.execute("SELECT create_graph(%s)", (GRAPH,))
    cur.execute("SELECT create_vlabel(%s, 'N')", (GRAPH,))
    cur.execute("SELECT create_elabel(%s, 'E')", (GRAPH,))
    conn.commit()     # AGE: commit after creating a graph or label (spec §3.1)
    cur.execute("SELECT load_labels_from_file(%s, 'N', '/tmp/kgbench_nodes.csv', true)", (GRAPH,))
    cur.execute("SELECT load_edges_from_file(%s, 'E', '/tmp/kgbench_edges.csv')", (GRAPH,))
    cur.execute(f"""
        CREATE INDEX ON {GRAPH}."N" USING gin (properties);
        CREATE INDEX ON {GRAPH}."N" (id);
        CREATE INDEX ON {GRAPH}."E" (start_id);
        CREATE INDEX ON {GRAPH}."E" (end_id);
        ANALYZE {GRAPH}."N"; ANALYZE {GRAPH}."E";
    """)
    conn.commit()
    t2 = time.perf_counter()
    cur.execute(f"""SELECT pg_total_relation_size('{SCHEMA}.kg_nodes') + pg_total_relation_size('{SCHEMA}.kg_edges'),
                           pg_total_relation_size('{GRAPH}."N"') + pg_total_relation_size('{GRAPH}."E"'),
                           (SELECT count(*) FROM {SCHEMA}.kg_edges)""")
    rel, age, edges = cur.fetchone()
    return {"nodes": nodes, "edges": edges, "relational_load_s": round(t1 - t0, 1),
            "age_load_s": round(t2 - t1, 1), "relational_mb": round(rel / 2**20), "age_mb": round(age / 2**20)}


def seeds_for(conn, direction: str, count: int) -> list[int]:
    cur = conn.cursor()
    if direction == "out":
        cur.execute("SELECT setseed(0.7)")
        cur.execute(f"SELECT n FROM {SCHEMA}.kg_nodes ORDER BY random() LIMIT {count}")
    else:
        # Mid-popularity targets: a realistic "who breaks if I change this" question, not the hubs.
        cur.execute(f"""SELECT substr(dst_id, 5)::bigint FROM {SCHEMA}.kg_edges GROUP BY dst_id
                        HAVING count(*) BETWEEN 5 AND 40 ORDER BY dst_id LIMIT {count}""")
    return [r[0] for r in cur.fetchall()]


def pct(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(q * (len(values) - 1))))], 1)


def run(conn, direction: str, depth: int, seeds: list[int], timeout_s: int) -> dict:
    cur = conn.cursor()
    cur.execute(f"SET statement_timeout = '{timeout_s}s'; LOAD 'age'; SET search_path = ag_catalog, \"$user\", public")
    out = {"direction": direction, "depth": depth, "seeds": len(seeds)}
    cte_ms, age_ms, fixed_ms, reached, mismatches, timeouts = [], [], [], [], 0, {"cte": 0, "age": 0, "age_fixed": 0}
    first_age = None
    for i, seed in enumerate(seeds):
        ms, rows = timed(cur, CTE.format(step=STEP[direction]), {"seeds": [f"kgn_{seed}"], "depth": depth})
        cte_count = rows[0][0] if rows else None
        if ms is None:
            timeouts["cte"] += 1
        else:
            cte_ms.append(ms)
            reached.append(cte_count)
        ms, rows = timed(cur, f"SELECT count(*) FROM cypher('{GRAPH}', $$ {age_fixed(direction, seed, depth)} $$) AS (v agtype)")
        if ms is None:
            timeouts["age_fixed"] += 1
        else:
            fixed_ms.append(ms)
            if cte_count is not None and rows[0][0] != cte_count:
                mismatches += 1
        cypher = AGE[direction].format(seed=seed, depth=depth)
        ms, rows = timed(cur, f"SELECT count(*) FROM cypher('{GRAPH}', $$ {cypher} $$) AS (v agtype)")
        if ms is None:
            timeouts["age"] += 1
            continue
        if i == 0:
            first_age = round(ms, 1)     # AGE builds its in-memory graph context on first VLE use
        else:
            age_ms.append(ms)
        if cte_count is not None and rows[0][0] != cte_count:
            mismatches += 1
    for name, values in (("cte", cte_ms), ("age_fixed", fixed_ms), ("age", age_ms)):
        out[name] = {"p50_ms": pct(values, 0.5), "p95_ms": pct(values, 0.95),
                     "mean_ms": round(statistics.fmean(values), 1)} if values else None
    out["age_first_call_ms"] = first_age
    out["reached_mean"] = round(statistics.fmean(reached)) if reached else None
    out["reached_max"] = max(reached) if reached else None
    out["timeouts"] = timeouts
    out["answer_mismatches"] = mismatches
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", required=True)
    p.add_argument("--nodes", type=int, default=1_000_000)
    p.add_argument("--out-degree", type=int, default=5)
    p.add_argument("--skew", type=float, default=3.0, help="dst = N * random()^skew (higher: heavier hubs)")
    p.add_argument("--seeds", type=int, default=30)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--skip-build", action="store_true")
    args = p.parse_args()
    with psycopg.connect(args.dsn) as conn:
        report = {"build": None if args.skip_build else build(conn, args.nodes, args.out_degree, args.skew)}
        cur = conn.cursor()
        cur.execute("SELECT version()")
        report["server"] = cur.fetchone()[0].split(" on ")[0]
        results = []
        for direction, depths in (("out", (2, 3, 4)), ("in", (2, 3))):
            seeds = seeds_for(conn, direction, args.seeds)
            for depth in depths:
                # A fresh session per series, so AGE's first-call graph load is measured each time.
                with psycopg.connect(args.dsn) as fresh:
                    results.append(run(fresh, direction, depth, seeds, args.timeout))
                print(json.dumps(results[-1]), flush=True)
        report["results"] = results
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
