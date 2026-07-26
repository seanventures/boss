from __future__ import annotations

from typing import Any, Optional


class AllowAllEntitlementPort:
    """OSS entitlement boundary for user-supplied providers."""

    def __init__(self, model_id: str = "user-supplied-model") -> None:
        self.model_id = model_id

    def check(self, project_id: str, capability: str) -> dict[str, Any]:
        return {
            "allowed": True,
            "project_id": project_id,
            "capability": capability,
            "source": "oss_allow_all",
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
        del connection
        return {
            "project_id": project_id,
            "source_id": "oss_user_supplied",
            "source_kind": "user_supplied",
            "model_id": self.model_id,
            "route_decision_log_id": "oss_unmetered_route",
            "skill_id": skill_id,
            "task_kind": task_kind,
            "workload_class": workload_class,
            "risk_tier": risk_tier,
            "preferred_capabilities": preferred_capabilities or [],
            "budget_state": budget_state or "unmetered",
            "data_boundary": data_boundary,
            "actor": actor,
            "rejected_sources": [],
        }
