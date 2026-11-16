"""Count firing alerts a silence or an inhibit holds off the incident list.

Those rows are still firing. Zero held back is not a healthy system.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text


def silenced_firing(connection: Any, project_id: str) -> dict[str, Any]:
    """Firing app_alerts in this project with silenced_by or inhibited_by set."""
    held = connection.execute(text(
        "SELECT count(*) FROM app_alerts WHERE project_id = :p AND state = 'firing' "
        "AND (silenced_by IS NOT NULL OR inhibited_by IS NOT NULL)"),
        {"p": str(project_id)}).scalar() or 0
    n = int(held)
    if n == 0:
        words = "No firing alert is held back."
    elif n == 1:
        words = "1 firing alert held back."
    else:
        words = f"{n} firing alerts held back."
    return {"project_id": str(project_id), "held_back": n, "words": words}
