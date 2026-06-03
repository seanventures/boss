from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    database_connected: bool
    github_configured: bool
    openai_configured: bool


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
    utilization_ratio: float
    active_reservations_count: int
    top_cost_drivers: list[dict[str, Any]]


class OpenAIPlanRequest(BaseModel):
    project_id: str
    reservation_id: str
    dedupe_key: str
    prompt: str
    system_prompt: Optional[str] = None
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
    created_by_job_id: Optional[str] = None


class PullRequestProposalJobRequest(PullRequestProposalRequest):
    task_id: Optional[str] = None
    priority: int = Field(default=50, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)


class PullRequestProposalResponse(BaseModel):
    status: str
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
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    environment: Optional[str] = None
    deployment_id: Optional[int] = None
    deployment_status: Optional[str] = None
    deployment_url: Optional[str] = None
    merged_sha: Optional[str] = None
    message: Optional[str] = None


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
    priority: int
    max_attempts: int
    attempt: int
    run_after: str


class JobCreateRequest(BaseModel):
    project_id: str
    kind: str
    task_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
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
    environment: Optional[str] = None
    deployment_id: Optional[int] = None
    deployment_status: Optional[str] = None
    deployment_url: Optional[str] = None


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
    priority: str
    risk_tier: int
    dedupe_key: str
    created_at: str
    updated_at: str


class TaskListResponse(BaseModel):
    items: list[TaskSummary]


class TaskCreateRequest(BaseModel):
    project_id: str
    title: str
    description: Optional[str] = None
    priority: str = "p2"
    risk_tier: int = Field(default=1, ge=0, le=3)
    dedupe_key: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    required_artifacts: list[str] = Field(default_factory=list)


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


class SelfImprovementRolloutRequest(BaseModel):
    actor: str = "release_manager"
    summary: str
    external_ref: Optional[str] = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)


class SelfImprovementRollbackRequest(BaseModel):
    actor: str = "release_manager"
    reason: str
    external_ref: Optional[str] = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)


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
    created_at: str


class ArtifactDetail(ArtifactSummary):
    content: dict[str, Any] = Field(default_factory=dict)


class ArtifactListResponse(BaseModel):
    project_id: str
    items: list[ArtifactSummary]


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


class OutboxMessageCreateRequest(BaseModel):
    project_id: str
    channel: str
    subject: Optional[str] = None
    body: str
    dedupe_key: str
    actor: str = "executive_console"


class OutboxMessageSummary(BaseModel):
    id: str
    project_id: str
    channel: str
    subject: Optional[str] = None
    body: str
    status: str
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
    created_by_job_id: Optional[str] = None


class OutboxDeliveryResponse(BaseModel):
    status: str
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
    actor: str = "system"


class InboxMessageSummary(BaseModel):
    id: str
    project_id: Optional[str] = None
    channel: str
    raw_ref: Optional[str] = None
    subject: Optional[str] = None
    body: str
    status: str
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
    created_at: str


class RoleDetail(RoleSummary):
    spec: dict[str, Any]


class RoleListResponse(BaseModel):
    project_id: str
    items: list[RoleSummary]


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


class SkillSummary(BaseModel):
    id: str
    kind: str
    risk_tier: int
    description: str
    mock_mode: bool
    cost: dict[str, Any]
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
    preferred_capabilities: list[str] = Field(default_factory=list)
    budget_state: Optional[str] = None
    actor: str = "system"


class RoutingPreviewResponse(BaseModel):
    route_decision_log_id: str
    skill_id: str
    task_kind: str
    workload_class: str
    risk_tier: int
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
