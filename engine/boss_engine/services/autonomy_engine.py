"""Evidence-based autonomy engine (spec §C: evidence-based autonomy auto-upgrade/downgrade).

The role-governance review engine already decides upgrades/downgrades from a metrics bundle — but
until now a human had to hand it those numbers. This engine *gathers the evidence automatically*
from the durable event log (allowed vs. denied authorization checks, scope violations) since the
role's last review, then feeds it through the same reviewed-and-tested `record_review` machinery.

The result: clean evidence (enough allowed actions, zero violations) auto-proposes an autonomy
upgrade decision for human approval; a violation in the window auto-downgrades immediately. No
human bookkeeping, same governance guarantees.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event
from .role_governance import RoleGovernanceService

# Event types that count as evidence, mapped onto the governance review metric bundle.
SUCCESS_EVENTS = ("role.scope_allowed", "role.autonomy_allowed")
POLICY_VIOLATION_EVENTS = ("role.autonomy_denied",)
SCOPE_VIOLATION_EVENTS = ("role.scope_violation",)
TRUST_EVIDENCE_EVENT_TYPES = {
    "successful_run": "role.execution_succeeded",
    "duplicate_effect_attempt": "role.duplicate_effect_attempt",
    "hidden_failure": "role.hidden_failure_detected",
    "low_quality_output": "role.low_quality_output_detected",
}
DUPLICATE_EFFECT_EVENTS = ("role.duplicate_effect_attempt",)
HIDDEN_FAILURE_EVENTS = ("role.hidden_failure_detected",)
LOW_QUALITY_OUTPUT_EVENTS = ("role.low_quality_output_detected",)
EVIDENCE_SAMPLE_LIMIT = 25


class AutonomyEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.governance = RoleGovernanceService(settings)

    def evaluate(
        self,
        connection: Connection,
        *,
        project_id: str,
        role_key: str,
        actor: str,
    ) -> dict[str, Any]:
        """Gather event-log evidence for a role since its last review and run a governance review,
        which auto-proposes an upgrade decision or auto-downgrades on a detected violation."""
        role = self.governance.get_role(connection, project_id, role_key)
        role_id = role["id"]
        since = connection.execute(
            text("SELECT last_reviewed_at FROM roles WHERE id = :role_id"),
            {"role_id": role_id},
        ).scalar()

        counts = self._event_counts(connection, project_id, role_id, since)
        evidence_refs = self._evidence_refs(connection, project_id, role_id, since)

        successful_runs = sum(counts.get(t, 0) for t in SUCCESS_EVENTS)
        successful_runs += counts.get(TRUST_EVIDENCE_EVENT_TYPES["successful_run"], 0)
        policy_violations = sum(counts.get(t, 0) for t in POLICY_VIOLATION_EVENTS)
        scope_violations = sum(counts.get(t, 0) for t in SCOPE_VIOLATION_EVENTS)
        duplicate_effect_attempts = sum(
            counts.get(event_type, 0) for event_type in DUPLICATE_EFFECT_EVENTS
        )
        hidden_failures = sum(
            counts.get(event_type, 0) for event_type in HIDDEN_FAILURE_EVENTS
        )
        low_quality_outputs = sum(
            counts.get(event_type, 0) for event_type in LOW_QUALITY_OUTPUT_EVENTS
        )

        metrics = {
            "successful_runs": successful_runs,
            "policy_violations": policy_violations,
            "scope_violations": scope_violations,
            "duplicate_effect_attempts": duplicate_effect_attempts,
            "hidden_failures": hidden_failures,
            "low_quality_outputs": low_quality_outputs,
            "evidence_window_since": since.isoformat() if since else None,
            "evidence_event_counts": counts,
        }
        window_label = since.isoformat() if since else "role inception"
        summary = (
            f"Evidence-based autonomy evaluation for {role['display_name']}: "
            f"{successful_runs} allowed actions, {policy_violations} denied actions, "
            f"{scope_violations} scope violations, {duplicate_effect_attempts} duplicate "
            f"effect attempts, {hidden_failures} hidden failures, and {low_quality_outputs} "
            f"low-quality outputs observed since {window_label}."
        )
        detail = self.governance.record_review(
            connection,
            project_id=project_id,
            role_key=role_key,
            actor=actor,
            summary=summary,
            metrics=metrics,
            evidence_refs=evidence_refs,
        )
        detail["evidence"] = {
            "since": since.isoformat() if since else None,
            "event_counts": counts,
            "successful_runs": successful_runs,
            "policy_violations": policy_violations,
            "scope_violations": scope_violations,
            "duplicate_effect_attempts": duplicate_effect_attempts,
            "hidden_failures": hidden_failures,
            "low_quality_outputs": low_quality_outputs,
            "evidence_refs": evidence_refs,
        }
        return detail

    def record_trust_evidence(
        self,
        connection: Connection,
        *,
        project_id: str,
        role_key: str,
        evidence_type: str,
        evidence_ref: str,
        actor: str,
        detail: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Record one idempotent, attributable observation for a role trust review.

        This writes only a durable audit event.  It never mutates autonomy or trust directly;
        `evaluate` remains the sole deterministic consumer and upgrades still require a decision.
        """
        event_type = TRUST_EVIDENCE_EVENT_TYPES.get(evidence_type)
        if event_type is None:
            raise ValueError("Unsupported trust evidence type.")
        evidence_ref = evidence_ref.strip()
        if not evidence_ref:
            raise ValueError("evidence_ref is required.")
        role = self.governance.get_role(connection, project_id, role_key)
        existing = connection.execute(
            text(
                """
                SELECT id
                FROM events
                WHERE project_id = :project_id
                  AND entity_type = 'role'
                  AND entity_id = :role_id
                  AND event_type = :event_type
                  AND data ->> 'evidence_ref' = :evidence_ref
                ORDER BY created_at ASC
                LIMIT 1
                """
            ),
            {
                "project_id": project_id,
                "role_id": role["id"],
                "event_type": event_type,
                "evidence_ref": evidence_ref,
            },
        ).scalar()
        if existing:
            return {"event_id": existing, "recorded": False, "event_type": event_type}

        event_id = emit_event(
            connection,
            project_id,
            event_type,
            actor,
            "role",
            role["id"],
            {
                "role_key": role_key,
                "evidence_type": evidence_type,
                "evidence_ref": evidence_ref,
                "detail": detail or {},
            },
        )
        return {"event_id": event_id, "recorded": True, "event_type": event_type}

    def _event_counts(
        self,
        connection: Connection,
        project_id: str,
        role_id: str,
        since: Optional[Any],
    ) -> dict[str, int]:
        rows = connection.execute(
            text(
                """
                SELECT event_type, count(*) AS n
                FROM events
                WHERE project_id = :project_id
                  AND entity_type = 'role'
                  AND entity_id = :role_id
                  AND (CAST(:since AS timestamptz) IS NULL OR created_at > CAST(:since AS timestamptz))
                GROUP BY event_type
                """
            ),
            {"project_id": project_id, "role_id": role_id, "since": since},
        ).mappings().all()
        return {row["event_type"]: int(row["n"]) for row in rows}

    def _evidence_refs(
        self,
        connection: Connection,
        project_id: str,
        role_id: str,
        since: Optional[Any],
    ) -> list[str]:
        rows = connection.execute(
            text(
                """
                SELECT id
                FROM events
                WHERE project_id = :project_id
                  AND entity_type = 'role'
                  AND entity_id = :role_id
                  AND (CAST(:since AS timestamptz) IS NULL OR created_at > CAST(:since AS timestamptz))
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {
                "project_id": project_id,
                "role_id": role_id,
                "since": since,
                "limit": EVIDENCE_SAMPLE_LIMIT,
            },
        ).all()
        refs = [f"event:{row[0]}" for row in rows]
        if not refs:
            refs = ["autonomy_engine:no_events_in_window"]
        return refs
