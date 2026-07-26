from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class EventSink(Protocol):
    """Destination for immutable engine events."""

    def emit(
        self,
        connection: Any,
        project_id: str,
        event_type: str,
        actor: str,
        entity_type: str,
        entity_id: str,
        data: dict[str, Any],
    ) -> str: ...


@runtime_checkable
class DecisionSink(Protocol):
    """Destination for hold-default human decisions."""

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
    ) -> str: ...
