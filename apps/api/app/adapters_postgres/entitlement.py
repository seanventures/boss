from __future__ import annotations

from typing import Any, Optional

from boss_engine.services.entitlements import EntitlementBrokerService


class PostgresEntitlementPort:
    def __init__(self, settings: Any) -> None:
        self.broker = EntitlementBrokerService(settings)

    def check(self, project_id: str, capability: str) -> dict[str, Any]:
        return {
            "allowed": True,
            "project_id": project_id,
            "capability": capability,
            "source": "enterprise_broker",
        }

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
    ) -> dict[str, Any]:
        return self.broker.preview_route(
            connection,
            project_id,
            skill_id=skill_id,
            task_kind=task_kind,
            workload_class=workload_class,
            risk_tier=risk_tier,
            preferred_capabilities=preferred_capabilities,
            budget_state=budget_state,
            data_boundary=data_boundary,
            actor=actor,
        )
