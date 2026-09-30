from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class PersistencePort(Protocol):
    """Durable engine-state boundary.

    The transaction payload is deliberately opaque to the engine. Enterprise
    composition supplies a Postgres-backed session; the OSS default supplies
    an in-memory session with the same run-state operations.
    """

    def transaction(self, engine: Any = None) -> AbstractContextManager[Any]: ...

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]: ...

    def list_runs(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]: ...

    def save_run(self, run_id: str, state: dict[str, Any]) -> None: ...


def sql(statement: str) -> Any:
    """Create an enterprise SQL statement without importing SQLAlchemy eagerly.

    Legacy pipeline queries are being moved behind high-level persistence
    methods incrementally. Keeping this compatibility marker here prevents
    importing SQLAlchemy when the standalone OSS engine is imported.
    """

    try:
        from sqlalchemy import text
    except ImportError:
        return statement
    return text(statement)
