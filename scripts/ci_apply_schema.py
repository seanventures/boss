#!/usr/bin/env python3
"""Apply BOSS's schema and first-boot seed (boss_engine.bootstrap.seed) to a test database.

    BOSS_TEST_PG_URL=postgresql://user:pass@host:5432 python scripts/ci_apply_schema.py boss_test

The engine suite runs against a database that already has the schema (on a laptop: the seeded
boss_test). CI starts from an empty Postgres, so its p0 job creates the database and applies the
schema first. Refuses any database not named boss_test*.
"""
from __future__ import annotations

import os
import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "engine"), str(ROOT)]


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "boss_test"
    if not re.fullmatch(r"boss_test\w*", name):
        print(f"ci_apply_schema: refusing {name!r}: only a boss_test* database", file=sys.stderr)
        return 2
    base = os.environ.get("BOSS_TEST_PG_URL", "")
    if not base:
        print("ci_apply_schema: set BOSS_TEST_PG_URL=postgresql://user:pass@host:port", file=sys.stderr)
        return 2
    import psycopg
    from psycopg import sql

    u = urllib.parse.urlsplit(base)
    with psycopg.connect(f"postgresql://{u.netloc}/postgres", autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    # The install's own first boot: the schema, then the default org, workspace, project and
    # their defaults (boss_engine.bootstrap.seed). Engine tests that borrow "a workspace" from the
    # database (test_a_deploy_waits_for_its_merge) need the seeded rows, as the laptop's
    # boss_test has them.
    import tempfile

    os.environ["DATABASE_URL"] = f"postgresql+psycopg://{u.netloc}/{name}"
    os.environ.setdefault("BOSS_ARTIFACTS_DIR", tempfile.mkdtemp(prefix="boss-ci-artifacts-"))
    os.environ.setdefault("BOSS_AUTHZ_MODE", "shadow")
    os.environ.setdefault("BOSS_IDENTITY_SERVER_KEY",
                          "dGVzdC1vbmx5LWlkZW50aXR5LXNlcnZlci1rZXktLS0tLS0tLS0tLS0tLS0=")
    from boss_engine.bootstrap import seed

    seed()
    print(f"ci_apply_schema: {name} has the schema and the install's seed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
