from __future__ import annotations

from contextlib import contextmanager
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings, get_settings


def get_engine(settings: Optional[Settings] = None) -> Engine:
    resolved = settings or get_settings()
    return create_engine(resolved.database_url, future=True, pool_pre_ping=True)


def database_is_reachable(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False


def ensure_schema(engine: Engine, settings: Optional[Settings] = None) -> None:
    resolved = settings or get_settings()
    sql = resolved.schema_path.read_text(encoding="utf-8")
    raw_connection = engine.raw_connection()
    try:
        with raw_connection.cursor() as cursor:
            cursor.execute(sql)
        raw_connection.commit()
    finally:
        raw_connection.close()


@contextmanager
def begin(engine: Optional[Engine] = None):
    active_engine = engine or get_engine()
    with active_engine.begin() as connection:
        yield connection
