"""Dump one Alpine row, restore it into boss-postgres-kg:16, and check extensions.

Creates and removes only the containers boss-kg-upgrade-src and
boss-kg-upgrade-dst. Does not change compose. Prints one JSON object.
"""
from __future__ import annotations

import json
import subprocess
import time


SRC = "boss-kg-upgrade-src"
DST = "boss-kg-upgrade-dst"
IMAGE = "boss-postgres-kg:16"


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def _wait(name: str) -> None:
    for _ in range(40):
        probe = _run(["docker", "exec", name, "pg_isready", "-U", "postgres"], check=False)
        if probe.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"{name} did not become ready")


def _psql(name: str, sql: str, database: str = "boss_kg_test") -> str:
    result = _run(
        ["docker", "exec", name, "psql", "-U", "postgres", "-d", database, "-tA", "-c", sql]
    )
    return result.stdout.strip()


def main() -> None:
    _run(["docker", "rm", "-f", SRC, DST], check=False)
    _run(
        [
            "docker", "run", "-d", "--name", SRC,
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
            "postgres:16-alpine",
        ]
    )
    try:
        _wait(SRC)
        _psql(SRC, "CREATE DATABASE boss_kg_test", database="postgres")
        _psql(SRC, "CREATE TABLE probe(id int primary key, note text)")
        _psql(SRC, "INSERT INTO probe(id, note) VALUES (1, 'alpine-row')")
        _run(["docker", "exec", SRC, "pg_dump", "-U", "postgres", "-d", "boss_kg_test", "-Fc", "-f", "/tmp/src.dump"])
        _run(["docker", "cp", f"{SRC}:/tmp/src.dump", "/tmp/boss-kg-upgrade.dump"])
        _run(
            [
                "docker", "run", "-d", "--name", DST,
                "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                "-p", "55474:5432",
                IMAGE,
            ]
        )
        _wait(DST)
        preload = _psql(DST, "SHOW shared_preload_libraries", database="postgres")
        _run(["docker", "cp", "/tmp/boss-kg-upgrade.dump", f"{DST}:/tmp/src.dump"])
        _psql(DST, "CREATE DATABASE boss_kg_test", database="postgres")
        _run(
            ["docker", "exec", DST, "pg_restore", "-U", "postgres", "-d", "boss_kg_test", "--no-owner", "/tmp/src.dump"],
            check=False,
        )
        note = _psql(DST, "SELECT note FROM probe WHERE id = 1")
        vector = _psql(DST, "CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        age = _psql(DST, "CREATE EXTENSION IF NOT EXISTS age; SELECT extversion FROM pg_extension WHERE extname = 'age'")
        print(
            json.dumps(
                {
                    "source_image": "postgres:16-alpine",
                    "target_image": IMAGE,
                    "method": "dump_restore",
                    "in_place_datadir_copy": False,
                    "row": note,
                    "shared_preload_libraries": preload,
                    "vector_extension": vector.splitlines()[-1] if vector else "",
                    "age_extension": age.splitlines()[-1] if age else "",
                },
                indent=2,
            )
        )
        if note != "alpine-row":
            raise SystemExit("restored row did not match")
        if "age" not in preload:
            raise SystemExit(f"age is not preloaded: {preload}")
    finally:
        _run(["docker", "rm", "-f", SRC], check=False)


if __name__ == "__main__":
    main()
