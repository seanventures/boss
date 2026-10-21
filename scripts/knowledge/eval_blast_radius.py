#!/usr/bin/env python3
"""Score kg_blast_radius (depth 1) against the hand-checked list (Phase 1 acceptance: >= 90%).

    eval_blast_radius.py --dsn ... --project kg-measure --truth results/blast_radius_saleor_truth.json

A symbol is correct when the tool's dependent files match the hand-checked set with precision >= 0.9
and recall >= 0.9; an empty hand-checked set is correct only when the tool also returns nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine"))

from sqlalchemy import create_engine  # noqa: E402

from boss_engine.services.knowledge import Scope  # noqa: E402
from boss_engine.services.knowledge.code_queries import KnowledgeQueries  # noqa: E402


def score(got: set[str], want: set[str]) -> tuple[float, float, bool]:
    if not want:
        return (1.0 if not got else 0.0), 1.0, not got
    tp = len(got & want)
    precision = tp / len(got) if got else 0.0
    recall = tp / len(want)
    return precision, recall, precision >= 0.9 and recall >= 0.9


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dsn", required=True)
    p.add_argument("--project", default="kg-measure")
    p.add_argument("--truth", required=True)
    p.add_argument("--out")
    args = p.parse_args()
    truth = json.loads(Path(args.truth).read_text())
    q, scope = KnowledgeQueries(), Scope(args.project)
    rows, tp_all, got_all, want_all = [], 0, 0, 0
    with create_engine(args.dsn, future=True).begin() as c:
        for key, want in truth["symbols"].items():
            got = q.direct_dependent_files(c, scope, f"{truth['repo']}:{key}")
            precision, recall, ok = score(got, set(want))
            tp_all += len(got & set(want))
            got_all += len(got)
            want_all += len(want)
            rows.append({"symbol": key, "correct": ok, "precision": round(precision, 3), "recall": round(recall, 3),
                         "missed": sorted(set(want) - got), "extra": sorted(got - set(want))})
    correct = sum(r["correct"] for r in rows)
    report = {"symbols": len(rows), "correct": correct, "accuracy": round(correct / len(rows), 3),
              "micro_precision": round(tp_all / got_all, 3) if got_all else None,
              "micro_recall": round(tp_all / want_all, 3) if want_all else None, "rows": rows}
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    for r in rows:
        if not r["correct"]:
            print("MISS", r["symbol"], "missed", r["missed"], "extra", r["extra"])
    print(json.dumps({k: report[k] for k in ("symbols", "correct", "accuracy", "micro_precision", "micro_recall")}))


if __name__ == "__main__":
    main()
