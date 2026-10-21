#!/usr/bin/env python3
"""Knowledge Graph Phase 1 acceptance: index a repository and time it (spec §6, Phase 1).

    measure_code_index.py --dsn postgresql+psycopg://USER:PASS@127.0.0.1:5432/kgp1 \\
        --repo-dir ../saleor --repo saleor/saleor --base HEAD~10 --head HEAD --out results.json

1. Full index at --base (empty graph: every file parsed).
2. Incremental update for each commit from --base to --head, one at a time, as BOSS does after
   each push.
3. Optionally (--blast-radius FILE), score kg_blast_radius against a hand-checked list.

The scratch database gets only the knowledge tables from db/schema.sql, plus a `projects` row.
The working tree is checked out commit by commit (detached) and put back afterwards.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "engine"))

from sqlalchemy import create_engine, text  # noqa: E402

from boss_engine.services.knowledge import Scope  # noqa: E402
from boss_engine.services.knowledge.code_index import CodeIndexer  # noqa: E402

PROJECT = "kg-measure"


def knowledge_ddl() -> str:
    schema = (REPO_ROOT / "db/schema.sql").read_text()
    start = schema.index("CREATE TABLE IF NOT EXISTS kg_nodes (")
    end = schema.index("CREATE TABLE IF NOT EXISTS kg_eval_snapshots (")
    return ("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now());\n"
            + schema[start:end])


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def prepare(engine, reset: bool) -> None:
    with engine.begin() as c:
        if reset:
            c.execute(text("DROP TABLE IF EXISTS kg_code_index_state, kg_code_files, kg_edges, kg_nodes, projects CASCADE"))
        for stmt in knowledge_ddl().split(";\n"):
            if stmt.strip():
                c.execute(text(stmt))
        c.execute(text("INSERT INTO projects (id) VALUES (:p) ON CONFLICT DO NOTHING"), {"p": PROJECT})


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", required=True)
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--head", default="HEAD")
    p.add_argument("--workers", type=int)
    p.add_argument("--out")
    args = p.parse_args()
    root = Path(args.repo_dir).resolve()
    engine = create_engine(args.dsn, future=True)
    prepare(engine, reset=True)
    indexer, scope = CodeIndexer(workers=args.workers), Scope(PROJECT)
    original = git(root, "rev-parse", "HEAD")
    head = git(root, "rev-parse", args.head)
    commits = git(root, "rev-list", "--reverse", "--first-parent", f"{args.base}..{head}").split()
    report: dict = {"repo": args.repo, "base": git(root, "rev-parse", args.base), "head": head}
    try:
        git(root, "checkout", "-q", "--detach", report["base"])
        t = time.monotonic()
        with engine.begin() as c:
            report["full"] = indexer.index_full(c, scope, args.repo, root, report["base"])
        report["full"]["wall_seconds"] = round(time.monotonic() - t, 1)
        print(json.dumps(report["full"]), flush=True)
        report["incremental"] = []
        for sha in commits:
            git(root, "checkout", "-q", "--detach", sha)
            t = time.monotonic()
            with engine.begin() as c:
                run = indexer.index_incremental(c, scope, args.repo, root, sha)
            run["wall_seconds"] = round(time.monotonic() - t, 2)
            run["subject"] = git(root, "log", "-1", "--format=%s", sha)[:80]
            report["incremental"].append(run)
            print(json.dumps({k: run[k] for k in ("commit", "changed", "deleted", "reresolved", "edges",
                                                  "wall_seconds")}), flush=True)
    finally:
        git(root, "checkout", "-q", "--detach", original)
    with engine.begin() as c:
        report["graph"] = dict(c.execute(text(
            "SELECT (SELECT count(*) FROM kg_nodes WHERE valid_to IS NULL) AS current_nodes, "
            "(SELECT count(*) FROM kg_edges WHERE valid_to IS NULL) AS current_edges, "
            "(SELECT count(*) FROM kg_nodes WHERE valid_to IS NOT NULL) AS closed_nodes, "
            "(SELECT count(*) FROM kg_edges WHERE valid_to IS NOT NULL) AS closed_edges")).mappings().one())
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report["graph"]))


if __name__ == "__main__":
    main()
