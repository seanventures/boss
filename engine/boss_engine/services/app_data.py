"""Records for one built app: tables, filters, import, and a 30-day trash.

Soft-delete sets deleted_at. A row younger than 30 days can be restored. purge_expired
removes only trash older than 30 days, and only from app_data_rows. Live rows and every
other table stay.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from .core import make_id
from .naming import identity_key

TRASH_DAYS = 30


class AppDataError(ValueError):
    """A data-page action that did not happen. The message is safe to show."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now(value: Optional[datetime]) -> datetime:
    return _as_utc(value) if value is not None else datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return _as_utc(value).isoformat()


def _cells(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        import json
        value = json.loads(value)
    return {str(key): "" if item is None else str(item) for key, item in dict(value or {}).items()}


def _app_key(value: str) -> str:
    key = identity_key(value or "")
    if not key:
        raise AppDataError("Name the app.")
    return key


def _project_id(value: str) -> str:
    project = (value or "").strip()
    if not project:
        raise AppDataError("Name the project.")
    return project


def _table_name(value: str) -> str:
    name = " ".join((value or "").split())
    if not name:
        raise AppDataError("Name the table.")
    if len(name) > 60:
        raise AppDataError("Table names stay under 60 characters.")
    return name


def _expired(deleted_at: datetime, now: datetime) -> bool:
    return _as_utc(deleted_at) < _as_utc(now) - timedelta(days=TRASH_DAYS)


def _days_left(deleted_at: datetime, now: datetime) -> int:
    remaining = (_as_utc(deleted_at) + timedelta(days=TRASH_DAYS)) - _as_utc(now)
    if remaining.total_seconds() <= 0:
        return 0
    days = remaining.days
    if remaining.seconds or remaining.microseconds:
        days += 1
    return days


def _public(row: Mapping[str, Any], *, table: Optional[str] = None,
            now: Optional[datetime] = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": row["id"],
        "cells": _cells(row["cells"]),
        "created_at": _iso(row["created_at"]),
        "deleted_at": _iso(row["deleted_at"]),
    }
    if table is not None:
        body["table"] = table
    if now is not None and row["deleted_at"] is not None:
        body["days_left"] = _days_left(row["deleted_at"], now)
        body["restorable"] = not _expired(row["deleted_at"], now)
    return body


def _parse_csv(csv_text: str) -> list[dict[str, str]]:
    raw = (csv_text or "").strip()
    if not raw:
        raise AppDataError("The file has no rows.")
    try:
        reader = csv.DictReader(io.StringIO(raw), restkey="_extra", restval="")
        original = list(reader.fieldnames or [])
        header = [(name or "").strip() for name in original]
    except csv.Error as error:
        raise AppDataError("The file is not a CSV.") from error
    if not header or any(not name for name in header):
        raise AppDataError("The file has no rows.")
    if len(header) != len(set(header)):
        raise AppDataError("The header repeats a column.")
    rows: list[dict[str, str]] = []
    for incoming in reader:
        if incoming.get("_extra"):
            raise AppDataError("A row has more columns than the header.")
        cells = {clean: (incoming.get(raw_name) or "").strip()
                 for clean, raw_name in zip(header, original)}
        if any(cells.values()):
            rows.append(cells)
    if not rows:
        raise AppDataError("The file has no rows.")
    return rows


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _ensure_table(connection: Connection, project_id: str, app_key: str, name: str) -> str:
    found = connection.execute(text(
        "SELECT id FROM app_data_tables "
        "WHERE project_id = :project_id AND app_key = :app_key AND name = :name"),
        {"project_id": project_id, "app_key": app_key, "name": name}).scalar()
    if found:
        return str(found)
    table_id = make_id("adt")
    try:
        with connection.begin_nested():
            connection.execute(text(
                "INSERT INTO app_data_tables (id, project_id, app_key, name) "
                "VALUES (:id, :project_id, :app_key, :name)"),
                {"id": table_id, "project_id": project_id, "app_key": app_key, "name": name})
    except IntegrityError:
        found = connection.execute(text(
            "SELECT id FROM app_data_tables "
            "WHERE project_id = :project_id AND app_key = :app_key AND name = :name"),
            {"project_id": project_id, "app_key": app_key, "name": name}).scalar()
        if found:
            return str(found)
        raise AppDataError("No such project.", 404) from None
    return table_id


def _table_id(connection: Connection, project_id: str, app_key: str, table: str) -> Optional[str]:
    name = _table_name(table)
    found = connection.execute(text(
        "SELECT id FROM app_data_tables "
        "WHERE project_id = :project_id AND app_key = :app_key AND name = :name"),
        {"project_id": project_id, "app_key": app_key, "name": name}).scalar()
    return str(found) if found else None


def import_csv(connection: Connection, *, project_id: str, app_key: str, table: str,
               csv_text: str) -> dict[str, Any]:
    """Add rows. Never deletes a row that is already there."""
    project = _project_id(project_id)
    key = _app_key(app_key)
    name = _table_name(table)
    parsed = _parse_csv(csv_text)
    table_id = _ensure_table(connection, project, key, name)
    stored: list[dict[str, Any]] = []
    for cells in parsed:
        row_id = make_id("adr")
        connection.execute(text(
            "INSERT INTO app_data_rows (id, table_id, project_id, app_key, cells) "
            "VALUES (:id, :table_id, :project_id, :app_key, CAST(:cells AS jsonb))"),
            {"id": row_id, "table_id": table_id, "project_id": project, "app_key": key,
             "cells": json.dumps(cells)})
        stored.append({"id": row_id, "cells": cells, "created_at": None, "deleted_at": None})
    return {"table": name, "imported": len(stored),
            "rows": [_public(row) for row in stored]}


def filter_rows(connection: Connection, *, project_id: str, app_key: str, table: str,
                field: str = "", value: str = "", mode: str = "is") -> list[dict[str, Any]]:
    """Live rows of one table. A missing table is an empty list, not another app's rows."""
    project = _project_id(project_id)
    key = _app_key(app_key)
    table_id = _table_id(connection, project, key, table)
    if table_id is None:
        return []
    match = (mode or "is").strip().lower()
    column = (field or "").strip()
    if match not in ("is", "contains"):
        raise AppDataError("Match is or contains.")
    if not column and value:
        raise AppDataError("Name the field.")
    sql = ("SELECT id, cells, created_at, deleted_at FROM app_data_rows "
           "WHERE table_id = :table_id AND project_id = :project_id AND app_key = :app_key "
           "AND deleted_at IS NULL")
    params: dict[str, Any] = {"table_id": table_id, "project_id": project, "app_key": key}
    if column:
        if match == "is":
            sql += " AND cells ->> :field = :value"
            params["value"] = value
        else:
            sql += " AND cells ->> :field ILIKE :pattern ESCAPE :escape"
            params["pattern"] = _like(value)
            params["escape"] = "\\"
        params["field"] = column
    sql += " ORDER BY created_at, id"
    found = connection.execute(text(sql), params).mappings().all()
    return [_public(row) for row in found]


def _owned(connection: Connection, project_id: str, app_key: str, row_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT id, cells, created_at, deleted_at FROM app_data_rows "
        "WHERE id = :id AND project_id = :project_id AND app_key = :app_key"),
        {"id": row_id, "project_id": project_id, "app_key": app_key}).mappings().first()
    if row is None:
        raise AppDataError("No such row.", 404)
    return dict(row)


