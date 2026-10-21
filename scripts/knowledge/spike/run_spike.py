#!/usr/bin/env python3
"""Run the Graphiti spike: option A (Graphiti + gateway clients) vs option B (native), same corpus.

    GRAPHITI_TELEMETRY_ENABLED=false python run_spike.py \\
        --pg postgresql+psycopg://boss:boss@127.0.0.1:55432/kgspike \\
        --neo4j bolt://127.0.0.1:57687 --neo4j-password <throwaway> --out spike_results.json

B writes through BOSS's PostgresKnowledgeStore into a scratch database (kg tables only). A ingests
into a throwaway Neo4j (Graphiti has no Postgres driver). Both use the metered scripted oracle
(oracle.py), so no model is called and nothing is spent. Prices are per 1M tokens, from
boss_engine/services/openai_adapter.py PRICE_PER_MTOK.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "engine"))

import corpus  # noqa: E402
import oracle  # noqa: E402

PRICES = {  # USD per 1M tokens (input, output); openai_adapter.PRICE_PER_MTOK
    "claude-sonnet-5": (2.00, 10.00),
    "qwen3-32b": (0.29, 0.59),
}
EMBED_PRICE = 0.02  # USD per 1M tokens, a small hosted embedding model (assumption, stated in the report)


def kg_ddl() -> str:
    schema = (REPO / "db/schema.sql").read_text()
    start = schema.index("CREATE TABLE IF NOT EXISTS kg_nodes (")
    end = schema.index("CREATE TABLE IF NOT EXISTS kg_eval_snapshots (")
    return "CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now());\n" + schema[start:end]


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_+<=-]+", text.lower()))


def score(statements: list[str]) -> dict:
    """Match extracted statements to labels by token overlap (Jaccard >= 0.6)."""
    labels = {f.id: words(f.statement) for f in corpus.FACTS}
    matched, hits = set(), 0
    for s in statements:
        w = words(s)
        best = max(labels, key=lambda k: len(w & labels[k]) / max(1, len(w | labels[k])))
        if len(w & labels[best]) / max(1, len(w | labels[best])) >= 0.6:
            hits += 1
            matched.add(best)
    return {"extracted": len(statements), "matching_a_label": hits, "labels_recovered": len(matched),
            "precision_vs_labels": round(hits / len(statements), 3) if statements else None,
            "recall_vs_labels": round(len(matched) / len(corpus.FACTS), 3)}


def costs(meter: oracle.Meter, episodes: int) -> dict:
    out = {"calls_per_episode": round(meter.calls / episodes, 2),
           "input_tokens_per_episode": round(meter.input_tokens / episodes),
           "output_tokens_per_episode": round(meter.output_tokens / episodes),
           "embedding_inputs_per_episode": round(meter.embed_inputs / episodes, 2),
           "calls_by_purpose": meter.by_purpose}
    embed = meter.embed_tokens * EMBED_PRICE / 1e6
    for model, (pin, pout) in PRICES.items():
        total = (meter.input_tokens * pin + meter.output_tokens * pout) / 1e6 + embed
        out[f"usd_per_episode_{model}"] = round(total / episodes, 5)
        out[f"usd_per_1000_episodes_{model}"] = round(total / episodes * 1000, 2)
    return out


def temporal(closed: dict[str, bool]) -> dict:
    """The four labelled supersessions: old closed, new current."""
    pairs = [(f.id, f.supersedes) for f in corpus.FACTS if f.supersedes]
    ok = sum(1 for new, old in pairs
             if closed.get(corpus.FACT_BY_ID[old].statement) is True
             and closed.get(corpus.FACT_BY_ID[new].statement) is False)
    return {"supersessions_handled": ok, "of": len(pairs)}


def run_b(episodes, pg: str, latency: tuple[float, float]) -> dict:
    from sqlalchemy import create_engine, text

    import option_b
    from boss_engine.services.knowledge import PostgresKnowledgeStore, Scope

    engine = create_engine(pg, future=True)
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS kg_edges, kg_nodes, projects CASCADE"))
        for stmt in kg_ddl().split(";"):
            if stmt.strip():
                c.execute(text(stmt))
        c.execute(text("INSERT INTO projects (id) VALUES ('northpeak')"))
    store, scope = PostgresKnowledgeStore(), Scope("northpeak")
    meter = oracle.Meter()
    o = oracle.ScriptedOracle(meter, base_latency_s=latency[0], per_output_token_s=latency[1])
    walls, totals = [], {"new": 0, "duplicate": 0, "superseded": 0}
    for ep in episodes:
        o.episode = ep
        t = time.perf_counter()
        facts = option_b.extract(o, ep.body)
        with engine.begin() as c:
            for k, v in option_b.store_facts(store, c, scope, facts, episode_id=ep.id,
                                             reference_time=ep.reference_time).items():
                totals[k] += v
        walls.append(time.perf_counter() - t)
    with engine.begin() as c:
        rows = option_b.current_facts(c, scope)
    closed = {r["statement"]: r["valid_to"] is not None for r in rows}
    return {"meter": meter, "walls": walls, "writes": totals, "closed": closed,
            "statements": [r["statement"] for r in rows]}


def run_a(episodes, uri: str, password: str, latency: tuple[float, float]) -> dict:
    import option_a

    logging.disable(logging.WARNING)
    meter = oracle.Meter()
    o = oracle.ScriptedOracle(meter, base_latency_s=latency[0], per_output_token_s=latency[1])
    walls: list[float] = []

    class Timed(list):
        pass

    async def go():
        result = None
        t0 = time.perf_counter()
        result = await option_a.ingest(episodes, o, meter, uri, password)
        return result, time.perf_counter() - t0

    # Per-episode wall time comes from ingest's own loop; measure it by wrapping add_episode.
    from graphiti_core import Graphiti
    original = Graphiti.add_episode

    async def timed_add(self, *a, **kw):
        t = time.perf_counter()
        try:
            return await original(self, *a, **kw)
        finally:
            walls.append(time.perf_counter() - t)

    Graphiti.add_episode = timed_add
    try:
        result, _ = asyncio.run(go())
    finally:
        Graphiti.add_episode = original
    closed = {r["fact"]: bool(r["closed"]) for r in result["facts"]}
    return {"meter": meter, "walls": walls, "closed": closed, "statements": [r["fact"] for r in result["facts"]]}


def summarize(name: str, run: dict, n: int) -> dict:
    walls = sorted(run["walls"])
    return {"option": name, **costs(run["meter"], n),
            "latency_s_per_episode": {"mean": round(statistics.fmean(walls), 3),
                                      "p95": round(walls[int(0.95 * (len(walls) - 1))], 3)},
            "harness_check_vs_labels": score(run["statements"]),
            "temporal": temporal(run["closed"]),
            "precision": "PENDING: needs a governed run with a real model (the oracle is right by construction)"}


def code_size() -> dict:
    def loc(path: Path) -> int:
        return sum(1 for line in path.read_text().splitlines() if line.strip() and not line.strip().startswith("#"))

    return {"option_b_native_lines": loc(HERE / "option_b.py"),
            "option_a_glue_lines": loc(HERE / "option_a.py")}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pg", required=True)
    p.add_argument("--neo4j", required=True)
    p.add_argument("--neo4j-password", required=True)
    p.add_argument("--episodes", type=int, default=200)
    p.add_argument("--latency", type=float, nargs=2, default=(0.25, 0.005),
                   help="simulated model latency: base seconds, seconds per output token")
    p.add_argument("--out", default="spike_results.json")
    args = p.parse_args()
    episodes = corpus.episodes(args.episodes)
    latency = tuple(args.latency)
    report = {"episodes": len(episodes), "labelled_facts": len(corpus.FACTS),
              "simulated_latency": {"base_s": latency[0], "per_output_token_s": latency[1]},
              "prices_per_mtok": PRICES, "embedding_price_per_mtok": EMBED_PRICE}
    b = run_b(episodes, args.pg, latency)
    report["B"] = {**summarize("B native", b, len(episodes)), "writes": b["writes"]}
    print(json.dumps(report["B"], indent=1), flush=True)
    a = run_a(episodes, args.neo4j, args.neo4j_password, latency)
    report["A"] = summarize("A graphiti", a, len(episodes))
    print(json.dumps(report["A"], indent=1), flush=True)
    report["code"] = code_size()
    Path(args.out).write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
