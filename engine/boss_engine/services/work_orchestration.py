from __future__ import annotations

import json
import re
from html import escape
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import emit_event, make_id
from .role_governance import RoleGovernanceService
from .work_tracker_adapters import (
    TRACKER_ADAPTERS,
    get_adapter,
    list_catalog as list_tracker_catalog,
    strip_html,
)
from .work_value_scoring import (
    SCORE_WEIGHTS as VALUE_SCORE_WEIGHTS,
    annotate_work_item_scores,
    compute_priority_score,
    compute_value_density,
    priority_band as value_priority_band,
    rank_by_value_density,
    score_preview as value_score_preview,
)


DELIVERY_PLUGINS: tuple[dict[str, Any], ...] = (
    {
        "provider": "mattermost",
        "display_name": "Mattermost",
        "capabilities": [
            "channel.ensure",
            "message.post",
            "thread.reply",
            "bot.identity",
            "deployment.status",
        ],
    },
    {
        "provider": "github",
        "display_name": "GitHub",
        "capabilities": [
            "pull_request.status",
            "check_run.status",
            "deployment.status",
            "branch.status",
            "bot.identity",
        ],
    },
    {
        "provider": "plane",
        "display_name": "Plane",
        "capabilities": [
            "issue.status",
            "issue.comment",
            "module.status",
            "bot.identity",
        ],
    },
)


