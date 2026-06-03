from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

DataBoundary = Literal["public", "internal", "confidential", "restricted", "local-only"]


def _require_text(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank.")
    return cleaned


def _require_non_empty_string_list(values: list[str], field_name: str) -> list[str]:
    cleaned = [item.strip() for item in values if isinstance(item, str) and item.strip()]
    if not cleaned:
        raise ValueError(f"{field_name} must contain at least one non-empty item.")
    return cleaned


def _require_non_empty_mapping(
    values: dict[str, Any], field_name: str
) -> dict[str, Any]:
    if not values:
        raise ValueError(f"{field_name} must contain at least one entry.")
    return values


class HealthResponse(BaseModel):
    status: str
    database_connected: bool
    github_configured: bool
    openai_configured: bool


class ProjectSummary(BaseModel):
    id: str
    name: str
    slug: str
    workspace_id: str
    workspace_name: str
    org_id: str
    org_name: str
    created_at: str


class ProjectListResponse(BaseModel):
    items: list[ProjectSummary]


class AuthSessionResponse(BaseModel):
    auth_required: bool
    transport_authenticated: bool
    actor_binding_required: bool
    principal_member_id: Optional[str] = None
    message: str


class ProjectMemberSummary(BaseModel):
    id: str
    project_id: str
    member_id: str
    display_name: str
    member_type: str
    role_keys: list[str]
    permissions: list[str]
    status: str
    created_by: str
    created_at: str
    updated_at: str


class ProjectMemberListResponse(BaseModel):
    project_id: str
    items: list[ProjectMemberSummary]


class ProjectMemberUpsertRequest(BaseModel):
    project_id: str
    member_id: str
    display_name: str
    member_type: str = "human"
    role_keys: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_member_fields(self) -> "ProjectMemberUpsertRequest":
        self.member_id = _require_text(self.member_id, "member_id")
        self.display_name = _require_text(self.display_name, "display_name")
        self.member_type = _require_text(self.member_type, "member_type")
        self.role_keys = _require_non_empty_string_list(
            self.role_keys, "role_keys"
        )
        self.permissions = _require_non_empty_string_list(
            self.permissions, "permissions"
        )
        self.actor = _require_text(self.actor, "actor")
        return self


class BreakGlassGrantSummary(BaseModel):
    id: str
    project_id: str
    target_member_id: str
    requested_by: str
    reason: str
    scope: dict[str, Any] = Field(default_factory=dict)
    granted_permissions: list[str]
    status: str
    expires_at: str
    postmortem_task_id: Optional[str] = None
    revoked_by: Optional[str] = None
    revoked_reason: Optional[str] = None
    revoked_at: Optional[str] = None
    created_at: str
    updated_at: str


class BreakGlassGrantListResponse(BaseModel):
    project_id: str
    items: list[BreakGlassGrantSummary]


class BreakGlassGrantCreateRequest(BaseModel):
    project_id: str
    target_member_id: str
    reason: str
    scope: dict[str, Any] = Field(default_factory=dict)
    granted_permissions: list[str]
    ttl_minutes: int = Field(default=60, ge=5, le=1440)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_grant(self) -> "BreakGlassGrantCreateRequest":
        self.target_member_id = _require_text(
            self.target_member_id, "target_member_id"
        )
        self.reason = _require_text(self.reason, "reason")
        self.granted_permissions = _require_non_empty_string_list(
            self.granted_permissions,
            "granted_permissions",
        )
        self.actor = _require_text(self.actor, "actor")
        return self


class BreakGlassGrantRevokeRequest(BaseModel):
    actor: str = "ceo"
    reason: str

    @model_validator(mode="after")
    def validate_revoke(self) -> "BreakGlassGrantRevokeRequest":
        self.actor = _require_text(self.actor, "actor")
        self.reason = _require_text(self.reason, "reason")
        return self


class StatusResponse(BaseModel):
    project_id: str
    project_name: str
    workspace_name: str
    org_name: str
    database_connected: bool
    api_reachable: bool = True
    budget_state: str
    pending_decisions: int
    open_tasks: int
    deadletters: int = 0
    active_jobs: int = 0
    top_risks: list[dict[str, Any]] = Field(default_factory=list)
    latest_snapshot_id: Optional[str] = None
    latest_snapshot_at: Optional[str] = None
    blocked_reason: Optional[str] = None


class MetatronReference(BaseModel):
    object_type: str
    object_id: str
    title: str
    status: Optional[str] = None
    detail: Optional[str] = None
    created_at: Optional[str] = None


class MetatronQueryRequest(BaseModel):
    project_id: str
    question: str
    task_id: Optional[str] = None
    decision_id: Optional[str] = None
    limit: int = Field(default=5, ge=1, le=25)


class MetatronQueryResponse(BaseModel):
    project_id: str
    question: str
    category: str
    answer: str
    freshness_at: str
    references: list[MetatronReference] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    top_risks: list[dict[str, Any]] = Field(default_factory=list)


class HealthSnapshotSummary(BaseModel):
    id: str
    project_id: str
    generated_by: str
    budget_state: str
    queue_state: dict[str, Any] = Field(default_factory=dict)
    workload_state: dict[str, Any] = Field(default_factory=dict)
    incident_state: dict[str, Any] = Field(default_factory=dict)
    top_risks: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class HealthSnapshotListResponse(BaseModel):
    project_id: str
    items: list[HealthSnapshotSummary]


class HealthSnapshotRefreshRequest(BaseModel):
    project_id: str
    actor: str = "metatron"


class CommandProposeRequest(BaseModel):
    project_id: str
    command: str
    constraints: dict[str, Any] = Field(default_factory=dict)
    actor: str = "executive_console"


class CommandProposeResponse(BaseModel):
    status: str
    task_id: str
    job_id: str
    artifact_id: str


class BudgetReserveRequest(BaseModel):
    project_id: str
    category: str
    amount_usd_micros: int = Field(gt=0)
    reason: str
    job_id: Optional[str] = None
    allocation_id: Optional[str] = None
    expires_in_minutes: int = Field(default=30, ge=5, le=240)


class BudgetReserveResponse(BaseModel):
    status: str
    reservation_id: Optional[str] = None
    decision_id: Optional[str] = None
    budget_state: str
    message: str


class BudgetQuoteRequest(BaseModel):
    project_id: str
    category: str
    amount_usd_micros: int = Field(gt=0)
    reason: str


class BudgetQuoteResponse(BaseModel):
    allowed: bool
    category: str
    reason: str
    requested_usd_micros: int
    budget_state: str
    post_reservation_budget_state: str
    cap_usd_micros: int
    available_usd_micros: int
    post_reservation_available_usd_micros: int


class BudgetReleaseRequest(BaseModel):
    project_id: str
    reservation_id: str
    reason: str


class BudgetReleaseResponse(BaseModel):
    status: str
    reservation_id: str
    budget_state: str
    message: str


class BudgetStatusResponse(BaseModel):
    scope_type: str
    scope_id: str
    budget_state: str
    cap_usd_micros: int
    reserved_usd_micros: int
    actual_usd_micros: int
    available_usd_micros: int
    committed_usd_micros: int
    burn_approx_usd_micros: int
    burn_window_hours: int
    burn_rate_usd_micros_per_hour: int
    forecasted_24h_usd_micros: int
    time_to_cap_seconds: Optional[int] = None
    utilization_ratio: float
    active_reservations_count: int
    top_cost_drivers: list[dict[str, Any]]


class BudgetAllocationSummary(BaseModel):
    id: str
    parent_allocation_id: Optional[str] = None
    project_id: str
    scope_type: str
    scope_id: str
    amount_usd_micros: int
    child_allocated_usd_micros: int
    reserved_usd_micros: int
    actual_usd_micros: int
    available_usd_micros: int
    period_start: str
    period_end: str
    status: str
    created_at: str


class BudgetAllocationListResponse(BaseModel):
    project_id: str
    items: list[BudgetAllocationSummary]


class BudgetAllocationCreateRequest(BaseModel):
    project_id: str
    scope_type: str
    scope_id: str
    amount_usd_micros: int = Field(gt=0)
    period_start: datetime
    period_end: datetime
    parent_allocation_id: Optional[str] = None
    actor: str = "ceo"


class OpenAIPlanRequest(BaseModel):
    project_id: str
    reservation_id: str
    dedupe_key: str
    prompt: str
    system_prompt: Optional[str] = None
    agent_module_id: Optional[str] = None
    created_by_job_id: Optional[str] = None


class OpenAIPlanResponse(BaseModel):
    status: str
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    output_text: Optional[str] = None
    decision_id: Optional[str] = None
    message: Optional[str] = None


class FileChange(BaseModel):
    path: str
    content: str


class PullRequestProposalRequest(BaseModel):
    project_id: str
    task_id: Optional[str] = None
    reservation_id: str
    dedupe_key: str
    title: str
    branch_name: str
    summary: str
    files: list[FileChange]
    agent_module_id: Optional[str] = None
    created_by_job_id: Optional[str] = None


class PullRequestProposalJobRequest(PullRequestProposalRequest):
    task_id: Optional[str] = None
    priority: int = Field(default=50, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)


class PullRequestProposalResponse(BaseModel):
    status: str
    data_boundary: Optional[DataBoundary] = None
    artifact_id: Optional[str] = None
    diff_artifact_id: Optional[str] = None
    test_report_artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    pull_request_url: Optional[str] = None
    pull_request_number: Optional[int] = None
    branch_name: Optional[str] = None
    validation_status: Optional[str] = None
    validation_summary: Optional[str] = None
    validation_sources: list[str] = Field(default_factory=list)
    local_validation_receipt_id: Optional[str] = None
    message: Optional[str] = None


class PullRequestMergeRequest(BaseModel):
    project_id: str
    decision_id: str
    dedupe_key: str
    pull_request_number: int = Field(gt=0)
    pull_request_url: str
    title: str
    branch_name: Optional[str] = None
    created_by_job_id: Optional[str] = None


class PullRequestMergeJobRequest(PullRequestMergeRequest):
    task_id: Optional[str] = None
    priority: int = Field(default=30, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)


class SandboxDeploymentRequest(BaseModel):
    project_id: str
    decision_id: str
    dedupe_key: str
    merged_sha: str
    pull_request_url: str
    title: str
    environment: str
    branch_name: Optional[str] = None
    created_by_job_id: Optional[str] = None


class SandboxDeploymentJobRequest(SandboxDeploymentRequest):
    task_id: Optional[str] = None
    priority: int = Field(default=20, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)


class PullRequestMergeResponse(BaseModel):
    status: str
    data_boundary: Optional[DataBoundary] = None
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    deployment_decision_id: Optional[str] = None
    pull_request_url: Optional[str] = None
    branch_name: Optional[str] = None
    merged_sha: Optional[str] = None
    message: Optional[str] = None


class SandboxDeploymentResponse(BaseModel):
    status: str
    data_boundary: Optional[DataBoundary] = None
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    health_decision_id: Optional[str] = None
    health_verification_artifact_id: Optional[str] = None
    health_verification_status: Optional[str] = None
    healthcheck_url: Optional[str] = None
    rollout_id: Optional[str] = None
    rollout_artifact_id: Optional[str] = None
    rollout_status: Optional[str] = None
    rollout_stage: Optional[str] = None
    environment: Optional[str] = None
    deployment_id: Optional[int] = None
    deployment_status: Optional[str] = None
    deployment_url: Optional[str] = None
    merged_sha: Optional[str] = None
    message: Optional[str] = None


class InfraProvisionReviewRequest(BaseModel):
    project_id: str
    task_id: Optional[str] = None
    reservation_id: str
    title: str
    region: str
    size: str
    ttl_minutes: int = Field(ge=5, le=10_080)
    tags: list[str] = Field(default_factory=list)
    data_boundary: Optional[DataBoundary] = None
    actor: str = "ceo"


class InfraProvisionReviewResponse(BaseModel):
    status: str
    artifact_id: str
    decision_id: str
    data_boundary: DataBoundary = "internal"


class InfraProvisionRequest(BaseModel):
    project_id: str
    decision_id: str
    reservation_id: str
    dedupe_key: str
    title: str
    region: str
    size: str
    ttl_minutes: int = Field(ge=5, le=10_080)
    tags: list[str] = Field(default_factory=list)
    task_id: Optional[str] = None
    data_boundary: Optional[DataBoundary] = None
    created_by_job_id: Optional[str] = None


class InfraProvisionJobRequest(InfraProvisionRequest):
    actor: str = "release_manager"
    priority: int = Field(default=15, ge=0)
    max_attempts: int = Field(default=1, ge=1, le=20)


class InfraProvisionResponse(BaseModel):
    status: str
    resource_id: Optional[str] = None
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    provider_ref: Optional[str] = None
    provider_resource_id: Optional[str] = None
    resource_name: Optional[str] = None
    expires_at: Optional[str] = None
    data_boundary: Optional[DataBoundary] = None
    message: Optional[str] = None


class InfraTerminationRequest(BaseModel):
    project_id: str
    dedupe_key: str
    actor: str = "release_manager"
    reason: Optional[str] = None
    created_by_job_id: Optional[str] = None


class InfraTerminationJobRequest(InfraTerminationRequest):
    priority: int = Field(default=10, ge=0)
    max_attempts: int = Field(default=1, ge=1, le=20)


class InfraTerminationResponse(BaseModel):
    status: str
    resource_id: Optional[str] = None
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    provider_ref: Optional[str] = None
    message: Optional[str] = None


class InfraJanitorRunRequest(BaseModel):
    project_id: str
    actor: str = "system"
    limit: int = Field(default=20, ge=1, le=200)


class InfraJanitorRunResponse(BaseModel):
    status: str
    project_id: str
    queued_job_ids: list[str] = Field(default_factory=list)
    resource_ids: list[str] = Field(default_factory=list)


class InfraResourceSummary(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    decision_id: Optional[str] = None
    provider: str
    resource_type: str
    resource_name: str
    external_resource_id: str
    region: str
    size: str
    image: str
    tags: list[str] = Field(default_factory=list)
    status: str
    ttl_minutes: int
    expires_at: str
    terminated_at: Optional[str] = None
    termination_reason: Optional[str] = None
    provision_receipt_id: Optional[str] = None
    provision_artifact_id: Optional[str] = None
    termination_receipt_id: Optional[str] = None
    termination_artifact_id: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by_job_id: Optional[str] = None
    updated_by_job_id: Optional[str] = None
    created_at: str
    updated_at: str


class InfraResourceListResponse(BaseModel):
    project_id: str
    items: list[InfraResourceSummary]


class DeploymentRolloutSummary(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    decision_id: Optional[str] = None
    deployment_receipt_id: Optional[str] = None
    deployment_artifact_id: Optional[str] = None
    rollout_artifact_id: Optional[str] = None
    title: str
    environment: str
    merged_sha: str
    pull_request_url: str
    branch_name: Optional[str] = None
    status: str
    current_stage: str
    health_verification_status: Optional[str] = None
    rollback_decision_id: Optional[str] = None
    stages: list[dict[str, Any]]
    created_by_job_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class DeploymentRolloutListResponse(BaseModel):
    project_id: str
    items: list[DeploymentRolloutSummary]


class ReceiptResponse(BaseModel):
    id: str
    project_id: str
    dedupe_key: str
    effect_kind: str
    status: str
    effect_scope: dict[str, Any] = Field(default_factory=dict)
    payload_hash: Optional[str] = None
    provider_ref: Optional[str] = None
    created_by_job_id: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    created_at: str
    updated_at: str


class ReceiptListResponse(BaseModel):
    project_id: str
    items: list[ReceiptResponse]


class JobSummary(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    kind: str
    status: str
    data_boundary: DataBoundary = "internal"
    priority: int
    max_attempts: int
    attempt: int
    run_after: str


class JobCreateRequest(BaseModel):
    project_id: str
    kind: str
    task_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    data_boundary: Optional[DataBoundary] = None
    requires_reservation: bool = False
    reservation_id: Optional[str] = None
    priority: int = Field(default=100, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    run_after_seconds: int = Field(default=0, ge=0, le=86_400)


class LeasedJob(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    kind: str
    payload: dict[str, Any]
    data_boundary: DataBoundary = "internal"
    attempt: int
    max_attempts: int
    lease_token: str
    lease_expires_at: str


class WorkerPollRequest(BaseModel):
    worker_id: str
    capabilities: list[str]
    max_jobs: int = Field(default=1, ge=1, le=1)
    lease_ttl_seconds: int = Field(default=60, ge=5, le=600)


class WorkerPollResponse(BaseModel):
    status: str
    job: Optional[LeasedJob] = None


class WorkerHeartbeatRequest(BaseModel):
    worker_id: str
    job_id: str
    lease_token: str
    lease_ttl_seconds: int = Field(default=60, ge=5, le=600)


class WorkerHeartbeatResponse(BaseModel):
    job_id: str
    status: str
    lease_expires_at: str


class WorkerCompleteRequest(BaseModel):
    worker_id: str
    job_id: str
    lease_token: str
    result: dict[str, Any] = Field(default_factory=dict)


class WorkerCompleteResponse(BaseModel):
    job_id: str
    status: str
    artifact_id: Optional[str] = None
    diff_artifact_id: Optional[str] = None
    test_report_artifact_id: Optional[str] = None
    decision_id: Optional[str] = None
    receipt_id: Optional[str] = None
    deployment_decision_id: Optional[str] = None
    pull_request_url: Optional[str] = None
    merged_sha: Optional[str] = None
    branch_name: Optional[str] = None
    validation_status: Optional[str] = None
    validation_summary: Optional[str] = None
    validation_sources: list[str] = Field(default_factory=list)
    local_validation_receipt_id: Optional[str] = None
    environment: Optional[str] = None
    deployment_id: Optional[int] = None
    deployment_status: Optional[str] = None
    deployment_url: Optional[str] = None
    rollout_id: Optional[str] = None
    rollout_artifact_id: Optional[str] = None
    rollout_status: Optional[str] = None
    rollout_stage: Optional[str] = None
    resource_id: Optional[str] = None
    provider_ref: Optional[str] = None
    provider_resource_id: Optional[str] = None
    resource_name: Optional[str] = None
    expires_at: Optional[str] = None


class WorkerFailRequest(BaseModel):
    worker_id: str
    job_id: str
    lease_token: str
    error: dict[str, Any]
    retryable: bool = True
    retry_delay_seconds: int = Field(default=30, ge=0, le=3600)


class WorkerFailResponse(BaseModel):
    job_id: str
    status: str
    run_after: Optional[str] = None
    deadletter_id: Optional[str] = None


class TaskSummary(BaseModel):
    id: str
    project_id: str
    title: str
    description: Optional[str] = None
    status: str
    owner_role_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    dependency_task_ids: list[str] = Field(default_factory=list)
    due_at: Optional[str] = None
    priority: str
    risk_tier: int
    data_boundary: DataBoundary = "internal"
    dedupe_key: str
    created_at: str
    updated_at: str


class TaskListResponse(BaseModel):
    items: list[TaskSummary]


class TaskCreateRequest(BaseModel):
    project_id: str
    title: str
    description: Optional[str] = None
    owner_role_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    dependency_task_ids: list[str] = Field(default_factory=list)
    due_at: Optional[datetime] = None
    priority: str = "p2"
    risk_tier: int = Field(default=1, ge=0, le=3)
    data_boundary: DataBoundary = "internal"
    dedupe_key: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    required_artifacts: list[str] = Field(default_factory=list)
    actor: str = "executive_console"


class DepartmentSummary(BaseModel):
    id: str
    project_id: str
    department_key: str
    name: str
    mission: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


class DepartmentCreateRequest(BaseModel):
    project_id: str
    department_key: str
    name: str
    mission: str
    actor: str = "ceo"


class DepartmentListResponse(BaseModel):
    project_id: str
    items: list[DepartmentSummary]


class PolicyRecordSummary(BaseModel):
    id: str
    project_id: str
    policy_key: str
    version: int
    title: str
    status: str
    body: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: str
    updated_at: str


class PolicyRecordCreateRequest(BaseModel):
    project_id: str
    policy_key: str
    version: int = Field(default=1, ge=1)
    title: str
    body: dict[str, Any]
    actor: str = "ceo"


class PolicyRecordListResponse(BaseModel):
    project_id: str
    items: list[PolicyRecordSummary]


class PlanSummary(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    title: str
    objective: str
    status: str
    phases: list[Any] = Field(default_factory=list)
    success_criteria: list[Any] = Field(default_factory=list)
    risks: list[Any] = Field(default_factory=list)
    artifact_id: Optional[str] = None
    created_by: str
    created_at: str
    updated_at: str


class PlanCreateRequest(BaseModel):
    project_id: str
    title: str
    objective: str
    task_id: Optional[str] = None
    phases: list[Any] = Field(default_factory=list)
    success_criteria: list[Any] = Field(default_factory=list)
    risks: list[Any] = Field(default_factory=list)
    artifact_id: Optional[str] = None
    actor: str = "ceo"


class PlanListResponse(BaseModel):
    project_id: str
    items: list[PlanSummary]


class MeetingSummary(BaseModel):
    id: str
    project_id: str
    title: str
    purpose: str
    status: str
    participants: list[str] = Field(default_factory=list)
    agenda: list[str] = Field(default_factory=list)
    minutes_artifact_id: Optional[str] = None
    decisions: list[Any] = Field(default_factory=list)
    created_by: str
    created_at: str
    updated_at: str


class MeetingCreateRequest(BaseModel):
    project_id: str
    title: str
    purpose: str
    participants: list[str] = Field(default_factory=list)
    agenda: list[str] = Field(default_factory=list)
    actor: str = "ceo"


class MeetingListResponse(BaseModel):
    project_id: str
    items: list[MeetingSummary]


class SelfImprovementProposalCreateRequest(BaseModel):
    project_id: str
    title: str
    summary: str
    change_ref: str
    diff_summary: str
    expected_impact: list[str] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    canary_scope: list[str] = Field(default_factory=list)
    rollback_plan: list[str] = Field(default_factory=list)
    success_metrics: list[str] = Field(default_factory=list)
    actor: str = "vp_agents"
    risk_tier: int = Field(default=2, ge=1, le=3)

    @model_validator(mode="after")
    def validate_change_control_evidence(self) -> "SelfImprovementProposalCreateRequest":
        self.title = _require_text(self.title, "title")
        self.summary = _require_text(self.summary, "summary")
        self.change_ref = _require_text(self.change_ref, "change_ref")
        self.diff_summary = _require_text(self.diff_summary, "diff_summary")
        self.expected_impact = _require_non_empty_string_list(
            self.expected_impact, "expected_impact"
        )
        self.test_plan = _require_non_empty_string_list(self.test_plan, "test_plan")
        self.canary_scope = _require_non_empty_string_list(
            self.canary_scope, "canary_scope"
        )
        self.rollback_plan = _require_non_empty_string_list(
            self.rollback_plan, "rollback_plan"
        )
        self.success_metrics = _require_non_empty_string_list(
            self.success_metrics, "success_metrics"
        )
        self.actor = _require_text(self.actor, "actor")
        return self


class SelfImprovementProposalSummary(BaseModel):
    id: str
    project_id: str
    task_id: str
    title: str
    summary: str
    change_ref: str
    diff_summary: str
    status: str
    risk_tier: int
    expected_impact: list[str] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    canary_scope: list[str] = Field(default_factory=list)
    rollback_plan: list[str] = Field(default_factory=list)
    success_metrics: list[str] = Field(default_factory=list)
    status_note: Optional[str] = None
    proposal_artifact_id: Optional[str] = None
    current_decision_id: Optional[str] = None
    canary_artifact_id: Optional[str] = None
    rollout_artifact_id: Optional[str] = None
    rollback_artifact_id: Optional[str] = None
    created_by: str
    created_at: str
    updated_at: str


class SelfImprovementProposalDetail(SelfImprovementProposalSummary):
    pass


class SelfImprovementProposalListResponse(BaseModel):
    project_id: str
    items: list[SelfImprovementProposalSummary]


class SelfImprovementCanaryRequest(BaseModel):
    actor: str = "release_manager"
    outcome: str
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_canary_evidence(self) -> "SelfImprovementCanaryRequest":
        self.actor = _require_text(self.actor, "actor")
        self.outcome = _require_text(self.outcome, "outcome")
        self.summary = _require_text(self.summary, "summary")
        self.metrics = _require_non_empty_mapping(self.metrics, "metrics")
        self.evidence_refs = _require_non_empty_string_list(
            self.evidence_refs, "evidence_refs"
        )
        return self


class SelfImprovementRolloutRequest(BaseModel):
    actor: str = "release_manager"
    summary: str
    external_ref: Optional[str] = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_rollout_evidence(self) -> "SelfImprovementRolloutRequest":
        self.actor = _require_text(self.actor, "actor")
        self.summary = _require_text(self.summary, "summary")
        if self.external_ref is not None:
            self.external_ref = _require_text(self.external_ref, "external_ref")
        self.metrics = _require_non_empty_mapping(self.metrics, "metrics")
        self.evidence_refs = _require_non_empty_string_list(
            self.evidence_refs, "evidence_refs"
        )
        return self


class SelfImprovementRollbackRequest(BaseModel):
    actor: str = "release_manager"
    reason: str
    external_ref: Optional[str] = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_rollback_evidence(self) -> "SelfImprovementRollbackRequest":
        self.actor = _require_text(self.actor, "actor")
        self.reason = _require_text(self.reason, "reason")
        if self.external_ref is not None:
            self.external_ref = _require_text(self.external_ref, "external_ref")
        self.metrics = _require_non_empty_mapping(self.metrics, "metrics")
        self.evidence_refs = _require_non_empty_string_list(
            self.evidence_refs, "evidence_refs"
        )
        return self


class DecisionCard(BaseModel):
    id: str
    task_id: Optional[str] = None
    question: str
    status: str
    risk_tier: int
    default_action: Optional[str] = None
    recommendation: dict[str, Any]
    options: list[dict[str, Any]]
    cost_summary: dict[str, Any]
    deadline_at: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    delegated_to: Optional[str] = None
    delegated_by: Optional[str] = None
    delegated_at: Optional[str] = None
    revision_requested_by: Optional[str] = None
    revision_requested_at: Optional[str] = None
    executed_by: Optional[str] = None
    executed_at: Optional[str] = None
    resolution_note: Optional[str] = None
    external_ref: Optional[str] = None
    created_at: str
    updated_at: str


class DecisionInboxResponse(BaseModel):
    project_id: str
    items: list[DecisionCard]


class DecisionActionRequest(BaseModel):
    actor: str = "executive_console"
    note: Optional[str] = None
    delegate_to: Optional[str] = None
    external_ref: Optional[str] = None


class DecisionDigestRequest(BaseModel):
    project_id: str
    channel: Optional[str] = None
    actor: str = "executive_console"
    max_items: int = Field(default=20, ge=1, le=100)
    low_risk_max: int = Field(default=1, ge=0, le=3)
    urgent_risk_min: int = Field(default=2, ge=0, le=3)
    dedupe_key: Optional[str] = None
    data_boundary: DataBoundary = "internal"


class DecisionDigestResponse(BaseModel):
    status: str
    artifact_id: str
    outbox_message_id: Optional[str] = None
    decision_count: int
    urgent_count: int
    batched_count: int
    body: str


class EventItem(BaseModel):
    id: str
    event_type: str
    actor: str
    entity_type: str
    entity_id: str
    data: dict[str, Any]
    created_at: str


class EventFeedResponse(BaseModel):
    project_id: str
    items: list[EventItem]


class ArtifactSummary(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    artifact_type: str
    title: str
    uri: Optional[str] = None
    content_hash: Optional[str] = None
    created_by_job_id: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    created_at: str


class ArtifactDetail(ArtifactSummary):
    content: dict[str, Any] = Field(default_factory=dict)


class ArtifactListResponse(BaseModel):
    project_id: str
    items: list[ArtifactSummary]


class ChangeJournalItem(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    artifact_type: str
    change_kind: str
    title: str
    summary: Optional[str] = None
    diff_summary: Optional[str] = None
    files_changed: list[str] = Field(default_factory=list)
    rollback_plan: Optional[str] = None
    created_by_job_id: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    created_at: str


class ChangeJournalDetail(ChangeJournalItem):
    content: dict[str, Any] = Field(default_factory=dict)


class ChangeJournalResponse(BaseModel):
    project_id: str
    items: list[ChangeJournalItem]


class DeadletterItem(BaseModel):
    id: str
    project_id: Optional[str] = None
    source_kind: str
    source_id: str
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class DeadletterListResponse(BaseModel):
    project_id: str
    items: list[DeadletterItem]


class BudgetReservationSummary(BaseModel):
    id: str
    project_id: str
    job_id: Optional[str] = None
    category: str
    amount_usd_micros: int
    status: str
    reason: Optional[str] = None
    expires_at: str
    created_at: str


class CostReceiptSummary(BaseModel):
    id: str
    project_id: str
    reservation_id: Optional[str] = None
    receipt_id: Optional[str] = None
    category: str
    skill_or_model_id: str
    estimated_usd_micros: int
    actual_usd_micros: Optional[int] = None
    status: str
    created_at: str


class AuditExportScope(BaseModel):
    task_id: Optional[str] = None
    decision_id: Optional[str] = None
    job_id: Optional[str] = None


class AuditExportCounts(BaseModel):
    tasks: int
    jobs: int
    decisions: int
    artifacts: int
    receipts: int
    budget_reservations: int
    cost_receipts: int
    routing_logs: int
    deadletters: int
    events: int


class AuditExportResponse(BaseModel):
    project_id: str
    project_name: str
    workspace_name: str
    org_name: str
    generated_at: str
    scope: AuditExportScope
    counts: AuditExportCounts
    chain: dict[str, list[str]]
    tasks: list[TaskSummary]
    jobs: list[JobSummary]
    decisions: list[DecisionCard]
    artifacts: list[ArtifactSummary]
    receipts: list[ReceiptResponse]
    budget_reservations: list[BudgetReservationSummary]
    cost_receipts: list[CostReceiptSummary]
    routing_logs: list["RoutingLogItem"]
    deadletters: list[DeadletterItem]
    events: list[EventItem]


class ReplayBundleRequest(BaseModel):
    project_id: str
    task_id: Optional[str] = None
    decision_id: Optional[str] = None
    job_id: Optional[str] = None
    actor: str = "system"


class ReplayBundleResponse(BaseModel):
    status: str
    artifact_id: str
    project_id: str
    artifact_type: str
    created_at: str
    scope: AuditExportScope
    counts: AuditExportCounts


class ComplianceEvidenceBundleRequest(BaseModel):
    project_id: str
    actor: str = "ceo"
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_period(self) -> "ComplianceEvidenceBundleRequest":
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError("period_end must be after period_start.")
        self.actor = _require_text(self.actor, "actor")
        return self


class ComplianceEvidenceCounts(BaseModel):
    project_members: int
    roles: int
    policy_records: int
    break_glass_grants: int
    decisions: int
    receipts: int
    budget_reservations: int
    cost_receipts: int
    deadletters: int
    compliance_events: int


class ComplianceEvidenceBundleResponse(BaseModel):
    status: str
    artifact_id: str
    project_id: str
    artifact_type: str
    created_at: str
    period: dict[str, Optional[str]]
    counts: ComplianceEvidenceCounts


class OutboxMessageCreateRequest(BaseModel):
    project_id: str
    channel: str
    subject: Optional[str] = None
    body: str
    data_boundary: DataBoundary = "internal"
    dedupe_key: str
    actor: str = "executive_console"


class OutboxMessageSummary(BaseModel):
    id: str
    project_id: str
    channel: str
    subject: Optional[str] = None
    body: str
    status: str
    data_boundary: DataBoundary = "internal"
    dedupe_key: str
    receipt_id: Optional[str] = None
    created_at: str
    updated_at: str


class OutboxMessageListResponse(BaseModel):
    project_id: str
    items: list[OutboxMessageSummary]


class OutboxQueueRequest(BaseModel):
    project_id: str
    reservation_id: str
    dedupe_key: str
    actor: str = "executive_console"
    priority: int = Field(default=25, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    urgency: Literal["low", "normal", "urgent"] = "normal"
    created_by_job_id: Optional[str] = None


class OutboxDeliveryResponse(BaseModel):
    status: str
    data_boundary: Optional[DataBoundary] = None
    message_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    provider_ref: Optional[str] = None
    message: Optional[str] = None


class InboxMessageIngestRequest(BaseModel):
    project_id: str
    channel: str
    subject: Optional[str] = None
    body: str
    raw_ref: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    actor: str = "system"


class InboxMessageSummary(BaseModel):
    id: str
    project_id: Optional[str] = None
    channel: str
    raw_ref: Optional[str] = None
    subject: Optional[str] = None
    body: str
    status: str
    data_boundary: DataBoundary = "internal"
    parsed: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class InboxMessageListResponse(BaseModel):
    project_id: str
    items: list[InboxMessageSummary]


class RoleSummary(BaseModel):
    id: str
    project_id: str
    role_key: str
    display_name: str
    autonomy_level: int
    source_module_id: Optional[str] = None
    source_module_version: Optional[str] = None
    trust_score: int
    successful_runs: int = 0
    policy_violation_count: int = 0
    duplicate_effect_attempt_count: int = 0
    hidden_failure_count: int = 0
    low_quality_output_count: int = 0
    scope_violation_count: int = 0
    last_reviewed_at: Optional[str] = None
    last_violation_at: Optional[str] = None
    created_at: str


class RoleDetail(RoleSummary):
    spec: dict[str, Any]


class RoleListResponse(BaseModel):
    project_id: str
    items: list[RoleSummary]


class RoleReviewRequest(BaseModel):
    project_id: str
    actor: str = "vp_agents"
    summary: str
    metrics: dict[str, Any]
    evidence_refs: list[str]

    @model_validator(mode="after")
    def validate_fields(self) -> "RoleReviewRequest":
        self.actor = _require_text(self.actor, "actor")
        self.summary = _require_text(self.summary, "summary")
        self.metrics = _require_non_empty_mapping(self.metrics, "metrics")
        self.evidence_refs = _require_non_empty_string_list(
            self.evidence_refs, "evidence_refs"
        )
        return self


class RoleAutonomyCheckRequest(BaseModel):
    project_id: str
    actor: str = "system"
    action: str
    risk_tier: int = Field(default=0, ge=0, le=3)
    sandboxed: bool = False
    policy_approved: bool = False

    @model_validator(mode="after")
    def validate_fields(self) -> "RoleAutonomyCheckRequest":
        self.actor = _require_text(self.actor, "actor")
        self.action = _require_text(self.action, "action")
        return self


class RoleAutonomyCheckResponse(BaseModel):
    project_id: str
    role_key: str
    allowed: bool
    reason: str
    action: str
    required_capability: str
    risk_tier: int
    sandboxed: bool
    policy_approved: bool
    current_level: int
    required_level: Optional[int] = None
    allowed_capabilities: list[str] = Field(default_factory=list)
    trust_score: int


class RoleScopeCheckRequest(BaseModel):
    project_id: str
    actor: str = "system"
    action: str
    skill_id: Optional[str] = None
    evidence_ref: Optional[str] = None

    @model_validator(mode="after")
    def validate_fields(self) -> "RoleScopeCheckRequest":
        self.actor = _require_text(self.actor, "actor")
        self.action = _require_text(self.action, "action")
        if self.skill_id is not None:
            self.skill_id = _require_text(self.skill_id, "skill_id")
        if self.evidence_ref is not None:
            self.evidence_ref = _require_text(self.evidence_ref, "evidence_ref")
        return self


class RoleScopeCheckResponse(BaseModel):
    project_id: str
    role_key: str
    allowed: bool
    reason: str
    action: str
    skill_id: str
    allowed_skills: list[str] = Field(default_factory=list)
    forbidden_skills: list[str] = Field(default_factory=list)
    trust_score_before: int
    trust_score_after: int
    autonomy_level_before: int
    autonomy_level_after: int
    scope_violation_count: int
    artifact_id: Optional[str] = None
    autonomy_change_id: Optional[str] = None


class RoleReviewSummary(BaseModel):
    id: str
    role_key: str
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    trust_score_before: int
    trust_delta: int
    trust_score_after: int
    autonomy_level_before: int
    autonomy_level_after: int
    governance_outcome: str
    autonomy_change_id: Optional[str] = None
    artifact_id: Optional[str] = None
    created_by: str
    created_at: str


class AutonomyChangeSummary(BaseModel):
    id: str
    role_key: str
    decision_id: Optional[str] = None
    change_type: str
    trigger: str
    from_level: int
    to_level: int
    status: str
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    created_at: str
    updated_at: str


class RoleGovernanceDetail(BaseModel):
    project_id: str
    role: RoleDetail
    policy: dict[str, Any] = Field(default_factory=dict)
    pending_autonomy_change: Optional[AutonomyChangeSummary] = None
    recent_reviews: list[RoleReviewSummary] = Field(default_factory=list)
    recent_autonomy_changes: list[AutonomyChangeSummary] = Field(default_factory=list)


class PackSummary(BaseModel):
    pack_id: str
    version: str
    description: str
    status: str
    requires: list[str]
    roles: list[str]
    workflows: list[str]
    policies: list[str]
    skills: list[str]
    installed: bool
    installed_by: Optional[str] = None
    installed_at: Optional[str] = None
    updated_at: Optional[str] = None
    installed_version: Optional[str] = None


class PackDetail(PackSummary):
    role_count: Optional[int] = None
    role_results: list[dict[str, Any]] = Field(default_factory=list)
    install_status: Optional[str] = None


class PackListResponse(BaseModel):
    project_id: str
    items: list[PackSummary]


class PackInstallRequest(BaseModel):
    project_id: str
    pack_id: str
    actor: str = "system"


class AgentModuleSummary(BaseModel):
    module_id: str
    version: str
    display_name: str
    description: str
    publisher: str
    category: str
    status: str
    role_key: Optional[str] = None
    job_kinds: list[str] = Field(default_factory=list)
    default_skill_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    price_model: Optional[str] = None
    price_usd_micros: Optional[int] = None
    config_schema: dict[str, Any] = Field(default_factory=dict)
    installed: bool
    installed_by: Optional[str] = None
    installed_at: Optional[str] = None
    updated_at: Optional[str] = None
    installed_version: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)


class AgentModuleDetail(AgentModuleSummary):
    prompt_bundle: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    marketplace: dict[str, Any] = Field(default_factory=dict)
    role: dict[str, Any] = Field(default_factory=dict)
    role_result: dict[str, Any] = Field(default_factory=dict)
    install_status: Optional[str] = None


class AgentModuleListResponse(BaseModel):
    project_id: str
    items: list[AgentModuleSummary]


class AgentModuleInstallRequest(BaseModel):
    project_id: str
    module_id: str
    actor: str = "system"
    config: dict[str, Any] = Field(default_factory=dict)


class SkillSummary(BaseModel):
    id: str
    kind: str
    risk_tier: int
    description: str
    mock_mode: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    cost: dict[str, Any]
    retry: dict[str, Any]
    permissions: dict[str, Any]
    data_boundary: str
    policy: dict[str, Any]


class SkillListResponse(BaseModel):
    items: list[SkillSummary]


class EntitlementSourceSummary(BaseModel):
    id: str
    project_id: str
    provider: str
    source_id: str
    source_kind: str
    entitlement_kind: str
    owner_type: str
    owner_id: str
    workload_classes_allowed: list[str]
    multi_tenant_allowed: bool
    credential_ref: Optional[str] = None
    cost_mode: str
    limit_shape: dict[str, Any]
    supported_models: list[str]
    task_strengths: list[str]
    risk_tier_max: int
    status: str
    effective_status: str
    policy_notes: list[str]
    observed_capacity: dict[str, Any]
    runtime_supported: bool
    created_at: str
    updated_at: str


class EntitlementSourceListResponse(BaseModel):
    project_id: str
    items: list[EntitlementSourceSummary]


class EntitlementSourceUpsertRequest(BaseModel):
    project_id: str
    provider: str
    source_id: str
    source_kind: str
    entitlement_kind: str
    owner_type: str
    owner_id: str
    workload_classes_allowed: list[str] = Field(default_factory=list)
    multi_tenant_allowed: bool = False
    credential_ref: Optional[str] = None
    cost_mode: str = "usage_based_api"
    limit_shape: dict[str, Any] = Field(default_factory=dict)
    supported_models: list[str] = Field(default_factory=list)
    task_strengths: list[str] = Field(default_factory=list)
    risk_tier_max: int = 1
    status: str = "declared"
    policy_notes: list[str] = Field(default_factory=list)
    observed_capacity: dict[str, Any] = Field(default_factory=dict)
    runtime_supported: bool = False
    actor: str = "system"


class RoutingPreviewRequest(BaseModel):
    project_id: str
    skill_id: str
    task_kind: str
    workload_class: str
    risk_tier: int = Field(default=1, ge=0, le=3)
    data_boundary: DataBoundary = "internal"
    preferred_capabilities: list[str] = Field(default_factory=list)
    budget_state: Optional[str] = None
    actor: str = "system"


class RoutingPreviewResponse(BaseModel):
    route_decision_log_id: str
    skill_id: str
    task_kind: str
    workload_class: str
    risk_tier: int
    data_boundary: DataBoundary = "internal"
    budget_state: str
    source_id: Optional[str] = None
    provider: Optional[str] = None
    model_id: Optional[str] = None
    denial_reason: Optional[str] = None
    rejected_sources: list[dict[str, Any]]
    decision: dict[str, Any]


class RoutingLogItem(BaseModel):
    id: str
    project_id: str
    skill_id: Optional[str] = None
    task_kind: str
    workload_class: str
    risk_tier: int
    data_boundary: DataBoundary = "internal"
    budget_state: str
    selected_source_id: Optional[str] = None
    selected_provider: Optional[str] = None
    selected_model_id: Optional[str] = None
    denial_reason: Optional[str] = None
    rejected_sources: list[dict[str, Any]]
    decision: dict[str, Any]
    created_at: str


class RoutingLogListResponse(BaseModel):
    project_id: str
    items: list[RoutingLogItem]


AuditExportResponse.model_rebuild()
