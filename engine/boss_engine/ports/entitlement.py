from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class EntitlementPort(Protocol):
    """Capability/model-routing boundary for an engine execution."""

    def check(
        self,
        project_id: str,
        capability: str,
    ) -> dict[str, Any]: ...

    def preview_route(
        self,
        connection: Any,
        project_id: str,
        *,
        skill_id: str,
        task_kind: str,
        workload_class: str,
        risk_tier: int,
        preferred_capabilities: Optional[list[str]] = None,
        budget_state: Optional[str] = None,
        data_boundary: str = "internal",
        actor: str = "system",
    ) -> dict[str, Any]: ...
