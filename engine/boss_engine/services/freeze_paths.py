"""A builder freeze on a file or a data table (BOSSAPP-1269).

An agent write to a frozen path is refused and the stored bytes stay as they
were. An unfrozen path can change. File bytes live on disk under the caller's
root. Table bytes live in freeze_table_bytes. The freeze itself is a row in
freeze_paths, one per project, kind and path.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Union

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import make_id

KINDS = ("file", "table")
Bytes = Union[bytes, bytearray]


class FreezeError(ValueError):
    """A path or kind the caller can fix."""

    status = 422


class FrozenPath(FreezeError):
    """The write was refused. Nothing was written."""

    status = 409


def _project(project_id: str) -> str:
    value = str(project_id or "").strip()
    if not value or any(mark in value for mark in ("/", "\\", "..", "\0")):
        raise FreezeError("a project id is required")
    return value


def _kind(kind: str) -> str:
    value = str(kind or "").strip().lower()
    if value not in KINDS:
        raise FreezeError("kind is file or table")
    return value


def normalize_path(path: str) -> str:
    """A relative path. `..`, an absolute path and an empty path are refused."""
    raw = str(path or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or "\0" in raw:
        raise FreezeError("a path is a relative file or table name")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise FreezeError("a path cannot climb out of the workspace")
        parts.append(part)
    if not parts or len(parts) > 64:
        raise FreezeError("a path is a relative file or table name")
    return "/".join(parts)


def _as_bytes(content: Union[Bytes, str]) -> bytes:
    if isinstance(content, str):
        return content.encode("utf-8")
    return bytes(content)


def _target(root: Path, path: str) -> Path:
    base = Path(root).resolve()
    target = (base / path).resolve()
    if not target.is_relative_to(base):
        raise FreezeError("a path cannot climb out of the workspace")
    return target


def is_frozen(connection: Connection, project_id: str, kind: str, path: str) -> bool:
    project_id = _project(project_id)
    kind = _kind(kind)
    path = normalize_path(path)
    row = connection.execute(text(
        "SELECT 1 FROM freeze_paths"
        " WHERE project_id = :project_id AND kind = :kind AND path = :path"
    ), {"project_id": project_id, "kind": kind, "path": path}).first()
    return row is not None


def freeze(connection: Connection, project_id: str, kind: str, path: str, *,
           actor: str = "builder") -> dict[str, Any]:
    project_id = _project(project_id)
    kind = _kind(kind)
    path = normalize_path(path)
    who = str(actor or "builder").strip() or "builder"
    connection.execute(text(
        "INSERT INTO freeze_paths (id, project_id, kind, path, frozen_by)"
        " VALUES (:id, :project_id, :kind, :path, :actor)"
        " ON CONFLICT (project_id, kind, path) DO NOTHING"
    ), {
        "id": make_id("frz"),
        "project_id": project_id,
        "kind": kind,
        "path": path,
        "actor": who,
    })
    return {"project_id": project_id, "kind": kind, "path": path, "frozen": True}


def thaw(connection: Connection, project_id: str, kind: str, path: str) -> dict[str, Any]:
    project_id = _project(project_id)
    kind = _kind(kind)
    path = normalize_path(path)
    connection.execute(text(
        "DELETE FROM freeze_paths"
        " WHERE project_id = :project_id AND kind = :kind AND path = :path"
    ), {"project_id": project_id, "kind": kind, "path": path})
    return {"project_id": project_id, "kind": kind, "path": path, "frozen": False}


def list_freezes(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    project_id = _project(project_id)
    rows = connection.execute(text(
        "SELECT kind, path, frozen_by FROM freeze_paths"
        " WHERE project_id = :project_id ORDER BY kind, path"
    ), {"project_id": project_id}).mappings().all()
    return [
        {"kind": row["kind"], "path": row["path"], "frozen": True, "frozen_by": row["frozen_by"]}
        for row in rows
    ]


def _refuse_if_frozen(connection: Connection, project_id: str, kind: str, path: str) -> None:
    if is_frozen(connection, project_id, kind, path):
        raise FrozenPath(
            f"{path} is frozen. The write was refused and the bytes stayed."
        )


def read_table(connection: Connection, project_id: str, path: str) -> Optional[bytes]:
    project_id = _project(project_id)
    path = normalize_path(path)
    row = connection.execute(text(
        "SELECT body FROM freeze_table_bytes"
        " WHERE project_id = :project_id AND path = :path"
    ), {"project_id": project_id, "path": path}).first()
    if row is None or row[0] is None:
        return None
    return bytes(row[0])


def write_table(connection: Connection, project_id: str, path: str,
                content: Union[Bytes, str]) -> dict[str, Any]:
    """Replace a data table's bytes, unless that table path is frozen."""
    project_id = _project(project_id)
    path = normalize_path(path)
    _refuse_if_frozen(connection, project_id, "table", path)
    data = _as_bytes(content)
    connection.execute(text(
        "INSERT INTO freeze_table_bytes (project_id, path, body)"
        " VALUES (:project_id, :path, :body)"
        " ON CONFLICT (project_id, path) DO UPDATE"
        " SET body = EXCLUDED.body, updated_at = now()"
    ), {"project_id": project_id, "path": path, "body": data})
    return {"project_id": project_id, "kind": "table", "path": path,
            "changed": True, "bytes": len(data)}


def write_file(connection: Connection, project_id: str, root: Path, path: str,
               content: Union[Bytes, str]) -> dict[str, Any]:
    """Write file bytes under `root`, unless that file path is frozen.

    A frozen path is refused before the file is opened, so the bytes on disk
    stay as they were. A missing frozen file is not created.
    """
    project_id = _project(project_id)
    path = normalize_path(path)
    _refuse_if_frozen(connection, project_id, "file", path)
    data = _as_bytes(content)
    target = _target(Path(root), path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"project_id": project_id, "kind": "file", "path": path,
            "changed": True, "bytes": len(data)}


def file_root(project_id: str, *, base: Optional[os.PathLike[str] | str] = None) -> Path:
    """Where one project's frozen-or-not files live."""
    project_id = _project(project_id)
    if base is None:
        base = os.environ.get("BOSS_ARTIFACTS_DIR")
    if not base:
        from ..config import get_settings
        base = get_settings().artifact_dir
    return Path(base) / "freeze-paths" / project_id
