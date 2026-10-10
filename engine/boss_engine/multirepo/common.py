"""Small helpers every multirepo module shares."""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text


class MultiRepoError(Exception):
    """A refusal with a status, a machine code and a sentence a person can act on."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


def sql(statement: str):
    return text(statement)


def jdump(value: Any) -> str:
    return json.dumps(value, default=str)


def jload(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def org_of_project(db: Any, project_id: str) -> Optional[str]:
    row = db.execute(sql("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                         "WHERE p.id = :p"), {"p": project_id}).scalar()
    return str(row) if row else None


def iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def norm_repo(full_name: str) -> str:
    return str(full_name or "").strip().strip("/").lower()


def event(db: Any, project_id: str, event_type: str, actor: str, entity_type: str, entity_id: str,
          data: dict[str, Any]) -> None:
    """Best effort, in a savepoint: the record of an act never undoes the act."""
    from ..services.core import emit_event

    try:
        with db.begin_nested():
            emit_event(db, project_id, event_type, actor or "operator", entity_type, entity_id, data)
    except Exception:  # noqa: BLE001
        pass
