from __future__ import annotations

import logging
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Optional
from uuid import uuid4


class LoggingEventSink:
    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self.logger = logger or logging.getLogger("boss_engine.events")
        self.events: list[dict[str, Any]] = []
        self._lock = RLock()

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
        del connection
        event_id = f"oss_evt_{uuid4().hex[:20]}"
        event = {
            "id": event_id,
            "project_id": project_id,
            "event_type": event_type,
            "actor": actor,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "data": dict(data),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self.events.append(event)
        self.logger.info("boss_engine.event", extra={"boss_event": event})
        return event_id


class InMemoryDecisionSink:
    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = []
        self._lock = RLock()

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
        del connection
        decision_id = f"oss_dec_{uuid4().hex[:20]}"
        rec = dict(recommendation)
        text = str(reason or "").strip()
        if text:
            rec.setdefault("reason_code", text)
            rec.setdefault("reason", text)
        decision = {
            "id": decision_id,
            "project_id": project_id,
            "task_id": task_id,
            "question": question,
            "recommendation": rec,
            "cost_summary": dict(cost_summary),
            "reason": reason,
            "risk_tier": risk_tier,
            "default_action": default_action,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self.decisions.append(decision)
        return decision_id
