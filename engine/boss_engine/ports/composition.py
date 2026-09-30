from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .budget import BudgetPort
from .entitlement import EntitlementPort
from .events import DecisionSink, EventSink
from .persistence import PersistencePort


@dataclass(frozen=True)
class EnginePorts:
    budget: BudgetPort
    entitlement: EntitlementPort
    persistence: PersistencePort
    events: EventSink
    decisions: DecisionSink

    @classmethod
    def oss_defaults(cls, settings: Any) -> "EnginePorts":
        from ..defaults import (
            AllowAllEntitlementPort,
            InMemoryPersistence,
            InMemoryDecisionSink,
            LoggingEventSink,
            NoopBudgetPort,
        )

        return cls(
            budget=NoopBudgetPort(),
            entitlement=AllowAllEntitlementPort(
                model_id=getattr(settings, "openai_model", "user-supplied-model")
            ),
            persistence=InMemoryPersistence(),
            events=LoggingEventSink(),
            decisions=InMemoryDecisionSink(),
        )


_configured_ports: Optional[EnginePorts] = None


def configure_engine_ports(ports: Optional[EnginePorts]) -> None:
    """Set the process composition once at its application boundary."""

    global _configured_ports
    _configured_ports = ports


def get_engine_ports() -> Optional[EnginePorts]:
    return _configured_ports