def soft_delete(connection: Connection, *, project_id: str, app_key: str, row_id: str,
                now: Optional[datetime] = None) -> dict[str, Any]:
    """Set deleted_at. A second delete leaves the original clock alone."""
    project = _project_id(project_id)
    key = _app_key(app_key)
    moment = _now(now)
    row = _owned(connection, project, key, row_id)
    if row["deleted_at"] is None:
        connection.execute(text(
            "UPDATE app_data_rows SET deleted_at = :at, updated_at = :at WHERE id = :id"),
            {"at": moment, "id": row_id})
    return _public(_owned(connection, project, key, row_id))


def restore(connection: Connection, *, project_id: str, app_key: str, row_id: str,
            now: Optional[datetime] = None) -> dict[str, Any]:
    """Clear deleted_at when the row is not older than 30 days."""
    project = _project_id(project_id)
    key = _app_key(app_key)
    moment = _now(now)
    row = _owned(connection, project, key, row_id)
    if row["deleted_at"] is None:
        raise AppDataError("This row is not in the trash.", 409)
    if _expired(row["deleted_at"], moment):
        raise AppDataError("This row is older than 30 days.", 409)
    connection.execute(text(
        "UPDATE app_data_rows SET deleted_at = NULL, updated_at = :at WHERE id = :id"),
        {"at": moment, "id": row_id})
    return _public(_owned(connection, project, key, row_id))