class WorkOrchestrationService:
    """Rank work, allocate bounded capacity, and mirror delivery presence."""

    # Shared with work_value_scoring (single source of weight truth).
    SCORE_WEIGHTS = VALUE_SCORE_WEIGHTS

    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.governance = RoleGovernanceService(settings)

    # ------------------------------------------------------------------
    # Seeded plugin and policy defaults
    # ------------------------------------------------------------------
    def seed_defaults(
        self,
        connection: Connection,
        project_id: str,
        actor: str = "bootstrap",
    ) -> None:
        project_url = str(getattr(self.settings, "plane_project_url", "") or "")
        plane_project_id = self._plane_project_id(project_url)
        project_url_match = re.match(
            r"(https?://[^/]+)/([^/]+)/projects/",
            project_url,
        )
        derived_base_url = project_url_match.group(1) if project_url_match else ""
        derived_workspace = project_url_match.group(2) if project_url_match else ""
        configured_base_url = str(
            getattr(self.settings, "plane_base_url", "") or ""
        ).rstrip("/")
        configured_workspace = str(
            getattr(self.settings, "plane_workspace", "") or ""
        )
        plane_configuration = {
            "web_base_url": configured_base_url or derived_base_url,
            "api_base_url": configured_base_url or derived_base_url,
            "workspace_slug": (
                derived_workspace
                if configured_workspace in {"", "default"} and derived_workspace
                else configured_workspace or "default"
            ),
            "project_id": plane_project_id,
            "credential_ref": "secret:PLANE_API_KEY",
        }
        seed_rows = [
            ("plane", "Plane", True, plane_configuration),
            (
                "github_issues",
                "GitHub Issues",
                False,
                {
                    "credential_ref": "env:BOSS_GITHUB_TOKEN",
                    "api_base_url": "https://api.github.com",
                    "owner": "",
                    "repo": "",
                },
            ),
            (
                "linear",
                "Linear",
                False,
                {"credential_ref": "env:LINEAR_API_KEY", "team_id": "", "team_key": ""},
            ),
            (
                "jira",
                "Jira",
                False,
                {
                    "credential_ref": "env:JIRA_API_TOKEN",
                    "base_url": "",
                    "email": "",
                    "project_key": "",
                    "issue_type": "Task",
                },
            ),
            (
                "azure_devops",
                "Azure DevOps",
                False,
                {
                    "credential_ref": "env:AZURE_DEVOPS_PAT",
                    "organization": "",
                    "project": "",
                    "work_item_type": "Task",
                },
            ),
        ]
        for provider_key, display_name, is_default, configuration in seed_rows:
            adapter = TRACKER_ADAPTERS[provider_key]
            connection.execute(
                text(
                    """
                    INSERT INTO work_tracker_providers (
                        id, project_id, provider_key, display_name, adapter_kind,
                        status, is_default, enforcement_mode, capabilities,
                        configuration, created_by
                    )
                    VALUES (
                        :id, :project_id, :provider_key, :display_name, 'plugin',
                        'active', :is_default, :enforcement_mode,
                        CAST(:capabilities AS JSONB), CAST(:configuration AS JSONB),
                        :created_by
                    )
                    ON CONFLICT (project_id, provider_key) DO UPDATE
                    SET display_name=EXCLUDED.display_name,
                        capabilities=EXCLUDED.capabilities,
                        configuration=work_tracker_providers.configuration
                          || EXCLUDED.configuration,
                        enforcement_mode=EXCLUDED.enforcement_mode,
                        updated_at=now()
                    """
                ),
                {
                    "id": make_id("tracker"),
                    "project_id": project_id,
                    "provider_key": provider_key,
                    "display_name": display_name,
                    "is_default": is_default,
                    "enforcement_mode": (
                        "required"
                        if is_default
                        and bool(getattr(self.settings, "plane_api_token", None))
                        else "advisory"
                    ),
                    "capabilities": json.dumps(list(adapter.capabilities)),
                    "configuration": json.dumps(configuration),
                    "created_by": actor,
                },
            )

        budget_limit = connection.execute(
            text(
                """
                SELECT max_concurrent_jobs
                FROM budget_policies
                WHERE scope_type='project' AND scope_id=:project_id
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).scalar_one_or_none()
        self.upsert_quota(
            connection,
            project_id=project_id,
            scope_type="project",
            scope_id=project_id,
            resource_kind="job_concurrency",
            reserved_min=1,
            soft_limit=max(1, int(budget_limit or 3) - 1),
            hard_limit=max(1, int(budget_limit or 3)),
            burst_limit=0,
            allocation_weight=1,
            borrow_enabled=False,
            actor=actor,
        )

    # ------------------------------------------------------------------
    # Work-tracker plugins: catalog, connect, default selection
    # ------------------------------------------------------------------
    def tracker_catalog(self) -> list[dict[str, Any]]:
        """Static catalog of selectable trackers (Plane default + alternatives)."""
        return list_tracker_catalog()

    def list_tracker_providers(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        self.seed_defaults(connection, project_id)
        rows = connection.execute(
            text(
                """
                SELECT p.id, p.project_id, p.provider_key, p.display_name,
                       p.adapter_kind, p.status, p.is_default, p.enforcement_mode,
                       p.capabilities, p.configuration, p.created_by,
                       p.created_at, p.updated_at,
                       COALESCE((
                         SELECT count(*) FROM work_tracker_bindings b
                         WHERE b.provider_id = p.id
                       ), 0) AS binding_count
                FROM work_tracker_providers p
                WHERE p.project_id = :project_id
                ORDER BY p.is_default DESC, p.display_name ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._serialize_tracker_provider(row) for row in rows]

    def connect_tracker_provider(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_key: str,
        configuration: dict[str, Any],
        actor: str,
        set_as_default: bool = False,
        test: bool = True,
        api_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Configure and optionally test a work-tracker connection.

        Secrets: pass ``api_token`` once; it is stored under configuration as
        ``api_token`` for local/dev (prefer env credential_ref in production).
        """
        adapter = get_adapter(provider_key)
        self.seed_defaults(connection, project_id)
        config = dict(configuration or {})
        if api_token and str(api_token).strip():
            config["api_token"] = str(api_token).strip()
            # Prefer env-style refs when caller only passed a bare token
            if not config.get("credential_ref"):
                env_name = {
                    "plane": "PLANE_API_KEY",
                    "jira": "JIRA_API_TOKEN",
                    "linear": "LINEAR_API_KEY",
                    "github_issues": "BOSS_GITHUB_TOKEN",
                    "azure_devops": "AZURE_DEVOPS_PAT",
                }.get(provider_key, "BOSS_TRACKER_TOKEN")
                config.setdefault("credential_ref", f"env:{env_name}")

        test_result: Optional[dict[str, Any]] = None
        if test:
            test_result = adapter.test_connection(config)
            if not test_result.get("ok"):
                raise ValueError(
                    test_result.get("message")
                    or f"Connection test failed for {provider_key}"
                )

        # Upsert provider row
        connection.execute(
            text(
                """
                INSERT INTO work_tracker_providers (
                    id, project_id, provider_key, display_name, adapter_kind,
                    status, is_default, enforcement_mode, capabilities,
                    configuration, created_by
                )
                VALUES (
                    :id, :project_id, :provider_key, :display_name, 'plugin',
                    'active', false, 'advisory',
                    CAST(:capabilities AS JSONB), CAST(:configuration AS JSONB),
                    :created_by
                )
                ON CONFLICT (project_id, provider_key) DO UPDATE
                SET display_name = EXCLUDED.display_name,
                    status = 'active',
                    capabilities = EXCLUDED.capabilities,
                    configuration = EXCLUDED.configuration,
                    updated_at = now()
                """
            ),
            {
                "id": make_id("tracker"),
                "project_id": project_id,
                "provider_key": provider_key,
                "display_name": adapter.display_name,
                "capabilities": json.dumps(list(adapter.capabilities)),
                "configuration": json.dumps(config),
                "created_by": actor,
            },
        )
        if set_as_default:
            self.set_default_tracker(
                connection,
                project_id=project_id,
                provider_key=provider_key,
                actor=actor,
            )
        providers = self.list_tracker_providers(connection, project_id)
        provider = next(
            (p for p in providers if p["provider_key"] == provider_key), None
        )
        emit_event(
            connection,
            project_id,
            "work_tracker.provider_connected",
            actor,
            "work_tracker_provider",
            (provider or {}).get("id") or provider_key,
            {
                "provider_key": provider_key,
                "set_as_default": set_as_default,
                "tested": bool(test),
            },
        )
        return {
            "provider": provider,
            "test": test_result,
            "default_provider_key": next(
                (p["provider_key"] for p in providers if p.get("is_default")),
                "plane",
            ),
        }

    def set_default_tracker(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_key: str,
        actor: str,
    ) -> dict[str, Any]:
        get_adapter(provider_key)
        self.seed_defaults(connection, project_id)
        row = connection.execute(
            text(
                """
                SELECT id FROM work_tracker_providers
                WHERE project_id = :project_id AND provider_key = :provider_key
                """
            ),
            {"project_id": project_id, "provider_key": provider_key},
        ).mappings().first()
        if row is None:
            raise ValueError(f"Tracker provider '{provider_key}' is not installed.")
        connection.execute(
            text(
                """
                UPDATE work_tracker_providers
                SET is_default = false, updated_at = now()
                WHERE project_id = :project_id AND is_default = true
                """
            ),
            {"project_id": project_id},
        )
        connection.execute(
            text(
                """
                UPDATE work_tracker_providers
                SET is_default = true,
                    status = 'active',
                    enforcement_mode = 'required',
                    updated_at = now()
                WHERE project_id = :project_id AND provider_key = :provider_key
                """
            ),
            {"project_id": project_id, "provider_key": provider_key},
        )
        emit_event(
            connection,
            project_id,
            "work_tracker.default_changed",
            actor,
            "work_tracker_provider",
            row["id"],
            {"provider_key": provider_key},
        )
        return {
            "project_id": project_id,
            "default_provider_key": provider_key,
            "providers": self.list_tracker_providers(connection, project_id),
        }

    def test_tracker_provider(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_key: str,
    ) -> dict[str, Any]:
        providers = {
            p["provider_key"]: p
            for p in self.list_tracker_providers(connection, project_id)
        }
        if provider_key not in providers:
            raise ValueError(f"Tracker provider '{provider_key}' is not installed.")
        adapter = get_adapter(provider_key)
        config = dict(providers[provider_key].get("configuration") or {})
        result = adapter.test_connection(config)
        return {
            "provider_key": provider_key,
            "display_name": providers[provider_key]["display_name"],
            **result,
        }

    def execute_tracker_issue_create(
        self,
        connection: Connection,
        *,
        project_id: str,
        binding_id: str,
    ) -> dict[str, Any]:
        """Create external issue for a binding via the configured adapter."""
        context = self.tracker_sync_context(
            connection, project_id=project_id, binding_id=binding_id
        )
        provider_key = context["provider"]["provider_key"]
        adapter = get_adapter(provider_key)
        config = dict(context["provider"]["configuration"] or {})
        # Binding can override project/workspace
        if context["binding"].get("external_project_id"):
            config.setdefault(
                "project_id", context["binding"]["external_project_id"]
            )
            config.setdefault(
                "project_key", context["binding"]["external_project_id"]
            )
            config.setdefault(
                "project", context["binding"]["external_project_id"]
            )
        html = context["description_html"]
        text_body = strip_html(html)
        result = adapter.create_issue(
            config,
            title=context["work_item"]["title"],
            description_html=html,
            description_text=text_body,
        )
        if result is None:
            return {
                "ok": False,
                "reason": f"{provider_key}_issue_create_failed",
                "provider": provider_key,
            }
        return {
            "ok": True,
            "provider": provider_key,
            "external_issue_id": result.external_issue_id,
            "external_key": result.external_key,
            "external_url": result.external_url,
            "external_state": result.external_state,
        }

    def _serialize_tracker_provider(self, row: Any) -> dict[str, Any]:
        data = self._serialize(row)
        config = data.get("configuration") or {}
        if not isinstance(config, dict):
            config = {}
        # Never return raw secrets in list APIs
        redacted = dict(config)
        for secret_key in ("api_token", "access_token", "token", "pat", "password"):
            if redacted.get(secret_key):
                redacted[secret_key] = "***"
        data["configuration"] = redacted
        data["connected"] = self._provider_looks_configured(
            data["provider_key"], config
        )
        return data

    @staticmethod
    def _provider_looks_configured(
        provider_key: str, configuration: dict[str, Any]
    ) -> bool:
        if provider_key == "plane":
            return bool(
                configuration.get("api_base_url")
                or configuration.get("web_base_url")
            ) and bool(configuration.get("project_id"))
        if provider_key == "jira":
            return bool(configuration.get("base_url") and configuration.get("email"))
        if provider_key == "linear":
            return bool(
                configuration.get("team_id") or configuration.get("team_key")
            )
        if provider_key == "github_issues":
            return bool(configuration.get("owner") and configuration.get("repo"))
        if provider_key == "azure_devops":
            return bool(
                configuration.get("organization") and configuration.get("project")
            )
        return bool(configuration)

    # ------------------------------------------------------------------
    # Explainable portfolio priority
    # ------------------------------------------------------------------
    def evaluate_admission(
        self,
        connection: Connection,
        *,
        project_id: str,
        work_item_id: str,
        role_key: str,
        requested_autonomy_level: int,
        estimated_cost_usd_micros: int,
        actor: str,
    ) -> dict[str, Any]:
        """Return an explainable, side-effect-free admission decision for a work claim.

        This does not reserve budget, allocate capacity, or elevate permissions. It makes
        those later effects auditable by showing exactly why a role and budget may admit
        the proposed work now.
        """
        work_item = connection.execute(
            text(
                "SELECT * FROM work_items WHERE id=:id AND project_id=:project_id"
            ),
            {"id": work_item_id, "project_id": project_id},
        ).mappings().first()
        if work_item is None:
            raise ValueError("Work item not found.")
        if requested_autonomy_level < 0:
            raise ValueError("requested_autonomy_level must be >= 0.")
        if estimated_cost_usd_micros < 0:
            raise ValueError("estimated_cost_usd_micros must be >= 0.")

        role = self.governance.get_role(connection, project_id, role_key)
        budget = self.budget.status_for_project(connection, project_id)
        reasons: list[str] = []
        current_level = int(role["autonomy_level"])
        if requested_autonomy_level > current_level:
            reasons.append("requested_autonomy_level_exceeds_role_ceiling")
        if int(budget["available_usd_micros"]) < estimated_cost_usd_micros:
            reasons.append("budget_available_below_estimate")
        if budget["budget_state"] == "red" and work_item["priority_band"] not in {"p0", "p1"}:
            reasons.append("red_budget_state_admits_only_p0_p1_work")
        if work_item["status"] != "active":
            reasons.append("work_item_not_active")

        decision = "admit" if not reasons else "defer"
        priority_score = float(work_item["effective_score"])
        density = compute_value_density(
            priority_score, estimated_cost_usd_micros
        )
        result = {
            "project_id": project_id,
            "work_item_id": work_item_id,
            "role_key": role_key,
            "decision": decision,
            "reasons": reasons,
            "effective_score": priority_score,
            "priority_score": priority_score,
            "priority_band": work_item["priority_band"],
            "role_autonomy_ceiling": current_level,
            "requested_autonomy_level": requested_autonomy_level,
            "estimated_cost_usd_micros": estimated_cost_usd_micros,
            "available_budget_usd_micros": int(budget["available_usd_micros"]),
            "budget_state": budget["budget_state"],
            "value_density": density["value_density"],
            "value_per_cost": density["value_per_cost"],
            "value_per_cost_micros": density["value_per_cost_micros"],
            "cost_floor_applied": density["cost_floor_applied"],
        }
        emit_event(
            connection,
            project_id,
            "work.claim_admission_evaluated",
            actor,
            "work_item",
            work_item_id,
            result,
        )
        return result

    def upsert_work_item(
        self,
        connection: Connection,
        *,
        project_id: str,
        subject_type: str,
        subject_id: str,
        title: str,
        description: str = "",
        status: str = "active",
        strategic_value: int = 50,
        customer_impact: int = 50,
        urgency: int = 50,
        cost_of_delay: int = 50,
        risk_reduction: int = 50,
        dependency_pressure: int = 50,
        confidence: int = 75,
        manual_adjustment: int = 0,
        rationale: str = "",
        workload_class: str = "general",
        estimated_units: int = 1,
        due_at: Optional[Any] = None,
        owner_actor: Optional[str] = None,
        evidence: Optional[list[Any]] = None,
        source: str = "boss",
        actor: str = "system",
    ) -> dict[str, Any]:
        values = {
            "strategic_value": self._score_value(strategic_value),
            "customer_impact": self._score_value(customer_impact),
            "urgency": self._score_value(urgency),
            "cost_of_delay": self._score_value(cost_of_delay),
            "risk_reduction": self._score_value(risk_reduction),
            "dependency_pressure": self._score_value(dependency_pressure),
        }
        confidence = self._score_value(confidence)
        manual_adjustment = max(-100, min(100, int(manual_adjustment)))
        score = compute_priority_score(
            **values,
            confidence=confidence,
            manual_adjustment=manual_adjustment,
        )
        priority_band = self.priority_band(score)
        work_item_id = make_id("work")
        row = connection.execute(
            text(
                """
                INSERT INTO work_items (
                    id, project_id, subject_type, subject_id, title, description,
                    status, priority_band, effective_score, strategic_value,
                    customer_impact, urgency, cost_of_delay, risk_reduction,
                    dependency_pressure, confidence, manual_adjustment, rationale,
                    workload_class, estimated_units, due_at, owner_actor, evidence,
                    source, created_by
                )
                VALUES (
                    :id, :project_id, :subject_type, :subject_id, :title,
                    :description, :status, :priority_band, :effective_score,
                    :strategic_value, :customer_impact, :urgency, :cost_of_delay,
                    :risk_reduction, :dependency_pressure, :confidence,
                    :manual_adjustment, :rationale, :workload_class,
                    :estimated_units, :due_at, :owner_actor,
                    CAST(:evidence AS JSONB), :source, :created_by
                )
                ON CONFLICT (project_id, subject_type, subject_id) DO UPDATE
                SET title=EXCLUDED.title,
                    description=EXCLUDED.description,
                    status=EXCLUDED.status,
                    priority_band=EXCLUDED.priority_band,
                    effective_score=EXCLUDED.effective_score,
                    strategic_value=EXCLUDED.strategic_value,
                    customer_impact=EXCLUDED.customer_impact,
                    urgency=EXCLUDED.urgency,
                    cost_of_delay=EXCLUDED.cost_of_delay,
                    risk_reduction=EXCLUDED.risk_reduction,
                    dependency_pressure=EXCLUDED.dependency_pressure,
                    confidence=EXCLUDED.confidence,
                    manual_adjustment=EXCLUDED.manual_adjustment,
                    rationale=EXCLUDED.rationale,
                    workload_class=EXCLUDED.workload_class,
                    estimated_units=EXCLUDED.estimated_units,
                    due_at=EXCLUDED.due_at,
                    owner_actor=EXCLUDED.owner_actor,
                    evidence=EXCLUDED.evidence,
                    source=EXCLUDED.source,
                    updated_at=now()
                RETURNING *
                """
            ),
            {
                "id": work_item_id,
                "project_id": project_id,
                "subject_type": self._key(subject_type, "subject_type"),
                "subject_id": self._required(subject_id, "subject_id"),
                "title": self._required(title, "title"),
                "description": description.strip(),
                "status": self._key(status, "status"),
                "priority_band": priority_band,
                "effective_score": score,
                **values,
                "confidence": confidence,
                "manual_adjustment": manual_adjustment,
                "rationale": rationale.strip(),
                "workload_class": self._key(workload_class, "workload_class"),
                "estimated_units": max(1, int(estimated_units)),
                "due_at": due_at,
                "owner_actor": owner_actor,
                "evidence": json.dumps(evidence or []),
                "source": self._key(source, "source"),
                "created_by": actor,
            },
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            "work.priority_scored",
            actor,
            "work_item",
            row["id"],
            {
                "score": float(row["effective_score"]),
                "priority_score": float(row["effective_score"]),
                "priority_band": row["priority_band"],
                "drivers": values,
                "manual_adjustment": manual_adjustment,
            },
        )
        return self._serialize_work_item(row)

    def calculate_score(
        self,
        *,
        strategic_value: int,
        customer_impact: int,
        urgency: int,
        cost_of_delay: int,
        risk_reduction: int,
        dependency_pressure: int,
        confidence: int,
        manual_adjustment: int,
    ) -> Decimal:
        """Backward-compatible wrapper around compute_priority_score."""
        return compute_priority_score(
            strategic_value=strategic_value,
            customer_impact=customer_impact,
            urgency=urgency,
            cost_of_delay=cost_of_delay,
            risk_reduction=risk_reduction,
            dependency_pressure=dependency_pressure,
            confidence=confidence,
            manual_adjustment=manual_adjustment,
        )

    def compute_priority_score(
        self,
        *,
        strategic_value: int,
        customer_impact: int,
        urgency: int,
        cost_of_delay: int,
        risk_reduction: int,
        dependency_pressure: int,
        confidence: int = 75,
        manual_adjustment: int = 0,
    ) -> Decimal:
        return compute_priority_score(
            strategic_value=strategic_value,
            customer_impact=customer_impact,
            urgency=urgency,
            cost_of_delay=cost_of_delay,
            risk_reduction=risk_reduction,
            dependency_pressure=dependency_pressure,
            confidence=confidence,
            manual_adjustment=manual_adjustment,
        )

    def compute_value_density(
        self,
        priority_score: Decimal | float | int,
        estimated_cost_usd_micros: int,
    ) -> dict[str, Any]:
        return compute_value_density(priority_score, estimated_cost_usd_micros)

    def score_preview(
        self,
        *,
        strategic_value: int = 50,
        customer_impact: int = 50,
        urgency: int = 50,
        cost_of_delay: int = 50,
        risk_reduction: int = 50,
        dependency_pressure: int = 50,
        confidence: int = 75,
        manual_adjustment: int = 0,
        estimated_cost_usd_micros: int = 0,
    ) -> dict[str, Any]:
        """Side-effect-free score + value-density preview (no DB write)."""
        return value_score_preview(
            strategic_value=strategic_value,
            customer_impact=customer_impact,
            urgency=urgency,
            cost_of_delay=cost_of_delay,
            risk_reduction=risk_reduction,
            dependency_pressure=dependency_pressure,
            confidence=confidence,
            manual_adjustment=manual_adjustment,
            estimated_cost_usd_micros=estimated_cost_usd_micros,
        )

    def rank_by_value_density(
        self,
        candidates: list[dict[str, Any]],
        *,
        score_key: str = "priority_score",
        cost_key: str = "estimated_cost_usd_micros",
        id_key: str = "id",
    ) -> list[dict[str, Any]]:
        """Order candidates by value/$ then priority (pure; does not touch DB)."""
        return rank_by_value_density(
            candidates,
            score_key=score_key,
            cost_key=cost_key,
            id_key=id_key,
        )

    @staticmethod
    def priority_band(score: Decimal | float) -> str:
        return value_priority_band(score)

    def list_work_items(
        self,
        connection: Connection,
        project_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 100,
        estimated_cost_usd_micros: Optional[int] = None,
        order_by_value_density: bool = False,
    ) -> list[dict[str, Any]]:
        filters = ["project_id=:project_id"]
        params: dict[str, Any] = {
            "project_id": project_id,
            "limit": max(1, min(500, int(limit))),
        }
        if status:
            filters.append("status=:status")
            params["status"] = status
        rows = connection.execute(
            text(
                f"""
                SELECT * FROM work_items
                WHERE {' AND '.join(filters)}
                ORDER BY effective_score DESC, due_at NULLS LAST, created_at
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        items = [self._serialize_work_item(row) for row in rows]
        if estimated_cost_usd_micros is not None:
            cost = int(estimated_cost_usd_micros)
            items = [
                annotate_work_item_scores(
                    item, estimated_cost_usd_micros=cost
                )
                for item in items
            ]
            if order_by_value_density:
                items = rank_by_value_density(
                    items,
                    score_key="priority_score",
                    cost_key="estimated_cost_usd_micros",
                    id_key="id",
                )
        return items

    # ------------------------------------------------------------------
    # Quotas and dynamic allocation
    # ------------------------------------------------------------------
    def upsert_quota(
        self,
        connection: Connection,
        *,
        project_id: str,
        scope_type: str,
        scope_id: str,
        resource_kind: str,
        reserved_min: int,
        soft_limit: int,
        hard_limit: int,
        burst_limit: int = 0,
        allocation_weight: float = 1,
        borrow_enabled: bool = False,
        policy: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        reserved_min = max(0, int(reserved_min))
        soft_limit = max(reserved_min, int(soft_limit))
        hard_limit = max(soft_limit, int(hard_limit))
        row = connection.execute(
            text(
                """
                INSERT INTO resource_quotas (
                    id, project_id, scope_type, scope_id, resource_kind,
                    reserved_min, soft_limit, hard_limit, burst_limit,
                    allocation_weight, borrow_enabled, policy, created_by
                )
                VALUES (
                    :id, :project_id, :scope_type, :scope_id, :resource_kind,
                    :reserved_min, :soft_limit, :hard_limit, :burst_limit,
                    :allocation_weight, :borrow_enabled, CAST(:policy AS JSONB),
                    :created_by
                )
                ON CONFLICT (project_id, scope_type, scope_id, resource_kind)
                DO UPDATE SET
                    reserved_min=EXCLUDED.reserved_min,
                    soft_limit=EXCLUDED.soft_limit,
                    hard_limit=EXCLUDED.hard_limit,
                    burst_limit=EXCLUDED.burst_limit,
                    allocation_weight=EXCLUDED.allocation_weight,
                    borrow_enabled=EXCLUDED.borrow_enabled,
                    policy=EXCLUDED.policy,
                    status='active',
                    updated_at=now()
                RETURNING *
                """
            ),
            {
                "id": make_id("quota"),
                "project_id": project_id,
                "scope_type": self._key(scope_type, "scope_type"),
                "scope_id": self._required(scope_id, "scope_id"),
                "resource_kind": self._key(resource_kind, "resource_kind"),
                "reserved_min": reserved_min,
                "soft_limit": soft_limit,
                "hard_limit": hard_limit,
                "burst_limit": max(0, int(burst_limit)),
                "allocation_weight": max(float(allocation_weight), 0.001),
                "borrow_enabled": bool(borrow_enabled),
                "policy": json.dumps(policy or {}),
                "created_by": actor,
            },
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            "resource.quota_updated",
            actor,
            "resource_quota",
            row["id"],
            {
                "scope_type": row["scope_type"],
                "scope_id": row["scope_id"],
                "resource_kind": row["resource_kind"],
                "hard_limit": row["hard_limit"],
                "burst_limit": row["burst_limit"],
            },
        )
        return self._serialize(row)

    def list_quotas(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                WITH target AS (
                  SELECT p.workspace_id, ws.org_id
                  FROM projects p
                  JOIN workspaces ws ON ws.id=p.workspace_id
                  WHERE p.id=:project_id
                )
                SELECT q.*,
                       COALESCE((
                         SELECT SUM(a.used_units)
                         FROM resource_allocations a
                         WHERE a.quota_id=q.id AND a.status='active'
                       ), 0) AS used_units,
                       COALESCE((
                         SELECT SUM(a.granted_units)
                         FROM resource_allocations a
                         WHERE a.quota_id=q.id AND a.status='active'
                       ), 0) AS granted_units
                FROM resource_quotas q
                JOIN projects qp ON qp.id=q.project_id
                JOIN workspaces qws ON qws.id=qp.workspace_id
                CROSS JOIN target t
                WHERE q.project_id=:project_id
                   OR (q.scope_type='global' AND qws.org_id=t.org_id)
                   OR (
                     q.scope_type='org' AND q.scope_id=t.org_id
                     AND qws.org_id=t.org_id
                   )
                   OR (
                     q.scope_type='workspace'
                     AND q.scope_id=t.workspace_id
                   )
                ORDER BY q.resource_kind, q.scope_type, q.scope_id
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        items = [self._serialize(row) for row in rows]
        for item in items:
            item["used_units"] = self._quota_active_units(
                connection,
                quota=item,
                reference_project_id=project_id,
            )
        return items

    def plan_allocations(
        self,
        connection: Connection,
        *,
        project_id: str,
        resource_kind: str = "job_concurrency",
        actor: str = "allocator",
    ) -> dict[str, Any]:
        quota = self._quota_for(
            connection, project_id, resource_kind, scope_type="project"
        )
        if quota is None:
            raise ValueError(
                f"No active project quota exists for {resource_kind}."
            )
        connection.execute(
            text(
                """
                UPDATE resource_allocations
                SET status='released', released_at=now(), updated_at=now(),
                    reason=CASE WHEN reason='' THEN 'superseded_by_new_plan'
                                ELSE reason END
                WHERE quota_id=:quota_id AND status IN ('planned','active')
                  AND used_units=0
                """
            ),
            {"quota_id": quota["id"]},
        )
        retained_used = int(
            connection.execute(
                text(
                    """
                    SELECT COALESCE(SUM(used_units), 0)
                    FROM resource_allocations
                    WHERE quota_id=:quota_id AND status='active'
                    """
                ),
                {"quota_id": quota["id"]},
            ).scalar_one()
            or 0
        )
        capacity = int(quota["hard_limit"]) + int(quota["burst_limit"])
        available = max(0, capacity - retained_used)
        items = self.list_work_items(
            connection, project_id, status="active", limit=500
        )
        allocations: list[dict[str, Any]] = []
        for item in items:
            requested = max(1, int(item["estimated_units"]))
            granted = min(requested, available)
            available -= granted
            allocation = self._insert_allocation(
                connection,
                project_id=project_id,
                work_item=item,
                quota=quota,
                resource_kind=resource_kind,
                requested_units=requested,
                granted_units=granted,
                reason=(
                    "ranked_capacity_grant"
                    if granted == requested
                    else "capacity_exhausted"
                    if granted == 0
                    else "partial_ranked_capacity_grant"
                ),
                actor=actor,
            )
            allocations.append(allocation)
        emit_event(
            connection,
            project_id,
            "resource.allocation_plan_created",
            actor,
            "resource_quota",
            quota["id"],
            {
                "resource_kind": resource_kind,
                "capacity": capacity,
                "retained_used": retained_used,
                "granted": sum(int(item["granted_units"]) for item in allocations),
                "throttled": sum(
                    item["status"] == "throttled" for item in allocations
                ),
            },
        )
        return {
            "project_id": project_id,
            "quota": self._serialize(quota),
            "capacity": capacity,
            "retained_used": retained_used,
            "allocations": allocations,
        }

    def ensure_allocation(
        self,
        connection: Connection,
        *,
        project_id: str,
        work_item_id: str,
        resource_kind: str = "job_concurrency",
        actor: str = "allocator",
    ) -> dict[str, Any]:
        existing = connection.execute(
            text(
                """
                SELECT * FROM resource_allocations
                WHERE project_id=:project_id AND work_item_id=:work_item_id
                  AND resource_kind=:resource_kind AND status='active'
                  AND granted_units > used_units
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {
                "project_id": project_id,
                "work_item_id": work_item_id,
                "resource_kind": resource_kind,
            },
        ).mappings().first()
        if existing:
            return self._serialize(existing)
        work_item = connection.execute(
            text(
                "SELECT * FROM work_items "
                "WHERE id=:id AND project_id=:project_id"
            ),
            {"id": work_item_id, "project_id": project_id},
        ).mappings().first()
        if work_item is None:
            raise ValueError("Work item not found.")
        quota = self._quota_for(
            connection, project_id, resource_kind, scope_type="project"
        )
        if quota is None:
            raise ValueError(
                f"No active project quota exists for {resource_kind}."
            )
        used = int(
            connection.execute(
                text(
                    """
                    SELECT COALESCE(SUM(used_units), 0)
                    FROM resource_allocations
                    WHERE quota_id=:quota_id AND status='active'
                    """
                ),
                {"quota_id": quota["id"]},
            ).scalar_one()
            or 0
        )
        capacity = int(quota["hard_limit"]) + int(quota["burst_limit"])
        granted = min(
            max(1, int(work_item["estimated_units"])),
            max(0, capacity - used),
        )
        return self._insert_allocation(
            connection,
            project_id=project_id,
            work_item=work_item,
            quota=quota,
            resource_kind=resource_kind,
            requested_units=max(1, int(work_item["estimated_units"])),
            granted_units=granted,
            reason="on_demand_capacity_grant" if granted else "quota_exhausted",
            actor=actor,
        )

    def reserve_job_slot(
        self,
        connection: Connection,
        *,
        project_id: str,
        job_id: str,
        job_kind: str,
        work_item_id: Optional[str],
        tracker_binding_id: Optional[str],
        allocation_id: Optional[str],
        resource_units: int,
        already_held: bool = False,
    ) -> tuple[bool, str]:
        if not work_item_id:
            return True, "legacy_unscoped_job"
        binding = connection.execute(
            text(
                """
                SELECT b.id, b.sync_status, p.enforcement_mode,
                       p.status AS provider_status
                FROM work_tracker_bindings b
                JOIN work_tracker_providers p ON p.id=b.provider_id
                WHERE b.id=:binding_id AND b.project_id=:project_id
                  AND b.work_item_id=:work_item_id
                """
            ),
            {
                "binding_id": tracker_binding_id,
                "project_id": project_id,
                "work_item_id": work_item_id,
            },
        ).mappings().first()
        if binding is None:
            return False, "work_tracking_binding_required"
        if binding["provider_status"] != "active":
            return False, "work_tracker_provider_inactive"
        if (
            binding["enforcement_mode"] == "required"
            and binding["sync_status"] != "synced"
            and job_kind != "work_tracker.issue.sync"
        ):
            return False, "work_tracker_sync_required"
        if not allocation_id:
            return False, "resource_allocation_required"
        if already_held:
            return True, "reclaimed_existing_allocation"
        quota_allowed, quota_reason = self._hierarchical_quota_allows(
            connection,
            project_id=project_id,
            work_item_id=work_item_id,
            resource_kind="job_concurrency",
            resource_units=max(1, int(resource_units)),
        )
        if not quota_allowed:
            return False, quota_reason
        row = connection.execute(
            text(
                """
                UPDATE resource_allocations
                SET used_units=used_units+:units, updated_at=now()
                WHERE id=:allocation_id AND project_id=:project_id
                  AND work_item_id=:work_item_id AND status='active'
                  AND used_units+:units <= granted_units
                RETURNING id
                """
            ),
            {
                "units": max(1, int(resource_units)),
                "allocation_id": allocation_id,
                "project_id": project_id,
                "work_item_id": work_item_id,
            },
        ).scalar_one_or_none()
        if row is None:
            return False, "resource_quota_exhausted"
        emit_event(
            connection,
            project_id,
            "resource.allocation_consumed",
            "job_allocator",
            "job",
            job_id,
            {
                "allocation_id": allocation_id,
                "work_item_id": work_item_id,
                "units": max(1, int(resource_units)),
            },
        )
        return True, "allocated"

    def _hierarchical_quota_allows(
        self,
        connection: Connection,
        *,
        project_id: str,
        work_item_id: str,
        resource_kind: str,
        resource_units: int,
    ) -> tuple[bool, str]:
        context = connection.execute(
            text(
                """
                SELECT p.id AS project_id, p.workspace_id, ws.org_id,
                       w.workload_class, w.owner_actor
                FROM work_items w
                JOIN projects p ON p.id=w.project_id
                JOIN workspaces ws ON ws.id=p.workspace_id
                WHERE w.id=:work_item_id AND w.project_id=:project_id
                """
            ),
            {"work_item_id": work_item_id, "project_id": project_id},
        ).mappings().first()
        if context is None:
            return False, "work_item_not_found"
        quotas = connection.execute(
            text(
                """
                SELECT q.*
                FROM resource_quotas q
                JOIN projects qp ON qp.id=q.project_id
                JOIN workspaces qws ON qws.id=qp.workspace_id
                WHERE q.resource_kind=:resource_kind
                  AND q.status='active'
                  AND (
                    (
                      q.scope_type='global'
                      AND qws.org_id=:org_id
                    )
                    OR (
                      q.scope_type='org'
                      AND q.scope_id=:org_id
                      AND qws.org_id=:org_id
                    )
                    OR (
                      q.scope_type='workspace'
                      AND q.scope_id=:workspace_id
                      AND qws.org_id=:org_id
                    )
                    OR (
                      q.scope_type='project'
                      AND q.scope_id=:project_id
                    )
                    OR (
                      q.project_id=:project_id
                      AND q.scope_type='workload'
                      AND q.scope_id=:workload_class
                    )
                    OR (
                      q.project_id=:project_id
                      AND q.scope_type='agent'
                      AND q.scope_id=:owner_actor
                    )
                  )
                ORDER BY
                  CASE scope_type
                    WHEN 'global' THEN 0
                    WHEN 'org' THEN 1
                    WHEN 'workspace' THEN 2
                    WHEN 'project' THEN 3
                    WHEN 'workload' THEN 4
                    WHEN 'agent' THEN 5
                  END
                FOR UPDATE
                """
            ),
            {
                "project_id": project_id,
                "resource_kind": resource_kind,
                "org_id": context["org_id"],
                "workspace_id": context["workspace_id"],
                "workload_class": context["workload_class"],
                "owner_actor": context["owner_actor"] or "",
            },
        ).mappings().all()
        if not quotas:
            return False, "resource_quota_required"
        for quota in quotas:
            used = self._quota_active_units(
                connection,
                quota=quota,
                reference_project_id=project_id,
            )
            capacity = int(quota["hard_limit"]) + int(quota["burst_limit"])
            if used + resource_units > capacity:
                emit_event(
                    connection,
                    project_id,
                    "resource.hierarchical_quota_denied",
                    "job_allocator",
                    "work_item",
                    work_item_id,
                    {
                        "quota_id": quota["id"],
                        "scope_type": quota["scope_type"],
                        "scope_id": quota["scope_id"],
                        "resource_kind": resource_kind,
                        "used_units": used,
                        "requested_units": resource_units,
                        "capacity": capacity,
                    },
                )
                return False, "resource_quota_exhausted"
        return True, "hierarchical_quota_available"

    def _quota_active_units(
        self,
        connection: Connection,
        *,
        quota: Any,
        reference_project_id: str,
    ) -> int:
        scope_type = str(quota["scope_type"])
        scope_id = str(quota["scope_id"])
        filters = [
            "j.status IN ('leased', 'running')",
            "j.lease_expires_at > now()",
        ]
        params: dict[str, Any] = {
            "scope_id": scope_id,
            "reference_project_id": reference_project_id,
        }
        joins = [
            "JOIN projects jp ON jp.id=j.project_id",
            "JOIN workspaces jws ON jws.id=jp.workspace_id",
        ]
        if scope_type == "global":
            # Global means the current BOSS organization, not cross-tenant.
            filters.append(
                "jws.org_id=(SELECT ws.org_id FROM projects p "
                "JOIN workspaces ws ON ws.id=p.workspace_id "
                "WHERE p.id=:reference_project_id)"
            )
        elif scope_type == "org":
            filters.append("jws.org_id=:scope_id")
        elif scope_type == "workspace":
            filters.append("jp.workspace_id=:scope_id")
        elif scope_type == "project":
            filters.append("j.project_id=:scope_id")
        elif scope_type == "workload":
            filters.extend(
                [
                    "j.project_id=:reference_project_id",
                    "j.workload_class=:scope_id",
                ]
            )
        elif scope_type == "agent":
            joins.append("JOIN work_items jw ON jw.id=j.work_item_id")
            filters.extend(
                [
                    "j.project_id=:reference_project_id",
                    "jw.owner_actor=:scope_id",
                ]
            )
        else:
            return 0
        value = connection.execute(
            text(
                f"""
                SELECT COALESCE(SUM(j.resource_units), 0)
                FROM jobs j
                {' '.join(joins)}
                WHERE {' AND '.join(filters)}
                """
            ),
            params,
        ).scalar_one()
        return int(value or 0)

    def release_job_slot(
        self,
        connection: Connection,
        *,
        project_id: str,
        job_id: str,
        allocation_id: Optional[str],
        resource_units: int,
        reason: str,
    ) -> None:
        if not allocation_id:
            return
        row = connection.execute(
            text(
                """
                UPDATE resource_allocations
                SET used_units=GREATEST(0, used_units-:units), updated_at=now()
                WHERE id=:allocation_id AND project_id=:project_id
                RETURNING work_item_id, used_units
                """
            ),
            {
                "units": max(1, int(resource_units)),
                "allocation_id": allocation_id,
                "project_id": project_id,
            },
        ).mappings().first()
        if row:
            emit_event(
                connection,
                project_id,
                "resource.allocation_released",
                "job_allocator",
                "job",
                job_id,
                {
                    "allocation_id": allocation_id,
                    "work_item_id": row["work_item_id"],
                    "units": max(1, int(resource_units)),
                    "remaining_used_units": int(row["used_units"]),
                    "reason": reason,
                },
            )

    # ------------------------------------------------------------------
    # Tracker bindings and provider plugins
    # ------------------------------------------------------------------
    def ensure_tracked_work(
        self,
        connection: Connection,
        *,
        project_id: str,
        subject_type: str,
        subject_id: str,
        title: str,
        description: str = "",
        workload_class: str = "general",
        actor: str = "system",
        priority_inputs: Optional[dict[str, int]] = None,
    ) -> dict[str, Any]:
        inputs = priority_inputs or {}
        work_item = self.upsert_work_item(
            connection,
            project_id=project_id,
            subject_type=subject_type,
            subject_id=subject_id,
            title=title,
            description=description,
            workload_class=workload_class,
            actor=actor,
            **{
                key: inputs[key]
                for key in (
                    "strategic_value",
                    "customer_impact",
                    "urgency",
                    "cost_of_delay",
                    "risk_reduction",
                    "dependency_pressure",
                    "confidence",
                    "manual_adjustment",
                )
                if key in inputs
            },
        )
        binding = self.bind_tracker(
            connection,
            project_id=project_id,
            work_item_id=work_item["id"],
            provider_key=None,
            actor=actor,
        )
        allocation = self.ensure_allocation(
            connection,
            project_id=project_id,
            work_item_id=work_item["id"],
            actor=actor,
        )
        return {
            "work_item": work_item,
            "tracker_binding": binding,
            "allocation": allocation,
        }

    def enqueue_tracker_sync(
        self,
        connection: Connection,
        *,
        project_id: str,
        binding_id: str,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Queue provider creation behind the same fenced job system as agents.

        The provider write itself happens only when a worker holds this job's
        lease. The sync job shares the work item's allocation, so tracking does
        not become a hidden path around resource quotas.
        """
        context = self.tracker_sync_context(
            connection,
            project_id=project_id,
            binding_id=binding_id,
        )
        if context["binding"]["sync_status"] == "synced":
            return {
                "status": "already_synced",
                "binding_id": binding_id,
                "job_id": None,
            }
        allocation = self.ensure_allocation(
            connection,
            project_id=project_id,
            work_item_id=context["work_item"]["id"],
            actor=actor,
        )
        # Local import avoids coupling the provider plugin protocol to the job
        # implementation at module-import time.
        from .jobs import enqueue_job

        job = enqueue_job(
            connection,
            project_id,
            "work_tracker.issue.sync",
            {
                "binding_id": binding_id,
                "work_item_id": context["work_item"]["id"],
                "provider_key": context["provider"]["provider_key"],
                "requires_reservation": False,
            },
            priority=max(
                0,
                100 - round(float(context["work_item"]["effective_score"])),
            ),
            priority_score=float(context["work_item"]["effective_score"]),
            workload_class=context["work_item"]["workload_class"],
            resource_units=1,
            max_attempts=5,
            dedupe_key=f"work-tracker-create:{binding_id}",
            work_item_id=context["work_item"]["id"],
            tracker_binding_id=binding_id,
            allocation_id=allocation["id"],
        )
        emit_event(
            connection,
            project_id,
            "work_tracker.sync_queued",
            actor,
            "work_tracker_binding",
            binding_id,
            {
                "job_id": job["id"],
                "provider": context["provider"]["provider_key"],
                "created": job["created"],
            },
        )
        return {
            "status": "queued",
            "binding_id": binding_id,
            "job_id": job["id"],
            "created": job["created"],
        }

    def tracker_sync_context(
        self,
        connection: Connection,
        *,
        project_id: str,
        binding_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT b.*, p.provider_key, p.display_name AS provider_name,
                       p.enforcement_mode, p.configuration,
                       w.title, w.description, w.priority_band,
                       w.effective_score, w.rationale, w.evidence,
                       w.subject_type, w.subject_id, w.workload_class
                FROM work_tracker_bindings b
                JOIN work_tracker_providers p ON p.id=b.provider_id
                JOIN work_items w ON w.id=b.work_item_id
                WHERE b.id=:binding_id AND b.project_id=:project_id
                """
            ),
            {"binding_id": binding_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise ValueError("Work-tracker binding not found.")
        provider = {
            "provider_key": row["provider_key"],
            "display_name": row["provider_name"],
            "enforcement_mode": row["enforcement_mode"],
            "configuration": self._json_value(row["configuration"], {}),
        }
        binding = {
            key: self._serialize_value(row[key])
            for key in (
                "id",
                "work_item_id",
                "external_workspace_id",
                "external_project_id",
                "external_issue_id",
                "external_key",
                "external_url",
                "external_state",
                "sync_status",
                "sync_error",
                "metadata",
            )
        }
        work_item = {
            "id": row["work_item_id"],
            **{
                key: self._serialize_value(row[key])
                for key in (
                    "title",
                    "description",
                    "priority_band",
                    "effective_score",
                    "rationale",
                    "evidence",
                    "subject_type",
                    "subject_id",
                    "workload_class",
                )
            },
        }
        return {
            "binding": binding,
            "provider": provider,
            "work_item": work_item,
            "description_html": self._tracker_description_html(work_item),
        }

    def mark_tracker_synced(
        self,
        connection: Connection,
        *,
        project_id: str,
        binding_id: str,
        external_issue_id: str,
        external_key: Optional[str],
        external_url: Optional[str],
        external_state: Optional[str],
        actor: str,
    ) -> dict[str, Any]:
        context = self.tracker_sync_context(
            connection,
            project_id=project_id,
            binding_id=binding_id,
        )
        adapter = TRACKER_ADAPTERS[context["provider"]["provider_key"]]
        provider_project = (
            context["binding"]["external_project_id"]
            or context["provider"]["configuration"].get("project_id")
        )
        url = external_url or adapter.issue_url(
            context["provider"]["configuration"],
            external_project_id=provider_project,
            external_issue_id=external_issue_id,
        )
        row = connection.execute(
            text(
                """
                UPDATE work_tracker_bindings
                SET external_issue_id=:external_issue_id,
                    external_key=:external_key,
                    external_url=:external_url,
                    external_state=:external_state,
                    sync_status='synced',
                    sync_error='{}'::jsonb,
                    last_synced_at=now(),
                    updated_at=now()
                WHERE id=:binding_id AND project_id=:project_id
                RETURNING *
                """
            ),
            {
                "binding_id": binding_id,
                "project_id": project_id,
                "external_issue_id": external_issue_id,
                "external_key": external_key,
                "external_url": url,
                "external_state": external_state,
            },
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            "work_tracker.binding_synced",
            actor,
            "work_tracker_binding",
            binding_id,
            {
                "provider": context["provider"]["provider_key"],
                "external_issue_id": external_issue_id,
                "external_key": external_key,
                "external_url": url,
            },
        )
        return self._serialize(row)

    def mark_tracker_sync_failed(
        self,
        connection: Connection,
        *,
        project_id: str,
        binding_id: str,
        error: dict[str, Any],
        actor: str,
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE work_tracker_bindings
                SET sync_status='failed', sync_error=CAST(:error AS JSONB),
                    updated_at=now()
                WHERE id=:binding_id AND project_id=:project_id
                """
            ),
            {
                "binding_id": binding_id,
                "project_id": project_id,
                "error": json.dumps(error),
            },
        )
        emit_event(
            connection,
            project_id,
            "work_tracker.sync_failed",
            actor,
            "work_tracker_binding",
            binding_id,
            error,
        )

    def bind_tracker(
        self,
        connection: Connection,
        *,
        project_id: str,
        work_item_id: str,
        provider_key: Optional[str],
        external_workspace_id: Optional[str] = None,
        external_project_id: Optional[str] = None,
        external_issue_id: Optional[str] = None,
        external_key: Optional[str] = None,
        external_url: Optional[str] = None,
        external_state: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        provider = self._tracker_provider(
            connection, project_id, provider_key
        )
        if provider is None:
            raise ValueError("No active work tracker provider is configured.")
        adapter = TRACKER_ADAPTERS.get(provider["provider_key"])
        if adapter is None:
            raise ValueError(
                f"No work-tracker plugin is installed for {provider['provider_key']}."
            )
        configuration = provider["configuration"] or {}
        project_ref = external_project_id or configuration.get("project_id")
        url = external_url or adapter.issue_url(
            configuration,
            external_project_id=project_ref,
            external_issue_id=external_issue_id,
        )
        sync_status = "synced" if external_issue_id else "pending_create"
        row = connection.execute(
            text(
                """
                INSERT INTO work_tracker_bindings (
                    id, project_id, provider_id, work_item_id,
                    external_workspace_id, external_project_id, external_issue_id,
                    external_key, external_url, external_state, sync_status,
                    metadata, created_by, last_synced_at
                )
                VALUES (
                    :id, :project_id, :provider_id, :work_item_id,
                    :external_workspace_id, :external_project_id,
                    :external_issue_id, :external_key, :external_url,
                    :external_state, :sync_status, CAST(:metadata AS JSONB),
                    :created_by,
                    CASE WHEN :sync_status='synced' THEN now() ELSE NULL END
                )
                ON CONFLICT (project_id, provider_id, work_item_id) DO UPDATE
                SET external_workspace_id=COALESCE(
                      EXCLUDED.external_workspace_id,
                      work_tracker_bindings.external_workspace_id
                    ),
                    external_project_id=COALESCE(
                      EXCLUDED.external_project_id,
                      work_tracker_bindings.external_project_id
                    ),
                    external_issue_id=COALESCE(
                      EXCLUDED.external_issue_id,
                      work_tracker_bindings.external_issue_id
                    ),
                    external_key=COALESCE(
                      EXCLUDED.external_key,
                      work_tracker_bindings.external_key
                    ),
                    external_url=COALESCE(
                      EXCLUDED.external_url,
                      work_tracker_bindings.external_url
                    ),
                    external_state=COALESCE(
                      EXCLUDED.external_state,
                      work_tracker_bindings.external_state
                    ),
                    sync_status=CASE
                      WHEN EXCLUDED.external_issue_id IS NOT NULL THEN 'synced'
                      ELSE work_tracker_bindings.sync_status
                    END,
                    metadata=work_tracker_bindings.metadata || EXCLUDED.metadata,
                    last_synced_at=CASE
                      WHEN EXCLUDED.external_issue_id IS NOT NULL THEN now()
                      ELSE work_tracker_bindings.last_synced_at
                    END,
                    updated_at=now()
                RETURNING *
                """
            ),
            {
                "id": make_id("trackbind"),
                "project_id": project_id,
                "provider_id": provider["id"],
                "work_item_id": work_item_id,
                "external_workspace_id": external_workspace_id
                or configuration.get("workspace_slug"),
                "external_project_id": project_ref,
                "external_issue_id": external_issue_id,
                "external_key": external_key,
                "external_url": url,
                "external_state": external_state,
                "sync_status": sync_status,
                "metadata": json.dumps(metadata or {}),
                "created_by": actor,
            },
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            "work_tracker.binding_upserted",
            actor,
            "work_tracker_binding",
            row["id"],
            {
                "provider": provider["provider_key"],
                "work_item_id": work_item_id,
                "sync_status": row["sync_status"],
                "external_key": row["external_key"],
            },
        )
        return {
            **self._serialize(row),
            "provider_key": provider["provider_key"],
            "provider_name": provider["display_name"],
            "enforcement_mode": provider["enforcement_mode"],
        }

    def list_tracker_bindings(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT b.*, p.provider_key, p.display_name AS provider_name,
                       p.enforcement_mode, w.title AS work_title,
                       w.priority_band, w.effective_score
                FROM work_tracker_bindings b
                JOIN work_tracker_providers p ON p.id=b.provider_id
                JOIN work_items w ON w.id=b.work_item_id
                WHERE b.project_id=:project_id
                ORDER BY w.effective_score DESC, b.updated_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._serialize(row) for row in rows]

    @staticmethod
    def _tracker_description_html(work_item: dict[str, Any]) -> str:
        evidence = work_item.get("evidence") or []
        evidence_items = "".join(
            f"<li>{escape(str(item))}</li>" for item in evidence
        )
        evidence_html = (
            f"<p><strong>Evidence</strong></p><ul>{evidence_items}</ul>"
            if evidence_items
            else ""
        )
        description = escape(str(work_item.get("description") or ""))
        rationale = escape(str(work_item.get("rationale") or ""))
        return (
            f"<p>{description}</p>"
            f"<p><strong>BOSS priority:</strong> "
            f"{escape(str(work_item.get('priority_band') or '').upper())} · "
            f"{escape(str(work_item.get('effective_score') or 0))}/100</p>"
            f"<p><strong>Why now:</strong> {rationale or 'No rationale supplied.'}</p>"
            f"<p><strong>BOSS source:</strong> "
            f"{escape(str(work_item.get('subject_type')))} / "
            f"<code>{escape(str(work_item.get('subject_id')))}</code></p>"
            f"{evidence_html}"
        )

    # ------------------------------------------------------------------
    # Delivery presence: channels, bot identities, and status ledger
    # ------------------------------------------------------------------
    def upsert_bot_identity(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider: str,
        agent_key: str,
        display_name: str,
        external_identity: Optional[str] = None,
        agent_definition_id: Optional[str] = None,
        credential_secret_ref_id: Optional[str] = None,
        avatar_url: Optional[str] = None,
        policy: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        self._require_delivery_provider(provider)
        row = connection.execute(
            text(
                """
                INSERT INTO delivery_bot_identities (
                    id, project_id, provider, agent_definition_id, agent_key,
                    display_name, external_identity, credential_secret_ref_id,
                    avatar_url, policy, created_by
                )
                VALUES (
                    :id, :project_id, :provider, :agent_definition_id, :agent_key,
                    :display_name, :external_identity, :credential_secret_ref_id,
                    :avatar_url, CAST(:policy AS JSONB), :created_by
                )
                ON CONFLICT (project_id, provider, agent_key) DO UPDATE
                SET display_name=EXCLUDED.display_name,
                    external_identity=EXCLUDED.external_identity,
                    agent_definition_id=EXCLUDED.agent_definition_id,
                    credential_secret_ref_id=EXCLUDED.credential_secret_ref_id,
                    avatar_url=EXCLUDED.avatar_url,
                    policy=EXCLUDED.policy,
                    status='active',
                    updated_at=now()
                RETURNING *
                """
            ),
            {
                "id": make_id("bot"),
                "project_id": project_id,
                "provider": self._key(provider, "provider"),
                "agent_definition_id": agent_definition_id,
                "agent_key": self._key(agent_key, "agent_key"),
                "display_name": self._required(display_name, "display_name"),
                "external_identity": external_identity,
                "credential_secret_ref_id": credential_secret_ref_id,
                "avatar_url": avatar_url,
                "policy": json.dumps(policy or {}),
                "created_by": actor,
            },
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            "delivery.bot_identity_upserted",
            actor,
            "delivery_bot_identity",
            row["id"],
            {"provider": row["provider"], "agent_key": row["agent_key"]},
        )
        return self._serialize(row)

    def upsert_status_channel(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider: str,
        channel_key: str,
        display_name: str,
        purpose: str,
        external_channel_id: Optional[str] = None,
        external_url: Optional[str] = None,
        bot_identity_id: Optional[str] = None,
        event_kinds: Optional[list[str]] = None,
        allowed_data_boundaries: Optional[list[str]] = None,
        configuration: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        self._require_delivery_provider(provider)
        row = connection.execute(
            text(
                """
                INSERT INTO delivery_status_channels (
                    id, project_id, provider, channel_key, display_name, purpose,
                    external_channel_id, external_url, bot_identity_id,
                    event_kinds, allowed_data_boundaries, configuration, created_by
                )
                VALUES (
                    :id, :project_id, :provider, :channel_key, :display_name,
                    :purpose, :external_channel_id, :external_url,
                    :bot_identity_id, CAST(:event_kinds AS JSONB),
                    CAST(:allowed_data_boundaries AS JSONB),
                    CAST(:configuration AS JSONB), :created_by
                )
                ON CONFLICT (project_id, provider, channel_key) DO UPDATE
                SET display_name=EXCLUDED.display_name,
                    purpose=EXCLUDED.purpose,
                    external_channel_id=EXCLUDED.external_channel_id,
                    external_url=EXCLUDED.external_url,
                    bot_identity_id=EXCLUDED.bot_identity_id,
                    event_kinds=EXCLUDED.event_kinds,
                    allowed_data_boundaries=EXCLUDED.allowed_data_boundaries,
                    configuration=EXCLUDED.configuration,
                    status='active',
                    updated_at=now()
                RETURNING *
                """
            ),
            {
                "id": make_id("statusch"),
                "project_id": project_id,
                "provider": self._key(provider, "provider"),
                "channel_key": self._key(channel_key, "channel_key"),
                "display_name": self._required(display_name, "display_name"),
                "purpose": self._required(purpose, "purpose"),
                "external_channel_id": external_channel_id,
                "external_url": external_url,
                "bot_identity_id": bot_identity_id,
                "event_kinds": json.dumps(event_kinds or []),
                "allowed_data_boundaries": json.dumps(
                    allowed_data_boundaries or ["public", "internal"]
                ),
                "configuration": json.dumps(configuration or {}),
                "created_by": actor,
            },
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            "delivery.status_channel_upserted",
            actor,
            "delivery_status_channel",
            row["id"],
            {"provider": row["provider"], "channel_key": row["channel_key"]},
        )
        return self._serialize(row)

    def record_delivery_event(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider: str,
        event_kind: str,
        subject_type: str,
        subject_id: str,
        status: str,
        summary: str,
        dedupe_key: str,
        channel_id: Optional[str] = None,
        bot_identity_id: Optional[str] = None,
        external_url: Optional[str] = None,
        receipt_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        data_boundary: str = "internal",
    ) -> dict[str, Any]:
        self._require_delivery_provider(provider)
        boundary = self._key(data_boundary, "data_boundary")
        row = connection.execute(
            text(
                """
                INSERT INTO delivery_presence_events (
                    id, project_id, channel_id, bot_identity_id, provider,
                    event_kind, subject_type, subject_id, status, summary,
                    external_url, receipt_id, metadata, dedupe_key
                )
                VALUES (
                    :id, :project_id, :channel_id, :bot_identity_id, :provider,
                    :event_kind, :subject_type, :subject_id, :status, :summary,
                    :external_url, :receipt_id, CAST(:metadata AS JSONB),
                    :dedupe_key
                )
                ON CONFLICT (project_id, dedupe_key) DO UPDATE
                SET status=EXCLUDED.status,
                    summary=EXCLUDED.summary,
                    external_url=COALESCE(
                      EXCLUDED.external_url,
                      delivery_presence_events.external_url
                    ),
                    receipt_id=COALESCE(
                      EXCLUDED.receipt_id,
                      delivery_presence_events.receipt_id
                    ),
                    metadata=delivery_presence_events.metadata || EXCLUDED.metadata
                RETURNING *
                """
            ),
            {
                "id": make_id("presence"),
                "project_id": project_id,
                "channel_id": channel_id,
                "bot_identity_id": bot_identity_id,
                "provider": self._key(provider, "provider"),
                "event_kind": self._key(event_kind, "event_kind"),
                "subject_type": self._key(subject_type, "subject_type"),
                "subject_id": self._required(subject_id, "subject_id"),
                "status": self._key(status, "status"),
                "summary": self._required(summary, "summary"),
                "external_url": external_url,
                "receipt_id": receipt_id,
                "metadata": json.dumps(metadata or {}),
                "dedupe_key": self._required(dedupe_key, "dedupe_key"),
            },
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            "delivery.presence_recorded",
            "delivery_presence",
            "delivery_presence_event",
            row["id"],
            {
                "provider": row["provider"],
                "event_kind": row["event_kind"],
                "subject_type": row["subject_type"],
                "subject_id": row["subject_id"],
                "status": row["status"],
            },
        )
        projected_channels = self._project_delivery_event_to_channels(
            connection,
            project_id=project_id,
            source_provider=provider,
            event_kind=event_kind,
            subject_type=subject_type,
            subject_id=subject_id,
            status=status,
            summary=summary,
            dedupe_key=dedupe_key,
            external_url=external_url,
            receipt_id=receipt_id,
            metadata=metadata or {},
            data_boundary=boundary,
            skip_channel_id=channel_id,
        )
        return {
            **self._serialize(row),
            "projected_channel_ids": projected_channels,
        }

    def _project_delivery_event_to_channels(
        self,
        connection: Connection,
        *,
        project_id: str,
        source_provider: str,
        event_kind: str,
        subject_type: str,
        subject_id: str,
        status: str,
        summary: str,
        dedupe_key: str,
        external_url: Optional[str],
        receipt_id: Optional[str],
        metadata: dict[str, Any],
        data_boundary: str,
        skip_channel_id: Optional[str],
    ) -> list[str]:
        channels = connection.execute(
            text(
                """
                SELECT id, provider, bot_identity_id
                FROM delivery_status_channels
                WHERE project_id=:project_id
                  AND status='active'
                  AND (
                    CAST(:skip_channel_id AS TEXT) IS NULL
                    OR id<>CAST(:skip_channel_id AS TEXT)
                  )
                  AND (
                    jsonb_array_length(event_kinds)=0
                    OR event_kinds ? CAST(:event_kind AS TEXT)
                    OR event_kinds ? '*'
                  )
                  AND (
                    jsonb_array_length(allowed_data_boundaries)=0
                    OR allowed_data_boundaries ? CAST(:data_boundary AS TEXT)
                  )
                ORDER BY created_at, id
                """
            ),
            {
                "project_id": project_id,
                "skip_channel_id": skip_channel_id,
                "event_kind": event_kind,
                "data_boundary": data_boundary,
            },
        ).mappings().all()
        projected_ids: list[str] = []
        for channel in channels:
            projected = connection.execute(
                text(
                    """
                    INSERT INTO delivery_presence_events (
                        id, project_id, channel_id, bot_identity_id, provider,
                        event_kind, subject_type, subject_id, status, summary,
                        external_url, receipt_id, metadata, dedupe_key
                    )
                    VALUES (
                        :id, :project_id, :channel_id, :bot_identity_id,
                        :provider, :event_kind, :subject_type, :subject_id,
                        :status, :summary, :external_url, :receipt_id,
                        CAST(:metadata AS JSONB), :dedupe_key
                    )
                    ON CONFLICT (project_id, dedupe_key) DO UPDATE
                    SET status=EXCLUDED.status,
                        summary=EXCLUDED.summary,
                        external_url=COALESCE(
                          EXCLUDED.external_url,
                          delivery_presence_events.external_url
                        ),
                        receipt_id=COALESCE(
                          EXCLUDED.receipt_id,
                          delivery_presence_events.receipt_id
                        ),
                        metadata=delivery_presence_events.metadata
                          || EXCLUDED.metadata
                    RETURNING id
                    """
                ),
                {
                    "id": make_id("presence"),
                    "project_id": project_id,
                    "channel_id": channel["id"],
                    "bot_identity_id": channel["bot_identity_id"],
                    "provider": channel["provider"],
                    "event_kind": self._key(event_kind, "event_kind"),
                    "subject_type": self._key(subject_type, "subject_type"),
                    "subject_id": self._required(subject_id, "subject_id"),
                    "status": self._key(status, "status"),
                    "summary": self._required(summary, "summary"),
                    "external_url": external_url,
                    "receipt_id": receipt_id,
                    "metadata": json.dumps(
                        {
                            **metadata,
                            "source_provider": source_provider,
                            "data_boundary": data_boundary,
                            "projection_only": True,
                        }
                    ),
                    "dedupe_key": f"{dedupe_key}:channel:{channel['id']}",
                },
            ).scalar_one()
            projected_ids.append(str(projected))
        if projected_ids:
            emit_event(
                connection,
                project_id,
                "delivery.presence_projected",
                "delivery_presence",
                "delivery_presence_event",
                projected_ids[0],
                {
                    "source_provider": source_provider,
                    "event_kind": event_kind,
                    "channel_ids": [str(channel["id"]) for channel in channels],
                    "presence_event_ids": projected_ids,
                    "data_boundary": data_boundary,
                },
            )
        return [str(channel["id"]) for channel in channels]

    # ------------------------------------------------------------------
    # Embedded operator read model
    # ------------------------------------------------------------------
    def overview(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        work_items = self.list_work_items(connection, project_id, limit=100)
        quotas = self.list_quotas(connection, project_id)
        allocations = [
            self._serialize(row)
            for row in connection.execute(
                text(
                    """
                    SELECT a.*, w.title AS work_title, w.priority_band,
                           q.scope_type, q.scope_id
                    FROM resource_allocations a
                    JOIN work_items w ON w.id=a.work_item_id
                    JOIN resource_quotas q ON q.id=a.quota_id
                    WHERE a.project_id=:project_id
                    ORDER BY a.created_at DESC LIMIT 100
                    """
                ),
                {"project_id": project_id},
            ).mappings().all()
        ]
        jobs = [
            self._serialize(row)
            for row in connection.execute(
                text(
                    """
                    SELECT j.id, j.kind, j.status, j.priority, j.priority_score,
                           j.workload_class, j.resource_units, j.lease_owner,
                           j.lease_expires_at, j.heartbeat_at, j.run_after,
                           j.work_item_id, j.tracker_binding_id, j.allocation_id,
                           w.title AS work_title, w.priority_band,
                           b.external_key, b.external_url,
                           p.provider_key AS tracker_provider
                    FROM jobs j
                    LEFT JOIN work_items w ON w.id=j.work_item_id
                    LEFT JOIN work_tracker_bindings b ON b.id=j.tracker_binding_id
                    LEFT JOIN work_tracker_providers p ON p.id=b.provider_id
                    WHERE j.project_id=:project_id
                      AND j.status NOT IN ('succeeded','cancelled','deadlettered')
                    ORDER BY j.priority_score DESC NULLS LAST,
                             j.priority, j.created_at
                    LIMIT 100
                    """
                ),
                {"project_id": project_id},
            ).mappings().all()
        ]
        bots = [
            self._serialize(row)
            for row in connection.execute(
                text(
                    """
                    SELECT * FROM delivery_bot_identities
                    WHERE project_id=:project_id
                    ORDER BY provider, display_name
                    """
                ),
                {"project_id": project_id},
            ).mappings().all()
        ]
        channels = [
            self._serialize(row)
            for row in connection.execute(
                text(
                    """
                    SELECT c.*, b.display_name AS bot_name, b.agent_key
                    FROM delivery_status_channels c
                    LEFT JOIN delivery_bot_identities b ON b.id=c.bot_identity_id
                    WHERE c.project_id=:project_id
                    ORDER BY c.provider, c.display_name
                    """
                ),
                {"project_id": project_id},
            ).mappings().all()
        ]
        presence = [
            self._serialize(row)
            for row in connection.execute(
                text(
                    """
                    SELECT e.*, c.display_name AS channel_name,
                           b.display_name AS bot_name, b.agent_key
                    FROM delivery_presence_events e
                    LEFT JOIN delivery_status_channels c ON c.id=e.channel_id
                    LEFT JOIN delivery_bot_identities b ON b.id=e.bot_identity_id
                    WHERE e.project_id=:project_id
                    ORDER BY e.created_at DESC LIMIT 100
                    """
                ),
                {"project_id": project_id},
            ).mappings().all()
        ]
        bindings = self.list_tracker_bindings(connection, project_id)
        return {
            "project_id": project_id,
            "summary": {
                "ranked_work": len(work_items),
                "p0_p1_work": sum(
                    item["priority_band"] in {"p0", "p1"} for item in work_items
                ),
                "active_jobs": len(jobs),
                "tracked_jobs": sum(bool(item["tracker_binding_id"]) for item in jobs),
                "throttled_work": sum(
                    item["allocation_status"] == "throttled"
                    for item in work_items
                ),
                "pending_tracker_sync": sum(
                    item["sync_status"] != "synced" for item in bindings
                ),
                "active_channels": sum(
                    item["status"] == "active" for item in channels
                ),
            },
            "work_items": work_items,
            "quotas": quotas,
            "allocations": allocations,
            "jobs": jobs,
            "tracker_providers": self.list_tracker_providers(
                connection, project_id
            ),
            "tracker_bindings": bindings,
            "delivery_plugins": list(DELIVERY_PLUGINS),
            "bot_identities": bots,
            "status_channels": channels,
            "presence_events": presence,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _insert_allocation(
        self,
        connection: Connection,
        *,
        project_id: str,
        work_item: Any,
        quota: Any,
        resource_kind: str,
        requested_units: int,
        granted_units: int,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        status = "active" if granted_units > 0 else "throttled"
        row = connection.execute(
            text(
                """
                INSERT INTO resource_allocations (
                    id, project_id, work_item_id, quota_id, resource_kind,
                    requested_units, granted_units, used_units, status, reason,
                    score_snapshot, policy_snapshot, created_by
                )
                VALUES (
                    :id, :project_id, :work_item_id, :quota_id, :resource_kind,
                    :requested_units, :granted_units, 0, :status, :reason,
                    :score_snapshot, CAST(:policy_snapshot AS JSONB), :created_by
                )
                RETURNING *
                """
            ),
            {
                "id": make_id("alloc"),
                "project_id": project_id,
                "work_item_id": work_item["id"],
                "quota_id": quota["id"],
                "resource_kind": resource_kind,
                "requested_units": requested_units,
                "granted_units": granted_units,
                "status": status,
                "reason": reason,
                "score_snapshot": work_item["effective_score"],
                "policy_snapshot": json.dumps(
                    {
                        "quota_scope_type": quota["scope_type"],
                        "quota_scope_id": quota["scope_id"],
                        "hard_limit": int(quota["hard_limit"]),
                        "burst_limit": int(quota["burst_limit"]),
                        "allocation_weight": float(quota["allocation_weight"]),
                    }
                ),
                "created_by": actor,
            },
        ).mappings().one()
        connection.execute(
            text(
                """
                UPDATE work_items
                SET allocated_units=:allocated_units,
                    allocation_status=:allocation_status,
                    updated_at=now()
                WHERE id=:work_item_id
                """
            ),
            {
                "work_item_id": work_item["id"],
                "allocated_units": granted_units,
                "allocation_status": (
                    "allocated" if granted_units >= requested_units else status
                ),
            },
        )
        return self._serialize(row)

    def _quota_for(
        self,
        connection: Connection,
        project_id: str,
        resource_kind: str,
        *,
        scope_type: str,
    ) -> Optional[Any]:
        return connection.execute(
            text(
                """
                SELECT * FROM resource_quotas
                WHERE project_id=:project_id AND resource_kind=:resource_kind
                  AND scope_type=:scope_type AND status='active'
                ORDER BY updated_at DESC LIMIT 1
                """
            ),
            {
                "project_id": project_id,
                "resource_kind": resource_kind,
                "scope_type": scope_type,
            },
        ).mappings().first()

    def _tracker_provider(
        self,
        connection: Connection,
        project_id: str,
        provider_key: Optional[str],
    ) -> Optional[Any]:
        if provider_key:
            return connection.execute(
                text(
                    """
                    SELECT * FROM work_tracker_providers
                    WHERE project_id=:project_id AND provider_key=:provider_key
                      AND status='active'
                    """
                ),
                {"project_id": project_id, "provider_key": provider_key},
            ).mappings().first()
        return connection.execute(
            text(
                """
                SELECT * FROM work_tracker_providers
                WHERE project_id=:project_id AND is_default=true
                  AND status='active'
                ORDER BY updated_at DESC LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()

    @staticmethod
    def _score_value(value: Any) -> int:
        return max(0, min(100, int(value)))

    @staticmethod
    def _key(value: Any, field: str) -> str:
        normalized = re.sub(r"[^a-z0-9_.:-]+", "_", str(value or "").lower())
        normalized = normalized.strip("_")
        if not normalized:
            raise ValueError(f"{field} is required.")
        return normalized

    @staticmethod
    def _required(value: Any, field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field} is required.")
        return normalized

    @staticmethod
    def _plane_project_id(project_url: str) -> Optional[str]:
        match = re.search(r"/projects/([^/?#]+)", project_url)
        return match.group(1) if match else None

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        data = dict(row)
        for key, value in list(data.items()):
            data[key] = WorkOrchestrationService._serialize_value(value)
        return data

    @classmethod
    def _serialize_work_item(cls, row: Any) -> dict[str, Any]:
        """Serialize a work_items row and expose priority_score as the public alias."""
        data = cls._serialize(row)
        return annotate_work_item_scores(data)

    @staticmethod
    def _serialize_value(value: Any) -> Any:
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        return value

    @staticmethod
    def _json_value(value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return default
        return value

    @staticmethod
    def _require_delivery_provider(provider: str) -> None:
        supported = {item["provider"] for item in DELIVERY_PLUGINS}
        if str(provider or "").lower() not in supported:
            raise ValueError(
                f"Delivery provider '{provider}' is not installed. "
                f"Installed providers: {', '.join(sorted(supported))}."
            )
