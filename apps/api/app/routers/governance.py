"""HTTP surface for assurance, Agent Groups, and Integration Connections."""

from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.config import Settings
from boss_engine.services.assurance import (
    AssuranceConflictError,
    AssuranceEvidenceError,
    AssuranceNotFoundError,
    AssuranceService,
)
from boss_engine.services.governed_collaboration import (
    AgentGroupService,
    CollaborationConflictError,
    CollaborationNotFoundError,
    IntegrationRegistryService,
)
from boss_engine.services.jobs import LeaseNotFoundError

from ..db import begin


class ActorRequest(BaseModel):
    project_id: str
    actor: str = "system"


class MetricCreateRequest(ActorRequest):
    metric_key: str
    metric_kind: str
    unit: str = "count"
    comparator: str = "gte"
    baseline: float
    threshold: float
    threshold_upper: Optional[float] = None
    cadence_seconds: int = Field(ge=1)
    alert_sla_seconds: int = Field(ge=1)
    owner_ref: str
    evidence_query: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class MetricObservationRequest(BaseModel):
    project_id: str
    value: float
    evidence: dict[str, Any]
    evaluator_job_id: str
    evaluator_worker_id: str
    lease_token: str
    dedupe_key: str


class MetricBaselineRequest(ActorRequest):
    baseline: float
    threshold: float
    threshold_upper: Optional[float] = None
    reason: str
    approved_decision_id: Optional[str] = None


class MetricScheduleRequest(ActorRequest):
    force: bool = False
    metric_definition_id: Optional[str] = None


class ConclusionCreateRequest(ActorRequest):
    conclusion_key: str
    conclusion: str
    scope: dict[str, Any] = Field(default_factory=dict)
    owner_ref: str
    tell_patterns: list[Any]
    cause_menu: list[Any]
    evidence_refs: list[Any]
    reopen_conditions: list[Any] = Field(default_factory=list)
    review_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class PublicationGuardRequest(BaseModel):
    project_id: str
    publication_boundary: str
    content: str
    dedupe_key: str


class ConclusionReopenRequest(ActorRequest):
    expected_version: int = Field(ge=1)
    reason: str
    new_evidence_refs: list[Any]
    approved_decision_id: str


class AssuranceCaseCreateRequest(ActorRequest):
    subject_type: str
    subject_ref: str
    title: str
    risk_tier: int = Field(default=1, ge=0, le=3)
    references: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str


class ClaimCreateRequest(ActorRequest):
    assurance_case_id: str
    claim_type: str
    statement: str
    evidence_contract: dict[str, Any] = Field(default_factory=dict)
    producer_ref: str
    dedupe_key: str


class EvidenceCreateRequest(ActorRequest):
    assurance_case_id: str
    claim_id: Optional[str] = None
    evidence_type: str
    uri: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any]
    reproduction: dict[str, Any]
    content_hash: Optional[str] = None
    valid_until: Optional[datetime] = None


class EvidenceInvalidateRequest(ActorRequest):
    reason: str


class ClaimEvaluateRequest(ActorRequest):
    dedupe_key: str


class CaseEvaluateRequest(ActorRequest):
    org_rules: dict[str, Any] = Field(default_factory=dict)
    project_rules: dict[str, Any] = Field(default_factory=dict)


class SurfaceAcquireRequest(ActorRequest):
    surface_key: str
    environment: str
    owner_ref: str
    coordination_route: str
    reason: str
    dedupe_key: str
    expires_at: Optional[datetime] = None


class SurfaceTransferRequest(ActorRequest):
    expected_version: int = Field(ge=1)
    to_owner_ref: str
    coordination_route: str
    reason: str
    dedupe_key: str
    emergency: bool = False
    approved_decision_id: Optional[str] = None


class SurfacePreflightRequest(BaseModel):
    project_id: str
    surface_key: str
    environment: str
    actor_ref: str
    expected_version: int = Field(ge=1)
    effect_ref: str
    dedupe_key: str


class VerificationPolicyCreateRequest(ActorRequest):
    policy_key: str
    surface_patterns: list[Any] = Field(default_factory=list)
    claim_types: list[str]
    risk_tiers: list[int] = Field(default_factory=list)
    cadence_seconds: int = Field(ge=1)
    sample_rate: float = Field(default=1, ge=0, le=1)
    verification_method: dict[str, Any]
    independence_policy: dict[str, Any] = Field(default_factory=dict)
    escalation_target: str
    estimated_cost_usd_micros: int = Field(default=0, ge=0)


