"""Measure retrieval p95 on a disposable database and print JSON.

The database name must contain test. The default sample is well below the
10M-node scale target. The printed scale_target_met flag is computed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

from sqlalchemy import create_engine, text  # noqa: E402

from boss_engine.db import ensure_schema  # noqa: E402
from boss_engine.services.knowledge.bench import measure_retrieval  # noqa: E402
from boss_engine.services.knowledge.store import PostgresKnowledgeStore  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, default=20000)
    parser.add_argument("--edges", type=int, default=20000)
    parser.add_argument("--queries", type=int, default=40)
    args = parser.parse_args()
    url = os.environ.get("BOSS_KG_TEST_DATABASE_URL") or ""
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if "test" not in name:
        raise SystemExit("BOSS_KG_TEST_DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    ensure_schema(engine)
    store = PostgresKnowledgeStore()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO orgs (id, name) VALUES ('org_bench', 'Bench') ON CONFLICT (id) DO NOTHING"))
        connection.execute(
            text(
                "INSERT INTO workspaces (id, org_id, name) VALUES ('ws_bench', 'org_bench', 'Bench') "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        connection.execute(
            text(
                "INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                "('proj_bench', 'ws_bench', 'Bench', 'bench') ON CONFLICT (id) DO NOTHING"
            )
        )
        measured = measure_retrieval(
            connection,
            store,
            project_id="proj_bench",
            nodes=args.nodes,
            edges=args.edges,
            queries=args.queries,
        )
    print(json.dumps(measured, indent=2))


if __name__ == "__main__":
    main()
