from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import DATA_BOUNDARIES, emit_event, make_id, normalize_data_boundary


class SkillNotFoundError(Exception):
    pass


class EntitlementSourceNotFoundError(Exception):
    pass


class EntitlementBrokerService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{path} did not contain an object payload")
        return payload

    def _provider_registry(self) -> dict[str, Any]:
        return self._load_yaml(self.settings.specpack_root / "models" / "providers.yaml")

    def _source_record_defaults(self) -> dict[str, dict[str, Any]]:
        defaults: dict[str, dict[str, Any]] = {}
        registry = self._provider_registry()
        for provider in registry.get("providers", []):
            provider_id = provider.get("id")
            if not isinstance(provider_id, str):
                continue
            for source_record in provider.get("source_records", []):
                source_id = source_record.get("source_id")
                if not isinstance(source_id, str):
                    continue
                defaults[source_id] = {
                    **source_record,
                    "provider": provider_id,
                    "provider_description": provider.get("description", ""),
                }
        return defaults

    def _skill_files(self) -> list[Path]:
        return sorted((self.settings.specpack_root / "skills").glob("*.yaml"))

    def list_skills(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in self._skill_files():
            payload = self._load_yaml(path)
            for skill in payload.get("skills", []):
                if not isinstance(skill, dict):
                    continue
                self.validate_skill_spec(skill, str(path))
                items.append(
                    {
                        "id": skill.get("id", path.stem),
                        "kind": skill.get("kind", "unknown"),
                        "risk_tier": int(skill.get("risk_tier", 0)),
                        "description": skill.get("description", ""),
                        "mock_mode": bool(skill.get("mock_mode", False)),
                        "input_schema": skill.get("input_schema", {}) or {},
                        "output_schema": skill.get("output_schema", {}) or {},
                        "cost": skill.get("cost", {}) or {},
                        "retry": skill.get("retry", {}) or {},
                        "permissions": skill.get("permissions", {}) or {},
                        "data_boundary": skill.get("data_boundary", "internal"),
                        "policy": skill.get("policy", {}) or {},
                    }
                )
        items.sort(key=lambda item: item["id"])
        return items

    def validate_skill_spec(self, skill: dict[str, Any], source: str) -> None:
        errors: list[str] = []
        for key in ("id", "kind", "description", "data_boundary"):
            if not isinstance(skill.get(key), str) or not skill[key].strip():
                errors.append(f"{key} must be a non-empty string")

        risk_tier = skill.get("risk_tier")
        if not isinstance(risk_tier, int) or risk_tier < 0 or risk_tier > 3:
            errors.append("risk_tier must be an integer between 0 and 3")

        if skill.get("mock_mode") is not False:
            errors.append("mock_mode must be false in real-only runtime specs")

        for key in (
            "input_schema",
            "output_schema",
            "cost",
            "retry",
            "permissions",
            "policy",
        ):
            if not isinstance(skill.get(key), dict) or not skill[key]:
                errors.append(f"{key} must be a non-empty object")

        cost = skill.get("cost") if isinstance(skill.get("cost"), dict) else {}
        if not isinstance(cost.get("estimator"), str) or not cost["estimator"].strip():
            errors.append("cost.estimator must be a non-empty string")
        if not isinstance(cost.get("category"), str) or not cost["category"].strip():
            errors.append("cost.category must be a non-empty string")

        retry = skill.get("retry") if isinstance(skill.get("retry"), dict) else {}
        max_attempts = retry.get("max_attempts")
        if not isinstance(max_attempts, int) or max_attempts < 1:
            errors.append("retry.max_attempts must be a positive integer")
        if not isinstance(retry.get("backoff"), str) or not retry["backoff"].strip():
            errors.append("retry.backoff must be a non-empty string")

        permissions = (
            skill.get("permissions") if isinstance(skill.get("permissions"), dict) else {}
        )
        for key in ("allowed_callers", "required_permissions"):
            value = permissions.get(key)
            if not isinstance(value, list) or not value:
                errors.append(f"permissions.{key} must be a non-empty list")
            elif any(not isinstance(item, str) or not item.strip() for item in value):
                errors.append(
                    f"permissions.{key} must contain only non-empty strings"
                )

        if errors:
            skill_id = skill.get("id", "<unknown>")
            raise ValueError(f"SkillSpec {skill_id} in {source} is invalid: {'; '.join(errors)}")

    def get_skill(self, skill_id: str) -> dict[str, Any]:
        for skill in self.list_skills():
            if skill["id"] == skill_id:
                return skill
        raise SkillNotFoundError(f"Skill '{skill_id}' not found")

    def _runtime_flags(self, source_id: str) -> tuple[bool, bool]:
        if source_id == "openai_api":
            return True, bool(self.settings.openai_api_key)
        if source_id == "anthropic_api":
            return False, bool(self.settings.anthropic_api_key)
        if source_id == "google_api":
            return False, bool(self.settings.google_api_key)
        if source_id == "xai_api":
            return False, bool(self.settings.xai_api_key)
        if source_id == "cursor_cli":
            return False, bool(self.settings.cursor_cli_enabled)
        if source_id == "approved_local_model_host":
            local_ready = bool(
                self.settings.local_model_enabled and self.settings.local_model_id
            )
            return local_ready, local_ready
        return False, False

    def _credential_ref(self, source_id: str) -> Optional[str]:
        if source_id == "openai_api":
            return "OPENAI_API_KEY"
        if source_id == "anthropic_api":
            return "ANTHROPIC_API_KEY"
        if source_id == "google_api":
            return "GOOGLE_API_KEY"
        if source_id == "xai_api":
            return "XAI_API_KEY"
        if source_id == "cursor_cli":
            return "BOSS_CURSOR_CLI_ENABLED"
        if source_id == "approved_local_model_host":
            return "BOSS_LOCAL_MODEL_ID"
        return None

    def _effective_status(self, row: dict[str, Any]) -> str:
        stored_status = row.get("status") or "declared"
        runtime_supported, credential_present = self._runtime_context(row)
        if stored_status in {"inactive", "disabled"}:
            return stored_status
        if runtime_supported and credential_present:
            return "active"
        if runtime_supported and not credential_present:
            return "missing_credentials"
        if stored_status == "active" and not runtime_supported:
            return "runtime_unavailable"
        if stored_status == "missing_credentials" and credential_present and runtime_supported:
            return "active"
        return stored_status

    def _runtime_context(self, row: dict[str, Any]) -> tuple[bool, bool]:
        computed_runtime_supported, computed_credential_present = self._runtime_flags(
            row["source_id"]
        )
        observed_capacity = row.get("observed_capacity") or {}
        runtime_supported = bool(row.get("runtime_supported", False)) or bool(
            computed_runtime_supported
        )
        credential_present = bool(observed_capacity.get("credential_present", False)) or bool(
            computed_credential_present
        )
        return runtime_supported, credential_present

    def seed_default_sources(
        self, connection: Connection, project_id: str, actor: str = "bootstrap"
    ) -> None:
        for source_id, source_defaults in self._source_record_defaults().items():
            runtime_supported, credential_present = self._runtime_flags(source_id)
            stored_status = "active" if runtime_supported and credential_present else "declared"
            if runtime_supported and not credential_present:
                stored_status = "missing_credentials"
            self.upsert_source(
                connection,
                {
                    "project_id": project_id,
                    "provider": source_defaults["provider"],
                    "source_id": source_id,
                    "source_kind": source_defaults.get("source_kind", "official_api"),
                    "entitlement_kind": source_defaults.get(
                        "entitlement_kind", "usage_based_api"
                    ),
                    "owner_type": "workspace_operator"
                    if source_defaults.get("source_kind") != "official_api"
                    else "project",
                    "owner_id": "founder" if source_defaults.get("source_kind") != "official_api" else project_id,
                    "workload_classes_allowed": source_defaults.get(
                        "workload_classes_allowed", []
                    ),
                    "multi_tenant_allowed": bool(
                        source_defaults.get("multi_tenant_allowed", False)
                    ),
                    "credential_ref": self._credential_ref(source_id),
                    "cost_mode": source_defaults.get("entitlement_kind", "usage_based_api"),
                    "limit_shape": {},
                    "supported_models": source_defaults.get("supported_models", []),
                    "task_strengths": source_defaults.get("task_strengths", []),
                    "risk_tier_max": 3
                    if source_defaults.get("source_kind") == "official_api"
                    else 1,
                    "status": stored_status,
                    "policy_notes": source_defaults.get("policy_notes", []),
                    "runtime_supported": runtime_supported,
                    "observed_capacity": {
                        "credential_present": credential_present,
                        "runtime_supported": runtime_supported,
                    },
                },
                actor=actor,
                emit_events=False,
            )

    def list_sources(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, provider, source_id, source_kind, entitlement_kind,
                       owner_type, owner_id, workload_classes_allowed, multi_tenant_allowed,
                       credential_ref, cost_mode, limit_shape, supported_models,
                       task_strengths, risk_tier_max, status, policy_notes,
                       observed_capacity, runtime_supported, created_at, updated_at
                FROM entitlement_sources
                WHERE project_id = :project_id
                ORDER BY provider ASC, source_id ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["effective_status"] = self._effective_status(payload)
            runtime_supported, credential_present = self._runtime_context(payload)
            payload["runtime_supported"] = runtime_supported
            payload["observed_capacity"] = {
                **(payload.get("observed_capacity") or {}),
                "credential_present": credential_present,
                "runtime_supported": runtime_supported,
            }
            payload["created_at"] = payload["created_at"].isoformat()
            payload["updated_at"] = payload["updated_at"].isoformat()
            items.append(payload)
        return items

    def upsert_source(
        self,
        connection: Connection,
        payload: dict[str, Any],
        *,
        actor: str,
        emit_events: bool = True,
    ) -> dict[str, Any]:
        defaults = self._source_record_defaults().get(payload["source_id"], {})
        record = {**defaults, **payload}
        computed_runtime_supported, computed_credential_present = self._runtime_flags(
            record["source_id"]
        )
        runtime_supported = bool(record.get("runtime_supported", False)) or bool(
            computed_runtime_supported
        )
        observed_capacity = record.get("observed_capacity", {}) or {}
        credential_present = bool(observed_capacity.get("credential_present", False)) or bool(
            computed_credential_present
        )
        source_pk = f"src_{record['project_id']}_{record['source_id']}"
        connection.execute(
            text(
                """
                INSERT INTO entitlement_sources (
                    id, project_id, provider, source_id, source_kind, entitlement_kind,
                    owner_type, owner_id, workload_classes_allowed, multi_tenant_allowed,
                    credential_ref, cost_mode, limit_shape, supported_models, task_strengths,
                    risk_tier_max, status, policy_notes, observed_capacity, runtime_supported
                )
                VALUES (
                    :id, :project_id, :provider, :source_id, :source_kind, :entitlement_kind,
                    :owner_type, :owner_id, CAST(:workload_classes_allowed AS JSONB),
                    :multi_tenant_allowed, :credential_ref, :cost_mode,
                    CAST(:limit_shape AS JSONB), CAST(:supported_models AS JSONB),
                    CAST(:task_strengths AS JSONB), :risk_tier_max, :status,
                    CAST(:policy_notes AS JSONB), CAST(:observed_capacity AS JSONB),
                    :runtime_supported
                )
                ON CONFLICT (project_id, source_id) DO UPDATE
                SET provider = EXCLUDED.provider,
                    source_kind = EXCLUDED.source_kind,
                    entitlement_kind = EXCLUDED.entitlement_kind,
                    owner_type = EXCLUDED.owner_type,
                    owner_id = EXCLUDED.owner_id,
                    workload_classes_allowed = EXCLUDED.workload_classes_allowed,
                    multi_tenant_allowed = EXCLUDED.multi_tenant_allowed,
                    credential_ref = EXCLUDED.credential_ref,
                    cost_mode = EXCLUDED.cost_mode,
                    limit_shape = EXCLUDED.limit_shape,
                    supported_models = EXCLUDED.supported_models,
                    task_strengths = EXCLUDED.task_strengths,
                    risk_tier_max = EXCLUDED.risk_tier_max,
                    status = EXCLUDED.status,
                    policy_notes = EXCLUDED.policy_notes,
                    observed_capacity = EXCLUDED.observed_capacity,
                    runtime_supported = EXCLUDED.runtime_supported,
                    updated_at = now()
                """
            ),
            {
                "id": source_pk,
                "project_id": record["project_id"],
                "provider": record["provider"],
                "source_id": record["source_id"],
                "source_kind": record.get("source_kind", "official_api"),
                "entitlement_kind": record.get("entitlement_kind", "usage_based_api"),
                "owner_type": record.get("owner_type", "project"),
                "owner_id": record.get("owner_id", record["project_id"]),
                "workload_classes_allowed": json.dumps(
                    record.get("workload_classes_allowed", [])
                ),
                "multi_tenant_allowed": bool(
                    record.get("multi_tenant_allowed", False)
                ),
                "credential_ref": record.get("credential_ref"),
                "cost_mode": record.get("cost_mode", "usage_based_api"),
                "limit_shape": json.dumps(record.get("limit_shape", {})),
                "supported_models": json.dumps(record.get("supported_models", [])),
                "task_strengths": json.dumps(record.get("task_strengths", [])),
                "risk_tier_max": int(record.get("risk_tier_max", 1)),
                "status": record.get("status", "declared"),
                "policy_notes": json.dumps(record.get("policy_notes", [])),
                "observed_capacity": json.dumps(
                    {
                        **observed_capacity,
                        "credential_present": credential_present,
                        "runtime_supported": runtime_supported,
                    }
                ),
                "runtime_supported": runtime_supported,
            },
        )
        if emit_events:
            emit_event(
                connection,
                record["project_id"],
                "entitlement.source_upserted",
                actor,
                "entitlement_source",
                source_pk,
                {"source_id": record["source_id"], "provider": record["provider"]},
            )
        source = next(
            item
            for item in self.list_sources(connection, record["project_id"])
            if item["source_id"] == record["source_id"]
        )
        return source

    def _allowed_tiers_for_budget_state(self, budget_state: str) -> list[str]:
        registry = self._provider_registry()
        return (
            ((registry.get("routing_policy") or {}).get("budget_states") or {})
            .get(budget_state, {})
            .get("allowed_tiers", [])
        )

    def _model_registry(self) -> list[dict[str, Any]]:
        return self._provider_registry().get("models", [])

    def _models_for_source(
        self, provider: str, supported_models: list[str], allowed_tiers: list[str]
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for model in self._model_registry():
            model_id = model.get("id")
            if not isinstance(model_id, str):
                continue
            if model.get("provider") != provider:
                continue
            suffix = model_id.split(":", 1)[-1]
            if supported_models and suffix not in supported_models and model_id not in supported_models:
                continue
            if allowed_tiers and model.get("tier") not in allowed_tiers:
                continue
            candidates.append(model)
        return candidates

    def _pick_model(
        self,
        models: list[dict[str, Any]],
        preferred_capabilities: list[str],
        task_kind: str,
    ) -> Optional[dict[str, Any]]:
        if not models:
            return None

        def score(model: dict[str, Any]) -> tuple[int, str]:
            strengths = set(model.get("strengths", []))
            capability_score = len(strengths.intersection(preferred_capabilities))
            task_score = 1 if task_kind in strengths else 0
            return (capability_score * 10 + task_score, model.get("id", ""))

        return sorted(models, key=score, reverse=True)[0]

    def _allowed_data_boundaries_for_source(
        self, source: dict[str, Any]
    ) -> set[str]:
        defaults = self._source_record_defaults().get(source["source_id"], {})
        configured = defaults.get("allowed_data_boundaries")
        if isinstance(configured, list) and configured:
            return {normalize_data_boundary(str(item)) for item in configured}
        boundary_policy = (
            (self._provider_registry().get("routing_policy") or {}).get(
                "data_boundaries"
            )
            or {}
        )
        if source.get("source_kind") == "approved_local_model":
            configured = boundary_policy.get("approved_local_model_default")
            if isinstance(configured, list) and configured:
                return {normalize_data_boundary(str(item)) for item in configured}
            return set(DATA_BOUNDARIES)
        configured = boundary_policy.get("external_official_source_default")
        if isinstance(configured, list) and configured:
            return {normalize_data_boundary(str(item)) for item in configured}
        return {"public", "internal", "confidential"}

    def list_routing_logs(
        self, connection: Connection, project_id: str, limit: int = 25
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, skill_id, task_kind, workload_class, risk_tier,
                       data_boundary, budget_state, selected_source_id, selected_provider,
                       selected_model_id, denial_reason, rejected_sources, decision,
                       created_at
                FROM routing_logs
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [
            {
                **dict(row),
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]

    def preview_route(
        self,
        connection: Connection,
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
        preferred_capabilities = preferred_capabilities or []
        skill = self.get_skill(skill_id)
        boundary = normalize_data_boundary(data_boundary)
        budget_snapshot = self.budget.status_for_project(connection, project_id)
        resolved_budget_state = budget_state or budget_snapshot["budget_state"]
        registry = self._provider_registry()
        routing_policy = registry.get("routing_policy", {}) or {}
        preferred_source_kinds = (
            ((routing_policy.get("workload_class_defaults") or {}).get(workload_class) or {})
            .get("preferred_source_kinds", [])
        )
        disallowed_combinations = routing_policy.get("disallowed_combinations", []) or []
        allowed_tiers = self._allowed_tiers_for_budget_state(resolved_budget_state)

        candidates: list[tuple[int, dict[str, Any], Optional[dict[str, Any]]]] = []
        rejected_sources: list[dict[str, Any]] = []
        for source in self.list_sources(connection, project_id):
            effective_status = source["effective_status"]
            if effective_status != "active":
                rejected_sources.append(
                    {
                        "source_id": source["source_id"],
                        "reason": effective_status,
                    }
                )
                continue
            if not source["runtime_supported"]:
                rejected_sources.append(
                    {
                        "source_id": source["source_id"],
                        "reason": "runtime_unavailable",
                    }
                )
                continue
            if workload_class not in (source.get("workload_classes_allowed") or []):
                rejected_sources.append(
                    {
                        "source_id": source["source_id"],
                        "reason": "workload_class_not_allowed",
                    }
                )
                continue
            if workload_class == "multi_tenant_customer" and not source.get(
                "multi_tenant_allowed", False
            ):
                rejected_sources.append(
                    {
                        "source_id": source["source_id"],
                        "reason": "multi_tenant_not_allowed",
                    }
                )
                continue
            if risk_tier > int(source.get("risk_tier_max", 0)):
                rejected_sources.append(
                    {
                        "source_id": source["source_id"],
                        "reason": "risk_tier_exceeds_source_limit",
                    }
                )
                continue
            if any(
                combo.get("source_kind") == source["source_kind"]
                and combo.get("workload_class") == workload_class
                for combo in disallowed_combinations
            ):
                rejected_sources.append(
                    {
                        "source_id": source["source_id"],
                        "reason": "provider_policy_disallows_workload",
                    }
                )
                continue
            if boundary not in self._allowed_data_boundaries_for_source(source):
                rejected_sources.append(
                    {
                        "source_id": source["source_id"],
                        "reason": "data_boundary_not_allowed",
                        "data_boundary": boundary,
                    }
                )
                continue

            model_candidates = self._models_for_source(
                source["provider"],
                source.get("supported_models") or [],
                allowed_tiers,
            )
            if allowed_tiers and not model_candidates and source["source_kind"] == "official_api":
                rejected_sources.append(
                    {
                        "source_id": source["source_id"],
                        "reason": "no_budget_eligible_model_tier",
                    }
                )
                continue
            selected_model = self._pick_model(
                model_candidates, preferred_capabilities, task_kind
            )
            source_strengths = set(source.get("task_strengths") or [])
            capability_score = len(source_strengths.intersection(preferred_capabilities))
            if task_kind in source_strengths:
                capability_score += 1
            try:
                preference_score = 100 - preferred_source_kinds.index(source["source_kind"]) * 10
            except ValueError:
                preference_score = 0
            candidates.append(
                (
                    preference_score + capability_score,
                    source,
                    selected_model,
                )
            )

        selected_source: Optional[dict[str, Any]] = None
        selected_model: Optional[dict[str, Any]] = None
        denial_reason: Optional[str] = None
        if candidates:
            candidates.sort(
                key=lambda item: (
                    item[0],
                    item[2].get("id", "") if item[2] else "",
                    item[1]["source_id"],
                ),
                reverse=True,
            )
            _, selected_source, selected_model = candidates[0]
        else:
            denial_reason = "no_eligible_source"

        decision = {
            "skill_id": skill_id,
            "task_kind": task_kind,
            "data_boundary": boundary,
            "reservation_required": bool(
                (skill.get("policy") or {}).get("requires_budget_reservation", False)
            ),
            "preferred_capabilities": preferred_capabilities,
            "fallback_order": [item[1]["source_id"] for item in candidates[1:4]],
            "provider_rule_compliance": selected_source is not None,
        }
        log_id = make_id("route")
        connection.execute(
            text(
                """
                INSERT INTO routing_logs (
                    id, project_id, skill_id, task_kind, workload_class, risk_tier,
                    data_boundary, budget_state, selected_source_id, selected_provider,
                    selected_model_id, denial_reason, rejected_sources, decision
                )
                VALUES (
                    :id, :project_id, :skill_id, :task_kind, :workload_class, :risk_tier,
                    :data_boundary, :budget_state, :selected_source_id, :selected_provider,
                    :selected_model_id, :denial_reason, CAST(:rejected_sources AS JSONB),
                    CAST(:decision AS JSONB)
                )
                """
            ),
            {
                "id": log_id,
                "project_id": project_id,
                "skill_id": skill_id,
                "task_kind": task_kind,
                "workload_class": workload_class,
                "risk_tier": risk_tier,
                "data_boundary": boundary,
                "budget_state": resolved_budget_state,
                "selected_source_id": selected_source["source_id"] if selected_source else None,
                "selected_provider": selected_source["provider"] if selected_source else None,
                "selected_model_id": selected_model.get("id") if selected_model else None,
                "denial_reason": denial_reason,
                "rejected_sources": json.dumps(rejected_sources),
                "decision": json.dumps(decision),
            },
        )
        emit_event(
            connection,
            project_id,
            "routing.selected" if selected_source else "routing.denied",
            actor,
            "routing_log",
            log_id,
            {
                "skill_id": skill_id,
                "task_kind": task_kind,
                "data_boundary": boundary,
                "selected_source_id": selected_source["source_id"] if selected_source else None,
                "selected_model_id": selected_model.get("id") if selected_model else None,
                "denial_reason": denial_reason,
            },
        )
        return {
            "route_decision_log_id": log_id,
            "skill_id": skill_id,
            "task_kind": task_kind,
            "workload_class": workload_class,
            "risk_tier": risk_tier,
            "data_boundary": boundary,
            "budget_state": resolved_budget_state,
            "source_id": selected_source["source_id"] if selected_source else None,
            "provider": selected_source["provider"] if selected_source else None,
            "model_id": selected_model.get("id") if selected_model else None,
            "denial_reason": denial_reason,
            "rejected_sources": rejected_sources,
            "decision": decision,
        }
