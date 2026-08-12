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

# Default trust floors for risk tiers when policy omits trust_risk_tiers.
# Fail-closed: higher tiers always require strictly higher floors.
DEFAULT_TRUST_RISK_TIERS: dict[int, int] = {
    0: 0,
    1: 40,
    2: 60,
    3: 80,
}


class AutonomyEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.governance = RoleGovernanceService(settings)

    def get_score(
        self,
        connection: Connection,
        *,
        project_id: str,
        role_key: str,
    ) -> dict[str, Any]:
        """G1 — read the current earned-autonomy score for a role (no side effects).

        Surfaces trust_score, autonomy_level, max risk tier the score currently
        unlocks, and the trust floors used for fail-closed gating (G2).
        """
        role = self.governance.get_role(connection, project_id, role_key)
        policy = self.governance._load_autonomy_policy()
        trust_floors = self.trust_risk_tier_floors(policy)
        trust_score = int(role.get("trust_score") or 50)
        autonomy_level = int(role.get("autonomy_level") or 0)
        max_allowed_risk_tier = self.max_allowed_risk_tier(trust_score, trust_floors)
        return {
            "project_id": project_id,
            "role_key": role_key,
            "role_id": role["id"],
            "display_name": role.get("display_name") or role_key,
            "trust_score": trust_score,
            "autonomy_level": autonomy_level,
            "max_allowed_risk_tier": max_allowed_risk_tier,
            "trust_risk_tier_floors": {
                str(tier): floor for tier, floor in sorted(trust_floors.items())
            },
            "successful_runs": int(role.get("successful_runs") or 0),
            "policy_violation_count": int(role.get("policy_violation_count") or 0),
            "scope_violation_count": int(role.get("scope_violation_count") or 0),
            "duplicate_effect_attempt_count": int(
                role.get("duplicate_effect_attempt_count") or 0
            ),
            "hidden_failure_count": int(role.get("hidden_failure_count") or 0),
            "low_quality_output_count": int(role.get("low_quality_output_count") or 0),
            "last_reviewed_at": role.get("last_reviewed_at"),
            "score_exists": True,
            "gate_mode": "fail_closed",
        }

    @staticmethod
    def trust_risk_tier_floors(policy: dict[str, Any]) -> dict[int, int]:
        """Resolve min trust_score required per risk_tier (0–3). Fail-closed defaults."""
        raw = policy.get("trust_risk_tiers") or {}
        floors = dict(DEFAULT_TRUST_RISK_TIERS)
        for key, value in raw.items():
            try:
                tier = int(key)
                floor = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= tier <= 3 and 0 <= floor <= 100:
                floors[tier] = floor
        # Enforce monotonic floors so a higher tier never requires less trust.
        running = 0
        for tier in range(0, 4):
            running = max(running, floors.get(tier, 0))
            floors[tier] = running
        return floors

    @staticmethod
    def max_allowed_risk_tier(
        trust_score: int, floors: dict[int, int]
    ) -> int:
        """Highest risk tier unlocked by the current trust score."""
        allowed = 0
        for tier in range(0, 4):
            if trust_score >= int(floors.get(tier, 0)):
                allowed = tier
        return allowed

    @staticmethod
    def trust_allows_risk_tier(
        trust_score: int, risk_tier: int, floors: dict[int, int]
    ) -> tuple[bool, int]:
        """Return (allowed, min_trust_required) for a risk tier. Fail-closed."""
        if risk_tier < 0 or risk_tier > 3:
            return False, 100
        required = int(floors.get(risk_tier, DEFAULT_TRUST_RISK_TIERS.get(risk_tier, 100)))
        return trust_score >= required, required

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
