"""The sqlite setup stub names every column the real GitHub tables have.

Setup reads `db/schema.sql` on Postgres. The tests that exercise that view
(`test_first_run_setup`, `test_one_status_from_the_last_proof`,
`test_a_saved_connection_survives_a_refresh`) build `CREATE_GITHUB_TABLES` instead.
A column added to the real table and missing from the stub is an OperationalError in
those tests, not a product failure (BOSSAPP-970, BOSSAPP-1028).
"""
from __future__ import annotations

import re
from pathlib import Path

from .conftest import CREATE_GITHUB_TABLES

ROOT = Path(__file__).resolve().parents[2]
TABLES = ("github_connections", "github_repository_bindings")
_SKIP = re.compile(r"^(UNIQUE|CHECK|PRIMARY|CONSTRAINT|FOREIGN|EXCLUDE)\b", re.I)
_ALTER = re.compile(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", re.I)


def _create_columns(sql: str, table: str) -> set[str]:
    start = sql.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    body = sql[start:sql.index("\n);", start)]
    names: set[str] = set()
    for raw in body.splitlines()[1:]:
        line = raw.split("--", 1)[0].strip().rstrip(",")
        if not line or _SKIP.match(line):
            continue
        names.add(line.split()[0].strip('"'))
    return names


def _alter_columns(sql: str, table: str) -> set[str]:
    found: set[str] = set()
    for raw in sql.splitlines():
        code = raw.split("--", 1)[0]
        match = _ALTER.search(code)
        if match and match.group(1) == table:
            found.add(match.group(2))
    return found


def _stub_columns(table: str) -> set[str]:
    statement = next(s for s in CREATE_GITHUB_TABLES if f"CREATE TABLE {table} " in s)
    body = statement[statement.index("(") + 1:statement.rindex(")")]
    return {part.strip().split()[0] for part in body.split(",") if part.strip()}


def test_the_setup_stub_names_every_github_column():
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    for table in TABLES:
        real = _create_columns(sql, table) | _alter_columns(sql, table)
        missing = sorted(real - _stub_columns(table))
        assert real, f"{table}: the parser saw no columns in db/schema.sql"
        assert not missing, f"{table} stub is missing {missing}"
    bindings = _create_columns(sql, "github_repository_bindings") | _alter_columns(
        sql, "github_repository_bindings")
    assert {"host", "role_hint", "registry_repo_id"} <= bindings
