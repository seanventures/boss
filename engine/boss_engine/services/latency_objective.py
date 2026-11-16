"""The latency objective a person stored for one project.

``project_configurations.config_key`` is ``latency_objective``. The body is
``{"ms": <number>}``. A missing row is not an objective, and nothing is filled in.
"""
from __future__ import annotations

import json
import math
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from boss_engine.services.project_experience.common import as_json

#: project_configurations.config_key. body is {"ms": <number>}.
_KEY = "latency_objective"


def _finite_ms(raw: Any) -> Optional[int | float]:
    """A finite number of milliseconds greater than 0, or None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    number = float(raw)
    if not math.isfinite(number) or number <= 0:
        return None
    if number.is_integer():
        return int(number)
    return number


def _require_ms(ms: Any) -> int | float:
    number = _finite_ms(ms)
    if number is None:
        raise ValueError("a latency objective is a finite number of milliseconds greater than 0")
    return number


def chosen_latency_ms(connection: Connection, project_id: str) -> Optional[int | float]:
    """The latency milliseconds a person stored for this project, or None."""
    found = connection.execute(text(
        "SELECT body FROM project_configurations WHERE project_id = :p AND config_key = :k"),
        {"p": project_id, "k": _KEY}).scalar()
    body = as_json(found)
    if not isinstance(body, dict):
        return None
    return _finite_ms(body.get("ms"))


def set_latency_objective(connection: Connection, project_id: str, ms: Any, *,
                          actor: Any = None) -> int | float:
    """Store the latency objective for this project, in milliseconds."""
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("a latency objective names who set it")
    number = _require_ms(ms)
    connection.execute(text(
        "INSERT INTO project_configurations (id, project_id, config_key, body, updated_by) "
        "VALUES (:id, :p, :k, CAST(:body AS JSONB), :by) "
        "ON CONFLICT (project_id, config_key) DO UPDATE "
        "SET body = EXCLUDED.body, updated_by = EXCLUDED.updated_by, updated_at = now()"),
        {"id": f"cfg_latency_{project_id}", "p": project_id, "k": _KEY,
         "body": json.dumps({"ms": number}), "by": actor.strip()})
    return number


def latency_words(ms: Optional[int | float]) -> str:
    """The sentence for a stored latency objective, or for none. N is that number."""
    if ms is None:
        return "No latency objective is stored."
    number = float(ms)
    shown: int | float = int(number) if number.is_integer() else number
    return f"Latency objective is {shown} ms."
