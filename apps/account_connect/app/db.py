from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from .config import Settings


def get_engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url, pool_pre_ping=True)


@contextmanager
def begin(engine: Engine):
    with engine.begin() as connection:
        yield connection


def _split_sql_statements(sql: str) -> list[str]:
    """Split a schema script into individual statements (no nested dollar-quotes needed)."""
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buffer).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            buffer = []
    trailing = "\n".join(buffer).strip().rstrip(";").strip()
    if trailing:
        statements.append(trailing)
    return statements


def ensure_schema(connection: Connection, schema_path: str) -> None:
    sql = Path(schema_path).read_text(encoding="utf-8")
    for statement in _split_sql_statements(sql):
        connection.execute(text(statement))


def ensure_seed_org(connection: Connection, org_id: str, org_name: str) -> None:
    connection.execute(
        text(
            """
            INSERT INTO ac_orgs (id, name, status)
            VALUES (:id, :name, 'active')
            ON CONFLICT (id) DO UPDATE
            SET name = EXCLUDED.name,
                updated_at = now()
            """
        ),
        {"id": org_id, "name": org_name},
    )
