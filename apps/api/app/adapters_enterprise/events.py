from __future__ import annotations

from typing import Any, Optional


class PostgresEventSink:
    def emit(
        self,
        connection: Any,
        project_id: str,
        event_type: str,
        actor: str,
        entity_type: str,
        entity_id: str,
        data: dict[str, Any],
    ) -> str:
        from boss_engine.services.core import _emit_event_postgres

        return _emit_event_postgres(
            connection,
            project_id,
            event_type,
            actor,
            entity_type,
            entity_id,
            data,
        )


class PostgresDecisionSink:
    def create_blocking(
        self,
        connection: Any,
        project_id: str,
        task_id: Optional[str],
        question: str,
        recommendation: dict[str, Any],
        cost_summary: dict[str, Any],
        reason: str,
        *,
        risk_tier: int = 2,
        default_action: str = "hold",
    ) -> str:
        from boss_engine.services.core import _create_blocking_decision_postgres

        return _create_blocking_decision_postgres(
            connection,
            project_id,
            task_id,
            question,
            recommendation,
            cost_summary,
            reason,
            risk_tier=risk_tier,
            default_action=default_action,
        )
