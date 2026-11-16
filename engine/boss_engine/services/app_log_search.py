"""Search one project's rows in `app_logs`.

Those rows are lines BOSS itself wrote while it worked: the api and the worker, command
timings, a stage waiting on a model lane. They are not the log of a customer's deployed
app. That other read talks to a host. This one only reads the table.

A search names one project, a text fragment, and a time window. No project id is not a
search of the whole table: it returns no lines. A fragment that matches nothing is an
empty list. An empty list is not a statement that a deployed app was silent.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: What a caller is looking at. Present on every result, including an empty one, so an
#: empty list cannot be retold as the app having nothing to say.
ABOUT = "BOSS process lines, not a customer's deployed app."


def _like(fragment: str) -> str:
    """A substring pattern. `%` and `_` in the fragment stay characters."""
    escaped = fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _empty() -> dict[str, Any]:
    return {"lines": [], "about": ABOUT}


def search(connection: Connection, *, project_id: Optional[str], fragment: str,
           since: Any, until: Any) -> dict[str, Any]:
    """Lines of one project whose message contains `fragment`, inside `[since, until]`.

    Oldest first. Another project's rows are not included, and a row with no project is
    not this project's either. No project id, or a blank fragment, returns no lines.
    """
    project = "" if project_id is None else str(project_id).strip()
    needle = "" if fragment is None else str(fragment).strip()
    if not project or not needle or since is None or until is None:
        return _empty()
    rows = connection.execute(text(
        "SELECT id, project_id, run_id, service, level, logger, message, created_at "
        "FROM app_logs "
        "WHERE project_id = :project_id "
        "AND created_at >= :since AND created_at <= :until "
        "AND message ILIKE :fragment ESCAPE '\\' "
        "ORDER BY created_at ASC, id ASC"
    ), {
        "project_id": project,
        "since": since,
        "until": until,
        "fragment": _like(needle),
    }).mappings().all()
    return {
        "lines": [
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "run_id": row["run_id"],
                "service": row["service"],
                "level": row["level"],
                "logger": row["logger"],
                "message": row["message"],
                "created_at": _iso(row["created_at"]),
            }
            for row in rows
        ],
        "about": ABOUT,
    }
