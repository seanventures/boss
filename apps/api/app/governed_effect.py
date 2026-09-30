"""Record a local governed effect without calling a provider.

A confirmed Community route may spend, lease, or destroy only after the actor,
project, budget reservation, lease, and rollback plan are on the receipt.
The receipt is the effect. Nothing in this module opens a network connection.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import text

from boss_engine.services.core import now_utc, stable_dedupe_key


def governed_steps(value: Any) -> Optional[list[str]]:
    """A rollback plan the human reviewed: a non-empty list of non-blank steps."""
    if not isinstance(value, list) or not value:
        return None
    steps: list[str] = []
    for item in value:
        step = str(item).strip()
        if not step:
            return None
        steps.append(step)
    return steps


def record_governed_effect(
    connection,
    *,
    project_id: str,
    actor: str,
    effect_kind: str,
    dedupe_parts: list[Any],
    payload: Optional[dict] = None,
    rollback_plan: Optional[list[str]] = None,
    destructive: bool = False,
    spends: bool = False,
    reservation_id: Optional[str] = None,
    expected_cost_usd_micros: int = 0,
    worker: bool = False,
) -> dict[str, Any]:
    """Persist the actor, scope, optional reservation, lease, and receipt.

    Callers already authorized the actor. This refuses a destructive effect
    without a rollback plan and a spend without an active reservation, then
    closes a local lease so a worker cannot pick the job up and call a provider.
    """
    from apps.api.app.main import budget, jobs, receipts

    actor = str(actor or "").strip()
    if not actor:
        raise HTTPException(status_code=422, detail="actor is required")
    plan = governed_steps(rollback_plan) if rollback_plan is not None else None
    if destructive and not plan:
        raise HTTPException(status_code=422, detail="rollback_plan is required")
    try:
        expected = int(expected_cost_usd_micros or 0)
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=422, detail="expected_cost_usd_micros must be an integer"
        ) from error
    if expected < 0:
        raise HTTPException(
            status_code=422, detail="expected_cost_usd_micros must be non-negative"
        )

    tenant = connection.execute(
        text(
            """
            SELECT w.org_id, p.workspace_id
            FROM projects p
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE p.id = :project_id
            """
        ),
        {"project_id": project_id},
    ).mappings().first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Project not found.")

    kept_reservation = str(reservation_id or "").strip() or None
    if spends:
        if not kept_reservation:
            raise HTTPException(status_code=422, detail="reservation_id is required")
        active = budget.assert_active_reservation(
            connection, project_id, kept_reservation
        )
        if not active:
            raise HTTPException(
                status_code=409, detail="active budget reservation required"
            )
        minimum = expected if expected > 0 else 1
        if int(active["amount_usd_micros"]) < minimum:
            raise HTTPException(
                status_code=409,
                detail="budget reservation is smaller than the expected cost",
            )

    dedupe_key = stable_dedupe_key(effect_kind, project_id, *dedupe_parts)
    job_id = None
    if worker:
        job = jobs.create_job(
            connection,
            project_id=project_id,
            kind=f"governed.{effect_kind}",
            payload={
                "effect_kind": effect_kind,
                "actor": actor,
                "provider_called": False,
            },
            dedupe_key=dedupe_key,
        )
        job_id = job.get("id")
        if not job_id:
            raise HTTPException(
                status_code=409, detail="governed job was not leased"
            )
        now = now_utc()
        token = hashlib.sha256(f"{job_id}:{dedupe_key}".encode()).hexdigest()
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'succeeded',
                    lease_owner = :owner,
                    lease_token_hash = :token_hash,
                    lease_expires_at = :expires,
                    heartbeat_at = :now,
                    updated_at = :now
                WHERE id = :job_id AND project_id = :project_id
                """
            ),
            {
                "owner": actor,
                "token_hash": token,
                "expires": now + timedelta(seconds=120),
                "now": now,
                "job_id": job_id,
                "project_id": project_id,
            },
        )

    scope = {
        "actor": actor,
        "provider_called": False,
        "rollback_plan": plan or [],
        "reservation_id": kept_reservation,
        "job_id": job_id,
    }
    receipt, _created = receipts.record_completed(
        connection,
        tenant["org_id"],
        tenant["workspace_id"],
        project_id,
        dedupe_key,
        effect_kind,
        scope,
        payload or {},
        provider_ref=None,
        created_by_job_id=job_id,
    )
    return {
        "receipt_id": receipt["id"],
        "dedupe_key": dedupe_key,
        "reservation_id": kept_reservation,
        "job_id": job_id,
        "rollback_plan": plan or [],
    }
