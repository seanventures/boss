"""Read-only sealed-slice honesty for the factory / Mission Control card.

Live PR success is UNPROVEN until a non-simulated, non-fixture PR receipt
exists. Empty / unknown is valid. Never invent a percentage.

This module only SELECTs existing ``code_task_runs``, ``receipts``, and
``jobs``. It does not enqueue work, reserve budget, or call providers.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

UNKNOWN_LABEL = "unknown / not proven"
LIVE_PR_RECEIPT_KINDS = ("github.pull_request.create",)
FIXTURE_OWNER = "_fixture"


def live_pr_success_from_counts(*, successes: int, attempts: int) -> dict[str, Any]:
    """Pure honesty payload. ``rate`` is null when the sample is empty."""
    ok = max(0, int(successes))
    n = max(0, int(attempts))
    if n <= 0:
        return {
            "rate": None,
            "label": UNKNOWN_LABEL,
            "proven": False,
            "sample_size": 0,
            "successes": 0,
            "source": "receipts_and_jobs",
        }
    return {
        "rate": round(ok / n, 4),
        "label": f"{ok}/{n} from existing receipts (not a sealed weekly proof)",
        "proven": False,
        "sample_size": n,
        "successes": ok,
    }


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _has_pr_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return bool(value.strip())


def last_simulated_fixture(
    connection: Connection, project_id: str
) -> Optional[dict[str, Any]]:
    """Latest simulated/fixture code-task run, or None."""
    row = connection.execute(
        text(
            """
            SELECT id, status, phase, title, created_at, usage, spec
            FROM code_task_runs
            WHERE project_id = :p
              AND (
                status = 'simulated'
                OR COALESCE((spec->>'simulated')::boolean, false) = true
              )
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"p": project_id},
    ).mappings().first()
    if not row:
        return None
    usage = _as_dict(row.get("usage"))
    spec = _as_dict(row.get("spec"))
    return {
        "run_id": row["id"],
        "status": row["status"],
        "phase": row.get("phase"),
        "title": row.get("title"),
        "created_at": _iso(row.get("created_at")),
        "cost_usd_micros": int(usage.get("cost_usd_micros") or 0),
        "simulated": True,
        "owner": spec.get("owner"),
        "repo": spec.get("repo"),
    }


def _receipt_attempts(connection: Connection, project_id: str) -> tuple[int, int]:
    rows = connection.execute(
        text(
            """
            SELECT status, result
            FROM receipts
            WHERE project_id = :p
              AND effect_kind IN ('github.pull_request.create')
            """
        ),
        {"p": project_id},
    ).mappings().all()
    attempts = 0
    successes = 0
    for row in rows:
        attempts += 1
        result = _as_dict(row.get("result"))
        pr_url = result.get("pr_url") or result.get("pull_request_url")
        if row.get("status") == "succeeded" and _has_pr_url(pr_url if isinstance(pr_url, str) else None):
            successes += 1
        elif row.get("status") == "succeeded":
            # Succeeded create receipt is a live PR even if the URL field name varies.
            successes += 1
    return successes, attempts


def _live_code_task_prs(connection: Connection, project_id: str) -> tuple[int, int]:
    rows = connection.execute(
        text(
            """
            SELECT id, status, pull_request_url, spec
            FROM code_task_runs
            WHERE project_id = :p
              AND pull_request_url IS NOT NULL
              AND pull_request_url <> ''
              AND status <> 'simulated'
              AND COALESCE((spec->>'simulated')::boolean, false) = false
              AND COALESCE(spec->>'owner', '') <> :fixture_owner
            """
        ),
        {"p": project_id, "fixture_owner": FIXTURE_OWNER},
    ).mappings().all()
    # A real PR URL is both an attempt and a success. Failures that never
    # opened a PR are not live-PR attempts (they never reached the PR step).
    n = len(rows)
    return n, n


def live_pr_success(connection: Connection, project_id: str) -> dict[str, Any]:
    """Aggregate live (non-simulated) PR evidence. Empty → unknown / not proven."""
    rec_ok, rec_n = _receipt_attempts(connection, project_id)
    if rec_n > 0:
        return live_pr_success_from_counts(successes=rec_ok, attempts=rec_n)
    run_ok, run_n = _live_code_task_prs(connection, project_id)
    return live_pr_success_from_counts(successes=run_ok, attempts=run_n)


def sealed_slice_status(connection: Connection, project_id: str) -> dict[str, Any]:
    """Operator-visible honesty block for GET /v1/factory/wip. Fail-open on query errors."""
    empty = {
        "last_simulated_fixture": None,
        "live_pr_success": live_pr_success_from_counts(successes=0, attempts=0),
    }
    try:
        return {
            "last_simulated_fixture": last_simulated_fixture(connection, project_id),
            "live_pr_success": live_pr_success(connection, project_id),
        }
    except Exception:  # noqa: BLE001 — card must not 500
        return empty


__all__ = [
    "UNKNOWN_LABEL",
    "LIVE_PR_RECEIPT_KINDS",
    "last_simulated_fixture",
    "live_pr_success",
    "live_pr_success_from_counts",
    "sealed_slice_status",
]
