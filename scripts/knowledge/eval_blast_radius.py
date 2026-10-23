#!/usr/bin/env python3
"""Score the code graph's depth-1 blast radius against a blind answer key.

    eval_blast_radius.py --dsn postgresql+psycopg://... --root ../saleor \\
        --truth scripts/knowledge/results/blast_radius_saleor_blind_truth.json --out scores.json

The checkout is indexed with ``index_repository`` into a throwaway project inside one transaction
that is rolled back, so the database keeps nothing. A symbol is correct when the files that
depend on it directly (``KnowledgeService.dependent_files``) match the hand-checked set with
precision and recall of at least 0.9; an empty hand-checked set is correct only when the tool
also returns nothing. The answer keys were written before the tool was run (PRs #11-#14).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine"))

from boss_engine.db import ensure_schema
from boss_engine.services.knowledge.code_index import index_repository, symbol_key
from boss_engine.services.knowledge.service import KnowledgeService
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from sqlalchemy import create_engine, text

PROJECT = "kg_eval_blast"


def score(got: set[str], want: set[str]) -> tuple[float, float, bool]:
    if not want:
        return (1.0 if not got else 0.0), 1.0, not got
    tp = len(got & want)
    precision = tp / len(got) if got else 0.0
    recall = tp / len(want)
    return precision, recall, precision >= 0.9 and recall >= 0.9


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    name = args.dsn.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not any(marker in name for marker in ("test", "scratch", "tmp")):
        raise SystemExit("--dsn must name a disposable database")
    truth = json.loads(args.truth.read_text())
    engine = create_engine(args.dsn, future=True)
    ensure_schema(engine)
    store = PostgresKnowledgeStore()
    service = KnowledgeService(store)
    rows, tp_all, got_all, want_all = [], 0, 0, 0
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("INSERT INTO orgs (id, name) VALUES ('org_kg_eval', 'eval')"))
            connection.execute(text(
                "INSERT INTO workspaces (id, org_id, name) VALUES ('ws_kg_eval', 'org_kg_eval', 'eval')"))
            connection.execute(text(
                "INSERT INTO projects (id, workspace_id, name, slug) "
                "VALUES (:p, 'ws_kg_eval', 'eval', 'kg-eval')"), {"p": PROJECT})
            started = time.monotonic()
            run = index_repository(connection, store, project_id=PROJECT, root=args.root)
            run["seconds"] = round(time.monotonic() - started, 1)
            for key, want in truth["symbols"].items():
                path, qual = key.split("::", 1)
                node = store.find_by_key(connection, project_id=PROJECT, kind="symbol",
                                         node_key=symbol_key(None, path, qual))
                got = set(service.dependent_files(connection, project_id=PROJECT, node_id=node["id"])) \
                    if node else set()
                precision, recall, ok = score(got, set(want))
                tp_all += len(got & set(want))
                got_all += len(got)
                want_all += len(want)
                rows.append({"symbol": key, "found": node is not None, "correct": ok,
                             "precision": round(precision, 3), "recall": round(recall, 3),
                             "missed": sorted(set(want) - got), "extra": sorted(got - set(want))})
        finally:
            transaction.rollback()
    correct = sum(r["correct"] for r in rows)
    report = {"repo": truth.get("repo"), "commit": truth.get("commit"), "symbols": len(rows),
              "correct": correct, "accuracy": round(correct / len(rows), 3),
              "micro_precision": round(tp_all / got_all, 3) if got_all else None,
              "micro_recall": round(tp_all / want_all, 3) if want_all else None,
              "index": {k: run[k] for k in ("files", "parsed", "symbols", "nodes", "edges", "seconds")},
              "rows": rows}
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    for row in rows:
        if not row["correct"]:
            print("MISS", row["symbol"], "missed", row["missed"], "extra", row["extra"])
    print(json.dumps({k: report[k] for k in ("symbols", "correct", "accuracy", "micro_precision",
                                              "micro_recall", "index")}))


if __name__ == "__main__":
    main()
