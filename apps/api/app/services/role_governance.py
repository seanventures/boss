from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_artifact, emit_event, make_id, now_utc
from .specpack import RoleNotFoundError


class RoleGovernanceService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def get_governance(
        self, connection: Connection, project_id: str, role_key: str
    ) -> dict[str, Any]:
        role = self.get_role(connection, project_id, role_key)
        policy = self._load_autonomy_policy()
        recent_reviews = self._list_reviews(connection, project_id, role_key, limit=10)
        recent_changes = self._list_autonomy_changes(
            connection, project_id, role_key, limit=10
        )
        pending_change = next(
            (item for item in recent_changes if item["status"] == "pending_decision"),
            None,
        )
        return {
            "project_id": project_id,
            "role": role,
            "policy": {
                "levels": policy.get("levels") or {},
                "upgrade_requires": policy.get("upgrade_requires") or {},
                "downgrade_triggers": policy.get("downgrade_triggers") or [],
            },
            "pending_autonomy_change": pending_change,
            "recent_reviews": recent_reviews,
            "recent_autonomy_changes": recent_changes,
        }

    def record_review(
        self,
        connection: Connection,
        *,
        project_id: str,
        role_key: str,
        actor: str,
        summary: str,
        metrics: dict[str, Any],
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        actor = self._require_text(actor, "actor")
        summary = self._require_text(summary, "summary")
        metrics = self._require_non_empty_mapping(metrics, "metrics")
        evidence_refs = self._require_non_empty_string_list(
            evidence_refs, "evidence_refs"
        )
        normalized_metrics = self._normalize_metrics(metrics)
        role = self.get_role(connection, project_id, role_key, for_update=True)
        policy = self._load_autonomy_policy()
        timestamp = now_utc()

        trust_score_before = int(role.get("trust_score") or 50)
        trust_delta = self._calculate_trust_delta(normalized_metrics, policy)
        trust_score_after = self._clamp_score(trust_score_before + trust_delta)
        autonomy_level_before = int(role["autonomy_level"])
        autonomy_level_after = autonomy_level_before
        governance_outcome = "stable"
        autonomy_change_id: Optional[str] = None
        decision_id: Optional[str] = None

        downgrade_trigger = self._downgrade_trigger(normalized_metrics, policy)
        if downgrade_trigger:
            governance_outcome = "drift_detected"
            if autonomy_level_before > 0:
                autonomy_level_after = autonomy_level_before - 1
                governance_outcome = "auto_downgraded"
                autonomy_change_id = self._create_autonomy_change(
                    connection,
                    project_id=project_id,
                    role=role,
                    change_type="downgrade",
                    trigger=downgrade_trigger,
                    from_level=autonomy_level_before,
                    to_level=autonomy_level_after,
                    status="applied",
                    reason="Automatic autonomy downgrade after governance review detected a violation trigger.",
                    evidence_refs=evidence_refs,
                    metrics=normalized_metrics,
                    actor=actor,
                    approved_by=actor,
                )
        elif self._eligible_for_upgrade(role, normalized_metrics, policy):
            existing_pending = self._pending_autonomy_change(
                connection, project_id, role_key
            )
            if existing_pending is None:
                autonomy_change_id = self._create_autonomy_change(
                    connection,
                    project_id=project_id,
                    role=role,
                    change_type="upgrade",
                    trigger="review_eligibility",
                    from_level=autonomy_level_before,
                    to_level=autonomy_level_before + 1,
                    status="pending_decision",
                    reason="Role met autonomy ladder upgrade requirements and needs explicit approval.",
                    evidence_refs=evidence_refs,
                    metrics=normalized_metrics,
                    actor=actor,
                )
                decision_id = self._create_autonomy_change_decision(
                    connection,
                    project_id=project_id,
                    role=role,
                    autonomy_change_id=autonomy_change_id,
                    from_level=autonomy_level_before,
                    to_level=autonomy_level_before + 1,
                    trust_score_after=trust_score_after,
                    actor=actor,
                )
                connection.execute(
                    text(
                        """
                        UPDATE autonomy_changes
                        SET decision_id = :decision_id,
                            updated_at = :updated_at
                        WHERE id = :autonomy_change_id
                        """
                    ),
                    {
                        "decision_id": decision_id,
                        "updated_at": timestamp,
                        "autonomy_change_id": autonomy_change_id,
                    },
                )
                governance_outcome = "upgrade_recommended"
            else:
                autonomy_change_id = existing_pending["id"]
                decision_id = existing_pending.get("decision_id")
                governance_outcome = "upgrade_pending"

        connection.execute(
            text(
                """
                UPDATE roles
                SET trust_score = :trust_score,
                    autonomy_level = :autonomy_level,
                    successful_runs = successful_runs + :successful_runs,
                    policy_violation_count = policy_violation_count + :policy_violations,
                    duplicate_effect_attempt_count = duplicate_effect_attempt_count + :duplicate_effect_attempts,
                    hidden_failure_count = hidden_failure_count + :hidden_failures,
                    low_quality_output_count = low_quality_output_count + :low_quality_outputs,
                    scope_violation_count = scope_violation_count + :scope_violations,
                    last_reviewed_at = :last_reviewed_at,
                    last_violation_at = COALESCE(:last_violation_at, last_violation_at)
                WHERE id = :role_id
                """
            ),
            {
                "role_id": role["id"],
                "trust_score": trust_score_after,
                "autonomy_level": autonomy_level_after,
                "successful_runs": normalized_metrics["successful_runs"],
                "policy_violations": normalized_metrics["policy_violations"],
                "duplicate_effect_attempts": normalized_metrics[
                    "duplicate_effect_attempts"
                ],
                "hidden_failures": normalized_metrics["hidden_failures"],
                "low_quality_outputs": normalized_metrics["low_quality_outputs"],
                "scope_violations": normalized_metrics["scope_violations"],
                "last_reviewed_at": timestamp,
                "last_violation_at": (timestamp if downgrade_trigger is not None else None),
            },
        )

        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "role_governance_review",
            f"Role governance review for {role['display_name']}",
            {
                "role_id": role["id"],
                "role_key": role["role_key"],
                "summary": summary,
                "metrics": normalized_metrics,
                "evidence_refs": evidence_refs,
                "trust_score_before": trust_score_before,
                "trust_delta": trust_delta,
                "trust_score_after": trust_score_after,
                "autonomy_level_before": autonomy_level_before,
                "autonomy_level_after": autonomy_level_after,
                "governance_outcome": governance_outcome,
                "autonomy_change_id": autonomy_change_id,
                "decision_id": decision_id,
                "created_by": actor,
                "created_at": timestamp.isoformat(),
            },
        )

        review_id = make_id("rrv")
        connection.execute(
            text(
                """
                INSERT INTO role_reviews (
                    id, project_id, role_id, role_key, summary, metrics, evidence_refs,
                    trust_score_before, trust_delta, trust_score_after,
                    autonomy_level_before, autonomy_level_after, governance_outcome,
                    autonomy_change_id, artifact_id, created_by
                )
                VALUES (
                    :id, :project_id, :role_id, :role_key, :summary,
                    CAST(:metrics AS JSONB), CAST(:evidence_refs AS JSONB),
                    :trust_score_before, :trust_delta, :trust_score_after,
                    :autonomy_level_before, :autonomy_level_after, :governance_outcome,
                    :autonomy_change_id, :artifact_id, :created_by
                )
                """
            ),
            {
                "id": review_id,
                "project_id": project_id,
                "role_id": role["id"],
                "role_key": role["role_key"],
                "summary": summary,
                "metrics": json.dumps(normalized_metrics),
                "evidence_refs": json.dumps(evidence_refs),
                "trust_score_before": trust_score_before,
                "trust_delta": trust_delta,
                "trust_score_after": trust_score_after,
                "autonomy_level_before": autonomy_level_before,
                "autonomy_level_after": autonomy_level_after,
                "governance_outcome": governance_outcome,
                "autonomy_change_id": autonomy_change_id,
                "artifact_id": artifact_id,
                "created_by": actor,
            },
        )

        emit_event(
            connection,
            project_id,
            "role.review_recorded",
            actor,
            "role",
            role["id"],
            {
                "role_key": role["role_key"],
                "review_id": review_id,
                "artifact_id": artifact_id,
                "governance_outcome": governance_outcome,
                "trust_delta": trust_delta,
                "trust_score_after": trust_score_after,
            },
        )
        if trust_score_after != trust_score_before:
            emit_event(
                connection,
                project_id,
                "role.trust_changed",
                actor,
                "role",
                role["id"],
                {
                    "role_key": role["role_key"],
                    "before": trust_score_before,
                    "after": trust_score_after,
                    "trust_delta": trust_delta,
                },
            )
        if downgrade_trigger:
            emit_event(
                connection,
                project_id,
                "role.drift_detected",
                actor,
                "role",
                role["id"],
                {
                    "role_key": role["role_key"],
                    "trigger": downgrade_trigger,
                    "autonomy_before": autonomy_level_before,
                    "autonomy_after": autonomy_level_after,
                    "autonomy_change_id": autonomy_change_id,
                },
            )
            if autonomy_change_id is not None:
                emit_event(
                    connection,
                    project_id,
                    "role.autonomy_changed",
                    actor,
                    "role",
                    role["id"],
                    {
                        "role_key": role["role_key"],
                        "change_type": "downgrade",
                        "from_level": autonomy_level_before,
                        "to_level": autonomy_level_after,
                        "autonomy_change_id": autonomy_change_id,
                    },
                )
        return self.get_governance(connection, project_id, role_key)

    def apply_decision_outcome(
        self,
        connection: Connection,
        decision_row: dict[str, Any],
        outcome: str,
        actor: str,
        note: Optional[str],
    ) -> None:
        recommendation = decision_row.get("recommendation") or {}
        if recommendation.get("action") != "role.autonomy_change":
            return

        autonomy_change_id = recommendation.get("autonomy_change_id")
        if not isinstance(autonomy_change_id, str) or not autonomy_change_id:
            return

        change = self._get_autonomy_change(connection, autonomy_change_id, for_update=True)
        timestamp = now_utc()
        resolution_note = note.strip() if isinstance(note, str) and note.strip() else None

        if outcome == "approve":
            connection.execute(
                text(
                    """
                    UPDATE roles
                    SET autonomy_level = :autonomy_level
                    WHERE id = :role_id
                    """
                ),
                {
                    "role_id": change["role_id"],
                    "autonomy_level": change["to_level"],
                },
            )
            artifact_id = create_artifact(
                connection,
                self.settings,
                change["project_id"],
                decision_row.get("task_id"),
                "role_autonomy_change",
                f"Autonomy change for {change['role_key']}",
                {
                    "autonomy_change_id": change["id"],
                    "decision_id": decision_row["id"],
                    "role_key": change["role_key"],
                    "change_type": change["change_type"],
                    "from_level": change["from_level"],
                    "to_level": change["to_level"],
                    "approved_by": actor,
                    "approved_at": timestamp.isoformat(),
                    "note": resolution_note,
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE autonomy_changes
                    SET status = 'applied',
                        approved_by = :approved_by,
                        approved_at = :approved_at,
                        updated_at = :updated_at
                    WHERE id = :autonomy_change_id
                    """
                ),
                {
                    "approved_by": actor,
                    "approved_at": timestamp,
                    "updated_at": timestamp,
                    "autonomy_change_id": change["id"],
                },
            )
            emit_event(
                connection,
                change["project_id"],
                "role.autonomy_changed",
                actor,
                "role",
                change["role_id"],
                {
                    "role_key": change["role_key"],
                    "change_type": change["change_type"],
                    "from_level": change["from_level"],
                    "to_level": change["to_level"],
                    "autonomy_change_id": change["id"],
                    "artifact_id": artifact_id,
                },
            )
            return

        next_status = "rejected" if outcome == "reject" else "revision_requested"
        event_type = (
            "role.autonomy_change_rejected"
            if outcome == "reject"
            else "role.autonomy_change_revision_requested"
        )
        connection.execute(
            text(
                """
                UPDATE autonomy_changes
                SET status = :status,
                    updated_at = :updated_at
                WHERE id = :autonomy_change_id
                """
            ),
            {
                "status": next_status,
                "updated_at": timestamp,
                "autonomy_change_id": change["id"],
            },
        )
        emit_event(
            connection,
            change["project_id"],
            event_type,
            actor,
            "role",
            change["role_id"],
            {
                "role_key": change["role_key"],
                "autonomy_change_id": change["id"],
                "decision_id": decision_row["id"],
                "note": resolution_note,
            },
        )

    def get_role(
        self,
        connection: Connection,
        project_id: str,
        role_key: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        lock_clause = "FOR UPDATE" if for_update else ""
        row = connection.execute(
            text(
                f"""
                SELECT id, project_id, role_key, display_name, autonomy_level, spec, created_at,
                       trust_score, successful_runs, policy_violation_count,
                       duplicate_effect_attempt_count, hidden_failure_count,
                       low_quality_output_count, scope_violation_count,
                       source_module_id, source_module_version,
                       last_reviewed_at, last_violation_at
                FROM roles
                WHERE project_id = :project_id AND role_key = :role_key
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                {lock_clause}
                """
            ),
            {"project_id": project_id, "role_key": role_key},
        ).mappings().first()
        if not row:
            raise RoleNotFoundError(f"Role '{role_key}' not installed for project")
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "role_key": row["role_key"],
            "display_name": row["display_name"],
            "autonomy_level": int(row["autonomy_level"]),
            "spec": row["spec"] or {},
            "source_module_id": row.get("source_module_id"),
            "source_module_version": row.get("source_module_version"),
            "trust_score": int(row.get("trust_score") or 50),
            "successful_runs": int(row.get("successful_runs") or 0),
            "policy_violation_count": int(row.get("policy_violation_count") or 0),
            "duplicate_effect_attempt_count": int(
                row.get("duplicate_effect_attempt_count") or 0
            ),
            "hidden_failure_count": int(row.get("hidden_failure_count") or 0),
            "low_quality_output_count": int(
                row.get("low_quality_output_count") or 0
            ),
            "scope_violation_count": int(row.get("scope_violation_count") or 0),
            "last_reviewed_at": (
                row["last_reviewed_at"].isoformat()
                if row.get("last_reviewed_at")
                else None
            ),
            "last_violation_at": (
                row["last_violation_at"].isoformat()
                if row.get("last_violation_at")
                else None
            ),
            "created_at": row["created_at"].isoformat(),
        }

    def _list_reviews(
        self, connection: Connection, project_id: str, role_key: str, *, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, role_key, summary, metrics, evidence_refs, trust_score_before,
                       trust_delta, trust_score_after, autonomy_level_before,
                       autonomy_level_after, governance_outcome, autonomy_change_id,
                       artifact_id, created_by, created_at
                FROM role_reviews
                WHERE project_id = :project_id AND role_key = :role_key
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "role_key": role_key, "limit": limit},
        ).mappings().all()
        return [
            {
                "id": row["id"],
                "role_key": row["role_key"],
                "summary": row["summary"],
                "metrics": row["metrics"] or {},
                "evidence_refs": row["evidence_refs"] or [],
                "trust_score_before": int(row["trust_score_before"]),
                "trust_delta": int(row["trust_delta"]),
                "trust_score_after": int(row["trust_score_after"]),
                "autonomy_level_before": int(row["autonomy_level_before"]),
                "autonomy_level_after": int(row["autonomy_level_after"]),
                "governance_outcome": row["governance_outcome"],
                "autonomy_change_id": row["autonomy_change_id"],
                "artifact_id": row["artifact_id"],
                "created_by": row["created_by"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]

    def _list_autonomy_changes(
        self, connection: Connection, project_id: str, role_key: str, *, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, role_key, decision_id, change_type, trigger, from_level, to_level,
                       status, reason, evidence_refs, metrics, created_by, approved_by,
                       approved_at, created_at, updated_at
                FROM autonomy_changes
                WHERE project_id = :project_id AND role_key = :role_key
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "role_key": role_key, "limit": limit},
        ).mappings().all()
        return [self._serialize_autonomy_change(row) for row in rows]

    def _pending_autonomy_change(
        self, connection: Connection, project_id: str, role_key: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, role_key, decision_id, change_type, trigger, from_level, to_level,
                       status, reason, evidence_refs, metrics, created_by, approved_by,
                       approved_at, created_at, updated_at
                FROM autonomy_changes
                WHERE project_id = :project_id
                  AND role_key = :role_key
                  AND status = 'pending_decision'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "role_key": role_key},
        ).mappings().first()
        if not row:
            return None
        return self._serialize_autonomy_change(row)

    def _get_autonomy_change(
        self, connection: Connection, autonomy_change_id: str, *, for_update: bool = False
    ) -> dict[str, Any]:
        lock_clause = "FOR UPDATE" if for_update else ""
        row = connection.execute(
            text(
                f"""
                SELECT id, project_id, role_id, role_key, decision_id, change_type, trigger,
                       from_level, to_level, status, reason, evidence_refs, metrics,
                       created_by, approved_by, approved_at, created_at, updated_at
                FROM autonomy_changes
                WHERE id = :autonomy_change_id
                {lock_clause}
                """
            ),
            {"autonomy_change_id": autonomy_change_id},
        ).mappings().first()
        if not row:
            raise ValueError("Autonomy change not found")
        return dict(row)

    def _create_autonomy_change(
        self,
        connection: Connection,
        *,
        project_id: str,
        role: dict[str, Any],
        change_type: str,
        trigger: str,
        from_level: int,
        to_level: int,
        status: str,
        reason: str,
        evidence_refs: list[str],
        metrics: dict[str, Any],
        actor: str,
        approved_by: Optional[str] = None,
    ) -> str:
        autonomy_change_id = make_id("rac")
        timestamp = now_utc()
        connection.execute(
            text(
                """
                INSERT INTO autonomy_changes (
                    id, project_id, role_id, role_key, change_type, trigger, from_level,
                    to_level, status, reason, evidence_refs, metrics, created_by,
                    approved_by, approved_at
                )
                VALUES (
                    :id, :project_id, :role_id, :role_key, :change_type, :trigger,
                    :from_level, :to_level, :status, :reason,
                    CAST(:evidence_refs AS JSONB), CAST(:metrics AS JSONB), :created_by,
                    :approved_by, :approved_at
                )
                """
            ),
            {
                "id": autonomy_change_id,
                "project_id": project_id,
                "role_id": role["id"],
                "role_key": role["role_key"],
                "change_type": change_type,
                "trigger": trigger,
                "from_level": from_level,
                "to_level": to_level,
                "status": status,
                "reason": reason,
                "evidence_refs": json.dumps(evidence_refs),
                "metrics": json.dumps(metrics),
                "created_by": actor,
                "approved_by": approved_by,
                "approved_at": timestamp if approved_by else None,
            },
        )
        return autonomy_change_id

    def _create_autonomy_change_decision(
        self,
        connection: Connection,
        *,
        project_id: str,
        role: dict[str, Any],
        autonomy_change_id: str,
        from_level: int,
        to_level: int,
        trust_score_after: int,
        actor: str,
    ) -> str:
        decision_id = make_id("dec")
        deadline_at = now_utc().replace(microsecond=0) + self._decision_window()
        question = (
            f"Upgrade {role['display_name']} autonomy from {from_level} to {to_level}?"
        )
        recommendation = {
            "action": "role.autonomy_change",
            "autonomy_change_id": autonomy_change_id,
            "role_id": role["id"],
            "role_key": role["role_key"],
            "from_level": from_level,
            "to_level": to_level,
            "change_type": "upgrade",
            "trust_score_after": trust_score_after,
        }
        options = [
            {"action": "approve", "label": "Approve upgrade"},
            {"action": "revise", "label": "Request more evidence"},
            {"action": "reject", "label": "Keep current autonomy"},
        ]
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options,
                    risk_tier, cost_summary, deadline_at, default_action, status
                )
                VALUES (
                    :id, :project_id, NULL, :question, CAST(:recommendation AS JSONB),
                    CAST(:options AS JSONB), 1, CAST(:cost_summary AS JSONB),
                    :deadline_at, 'hold', 'pending'
                )
                """
            ),
            {
                "id": decision_id,
                "project_id": project_id,
                "question": question,
                "recommendation": json.dumps(recommendation),
                "options": json.dumps(options),
                "cost_summary": json.dumps(
                    {
                        "role_key": role["role_key"],
                        "from_level": from_level,
                        "to_level": to_level,
                        "trust_score_after": trust_score_after,
                    }
                ),
                "deadline_at": deadline_at,
            },
        )
        emit_event(
            connection,
            project_id,
            "decision.created",
            actor,
            "decision",
            decision_id,
            {
                "reason": "role_autonomy_upgrade_review",
                "role_key": role["role_key"],
                "autonomy_change_id": autonomy_change_id,
                "deadline_at": deadline_at.isoformat(),
                "default_action": "hold",
            },
        )
        emit_event(
            connection,
            project_id,
            "role.autonomy_change_requested",
            actor,
            "role",
            role["id"],
            {
                "role_key": role["role_key"],
                "autonomy_change_id": autonomy_change_id,
                "decision_id": decision_id,
                "from_level": from_level,
                "to_level": to_level,
            },
        )
        return decision_id

    def _decision_window(self):
        return timedelta(hours=24)

    def _serialize_autonomy_change(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "role_key": row["role_key"],
            "decision_id": row["decision_id"],
            "change_type": row["change_type"],
            "trigger": row["trigger"],
            "from_level": int(row["from_level"]),
            "to_level": int(row["to_level"]),
            "status": row["status"],
            "reason": row["reason"],
            "evidence_refs": row["evidence_refs"] or [],
            "metrics": row["metrics"] or {},
            "created_by": row["created_by"],
            "approved_by": row["approved_by"],
            "approved_at": (
                row["approved_at"].isoformat() if row.get("approved_at") else None
            ),
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    def _load_autonomy_policy(self) -> dict[str, Any]:
        path = self._policy_path()
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Autonomy ladder policy must load as an object.")
        return payload

    def _policy_path(self) -> Path:
        return self.settings.specpack_root / "policies" / "autonomy_ladder.yaml"

    def _normalize_metrics(self, metrics: dict[str, Any]) -> dict[str, Any]:
        normalized = {
            "successful_runs": self._coerce_int(metrics.get("successful_runs")),
            "policy_violations": self._coerce_int(metrics.get("policy_violations")),
            "duplicate_effect_attempts": self._coerce_int(
                metrics.get("duplicate_effect_attempts")
            ),
            "hidden_failures": self._coerce_int(metrics.get("hidden_failures")),
            "low_quality_outputs": self._coerce_int(
                metrics.get("low_quality_outputs")
            ),
            "scope_violations": self._coerce_int(metrics.get("scope_violations")),
            "rollback_rate": self._coerce_float(metrics.get("rollback_rate")),
        }
        for key, value in metrics.items():
            if key not in normalized:
                normalized[key] = value
        return normalized

    def _coerce_int(self, value: Any) -> int:
        coerced = int(value or 0)
        return max(coerced, 0)

    def _coerce_float(self, value: Any) -> float:
        coerced = float(value or 0.0)
        return max(coerced, 0.0)

    def _calculate_trust_delta(
        self, metrics: dict[str, Any], policy: dict[str, Any]
    ) -> int:
        delta = min(metrics["successful_runs"] // 10, 5)
        if (
            metrics["successful_runs"]
            >= int((policy.get("upgrade_requires") or {}).get("min_successful_runs", 30))
            and metrics["policy_violations"] == 0
            and metrics["duplicate_effect_attempts"] == 0
            and metrics["hidden_failures"] == 0
            and metrics["scope_violations"] == 0
            and metrics["low_quality_outputs"] == 0
            and metrics["rollback_rate"]
            <= float((policy.get("upgrade_requires") or {}).get("rollback_rate_below", 0.02))
        ):
            delta += 10
        delta -= metrics["policy_violations"] * 20
        delta -= metrics["duplicate_effect_attempts"] * 20
        delta -= metrics["hidden_failures"] * 15
        delta -= metrics["scope_violations"] * 15
        delta -= metrics["low_quality_outputs"] * 5
        rollback_threshold = float(
            (policy.get("upgrade_requires") or {}).get("rollback_rate_below", 0.02)
        )
        if metrics["rollback_rate"] > rollback_threshold:
            delta -= max(5, int((metrics["rollback_rate"] - rollback_threshold) * 100))
        return delta

    def _eligible_for_upgrade(
        self, role: dict[str, Any], metrics: dict[str, Any], policy: dict[str, Any]
    ) -> bool:
        levels = policy.get("levels") or {}
        max_level = max((int(level) for level in levels.keys()), default=0)
        if int(role["autonomy_level"]) >= max_level:
            return False
        requirements = policy.get("upgrade_requires") or {}
        return (
            metrics["successful_runs"] >= int(requirements.get("min_successful_runs", 30))
            and metrics["policy_violations"] <= int(requirements.get("policy_violations", 0))
            and metrics["duplicate_effect_attempts"]
            <= int(requirements.get("duplicate_effects", 0))
            and metrics["hidden_failures"] == 0
            and metrics["scope_violations"] == 0
            and metrics["low_quality_outputs"] == 0
            and metrics["rollback_rate"]
            <= float(requirements.get("rollback_rate_below", 0.02))
        )

    def _downgrade_trigger(
        self, metrics: dict[str, Any], policy: dict[str, Any]
    ) -> Optional[str]:
        triggers = set(policy.get("downgrade_triggers") or [])
        if metrics["policy_violations"] > 0 and "policy_violation" in triggers:
            return "policy_violation"
        if (
            metrics["duplicate_effect_attempts"] > 0
            and "duplicate_effect_attempt" in triggers
        ):
            return "duplicate_effect_attempt"
        if metrics["hidden_failures"] > 0 and "hidden_failure" in triggers:
            return "hidden_failure"
        if (
            metrics["low_quality_outputs"] > 0
            and "repeated_low_quality_output" in triggers
        ):
            return "repeated_low_quality_output"
        if metrics["scope_violations"] > 0:
            return "scope_violation"
        return None

    def _clamp_score(self, value: int) -> int:
        return max(0, min(100, value))

    def _require_text(self, value: str, field_name: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{field_name} must not be blank.")
        return cleaned

    def _require_non_empty_string_list(
        self, values: list[str], field_name: str
    ) -> list[str]:
        cleaned = [
            item.strip() for item in values if isinstance(item, str) and item.strip()
        ]
        if not cleaned:
            raise ValueError(f"{field_name} must contain at least one non-empty item.")
        return cleaned

    def _require_non_empty_mapping(
        self, values: dict[str, Any], field_name: str
    ) -> dict[str, Any]:
        if not values:
            raise ValueError(f"{field_name} must contain at least one entry.")
        return values
