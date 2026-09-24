"""Paid model gateway extension for the shared entitlement broker.

Community does not import this module. The broker loads it only when a paid
edition is configured, and missing paid packaging fails at construction.
"""

from __future__ import annotations

from typing import Any

import yaml

from ..config import Settings


class EnterpriseGatewayPolicy:
    source_id = "enterprise_model_gateway"
    source_kind = "enterprise_model_gateway"

    def __init__(self, settings: Settings):
        self.settings = settings
        path = settings.specpack_root / "models" / "enterprise_gateway.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid paid gateway policy: {path}")
        if (payload.get("source_record") or {}).get("source_id") != self.source_id:
            raise ValueError(f"Paid gateway source missing from {path}")
        self.payload = payload

    def merge_registry(self, registry: dict[str, Any]) -> dict[str, Any]:
        provider = next(
            (item for item in registry.get("providers", []) if item.get("id") == "boss"),
            None,
        )
        if provider is None:
            raise ValueError("BOSS provider missing from model registry")
        provider["description"] = self.payload["provider_description"]
        provider.setdefault("source_records", []).insert(
            0, self.payload["source_record"]
        )
        models = registry.setdefault("models", [])
        model_position = next(
            (
                index
                for index, item in enumerate(models)
                if item.get("id") == "boss:managed-cloud-gateway-auto"
            ),
            len(models),
        )
        models.insert(model_position, self.payload["model"])
        routing = registry.setdefault("routing_policy", {})
        routing.setdefault("data_boundaries", {})[
            "enterprise_model_gateway_default"
        ] = self.payload["data_boundary_default"]
        workloads = routing.setdefault("workload_class_defaults", {})
        for workload, position in self.payload["preferred_source_kind_positions"].items():
            kinds = workloads[workload]["preferred_source_kinds"]
            kinds.insert(position, self.source_kind)
        return registry

    def runtime_flags(self) -> tuple[bool, bool]:
        runtime_supported = bool(
            self.settings.enterprise_model_gateway_enabled
            and self.settings.enterprise_model_gateway_url
        )
        return runtime_supported, bool(
            runtime_supported and self.settings.enterprise_model_gateway_token
        )

    def credential_ref(self) -> str:
        return "BOSS_ENTERPRISE_MODEL_GATEWAY_TOKEN"

    def supported_models(self, source_defaults: dict[str, Any]) -> list[str]:
        configured = list(self.settings.enterprise_model_gateway_model_values)
        return configured or list(source_defaults.get("supported_models") or [])

    def routing_modes(self) -> list[str]:
        return ["enterprise_policy", "quality_first", "budget_safe"]

    def data_boundaries(self) -> list[str]:
        return list(self.payload["data_boundary_default"])
