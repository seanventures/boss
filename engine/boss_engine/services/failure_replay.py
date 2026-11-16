"""Copy a failed workflow run's inputs into a new run.

The failed run keeps its id. The copy gets a new id, one budget reservation, and a
receipt whose dedupe key is not the failed run's. The same click does that once.
A run that did not fail is left alone. The workflow builder and the code-task
builder are not started: no step rows, no job, no model call.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .budget import BUILD_CATEGORY, BudgetGovernor
from .core import make_id, stable_dedupe_key
from .receipts import ReceiptService


EFFECT = "failure.replay"
HOLD_USD_MICROS = 100_000
HOLD_MINUTES = 45


class ReplayRefused(Exception):
    """The replay was not written. The caller's transaction should roll back."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def replay(
    connection: Connection,
    *,
    project_id: str,
    run_id: str,
    actor: str,
    click_id: str,
) -> dict[str, Any]:
    """Replay `run_id` when it failed. Same `click_id` returns the copy already made."""
    click = str(click_id or "").strip()
    if not click:
        raise ValueError("click_id is required")
    source = connection.execute(
        text(
            """
            SELECT id, status, input, workflow_definition_id, workflow_version_id,
                   dedupe_key, root_run_id
            FROM workflow_runs
            WHERE id = :id AND project_id = :project_id
            """
        ),
        {"id": run_id, "project_id": project_id},
    ).mappings().first()
    if source is None:
        return _none("not_found")
    if str(source["status"]) != "failed":
        return _none("not_failed")

    dedupe_key = stable_dedupe_key("failure-replay", project_id, run_id, click)
    if dedupe_key == source["dedupe_key"]:
        dedupe_key = stable_dedupe_key("failure-replay", "new", project_id, run_id, click)
    prior = _saved(connection, project_id, dedupe_key)
    if prior is not None:
        return prior

    place = connection.execute(
        text(
            """
            SELECT w.org_id, w.id AS workspace_id
            FROM projects p
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE p.id = :project_id
            """
        ),
        {"project_id": project_id},
    ).mappings().first()
    if place is None:
        return _none("not_found")

    from ..config import get_settings

    reservation = BudgetGovernor(get_settings()).reserve(
        connection, project_id, category=BUILD_CATEGORY,
        amount_usd_micros=HOLD_USD_MICROS,
        reason=f"replay saved failure {run_id}"[:200],
        expires_in_minutes=HOLD_MINUTES)
    if reservation.get("status") != "reserved" or not reservation.get("reservation_id"):
        raise ReplayRefused(str(reservation.get("message") or "The budget refused this replay."))

    new_id = make_id("wfr")
    inputs = _inputs(source["input"])
    inserted = connection.execute(
        text(
            """
            INSERT INTO workflow_runs (
                id, project_id, workflow_definition_id, workflow_version_id,
                parent_run_id, root_run_id, actor, status, input, context, dedupe_key
            ) VALUES (
                :id, :project_id, :definition_id, :version_id,
                :parent_run_id, :root_run_id, :actor, 'queued',
                CAST(:input AS JSONB), CAST(:context AS JSONB), :dedupe_key
            )
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "id": new_id,
            "project_id": project_id,
            "definition_id": source["workflow_definition_id"],
            "version_id": source["workflow_version_id"],
            "parent_run_id": source["id"],
            "root_run_id": source["root_run_id"] or source["id"],
            "actor": str(actor or "").strip() or "operator",
            "input": json.dumps(inputs),
            "context": json.dumps({"replay_of": source["id"], "click_id": click}),
            "dedupe_key": dedupe_key,
        },
    )
    if not inserted.rowcount:
        prior = _saved(connection, project_id, dedupe_key)
        if prior is not None:
            return prior
        raise ReplayRefused("The replay was not stored.")

    payload = {
        "project_id": project_id,
        "run_id": new_id,
        "source_run_id": source["id"],
        "reservation_id": reservation["reservation_id"],
        "dedupe_key": dedupe_key,
        "inputs": inputs,
    }
    receipt, created = ReceiptService().record_completed(
        connection, str(place["org_id"]), str(place["workspace_id"]), project_id,
        dedupe_key, EFFECT, {"source_run_id": source["id"]}, payload,
        provider_ref=None)
    if not created:
        prior = _saved(connection, project_id, dedupe_key)
        if prior is not None:
            return prior
        raise ReplayRefused("The replay receipt was not stored.")
    return {
        "created": True,
        "run_id": new_id,
        "receipt_id": receipt["id"],
        "reservation_id": reservation["reservation_id"],
        "dedupe_key": dedupe_key,
        "reason": None,
    }


def _none(reason: str) -> dict[str, Any]:
    return {"created": False, "run_id": None, "receipt_id": None,
            "reservation_id": None, "dedupe_key": None, "reason": reason}


def _inputs(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    return json.loads(json.dumps(raw, default=str))


def _saved(connection: Connection, project_id: str, dedupe_key: str) -> Optional[dict[str, Any]]:
    """The copy this click already wrote, if its receipt is the one with this key."""
    row = connection.execute(
        text(
            """
            SELECT r.id, r.project_id, r.result
            FROM receipts r
            JOIN projects p ON p.id = r.project_id
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE w.org_id = r.org_id AND r.project_id = :project_id
              AND r.dedupe_key = :dedupe_key
            """
        ),
        {"project_id": project_id, "dedupe_key": dedupe_key},
    ).mappings().first()
    if row is None:
        return None
    result = row["result"]
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            result = {}
    if not isinstance(result, dict):
        result = {}
    run_id = str(result.get("run_id") or "").strip()
    if not run_id or run_id == str(result.get("source_run_id") or ""):
        return None
    # A receipt without its run is not a finished click. Do not reserve again for it.
    found = connection.execute(
        text("SELECT id FROM workflow_runs WHERE id = :id AND project_id = :project_id"),
        {"id": run_id, "project_id": project_id},
    ).first()
    if found is None:
        return None
    return {
        "created": False,
        "run_id": run_id,
        "receipt_id": row["id"],
        "reservation_id": result.get("reservation_id"),
        "dedupe_key": dedupe_key,
        "reason": "already_replayed",
    }
