from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from .config import Settings


def get_engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url, pool_pre_ping=True)


@contextmanager
def begin(engine: Engine):
    with engine.begin() as connection:
        yield connection


def ensure_schema(connection: Connection, schema_path: str) -> None:
    sql = open(schema_path, encoding="utf-8").read()
    connection.execute(text(sql))


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
