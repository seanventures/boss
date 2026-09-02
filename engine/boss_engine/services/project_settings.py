"""Knobs an operator can turn without a deploy.

How hard BOSS runs — how many agents at once, how much it may spend — lived entirely in
environment variables. That is the right home for a secret and the wrong home for a dial
somebody wants to try at 20 and then at 5: changing one meant an ops action and a container
restart, so in practice nobody changed them and the defaults became the product.

A project setting overrides the environment; the environment remains the default. Nothing here
invents a value: a knob nobody has touched reads exactly as it did before this existed.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text

#: The knobs the console may set, with the settings attribute each one overrides and the
#: bounds a value has to fall inside. An unlisted key is refused rather than stored: a settings
#: table that accepts anything becomes a place where typos live forever.
KNOBS: dict[str, dict[str, Any]] = {
    "code_agent_max_concurrency": {
        "attr": "code_agent_max_concurrency",
        "label": "Agents at once",
        "help": "How many coding agents may run in parallel on this project.",
        "kind": "int", "min": 1, "max": 16,
    },
    "code_agent_max_turns": {
        "attr": "code_agent_max_turns",
        "label": "Turn budget per agent",
        "help": "How long one agent may keep working before it must hand back.",
        "kind": "int", "min": 10, "max": 200,
    },
    "code_review_inrun_max_rounds": {
        "attr": "code_review_inrun_max_rounds",
        "label": "Review rounds",
        "help": "How many times reviewers and the implementer may converse before the run "
                "falls back to a colder path.",
        "kind": "int", "min": 0, "max": 5,
    },
}


class UnknownKnob(ValueError):
    """The key is not one of ours. Refused rather than stored."""


def coerce(key: str, value: Any) -> Any:
    """The value this knob will actually take, or an error saying why it will not.

    Bounds are enforced HERE rather than at the edge, so every caller — console, API, a script
    — gets the same answer. A concurrency of 400 is not a preference, it is an outage.
    """
    spec = KNOBS.get(key)
    if spec is None:
        raise UnknownKnob(f"'{key}' is not a setting BOSS knows about.")
    if spec["kind"] == "int":
        try:
            number = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{spec['label']} has to be a whole number.") from error
        if not (spec["min"] <= number <= spec["max"]):
            raise ValueError(
                f"{spec['label']} has to be between {spec['min']} and {spec['max']}.")
        return number
    return value


def get_all(connection, project_id: str) -> dict[str, Any]:
    """Every knob this project has set. Absent keys are not defaults — they are silence."""
    rows = connection.execute(
        text("SELECT key, value FROM project_settings WHERE project_id = :p"),
        {"p": project_id},
    ).mappings().all()
    return {r["key"]: r["value"] for r in rows}


def set_one(connection, project_id: str, key: str, value: Any, actor: str = "operator") -> Any:
    clean = coerce(key, value)
    connection.execute(
        text("INSERT INTO project_settings (project_id, key, value, updated_by, updated_at) "
             "VALUES (:p, :k, CAST(:v AS JSONB), :by, now()) "
             "ON CONFLICT (project_id, key) DO UPDATE "
             "SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, "
             "    updated_at = now()"),
        {"p": project_id, "k": key, "v": json.dumps(clean), "by": actor},
    )
    return clean


def clear_one(connection, project_id: str, key: str) -> None:
    """Hand the knob back to the environment. Deleting is how a project says 'use the default'
    — storing the current default instead would freeze it against a later change."""
    connection.execute(
        text("DELETE FROM project_settings WHERE project_id = :p AND key = :k"),
        {"p": project_id, "k": key},
    )


def effective(connection, project_id: Optional[str], settings: Any, key: str) -> Any:
    """What this knob is worth right now: the project's value, else the environment's.

    Never raises. A settings table that is unreachable must not stop work — the environment is
    a complete answer on its own, and was the only answer until this existed.
    """
    spec = KNOBS.get(key)
    fallback = getattr(settings, spec["attr"], None) if spec else None
    if not project_id or spec is None:
        return fallback
    try:
        row = connection.execute(
            text("SELECT value FROM project_settings WHERE project_id = :p AND key = :k"),
            {"p": project_id, "k": key},
        ).scalar()
    except Exception:  # noqa: BLE001 — the environment is a complete answer
        return fallback
    return fallback if row is None else row
