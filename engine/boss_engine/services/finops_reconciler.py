"""InfraCommitment reconciler + tier-2+ initiative budget approval (spec §F).

- reconcile_commitments: for every active commitment, compare committed spend against actual
  cost_events in its period → a utilization ratio + verdict (on_track | under_utilized |
  over_utilized). Under-utilized commitments (waste) and over-utilized ones (overrun) open a review
  Decision. The schema had commitments but no reconciler; this is it.
- request_initiative_budget: a tier-2+ initiative above the approval threshold must be approved by a
  human (blocking Decision); smaller/lower-tier initiatives auto-approve.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from ..db import begin
from .core import create_artifact, create_blocking_decision, emit_event, make_id

UNDER_UTILIZED_BELOW = 0.5   # < 50% of a commitment used → waste review
OVER_UTILIZED_ABOVE = 1.0    # > 100% used → overrun review
INITIATIVE_APPROVAL_THRESHOLD_USD_MICROS = 50_000_000  # $50 — tier2+ above this needs approval


class FinopsReconcilerService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def reconcile_commitments(
        self, engine, project_id: str, *, actor: str = "finops_reconciler"
    ) -> dict[str, Any]:
        reconciliations: list[dict[str, Any]] = []
        with begin(engine) as connection:
            commitments = connection.execute(
                text(
                    """
                    SELECT id, provider, amount_usd_micros, period_start, period_end
                    FROM commitments
                    WHERE project_id = :p AND status = 'active'
                    """
                ),
                {"p": project_id},
            ).mappings().all()

            for commitment in commitments:
                committed = int(commitment["amount_usd_micros"] or 0)
                actual = int(
                    connection.execute(
                        text(
                            """
                            SELECT COALESCE(SUM(amount_usd_micros), 0)
                            FROM cost_events
                            WHERE project_id = :p AND provider = :prov
                              AND created_at >= :start AND created_at <= :end
                            """
                        ),
                        {"p": project_id, "prov": commitment["provider"],
                         "start": commitment["period_start"], "end": commitment["period_end"]},
                    ).scalar()
                    or 0
                )
                utilization = (actual / committed) if committed else 0.0
                if utilization < UNDER_UTILIZED_BELOW:
                    verdict = "under_utilized"
                elif utilization > OVER_UTILIZED_ABOVE:
                    verdict = "over_utilized"
                else:
                    verdict = "on_track"

                decision_id = None
                if verdict != "on_track":
                    decision_id = create_blocking_decision(
                        connection,
                        project_id,
                        None,
                        f"Commitment {commitment['id']} is {verdict} ({utilization:.0%} used)",
                        {"action": "review_commitment", "commitment_id": commitment["id"],
                         "verdict": verdict},
                        {"committed_usd_micros": committed, "actual_usd_micros": actual,
                         "utilization": round(utilization, 4)},
                        f"commitment_{verdict}",
                        risk_tier=2,
                    )
                recon_id = make_id("crec")
                connection.execute(
                    text(
                        """
                        INSERT INTO commitment_reconciliations (
                            id, project_id, commitment_id, provider, committed_usd_micros,
                            actual_usd_micros, utilization, verdict, decision_id)
                        VALUES (:id, :p, :cid, :prov, :committed, :actual, :util, :verdict, :dec)
                        """
                    ),
                    {"id": recon_id, "p": project_id, "cid": commitment["id"],
                     "prov": commitment["provider"], "committed": committed, "actual": actual,
                     "util": round(utilization, 6), "verdict": verdict, "dec": decision_id},
                )
                emit_event(
                    connection, project_id, "commitment.reconciled", actor,
                    "commitment", commitment["id"],
                    {"verdict": verdict, "utilization": round(utilization, 4),
                     "decision_id": decision_id})
                reconciliations.append(
                    {"id": recon_id, "commitment_id": commitment["id"],
                     "provider": commitment["provider"], "committed_usd_micros": committed,
                     "actual_usd_micros": actual, "utilization": round(utilization, 4),
                     "verdict": verdict, "decision_id": decision_id})

        return {
            "project_id": project_id,
            "reconciled": len(reconciliations),
            "flagged": sum(1 for r in reconciliations if r["verdict"] != "on_track"),
            "reconciliations": reconciliations,
        }

    def list_reconciliations(self, engine, project_id: str, limit: int = 50) -> list[dict]:
        with begin(engine) as connection:
            rows = connection.execute(
                text(
                    "SELECT id, commitment_id, provider, committed_usd_micros, actual_usd_micros, "
                    "utilization, verdict, decision_id, created_at FROM commitment_reconciliations "
                    "WHERE project_id = :p ORDER BY created_at DESC LIMIT :l"
                ),
                {"p": project_id, "l": limit},
            ).mappings().all()
        return [dict(r) for r in rows]

    def request_initiative_budget(
        self,
        connection: Connection,
        project_id: str,
        *,
        title: str,
        tier: int,
        amount_usd_micros: int,
        actor: str,
        threshold_usd_micros: int = INITIATIVE_APPROVAL_THRESHOLD_USD_MICROS,
    ) -> dict[str, Any]:
        """A tier-2+ initiative above the approval threshold needs an explicit human Decision."""
        approval_required = tier >= 2 and int(amount_usd_micros) >= threshold_usd_micros
        artifact_id = create_artifact(
            connection, self.settings, project_id, None, "initiative_budget_request",
            f"Initiative budget: {title}",
            {"title": title, "tier": tier, "amount_usd_micros": int(amount_usd_micros),
             "approval_required": approval_required, "requested_by": actor})
        decision_id = None
        status = "auto_approved"
        if approval_required:
            decision_id = create_blocking_decision(
                connection, project_id, None,
                f"Approve tier-{tier} initiative budget: {title}",
                {"action": "approve_initiative_budget", "title": title, "tier": tier,
                 "amount_usd_micros": int(amount_usd_micros), "artifact_id": artifact_id},
                {"amount_usd_micros": int(amount_usd_micros), "threshold_usd_micros": threshold_usd_micros},
                "tier2_initiative_budget_approval", risk_tier=max(tier, 2))
            status = "pending_approval"
        emit_event(
            connection, project_id, "initiative_budget.requested", actor,
            "artifact", artifact_id,
            {"title": title, "tier": tier, "amount_usd_micros": int(amount_usd_micros),
             "approval_required": approval_required, "decision_id": decision_id})
        return {"title": title, "tier": tier, "amount_usd_micros": int(amount_usd_micros),
                "approval_required": approval_required, "status": status,
                "artifact_id": artifact_id, "decision_id": decision_id}
