"""InfraCommitment reconciler + tier-2+ initiative budget approval (spec §F).

- reconcile_commitments: for every active commitment, compare committed spend against actual
  cost_events in its period → a utilization ratio + verdict (on_track | under_utilized |
  over_utilized). Under-utilized commitments (waste) and over-utilized ones (overrun) open a review
  Decision. The schema had commitments but no reconciler; this is it.
- request_initiative_budget: a tier-2+ initiative above the approval threshold must be approved by a
  human (blocking Decision); smaller/lower-tier initiatives auto-approve.
- build_summary (BOSS-244): multi-dimension FinOps chart payload — spend_by_provider,
  spend_by_day, open anomalies, commitment utilization, top resources — for Mission Control.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from ..db import begin
from .core import create_artifact, create_blocking_decision, emit_event, make_id

UNDER_UTILIZED_BELOW = 0.5   # < 50% of a commitment used → waste review
OVER_UTILIZED_ABOVE = 1.0    # > 100% used → overrun review
INITIATIVE_APPROVAL_THRESHOLD_USD_MICROS = 50_000_000  # $50 — tier2+ above this needs approval
DEFAULT_SUMMARY_DAYS = 30
MIN_SUMMARY_DAYS = 1
MAX_SUMMARY_DAYS = 366
TOP_RESOURCES_LIMIT = 10


def _clamp_days(days: int) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        value = DEFAULT_SUMMARY_DAYS
    return max(MIN_SUMMARY_DAYS, min(MAX_SUMMARY_DAYS, value))


def _verdict_for_utilization(utilization: float) -> str:
    if utilization < UNDER_UTILIZED_BELOW:
        return "under_utilized"
    if utilization > OVER_UTILIZED_ABOVE:
        return "over_utilized"
    return "on_track"


class FinopsReconcilerService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def build_summary(
        self,
        engine,
        project_id: str,
        *,
        days: int = DEFAULT_SUMMARY_DAYS,
    ) -> dict[str, Any]:
        """Multi-dimension FinOps summary for charts (BOSS-244).

        Aggregates ``cost_events`` over the trailing window, counts open
        ``cost_anomalies``, scores active ``commitments``, and ranks resources
        that have ``external_resource_id`` refs.
        """
        window_days = _clamp_days(days)
        period_end = datetime.now(timezone.utc)
        period_start = period_end - timedelta(days=window_days)

        with begin(engine) as connection:
            spend_by_provider = [
                {
                    "provider": str(row["provider"]),
                    "cost_usd_micros": int(row["cost_usd_micros"] or 0),
                    "event_count": int(row["event_count"] or 0),
                }
                for row in connection.execute(
                    text(
                        """
                        SELECT provider,
                               COALESCE(SUM(amount_usd_micros), 0)::bigint AS cost_usd_micros,
                               COUNT(*)::int AS event_count
                        FROM cost_events
                        WHERE project_id = :p
                          AND created_at >= :start
                          AND created_at <= :end
                        GROUP BY provider
                        ORDER BY cost_usd_micros DESC, provider ASC
                        """
                    ),
                    {"p": project_id, "start": period_start, "end": period_end},
                ).mappings().all()
            ]

            spend_by_day = [
                {
                    "day": str(row["day"]),
                    "cost_usd_micros": int(row["cost_usd_micros"] or 0),
                }
                for row in connection.execute(
                    text(
                        """
                        SELECT to_char(date_trunc('day', created_at AT TIME ZONE 'UTC'),
                                       'YYYY-MM-DD') AS day,
                               COALESCE(SUM(amount_usd_micros), 0)::bigint AS cost_usd_micros
                        FROM cost_events
                        WHERE project_id = :p
                          AND created_at >= :start
                          AND created_at <= :end
                        GROUP BY date_trunc('day', created_at AT TIME ZONE 'UTC')
                        ORDER BY day ASC
                        """
                    ),
                    {"p": project_id, "start": period_start, "end": period_end},
                ).mappings().all()
            ]

            anomalies_open_count = int(
                connection.execute(
                    text(
                        """
                        SELECT COUNT(*)::int
                        FROM cost_anomalies
                        WHERE project_id = :p AND status = 'open'
                        """
                    ),
                    {"p": project_id},
                ).scalar()
                or 0
            )

            commitment_utilization: list[dict[str, Any]] = []
            commitments = connection.execute(
                text(
                    """
                    SELECT id, provider, amount_usd_micros, period_start, period_end
                    FROM commitments
                    WHERE project_id = :p AND status = 'active'
                    ORDER BY provider ASC, id ASC
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
                              AND created_at >= :cstart AND created_at <= :cend
                            """
                        ),
                        {
                            "p": project_id,
                            "prov": commitment["provider"],
                            "cstart": commitment["period_start"],
                            "cend": commitment["period_end"],
                        },
                    ).scalar()
                    or 0
                )
                utilization = (actual / committed) if committed else 0.0
                commitment_utilization.append(
                    {
                        "commitment_id": str(commitment["id"]),
                        "provider": str(commitment["provider"]),
                        "utilization": round(utilization, 4),
                        "verdict": _verdict_for_utilization(utilization),
                    }
                )

            top_resources = [
                {
                    "resource_ref": str(row["resource_ref"]),
                    "provider": str(row["provider"]),
                    "cost_usd_micros": int(row["cost_usd_micros"] or 0),
                }
                for row in connection.execute(
                    text(
                        """
                        SELECT COALESCE(er.external_id, ce.external_resource_id) AS resource_ref,
                               ce.provider,
                               COALESCE(SUM(ce.amount_usd_micros), 0)::bigint AS cost_usd_micros
                        FROM cost_events ce
                        LEFT JOIN external_resources er ON er.id = ce.external_resource_id
                        WHERE ce.project_id = :p
                          AND ce.created_at >= :start
                          AND ce.created_at <= :end
                          AND ce.external_resource_id IS NOT NULL
                        GROUP BY COALESCE(er.external_id, ce.external_resource_id), ce.provider
                        ORDER BY cost_usd_micros DESC, resource_ref ASC
                        LIMIT :lim
                        """
                    ),
                    {
                        "p": project_id,
                        "start": period_start,
                        "end": period_end,
                        "lim": TOP_RESOURCES_LIMIT,
                    },
                ).mappings().all()
            ]

        return {
            "project_id": project_id,
            "days": window_days,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "spend_by_provider": spend_by_provider,
            "spend_by_day": spend_by_day,
            "anomalies_open_count": anomalies_open_count,
            "commitment_utilization": commitment_utilization,
            "top_resources": top_resources,
        }

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
                verdict = _verdict_for_utilization(utilization)

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
