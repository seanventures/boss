"""Open work for one project, grouped by the person on the row.

The person is `assignee` when that column exists, otherwise `owner_actor`. An
`agent:…` value, or a role key this project has, is not a person. A row with no
person is the group ``none``. Closed items and other projects are not counted.
The age is seconds since the oldest still-open item in the group was created.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import now_utc
from .work_items import CLOSED

_PERSON_COLUMNS = ("assignee", "owner_actor")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _person_column(connection: Connection) -> str:
    found = set(connection.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'work_items' "
        "AND column_name = ANY(:names)"),
        {"names": list(_PERSON_COLUMNS)}).scalars())
    # assignee is who works the item. owner_actor is that same value on the
    # canonical write, and the only column if assignee was never added.
    for name in _PERSON_COLUMNS:
        if name in found:
            return name
    raise LookupError("work_items has neither assignee nor owner_actor")


def _person_sql(column: str) -> str:
    if column not in _PERSON_COLUMNS:
        raise LookupError(column)
    raw = f"NULLIF(btrim(w.{column}), '')"
    return (
        "CASE "
        f"WHEN {raw} IS NULL THEN 'none' "
        f"WHEN lower({raw}) LIKE 'agent:%' THEN 'none' "
        "WHEN EXISTS (SELECT 1 FROM roles r WHERE r.project_id = w.project_id "
        f"AND r.role_key = {raw}) THEN 'none' "
        f"ELSE {raw} END"
    )


def open_work_by_person(connection: Connection, project_id: str, *,
                        now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Each person's open-item count and the age of their oldest open item."""
    moment = _aware(now or now_utc())
    person = _person_sql(_person_column(connection))
    rows = connection.execute(text(
        f"SELECT {person} AS person, w.created_at FROM work_items w "
        "WHERE w.project_id = :project_id AND w.status <> ALL(:closed)"),
        {"project_id": project_id, "closed": list(CLOSED)}).mappings()
    grouped: dict[str, list[datetime]] = {}
    for row in rows:
        grouped.setdefault(str(row["person"]), []).append(_aware(row["created_at"]))
    out = []
    for name, created in grouped.items():
        oldest = min(created)
        age = int(round((moment - oldest).total_seconds()))
        out.append({"person": name, "count": len(created), "oldest_age_seconds": max(0, age)})
    out.sort(key=lambda row: (row["person"] == "none", row["person"]))
    return out