def purge_expired(connection: Connection, *, now: datetime, project_id: Optional[str] = None,
                  app_key: Optional[str] = None) -> dict[str, Any]:
    """Delete trash older than 30 days. Never a live row, and never any other table."""
    moment = _as_utc(now)
    cutoff = moment - timedelta(days=TRASH_DAYS)
    project = _project_id(project_id) if project_id else None
    key = _app_key(app_key) if app_key else None
    found = connection.execute(text(
        "DELETE FROM app_data_rows "
        "WHERE deleted_at IS NOT NULL "
        "AND deleted_at < :cutoff "
        "AND (CAST(:project_id AS TEXT) IS NULL OR project_id = :project_id) "
        "AND (CAST(:app_key AS TEXT) IS NULL OR app_key = :app_key) "
        "RETURNING id"),
        {"cutoff": cutoff, "project_id": project, "app_key": key}).scalars().all()
    return {"purged": [str(row_id) for row_id in found], "purged_count": len(found)}


def page(connection: Connection, *, project_id: str, app_key: str, table: str = "",
         field: str = "", value: str = "", mode: str = "is",
         now: Optional[datetime] = None) -> dict[str, Any]:
    """One app's drawers, the open table, and the trash."""
    project = _project_id(project_id)
    key = _app_key(app_key)
    moment = _now(now)
    tables = [dict(row) for row in connection.execute(text(
        "SELECT t.name AS name, "
        "count(r.id) FILTER (WHERE r.deleted_at IS NULL) AS live_count "
        "FROM app_data_tables t "
        "LEFT JOIN app_data_rows r ON r.table_id = t.id "
        "WHERE t.project_id = :project_id AND t.app_key = :app_key "
        "GROUP BY t.name ORDER BY t.name"),
        {"project_id": project, "app_key": key}).mappings().all()]
    for item in tables:
        item["live_count"] = int(item["live_count"] or 0)
    chosen = _table_name(table) if table.strip() else (tables[0]["name"] if tables else "")
    rows = (filter_rows(connection, project_id=project, app_key=key, table=chosen,
                        field=field, value=value, mode=mode) if chosen else [])
    columns: list[str] = []
    for row in rows:
        for column in row["cells"]:
            if column not in columns:
                columns.append(column)
    trash_rows = connection.execute(text(
        "SELECT r.id, t.name AS table_name, r.cells, r.created_at, r.deleted_at "
        "FROM app_data_rows r JOIN app_data_tables t ON t.id = r.table_id "
        "WHERE r.project_id = :project_id AND r.app_key = :app_key "
        "AND r.deleted_at IS NOT NULL "
        "ORDER BY r.deleted_at DESC, r.id"),
        {"project_id": project, "app_key": key}).mappings().all()
    trash = [_public(row, table=row["table_name"], now=moment) for row in trash_rows]
    return {"app_key": key, "trash_days": TRASH_DAYS, "tables": tables, "table": chosen,
            "columns": columns, "rows": rows, "trash": trash}


def list_apps(connection: Connection, *, project_id: str) -> dict[str, Any]:
    """Apps in this project that already have a drawer."""
    project = _project_id(project_id)
    rows = connection.execute(text(
        "SELECT app_key, count(DISTINCT id) AS tables FROM app_data_tables "
        "WHERE project_id = :project_id GROUP BY app_key ORDER BY app_key"),
        {"project_id": project}).mappings().all()
    apps = []
    for row in rows:
        counts = connection.execute(text(
            "SELECT count(*) FILTER (WHERE deleted_at IS NULL) AS live_rows, "
            "count(*) FILTER (WHERE deleted_at IS NOT NULL) AS trash_rows "
            "FROM app_data_rows WHERE project_id = :project_id AND app_key = :app_key"),
            {"project_id": project, "app_key": row["app_key"]}).mappings().one()
        apps.append({"app_key": row["app_key"], "tables": int(row["tables"]),
                     "live_rows": int(counts["live_rows"]),
                     "trash_rows": int(counts["trash_rows"])})
    return {"apps": apps}