class VerificationScheduleRequest(ActorRequest):
    assurance_case_id: str
    claim_id: str
    verification_policy_id: str
    producer_path: dict[str, Any]
    verifier_path: dict[str, Any]
    dedupe_key: str


class VerificationScheduleDueRequest(ActorRequest):
    claim_id: Optional[str] = None
    verification_policy_id: Optional[str] = None


class VerificationCompleteRequest(BaseModel):
    project_id: str
    worker_id: str
    job_lease_token: str
    outcome: str
    evidence_bundle: list[Any]
    receipt_refs: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)


class IntegrationDefinitionCreateRequest(ActorRequest):
    integration_key: str
    name: str
    category: str
    skill_contracts: dict[str, Any]
    executor_bindings: list[dict[str, Any]] = Field(default_factory=list)
    provenance: dict[str, Any]


class IntegrationConnectionCreateRequest(ActorRequest):
    integration_definition_id: str
    connection_key: str
    scope_type: str = "project"
    scope_id: str
    owner_ref: str
    secret_ref_ids: list[str] = Field(default_factory=list)
    granted_capabilities: list[str]
    data_boundaries: list[str]
    max_risk_tier: int = Field(default=1, ge=0, le=3)
    freshness_seconds: int = Field(default=3600, ge=1)
    provenance: dict[str, Any] = Field(default_factory=dict)


class IntegrationConnectionUpdateRequest(ActorRequest):
    health_status: str
    status: str
    rate_limit_state: dict[str, Any] = Field(default_factory=dict)


class CapabilityResolveRequest(BaseModel):
    project_id: str
    skill_id: str
    actor_ref: str
    role_allowed: list[str]
    group_allowed: list[str]
    workflow_allowed: list[str]
    project_allowed: list[str]
    requested_data_boundary: str
    risk_tier: int = Field(ge=0, le=3)
    dedupe_key: str
    workflow_step_ref: Optional[str] = None
    agent_group_version_ref: Optional[str] = None


class AgentGroupCreateRequest(ActorRequest):
    group_key: str
    name: str
    purpose: str
    owner_ref: str
    topology: str
    members: list[dict[str, Any]]
    collaboration_protocol: dict[str, Any]
    allowed_capabilities: list[str] = Field(default_factory=list)
    context_policy: dict[str, Any] = Field(default_factory=dict)
    independence_policy: dict[str, Any] = Field(default_factory=dict)
    bounds: dict[str, Any]
    completion_rule: dict[str, Any]
    escalation_target: str


class AgentGroupRunCreateRequest(ActorRequest):
    agent_group_version_id: str
    input_payload: dict[str, Any]
    context: dict[str, Any] = Field(default_factory=dict)
    capability_resolution_ids: list[str] = Field(default_factory=list)
    governance_gate_results: dict[str, bool] = Field(default_factory=dict)
    dedupe_key: str
    assurance_case_id: Optional[str] = None
    workflow_run_id: Optional[str] = None
    task_id: Optional[str] = None


class AgentGroupMemberCompleteRequest(BaseModel):
    project_id: str
    worker_id: str
    job_lease_token: str
    succeeded: bool
    output: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[Any] = Field(default_factory=list)
    messages: list[Any] = Field(default_factory=list)
    evidence_refs: list[Any] = Field(default_factory=list)
    receipt_refs: list[Any] = Field(default_factory=list)
    cost_usd_micros: int = Field(default=0, ge=0)
    error: dict[str, Any] = Field(default_factory=dict)


class AgentGroupFinalizeRequest(ActorRequest):
    output: dict[str, Any] = Field(default_factory=dict)


