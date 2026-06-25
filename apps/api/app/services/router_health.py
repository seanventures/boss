"""Router health + rationale (spec §C: router fallback canary + live provider-health polling +
router rationale as a structured artifact).

The model router (entitlements.preview_route) already ranks eligible sources and emits a fallback
order. This module makes that operable:
  - poll_provider_health: snapshot every model entitlement source's health into provider_health,
    so the console/canaries can see which lanes are live *before* an incident.
  - record_rationale: persist a routing decision (selected source + ranked fallback chain +
    rejected sources with reasons) as a durable `router_rationale` artifact — the structured
    answer to "why did the router pick this lane, and what would it fail over to?".
The router-fallback canary (canaries.py) uses both to prove the fallback chain is coherent.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_artifact, emit_event, make_id, now_utc
from .entitlements import EntitlementBrokerService


class RouterHealthService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.entitlements = EntitlementBrokerService(settings)

    def poll_provider_health(
        self, connection: Connection, project_id: str, actor: str = "router_health"
    ) -> dict[str, Any]:
        """Snapshot the health of every model entitlement source into provider_health."""
        sources = self.entitlements.list_sources(connection, project_id)
        snapshots: list[dict[str, Any]] = []
        timestamp = now_utc()
        for source in sources:
            effective_status = source.get("effective_status")
            runtime_supported = bool(source.get("runtime_supported"))
            healthy = effective_status == "active" and runtime_supported
            if healthy:
                status = "healthy"
                reason = "active and runtime-supported"
            elif effective_status == "active":
                status = "degraded"
                reason = "active but runtime unsupported (missing credentials/runtime)"
            else:
                status = "unavailable"
                reason = f"status={effective_status}"
            detail = {
                "source_id": source.get("source_id"),
                "source_kind": source.get("source_kind"),
                "effective_status": effective_status,
                "runtime_supported": runtime_supported,
                "reason": reason,
            }
            health_id = make_id("ph")
            connection.execute(
                text(
                    """
                    INSERT INTO provider_health (
                        id, project_id, provider_account_id, provider, status,
                        api_available, last_checked_at, detail
                    )
                    VALUES (
                        :id, :project_id, NULL, :provider, :status,
                        :api_available, :last_checked_at, CAST(:detail AS JSONB)
                    )
                    """
                ),
                {
                    "id": health_id,
                    "project_id": project_id,
                    "provider": source.get("provider"),
                    "status": status,
                    "api_available": healthy,
                    "last_checked_at": timestamp,
                    "detail": json.dumps(detail),
                },
            )
            snapshots.append(
                {
                    "id": health_id,
                    "provider": source.get("provider"),
                    "source_id": source.get("source_id"),
                    "status": status,
                    "api_available": healthy,
                    "reason": reason,
                }
            )
        healthy_count = sum(1 for s in snapshots if s["api_available"])
        emit_event(
            connection,
            project_id,
            "router.provider_health_polled",
            actor,
            "provider_health",
            project_id,
            {"sources": len(snapshots), "healthy": healthy_count},
        )
        return {
            "project_id": project_id,
            "polled": len(snapshots),
            "healthy": healthy_count,
            "snapshots": snapshots,
        }

    def record_rationale(
        self,
        connection: Connection,
        project_id: str,
        *,
        skill_id: str = "model.generate_text",
        task_kind: str = "code",
        workload_class: str = "owner_sandbox",
        risk_tier: int = 1,
        actor: str = "router_health",
    ) -> dict[str, Any]:
        """Persist the router's decision for a given routing context as a structured artifact."""
        route = self.entitlements.preview_route(
            connection,
            project_id,
            skill_id=skill_id,
            task_kind=task_kind,
            workload_class=workload_class,
            risk_tier=risk_tier,
            actor=actor,
        )
        decision = route.get("decision") or {}
        content = {
            "skill_id": skill_id,
            "task_kind": task_kind,
            "workload_class": workload_class,
            "risk_tier": risk_tier,
            "route_decision_log_id": route.get("route_decision_log_id"),
            "selected_source_id": route.get("source_id"),
            "selected_provider": route.get("provider"),
            "selected_model_id": route.get("model_id"),
            "denial_reason": route.get("denial_reason"),
            "fallback_order": decision.get("fallback_order") or [],
            "eligible_sources": decision.get("eligible_sources") or [],
            "rejected_sources": route.get("rejected_sources") or [],
            "recorded_by": actor,
            "recorded_at": now_utc().isoformat(),
        }
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "router_rationale",
            f"Router rationale: {skill_id} / {task_kind}",
            content,
        )
        emit_event(
            connection,
            project_id,
            "router.rationale_recorded",
            actor,
            "artifact",
            artifact_id,
            {
                "skill_id": skill_id,
                "selected_source_id": route.get("source_id"),
                "fallback_depth": len(content["fallback_order"]),
            },
        )
        return {"artifact_id": artifact_id, "rationale": content}