def build_governance_router(
    *,
    engine: Any,
    settings: Settings,
    require_actor: Callable[[Any, str, str, str], None],
) -> APIRouter:
    router = APIRouter()
    assurance = AssuranceService(settings)
    integrations = IntegrationRegistryService(settings)
    groups = AgentGroupService(settings)

    def mutate(
        payload: ActorRequest, permission: str, callback: Callable[[Any], Any]
    ) -> Any:
        with begin(engine) as connection:
            require_actor(connection, payload.project_id, payload.actor, permission)
            try:
                return callback(connection)
            except AssuranceNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except CollaborationNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except (
                AssuranceConflictError,
                CollaborationConflictError,
                LeaseNotFoundError,
            ) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except (AssuranceEvidenceError, ValueError) as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

    def worker(callback: Callable[[Any], Any]) -> Any:
        with begin(engine) as connection:
            try:
                return callback(connection)
            except (AssuranceNotFoundError, CollaborationNotFoundError) as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except (
                AssuranceConflictError,
                CollaborationConflictError,
                LeaseNotFoundError,
            ) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except (AssuranceEvidenceError, ValueError) as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/v1/assurance/overview")
    @permit(P.ASSURANCE_READ, scope=Scope.project(query="project_id"))
    def assurance_overview(project_id: str = Query(...)) -> dict[str, Any]:
        with begin(engine) as connection:
            return assurance.overview(connection, project_id)

    @router.get("/v1/assurance/metrics")
    @permit(P.ASSURANCE_READ, scope=Scope.project(query="project_id"))
    def list_metrics(project_id: str = Query(...)) -> dict[str, Any]:
        with begin(engine) as connection:
            return {
                "project_id": project_id,
                "items": assurance.list_metrics(connection, project_id),
            }

    @router.post("/v1/assurance/metrics", status_code=201)
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def create_metric(payload: MetricCreateRequest) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.create_metric(
                connection, **payload.model_dump()
            ),
        )

    @router.post("/v1/assurance/metrics/schedule")
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def schedule_metrics(payload: MetricScheduleRequest) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.schedule_due_metric_evaluations(
                connection, **payload.model_dump()
            ),
        )

    @router.post("/v1/assurance/metrics/{metric_id}/observations", status_code=201)
    @permit(P.WORKER_LEASE)
    def record_observation(
        metric_id: str, payload: MetricObservationRequest
    ) -> dict[str, Any]:
        return worker(
            lambda connection: assurance.record_observation(
                connection, metric_definition_id=metric_id, **payload.model_dump()
            )
        )

    @router.post("/v1/assurance/metrics/{metric_id}/baseline")
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def change_baseline(
        metric_id: str, payload: MetricBaselineRequest
    ) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.change_metric_baseline(
                connection, metric_definition_id=metric_id, **payload.model_dump()
            ),
        )

    @router.get("/v1/assurance/conclusions")
    @permit(P.ASSURANCE_READ, scope=Scope.project(query="project_id"))
    def list_conclusions(project_id: str = Query(...)) -> dict[str, Any]:
        with begin(engine) as connection:
            return {
                "project_id": project_id,
                "items": assurance.list_conclusions(connection, project_id),
            }

    @router.post("/v1/assurance/conclusions", status_code=201)
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def create_conclusion(payload: ConclusionCreateRequest) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.create_conclusion(
                connection, **payload.model_dump()
            ),
        )

    @router.post("/v1/assurance/publication-guard")
    @permit(P.ASSURANCE_RUN, scope=Scope.project(body="project_id"))
    def publication_guard(payload: PublicationGuardRequest) -> dict[str, Any]:
        return worker(
            lambda connection: assurance.scan_publication(
                connection, **payload.model_dump()
            )
        )

    @router.post("/v1/assurance/conclusions/{conclusion_id}/reopen")
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def reopen_conclusion(
        conclusion_id: str, payload: ConclusionReopenRequest
    ) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.reopen_conclusion(
                connection, conclusion_id=conclusion_id, **payload.model_dump()
            ),
        )

    @router.get("/v1/assurance/cases")
    @permit(P.ASSURANCE_READ, scope=Scope.project(query="project_id"))
    def list_cases(project_id: str = Query(...)) -> dict[str, Any]:
        with begin(engine) as connection:
            return {
                "project_id": project_id,
                "items": assurance.list_cases(connection, project_id),
            }

    @router.post("/v1/assurance/cases", status_code=201)
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def create_case(payload: AssuranceCaseCreateRequest) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.create_case(
                connection, **payload.model_dump()
            ),
        )

    @router.post("/v1/assurance/claims", status_code=201)
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def create_claim(payload: ClaimCreateRequest) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.create_claim(
                connection, **payload.model_dump()
            ),
        )

    @router.post("/v1/assurance/evidence", status_code=201)
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def create_evidence(payload: EvidenceCreateRequest) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.add_evidence(
                connection, **payload.model_dump()
            ),
        )

    @router.post("/v1/assurance/evidence/{evidence_id}/invalidate")
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def invalidate_evidence(
        evidence_id: str, payload: EvidenceInvalidateRequest
    ) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.invalidate_evidence(
                connection, evidence_id=evidence_id, **payload.model_dump()
            ),
        )

    @router.post("/v1/assurance/claims/{claim_id}/evaluate")
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def evaluate_claim(claim_id: str, payload: ClaimEvaluateRequest) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.evaluate_claim(
                connection, claim_id=claim_id, **payload.model_dump()
            ),
        )

    @router.post("/v1/assurance/cases/{case_id}/evaluate")
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def evaluate_case(case_id: str, payload: CaseEvaluateRequest) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.evaluate_case(
                connection, assurance_case_id=case_id, **payload.model_dump()
            ),
        )

    @router.get("/v1/assurance/surfaces")
    @permit(P.ASSURANCE_READ, scope=Scope.project(query="project_id"))
    def list_surfaces(project_id: str = Query(...)) -> dict[str, Any]:
        with begin(engine) as connection:
            return {
                "project_id": project_id,
                "items": assurance.list_surfaces(connection, project_id),
            }

    @router.post("/v1/assurance/surfaces/acquire")
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def acquire_surface(payload: SurfaceAcquireRequest) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.acquire_surface(
                connection, **payload.model_dump()
            ),
        )

    @router.post("/v1/assurance/surfaces/{ownership_id}/transfer")
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def transfer_surface(
        ownership_id: str, payload: SurfaceTransferRequest
    ) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.transfer_surface(
                connection, ownership_id=ownership_id, **payload.model_dump()
            ),
        )

    @router.post("/v1/assurance/surfaces/preflight")
    @permit(P.ASSURANCE_RUN, scope=Scope.project(body="project_id"))
    def preflight_surface(payload: SurfacePreflightRequest) -> dict[str, Any]:
        return worker(
            lambda connection: assurance.preflight_surface(
                connection, **payload.model_dump()
            )
        )

    @router.post("/v1/assurance/verification-policies", status_code=201)
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def create_verification_policy(
        payload: VerificationPolicyCreateRequest,
    ) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.create_verification_policy(
                connection, **payload.model_dump()
            ),
        )

    @router.get("/v1/assurance/verification-runs")
    @permit(P.ASSURANCE_READ, scope=Scope.project(query="project_id"))
    def list_verification_runs(project_id: str = Query(...)) -> dict[str, Any]:
        with begin(engine) as connection:
            return {
                "project_id": project_id,
                "items": assurance.list_verification_runs(connection, project_id),
            }

    @router.post("/v1/assurance/verification-runs", status_code=201)
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def schedule_verification(payload: VerificationScheduleRequest) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.schedule_verification(
                connection, **payload.model_dump()
            ),
        )

    @router.post("/v1/assurance/verification-runs/schedule-due")
    @permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
    def schedule_due_verifications(
        payload: VerificationScheduleDueRequest,
    ) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: assurance.schedule_due_verifications(
                connection, **payload.model_dump()
            ),
        )

    @router.post("/v1/assurance/verification-runs/{run_id}/complete")
    @permit(P.WORKER_LEASE)
    def complete_verification(
        run_id: str, payload: VerificationCompleteRequest
    ) -> dict[str, Any]:
        return worker(
            lambda connection: assurance.complete_verification(
                connection, verification_run_id=run_id, **payload.model_dump()
            )
        )

    @router.get("/v1/integration-registry/definitions")
    @permit(P.INTEGRATION_READ, scope=Scope.project(query="project_id"))
    def list_integration_definitions(project_id: str = Query(...)) -> dict[str, Any]:
        with begin(engine) as connection:
            return {
                "project_id": project_id,
                "items": integrations.list_definitions(connection, project_id),
            }

    @router.post("/v1/integration-registry/definitions", status_code=201)
    @permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
    def create_integration_definition(
        payload: IntegrationDefinitionCreateRequest,
    ) -> dict[str, Any]:
        return mutate(
            payload,
            "providerops_manage",
            lambda connection: integrations.create_definition(
                connection, **payload.model_dump()
            ),
        )

    @router.get("/v1/integration-registry/connections")
    @permit(P.INTEGRATION_READ, scope=Scope.project(query="project_id"))
    def list_integration_connections(project_id: str = Query(...)) -> dict[str, Any]:
        with begin(engine) as connection:
            return {
                "project_id": project_id,
                "items": integrations.list_connections(connection, project_id),
            }

    @router.post("/v1/integration-registry/connections", status_code=201)
    @permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
    def create_integration_connection(
        payload: IntegrationConnectionCreateRequest,
    ) -> dict[str, Any]:
        return mutate(
            payload,
            "providerops_manage",
            lambda connection: integrations.create_connection(
                connection, **payload.model_dump()
            ),
        )

    @router.patch("/v1/integration-registry/connections/{connection_id}")
    @permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
    def update_integration_connection(
        connection_id: str, payload: IntegrationConnectionUpdateRequest
    ) -> dict[str, Any]:
        return mutate(
            payload,
            "providerops_manage",
            lambda connection: integrations.update_connection(
                connection, connection_id=connection_id, **payload.model_dump()
            ),
        )

    @router.post("/v1/integration-registry/resolve")
    @permit(P.WORKFLOW_RUN_START, scope=Scope.project(body="project_id"))
    def resolve_capability(payload: CapabilityResolveRequest) -> dict[str, Any]:
        with begin(engine) as connection:
            require_actor(
                connection, payload.project_id, payload.actor_ref, "workflow_execute"
            )
            try:
                return integrations.resolve_capability(
                    connection, **payload.model_dump()
                )
            except (CollaborationConflictError, CollaborationNotFoundError) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/v1/integration-registry/resolutions")
    @permit(P.INTEGRATION_READ, scope=Scope.project(query="project_id"))
    def list_capability_resolutions(
        project_id: str = Query(...), limit: int = Query(100, ge=1, le=500)
    ) -> dict[str, Any]:
        with begin(engine) as connection:
            return {
                "project_id": project_id,
                "items": integrations.list_resolutions(connection, project_id, limit),
            }

    @router.get("/v1/agent-groups")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def list_agent_groups(project_id: str = Query(...)) -> dict[str, Any]:
        with begin(engine) as connection:
            return {
                "project_id": project_id,
                "items": groups.list_groups(connection, project_id),
            }

    @router.post("/v1/agent-groups", status_code=201)
    @permit(P.AGENT_PUBLISH, scope=Scope.project(body="project_id"))
    def create_agent_group(payload: AgentGroupCreateRequest) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_manage",
            lambda connection: groups.create_group(connection, **payload.model_dump()),
        )

    @router.get("/v1/agent-groups/runs")
    @permit(P.WORKFLOW_RUN_READ, scope=Scope.project(query="project_id"))
    def list_agent_group_runs(project_id: str = Query(...)) -> dict[str, Any]:
        with begin(engine) as connection:
            return {
                "project_id": project_id,
                "items": groups.list_runs(connection, project_id),
            }

    @router.post("/v1/agent-groups/runs", status_code=201)
    @permit(P.WORKFLOW_RUN_START, scope=Scope.project(body="project_id"))
    def create_agent_group_run(payload: AgentGroupRunCreateRequest) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_execute",
            lambda connection: groups.start_run(connection, **payload.model_dump()),
        )

    @router.post("/v1/agent-groups/runs/{run_id}/members/{member_key}/complete")
    @permit(P.WORKER_LEASE)
    def complete_agent_group_member(
        run_id: str, member_key: str, payload: AgentGroupMemberCompleteRequest
    ) -> dict[str, Any]:
        return worker(
            lambda connection: groups.complete_member(
                connection, run_id=run_id, member_key=member_key, **payload.model_dump()
            )
        )

    @router.post("/v1/agent-groups/runs/{run_id}/finalize")
    @permit(P.WORKFLOW_RUN_CONTROL, scope=Scope.project(body="project_id"))
    def finalize_agent_group_run(
        run_id: str, payload: AgentGroupFinalizeRequest
    ) -> dict[str, Any]:
        return mutate(
            payload,
            "workflow_execute",
            lambda connection: groups.finalize_run(
                connection, run_id=run_id, **payload.model_dump()
            ),
        )

    return router


__all__ = ["build_governance_router"]
