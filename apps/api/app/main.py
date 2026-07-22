from contextlib import asynccontextmanager
from contextvars import ContextVar
import asyncio
import json
import logging
import re
from secrets import compare_digest
import time
from collections import defaultdict, deque
from typing import Any, Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text

from .bootstrap import seed
from .config import get_settings
from .db import begin, database_is_reachable, ensure_schema, get_engine
from .schemas import (
    AgentModuleDetail,
    AgentModuleInstallRecordListResponse,
    AgentModuleInstallRecordSummary,
    AgentModuleInstallRequest,
    AgentModuleLifecycleRequest,
    AgentModuleListResponse,
    AgentRuntimeResolveRequest,
    AgentRuntimeResolveResponse,
    AgentModuleSummary,
    ArtifactDetail,
    ArtifactListResponse,
    ArtifactSummary,
    AuthSessionResponse,
    SamlLoginResponse,
    AccountConnectAuthSessionCompleteRequest,
    AccountConnectAuthSessionCreateRequest,
    AccountConnectAuthSessionListResponse,
    AccountConnectAuthSessionSummary,
    AccountConnectHealthCheckListResponse,
    AccountConnectHealthCheckRunRequest,
    AccountConnectHealthCheckSummary,
    AccountConnectLinkRequest,
    AccountConnectBillingPortalSessionCreateRequest,
    AccountConnectBillingPortalSessionListResponse,
    AccountConnectBillingPortalSessionSummary,
    AccountConnectMutationResponse,
    AccountConnectSyncRequest,
    ReliabilityScanRequest,
    ReliabilityScanResponse,
    ReliabilityIncidentListResponse,
    ReliabilityIncidentDetail,
    ReliabilityDiagnoseRequest,
    ReliabilityDiagnoseResponse,
    ReliabilityFeedbackRequest,
    ReliabilityFeedbackResponse,
    ReliabilityRemediateRequest,
    ReliabilityRemediateResponse,
    ReliabilityVerifyRequest,
    ReliabilityVerifyResponse,
    ReliabilitySourceRequest,
    ReliabilitySourceSummary,
    ReliabilitySourceListResponse,
    ReliabilityGraphWalkRequest,
    ReliabilityGraphWalkResponse,
    ReliabilityGraphResponse,
    ReliabilityPolicyRequest,
    ReliabilityPolicyResponse,
    ReliabilityDecisionResponse,
    AccountConnectSyncResponse,
    AccountConnectTeamInviteCreateRequest,
    AccountConnectTeamInviteListResponse,
    AccountConnectTeamInviteRevokeRequest,
    AccountConnectTeamInviteSummary,
    AccountConnectUnlinkRequest,
    AuditExportCounts,
    AuditExportResponse,
    AuditExportScope,
    BreakGlassGrantCreateRequest,
    BreakGlassGrantListResponse,
    BreakGlassGrantRevokeRequest,
    BreakGlassGrantSummary,
    BudgetAllocationCreateRequest,
    BudgetAllocationListResponse,
    BudgetAllocationSummary,
    BudgetConsumeRequest,
    BudgetConsumeResponse,
    BudgetForecastResponse,
    BudgetQuoteRequest,
    BudgetQuoteResponse,
    BudgetReservationSummary,
    BudgetReleaseRequest,
    BudgetReleaseResponse,
    BudgetSetIntensityRequest,
    AnomalyScanRequest,
    CanaryRunRequest,
    EvalRunRequest,
    PlanDispatchRequest,
    ProviderHealthPollRequest,
    RouterRationaleRequest,
    SkillInvokeRequest,
    SkillToggleRequest,
    CapabilityBundleImportRequest,
    SlackDigestBatchRequest,
    SlackRenderRequest,
    BudgetStatusResponse,
    BudgetReserveRequest,
    BudgetReserveResponse,
    ChangeJournalDetail,
    ChangeJournalResponse,
    AgentChatRequest,
    AgentChatResponse,
    CommandProposeRequest,
    CommandProposeResponse,
    MeetingProposeRequest,
    MeetingProposeResponse,
    CommsChannelPolicyListResponse,
    CommsChannelPolicySummary,
    CommsChannelPolicyUpsertRequest,
    ComplianceEvidenceBundleRequest,
    ComplianceEvidenceBundleResponse,
    ComplianceEvidenceCounts,
    ChangeSetApplyRequest,
    ChangeSetApplyResponse,
    ChangeSetApproveRequest,
    ChangeSetCreateRequest,
    ChangeSetListResponse,
    ChangeSetSummary,
    TerraformDriftReconcileRequest,
    TerraformDriftReconcileResponse,
    TerraformExportArtifactResponse,
    TerraformExportRequest,
    TerraformExportResponse,
    CostAnomalyListResponse,
    CostAnomalySummary,
    CostEventListResponse,
    CostEventSummary,
    CostReceiptSummary,
    DepartmentCreateRequest,
    DepartmentListResponse,
    DepartmentSummary,
    KpiCreateRequest,
    KpiListResponse,
    KpiSummary,
    DeploymentRolloutListResponse,
    DeploymentRolloutAdvanceRequest,
    DeploymentRolloutRollbackRequest,
    DeploymentRolloutStartRequest,
    DeploymentRolloutSummary,
    DeploymentEnvironmentPolicyListResponse,
    DeploymentEnvironmentPolicySummary,
    DeploymentEnvironmentPolicyUpsertRequest,
    DeploymentRunCreateRequest,
    DeploymentRunHealthCheckRequest,
    DeploymentRunHealthCheckResponse,
    DeploymentRunListResponse,
    DeploymentRunSummary,
    DeploymentPromotionAdvanceRequest,
    DeploymentPromotionListResponse,
    DeploymentPromotionStartRequest,
    DeploymentPromotionSummary,
    DeploymentTargetCreateRequest,
    DeploymentTargetListResponse,
    DeploymentTargetSummary,
    DecisionActionRequest,
    DecisionCard,
    DecisionDigestRequest,
    DecisionDigestResponse,
    DecisionInboxResponse,
    DeadletterIncidentRequest,
    DeadletterIncidentResponse,
    DeadletterItem,
    DeadletterListResponse,
    DeadletterRequeueRequest,
    DeadletterRequeueResponse,
    DriftReportListResponse,
    DriftReportSummary,
    CIConfigProposalRequest,
    CIConfigProposalResponse,
    DocsUpdateExecuteRequest,
    DocsUpdateRequest,
    EntitlementSourceConnectRequest,
    EntitlementSourceDisconnectRequest,
    EntitlementSourceListResponse,
    EntitlementSourceSummary,
    EntitlementSourceUpsertRequest,
    EntitlementConnectionSessionCompleteRequest,
    EntitlementConnectionSessionCompleteResponse,
    EntitlementConnectionSessionCreateRequest,
    EntitlementConnectionSessionListResponse,
    EntitlementConnectionSessionSummary,
    EnterpriseClusterListResponse,
    EnterpriseClusterSummary,
    EnterpriseClusterValidationRunCreateRequest,
    EnterpriseClusterValidationRunListResponse,
    EnterpriseClusterValidationRunSummary,
    EnterpriseClusterUpsertRequest,
    EnterpriseIdentityRolloutPolicyListResponse,
    EnterpriseIdentityRolloutPolicySummary,
    EnterpriseIdentityRolloutPolicyUpsertRequest,
    EnterpriseIdentityProviderListResponse,
    EnterpriseIdentityProviderSummary,
    EnterpriseIdentityProviderUpsertRequest,
    EnterpriseScimGroupListResponse,
    EnterpriseScimGroupSummary,
    EnterpriseScimGroupUpsertRequest,
    EnterpriseScimUserListResponse,
    EnterpriseScimUserSummary,
    EnterpriseScimUserUpsertRequest,
    EnterpriseSupportCaseCreateRequest,
    EnterpriseSupportCaseListResponse,
    EnterpriseSupportCaseSummary,
    EnterpriseSupportCaseUpdateRequest,
    EventFeedResponse,
    EventItem,
    EventSubscriptionCreateRequest,
    EventSubscriptionListResponse,
    EventSubscriptionSummary,
    EventSubscriptionUpdateRequest,
    FileChange,
    InboundWebhookResponse,
    GitHubCommunityJobExecuteRequest,
    WebhookDeliveryCreateRequest,
    WebhookDeliveryExecuteRequest,
    WebhookDeliveryListResponse,
    WebhookDeliverySummary,
    WebhookEndpointCreateRequest,
    WebhookEndpointListResponse,
    WebhookEndpointSummary,
    WebhookEndpointUpdateRequest,
    ExternalResourceListResponse,
    ExternalResourceSummary,
    HealthResponse,
    HealthSnapshotListResponse,
    HealthSnapshotRefreshRequest,
    HealthSnapshotSummary,
    HostedMarketplaceListingListResponse,
    HostedMarketplaceListingSummary,
    HostedMarketplaceSyncRequest,
    HostedMarketplaceSyncResponse,
    HostedMarketplaceSyncRunListResponse,
    HostedMarketplaceSyncRunSummary,
    InfraJanitorRunRequest,
    InfraJanitorRunResponse,
    InfraProvisionJobRequest,
    InfraProvisionRequest,
    InfraProvisionResponse,
    InfraProvisionReviewRequest,
    InfraProvisionReviewResponse,
    InfraResourceListResponse,
    InfraResourceSummary,
    InfraTerminationJobRequest,
    InfraTerminationRequest,
    InfraTerminationResponse,
    InboxMessageIngestRequest,
    InboxMessageListResponse,
    InboxMessageSummary,
    JobCreateRequest,
    JobListItem,
    JobListResponse,
    JobSummary,
    LeasedJob,
    MetatronQueryRequest,
    MetatronQueryResponse,
    McpServerRegisterRequest,
    OpenAIPlanRequest,
    OpenAIPlanResponse,
    MeetingCreateRequest,
    MeetingListResponse,
    ChaosRunRequest,
    CommitmentReconcileRequest,
    FactoryChunkPlanRequest,
    FactoryMergeCheckRequest,
    FactoryQualityGateRequest,
    InitiativeBudgetRequest,
    MeetingRunRequest,
    MeetingSweepRequest,
    MeetingTallyRequest,
    SOC2EvidenceRequest,
    MeetingSummary,
    OpsDigestRequest,
    OpsDigestResponse,
    OpsQueueInspectionResponse,
    OrgMemberListResponse,
    OrgMemberSummary,
    OrgMemberUpsertRequest,
    OutboxDeliveryResponse,
    OutboxMessageCreateRequest,
    OutboxMessageListResponse,
    OutboxMessageSummary,
    OutboxQueueRequest,
    PackDetail,
    PackImportRequest,
    PackInstallRequest,
    PackListResponse,
    PackSummary,
    ProjectMemberListResponse,
    ProjectMemberSummary,
    ProjectMemberUpsertRequest,
    ProjectListResponse,
    ProjectSummary,
    PlanCreateRequest,
    PlanListResponse,
    PlanSummary,
    PolicyRecordCreateRequest,
    PolicyRecordListResponse,
    PolicyRecordSummary,
    CapacityReceiptSummary,
    DataBoundary,
    EntitlementPoolListResponse,
    EntitlementPoolProbeRequest,
    EntitlementPoolProbeResponse,
    EntitlementPoolSummary,
    EditionStatusResponse,
    LicenseEntitlementListResponse,
    LicenseEntitlementSummary,
    ModelAdapterListResponse,
    ModelAdapterSummary,
    ModelRouteDecisionListResponse,
    ModelRouteDecisionSimulateRequest,
    ModelRouteDecisionSimulateResponse,
    ModelRouteDecisionSummary,
    ProviderAccountCreateRequest,
    ProviderAccountListResponse,
    ProviderAccountSummary,
    ProviderConnectionTestResponse,
    ProviderHealthListResponse,
    ProviderHealthSummary,
    PullRequestMergeJobRequest,
    PullRequestMergeRequest,
    PullRequestMergeResponse,
    PullRequestProposalJobRequest,
    PullRequestProposalRequest,
    PullRequestProposalResponse,
    ReceiptListResponse,
    ReceiptResponse,
    ReconciliationRunListResponse,
    ReconciliationRunSummary,
    ReplayBundleRequest,
    ReplayBundleResponse,
    ResourceJanitorCandidateSummary,
    ResourceJanitorRunRequest,
    ResourceJanitorRunResponse,
    ResourceJanitorSkippedSummary,
    ResourceReceiptListResponse,
    ResourceReceiptSummary,
    ResourceSyncRequest,
    ResourceSyncResponse,
    ResourceSyncRunSummary,
    AutonomyEvaluateRequest,
    RoleAutonomyCheckRequest,
    RoleAutonomyCheckResponse,
    RoleGovernanceDetail,
    RoleReviewRequest,
    RoleDetail,
    RoleListResponse,
    RoleScopeCheckRequest,
    RoleScopeCheckResponse,
    RoleSummary,
    RoutingLogItem,
    RoutingLogListResponse,
    RoutingPreviewRequest,
    RoutingPreviewResponse,
    SandboxDeploymentJobRequest,
    SandboxDeploymentRequest,
    SandboxDeploymentResponse,
    SecretRefCreateRequest,
    SecretRefListResponse,
    SecretRefSummary,
    SecretRefUpdateRequest,
    SelfImprovementCanaryRequest,
    SelfImprovementProposalCreateRequest,
    SelfImprovementProposalDetail,
    SelfImprovementProposalListResponse,
    SelfImprovementProposalSummary,
    SelfImprovementAdvanceRolloutRequest,
    SelfImprovementRollbackRequest,
    SelfImprovementRolloutRequest,
    SelfImprovementRunCanaryRequest,
    SelfImprovementStartRolloutRequest,
    ScalePlanListResponse,
    ScalePlanRunRequest,
    ScalePlanRunResponse,
    ScalePlanSummary,
    SkillListResponse,
    SkillSummary,
    StatusResponse,
    TaskCreateRequest,
    TaskListResponse,
    TaskSummary,
    WorkerCompleteRequest,
    WorkerCompleteResponse,
    WorkerFailRequest,
    WorkerFailResponse,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerPoolListResponse,
    WorkerPoolSummary,
    WorkerPoolUpsertRequest,
    WorkerPollRequest,
    WorkerPollResponse,
    WorkflowCompensationBeginRequest,
    WorkflowCompensationClaimRequest,
    WorkflowCompensationCompleteRequest,
    WorkflowCompensationFailRequest,
    WorkflowDefinitionCreateRequest,
    WorkflowDefinitionDetail,
    WorkflowDefinitionListResponse,
    WorkflowDefinitionSummary,
    WorkflowMappingCreateRequest,
    WorkflowMappingListResponse,
    WorkflowMappingResolveRequest,
    WorkflowMappingSummary,
    WorkflowMigrationRollbackRequest,
    WorkflowRunActorRequest,
    WorkflowRunCancelRequest,
    WorkflowRunDetail,
    WorkflowRunListResponse,
    WorkflowRunMigrateRequest,
    WorkflowRunStartRequest,
    WorkflowRunSummary,
    WorkflowStepClaimRequest,
    WorkflowStepCompleteRequest,
    WorkflowStepFailRequest,
    WorkflowStepHeartbeatRequest,
    WorkflowStepRunSummary,
    WorkflowVersionCreateRequest,
    WorkflowVersionSummary,
)
from .services.audit import AuditTrailService
from .services.agent_loop import conversation_from_messages, describe_native_tools
from .services.agent_runtime import AgentRuntimeService
from .services.budget import BudgetGovernor
from .services.break_glass import BreakGlassGrantNotFoundError, BreakGlassService
from .services.canonical import CanonicalObjectService
from .services.comms import (
    CommsService,
    InvalidCreatedByJobError,
    OutboxMessageNotFoundError,
)
from .services.commercial import (
    AccountConnectAuthSessionNotFoundError,
    AccountConnectAuthSessionStateError,
    AccountConnectConfigurationError,
    AccountConnectLinkNotFoundError,
    AccountConnectSyncError,
    CommercialService,
)
from .services.core import emit_event, make_id
from .services.deadletters import DeadletterRemediationService
from .services.docs_agent import DocsUpdateService
from .services.decisions import (
    DecisionNotFoundError,
    DecisionService,
    InvalidDecisionTransitionError,
)
from .services.entitlements import (
    EntitlementBrokerService,
    EntitlementConnectionSessionNotFoundError,
    EntitlementConnectionSessionStateError,
    EntitlementPoolNotFoundError,
    EntitlementSourceNotFoundError,
    InvalidEntitlementConnectionError,
    SkillNotFoundError,
)
from .services.enterprise import EnterpriseService
from .services.health import HealthSnapshotService
from .services.event_subscription_service import (
    create_event_subscription,
    delete_event_subscription,
    list_event_subscriptions,
    update_event_subscription,
)
from .services.webhooks import (
    InboundWebhookService,
    WebhookDeliveryNotFoundError,
    WebhookEndpointNotFoundError,
    WebhookService,
)
from .services.plane_webhooks import PlaneWebhookService
from .services.github_community_service import GitHubCommunityService
from .services.infrastructure import (
    InfraResourceNotFoundError,
    InfrastructureService,
)
from .services.reliability import ReliabilityService
from .services.intake import CommandIntakeService
from .services.agent_service import (
    AgentService,
    AgentThreadNotFoundError,
    AgentThreadOwnershipError,
)
from .services.jobs import JobLeaseService, LeaseNotFoundError
from .services.members import (
    DEFAULT_MEMBER_IDS,
    MemberDirectoryService,
    ProjectMemberNotFoundError,
    ProjectMemberPermissionError,
)
from .services.metatron import MetatronScopeNotFoundError, MetatronService
from .services.oidc import (
    OidcAuthenticationError,
    OidcAuthenticator,
    OidcConfigurationError,
)
from .services.saml import (
    SamlAuthenticationError,
    SamlConfigurationError,
    SamlSessionService,
)
from .services.ops import OpsQueueService
from .services.reports import ReportService
from .services.mcp_client import MCPClient
from .services.mcp_registry import (
    delete_mcp_server,
    list_mcp_servers,
    register_mcp_server,
    set_mcp_enabled,
)
from .services.skill_packaging import (
    CapabilityBundleError,
    CapabilityBundleService,
)
from .services.receipts import ReceiptService
from .services.resource_plane import (
    ChangeSetNotFoundError,
    ProviderAccountNotFoundError,
    ResourcePlaneService,
)
from .services import terraform_drift as terraform_drift_service
from .services import terraform_export as terraform_export_service
from .services.autonomy_engine import AutonomyEngine
from .services.role_governance import RoleGovernanceService
from .services.router_health import RouterHealthService
from .services.skill_runtime import (
    SkillPermissionError,
    SkillRuntimeService,
)
from .services.skill_executors import SkillExecutorContext
from .services import workflows as workflow_runtime
from .services.skill_registry import (
    disable_skill,
    enable_skill,
    ensure_skill,
    list_project_skills,
)
from .services.slack_comms import (
    SlackCommsService,
    department_channel,
    render_persona_message,
)
from .services import meeting_consensus
from .services import factory_scale
from .services.factory_scale import FactoryScaleService
from .services.dashboard import DashboardService
from .services.finops_reconciler import FinopsReconcilerService
from .services.soc2 import SOC2EvidenceService
from .services.secret_refs import SecretRefNotFoundError, SecretRefService
from .services.self_improvement import (
    InvalidSelfImprovementTransitionError,
    SelfImprovementProposalNotFoundError,
    SelfImprovementService,
)
from .services.software_factory import SoftwareFactoryService
from .services.github_adapter import IntegrationConfigurationError
from .services.ci_config_generator import CIConfigGenerator
from .services.deployment_rollout_engine import DeploymentRolloutEngine
from .services.promotion_orchestrator import PromotionOrchestrator
from .services.specpack import (
    AgentModuleEntitlementError,
    AgentModuleNotFoundError,
    AgentModuleRollbackError,
    PackNotFoundError,
    RoleNotFoundError,
    SpecPackService,
)

settings = get_settings()
engine = get_engine(settings)
LOGGER = logging.getLogger("boss.api")
budget = BudgetGovernor(settings)
intake = CommandIntakeService(settings)
orchestration_agent = AgentService(settings)
jobs = JobLeaseService(settings)
docs_updates = DocsUpdateService(settings)
secret_refs = SecretRefService()
receipts = ReceiptService()
webhooks = WebhookService(settings)
inbound_webhooks = InboundWebhookService(settings)
plane_webhooks = PlaneWebhookService(settings)
github_community = GitHubCommunityService(settings)
software_factory = SoftwareFactoryService(settings)
decisions = DecisionService(settings)
specpack = SpecPackService(settings)
agent_runtime = AgentRuntimeService(settings)
entitlements = EntitlementBrokerService(settings)
capability_bundles = CapabilityBundleService(settings)
commercial = CommercialService(settings)
enterprise = EnterpriseService(settings)
audit = AuditTrailService(settings)
comms = CommsService(settings)
infrastructure = InfrastructureService(settings)
reliability = ReliabilityService(settings)
self_improvement = SelfImprovementService(settings)
health_snapshots = HealthSnapshotService(settings)
metatron = MetatronService(settings)
role_governance = RoleGovernanceService(settings)
autonomy_engine = AutonomyEngine(settings)
router_health = RouterHealthService(settings)
skill_runtime = SkillRuntimeService(settings)
slack_comms = SlackCommsService(settings)
factory_scale_service = FactoryScaleService(settings)
dashboard_service = DashboardService(settings)
finops_reconciler = FinopsReconcilerService(settings)
soc2_evidence = SOC2EvidenceService(settings)
members = MemberDirectoryService(settings)
canonical = CanonicalObjectService()
deadletter_remediation = DeadletterRemediationService(settings)
ops_queue = OpsQueueService(settings)
reports = ReportService(settings)
break_glass = BreakGlassService()
resource_plane = ResourcePlaneService(settings)
deployment_rollout_engine = DeploymentRolloutEngine(settings)
promotion_orchestrator = PromotionOrchestrator(
    settings,
    resource_plane=resource_plane,
    rollout_engine=deployment_rollout_engine,
    pipeline=software_factory.pipeline,
)
ci_config_generator = CIConfigGenerator()
oidc_auth = OidcAuthenticator(settings)
saml_sessions = SamlSessionService(settings)
current_api_principal: ContextVar[Optional[str]] = ContextVar(
    "current_api_principal",
    default=None,
)
current_transport_authenticated: ContextVar[bool] = ContextVar(
    "current_transport_authenticated",
    default=False,
)
current_auth_method: ContextVar[Optional[str]] = ContextVar(
    "current_auth_method",
    default=None,
)


# --- Simple in-memory rate limiter for P2 high-cost paths (per .ai/security-review.md) ---
# Protects e.g. /v1/metatron/query (Metatron), /v1/commands/propose (intake), provider
# sync /v1/resources/sync (and similar costly like account-connect/marketplace syncs).
# Implementation: pure stdlib (time + collections.deque), no external deps per task reqs.
# Key strategy: per-IP+project+actor (allows per-actor or IP scoping).
# Limit: 10 requests per 60s sliding window (configurable via BOSS_RATE_LIMIT_RPM / _ENABLED).
# On exceed: raises 429 immediately + emits "rate_limit.exceeded" event (visible in audit/feed,
# using same pattern as policy.denied / budget.*denied for "every failure visible").
# NOTE per task + AGENTS: this is local/test sufficient only. Prod deployments MUST front
# with Redis-backed limiter, rate limit at LB/CDN, or equivalent (in-mem is per-process,
# lost on restart, not shared across replicas). Set BOSS_RATE_LIMIT_ENABLED=false to bypass
# if using external. No budget spend here (rate is separate from budget governor).
# Follows: small vertical, typed, deterministic, events for effects, no direct external.
class InMemoryRateLimiter:
    def __init__(self, rpm: int = 10):
        self.rpm = max(1, int(rpm))
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        dq = self._hits[key]
        cutoff = now - 60.0
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= self.rpm:
            return False
        dq.append(now)
        return True

    def clear(self) -> None:
        self._hits.clear()


# Global configured at import (after settings). Tests can reconfig via helpers.
_rate_limiter = InMemoryRateLimiter(
    rpm=(
        settings.rate_limit_requests_per_minute
        if getattr(settings, "rate_limit_enabled", True)
        else 1_000_000
    )
)


def configure_rate_limiter_for_testing(rpm: int) -> None:
    """Test-only helper: raise/lower RPM and clear state so normal test suite
    (which makes ~10+ propose/metatron calls) is not impacted by the 10/min
    production-ish limit. The dedicated rate limit test sets low RPM + asserts 429.
    """
    global _rate_limiter
    _rate_limiter.rpm = max(1, int(rpm))
    _rate_limiter.clear()


def reset_rate_limiter_hits() -> None:
    """Test helper to clear hit deque (fresh window)."""
    global _rate_limiter
    _rate_limiter.clear()


def record_rate_limit_denial(
    project_id: str, actor: str, reason: str, ip: Optional[str] = None
) -> None:
    """Record rate limit exceed as auditable event. Mirrors record_policy_denial
    and record_budget_denial_event exactly (open short conn, check project, emit,
    log on failure to write -- never silent).
    """
    try:
        with begin(engine) as audit_connection:
            project_exists = audit_connection.execute(
                text("SELECT 1 FROM projects WHERE id = :project_id"),
                {"project_id": project_id},
            ).first()
            if not project_exists:
                return
            emit_event(
                audit_connection,
                project_id,
                "rate_limit.exceeded",
                actor,
                "project",
                project_id,
                {
                    "reason": reason,
                    "ip": ip or "unknown",
                },
            )
    except Exception as audit_error:
        LOGGER.exception(
            "rate_limit_denial_audit_write_failed project_id=%s actor=%s reason=%s ip=%s",
            project_id,
            actor,
            reason,
            ip,
            exc_info=audit_error,
        )
        return


def _enforce_high_cost_rate_limit(
    request: Request, project_id: str, actor: str, endpoint: str = "high_cost"
) -> None:
    """Enforce for the listed P2 paths. Call early in handler (before DB work or
    require_project_actor) so rate limit is first-class protection.
    """
    if not getattr(settings, "rate_limit_enabled", True):
        return
    client_ip = "unknown"
    if request is not None and getattr(request, "client", None) is not None:
        client_ip = getattr(request.client, "host", None) or "unknown"
    key = f"{client_ip}:{project_id}:{actor or 'anon'}"
    if not _rate_limiter.is_allowed(key):
        record_rate_limit_denial(
            project_id,
            actor or "anon",
            f"rate limit exceeded on {endpoint}",
            client_ip,
        )
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded (10 req/min for high-cost path). Retry later.",
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    # Self-migrate on startup: schema.sql is fully idempotent (CREATE/ALTER … IF NOT EXISTS), so
    # every deploy converges the managed Postgres to the shipped schema with no separate migration
    # step. Guarded so a transient DB blip at boot can't crash-loop the API.
    try:
        ensure_schema(engine, settings)
        print("[lifespan] ensure_schema applied (idempotent schema sync)")
    except Exception as exc:  # noqa: BLE001
        print(f"[lifespan] ensure_schema skipped — DB not ready or migration error: {exc}")
    # Preview lifecycle is independent from the API. Reconcile the durable rows against
    # live processes/containers/rootfs services; never blanket-kill healthy previews.
    try:
        from .services.preview_service import reconcile_previews
        result = reconcile_previews(engine, settings)
        print(f"[lifespan] preview reconciliation: {result}")
    except Exception as exc:  # noqa: BLE001
        print(f"[lifespan] preview reconciliation skipped: {exc}")
    try:
        from .services.execution_substrate import effective_substrates
        print(f"[lifespan] execution substrates: {effective_substrates(settings)}")
    except ValueError as exc:
        raise RuntimeError(f"invalid execution-substrate configuration: {exc}") from exc
    yield


app = FastAPI(title="BOSS Control Plane", version="0.2.1", lifespan=lifespan)


def extract_api_token(request: Request) -> Optional[str]:
    header_value = request.headers.get("x-boss-api-key")
    if header_value and header_value.strip():
        return header_value.strip()

    auth_value = request.headers.get("authorization")
    if not auth_value:
        return None
    scheme, _, token = auth_value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def extract_bearer_token(request: Request) -> Optional[str]:
    auth_value = request.headers.get("authorization")
    if not auth_value:
        return None
    scheme, _, token = auth_value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def api_token_allowed(token: str, allowed_tokens: tuple[str, ...]) -> bool:
    return any(compare_digest(token, allowed) for allowed in allowed_tokens)


@app.middleware("http")
async def require_transport_auth_for_v1(request: Request, call_next):
    auth_required = (
        settings.require_api_auth
        or settings.oidc_enabled
        or settings.enterprise_saml_enabled
    )
    # The preview reverse-proxy (/v1/previews/<id>/web/...) is browser-facing and must work
    # without the API key — it's gated by the unguessable preview id + the preview TTL.
    _parts = request.url.path.split("/")
    is_preview_web = (
        len(_parts) >= 5 and _parts[1] == "v1" and _parts[2] == "previews"
        and _parts[4] in {"web", "feedback", "logs"}
    )
    is_inbound_webhook = (
        len(_parts) >= 5
        and _parts[1] == "v1"
        and _parts[2] == "webhooks"
        and _parts[3] == "inbound"
    )
    is_github_webhook = request.url.path == "/v1/webhooks/github"
    # Plane's issue webhook is a machine callback that verifies its own X-Plane-Signature HMAC,
    # so it is exempt from transport auth exactly like the GitHub webhook.
    is_plane_webhook = request.url.path == "/v1/webhooks/plane"
    # Alerting tools sign reliability alert deliveries (X-Reliability-Signature HMAC) — same model.
    is_reliability_alert = request.url.path == "/v1/reliability/alert"
    if (
        not request.url.path.startswith("/v1/")
        or not auth_required
        or is_preview_web
        or is_inbound_webhook
        or is_github_webhook
        or is_plane_webhook
        or is_reliability_alert
    ):
        principal_token = current_api_principal.set(None)
        authenticated_token = current_transport_authenticated.set(False)
        auth_method_token = current_auth_method.set(None)
        try:
            return await call_next(request)
        finally:
            current_api_principal.reset(principal_token)
            current_transport_authenticated.reset(authenticated_token)
            current_auth_method.reset(auth_method_token)

    allowed_tokens = settings.api_key_values
    if (
        settings.require_api_auth
        and not allowed_tokens
        and not settings.oidc_enabled
        and not settings.enterprise_saml_enabled
    ):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "API authentication is required but no BOSS_API_KEYS are configured."
            },
        )

    token = extract_api_token(request)
    principal: Optional[str] = None
    auth_method: Optional[str] = None
    if token and allowed_tokens and api_token_allowed(token, allowed_tokens):
        principal_map = settings.api_key_principal_map
        principal = principal_map.get(token)
        if principal_map and not principal:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        "API key is valid for transport authentication but is not "
                        "bound to a project member principal."
                    )
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        auth_method = "api_key"
    elif settings.oidc_enabled:
        bearer_token = extract_bearer_token(request)
        if bearer_token:
            try:
                principal = oidc_auth.verify_bearer_token(bearer_token).member_id
            except OidcConfigurationError as error:
                return JSONResponse(status_code=503, content={"detail": str(error)})
            except OidcAuthenticationError as error:
                return JSONResponse(
                    status_code=401,
                    content={"detail": str(error)},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            auth_method = "oidc"
    if not auth_method and settings.enterprise_saml_enabled:
        bearer_token = extract_bearer_token(request)
        if bearer_token:
            with begin(engine) as connection:
                session = saml_sessions.authenticate_token(connection, bearer_token)
            if session:
                principal = session["member_id"]
                auth_method = "saml"

    if not auth_method:
        return JSONResponse(
            status_code=401,
            content={"detail": "Valid BOSS API key or OIDC bearer token required."},
            headers={"WWW-Authenticate": "Bearer"},
        )

    principal_token = current_api_principal.set(principal)
    authenticated_token = current_transport_authenticated.set(True)
    auth_method_token = current_auth_method.set(auth_method)
    try:
        return await call_next(request)
    finally:
        current_api_principal.reset(principal_token)
        current_transport_authenticated.reset(authenticated_token)
        current_auth_method.reset(auth_method_token)


def project_summary_from_row(row) -> ProjectSummary:
    return ProjectSummary(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        workspace_id=row["workspace_id"],
        workspace_name=row["workspace_name"],
        org_id=row["org_id"],
        org_name=row["org_name"],
        created_at=row["created_at"].isoformat(),
    )


def project_member_summary_from_service(payload) -> ProjectMemberSummary:
    return ProjectMemberSummary(
        id=payload["id"],
        project_id=payload["project_id"],
        member_id=payload["member_id"],
        display_name=payload["display_name"],
        member_type=payload["member_type"],
        role_keys=payload.get("role_keys") or [],
        permissions=payload.get("permissions") or [],
        status=payload["status"],
        created_by=payload["created_by"],
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
    )


def org_member_summary_from_service(payload) -> OrgMemberSummary:
    return OrgMemberSummary(
        id=payload["id"],
        org_id=payload["org_id"],
        project_id=payload.get("project_id"),
        member_id=payload["member_id"],
        display_name=payload["display_name"],
        member_type=payload["member_type"],
        role_keys=payload.get("role_keys") or [],
        permissions=payload.get("permissions") or [],
        status=payload["status"],
        created_by=payload["created_by"],
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
    )


def break_glass_grant_summary_from_service(payload) -> BreakGlassGrantSummary:
    return BreakGlassGrantSummary(
        id=payload["id"],
        project_id=payload["project_id"],
        target_member_id=payload["target_member_id"],
        requested_by=payload["requested_by"],
        reason=payload["reason"],
        scope=payload.get("scope") or {},
        granted_permissions=payload.get("granted_permissions") or [],
        status=payload["status"],
        expires_at=payload["expires_at"],
        postmortem_task_id=payload.get("postmortem_task_id"),
        revoked_by=payload.get("revoked_by"),
        revoked_reason=payload.get("revoked_reason"),
        revoked_at=payload.get("revoked_at"),
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
    )


def require_authenticated_actor_binding(
    project_id: str,
    actor: str,
    permission: str,
    connection=None,
) -> None:
    principal = current_api_principal.get()
    if not principal or principal == actor:
        return
    # Bounded delegation: allow the authenticated principal to act as a member whose
    # role_keys explicitly include the principal — e.g. the console's system actor
    # 'executive_console' carries role_keys ['ceo', 'ceo_delegate'], so a 'ceo' key
    # may act as it. This is NOT arbitrary impersonation: the target actor must carry
    # the principal as one of its roles. Without a connection to check roles, fall
    # back to the strict principal == actor rule.
    if connection is not None:
        try:
            member = members.get_member(connection, project_id, actor)
            role_keys = (member or {}).get("role_keys") or []
        except Exception:  # noqa: BLE001 — any lookup failure ⇒ no delegation
            role_keys = []
        if principal in role_keys:
            return
    reason = (
        f"Authenticated principal '{principal}' is not allowed to act as "
        f"'{actor}'."
    )
    record_policy_denial(project_id, principal, permission, reason)
    raise HTTPException(status_code=403, detail=reason)


def require_project_actor(connection, project_id: str, actor: str, permission: str) -> None:
    try:
        members.require_member(
            connection,
            project_id,
            actor,
            permission=permission,
        )
    except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
        # Self-heal: a project seeded before a default member/permission existed can
        # leave a baseline system actor (e.g. executive_console) missing an intended
        # permission. Re-seed the defaults once and retry before denying — this grants
        # only what the defaults specify, so a genuinely-unauthorized actor is still
        # refused.
        if actor in DEFAULT_MEMBER_IDS:
            try:
                members.seed_default_members(connection, project_id, actor="bootstrap")
                members.require_member(
                    connection, project_id, actor, permission=permission
                )
            except (ProjectMemberNotFoundError, ProjectMemberPermissionError):
                record_policy_denial(project_id, actor, permission, str(error))
                raise HTTPException(status_code=403, detail=str(error)) from error
            require_authenticated_actor_binding(project_id, actor, permission, connection)
            return
        record_policy_denial(project_id, actor, permission, str(error))
        raise HTTPException(status_code=403, detail=str(error)) from error
    require_authenticated_actor_binding(project_id, actor, permission, connection)


def require_decision_actor_binding(
    connection,
    decision_id: str,
    actor: str,
) -> None:
    row = connection.execute(
        text("SELECT project_id FROM decisions WHERE id = :decision_id"),
        {"decision_id": decision_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Decision not found")
    require_authenticated_actor_binding(row["project_id"], actor, "decision_resolve")


def raise_project_actor_error(error: Exception) -> None:
    raise HTTPException(status_code=403, detail=str(error)) from error


def raise_decision_actor_error(
    decision_id: str, actor: str, error: Exception
) -> None:
    try:
        with begin(engine) as audit_connection:
            row = audit_connection.execute(
                text("SELECT project_id FROM decisions WHERE id = :decision_id"),
                {"decision_id": decision_id},
            ).mappings().first()
            if row:
                emit_event(
                    audit_connection,
                    row["project_id"],
                    "policy.denied",
                    actor,
                    "decision",
                    decision_id,
                    {
                        "permission": "decision_resolve",
                        "reason": str(error),
                    },
                )
    except Exception as audit_error:
        LOGGER.exception(
            "policy_denial_audit_write_failed decision_id=%s actor=%s reason=%s",
            decision_id,
            actor,
            error,
            exc_info=audit_error,
        )
    raise HTTPException(status_code=403, detail=str(error)) from error


def record_policy_denial(
    project_id: str, actor: str, permission: str, reason: str
) -> None:
    try:
        with begin(engine) as audit_connection:
            project_exists = audit_connection.execute(
                text("SELECT 1 FROM projects WHERE id = :project_id"),
                {"project_id": project_id},
            ).first()
            if not project_exists:
                return
            emit_event(
                audit_connection,
                project_id,
                "policy.denied",
                actor,
                "project",
                project_id,
                {
                    "permission": permission,
                    "reason": reason,
                },
            )
    except Exception as audit_error:
        LOGGER.exception(
            "policy_denial_audit_write_failed project_id=%s actor=%s permission=%s reason=%s",
            project_id,
            actor,
            permission,
            reason,
            exc_info=audit_error,
        )
        return


def record_account_connect_sync_denial(project_id: str, actor: str, reason: str) -> None:
    try:
        with begin(engine) as audit_connection:
            project_exists = audit_connection.execute(
                text("SELECT 1 FROM projects WHERE id = :project_id"),
                {"project_id": project_id},
            ).first()
            if not project_exists:
                return
            emit_event(
                audit_connection,
                project_id,
                "account_connect.sync_denied",
                actor,
                "account_connect_org",
                project_id,
                {"reason": reason},
            )
    except Exception as audit_error:
        LOGGER.exception(
            "account_connect_sync_denial_audit_write_failed project_id=%s actor=%s reason=%s",
            project_id,
            actor,
            reason,
            exc_info=audit_error,
        )
        return


def record_account_connect_denial(
    project_id: str,
    actor: str,
    event_type: str,
    reason: str,
    entity_type: str,
) -> None:
    try:
        with begin(engine) as audit_connection:
            project_exists = audit_connection.execute(
                text("SELECT 1 FROM projects WHERE id = :project_id"),
                {"project_id": project_id},
            ).first()
            if not project_exists:
                return
            emit_event(
                audit_connection,
                project_id,
                event_type,
                actor,
                entity_type,
                project_id,
                {"reason": reason},
            )
    except Exception as audit_error:
        LOGGER.exception(
            "account_connect_denial_audit_write_failed project_id=%s actor=%s event_type=%s reason=%s",
            project_id,
            actor,
            event_type,
            reason,
            exc_info=audit_error,
        )
        return


def record_budget_denial_event(
    project_id: str,
    actor: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    data: dict,
) -> None:
    try:
        with begin(engine) as audit_connection:
            project_exists = audit_connection.execute(
                text("SELECT 1 FROM projects WHERE id = :project_id"),
                {"project_id": project_id},
            ).first()
            if not project_exists:
                return
            emit_event(
                audit_connection,
                project_id,
                event_type,
                actor,
                entity_type,
                entity_id,
                data,
            )
    except Exception as audit_error:
        LOGGER.exception(
            "budget_denial_audit_write_failed project_id=%s actor=%s event_type=%s entity_type=%s entity_id=%s",
            project_id,
            actor,
            event_type,
            entity_type,
            entity_id,
            exc_info=audit_error,
        )
        return


def project_id_for_changeset(connection, changeset_id: str) -> str:
    row = connection.execute(
        text("SELECT project_id FROM changesets WHERE id = :changeset_id"),
        {"changeset_id": changeset_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="ChangeSet not found.")
    return row["project_id"]


def project_id_for_deployment_run(connection, deployment_run_id: str) -> str:
    row = connection.execute(
        text("SELECT project_id FROM deployment_runs WHERE id = :deployment_run_id"),
        {"deployment_run_id": deployment_run_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Deployment run not found.")
    return row["project_id"]


def task_summary_from_row(row) -> TaskSummary:
    return TaskSummary(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        owner_role_id=row.get("owner_role_id"),
        parent_task_id=row.get("parent_task_id"),
        dependency_task_ids=row.get("dependency_task_ids") or [],
        due_at=row["due_at"].isoformat() if row.get("due_at") else None,
        priority=row["priority"],
        risk_tier=row["risk_tier"],
        data_boundary=row.get("data_boundary") or "internal",
        dedupe_key=row["dedupe_key"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def job_list_item_from_row(row: dict[str, Any]) -> JobListItem:
    return JobListItem(**row)


def secret_ref_summary_from_service(payload: dict[str, Any]) -> SecretRefSummary:
    return SecretRefSummary(**payload)


def job_summary_from_row(row) -> JobSummary:
    return JobSummary(
        id=row["id"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        kind=row["kind"],
        status=row["status"],
        data_boundary=row.get("data_boundary") or "internal",
        priority=row["priority"],
        max_attempts=row["max_attempts"],
        attempt=row["attempt"],
        run_after=row["run_after"].isoformat(),
    )


def decision_card_from_row(row) -> DecisionCard:
    return DecisionCard(
        id=row["id"],
        task_id=row.get("task_id"),
        question=row["question"],
        status=row["status"],
        risk_tier=row["risk_tier"],
        default_action=row["default_action"],
        recommendation=row["recommendation"] or {},
        options=row["options"] or [],
        cost_summary=row["cost_summary"] or {},
        deadline_at=row["deadline_at"].isoformat() if row["deadline_at"] else None,
        approved_by=row["approved_by"],
        approved_at=row["approved_at"].isoformat() if row["approved_at"] else None,
        delegated_to=row.get("delegated_to"),
        delegated_by=row.get("delegated_by"),
        delegated_at=row["delegated_at"].isoformat() if row.get("delegated_at") else None,
        revision_requested_by=row.get("revision_requested_by"),
        revision_requested_at=(
            row["revision_requested_at"].isoformat()
            if row.get("revision_requested_at")
            else None
        ),
        executed_by=row.get("executed_by"),
        executed_at=row["executed_at"].isoformat() if row.get("executed_at") else None,
        resolution_note=row.get("resolution_note"),
        external_ref=row.get("external_ref"),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def self_improvement_summary_from_row(row) -> SelfImprovementProposalSummary:
    return SelfImprovementProposalSummary(
        id=row["id"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        title=row["title"],
        summary=row["summary"],
        change_ref=row["change_ref"],
        diff_summary=row["diff_summary"],
        status=row["status"],
        risk_tier=row["risk_tier"],
        expected_impact=row.get("expected_impact") or [],
        test_plan=row.get("test_plan") or [],
        canary_scope=row.get("canary_scope") or [],
        rollback_plan=row.get("rollback_plan") or [],
        success_metrics=row.get("success_metrics") or [],
        status_note=row.get("status_note"),
        proposal_artifact_id=row.get("proposal_artifact_id"),
        current_decision_id=row.get("current_decision_id"),
        canary_artifact_id=row.get("canary_artifact_id"),
        rollout_artifact_id=row.get("rollout_artifact_id"),
        rollback_artifact_id=row.get("rollback_artifact_id"),
        rollout_state=row.get("rollout_state") or {},
        created_by=row["created_by"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def self_improvement_detail_from_row(row) -> SelfImprovementProposalDetail:
    return SelfImprovementProposalDetail(**self_improvement_summary_from_row(row).model_dump())


def health_snapshot_summary_from_row(row) -> HealthSnapshotSummary:
    return HealthSnapshotSummary(
        id=row["id"],
        project_id=row["project_id"],
        generated_by=row["generated_by"],
        budget_state=row["budget_state"],
        queue_state=row.get("queue_state") or {},
        workload_state=row.get("workload_state") or {},
        incident_state=row.get("incident_state") or {},
        top_risks=row.get("top_risks") or [],
        summary=row.get("summary") or {},
        created_at=row["created_at"].isoformat(),
    )


def artifact_summary_from_row(row) -> ArtifactSummary:
    return ArtifactSummary(
        id=row["id"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        artifact_type=row["artifact_type"],
        title=row["title"],
        uri=row["uri"],
        content_hash=row["content_hash"],
        created_by_job_id=row["created_by_job_id"],
        data_boundary=row.get("data_boundary") or "internal",
        created_at=row["created_at"].isoformat(),
    )


def artifact_detail_from_row(row) -> ArtifactDetail:
    return ArtifactDetail(
        id=row["id"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        artifact_type=row["artifact_type"],
        title=row["title"],
        uri=row["uri"],
        content_hash=row["content_hash"],
        created_by_job_id=row["created_by_job_id"],
        data_boundary=row.get("data_boundary") or "internal",
        created_at=row["created_at"].isoformat(),
        content=row["content"] or {},
    )


def deadletter_item_from_row(row) -> DeadletterItem:
    created_at = row["created_at"]
    updated_at = row.get("updated_at")
    resolved_at = row.get("resolved_at")
    return DeadletterItem(
        id=row["id"],
        project_id=row["project_id"],
        source_kind=row["source_kind"],
        source_id=row["source_id"],
        related_entity_type=row.get("related_entity_type"),
        related_entity_id=row.get("related_entity_id"),
        related_status=row.get("related_status"),
        reason=row["reason"],
        payload=row["payload"] or {},
        status=row.get("status") or "open",
        incident_task_id=row.get("incident_task_id"),
        incident_artifact_id=row.get("incident_artifact_id"),
        remediation_decision_id=row.get("remediation_decision_id"),
        requeued_job_id=row.get("requeued_job_id"),
        resolved_by=row.get("resolved_by"),
        resolved_at=resolved_at.isoformat()
        if hasattr(resolved_at, "isoformat")
        else resolved_at,
        created_at=created_at.isoformat()
        if hasattr(created_at, "isoformat")
        else str(created_at),
        updated_at=updated_at.isoformat()
        if hasattr(updated_at, "isoformat")
        else updated_at,
    )


def event_item_from_row(row) -> EventItem:
    return EventItem(
        id=row["id"],
        event_type=row["event_type"],
        actor=row["actor"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        data=row["data"] or {},
        created_at=row["created_at"].isoformat(),
    )


def outbox_message_summary_from_row(row) -> OutboxMessageSummary:
    return OutboxMessageSummary(
        id=row["id"],
        project_id=row["project_id"],
        channel=row["channel"],
        subject=row.get("subject"),
        body=row["body"],
        status=row["status"],
        dedupe_key=row["dedupe_key"],
        data_boundary=row.get("data_boundary") or "internal",
        receipt_id=row.get("receipt_id"),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def comms_channel_policy_summary_from_row(row) -> CommsChannelPolicySummary:
    return CommsChannelPolicySummary(
        id=row["id"],
        project_id=row["project_id"],
        provider=row["provider"],
        delivery_mode=row["delivery_mode"],
        allowed_data_boundaries=row["allowed_data_boundaries"] or [],
        rollout_notes=row.get("rollout_notes"),
        status=row["status"],
        created_by=row["created_by"],
        metadata=row["metadata"] or {},
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def inbox_message_summary_from_row(row) -> InboxMessageSummary:
    return InboxMessageSummary(
        id=row["id"],
        project_id=row.get("project_id"),
        channel=row["channel"],
        raw_ref=row.get("raw_ref"),
        subject=row.get("subject"),
        body=row["body"],
        status=row["status"],
        data_boundary=row.get("data_boundary") or "internal",
        parsed=row.get("parsed") or {},
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def reload_inbox_message(connection, message_id: str) -> dict:
    row = connection.execute(
        text(
            """
            SELECT id, project_id, channel, raw_ref, subject, body, status, parsed,
                   data_boundary, created_at, updated_at
            FROM inbox_messages
            WHERE id = :message_id
            """
        ),
        {"message_id": message_id},
    ).mappings().one()
    return dict(row)


def infra_resource_summary_from_row(row) -> InfraResourceSummary:
    return InfraResourceSummary(
        id=row["id"],
        project_id=row["project_id"],
        task_id=row.get("task_id"),
        decision_id=row.get("decision_id"),
        provider=row["provider"],
        resource_type=row["resource_type"],
        resource_name=row["resource_name"],
        external_resource_id=row["external_resource_id"],
        region=row["region"],
        size=row["size"],
        image=row["image"],
        tags=row.get("tags") or [],
        status=row["status"],
        ttl_minutes=int(row["ttl_minutes"]),
        expires_at=row["expires_at"].isoformat(),
        terminated_at=(
            row["terminated_at"].isoformat() if row.get("terminated_at") else None
        ),
        termination_reason=row.get("termination_reason"),
        provision_receipt_id=row.get("provision_receipt_id"),
        provision_artifact_id=row.get("provision_artifact_id"),
        termination_receipt_id=row.get("termination_receipt_id"),
        termination_artifact_id=row.get("termination_artifact_id"),
        data_boundary=row.get("data_boundary") or "internal",
        metadata=row.get("metadata") or {},
        created_by_job_id=row.get("created_by_job_id"),
        updated_by_job_id=row.get("updated_by_job_id"),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def receipt_response_from_row(row) -> ReceiptResponse:
    return ReceiptResponse(
        id=row["id"],
        project_id=row["project_id"],
        dedupe_key=row["dedupe_key"],
        effect_kind=row["effect_kind"],
        effect_scope=row.get("effect_scope") or {},
        payload_hash=row.get("payload_hash"),
        status=row["status"],
        provider_ref=row.get("provider_ref"),
        created_by_job_id=row.get("created_by_job_id"),
        data_boundary=row.get("data_boundary") or "internal",
        result=row.get("result"),
        error=row.get("error"),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def budget_reservation_summary_from_row(row) -> BudgetReservationSummary:
    return BudgetReservationSummary(
        id=row["id"],
        project_id=row["project_id"],
        job_id=row.get("job_id"),
        category=row["category"],
        amount_usd_micros=int(row["amount_usd_micros"]),
        status=row["status"],
        reason=row.get("reason"),
        expires_at=row["expires_at"].isoformat(),
        created_at=row["created_at"].isoformat(),
    )


def budget_allocation_summary_from_service(payload) -> BudgetAllocationSummary:
    return BudgetAllocationSummary(
        id=payload["id"],
        parent_allocation_id=payload.get("parent_allocation_id"),
        project_id=payload["project_id"],
        scope_type=payload["scope_type"],
        scope_id=payload["scope_id"],
        amount_usd_micros=int(payload["amount_usd_micros"]),
        child_allocated_usd_micros=int(payload["child_allocated_usd_micros"]),
        reserved_usd_micros=int(payload["reserved_usd_micros"]),
        actual_usd_micros=int(payload["actual_usd_micros"]),
        available_usd_micros=int(payload["available_usd_micros"]),
        period_start=payload["period_start"],
        period_end=payload["period_end"],
        status=payload["status"],
        created_at=payload["created_at"],
    )


def cost_receipt_summary_from_row(row) -> CostReceiptSummary:
    return CostReceiptSummary(
        id=row["id"],
        project_id=row["project_id"],
        reservation_id=row.get("reservation_id"),
        receipt_id=row.get("receipt_id"),
        category=row["category"],
        skill_or_model_id=row["skill_or_model_id"],
        estimated_usd_micros=int(row["estimated_usd_micros"]),
        actual_usd_micros=(
            int(row["actual_usd_micros"])
            if row.get("actual_usd_micros") is not None
            else None
        ),
        status=row["status"],
        created_at=row["created_at"].isoformat(),
    )


def role_summary_from_service(payload) -> RoleSummary:
    return RoleSummary(
        id=payload["id"],
        project_id=payload["project_id"],
        role_key=payload["role_key"],
        display_name=payload["display_name"],
        autonomy_level=payload["autonomy_level"],
        source_module_id=payload.get("source_module_id"),
        source_module_version=payload.get("source_module_version"),
        trust_score=payload.get("trust_score", 50),
        successful_runs=payload.get("successful_runs", 0),
        policy_violation_count=payload.get("policy_violation_count", 0),
        duplicate_effect_attempt_count=payload.get(
            "duplicate_effect_attempt_count", 0
        ),
        hidden_failure_count=payload.get("hidden_failure_count", 0),
        low_quality_output_count=payload.get("low_quality_output_count", 0),
        scope_violation_count=payload.get("scope_violation_count", 0),
        last_reviewed_at=payload.get("last_reviewed_at"),
        last_violation_at=payload.get("last_violation_at"),
        created_at=payload["created_at"],
    )


def role_detail_from_service(payload) -> RoleDetail:
    return RoleDetail(
        id=payload["id"],
        project_id=payload["project_id"],
        role_key=payload["role_key"],
        display_name=payload["display_name"],
        autonomy_level=payload["autonomy_level"],
        source_module_id=payload.get("source_module_id"),
        source_module_version=payload.get("source_module_version"),
        trust_score=payload.get("trust_score", 50),
        successful_runs=payload.get("successful_runs", 0),
        policy_violation_count=payload.get("policy_violation_count", 0),
        duplicate_effect_attempt_count=payload.get(
            "duplicate_effect_attempt_count", 0
        ),
        hidden_failure_count=payload.get("hidden_failure_count", 0),
        low_quality_output_count=payload.get("low_quality_output_count", 0),
        scope_violation_count=payload.get("scope_violation_count", 0),
        last_reviewed_at=payload.get("last_reviewed_at"),
        last_violation_at=payload.get("last_violation_at"),
        created_at=payload["created_at"],
        spec=payload["spec"] or {},
    )


def role_governance_detail_from_service(payload) -> RoleGovernanceDetail:
    return RoleGovernanceDetail(
        project_id=payload["project_id"],
        role=role_detail_from_service(payload["role"]),
        policy=payload.get("policy") or {},
        pending_autonomy_change=payload.get("pending_autonomy_change"),
        recent_reviews=payload.get("recent_reviews") or [],
        recent_autonomy_changes=payload.get("recent_autonomy_changes") or [],
    )


def pack_summary_from_service(payload) -> PackSummary:
    return PackSummary(
        pack_id=payload["pack_id"],
        version=payload["version"],
        description=payload["description"],
        status=payload["status"],
        requires=payload["requires"] or [],
        roles=payload["roles"] or [],
        workflows=payload["workflows"] or [],
        policies=payload["policies"] or [],
        skills=payload["skills"] or [],
        installed=bool(payload["installed"]),
        installed_by=payload.get("installed_by"),
        installed_at=payload.get("installed_at"),
        updated_at=payload.get("updated_at"),
        installed_version=payload.get("installed_version"),
    )


def pack_detail_from_service(payload) -> PackDetail:
    return PackDetail(
        **pack_summary_from_service(payload).model_dump(),
        role_count=payload.get("role_count"),
        role_results=payload.get("role_results") or [],
        install_status=payload.get("install_status"),
    )


def agent_module_summary_from_service(payload) -> AgentModuleSummary:
    return AgentModuleSummary(
        module_id=payload["module_id"],
        version=payload["version"],
        display_name=payload["display_name"],
        description=payload["description"],
        publisher=payload["publisher"],
        category=payload["category"],
        status=payload["status"],
        role_key=payload.get("role_key"),
        job_kinds=payload.get("job_kinds") or [],
        default_skill_ids=payload.get("default_skill_ids") or [],
        tags=payload.get("tags") or [],
        price_model=payload.get("price_model"),
        price_usd_micros=payload.get("price_usd_micros"),
        required_entitlements=payload.get("required_entitlements") or [],
        entitlement_status=payload.get("entitlement_status") or "not_required",
        config_schema=payload.get("config_schema") or {},
        installed=bool(payload["installed"]),
        installed_by=payload.get("installed_by"),
        installed_at=payload.get("installed_at"),
        updated_at=payload.get("updated_at"),
        installed_version=payload.get("installed_version"),
        config=payload.get("config") or {},
    )


def agent_module_detail_from_service(payload) -> AgentModuleDetail:
    return AgentModuleDetail(
        **agent_module_summary_from_service(payload).model_dump(),
        prompt_bundle=payload.get("prompt_bundle") or {},
        runtime=payload.get("runtime") or {},
        marketplace=payload.get("marketplace") or {},
        role=payload.get("role") or {},
        role_result=payload.get("role_result") or {},
        entitlement_result=payload.get("entitlement_result") or {},
        install_status=payload.get("install_status"),
    )


def agent_module_install_record_summary_from_service(
    payload,
) -> AgentModuleInstallRecordSummary:
    return AgentModuleInstallRecordSummary(**payload)


def skill_summary_from_service(payload) -> SkillSummary:
    return SkillSummary(
        id=payload["id"],
        kind=payload["kind"],
        system=payload.get("system", False),
        load=payload.get("load", "eager"),
        source=payload.get("source", "catalog"),
        enabled=bool(payload.get("enabled", False)),
        on_demand=bool(
            payload.get("on_demand", payload.get("load") == "on_demand")
        ),
        risk_tier=payload["risk_tier"],
        description=payload["description"],
        mock_mode=payload["mock_mode"],
        input_schema=payload.get("input_schema") or {},
        output_schema=payload.get("output_schema") or {},
        cost=payload.get("cost") or {},
        retry=payload.get("retry") or {},
        permissions=payload.get("permissions") or {},
        data_boundary=payload.get("data_boundary", "internal"),
        policy=payload.get("policy") or {},
        publisher=payload.get("publisher") or {},
        provenance=payload.get("provenance") or {},
    )


def entitlement_source_summary_from_service(
    payload,
) -> EntitlementSourceSummary:
    return EntitlementSourceSummary(
        id=payload["id"],
        project_id=payload["project_id"],
        provider=payload["provider"],
        source_id=payload["source_id"],
        source_kind=payload["source_kind"],
        entitlement_kind=payload["entitlement_kind"],
        owner_type=payload["owner_type"],
        owner_id=payload["owner_id"],
        workload_classes_allowed=payload.get("workload_classes_allowed") or [],
        multi_tenant_allowed=bool(payload["multi_tenant_allowed"]),
        credential_ref=payload.get("credential_ref"),
        cost_mode=payload["cost_mode"],
        limit_shape=payload.get("limit_shape") or {},
        supported_models=payload.get("supported_models") or [],
        task_strengths=payload.get("task_strengths") or [],
        risk_tier_max=payload["risk_tier_max"],
        status=payload["status"],
        effective_status=payload["effective_status"],
        auth_method=payload.get("auth_method", "api_key"),
        connection_status=payload.get("connection_status", "not_connected"),
        connection_ref=payload.get("connection_ref"),
        oauth_scopes=payload.get("oauth_scopes") or [],
        connection_metadata=payload.get("connection_metadata") or {},
        policy_notes=payload.get("policy_notes") or [],
        observed_capacity=payload.get("observed_capacity") or {},
        runtime_supported=bool(payload["runtime_supported"]),
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
    )


def entitlement_connection_session_summary_from_service(
    payload,
) -> EntitlementConnectionSessionSummary:
    return EntitlementConnectionSessionSummary(**payload)


def model_adapter_summary_from_service(payload) -> ModelAdapterSummary:
    return ModelAdapterSummary(**payload)


def entitlement_pool_summary_from_service(payload) -> EntitlementPoolSummary:
    return EntitlementPoolSummary(**payload)


def edition_status_from_service(payload) -> EditionStatusResponse:
    return EditionStatusResponse(**payload)


def license_entitlement_summary_from_service(payload) -> LicenseEntitlementSummary:
    return LicenseEntitlementSummary(**payload)


def account_connect_auth_session_from_service(
    payload,
) -> AccountConnectAuthSessionSummary:
    return AccountConnectAuthSessionSummary(**payload)


def account_connect_health_check_from_service(
    payload,
) -> AccountConnectHealthCheckSummary:
    return AccountConnectHealthCheckSummary(**payload)


def account_connect_team_invite_from_service(
    payload,
) -> AccountConnectTeamInviteSummary:
    return AccountConnectTeamInviteSummary(**payload)


def account_connect_billing_session_from_service(
    payload,
) -> AccountConnectBillingPortalSessionSummary:
    return AccountConnectBillingPortalSessionSummary(**payload)


def hosted_marketplace_listing_from_service(
    payload,
) -> HostedMarketplaceListingSummary:
    return HostedMarketplaceListingSummary(**payload)


def hosted_marketplace_sync_run_from_service(
    payload,
) -> HostedMarketplaceSyncRunSummary:
    return HostedMarketplaceSyncRunSummary(**payload)


def enterprise_cluster_summary_from_service(payload) -> EnterpriseClusterSummary:
    return EnterpriseClusterSummary(**payload)


def enterprise_cluster_validation_run_summary_from_service(
    payload,
) -> EnterpriseClusterValidationRunSummary:
    return EnterpriseClusterValidationRunSummary(**payload)


def enterprise_support_case_summary_from_service(
    payload,
) -> EnterpriseSupportCaseSummary:
    return EnterpriseSupportCaseSummary(**payload)


def enterprise_identity_provider_summary_from_service(
    payload,
) -> EnterpriseIdentityProviderSummary:
    return EnterpriseIdentityProviderSummary(**payload)


def enterprise_identity_rollout_policy_summary_from_service(
    payload,
) -> EnterpriseIdentityRolloutPolicySummary:
    return EnterpriseIdentityRolloutPolicySummary(**payload)


def enterprise_scim_group_summary_from_service(payload) -> EnterpriseScimGroupSummary:
    return EnterpriseScimGroupSummary(**payload)


def enterprise_scim_user_summary_from_service(payload) -> EnterpriseScimUserSummary:
    return EnterpriseScimUserSummary(**payload)


SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
BOSS_SCIM_USER_EXTENSION = "urn:boss:params:scim:schemas:extension:2.0:User"
BOSS_SCIM_GROUP_EXTENSION = "urn:boss:params:scim:schemas:extension:2.0:Group"


def scim_error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
            "status": str(status_code),
            "detail": detail,
        },
        headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
    )


def require_enterprise_scim_access(
    request: Request,
    connection,
    project_id: str,
) -> Optional[JSONResponse]:
    configured_tokens = settings.enterprise_scim_bearer_token_values
    if not configured_tokens:
        return scim_error(
            503,
            "BOSS_ENTERPRISE_SCIM_BEARER_TOKENS is required before SCIM runtime provisioning.",
        )
    token = extract_bearer_token(request)
    if not token or not api_token_allowed(token, configured_tokens):
        return scim_error(401, "Valid enterprise SCIM bearer token required.")
    runtime = enterprise.scim_runtime_status(connection, project_id)
    if not runtime["enabled"]:
        emit_event(
            connection,
            project_id,
            "enterprise.scim_runtime_blocked",
            "scim",
            "enterprise_identity_provider",
            project_id,
            {"reason": runtime["reason"]},
        )
        return scim_error(403, "No active SCIM identity provider is configured.")
    return None


def scim_extension(payload: dict[str, Any], schema: str) -> dict[str, Any]:
    extension = payload.get(schema) or payload.get("boss") or {}
    return extension if isinstance(extension, dict) else {}


def scim_token_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def scim_group_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        if isinstance(item, dict):
            raw = item.get("value") or item.get("display") or item.get("$ref")
            if raw:
                values.append(str(raw))
        elif str(item).strip():
            values.append(str(item))
    return values


def scim_display_name(payload: dict[str, Any]) -> str:
    display_name = payload.get("displayName")
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()
    name = payload.get("name")
    if isinstance(name, dict):
        formatted = name.get("formatted")
        if isinstance(formatted, str) and formatted.strip():
            return formatted.strip()
    user_name = payload.get("userName")
    return str(user_name).strip() if user_name else ""


def scim_group_payload_to_service(payload: dict[str, Any]) -> dict[str, Any]:
    extension = scim_extension(payload, BOSS_SCIM_GROUP_EXTENSION)
    return {
        "external_id": payload.get("externalId") or payload.get("id"),
        "display_name": str(payload.get("displayName") or "").strip(),
        "role_keys": scim_token_list(extension.get("roleKeys") or extension.get("role_keys")),
        "permissions": scim_token_list(extension.get("permissions")),
        "raw_group": payload,
        "status": "active" if payload.get("active", True) is not False else "inactive",
    }


def scim_user_payload_to_service(payload: dict[str, Any]) -> dict[str, Any]:
    extension = scim_extension(payload, BOSS_SCIM_USER_EXTENSION)
    return {
        "external_id": payload.get("externalId") or payload.get("id"),
        "user_name": str(payload.get("userName") or "").strip().lower(),
        "display_name": scim_display_name(payload),
        "active": bool(payload.get("active", True)),
        "role_keys": scim_token_list(extension.get("roleKeys") or extension.get("role_keys")),
        "permissions": scim_token_list(extension.get("permissions")),
        "groups": scim_group_values(payload.get("groups")),
        "raw_profile": payload,
    }


def scim_group_resource(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemas": [SCIM_GROUP_SCHEMA, BOSS_SCIM_GROUP_EXTENSION],
        "id": group["id"],
        "externalId": group.get("external_id"),
        "displayName": group["display_name"],
        "active": group["status"] == "active",
        "members": [],
        BOSS_SCIM_GROUP_EXTENSION: {
            "roleKeys": group.get("role_keys") or [],
            "permissions": group.get("permissions") or [],
        },
        "meta": {
            "resourceType": "Group",
            "created": group["created_at"],
            "lastModified": group["updated_at"],
        },
    }


def scim_user_resource(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemas": [SCIM_USER_SCHEMA, BOSS_SCIM_USER_EXTENSION],
        "id": user["id"],
        "externalId": user.get("external_id"),
        "userName": user["user_name"],
        "displayName": user["display_name"],
        "active": user["active"],
        "groups": [{"value": group, "display": group} for group in user.get("groups") or []],
        BOSS_SCIM_USER_EXTENSION: {
            "memberId": user["member_id"],
            "roleKeys": user.get("role_keys") or [],
            "permissions": user.get("permissions") or [],
        },
        "meta": {
            "resourceType": "User",
            "created": user["created_at"],
            "lastModified": user["updated_at"],
        },
    }


def scim_list_response(
    resources: list[dict[str, Any]],
    *,
    start_index: int = 1,
) -> dict[str, Any]:
    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": len(resources),
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def scim_filter_value(filter_expression: Optional[str], attribute: str) -> Optional[str]:
    if not filter_expression:
        return None
    match = re.fullmatch(
        rf"\s*{re.escape(attribute)}\s+eq\s+\"([^\"]+)\"\s*",
        filter_expression,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def capacity_receipt_summary_from_service(payload) -> CapacityReceiptSummary:
    return CapacityReceiptSummary(**payload)


def routing_log_item_from_service(payload) -> RoutingLogItem:
    return RoutingLogItem(
        id=payload["id"],
        project_id=payload["project_id"],
        skill_id=payload.get("skill_id"),
        task_kind=payload["task_kind"],
        workload_class=payload["workload_class"],
        risk_tier=payload["risk_tier"],
        data_boundary=payload.get("data_boundary") or "internal",
        budget_state=payload["budget_state"],
        selected_source_id=payload.get("selected_source_id"),
        selected_provider=payload.get("selected_provider"),
        selected_model_id=payload.get("selected_model_id"),
        denial_reason=payload.get("denial_reason"),
        rejected_sources=payload.get("rejected_sources") or [],
        decision=payload.get("decision") or {},
        created_at=payload["created_at"].isoformat()
        if hasattr(payload["created_at"], "isoformat")
        else payload["created_at"],
    )


def model_route_decision_summary_from_service(payload) -> ModelRouteDecisionSummary:
    return ModelRouteDecisionSummary(**payload)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        database_connected=database_is_reachable(engine),
        github_configured=bool(settings.github_token),
        openai_configured=bool(settings.openai_api_key),
    )


@app.get("/v1/auth/session", response_model=AuthSessionResponse)
def auth_session() -> AuthSessionResponse:
    principal = current_api_principal.get()
    auth_method = current_auth_method.get()
    binding_required = (
        bool(settings.api_key_principal_map)
        or settings.oidc_enabled
        or settings.enterprise_saml_enabled
    )
    transport_authenticated = current_transport_authenticated.get()
    auth_required = (
        settings.require_api_auth
        or settings.oidc_enabled
        or settings.enterprise_saml_enabled
    )
    if not auth_required:
        message = "API authentication is disabled for this runtime."
    elif settings.oidc_enabled and not settings.oidc_configured:
        message = "OIDC authentication is enabled but not fully configured."
    elif settings.enterprise_saml_enabled and auth_method == "saml":
        message = f"Authenticated as project member '{principal}' via saml."
    elif binding_required and principal:
        method_label = auth_method or "transport"
        message = f"Authenticated as project member '{principal}' via {method_label}."
    elif binding_required:
        message = "Transport principal binding is required but no principal was resolved."
    else:
        message = "Transport API key is valid; actor binding is not configured."
    return AuthSessionResponse(
        auth_required=auth_required,
        transport_authenticated=transport_authenticated,
        actor_binding_required=binding_required,
        principal_member_id=principal,
        auth_method=auth_method,
        oidc_enabled=settings.oidc_enabled,
        message=message,
    )


@app.get("/saml/v2/{project_id}/metadata", response_class=Response)
def saml_metadata(project_id: str, request: Request) -> Response:
    with begin(engine) as connection:
        metadata_xml = saml_sessions.metadata(
            connection,
            project_id,
            str(request.base_url).rstrip("/"),
        )
    return Response(content=metadata_xml, media_type="application/samlmetadata+xml")


async def saml_response_from_request(request: Request) -> str:
    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if content_type == "application/json":
        payload = await request.json()
        value = payload.get("SAMLResponse") if isinstance(payload, dict) else None
    else:
        body = (await request.body()).decode("utf-8")
        parsed = parse_qs(body)
        values = parsed.get("SAMLResponse") or parsed.get("saml_response")
        value = values[0] if values else None
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail="SAMLResponse is required.")
    return value.strip()


@app.post("/saml/v2/{project_id}/acs", response_model=SamlLoginResponse)
async def saml_acs(project_id: str, request: Request) -> SamlLoginResponse:
    if not settings.enterprise_saml_enabled:
        with begin(engine) as connection:
            emit_event(
                connection,
                project_id,
                "enterprise.saml_runtime_blocked",
                "saml",
                "enterprise_identity_provider",
                project_id,
                {"reason": "enterprise_saml_disabled"},
            )
        raise HTTPException(
            status_code=503,
            detail="BOSS_ENTERPRISE_SAML_ENABLED is required before SAML login.",
        )
    saml_response = await saml_response_from_request(request)
    saml_error: Optional[Exception] = None
    session = None
    with begin(engine) as connection:
        try:
            session = saml_sessions.create_session(
                connection,
                project_id,
                saml_response,
            )
        except SamlConfigurationError as error:
            saml_error = error
        except SamlAuthenticationError as error:
            saml_error = error
    if isinstance(saml_error, SamlConfigurationError):
        raise HTTPException(status_code=403, detail=str(saml_error)) from saml_error
    if isinstance(saml_error, SamlAuthenticationError):
        raise HTTPException(status_code=401, detail=str(saml_error)) from saml_error
    if session is None:
        raise HTTPException(status_code=500, detail="SAML session was not created.")
    return SamlLoginResponse(
        project_id=project_id,
        session_id=session.id,
        access_token=session.token,
        member_id=session.member_id,
        provider_id=session.provider_id,
        expires_at=session.expires_at,
    )


@app.get("/v1/edition", response_model=EditionStatusResponse)
def get_edition(project_id: str) -> EditionStatusResponse:
    with begin(engine) as connection:
        return edition_status_from_service(
            commercial.edition_status(connection, project_id)
        )


@app.get("/v1/diag/sandbox-roundtrip")
def diag_sandbox_roundtrip() -> dict:
    """Run a submit->poll round-trip to the sandbox-service FROM INSIDE this container,
    so we can tell (API, not SSH) whether the control plane can actually reach + drive
    the micro-VM service. Submits a throwaway echo task; polls up to ~40s."""
    import time as _time

    import httpx as _httpx

    base = (settings.sandbox_agent_url or "").rstrip("/")
    headers = {}
    if settings.sandbox_token:
        headers["X-Sandbox-Token"] = settings.sandbox_token
    out: dict = {"url": base, "token_set": bool(settings.sandbox_token),
                 "provider": settings.sandbox_provider}
    body = {
        "task_id": "diag-roundtrip", "tenant_id": "boss-dogfood",
        "repo": {"owner": settings.target_github_owner, "name": settings.target_github_repo,
                 "base_branch": "main", "work_branch": "boss/diag-roundtrip"},
        "git_token": settings.github_token or "",
        "instructions": "diag", "acceptance_criteria": [], "test_commands": [],
        "engine": "echo", "engine_env": {}, "run_mutation": False,
        "resources": {"vcpu": 1, "mem_mb": 1024, "disk_gb": 4, "timeout_s": 180},
    }
    t0 = _time.monotonic()
    try:
        with _httpx.Client(timeout=15.0) as client:
            r = client.post(f"{base}/v1/sandbox/tasks", json=body, headers=headers)
            out["submit_http"] = r.status_code
            out["submit_body"] = r.text[:200]
            r.raise_for_status()
            sid = r.json().get("sandbox_task_id")
            out["sid"] = sid
            last = None
            for _ in range(20):  # ~40s
                _time.sleep(2)
                pr = client.get(f"{base}/v1/sandbox/tasks/{sid}", headers=headers)
                pr.raise_for_status()
                last = pr.json()
                if last.get("status") in ("done", "failed", "timeout"):
                    break
            out["final_status"] = (last or {}).get("status")
            out["final_phase"] = (last or {}).get("phase")
            res = (last or {}).get("result") or {}
            out["result_diff_files"] = [f.get("path") for f in res.get("diff", [])]
            out["reached"] = True
    except Exception as exc:  # noqa: BLE001
        out["reached"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"[:400]
    out["elapsed_s"] = round(_time.monotonic() - t0, 1)
    return out


@app.get("/v1/diag/host-capabilities")
def diag_host_capabilities() -> dict:
    """Introspect what THIS container (boss-api) can do on the host, so we can tell
    empirically — API, not SSH — whether it can create micro-VMs / containers.
    Read-only except one guarded, throwaway container-launch attempt."""
    import os as _os
    import platform as _platform
    import shutil as _shutil
    import subprocess as _subprocess

    def _run(cmd: list[str], timeout: float = 8.0) -> dict:
        try:
            p = _subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
            out = ((p.stdout or "") + (p.stderr or "")).strip()
            return {"rc": p.returncode, "out": out[:600]}
        except FileNotFoundError:
            return {"rc": None, "out": "binary-not-found"}
        except Exception as exc:  # timeout, permission, etc.
            return {"rc": None, "out": f"{type(exc).__name__}: {exc}"[:600]}

    # Effective capabilities — decode CapEff bitmask; CAP_SYS_ADMIN = bit 21.
    cap_eff = None
    has_sys_admin = None
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("CapEff:"):
                    cap_eff = line.split()[1]
                    has_sys_admin = bool(int(cap_eff, 16) & (1 << 21))
                    break
    except Exception:
        pass

    # /dev/kvm — the gate for real Firecracker/Kata micro-VMs.
    kvm = {"exists": _os.path.exists("/dev/kvm")}
    if kvm["exists"]:
        kvm["readable"] = _os.access("/dev/kvm", _os.R_OK)
        kvm["writable"] = _os.access("/dev/kvm", _os.W_OK)
        try:
            fd = _os.open("/dev/kvm", _os.O_RDWR)
            _os.close(fd)
            kvm["openable_rw"] = True
        except Exception as exc:
            kvm["openable_rw"] = False
            kvm["open_error"] = f"{type(exc).__name__}: {exc}"

    docker_sock = "/var/run/docker.sock"
    sock_present = _os.path.exists(docker_sock)
    docker_cli = _shutil.which("docker")
    result: dict = {
        "identity": {"uid": _os.getuid(), "gid": _os.getgid(), "euid": _os.geteuid()},
        "capabilities": {"cap_eff": cap_eff, "has_CAP_SYS_ADMIN": has_sys_admin},
        "kernel": {
            "system": _platform.system(),
            "release": _platform.release(),
            "machine": _platform.machine(),
        },
        "kvm": kvm,
        "docker": {
            "sock_present": sock_present,
            "cli_present": bool(docker_cli),
        },
        "virt_runtimes": {
            "firecracker": bool(_shutil.which("firecracker")),
            "kata_runtime": bool(_shutil.which("kata-runtime")),
            "runsc_gvisor": bool(_shutil.which("runsc")),
            "qemu_kvm": bool(_shutil.which("qemu-system-x86_64")),
        },
    }
    # Guarded, real attempt to actually create a container via the socket.
    if sock_present and docker_cli:
        result["docker"]["info"] = _run([docker_cli, "info", "--format", "{{.ServerVersion}}"])
        result["docker"]["launch_attempt"] = _run(
            [docker_cli, "run", "--rm", "hello-world"], timeout=30.0
        )

    # Bottom-line verdicts.
    can_container_now = bool(sock_present and docker_cli)
    can_microvm_now = bool(kvm.get("openable_rw") and has_sys_admin)
    result["verdict"] = {
        "can_launch_containers_now": can_container_now,
        "can_launch_microvms_now": can_microvm_now,
        "kvm_device_present": bool(kvm["exists"]),
        "note": (
            "boss-api can create sandboxes from inside its own container only if the "
            "host passes through the relevant privilege: docker.sock for containers, "
            "or /dev/kvm + CAP_SYS_ADMIN for micro-VMs. Missing => needs a compose "
            "grant from the platform (device/socket/cap), not a code change."
        ),
    }
    return result


@app.get("/v1/entitlements", response_model=LicenseEntitlementListResponse)
def list_license_entitlements(
    project_id: str,
    include_inactive: bool = False,
) -> LicenseEntitlementListResponse:
    with begin(engine) as connection:
        edition = commercial.edition_status(connection, project_id)
        items = commercial.list_license_entitlements(
            connection,
            project_id,
            include_inactive=include_inactive,
        )
        return LicenseEntitlementListResponse(
            project_id=project_id,
            edition=edition_status_from_service(edition),
            items=[license_entitlement_summary_from_service(item) for item in items],
        )


@app.post("/v1/account-connect/link", response_model=AccountConnectMutationResponse)
def link_account_connect(
    payload: AccountConnectLinkRequest,
) -> AccountConnectMutationResponse:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        edition = commercial.link_account_connect_org(
            connection,
            payload.project_id,
            hosted_org_id=payload.hosted_org_id,
            actor=payload.actor,
            connection_ref=payload.connection_ref,
            marketplace_identity=payload.marketplace_identity,
            billing_portal_ref=payload.billing_portal_ref,
            connector_authorization_refs=payload.connector_authorization_refs,
            optional_telemetry_enabled=payload.optional_telemetry_enabled,
            metadata=payload.metadata,
        )
        return AccountConnectMutationResponse(
            project_id=payload.project_id,
            edition=edition_status_from_service(edition),
        )


@app.post("/v1/account-connect/unlink", response_model=AccountConnectMutationResponse)
def unlink_account_connect(
    payload: AccountConnectUnlinkRequest,
) -> AccountConnectMutationResponse:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            edition = commercial.unlink_account_connect_org(
                connection,
                payload.project_id,
                actor=payload.actor,
                hosted_org_id=payload.hosted_org_id,
                reason=payload.reason,
            )
        except AccountConnectLinkNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return AccountConnectMutationResponse(
            project_id=payload.project_id,
            edition=edition_status_from_service(edition),
        )


@app.post("/v1/account-connect/sync", response_model=AccountConnectSyncResponse)
def sync_account_connect(
    payload: AccountConnectSyncRequest,
) -> AccountConnectSyncResponse:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            result = commercial.sync_account_connect_entitlements(
                connection,
                payload.project_id,
                actor=payload.actor,
                hosted_org_id=payload.hosted_org_id,
            )
        except AccountConnectLinkNotFoundError as error:
            record_account_connect_sync_denial(
                payload.project_id,
                payload.actor,
                "account_connect_not_linked",
            )
            raise HTTPException(status_code=404, detail=str(error)) from error
        except AccountConnectConfigurationError as error:
            record_account_connect_sync_denial(
                payload.project_id,
                payload.actor,
                "account_connect_url_missing",
            )
            raise HTTPException(status_code=503, detail=str(error)) from error
        except AccountConnectSyncError as error:
            record_account_connect_sync_denial(
                payload.project_id,
                payload.actor,
                "account_connect_sync_failed",
            )
            raise HTTPException(status_code=502, detail=str(error)) from error
        return AccountConnectSyncResponse(
            project_id=result["project_id"],
            edition=edition_status_from_service(result["edition"]),
            synced_entitlement_count=result["synced_entitlement_count"],
        )


# ── AI SRE reliability pipeline (RB-3/RB-4/RB-5) ─────────────────────────────
@app.post("/v1/reliability/scan", response_model=ReliabilityScanResponse)
def reliability_scan(payload: ReliabilityScanRequest) -> ReliabilityScanResponse:
    """Distill a telemetry window into the Insight report; when `persist`, cluster into incidents."""
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "providerops_manage")
        try:
            report = reliability.scan(
                connection, payload.project_id, payload.source,
                {"start": payload.window.start, "end": payload.window.end},
                source_id=payload.source_id, baseline=payload.baseline,
                query=payload.query, persist=payload.persist,
            )
        except (ValueError, KeyError) as error:
            raise HTTPException(status_code=400, detail=f"invalid scan request: {error}") from error
        return ReliabilityScanResponse(**report)


@app.get("/v1/reliability/incidents", response_model=ReliabilityIncidentListResponse)
def reliability_list_incidents(
    project_id: str, status: Optional[str] = None, limit: int = 50
) -> ReliabilityIncidentListResponse:
    with begin(engine) as connection:
        items = reliability.list_incidents(connection, project_id, status=status, limit=limit)
        return ReliabilityIncidentListResponse(project_id=project_id, items=items)


@app.get("/v1/reliability/incidents/{incident_id}", response_model=ReliabilityIncidentDetail)
def reliability_get_incident(incident_id: str, project_id: str) -> ReliabilityIncidentDetail:
    with begin(engine) as connection:
        inc = reliability.get_incident(connection, project_id, incident_id)
        if not inc:
            raise HTTPException(status_code=404, detail="incident not found")
        return ReliabilityIncidentDetail(**inc)


@app.post("/v1/reliability/incidents/{incident_id}/diagnose",
          response_model=ReliabilityDiagnoseResponse)
def reliability_diagnose(
    incident_id: str, payload: ReliabilityDiagnoseRequest
) -> ReliabilityDiagnoseResponse:
    """RCA over the knowledge graph: subgraph context + prior known_causes → ranked hypothesis."""
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "providerops_manage")
        result = reliability.diagnose(connection, payload.project_id, incident_id)
        if not result:
            raise HTTPException(status_code=404, detail="incident not found")
        return ReliabilityDiagnoseResponse(**result)


@app.post("/v1/reliability/incidents/{incident_id}/remediate",
          response_model=ReliabilityRemediateResponse)
def reliability_remediate(
    incident_id: str, payload: ReliabilityRemediateRequest
) -> ReliabilityRemediateResponse:
    """Diagnosed incident → bug-framed governed code task → gated fix PR (via the standard pipeline)."""
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "providerops_manage")
        result = reliability.remediate(
            connection, payload.project_id, incident_id, jobs=jobs,
            owner=payload.owner, repo=payload.repo, base_branch=payload.base_branch,
            actor=payload.actor,
        )
        if not result:
            raise HTTPException(status_code=404, detail="incident not found")
        return ReliabilityRemediateResponse(**result)


@app.post("/v1/reliability/incidents/{incident_id}/feedback",
          response_model=ReliabilityFeedbackResponse)
def reliability_feedback(
    incident_id: str, payload: ReliabilityFeedbackRequest
) -> ReliabilityFeedbackResponse:
    """Capture a verdict (real|false_alarm|fix_good|fix_bad) into the LearningLoop; a confirmed cause is
    written back to the knowledge graph."""
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "providerops_manage")
        try:
            result = reliability.feedback(connection, payload.project_id, incident_id,
                                          payload.verdict, cause=payload.cause, actor=payload.actor)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if not result:
            raise HTTPException(status_code=404, detail="incident not found")
        return ReliabilityFeedbackResponse(**result)


@app.get("/v1/reliability/graph", response_model=ReliabilityGraphResponse)
def reliability_graph(project_id: str, service: str, depth: int = 2) -> ReliabilityGraphResponse:
    """The knowledge-graph subgraph around a service — the same context bundle RCA reasons over."""
    with begin(engine) as connection:
        sub = reliability.kg.subgraph(connection, project_id, "service", service, depth=depth)
        return ReliabilityGraphResponse(project_id=project_id, root=sub["root"],
                                        nodes=sub["nodes"], edges=sub["edges"])


@app.post("/v1/reliability/graph/walk", response_model=ReliabilityGraphWalkResponse)
def reliability_graph_walk(payload: ReliabilityGraphWalkRequest) -> ReliabilityGraphWalkResponse:
    """Run a topology walker: kubernetes (deterministic, read-only RBAC), infra (BOSS inventory),
    or cooccurrence (inferred correlates_with edges from overlapping incidents)."""
    from .services import graph_walkers
    walker = graph_walkers.WALKERS.get(payload.walker)
    if not walker:
        raise HTTPException(status_code=400, detail=f"unknown walker {payload.walker!r}")
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "providerops_manage")
        try:
            if payload.walker == "kubernetes":
                counts = walker(connection, payload.project_id, reliability.kg,
                                payload.config["base_url"],
                                bearer_env=payload.config.get("bearer_env", ""),
                                namespace=payload.config.get("namespace", ""))
            elif payload.walker == "cooccurrence":
                counts = walker(connection, payload.project_id, reliability.kg,
                                window_minutes=int(payload.config.get("window_minutes", 30)),
                                min_overlap=int(payload.config.get("min_overlap", 2)))
            else:
                counts = walker(connection, payload.project_id, reliability.kg)
        except KeyError as error:
            raise HTTPException(status_code=400, detail=f"missing walker config: {error}") from error
        except OSError as error:
            raise HTTPException(status_code=502, detail=f"walker target unreachable: {error}") from error
        return ReliabilityGraphWalkResponse(walker=payload.walker, counts=counts)


@app.post("/v1/reliability/sources", response_model=ReliabilitySourceSummary, status_code=201)
def reliability_register_source(payload: ReliabilitySourceRequest) -> ReliabilitySourceSummary:
    """Register/enable a telemetry source (config only — creds stay indirect via env/SecretRef)."""
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "providerops_manage")
        row = reliability.register_source(
            connection, payload.project_id, payload.kind, payload.name,
            config=payload.config, secret_ref=payload.secret_ref, enabled=payload.enabled,
        )
        return ReliabilitySourceSummary(**{k: row[k] for k in
                                           ("id", "kind", "name", "config", "enabled", "created_at")})


@app.get("/v1/reliability/sources", response_model=ReliabilitySourceListResponse)
def reliability_list_sources(project_id: str) -> ReliabilitySourceListResponse:
    with begin(engine) as connection:
        items = reliability.list_sources(connection, project_id)
        return ReliabilitySourceListResponse(project_id=project_id, items=items)


@app.post("/v1/reliability/alert", status_code=202)
async def reliability_alert_webhook(request: Request):
    """RB-10 — the incident-response trigger: a signed alert (Alertmanager/Grafana/PagerDuty/generic)
    becomes an incident, every enabled source is scan-scoped to the alert window, and diagnose runs
    immediately. Auth-exempt but X-Reliability-Signature (HMAC-sha256) verified; fail-closed."""
    raw_body = await request.body()
    signature = request.headers.get("x-reliability-signature", "")
    if not reliability.verify_alert_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid alert signature.")
    try:
        payload = json.loads(raw_body.decode() or "{}")
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="Invalid alert payload.") from error
    project_id = payload.get("project_id") or settings.default_project_id
    with begin(engine) as connection:
        result = reliability.ingest_alert(connection, project_id, payload, jobs=jobs)
    if not result:
        raise HTTPException(status_code=400, detail="Unrecognized alert shape.")
    return JSONResponse(status_code=202, content=jsonable_encoder(result))


@app.get("/v1/reliability/policy", response_model=ReliabilityPolicyResponse)
def reliability_get_policy(project_id: str) -> ReliabilityPolicyResponse:
    with begin(engine) as connection:
        return ReliabilityPolicyResponse(**reliability.get_policy(connection, project_id))


@app.post("/v1/reliability/policy", response_model=ReliabilityPolicyResponse)
def reliability_set_policy(payload: ReliabilityPolicyRequest) -> ReliabilityPolicyResponse:
    """RB-19 — the governed autonomy spectrum, per project. Tier-0 auto-launch stays fail-closed:
    the remediation it triggers is the same gated pipeline PR."""
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "providerops_manage")
        row = reliability.set_policy(
            connection, payload.project_id, auto_remediate=payload.auto_remediate,
            min_confidence=payload.min_confidence, max_blast_radius=payload.max_blast_radius,
            max_auto_severity=payload.max_auto_severity,
        )
        return ReliabilityPolicyResponse(**row)


@app.get("/v1/reliability/incidents/{incident_id}/decision",
         response_model=ReliabilityDecisionResponse)
def reliability_decision(incident_id: str, project_id: str) -> ReliabilityDecisionResponse:
    """What the autonomy policy would do with this incident right now (tier + action + why)."""
    with begin(engine) as connection:
        return ReliabilityDecisionResponse(
            **reliability.decide_action(connection, project_id, incident_id))


@app.get("/v1/reliability/incidents/{incident_id}/investigation")
def reliability_investigation(incident_id: str, project_id: str):
    """RB-20 — the consolidated investigation bundle (the MCP surface): incident + signals +
    evidence + remediations + graph neighborhood + autonomy decision in one call."""
    with begin(engine) as connection:
        bundle = reliability.investigation(connection, project_id, incident_id)
        if not bundle:
            raise HTTPException(status_code=404, detail="incident not found")
        return JSONResponse(content=jsonable_encoder(bundle))


@app.post("/v1/reliability/meta-eval")
def reliability_meta_eval():
    """RB-14 — measure the measurer: detection precision/recall/F1 + RCA top-1 accuracy against
    the labeled synthetic corpus. Pure — writes nothing."""
    from .services.reliability_meta_eval import run_meta_eval
    return JSONResponse(content=jsonable_encoder(run_meta_eval()))


@app.post("/v1/reliability/incidents/{incident_id}/verify",
          response_model=ReliabilityVerifyResponse)
def reliability_verify(
    incident_id: str, payload: ReliabilityVerifyRequest
) -> ReliabilityVerifyResponse:
    """Production verification loop: re-watch the incident's own signals; a recurrence reopens it,
    a clear stability window auto-resolves it."""
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "providerops_manage")
        try:
            result = reliability.verify(
                connection, payload.project_id, incident_id, payload.source,
                {"start": payload.window.start, "end": payload.window.end},
                stability_minutes=payload.stability_minutes, query=payload.query,
                actor=payload.actor,
            )
        except (ValueError, KeyError) as error:
            raise HTTPException(status_code=400, detail=f"invalid verify request: {error}") from error
        if not result:
            raise HTTPException(status_code=404, detail="incident not found")
        return ReliabilityVerifyResponse(**result)


@app.get(
    "/v1/account-connect/health-checks",
    response_model=AccountConnectHealthCheckListResponse,
)
def list_account_connect_health_checks(
    project_id: str,
    limit: int = 50,
) -> AccountConnectHealthCheckListResponse:
    with begin(engine) as connection:
        items = commercial.list_health_checks(connection, project_id, limit=limit)
        return AccountConnectHealthCheckListResponse(
            project_id=project_id,
            items=[
                account_connect_health_check_from_service(item)
                for item in items
            ],
        )


@app.post(
    "/v1/account-connect/health-checks",
    response_model=AccountConnectHealthCheckSummary,
    status_code=201,
)
def run_account_connect_health_check(
    payload: AccountConnectHealthCheckRunRequest,
) -> AccountConnectHealthCheckSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            check = commercial.run_health_check(
                connection,
                payload.project_id,
                actor=payload.actor,
                hosted_org_id=payload.hosted_org_id,
                check_kind=payload.check_kind,
                metadata=payload.metadata,
            )
        except AccountConnectAuthSessionStateError as error:
            record_account_connect_denial(
                payload.project_id,
                payload.actor,
                "account_connect.health_check_denied",
                "account_connect_hosted_org_mismatch",
                "account_connect_health_check",
            )
            raise HTTPException(status_code=409, detail=str(error)) from error
        return account_connect_health_check_from_service(check)


@app.get(
    "/v1/account-connect/auth-sessions",
    response_model=AccountConnectAuthSessionListResponse,
)
def list_account_connect_auth_sessions(
    project_id: str,
    limit: int = 50,
) -> AccountConnectAuthSessionListResponse:
    with begin(engine) as connection:
        items = commercial.list_auth_sessions(connection, project_id, limit=limit)
        return AccountConnectAuthSessionListResponse(
            project_id=project_id,
            items=[
                account_connect_auth_session_from_service(item)
                for item in items
            ],
        )


@app.post(
    "/v1/account-connect/auth-sessions",
    response_model=AccountConnectAuthSessionSummary,
    status_code=201,
)
def create_account_connect_auth_session(
    payload: AccountConnectAuthSessionCreateRequest,
) -> AccountConnectAuthSessionSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            session = commercial.create_auth_session(
                connection,
                payload.project_id,
                actor=payload.actor,
                return_url=payload.return_url,
                hosted_org_id=payload.hosted_org_id,
                metadata=payload.metadata,
            )
        except AccountConnectAuthSessionStateError as error:
            record_account_connect_denial(
                payload.project_id,
                payload.actor,
                "account_connect.auth_session_denied",
                "account_connect_hosted_org_mismatch",
                "account_connect_auth_session",
            )
            raise HTTPException(status_code=409, detail=str(error)) from error
        return account_connect_auth_session_from_service(session)


@app.post(
    "/v1/account-connect/auth-sessions/{session_id}/complete",
    response_model=AccountConnectAuthSessionSummary,
)
def complete_account_connect_auth_session(
    session_id: str,
    payload: AccountConnectAuthSessionCompleteRequest,
) -> AccountConnectAuthSessionSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            session = commercial.complete_auth_session(
                connection,
                session_id,
                project_id=payload.project_id,
                actor=payload.actor,
                code=payload.code,
                state=payload.state,
                metadata=payload.metadata,
            )
        except AccountConnectAuthSessionNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except AccountConnectAuthSessionStateError as error:
            record_account_connect_denial(
                payload.project_id,
                payload.actor,
                "account_connect.auth_session_denied",
                "state_mismatch",
                "account_connect_auth_session",
            )
            raise HTTPException(status_code=409, detail=str(error)) from error
        return account_connect_auth_session_from_service(session)


@app.get(
    "/v1/account-connect/team-invites",
    response_model=AccountConnectTeamInviteListResponse,
)
def list_account_connect_team_invites(
    project_id: str,
    limit: int = 50,
) -> AccountConnectTeamInviteListResponse:
    with begin(engine) as connection:
        items = commercial.list_team_invites(connection, project_id, limit=limit)
        return AccountConnectTeamInviteListResponse(
            project_id=project_id,
            items=[
                account_connect_team_invite_from_service(item)
                for item in items
            ],
        )


@app.post(
    "/v1/account-connect/team-invites",
    response_model=AccountConnectTeamInviteSummary,
    status_code=201,
)
def create_account_connect_team_invite(
    payload: AccountConnectTeamInviteCreateRequest,
) -> AccountConnectTeamInviteSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            invite = commercial.create_team_invite(
                connection,
                payload.project_id,
                email=payload.email,
                display_name=payload.display_name,
                role_keys=payload.role_keys,
                permissions=payload.permissions,
                metadata=payload.metadata,
                actor=payload.actor,
            )
        except AccountConnectLinkNotFoundError as error:
            record_account_connect_denial(
                payload.project_id,
                payload.actor,
                "account_connect.team_invite_denied",
                "account_connect_not_linked",
                "account_connect_team_invite",
            )
            raise HTTPException(status_code=404, detail=str(error)) from error
        return account_connect_team_invite_from_service(invite)


@app.post(
    "/v1/account-connect/team-invites/{invite_id}/revoke",
    response_model=AccountConnectTeamInviteSummary,
)
def revoke_account_connect_team_invite(
    invite_id: str,
    payload: AccountConnectTeamInviteRevokeRequest,
) -> AccountConnectTeamInviteSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            invite = commercial.revoke_team_invite(
                connection,
                invite_id,
                project_id=payload.project_id,
                actor=payload.actor,
                reason=payload.reason,
            )
        except AccountConnectLinkNotFoundError as error:
            record_account_connect_denial(
                payload.project_id,
                payload.actor,
                "account_connect.team_invite_revoke_denied",
                "account_connect_team_invite_not_found",
                "account_connect_team_invite",
            )
            raise HTTPException(status_code=404, detail=str(error)) from error
        return account_connect_team_invite_from_service(invite)


@app.get(
    "/v1/account-connect/billing-portal-sessions",
    response_model=AccountConnectBillingPortalSessionListResponse,
)
def list_account_connect_billing_portal_sessions(
    project_id: str,
    limit: int = 50,
) -> AccountConnectBillingPortalSessionListResponse:
    with begin(engine) as connection:
        items = commercial.list_billing_portal_sessions(
            connection,
            project_id,
            limit=limit,
        )
        return AccountConnectBillingPortalSessionListResponse(
            project_id=project_id,
            items=[
                account_connect_billing_session_from_service(item)
                for item in items
            ],
        )


@app.post(
    "/v1/account-connect/billing-portal-sessions",
    response_model=AccountConnectBillingPortalSessionSummary,
    status_code=201,
)
def create_account_connect_billing_portal_session(
    payload: AccountConnectBillingPortalSessionCreateRequest,
) -> AccountConnectBillingPortalSessionSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            session = commercial.create_billing_portal_session(
                connection,
                payload.project_id,
                actor=payload.actor,
                return_url=payload.return_url,
                metadata=payload.metadata,
            )
        except AccountConnectLinkNotFoundError as error:
            record_account_connect_denial(
                payload.project_id,
                payload.actor,
                "account_connect.billing_portal_denied",
                "account_connect_not_linked",
                "account_connect_billing_portal_session",
            )
            raise HTTPException(status_code=404, detail=str(error)) from error
        return account_connect_billing_session_from_service(session)


@app.get("/v1/enterprise/clusters", response_model=EnterpriseClusterListResponse)
def list_enterprise_clusters(project_id: str) -> EnterpriseClusterListResponse:
    with begin(engine) as connection:
        items = enterprise.list_clusters(connection, project_id)
        return EnterpriseClusterListResponse(
            project_id=project_id,
            items=[enterprise_cluster_summary_from_service(item) for item in items],
        )


@app.post(
    "/v1/enterprise/clusters",
    response_model=EnterpriseClusterSummary,
    status_code=201,
)
def upsert_enterprise_cluster(
    payload: EnterpriseClusterUpsertRequest,
) -> EnterpriseClusterSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "enterprise_manage",
        )
        try:
            cluster = enterprise.upsert_cluster(
                connection,
                payload.project_id,
                deployment_mode=payload.deployment_mode,
                account_connect_mode=payload.account_connect_mode,
                actor=payload.actor,
                worker_pool_strategy=payload.worker_pool_strategy,
                ha_profile=payload.ha_profile,
                data_residency=payload.data_residency,
                identity_controls=payload.identity_controls,
                governance_controls=payload.governance_controls,
                audit_controls=payload.audit_controls,
                private_worker_pools=payload.private_worker_pools,
                compliance_packs=payload.compliance_packs,
                support_tier=payload.support_tier,
                support_sla=payload.support_sla,
                status=payload.status,
                metadata=payload.metadata,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return enterprise_cluster_summary_from_service(cluster)


@app.get(
    "/v1/enterprise/cluster-validation-runs",
    response_model=EnterpriseClusterValidationRunListResponse,
)
def list_enterprise_cluster_validation_runs(
    project_id: str,
    enterprise_cluster_id: Optional[str] = None,
    limit: int = 50,
) -> EnterpriseClusterValidationRunListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    with begin(engine) as connection:
        items = enterprise.list_cluster_validation_runs(
            connection,
            project_id,
            enterprise_cluster_id=enterprise_cluster_id,
            limit=limit,
        )
        return EnterpriseClusterValidationRunListResponse(
            project_id=project_id,
            items=[
                enterprise_cluster_validation_run_summary_from_service(item)
                for item in items
            ],
        )


@app.post(
    "/v1/enterprise/cluster-validation-runs",
    response_model=EnterpriseClusterValidationRunSummary,
    status_code=201,
)
def create_enterprise_cluster_validation_run(
    payload: EnterpriseClusterValidationRunCreateRequest,
) -> EnterpriseClusterValidationRunSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "enterprise_manage",
        )
        try:
            run = enterprise.record_cluster_validation_run(
                connection,
                payload.project_id,
                enterprise_cluster_id=payload.enterprise_cluster_id,
                validation_kind=payload.validation_kind,
                status=payload.status,
                checks=payload.checks,
                evidence_refs=payload.evidence_refs,
                actor=payload.actor,
                metadata=payload.metadata,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return enterprise_cluster_validation_run_summary_from_service(run)


@app.get(
    "/v1/enterprise/support-cases",
    response_model=EnterpriseSupportCaseListResponse,
)
def list_enterprise_support_cases(
    project_id: str,
    limit: int = 50,
) -> EnterpriseSupportCaseListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    with begin(engine) as connection:
        items = enterprise.list_support_cases(connection, project_id, limit)
        return EnterpriseSupportCaseListResponse(
            project_id=project_id,
            items=[
                enterprise_support_case_summary_from_service(item) for item in items
            ],
        )


@app.post(
    "/v1/enterprise/support-cases",
    response_model=EnterpriseSupportCaseSummary,
    status_code=201,
)
def create_enterprise_support_case(
    payload: EnterpriseSupportCaseCreateRequest,
) -> EnterpriseSupportCaseSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "enterprise_manage",
        )
        try:
            item = enterprise.create_support_case(
                connection,
                payload.project_id,
                title=payload.title,
                description=payload.description,
                actor=payload.actor,
                severity=payload.severity,
                category=payload.category,
                enterprise_cluster_id=payload.enterprise_cluster_id,
                escalate_to_hosted=payload.escalate_to_hosted,
                metadata=payload.metadata,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return enterprise_support_case_summary_from_service(item)


@app.post(
    "/v1/enterprise/support-cases/{case_id}",
    response_model=EnterpriseSupportCaseSummary,
)
def update_enterprise_support_case(
    case_id: str,
    payload: EnterpriseSupportCaseUpdateRequest,
) -> EnterpriseSupportCaseSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "enterprise_manage",
        )
        try:
            item = enterprise.update_support_case(
                connection,
                payload.project_id,
                case_id,
                actor=payload.actor,
                status=payload.status,
                assigned_to=payload.assigned_to,
                metadata=payload.metadata,
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return enterprise_support_case_summary_from_service(item)


@app.get(
    "/v1/enterprise/identity-rollout-policies",
    response_model=EnterpriseIdentityRolloutPolicyListResponse,
)
def list_enterprise_identity_rollout_policies(
    project_id: str,
) -> EnterpriseIdentityRolloutPolicyListResponse:
    with begin(engine) as connection:
        policies = enterprise.list_identity_rollout_policies(connection, project_id)
        return EnterpriseIdentityRolloutPolicyListResponse(
            project_id=project_id,
            items=[
                enterprise_identity_rollout_policy_summary_from_service(policy)
                for policy in policies
            ],
        )


@app.post(
    "/v1/enterprise/identity-rollout-policies",
    response_model=EnterpriseIdentityRolloutPolicySummary,
    status_code=201,
)
def upsert_enterprise_identity_rollout_policy(
    payload: EnterpriseIdentityRolloutPolicyUpsertRequest,
) -> EnterpriseIdentityRolloutPolicySummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "enterprise_manage",
        )
        try:
            policy = enterprise.upsert_identity_rollout_policy(
                connection,
                payload.project_id,
                provider_type=payload.provider_type,
                runtime_mode=payload.runtime_mode,
                allowed_domains=payload.allowed_domains,
                rollout_notes=payload.rollout_notes,
                metadata=payload.metadata,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return enterprise_identity_rollout_policy_summary_from_service(policy)


@app.get(
    "/v1/enterprise/identity-providers",
    response_model=EnterpriseIdentityProviderListResponse,
)
def list_enterprise_identity_providers(
    project_id: str,
) -> EnterpriseIdentityProviderListResponse:
    with begin(engine) as connection:
        providers = enterprise.list_identity_providers(connection, project_id)
        return EnterpriseIdentityProviderListResponse(
            project_id=project_id,
            items=[
                enterprise_identity_provider_summary_from_service(provider)
                for provider in providers
            ],
        )


@app.post(
    "/v1/enterprise/identity-providers",
    response_model=EnterpriseIdentityProviderSummary,
    status_code=201,
)
def upsert_enterprise_identity_provider(
    payload: EnterpriseIdentityProviderUpsertRequest,
) -> EnterpriseIdentityProviderSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "enterprise_manage",
        )
        try:
            provider = enterprise.upsert_identity_provider(
                connection,
                payload.project_id,
                provider_key=payload.provider_key,
                provider_type=payload.provider_type,
                display_name=payload.display_name,
                actor=payload.actor,
                issuer=payload.issuer,
                audience=payload.audience,
                entity_id=payload.entity_id,
                sso_url=payload.sso_url,
                jwks_url=payload.jwks_url,
                certificate_fingerprint=payload.certificate_fingerprint,
                scim_base_url=payload.scim_base_url,
                secret_ref=payload.secret_ref,
                status=payload.status,
                test_status=payload.test_status,
                metadata=payload.metadata,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return enterprise_identity_provider_summary_from_service(provider)


@app.get(
    "/v1/enterprise/scim/groups",
    response_model=EnterpriseScimGroupListResponse,
)
def list_enterprise_scim_groups(project_id: str) -> EnterpriseScimGroupListResponse:
    with begin(engine) as connection:
        groups = enterprise.list_scim_groups(connection, project_id)
        return EnterpriseScimGroupListResponse(
            project_id=project_id,
            items=[enterprise_scim_group_summary_from_service(group) for group in groups],
        )


@app.post(
    "/v1/enterprise/scim/groups",
    response_model=EnterpriseScimGroupSummary,
    status_code=201,
)
def upsert_enterprise_scim_group(
    payload: EnterpriseScimGroupUpsertRequest,
) -> EnterpriseScimGroupSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "enterprise_manage",
        )
        try:
            group = enterprise.upsert_scim_group(
                connection,
                payload.project_id,
                display_name=payload.display_name,
                actor=payload.actor,
                external_id=payload.external_id,
                role_keys=payload.role_keys,
                permissions=payload.permissions,
                raw_group=payload.raw_group,
                status=payload.status,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return enterprise_scim_group_summary_from_service(group)


@app.get(
    "/v1/enterprise/scim/users",
    response_model=EnterpriseScimUserListResponse,
)
def list_enterprise_scim_users(project_id: str) -> EnterpriseScimUserListResponse:
    with begin(engine) as connection:
        users = enterprise.list_scim_users(connection, project_id)
        return EnterpriseScimUserListResponse(
            project_id=project_id,
            items=[enterprise_scim_user_summary_from_service(user) for user in users],
        )


@app.post(
    "/v1/enterprise/scim/users",
    response_model=EnterpriseScimUserSummary,
    status_code=201,
)
def upsert_enterprise_scim_user(
    payload: EnterpriseScimUserUpsertRequest,
) -> EnterpriseScimUserSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "enterprise_manage",
        )
        try:
            user = enterprise.upsert_scim_user(
                connection,
                payload.project_id,
                user_name=payload.user_name,
                display_name=payload.display_name,
                actor=payload.actor,
                external_id=payload.external_id,
                active=payload.active,
                role_keys=payload.role_keys,
                permissions=payload.permissions,
                groups=payload.groups,
                raw_profile=payload.raw_profile,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return enterprise_scim_user_summary_from_service(user)


@app.get("/scim/v2/{project_id}/Groups")
def scim_list_groups(
    project_id: str,
    request: Request,
    filter: Optional[str] = Query(default=None),  # noqa: A002 - SCIM parameter name.
):
    with begin(engine) as connection:
        denied = require_enterprise_scim_access(request, connection, project_id)
        if denied:
            return denied
        groups = enterprise.list_scim_groups(connection, project_id)
        display_filter = scim_filter_value(filter, "displayName")
        if display_filter:
            groups = [group for group in groups if group["display_name"] == display_filter]
        return scim_list_response([scim_group_resource(group) for group in groups])


@app.post("/scim/v2/{project_id}/Groups", status_code=201)
async def scim_create_group(
    project_id: str,
    request: Request,
):
    with begin(engine) as connection:
        denied = require_enterprise_scim_access(request, connection, project_id)
        if denied:
            return denied
        payload = await request.json()
        if not isinstance(payload, dict):
            return scim_error(400, "SCIM group payload must be a JSON object.")
        service_payload = scim_group_payload_to_service(payload)
        try:
            group = enterprise.upsert_scim_group(
                connection,
                project_id,
                display_name=service_payload["display_name"],
                actor="scim",
                external_id=service_payload["external_id"],
                role_keys=service_payload["role_keys"],
                permissions=service_payload["permissions"],
                raw_group=service_payload["raw_group"],
                status=service_payload["status"],
            )
        except ValueError as error:
            return scim_error(400, str(error))
        return scim_group_resource(group)


@app.get("/scim/v2/{project_id}/Groups/{group_id}")
def scim_get_group(
    project_id: str,
    group_id: str,
    request: Request,
):
    with begin(engine) as connection:
        denied = require_enterprise_scim_access(request, connection, project_id)
        if denied:
            return denied
        try:
            group = enterprise.get_scim_group_by_identifier(connection, project_id, group_id)
        except ValueError as error:
            return scim_error(404, str(error))
        return scim_group_resource(group)


@app.put("/scim/v2/{project_id}/Groups/{group_id}")
async def scim_replace_group(
    project_id: str,
    group_id: str,
    request: Request,
):
    with begin(engine) as connection:
        denied = require_enterprise_scim_access(request, connection, project_id)
        if denied:
            return denied
        payload = await request.json()
        if not isinstance(payload, dict):
            return scim_error(400, "SCIM group payload must be a JSON object.")
        payload.setdefault("externalId", group_id)
        service_payload = scim_group_payload_to_service(payload)
        try:
            group = enterprise.upsert_scim_group(
                connection,
                project_id,
                display_name=service_payload["display_name"],
                actor="scim",
                external_id=service_payload["external_id"],
                role_keys=service_payload["role_keys"],
                permissions=service_payload["permissions"],
                raw_group=service_payload["raw_group"],
                status=service_payload["status"],
            )
        except ValueError as error:
            return scim_error(400, str(error))
        return scim_group_resource(group)


@app.delete("/scim/v2/{project_id}/Groups/{group_id}", status_code=204)
def scim_delete_group(
    project_id: str,
    group_id: str,
    request: Request,
):
    with begin(engine) as connection:
        denied = require_enterprise_scim_access(request, connection, project_id)
        if denied:
            return denied
        try:
            group = enterprise.get_scim_group_by_identifier(connection, project_id, group_id)
            enterprise.upsert_scim_group(
                connection,
                project_id,
                display_name=group["display_name"],
                actor="scim",
                external_id=group.get("external_id"),
                role_keys=group.get("role_keys") or [],
                permissions=group.get("permissions") or [],
                raw_group=group.get("raw_group") or {},
                status="inactive",
            )
        except ValueError as error:
            return scim_error(404, str(error))
        return Response(status_code=204)


@app.get("/scim/v2/{project_id}/Users")
def scim_list_users(
    project_id: str,
    request: Request,
    filter: Optional[str] = Query(default=None),  # noqa: A002 - SCIM parameter name.
):
    with begin(engine) as connection:
        denied = require_enterprise_scim_access(request, connection, project_id)
        if denied:
            return denied
        users = enterprise.list_scim_users(connection, project_id)
        user_name_filter = scim_filter_value(filter, "userName")
        if user_name_filter:
            users = [
                user
                for user in users
                if user["user_name"] == user_name_filter.strip().lower()
            ]
        return scim_list_response([scim_user_resource(user) for user in users])


@app.post("/scim/v2/{project_id}/Users", status_code=201)
async def scim_create_user(
    project_id: str,
    request: Request,
):
    with begin(engine) as connection:
        denied = require_enterprise_scim_access(request, connection, project_id)
        if denied:
            return denied
        payload = await request.json()
        if not isinstance(payload, dict):
            return scim_error(400, "SCIM user payload must be a JSON object.")
        service_payload = scim_user_payload_to_service(payload)
        try:
            user = enterprise.upsert_scim_user(
                connection,
                project_id,
                user_name=service_payload["user_name"],
                display_name=service_payload["display_name"],
                actor="scim",
                external_id=service_payload["external_id"],
                active=service_payload["active"],
                role_keys=service_payload["role_keys"],
                permissions=service_payload["permissions"],
                groups=service_payload["groups"],
                raw_profile=service_payload["raw_profile"],
            )
        except ValueError as error:
            return scim_error(400, str(error))
        return scim_user_resource(user)


@app.get("/scim/v2/{project_id}/Users/{user_id}")
def scim_get_user(
    project_id: str,
    user_id: str,
    request: Request,
):
    with begin(engine) as connection:
        denied = require_enterprise_scim_access(request, connection, project_id)
        if denied:
            return denied
        try:
            user = enterprise.get_scim_user_by_identifier(connection, project_id, user_id)
        except ValueError as error:
            return scim_error(404, str(error))
        return scim_user_resource(user)


@app.put("/scim/v2/{project_id}/Users/{user_id}")
async def scim_replace_user(
    project_id: str,
    user_id: str,
    request: Request,
):
    with begin(engine) as connection:
        denied = require_enterprise_scim_access(request, connection, project_id)
        if denied:
            return denied
        payload = await request.json()
        if not isinstance(payload, dict):
            return scim_error(400, "SCIM user payload must be a JSON object.")
        payload.setdefault("externalId", user_id)
        service_payload = scim_user_payload_to_service(payload)
        try:
            user = enterprise.upsert_scim_user(
                connection,
                project_id,
                user_name=service_payload["user_name"],
                display_name=service_payload["display_name"],
                actor="scim",
                external_id=service_payload["external_id"],
                active=service_payload["active"],
                role_keys=service_payload["role_keys"],
                permissions=service_payload["permissions"],
                groups=service_payload["groups"],
                raw_profile=service_payload["raw_profile"],
            )
        except ValueError as error:
            return scim_error(400, str(error))
        return scim_user_resource(user)


@app.delete("/scim/v2/{project_id}/Users/{user_id}", status_code=204)
def scim_delete_user(
    project_id: str,
    user_id: str,
    request: Request,
):
    with begin(engine) as connection:
        denied = require_enterprise_scim_access(request, connection, project_id)
        if denied:
            return denied
        try:
            user = enterprise.get_scim_user_by_identifier(connection, project_id, user_id)
            enterprise.upsert_scim_user(
                connection,
                project_id,
                user_name=user["user_name"],
                display_name=user["display_name"],
                actor="scim",
                external_id=user.get("external_id"),
                active=False,
                role_keys=user.get("role_keys") or [],
                permissions=user.get("permissions") or [],
                groups=user.get("groups") or [],
                raw_profile=user.get("raw_profile") or {},
            )
        except ValueError as error:
            return scim_error(404, str(error))
        return Response(status_code=204)


@app.get("/v1/orgs")
def list_orgs():
    """Organizations onboarded to BOSS, each with its primary project and some
    activity counts. Used by the org picker shown before entering the console."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT o.id AS org_id, o.name AS org_name, o.created_at,
                       p.id AS project_id, p.name AS project_name, p.slug AS project_slug,
                       w.name AS workspace_name,
                       (SELECT count(*) FROM tasks t WHERE t.project_id = p.id) AS task_count,
                       (SELECT count(*) FROM meetings m WHERE m.project_id = p.id) AS meeting_count
                FROM orgs o
                JOIN workspaces w ON w.org_id = o.id
                JOIN projects p ON p.workspace_id = w.id
                -- one primary (earliest) project per org
                WHERE p.created_at = (
                    SELECT min(p2.created_at) FROM projects p2
                    JOIN workspaces w2 ON w2.id = p2.workspace_id
                    WHERE w2.org_id = o.id
                )
                ORDER BY o.created_at
                """
            )
        ).mappings().all()
        return {
            "items": [
                {
                    "org_id": r["org_id"],
                    "org_name": r["org_name"],
                    "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else r["created_at"],
                    "project_id": r["project_id"],
                    "project_name": r["project_name"],
                    "project_slug": r["project_slug"],
                    "workspace_name": r["workspace_name"],
                    "task_count": int(r["task_count"] or 0),
                    "meeting_count": int(r["meeting_count"] or 0),
                }
                for r in rows
            ]
        }


@app.post("/v1/orgs", status_code=201)
def onboard_org_endpoint(payload: dict):
    """Onboard a new organization: create org → workspace → project and seed it
    with the common defaults (budget policy, members, packs, agent modules,
    entitlement sources, MCP servers). No auth yet — this is the admin action."""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Organization name is required.")
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .bootstrap import onboard_org
    with begin(engine) as connection:
        result = onboard_org(
            connection, settings, name,
            entitlements=entitlements, members=members, specpack=specpack,
        )
        # A clearly-labeled welcome task so a brand-new org isn't empty in Work/Overview.
        intake.create_task(
            connection,
            project_id=result["project_id"],
            title=f"👋 Welcome to BOSS, {name} — issue your first command",
            description=(
                "Your organization is set up with the shared capabilities (tools, skills, MCP servers, "
                "meeting roles) and a budget policy. Get started:\n"
                "• Overview → issue a CEO command and BOSS will plan it, gating any real effect behind your approval.\n"
                "• Meetings → convene a cross-functional agent panel to debate and draft a PRD.\n"
                "• Capabilities → review the tools and skills your agents can use, and plug in your own MCP servers.\n"
                "You can close this task once you're oriented."
            ),
            dedupe_key=make_id("welcome"),
            priority="p3",
            risk_tier=0,
            data_boundary="internal",
        )
    return result


@app.get("/v1/projects", response_model=ProjectListResponse)
def list_projects() -> ProjectListResponse:
    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT p.id, p.name, p.slug, p.workspace_id, p.created_at,
                       w.name AS workspace_name, o.id AS org_id, o.name AS org_name
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                ORDER BY o.name ASC, w.name ASC, p.name ASC
                """
            )
        ).mappings().all()
        return ProjectListResponse(
            items=[project_summary_from_row(row) for row in rows]
        )


@app.get("/v1/members", response_model=ProjectMemberListResponse)
def list_project_members(project_id: str) -> ProjectMemberListResponse:
    with begin(engine) as connection:
        return ProjectMemberListResponse(
            project_id=project_id,
            items=[
                project_member_summary_from_service(member)
                for member in members.list_members(connection, project_id)
            ],
        )


@app.get("/v1/members/{member_id}", response_model=ProjectMemberSummary)
def get_project_member(member_id: str, project_id: str) -> ProjectMemberSummary:
    with begin(engine) as connection:
        try:
            member = members.get_member(connection, project_id, member_id)
        except ProjectMemberNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return project_member_summary_from_service(member)


@app.post("/v1/members", response_model=ProjectMemberSummary, status_code=201)
def upsert_project_member(
    payload: ProjectMemberUpsertRequest,
) -> ProjectMemberSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "member_manage",
        )
        member = members.upsert_member(
            connection,
            project_id=payload.project_id,
            member_id=payload.member_id,
            display_name=payload.display_name,
            member_type=payload.member_type,
            role_keys=payload.role_keys,
            permissions=payload.permissions,
            actor=payload.actor,
        )
        return project_member_summary_from_service(member)


@app.get("/v1/org-members", response_model=OrgMemberListResponse)
def list_org_members(org_id: str, project_id: str, actor: str = "ceo") -> OrgMemberListResponse:
    with begin(engine) as connection:
        if not members.project_belongs_to_org(
            connection,
            project_id=project_id,
            org_id=org_id,
        ):
            raise HTTPException(
                status_code=404,
                detail=f"Project '{project_id}' is not in org '{org_id}'.",
            )
        require_project_actor(connection, project_id, actor, "member_manage")
        return OrgMemberListResponse(
            org_id=org_id,
            items=[
                org_member_summary_from_service(member)
                for member in members.list_org_members(connection, org_id)
            ],
        )


@app.post("/v1/org-members", response_model=OrgMemberSummary, status_code=201)
def upsert_org_member(payload: OrgMemberUpsertRequest) -> OrgMemberSummary:
    with begin(engine) as connection:
        if not members.project_belongs_to_org(
            connection,
            project_id=payload.project_id,
            org_id=payload.org_id,
        ):
            raise HTTPException(
                status_code=404,
                detail=f"Project '{payload.project_id}' is not in org '{payload.org_id}'.",
            )
        require_project_actor(connection, payload.project_id, payload.actor, "member_manage")
        member = members.upsert_org_member(
            connection,
            org_id=payload.org_id,
            member_id=payload.member_id,
            display_name=payload.display_name,
            member_type=payload.member_type,
            role_keys=payload.role_keys,
            permissions=payload.permissions,
            actor=payload.actor,
        )
        return org_member_summary_from_service(member)


@app.get("/v1/break-glass/grants", response_model=BreakGlassGrantListResponse)
def list_break_glass_grants(
    project_id: str,
    include_expired: bool = False,
) -> BreakGlassGrantListResponse:
    with begin(engine) as connection:
        return BreakGlassGrantListResponse(
            project_id=project_id,
            items=[
                break_glass_grant_summary_from_service(grant)
                for grant in break_glass.list_grants(
                    connection,
                    project_id,
                    include_expired=include_expired,
                )
            ],
        )


@app.post(
    "/v1/break-glass/grants",
    response_model=BreakGlassGrantSummary,
    status_code=201,
)
def create_break_glass_grant(
    payload: BreakGlassGrantCreateRequest,
) -> BreakGlassGrantSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "member_manage",
        )
        try:
            members.require_member(
                connection,
                payload.project_id,
                payload.target_member_id,
            )
            grant = break_glass.create_grant(
                connection,
                project_id=payload.project_id,
                target_member_id=payload.target_member_id,
                requested_by=payload.actor,
                reason=payload.reason,
                scope=payload.scope,
                granted_permissions=payload.granted_permissions,
                ttl_minutes=payload.ttl_minutes,
            )
        except ProjectMemberNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        task = intake.create_task(
            connection,
            project_id=payload.project_id,
            title=f"Postmortem for break-glass grant {grant['id']}",
            description=(
                f"Review emergency access granted to {payload.target_member_id}. "
                f"Reason: {payload.reason}"
            ),
            dedupe_key=f"break-glass:{grant['id']}:postmortem",
            priority="p1",
            risk_tier=3,
            inputs={
                "break_glass_grant_id": grant["id"],
                "target_member_id": payload.target_member_id,
                "scope": payload.scope,
            },
            required_artifacts=["break_glass_postmortem"],
        )
        grant = break_glass.attach_postmortem_task(
            connection,
            grant["id"],
            task["task_id"],
        )
        return break_glass_grant_summary_from_service(grant)


@app.post(
    "/v1/break-glass/grants/{grant_id}/revoke",
    response_model=BreakGlassGrantSummary,
)
def revoke_break_glass_grant(
    grant_id: str,
    payload: BreakGlassGrantRevokeRequest,
) -> BreakGlassGrantSummary:
    with begin(engine) as connection:
        try:
            existing = break_glass.get_grant(connection, grant_id)
        except BreakGlassGrantNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        require_project_actor(
            connection,
            existing["project_id"],
            payload.actor,
            "member_manage",
        )
        grant = break_glass.revoke_grant(
            connection,
            grant_id,
            actor=payload.actor,
            reason=payload.reason,
        )
        return break_glass_grant_summary_from_service(grant)


@app.get("/v1/reports/summary")
def report_summary(project_id: str):
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    with begin(engine) as connection:
        return reports.build_summary(connection, project_id)


@app.get("/v1/agent-runs")
def list_agent_runs(project_id: str, limit: int = 50):
    """Agent tool-calling transcripts (which read tools the agent invoked to
    ground each plan), surfaced for the Run view."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, task_id, created_at,
                       content->'agent_transcript' AS transcript,
                       content->>'tool_calls' AS tool_calls,
                       content->>'cost_usd_micros' AS cost_usd_micros,
                       content->>'output_text' AS output_text,
                       content->>'agent_run_id' AS agent_run_id,
                       content->'usage'->>'total_tokens' AS total_tokens,
                       content->'usage'->>'model' AS model
                FROM artifacts
                WHERE project_id = :p AND artifact_type = 'plan_draft'
                  AND content ? 'agent_transcript'
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"p": project_id, "limit": max(1, min(limit, 200))},
        ).mappings().all()
        # Join the raw LLM conversation (persisted in agent_runs.messages but never
        # otherwise surfaced) so the UI can show the full agent<->LLM back-and-forth.
        run_ids = [r["agent_run_id"] for r in rows if r["agent_run_id"]]
        convo_by_run: dict[str, list] = {}
        status_by_run: dict[str, str] = {}
        if run_ids:
            cr = connection.execute(
                text("SELECT id, status, messages FROM agent_runs WHERE id = ANY(:ids)"),
                {"ids": run_ids},
            ).mappings().all()
            for run in cr:
                msgs = run["messages"]
                if isinstance(msgs, str):
                    try:
                        msgs = json.loads(msgs)
                    except Exception:
                        msgs = []
                convo_by_run[run["id"]] = conversation_from_messages(msgs or [])
                status_by_run[run["id"]] = run["status"]
        return {
            "project_id": project_id,
            "items": [
                {
                    "artifact_id": r["id"],
                    "task_id": r["task_id"],
                    "created_at": r["created_at"].isoformat()
                    if hasattr(r["created_at"], "isoformat")
                    else r["created_at"],
                    "model": r["model"],
                    "tool_calls": int(r["tool_calls"] or 0),
                    "cost_usd_micros": int(r["cost_usd_micros"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "transcript": r["transcript"] or [],
                    "output_text": r["output_text"],
                    "status": status_by_run.get(r["agent_run_id"]) if r["agent_run_id"] else None,
                    "conversation": convo_by_run.get(r["agent_run_id"], []) if r["agent_run_id"] else [],
                }
                for r in rows
            ],
        }


def _artifact_output_preview(content) -> str:
    """Build a human-readable inline preview of an artifact's content for the
    Deliverables viewer. Prefers a natural-language field; otherwise pretty-prints
    the structured content with heavy/noisy keys removed. Size-capped."""
    if content is None:
        return ""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return content[:6000]
    if not isinstance(content, dict):
        return str(content)[:6000]
    for key in ("output_text", "summary", "narrative", "brief", "text", "body"):
        val = content.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:6000]
    drop = {"agent_transcript", "messages", "raw", "agent_module"}
    slim = {k: v for k, v in content.items() if k not in drop}
    try:
        body = json.dumps(slim, indent=2, default=str, ensure_ascii=False)
    except Exception:
        body = str(slim)
    return body[:6000] + ("\n… (truncated)" if len(body) > 6000 else "")


@app.get("/v1/outputs")
def list_task_outputs(project_id: str):
    """Unified, linkable 'deliverables' for every task — aggregates the actual
    outputs a task produced from the three places they land: artifacts
    (design docs, test reports, diffs), decisions (GitHub PR urls), and child
    tasks (incidents / follow-ups). One item per deliverable, tagged by task_id."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")

    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v

    with begin(engine) as connection:
        items: list[dict] = []
        # 1) Artifacts (design docs, test reports, diffs, review packets, ...)
        arts = connection.execute(
            text(
                """
                SELECT id, task_id, artifact_type, title, uri, content, created_at
                FROM artifacts
                WHERE project_id = :p AND task_id IS NOT NULL
                ORDER BY created_at DESC LIMIT 500
                """
            ),
            {"p": project_id},
        ).mappings().all()
        for a in arts:
            # artifacts.uri is a local filesystem path, not a browsable URL — only
            # surface it as a link when it is an actual http(s) URL. Otherwise the
            # content is shown inline via `preview`.
            uri = a["uri"]
            http_url = uri if isinstance(uri, str) and uri.startswith("http") else None
            content = a["content"]
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except Exception:
                    content = {}
            items.append(
                {
                    "task_id": a["task_id"],
                    "kind": a["artifact_type"],
                    "title": a["title"],
                    "link_kind": "artifact",
                    "artifact_id": a["id"],
                    "url": http_url,
                    "preview": _artifact_output_preview(a["content"]),
                    "created_at": _iso(a["created_at"]),
                }
            )
            # a published document carries the GitHub PR it landed in — surface it
            # as a clickable external deliverable too.
            pr_url = content.get("pull_request_url") if isinstance(content, dict) else None
            if pr_url:
                num = content.get("pull_request_number")
                items.append(
                    {
                        "task_id": a["task_id"],
                        "kind": "pull_request",
                        "title": f"GitHub PR #{num}" if num else "GitHub pull request",
                        "link_kind": "external",
                        "url": pr_url,
                        "created_at": _iso(a["created_at"]),
                    }
                )
        # 2) GitHub PRs (and other external refs) carried on decisions
        prs = connection.execute(
            text(
                """
                SELECT id, task_id,
                       recommendation->>'pull_request_url' AS url,
                       recommendation->>'pull_request_number' AS number,
                       created_at
                FROM decisions
                WHERE project_id = :p AND task_id IS NOT NULL
                  AND recommendation ? 'pull_request_url'
                ORDER BY created_at DESC LIMIT 500
                """
            ),
            {"p": project_id},
        ).mappings().all()
        seen_urls: set = set()
        for d in prs:
            if not d["url"] or d["url"] in seen_urls:
                continue
            seen_urls.add(d["url"])
            num = d["number"]
            items.append(
                {
                    "task_id": d["task_id"],
                    "kind": "pull_request",
                    "title": f"GitHub PR #{num}" if num else "GitHub pull request",
                    "link_kind": "external",
                    "url": d["url"],
                    "created_at": _iso(d["created_at"]),
                }
            )
        # 3) Child tasks (incidents / follow-ups) spawned by this task
        children = connection.execute(
            text(
                """
                SELECT id, parent_task_id, title, status, created_at
                FROM tasks
                WHERE project_id = :p AND parent_task_id IS NOT NULL
                ORDER BY created_at DESC LIMIT 500
                """
            ),
            {"p": project_id},
        ).mappings().all()
        for c in children:
            is_incident = (c["title"] or "").upper().startswith("INCIDENT")
            items.append(
                {
                    "task_id": c["parent_task_id"],
                    "kind": "incident" if is_incident else "follow_up",
                    "title": c["title"],
                    "link_kind": "task",
                    "ref_task_id": c["id"],
                    "status": c["status"],
                    "created_at": _iso(c["created_at"]),
                }
            )
        return {"project_id": project_id, "items": items}


@app.post("/v1/agent-runs/{run_id}/resume")
def resume_agent_run_endpoint(run_id: str):
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    with begin(engine) as connection:
        return software_factory.resume_plan_run(connection, run_id)


@app.get(
    "/v1/event-subscriptions",
    response_model=EventSubscriptionListResponse,
)
def list_event_subscription_routes(
    project_id: str, enabled_only: bool = False
) -> EventSubscriptionListResponse:
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    with begin(engine) as connection:
        return EventSubscriptionListResponse(
            project_id=project_id,
            items=list_event_subscriptions(
                connection, project_id, enabled_only=enabled_only
            ),
        )


@app.post(
    "/v1/event-subscriptions",
    response_model=EventSubscriptionSummary,
    status_code=201,
)
def create_event_subscription_route(
    payload: EventSubscriptionCreateRequest,
) -> EventSubscriptionSummary:
    with begin(engine) as connection:
        try:
            result = create_event_subscription(
                connection,
                payload.project_id,
                payload.event_type,
                payload.sink_kind,
                payload.sink_ref,
                data_boundary=payload.data_boundary,
                enabled=payload.enabled,
                config=payload.config,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return EventSubscriptionSummary(**result)


@app.patch(
    "/v1/event-subscriptions/{subscription_id}",
    response_model=EventSubscriptionSummary,
)
def update_event_subscription_route(
    subscription_id: str,
    payload: EventSubscriptionUpdateRequest,
) -> EventSubscriptionSummary:
    with begin(engine) as connection:
        try:
            result = update_event_subscription(
                connection,
                payload.project_id,
                subscription_id,
                event_type=payload.event_type,
                sink_kind=payload.sink_kind,
                sink_ref=payload.sink_ref,
                data_boundary=payload.data_boundary,
                enabled=payload.enabled,
                config=payload.config,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if result is None:
            raise HTTPException(status_code=404, detail="Event subscription not found.")
        return EventSubscriptionSummary(**result)


@app.delete("/v1/event-subscriptions/{subscription_id}", status_code=204)
def delete_event_subscription_route(
    subscription_id: str,
    project_id: str,
    actor: str = "system",
):
    with begin(engine) as connection:
        deleted = delete_event_subscription(
            connection,
            project_id,
            subscription_id,
            actor=actor,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Event subscription not found.")
    return Response(status_code=204)


@app.get("/v1/webhook-endpoints", response_model=WebhookEndpointListResponse)
def list_webhook_endpoints(project_id: str) -> WebhookEndpointListResponse:
    with begin(engine) as connection:
        return WebhookEndpointListResponse(
            project_id=project_id,
            items=webhooks.list_endpoints(connection, project_id),
        )


@app.post(
    "/v1/webhook-endpoints",
    response_model=WebhookEndpointSummary,
    status_code=201,
)
def create_webhook_endpoint(
    payload: WebhookEndpointCreateRequest,
) -> WebhookEndpointSummary:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "comms_manage",
            )
            item = webhooks.create_endpoint(
                connection,
                payload.project_id,
                payload.url,
                payload.secret_ref,
                description=payload.description,
                data_boundary=payload.data_boundary,
                enabled=payload.enabled,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return WebhookEndpointSummary(**item)


@app.patch(
    "/v1/webhook-endpoints/{endpoint_id}",
    response_model=WebhookEndpointSummary,
)
def update_webhook_endpoint(
    endpoint_id: str,
    payload: WebhookEndpointUpdateRequest,
) -> WebhookEndpointSummary:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "comms_manage",
            )
            item = webhooks.update_endpoint(
                connection,
                payload.project_id,
                endpoint_id,
                url=payload.url,
                secret_ref=payload.secret_ref,
                description=payload.description,
                data_boundary=payload.data_boundary,
                enabled=payload.enabled,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if item is None:
            raise HTTPException(status_code=404, detail="Webhook endpoint not found.")
        return WebhookEndpointSummary(**item)


@app.delete("/v1/webhook-endpoints/{endpoint_id}", status_code=204)
def delete_webhook_endpoint(
    endpoint_id: str,
    project_id: str,
    actor: str = "system",
):
    with begin(engine) as connection:
        require_project_actor(connection, project_id, actor, "comms_manage")
        if not webhooks.delete_endpoint(
            connection, project_id, endpoint_id, actor=actor
        ):
            raise HTTPException(status_code=404, detail="Webhook endpoint not found.")
    return Response(status_code=204)


@app.post(
    "/v1/webhook-endpoints/{endpoint_id}/deliver",
    response_model=WebhookDeliverySummary,
    status_code=201,
)
def queue_webhook_delivery(
    endpoint_id: str,
    payload: WebhookDeliveryCreateRequest,
) -> WebhookDeliverySummary:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "comms_manage",
            )
            item = webhooks.enqueue_delivery(
                connection,
                payload.project_id,
                endpoint_id,
                payload.event_type,
                payload.payload,
                payload.dedupe_key,
                data_boundary=payload.data_boundary,
                priority=payload.priority,
                max_attempts=payload.max_attempts,
                actor=payload.actor,
            )
        except WebhookEndpointNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return WebhookDeliverySummary(**item)


@app.get("/v1/webhook-deliveries", response_model=WebhookDeliveryListResponse)
def list_webhook_deliveries(
    project_id: str, limit: int = 100
) -> WebhookDeliveryListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    with begin(engine) as connection:
        return WebhookDeliveryListResponse(
            project_id=project_id,
            items=webhooks.list_deliveries(connection, project_id, limit),
        )


@app.post(
    "/v1/webhook-deliveries/{delivery_id}/deliver",
    response_model=WebhookDeliverySummary,
)
def execute_webhook_delivery(
    delivery_id: str,
    payload: WebhookDeliveryExecuteRequest,
) -> WebhookDeliverySummary:
    with begin(engine) as connection:
        try:
            item = webhooks.deliver(
                connection,
                payload.project_id,
                delivery_id,
                payload.created_by_job_id,
                payload.worker_id,
                payload.lease_token,
            )
        except WebhookDeliveryNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return WebhookDeliverySummary(**item)


@app.post(
    "/v1/webhooks/inbound/{source}",
    response_model=InboundWebhookResponse,
    status_code=202,
)
async def receive_inbound_webhook(
    source: str,
    request: Request,
) -> InboundWebhookResponse:
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    with begin(engine) as connection:
        result = inbound_webhooks.ingest(
            connection,
            source,
            raw_body,
            headers,
        )
    if result["status"] == "rejected":
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    if result["status"] == "not_configured":
        raise HTTPException(status_code=503, detail="Webhook source is not configured.")
    if result["status"] == "invalid":
        raise HTTPException(status_code=400, detail="Invalid webhook payload.")
    return InboundWebhookResponse(**result)


@app.post("/v1/webhooks/github", status_code=202)
async def receive_github_webhook(request: Request):
    """Compatibility GitHub ingress with the community event allowlist."""
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    with begin(engine) as connection:
        result = inbound_webhooks.ingest(
            connection,
            "github",
            raw_body,
            headers,
            github_community_only=True,
        )
    if result["status"] == "rejected":
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    if result["status"] == "not_configured":
        raise HTTPException(status_code=503, detail="Webhook source is not configured.")
    if result["status"] == "invalid":
        raise HTTPException(status_code=400, detail="Invalid webhook payload.")
    if result["status"] in {"unrouted", "ignored"}:
        return Response(status_code=204)
    return JSONResponse(
        status_code=202,
        content=InboundWebhookResponse(**result).model_dump(mode="json"),
    )


@app.post("/v1/webhooks/plane", status_code=202)
async def receive_plane_webhook(request: Request):
    """Plane issue webhook → auto code-task (Phase 1). Auth-exempt but X-Plane-Signature verified;
    a boss:coding/boss:bug issue in a watched project auto-starts a gated code task. See
    docs/infra/BOSS_PLANE_INTEGRATION_SPEC.md."""
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    with begin(engine) as connection:
        result = plane_webhooks.ingest(connection, raw_body, headers, jobs)
    if result["status"] == "rejected":
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    if result["status"] == "not_configured":
        raise HTTPException(status_code=503, detail="Plane integration is not configured.")
    if result["status"] == "invalid":
        raise HTTPException(status_code=400, detail="Invalid webhook payload.")
    if result["status"] == "ignored":
        # A valid, signed delivery we deliberately don't act on (wrong label/project/action).
        # 200 so Plane marks the delivery successful and doesn't retry.
        return JSONResponse(status_code=200, content=result)
    # accepted | duplicate
    return JSONResponse(status_code=202, content=result)


@app.post("/v1/integrations/plane/sync")
def plane_sync_endpoint(payload: dict):
    """Plane status sync-back (Phase 2 #115 / Phase 4 #117): mirror a code-task lifecycle event onto
    its Plane ticket — PATCH the state + comment (PR link, failure, merged→Done). payload:
    {run_id, event}. `started`/`pr_opened`/`completed_no_pr` auto-fire in the pipeline; this drives the
    rest (`code_task.failed`, `code_task.gate_overridden`, `software_factory.pull_request_merged`) and
    backfill. No-op for non-Plane runs."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    run_id = payload.get("run_id")
    event = payload.get("event")
    if not run_id or not event:
        raise HTTPException(status_code=422, detail="run_id and event are required.")
    from .services import plane_sync
    return plane_sync.on_event(engine, settings, run_id, event)


@app.post("/v1/github/community/jobs/{job_id}/execute")
def execute_github_community_job(
    job_id: str, payload: GitHubCommunityJobExecuteRequest
):
    with begin(engine) as connection:
        try:
            return github_community.execute_job(
                connection,
                job_id=job_id,
                project_id=payload.project_id,
                worker_id=payload.worker_id,
                lease_token=payload.lease_token,
            )
        except LeaseNotFoundError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/v1/mcp/servers")
def list_mcp(project_id: str):
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    with begin(engine) as connection:
        return {"project_id": project_id, "items": list_mcp_servers(connection, project_id)}


@app.post("/v1/mcp/servers", status_code=201)
def register_mcp(payload: McpServerRegisterRequest):
    with begin(engine) as connection:
        try:
            return register_mcp_server(
                connection,
                payload.project_id,
                payload.name,
                payload.url,
                transport=payload.transport,
                risk_tier=payload.risk_tier,
                data_boundary=payload.data_boundary,
                description=payload.description,
                publisher=payload.publisher,
                provenance=payload.provenance,
                tool_risk_overrides=payload.tool_risk_overrides,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/v1/mcp/servers/import", status_code=201)
def import_mcp_bundle(payload: CapabilityBundleImportRequest) -> dict:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection, payload.project_id, payload.actor, "pack_manage"
            )
            return capability_bundles.import_mcp(
                connection,
                payload.project_id,
                payload.bundle,
                allow_untrusted=payload.allow_untrusted,
                enabled=payload.enabled,
                actor=payload.actor,
            )
        except CapabilityBundleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/v1/mcp/servers/{server_id}/export")
def export_mcp_bundle(server_id: str, project_id: str, actor: str = "ceo"):
    with begin(engine) as connection:
        try:
            require_project_actor(connection, project_id, actor, "pack_manage")
            content = capability_bundles.export_mcp(
                connection, project_id, server_id
            )
        except CapabilityBundleError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(content=content, media_type="application/vnd.boss.capability+json")


@app.patch("/v1/mcp/servers/{server_id}")
def toggle_mcp(server_id: str, project_id: str, enabled: bool):
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    with begin(engine) as connection:
        row = set_mcp_enabled(connection, project_id, server_id, enabled, actor="executive_console")
        if row is None:
            raise HTTPException(status_code=404, detail="MCP server not found.")
        return row


@app.delete("/v1/mcp/servers/{server_id}", status_code=204)
def remove_mcp(server_id: str, project_id: str):
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    with begin(engine) as connection:
        ok = delete_mcp_server(connection, project_id, server_id, actor="executive_console")
        if not ok:
            raise HTTPException(status_code=404, detail="MCP server not found.")
    return Response(status_code=204)


@app.post("/v1/meetings/prd", response_model=MeetingProposeResponse, status_code=201)
def convene_prd_meeting(payload: MeetingProposeRequest):
    """Convene a multi-agent PRD meeting: a panel of role-agents debates, then
    synthesizes, then drafts — gated by a human approval before publish."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    with begin(engine) as connection:
        # Attach to an existing task if one was supplied, else create one.
        if payload.task_id:
            exists = connection.execute(
                text("SELECT id FROM tasks WHERE id = :t AND project_id = :p"),
                {"t": payload.task_id, "p": payload.project_id},
            ).mappings().first()
            if not exists:
                raise HTTPException(status_code=404, detail="task_id not found in project.")
            task_id = payload.task_id
        else:
            task = intake.create_task(
                connection,
                project_id=payload.project_id,
                title=f"PRD meeting: {payload.directive[:90]}",
                description=payload.directive,
                dedupe_key=make_id("mtg"),
                priority="p1",
                risk_tier=2,
                inputs={"directive": payload.directive, "url": payload.url, "constraints": payload.constraints},
                required_artifacts=["meeting_minutes"],
                data_boundary=str(payload.constraints.get("data_boundary") or "internal"),
            )
            task_id = task["task_id"]
        reservation = budget.reserve(
            connection, payload.project_id, "model",
            settings.plan_budget_usd_micros * 4,  # a panel runs ~10 model calls
            "Multi-agent PRD meeting.", 120,
        )
        reservation_id = reservation.get("reservation_id") if reservation.get("status") == "reserved" else None
        # Start a DURABLE meeting run (one phase per worker step → crash-resumable).
        from .services.meeting_service import start_meeting_run
        run_id = start_meeting_run(
            connection,
            project_id=payload.project_id, task_id=task_id,
            directive=payload.directive, url=payload.url,
            constraints=payload.constraints, role_keys=payload.roles,
            finalize_ctx={"reservation_id": reservation_id},
        )
        job = jobs.create_job(
            connection, project_id=payload.project_id, task_id=task_id,
            kind="meeting.step", priority=48, payload={"run_id": run_id},
        )
        return MeetingProposeResponse(status="accepted", task_id=task_id, job_id=job["id"])


@app.get("/v1/meeting-roles")
def list_meeting_roles(project_id: str):
    """The org's AI role-agent catalog (the meeting template) — read from the DB,
    seeded from the baseline on first use, editable per organization."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.meeting_service import list_project_roles
    with begin(engine) as connection:
        roles = list_project_roles(connection, project_id)
    return {
        "roles": [{**r, "default": r["is_default"]} for r in roles],
        "default_role_keys": [r["key"] for r in roles if r["is_default"]],
    }


@app.post("/v1/meeting-roles", status_code=201)
def upsert_meeting_role_endpoint(payload: dict):
    """Add or update a role-agent for an org's meeting template."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    project_id = str(payload.get("project_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    lens = str(payload.get("lens") or "").strip()
    if not project_id or not title or not lens:
        raise HTTPException(status_code=422, detail="project_id, title and lens are required.")
    from .services.meeting_service import upsert_meeting_role
    with begin(engine) as connection:
        return upsert_meeting_role(
            connection, project_id,
            key=payload.get("key") or title, title=title, lens=lens,
            risk_tier=int(payload.get("risk_tier") or 1),
            is_default=bool(payload.get("is_default")),
            enabled=bool(payload.get("enabled", True)),
        )


@app.patch("/v1/meeting-roles/{role_key}")
def toggle_meeting_role_endpoint(role_key: str, project_id: str, enabled: bool):
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.meeting_service import set_meeting_role_enabled
    with begin(engine) as connection:
        if not set_meeting_role_enabled(connection, project_id, role_key, enabled):
            raise HTTPException(status_code=404, detail="role not found")
    return {"role_key": role_key, "enabled": enabled}


@app.delete("/v1/meeting-roles/{role_key}", status_code=204)
def delete_meeting_role_endpoint(role_key: str, project_id: str):
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.meeting_service import delete_meeting_role
    with begin(engine) as connection:
        if not delete_meeting_role(connection, project_id, role_key):
            raise HTTPException(status_code=404, detail="role not found")
    return Response(status_code=204)


@app.get("/v1/meeting-rooms")
def list_meeting_rooms(project_id: str, limit: int = 25):
    """Full meeting records for the Meeting Room: participants, each round's
    contributions, the debate, the synthesis, the drafted PRD, and the PR it
    landed in."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")

    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v

    from .services.meeting_service import resolve_roles, load_role_map, FACILITATOR_TITLE

    def _pr_for_task(connection, tid):
        if not tid:
            return None, None
        pub = connection.execute(
            text("SELECT content FROM artifacts WHERE task_id = :t AND artifact_type = 'published_document' "
                 "ORDER BY created_at DESC LIMIT 1"),
            {"t": tid},
        ).mappings().first()
        if not pub:
            return None, None
        pc = pub["content"] or {}
        if isinstance(pc, str):
            try:
                pc = json.loads(pc)
            except Exception:
                pc = {}
        return pc.get("pull_request_url"), pc.get("pull_request_number")

    def _task_info(connection, tid):
        if not tid:
            return None, None
        tr = connection.execute(text("SELECT title, status FROM tasks WHERE id = :t"), {"t": tid}).mappings().first()
        return (tr["title"], tr["status"]) if tr else (None, None)

    with begin(engine) as connection:
        items = []
        covered_tasks: set = set()
        # 1) Durable meeting_runs — live phase/status + per-round state.
        runs = connection.execute(
            text("""SELECT id, task_id, status, phase, directive, url, role_keys, state, usage, created_at, updated_at
                    FROM meeting_runs WHERE project_id = :p ORDER BY updated_at DESC LIMIT :limit"""),
            {"p": project_id, "limit": max(1, min(limit, 100))},
        ).mappings().all()
        for r in runs:

            def _j(v):
                if isinstance(v, str):
                    try:
                        return json.loads(v)
                    except Exception:
                        return {}
                return v
            state = _j(r["state"]) or {}
            usage = _j(r["usage"]) or {}
            personas = resolve_roles(_j(r["role_keys"]) or [], load_role_map(connection, project_id))
            if r["task_id"]:
                covered_tasks.add(r["task_id"])
            pr_url, pr_num = _pr_for_task(connection, r["task_id"])
            t_title, t_status = _task_info(connection, r["task_id"])
            research = state.get("research") or {}
            items.append({
                "id": r["id"], "task_id": r["task_id"], "task_title": t_title, "task_status": t_status,
                "role_keys": [p["key"] for p in personas],
                "title": f"PRD meeting: {r['directive']}",
                "purpose": "Cross-functional PRD review and drafting",
                "status": r["status"], "phase": r["phase"],
                "participants": [p["title"] for p in personas] + [FACILITATOR_TITLE],
                "created_at": _iso(r["created_at"]), "completed_at": _iso(r["updated_at"]) if r["status"] == "completed" else None,
                "directive": r["directive"], "url": r["url"],
                "model": usage.get("model"), "cost_usd_micros": usage.get("cost_usd_micros"),
                "research_summary": {"sitemap": research.get("sitemap", []), "pages_studied": len(research.get("pages", []))},
                "round1": state.get("round1") or {}, "round2": state.get("round2") or {},
                "synthesis": state.get("synthesis"), "prd_markdown": state.get("prd_markdown"),
                "pull_request_url": pr_url, "pull_request_number": pr_num,
            })

        # 2) Legacy meetings table (pre-durable) — only those not covered by a run.
        rows = connection.execute(
            text(
                """
                SELECT id, title, purpose, status, participants, minutes_artifact_id,
                       completed_at, created_at
                FROM meetings WHERE project_id = :p
                ORDER BY created_at DESC LIMIT :limit
                """
            ),
            {"p": project_id, "limit": max(1, min(limit, 100))},
        ).mappings().all()

        for m in rows:
            content: dict = {}
            task_id = None
            if m["minutes_artifact_id"]:
                art = connection.execute(
                    text("SELECT task_id, content FROM artifacts WHERE id = :a"),
                    {"a": m["minutes_artifact_id"]},
                ).mappings().first()
                if art:
                    task_id = art["task_id"]
                    content = art["content"] or {}
                    if isinstance(content, str):
                        try:
                            content = json.loads(content)
                        except Exception:
                            content = {}
            if task_id and task_id in covered_tasks:
                continue  # a durable run already represents this meeting
            pr_url = None
            pr_num = None
            task_title = None
            task_status = None
            if task_id:
                tr = connection.execute(
                    text("SELECT title, status FROM tasks WHERE id = :t"),
                    {"t": task_id},
                ).mappings().first()
                if tr:
                    task_title = tr["title"]
                    task_status = tr["status"]
                pub = connection.execute(
                    text(
                        "SELECT content FROM artifacts WHERE task_id = :t AND artifact_type = 'published_document' "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"t": task_id},
                ).mappings().first()
                if pub:
                    pc = pub["content"] or {}
                    if isinstance(pc, str):
                        try:
                            pc = json.loads(pc)
                        except Exception:
                            pc = {}
                    pr_url = pc.get("pull_request_url")
                    pr_num = pc.get("pull_request_number")
            items.append(
                {
                    "id": m["id"],
                    "task_id": task_id,
                    "task_title": task_title,
                    "task_status": task_status,
                    "role_keys": content.get("role_keys") or [],
                    "title": m["title"],
                    "purpose": m["purpose"],
                    "status": m["status"],
                    "phase": "done",
                    "participants": m["participants"] or [],
                    "created_at": _iso(m["created_at"]),
                    "completed_at": _iso(m["completed_at"]),
                    "directive": content.get("directive"),
                    "url": content.get("url"),
                    "model": content.get("model"),
                    "cost_usd_micros": content.get("cost_usd_micros"),
                    "research_summary": content.get("research_summary") or {},
                    "round1": content.get("round1_perspectives") or {},
                    "round2": content.get("round2_debate") or {},
                    "synthesis": content.get("synthesis"),
                    "prd_markdown": content.get("prd_markdown"),
                    "pull_request_url": pr_url,
                    "pull_request_number": pr_num,
                }
            )
        return {"project_id": project_id, "items": items}


@app.post("/v1/meetings/step")
def run_meeting_step_endpoint(payload: dict):
    """Worker-invoked: advance a durable meeting by exactly ONE phase. Each step
    persists its result and enqueues the next step, so a crash/restart resumes
    from the last completed phase instead of re-running the whole meeting."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.meeting_service import advance_meeting_run
    return advance_meeting_run(engine, settings, jobs, payload["run_id"])


@app.post("/v1/code-tasks")
def start_code_task_endpoint(payload: dict):
    """Convene a durable coding task: clone -> agent edits + tests -> gated PR.
    Returns the run_id; a `code.task` worker picks it up and runs the pipeline."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    project_id = payload.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required.")
    from .services.code_task_service import start_code_task
    with begin(engine) as c:
        run_id = start_code_task(
            c, project_id=project_id, task_id=payload.get("task_id"),
            title=payload.get("title") or "Code task",
            instructions=payload.get("instructions") or "",
            acceptance_criteria=payload.get("acceptance_criteria") or [],
            test_commands=payload.get("test_commands") or [],
            owner=payload.get("owner") or settings.target_github_owner,
            repo=payload.get("repo") or settings.target_github_repo,
            base_branch=payload.get("base_branch") or "main",
            finalize_ctx=payload.get("finalize_ctx") or {},
            jobs=jobs,
            profile=payload.get("profile"),
            risk_tier=payload.get("risk_tier"),
            model_config=payload.get("model_config"),
            open_pr=payload.get("open_pr"),
            repair_retries=payload.get("repair_retries"),
            review_panel_size=payload.get("review_panel_size"),
        )
    return {"status": "queued", "run_id": run_id}


@app.post("/v1/prd/decompose", status_code=201)
def decompose_prd_endpoint(payload: dict):
    """Autonomous PRD -> scoped coding tasks -> build. BOSS reads a PRD (or a feature directive),
    decomposes it into a SMALL ordered set of composable coding tasks, and runs them as ONE branch
    lineage (scaffold -> features -> wire-up) — each task auto-starts when the prior lands. Body:
    {project_id, prd | directive, owner?, repo?, base_branch?, task_id?, profile?, risk_tier?,
    max_tasks?, plan_only?}. plan_only=true returns the decomposition WITHOUT starting the build."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    project_id = payload.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required.")
    prd = (payload.get("prd") or payload.get("directive") or "").strip()
    if not prd:
        raise HTTPException(status_code=422, detail="prd (or directive) is required.")
    from .services.prd_decomposition import decompose_prd_into_tasks, start_decomposed_build
    max_tasks = int(payload.get("max_tasks") or 6)
    if payload.get("plan_only"):
        tasks = decompose_prd_into_tasks(settings, prd, max_tasks=max_tasks)
        if not tasks:
            raise HTTPException(status_code=422, detail="could not decompose the PRD into tasks.")
        return {"plan_only": True, "total": len(tasks), "tasks": tasks}
    result = start_decomposed_build(
        engine, settings, jobs, project_id=project_id, task_id=payload.get("task_id"),
        prd_or_directive=prd,
        owner=payload.get("owner") or settings.target_github_owner,
        repo=payload.get("repo") or settings.target_github_repo,
        base_branch=payload.get("base_branch") or "main",
        profile=payload.get("profile"), risk_tier=payload.get("risk_tier"), max_tasks=max_tasks)
    if not result.get("started"):
        raise HTTPException(status_code=422, detail=result.get("reason") or "decomposition failed.")
    return result


@app.post("/v1/web-app", status_code=201)
def build_web_app_endpoint(payload: dict):
    """SINGLE-TASK web-app build — the robust path for a website. Seeds a deterministic
    Vite+React+TS+Tailwind scaffold (0 agent turns), then ONE governed task builds the whole app on
    top (fits the guest turn cap; no fragile decomposition chain). Body: {project_id, directive,
    owner?, repo?, task_id?, review?}. review defaults to false so the FIRST pass just works."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    project_id = payload.get("project_id")
    directive = (payload.get("directive") or payload.get("prd") or "").strip()
    if not project_id or not directive:
        raise HTTPException(status_code=422, detail="project_id and directive are required.")
    from .services.prd_decomposition import start_web_app_build
    return start_web_app_build(
        engine, settings, jobs, project_id=project_id, task_id=payload.get("task_id"),
        directive=directive,
        owner=payload.get("owner") or settings.target_github_owner,
        repo=payload.get("repo") or settings.target_github_repo,
        review=bool(payload.get("review", False)))


@app.get("/v1/pipeline-profiles")
def list_pipeline_profiles_endpoint(project_id: str):
    """List the cost/rigor profiles available to a project: built-in presets plus any custom
    ones, with the project default marked."""
    from .services import pipeline_profile_service as pps

    with begin(engine) as c:
        items = pps.list_profiles(c, project_id)
        default = pps.default_profile_name(c, project_id, settings)
    return {"project_id": project_id, "default": default, "items": items}


@app.post("/v1/pipeline-profiles", status_code=201)
def create_pipeline_profile_endpoint(payload: dict):
    """Create/update a custom profile (enterprise edition only)."""
    from .services import pipeline_profile_service as pps

    project_id = payload.get("project_id")
    if not project_id or not payload.get("name"):
        raise HTTPException(status_code=422, detail="project_id and name are required.")
    with begin(engine) as c:
        require_project_actor(c, project_id, payload.get("actor", "ceo"), "budget_manage")
        try:
            result = pps.create_custom_profile(
                c, project_id=project_id, name=payload["name"],
                label=payload.get("label"),
                tier_floor=payload.get("tier_floor", 0), tier_cap=payload.get("tier_cap", 3),
                model_routing=payload.get("model") or payload.get("model_routing") or {},
                caching=payload.get("caching", True),
                parallel_checks=payload.get("parallel_checks", True),
                actor=payload.get("actor", "ceo"), settings=settings,
            )
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    return {"status": "created", **result}


@app.post("/v1/pipeline-profiles/default")
def set_pipeline_profile_default_endpoint(payload: dict):
    """Set the project's default profile (a preset or a custom one)."""
    from .services import pipeline_profile_service as pps

    project_id = payload.get("project_id")
    if not project_id or not payload.get("name"):
        raise HTTPException(status_code=422, detail="project_id and name are required.")
    with begin(engine) as c:
        require_project_actor(c, project_id, payload.get("actor", "ceo"), "budget_manage")
        try:
            pps.set_default_profile(c, project_id, payload["name"], payload.get("actor", "ceo"))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    return {"status": "ok", "project_id": project_id, "default": payload["name"].strip().lower()}


@app.post("/v1/code-tasks/step")
def run_code_task_step_endpoint(payload: dict):
    """Worker-invoked: run one full code-task attempt. Safe to re-run after a
    crash — the PR receipt dedupe makes re-opening the PR a no-op."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.code_task_service import run_code_task
    return run_code_task(engine, settings, jobs, payload["run_id"])


@app.get("/v1/code-tasks/{run_id}/activity")
def code_task_activity_endpoint(run_id: str):
    """Per-stage model-call activity for a code task — the internal detail behind
    each phase (model, latency, turns, retries, cost, ok/error). Written per call,
    so it's live: use it to see WHY a phase (e.g. write_tests) is slow."""
    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT stage, engine, model, total_tokens, cost_usd_micros,
                       latency_ms, retries, turns, ok, error, created_at
                FROM llm_calls
                WHERE code_task_run_id = :rid
                ORDER BY created_at, id
                """
            ),
            {"rid": run_id},
        ).mappings().all()
    calls = [
        {
            "stage": r["stage"],
            "engine": r["engine"],
            "model": r["model"],
            "total_tokens": int(r["total_tokens"] or 0),
            "cost_usd_micros": int(r["cost_usd_micros"] or 0),
            "latency_ms": int(r["latency_ms"] or 0),
            "retries": int(r["retries"] or 0),
            "turns": int(r["turns"] or 0),
            "ok": bool(r["ok"]),
            "error": r["error"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return {"run_id": run_id, "calls": calls}


@app.post("/v1/code-tasks/{run_id}/retry")
def retry_code_task_endpoint(run_id: str, payload: Optional[dict] = None):
    """Resume a failed/blocked code task as a new attempt in the same lineage, carrying its prior
    findings forward so the agent fixes the identified issues instead of regenerating from scratch."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.code_task_service import retry_code_task
    try:
        return retry_code_task(engine, settings, jobs, run_id, fix_note=(payload or {}).get("fix_note", ""))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/v1/code-tasks/{run_id}/feedback")
def get_code_task_feedback_endpoint(run_id: str):
    from .services.feedback_agent import get_feedback_thread
    try:
        return get_feedback_thread(engine, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/v1/code-tasks/{run_id}/feedback")
def submit_code_task_feedback_endpoint(run_id: str, payload: dict):
    """Ask/clarify/structure iteration feedback; never dispatch without confirmation."""
    from .services.feedback_agent import submit_feedback
    try:
        return submit_feedback(
            engine, run_id, message=str(payload.get("message") or ""),
            expected=str(payload.get("expected") or ""),
            anchor=str(payload.get("anchor") or ""),
            severity=str(payload.get("severity") or "normal"),
            selected_element=str(payload.get("selected_element") or ""),
            screenshot_url=str(payload.get("screenshot_url") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/v1/feedback-threads/{thread_id}/confirm")
def confirm_code_task_feedback_endpoint(thread_id: str, payload: Optional[dict] = None):
    from .services.feedback_agent import confirm_feedback
    actor = str((payload or {}).get("actor") or "operator")
    with begin(engine) as connection:
        thread = connection.execute(text(
            "SELECT project_id FROM feedback_threads WHERE id=:id"
        ), {"id": thread_id}).mappings().first()
        if thread is None:
            raise HTTPException(status_code=404, detail="feedback thread not found")
        require_project_actor(connection, thread["project_id"], actor, "task_create")
    try:
        return confirm_feedback(
            engine, settings, jobs, thread_id, actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/v1/code-tasks/{run_id}/override")
def override_code_task_endpoint(run_id: str, payload: dict):
    """Human gate override: waive the named failed gate(s) + open the PR from the preserved diff
    (no re-run). Gates not waived still block; the PR keeps its own merge-gate Decision."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.code_task_service import override_code_task
    try:
        return override_code_task(engine, settings, jobs, run_id,
            waive_gates=payload.get("waive_gates") or [],
            actor=payload.get("actor") or "operator", reason=payload.get("reason") or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/v1/code-tasks/{run_id}/preview")
def start_preview_endpoint(run_id: str, payload: Optional[dict] = None):
    """Preview a built app from retained rootfs, Docker, or a managed host process."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    p = payload or {}
    mode = str(p.get("mode", "auto")).lower()
    ttl = int(p.get("ttl_minutes", 60))
    from .services.preview_service import (
        start_preview, start_container_preview, start_rootfs_preview, stop_preview,
    )
    # Replace (don't accumulate): stop any prior running preview for this run so there's one live
    # preview per task — a fresh "Run preview" regenerates it.
    with begin(engine) as _c:
        prior = [r[0] for r in _c.execute(
            text("SELECT id FROM app_previews WHERE run_id=:r AND status='running'"),
            {"r": run_id}).fetchall()]
    for _pid in prior:
        try:
            stop_preview(engine, _pid, settings)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
    # auto: containerize first (isolated), fall back to a host process (handles loopback-binding
    # apps / no-docker). Explicit mode forces one path.
    if mode == "process":
        attempts = [start_preview]
    elif mode == "docker":
        attempts = [start_container_preview]
    elif mode == "rootfs":
        attempts = [start_rootfs_preview]
    else:
        attempts = [start_rootfs_preview, start_container_preview, start_preview]
    last: Optional[dict] = None
    for fn in attempts:
        try:
            res = fn(engine, settings, run_id, ttl_minutes=ttl)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except RuntimeError as exc:
            last = {"status": "failed", "error": str(exc)}
            continue
        if res.get("status") == "running":
            return res
        last = res
    return last or {"status": "failed", "error": "preview could not start"}


@app.get("/v1/previews")
def list_previews_endpoint(project_id: str, limit: int = 50):
    """List app previews for a project (reaps expired ones)."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.preview_service import list_previews
    return {"previews": list_previews(engine, project_id, limit, settings)}


@app.get("/v1/previews/{preview_id}/logs")
def preview_logs_endpoint(preview_id: str, stream: bool = True):
    """Tail classified preview startup logs; SSE clients receive live updates."""
    from .services.preview_service import preview_log_snapshot, stream_preview_logs
    try:
        if not stream:
            return preview_log_snapshot(engine, preview_id)
        # Probe once so a missing id returns 404 before the stream starts.
        preview_log_snapshot(engine, preview_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return StreamingResponse(
        stream_preview_logs(engine, preview_id), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/previews/{preview_id}/feedback")
def submit_preview_feedback_endpoint(preview_id: str, payload: dict):
    """Capability-gated capture from the injected preview overlay (proposal only)."""
    from .services.feedback_agent import submit_preview_feedback
    try:
        return submit_preview_feedback(engine, preview_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/v1/previews/{preview_id}/smoke")
def smoke_preview_endpoint(preview_id: str, payload: Optional[dict] = None):
    """Tier-3 smoke canary: assert the live preview actually works (key routes), record a receipt."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.preview_service import smoke_test
    try:
        return smoke_test(engine, preview_id, (payload or {}).get("checks"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.api_route("/v1/previews/{preview_id}/web", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.api_route("/v1/previews/{preview_id}/web/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def preview_web_proxy(preview_id: str, request: Request, path: str = ""):
    """Browser-facing reverse proxy to a running preview's loopback port — lets a preview be opened
    over the api's tailnet route (the preview's 127.0.0.1 port isn't reachable directly). Auth-exempt
    (gated by the unguessable preview id + TTL) — see require_transport_auth_for_v1."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.preview_service import proxy_preview
    body = await request.body()
    status, ctype, data, headers = proxy_preview(
        engine, preview_id, path, request.method, request.url.query,
        body or None, request.headers.get("content-type"), settings.sandbox_token,
    )
    return Response(content=data, status_code=status, media_type=ctype, headers=headers)


@app.websocket("/v1/previews/{preview_id}/web/")
@app.websocket("/v1/previews/{preview_id}/web/{path:path}")
async def preview_websocket_proxy(websocket: WebSocket, preview_id: str, path: str = ""):
    """Bidirectional WebSocket proxy for SPA/live-preview development servers."""
    await websocket.accept()
    with begin(engine) as connection:
        row = connection.execute(text(
            "SELECT url,status,mode FROM app_previews WHERE id=:id"
        ), {"id": preview_id}).mappings().first()
    if row is None or row["status"] != "running" or not row["url"]:
        await websocket.close(code=4404, reason="preview not running")
        return
    base = str(row["url"]).rstrip("/")
    target = ("wss://" + base[8:] if base.startswith("https://") else
              "ws://" + base[7:] if base.startswith("http://") else base)
    target += "/" + path.lstrip("/")
    if websocket.url.query:
        target += "?" + websocket.url.query
    headers = ({"X-Sandbox-Token": settings.sandbox_token}
               if row["mode"] == "rootfs" and settings.sandbox_token else None)
    try:
        import websockets
        async with websockets.connect(target, additional_headers=headers) as upstream:
            async def client_to_upstream():
                while True:
                    message = await websocket.receive()
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    else:
                        return

            async def upstream_to_client():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except (WebSocketDisconnect, Exception):  # connection closure is reflected to the browser
        try:
            await websocket.close()
        except Exception:
            pass


@app.post("/v1/previews/{preview_id}/stop")
def stop_preview_endpoint(preview_id: str):
    """Tear down a running preview."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.preview_service import stop_preview
    try:
        return stop_preview(engine, preview_id, settings)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/v1/code-tasks")
def list_code_tasks_endpoint(project_id: str, limit: int = 50):
    """List code-task runs for a project (newest first) for the Code Tasks view."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.code_task_service import list_code_task_runs
    with begin(engine) as c:
        return {"code_tasks": list_code_task_runs(c, project_id, limit)}


@app.get("/v1/code-tasks/{run_id}")
def get_code_task_endpoint(run_id: str):
    """Fetch a single code-task run (status, phase, diff summary, tests, PR, decision)."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.code_task_service import load_code_task_run
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="code task not found")
    return run


@app.get("/v1/bakeoff/{batch_id}/report")
def bakeoff_report_endpoint(batch_id: str):
    """Aggregate a VAG bake-off batch (spec §8) server-side: gate pass-rate, retries,
    wall-clock, $/verified-outcome and false-pass per model config, from
    code_task_runs ⋈ quality_gates ⋈ llm_calls. Lets the driver report without
    direct DB access (goldeneye's DATABASE_URL is not exposed to operators)."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.bakeoff import aggregate_batch, render_markdown
    with begin(engine) as c:
        report = aggregate_batch(c, batch_id)
    report["markdown"] = render_markdown(report)
    return report


@app.post("/v1/verifier-meta-eval")
def verifier_meta_eval_endpoint(payload: Optional[dict] = None):
    """VAG #105: meta-eval the verifier. Run the adversarial reviewer over the labeled
    bugged/clean corpus and gate on recall (bugs caught) + false-fail rate (clean code
    wrongly rejected). Payload (all optional): {corpus, dry_run, min_recall, max_fp_rate,
    engine, model}. **dry_run defaults to true** — it replays each item's stub_verdict for a
    zero-cost harness check; dry_run=false runs the REAL reviewer (one paid review call per
    item)."""
    from .services import verifier_meta_eval as vme
    payload = payload or {}
    corpus = payload.get("corpus") or vme.load_corpus()
    # VAG #107: fold in the ratchet corpus (human↔gate disagreements captured from real runs),
    # so the verifier is scored against its own past misses. On by default; skip with a corpus override.
    if payload.get("include_ratchet", True) and not payload.get("corpus"):
        from .services import verifier_ratchet as vr
        with begin(engine) as c:
            corpus = vr.merged_corpus(c, corpus, payload.get("project_id"))
    min_recall = payload.get("min_recall", settings.verifier_meta_min_recall)
    max_fp_rate = payload.get("max_fp_rate", settings.verifier_meta_max_fp_rate)
    if payload.get("dry_run", True):
        verify_fn = vme.stub_verify_fn
    else:
        verify_fn = vme.review_verify_fn(
            settings, engine=payload.get("engine"), model=payload.get("model")
        )
    report = vme.run_meta_eval(
        corpus, verify_fn, min_recall=min_recall, max_fp_rate=max_fp_rate
    )
    report["markdown"] = vme.render_markdown(report)
    report["dry_run"] = bool(payload.get("dry_run", True))
    return report


@app.post("/v1/verifier-ratchet/capture")
def verifier_ratchet_capture_endpoint(payload: dict):
    """VAG #107: capture a code-task run as a labeled verifier-meta-eval corpus item — the
    growing "ratchet" of human↔gate disagreements. Overrides auto-capture as CLEAN; use this to
    record a gate-PASSED run a human REJECTED as BUGGED (the false-pass the verifier missed).
    payload: {run_id, label: 'bugged'|'clean', bug_class?, reason?, files?: [{path,content}]}.
    Files are taken from the run's preserved diff when present, else supply them here."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    run_id = payload.get("run_id")
    label = payload.get("label")
    if not run_id or label not in ("bugged", "clean"):
        raise HTTPException(status_code=422, detail="run_id and label ('bugged'|'clean') are required.")
    from .services import verifier_ratchet as vr
    with begin(engine) as c:
        result = vr.capture_run(
            c, run_id, label=label, source=payload.get("source", "manual"),
            bug_class=payload.get("bug_class"), files=payload.get("files"),
            reason=payload.get("reason"),
        )
    return result


@app.post("/v1/mutation-gate")
def mutation_gate_endpoint(payload: dict):
    """VAG #106: mutation-gate the frozen tests. Plants deterministic AST mutations in `target_file`
    and requires the tests to KILL them — a surviving mutant is a bug-class the suite can't catch.
    payload: {files: {path: content}, target_file, test_command?, min_score?, max_mutants?, timeout?,
    require_property_tests?}. Model-free, but it EXECUTES the provided tests, so call it from a
    trusted eval / sandbox context. Returns the mutation score, gate, and the surviving mutants."""
    from .services import mutation_gate as mg
    files = payload.get("files")
    target = payload.get("target_file")
    if not isinstance(files, dict) or not files or not target:
        raise HTTPException(status_code=422, detail="files (path->content) and target_file are required.")
    return mg.run_mutation_gate(
        files, target,
        payload.get("test_command") or ["python3", "-m", "unittest", "discover", "-v"],
        min_score=payload.get("min_score", settings.mutation_min_score),
        max_mutants=payload.get("max_mutants", settings.mutation_max_mutants),
        timeout=payload.get("timeout", settings.mutation_timeout_seconds),
        require_property_tests=payload.get(
            "require_property_tests", settings.mutation_require_property_tests),
    )


@app.post("/v1/independence-check")
def independence_check_endpoint(payload: Optional[dict] = None):
    """VAG #110: check generator/verifier independence. With `builder` + `verifiers`, assess those;
    with no payload, audit BOSS's CURRENT config (builder vs review engine). payload:
    {builder: {engine, model}, verifiers: [{engine, model}], required_level?: off|model|family|provider}.
    A verifier sharing the builder's model family shares its blind spots — the gate blocks it."""
    from .services import independence as ind
    payload = payload or {}
    level = payload.get("required_level", settings.independence_required_level)
    if not payload.get("builder"):
        return ind.configured_contract(settings, required_level=level)
    b = payload["builder"]
    builder = (b.get("engine"), b.get("model"))
    verifiers = [(v.get("engine"), v.get("model")) for v in payload.get("verifiers", [])]
    verdict = ind.assess_independence(builder, verifiers)
    return {**verdict, "gate": ind.independence_gate(verdict, required_level=level)}


@app.post("/v1/repair-policy")
def repair_policy_endpoint(payload: Optional[dict] = None):
    """VAG #109: budget-variable repair. With `attempt` + `spent_usd_micros`, decide the next repair
    action (stop / retry-cheap / escalate-to-strong); otherwise return the up-front plan (how deep the
    budget allows, when the tier flips). payload: {budget_usd_micros?, spent_usd_micros?, attempt?,
    est_attempt_cost?, max_retries?, escalate_after?, cheap_tier?, strong_tier?, verified_count?}."""
    from .services import repair_policy as rp
    payload = payload or {}
    budget = payload.get("budget_usd_micros", settings.code_task_budget_usd_micros)
    est = payload.get("est_attempt_cost", settings.repair_est_attempt_cost_usd_micros)
    max_retries = payload.get("max_retries", settings.code_repair_max_retries)
    escalate_after = payload.get("escalate_after", settings.repair_escalate_after)
    cheap = payload.get("cheap_tier") or settings.code_agent_low_cost_engine or settings.code_agent_engine
    strong = payload.get("strong_tier") or settings.code_agent_strong_engine or settings.code_review_engine or "strong"
    if "attempt" not in payload:
        return rp.plan_repair(remaining_usd_micros=budget, est_attempt_cost=est,
                              max_retries=max_retries, escalate_after=escalate_after,
                              cheap_tier=cheap, strong_tier=strong)
    decision = rp.decide_repair(
        attempt=int(payload["attempt"]), spent_usd_micros=payload.get("spent_usd_micros", 0),
        budget_usd_micros=budget, est_attempt_cost=est, max_retries=max_retries,
        escalate_after=escalate_after, cheap_tier=cheap, strong_tier=strong,
        strong_attempt_cost=payload.get("strong_attempt_cost"),
        last_gate_passed=payload.get("last_gate_passed", False),
    )
    decision["dollars_per_verified"] = rp.dollars_per_verified(
        payload.get("spent_usd_micros", 0), payload.get("verified_count", 0))
    return decision


@app.post("/v1/scaffold")
def scaffold_endpoint(payload: Optional[dict] = None):
    """APP-1 (#91): framework-aware scaffolding. Returns a deterministic, buildable baseline project
    (Vite+React+TS+Tailwind + shadcn-style primitives + a design-system token layer + web verification
    scripts) that the design role (APP-2) then themes to the domain. payload: {name?, kind?: vite|spa}.
    Templated — no model tokens, same input → same tree."""
    from .services import scaffold_service as sc
    payload = payload or {}
    try:
        return sc.scaffold_project(name=payload.get("name", "app"), kind=payload.get("kind", "vite"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@app.post("/v1/visual-gate")
def visual_gate_endpoint(payload: dict):
    """APP-4 (#94): judge render evidence against the deterministic visual/UX rubric — renders without
    an error overlay, has real (non-placeholder) content, has visual hierarchy, no mobile overflow.
    payload: {evidence: {html, console_errors?, viewport?, route?, screenshot_ref?}}. Returns
    passed|failed|not_applicable + findings. Model-free; a vision-model judge is the follow-on (APP-6)."""
    from .services import visual_gate as vg
    return vg.evaluate_visual((payload or {}).get("evidence"))


@app.post("/v1/design-routing")
def design_routing_endpoint(payload: Optional[dict] = None):
    """APP-6 (#96): hybrid frontier+OSS routing for the design axis. With `step`, resolve its tier +
    engine (design axis → frontier, fill-in → cheap OSS); otherwise return the design-pass budget plan
    (a metered line item capped at N frontier passes). payload: {step?, budget_usd_micros?, overrides?}."""
    from .services import design_routing as dr
    payload = payload or {}
    frontier = payload.get("frontier_engine") or settings.design_frontier_engine \
        or settings.code_agent_strong_engine or settings.code_review_engine or "strong"
    oss = payload.get("oss_engine") or settings.design_oss_engine \
        or settings.code_agent_low_cost_engine or settings.code_agent_engine
    if payload.get("step"):
        step = payload["step"]
        return {"step": step, "tier": dr.resolve_tier(step),
                "engine": dr.resolve_engine(step, frontier_engine=frontier, oss_engine=oss,
                                            default_engine=settings.code_agent_engine,
                                            overrides=payload.get("overrides"))}
    return dr.design_pass_plan(
        payload.get("budget_usd_micros", settings.code_task_budget_usd_micros),
        design_fraction=settings.design_pass_budget_fraction,
        est_design_pass_cost=settings.design_est_pass_cost_usd_micros,
        max_design_passes=settings.design_max_passes)


@app.post("/v1/concurrency-load")
def concurrency_load_endpoint(payload: Optional[dict] = None):
    """#103: drive N concurrent tasks and report throughput / latency / saturation for the Go-services
    concurrency layer. Defaults to a **stub** runner (simulated task, **zero model cost**). payload:
    {n, concurrency, stub_duration_ms?, fail_every?, sweep?: [concurrency levels]}."""
    from .services import concurrency_harness as ch
    payload = payload or {}
    dur = float(payload.get("stub_duration_ms", 20)) / 1000.0
    fail_every = int(payload.get("fail_every", 0))
    n = min(2000, max(0, int(payload.get("n", 100))))          # cap to keep it a smoke, not a DoS
    conc = min(500, max(1, int(payload.get("concurrency", 20))))

    def task_fn(i):
        return ch.stub_task(i, duration_s=dur, fail_every=fail_every)

    if payload.get("sweep"):
        levels = [min(500, max(1, int(c))) for c in payload["sweep"]][:12]
        return ch.sweep(task_fn, levels, n_per_level=min(500, n or 50))
    return ch.run_load(task_fn, n, conc)


# --- Discovery pipeline: research-driven outcome discovery -------------------
@app.post("/v1/discoveries")
def start_discovery_endpoint(payload: dict):
    """Kick off a Discovery run: research a seed (idea/problem/signal) via web search and
    produce a cited dossier + candidate outcomes. Returns run_id; progress polls discovery_runs."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    seed = (payload.get("seed") or payload.get("scope") or "").strip()
    title = (payload.get("title") or seed[:120] or "Discovery").strip()
    if not seed:
        raise HTTPException(status_code=400, detail="seed (the idea/problem to research) is required")
    project_id = payload.get("project_id") or "boss-dogfood"
    from .services.discovery_service import start_discovery
    run_id = start_discovery(
        project_id=project_id, title=title, seed=seed,
        context=(payload.get("context") or "").strip(), settings=settings,
    )
    return {"status": "running", "run_id": run_id}


@app.get("/v1/discoveries")
def list_discoveries_endpoint(project_id: str, limit: int = 50):
    """List discovery runs for a project (newest first) for the Discovery view."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.discovery_service import list_discovery_runs
    with begin(engine) as c:
        return {"discoveries": list_discovery_runs(c, project_id, limit)}


@app.get("/v1/discoveries/{run_id}")
def get_discovery_endpoint(run_id: str):
    """Fetch a single discovery run (status, phase, dossier, candidate outcomes, gate)."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.discovery_service import load_discovery_run
    with begin(engine) as c:
        run = load_discovery_run(c, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="discovery run not found")
    return run


@app.post("/v1/discoveries/{run_id}/promote")
def promote_discovery_outcome_endpoint(run_id: str, payload: dict):
    """Close the loop: turn a chosen candidate outcome into an Initiative + convene a PRD meeting
    seeded with the outcome and the dossier evidence. Returns the meeting task_id."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.discovery_service import load_discovery_run, build_promote_directive
    from .services.meeting_service import start_meeting_run
    idx = int(payload.get("outcome_index") or 0)
    with begin(engine) as connection:
        run = load_discovery_run(connection, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="discovery run not found")
        outcomes = (run.get("state") or {}).get("candidate_outcomes") or []
        if idx < 0 or idx >= len(outcomes):
            raise HTTPException(status_code=400, detail="outcome_index out of range")
        directive, title = build_promote_directive(run, idx)
        project_id = run["project_id"]
        task = intake.create_task(
            connection, project_id=project_id,
            title=title, description=directive, dedupe_key=make_id("mtg"),
            priority="p1", risk_tier=2,
            inputs={"directive": directive, "discovery_run_id": run_id, "outcome_index": idx},
            required_artifacts=["meeting_minutes"], data_boundary="internal",
        )
        task_id = task["task_id"]
        reservation = budget.reserve(
            connection, project_id, "model", settings.plan_budget_usd_micros * 4,
            "PRD meeting promoted from Discovery.", 120,
        )
        reservation_id = reservation.get("reservation_id") if reservation.get("status") == "reserved" else None
        meeting_run_id = start_meeting_run(
            connection, project_id=project_id, task_id=task_id, directive=directive, url=None,
            constraints={}, role_keys=None, finalize_ctx={"reservation_id": reservation_id},
        )
        jobs.create_job(connection, project_id=project_id, task_id=task_id,
                        kind="meeting.step", priority=48, payload={"run_id": meeting_run_id})
        # Link the promotion back onto the discovery run so the UI can show it.
        state = run.get("state") or {}
        promos = state.get("promotions") or []
        promos.append({"outcome_index": idx, "task_id": task_id, "meeting_run_id": meeting_run_id})
        state["promotions"] = promos
        connection.execute(
            text("UPDATE discovery_runs SET state=CAST(:s AS JSONB), updated_at=now() WHERE id=:id"),
            {"s": json.dumps(state), "id": run_id},
        )
        emit_event(connection, project_id, "discovery.promoted", "operator", "discovery_run", run_id,
                   {"task_id": task_id, "outcome_index": idx})
    return {"status": "convened", "task_id": task_id, "meeting_run_id": meeting_run_id}


@app.post("/v1/discoveries/{run_id}/deepen")
def deepen_discovery_endpoint(run_id: str):
    """Spawn a deeper second pass on a discovery's seed: a multi-source parallel scout sweep
    (competitor/pain/demand/timing) for a richer, more-cited dossier. Returns the new run_id."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    from .services.discovery_service import load_discovery_run, start_discovery
    with begin(engine) as c:
        parent = load_discovery_run(c, run_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="discovery run not found")
    new_id = start_discovery(
        project_id=parent["project_id"], title=f"[deep] {parent['title']}"[:300],
        seed=parent["seed"] or "", context=parent["context"] or "",
        settings=settings, deep=True, parent_run_id=run_id,
    )
    return {"status": "running", "run_id": new_id, "deep": True}


# --- Execution plane: governed sandbox visibility + console ------------------
@app.get("/v1/sandboxes")
def list_sandboxes_endpoint():
    """The connected micro-VM host + the inspectable (retained) VMs. A failed or
    incomplete code task leaves its rootfs held ~2h; those show here for root-cause."""
    from .services import sandbox_console
    try:
        result = sandbox_console.list_sandboxes(settings)  # host status
    except Exception as e:  # sandbox service unreachable
        result = {"sandboxes": [], "host": {"connected": False}, "unavailable": str(e)[:200]}
    cards = []
    try:
        with begin(engine) as c:
            rows = c.execute(text(
                "SELECT id, title, project_id, status, spec, state, finalize_ctx, "
                "EXTRACT(EPOCH FROM (now()-updated_at))::int AS age "
                "FROM code_task_runs WHERE status IN ('failed','blocked') "
                "AND finalize_ctx->>'sandbox_task_id' IS NOT NULL "
                "AND updated_at > now() - interval '115 minutes' "
                "ORDER BY updated_at DESC LIMIT 30")).mappings().all()
        for r in rows:
            def _j(v):
                return v if isinstance(v, dict) else (json.loads(v) if v else {})
            spec, state, fctx = _j(r["spec"]), _j(r["state"]), _j(r["finalize_ctx"])
            cards.append({
                "sandbox_id": fctx.get("sandbox_task_id"), "status": "failed",
                "title": r["title"], "tenant_id": r["project_id"],
                "repo": f"{spec.get('owner', '')}/{spec.get('repo', '')}",
                "age_seconds": int(r["age"] or 0), "preserved": False,
                "meta": {"engine": state.get("engine"), "run_id": r["id"],
                         "diff_summary": state.get("diff_summary"),
                         "tests_passed": state.get("tests_passed")},
            })
    except Exception:  # noqa: BLE001
        pass
    result["sandboxes"] = cards
    return result


@app.get("/v1/sandboxes/helpers")
def sandbox_helpers_endpoint():
    """The governed helper-command allowlist (runnable without break-glass)."""
    from .services import sandbox_console
    try:
        return sandbox_console.list_helpers(settings)
    except Exception as e:  # sandbox agent unreachable
        return {"categories": {}, "commands": [], "unavailable": str(e)[:200]}


@app.get("/v1/sandboxes/{sandbox_id}/tree")
def sandbox_tree_endpoint(sandbox_id: str):
    from .services import sandbox_console
    return sandbox_console.get_tree(settings, sandbox_id)


@app.get("/v1/sandboxes/{sandbox_id}/file")
def sandbox_file_endpoint(sandbox_id: str, path: str, actor: str = "operator"):
    """Read a tracked file from a sandbox working tree — audited."""
    from .services import sandbox_console
    with begin(engine) as c:
        return sandbox_console.read_file(c, settings, sandbox_id, path, actor=actor)


@app.post("/v1/sandboxes/{sandbox_id}/exec")
def sandbox_exec_endpoint(sandbox_id: str, payload: dict):
    """Run a command in a sandbox. Allowlisted helpers run freely; a raw shell
    (break_glass=true) requires a reason and emits an elevated audit event."""
    from .services import sandbox_console
    try:
        with begin(engine) as c:
            return sandbox_console.exec_command(
                c, settings, sandbox_id, payload.get("command", ""),
                break_glass=bool(payload.get("break_glass")),
                reason=payload.get("reason"), actor=payload.get("actor", "operator"),
            )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/v1/sandboxes/{sandbox_id}/stats")
def sandbox_stats_endpoint(sandbox_id: str):
    """Per-sandbox resource telemetry (CPU/mem/io, OOM, limits) for operators."""
    from .services import sandbox_console
    return sandbox_console.get_stats(settings, sandbox_id)


@app.get("/v1/sandboxes/{sandbox_id}/logs")
def sandbox_logs_endpoint(sandbox_id: str):
    """Guest serial console + code-agent log from a retained micro-VM (root-cause)."""
    from .services import sandbox_console
    try:
        return sandbox_console.get_logs(settings, sandbox_id)
    except Exception as e:  # noqa: BLE001
        return {"serial_tail": "", "agent_log": "", "unavailable": str(e)[:200]}


@app.post("/v1/sandboxes/{sandbox_id}/preserve")
def sandbox_preserve_endpoint(sandbox_id: str, payload: dict):
    """Hold a retained micro-VM longer for investigation (extends the reaper window)."""
    from .services import sandbox_console
    with begin(engine) as c:
        return sandbox_console.hold(
            c, settings, sandbox_id, hours=int(payload.get("hours", 2)),
            actor=payload.get("actor", "operator"),
        )


@app.delete("/v1/sandboxes/{sandbox_id}")
def sandbox_destroy_endpoint(sandbox_id: str):
    from .services import sandbox_console
    return sandbox_console.destroy(settings, sandbox_id)


# Curated catalog of the named service-level approval gates (the ~38 in-code
# create_blocking_decision triggers, grouped by domain for the Approval Rules view).
SERVICE_APPROVAL_GATES = [
    {"domain": "Agent loop", "gate": "Risk-tier autonomy gate", "trigger": "Any agent tool whose risk tier is above the autonomy threshold defers to a human Decision.", "reason": "agent_tool_requires_approval"},
    {"domain": "Software factory", "gate": "Plan / PRD review", "trigger": "A generated plan or PRD draft is reviewed before it becomes work.", "reason": "plan_ready_for_review · review_plan_draft"},
    {"domain": "Software factory", "gate": "Pull request proposal", "trigger": "Opening a GitHub pull request from an approved plan.", "reason": "review_pull_request"},
    {"domain": "Software factory", "gate": "PR merge", "trigger": "Merging a pull request requires explicit approval.", "reason": "blocked_manual_merge_required"},
    {"domain": "Software factory", "gate": "CI validation", "trigger": "A PR whose CI checks fail or are incomplete is held for a human.", "reason": "blocked_ci_validation_failed · _incomplete"},
    {"domain": "Software factory", "gate": "Deployment / rollout", "trigger": "Deploying or rolling out to an environment.", "reason": "blocked_manual_deploy_required · review_deployment"},
    {"domain": "Communications", "gate": "External send", "trigger": "Sending Slack / email to external recipients via the outbox.", "reason": "blocked_outbox_messages"},
    {"domain": "Infrastructure", "gate": "Provisioning / teardown", "trigger": "Provisioning, terminating, or changing infrastructure.", "reason": "blocked_manual_provision_required · review_infra_provisioning"},
    {"domain": "Budget", "gate": "Spend cap / freeze", "trigger": "An action that would exceed the daily cap or cross a freeze threshold is denied or gated.", "reason": "budget_denied · budget_governor"},
    {"domain": "Meetings", "gate": "Publish PRD", "trigger": "Publishing a cross-functional panel meeting's drafted PRD.", "reason": "meeting_prd_ready_to_publish"},
    {"domain": "Roles", "gate": "Autonomy change", "trigger": "Changing an agent role's autonomy level.", "reason": "role.autonomy_change"},
    {"domain": "Account connect", "gate": "Account connect required", "trigger": "An action that needs a connected external account before it can run.", "reason": "blocked_account_connect_required"},
]


@app.get("/v1/approval-rules")
def list_approval_rules(project_id: str):
    """The full set of rules that determine what requires human approval: the
    autonomy threshold, every tool/skill/MCP capability's risk tier (auto vs
    needs-approval), the named service gates, the budget gate thresholds, and the
    org's policy records. Read-only governance catalog."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    threshold = int(settings.agent_autonomous_risk_max)

    def _req(t):
        return int(t) > threshold

    tool_rules = []
    for t in describe_native_tools():
        tool_rules.append({"name": t["name"], "source": "native tool", "kind": t["kind"],
                           "risk_tier": t["risk_tier"], "requires_approval": _req(t["risk_tier"])})
    try:
        for s in entitlements.list_skills():
            sm = skill_summary_from_service(s).model_dump()
            tool_rules.append({"name": sm["id"], "source": "skill", "kind": sm["kind"],
                               "risk_tier": sm["risk_tier"], "requires_approval": _req(sm["risk_tier"])})
    except Exception:
        pass

    with begin(engine) as connection:
        for srv in list_mcp_servers(connection, project_id):
            tool_rules.append({"name": srv["name"], "source": "MCP server", "kind": "mcp",
                               "risk_tier": srv["risk_tier"], "requires_approval": _req(srv["risk_tier"])})
        bp = connection.execute(
            text("SELECT daily_cap_usd_micros, monthly_cap_usd_micros, soft_cap_pct, auto_lite_mode_pct, "
                 "freeze_provisioning_pct, freeze_tier2_pct FROM budget_policies "
                 "WHERE scope_type='project' AND scope_id=:p ORDER BY created_at DESC LIMIT 1"),
            {"p": project_id},
        ).mappings().first()
        budget_gates = dict(bp) if bp else {}
        pol = connection.execute(
            text("SELECT policy_key, title, status, version FROM policy_records "
                 "WHERE project_id=:p ORDER BY policy_key, version DESC"),
            {"p": project_id},
        ).mappings().all()
        policies = [dict(r) for r in pol]

    return {
        "project_id": project_id,
        "autonomy_threshold": threshold,
        "autonomy_explainer": (
            f"Agent actions at risk tier {threshold + 1} or higher require human approval; "
            f"tier {threshold} and below run autonomously."
        ),
        "tool_rules": tool_rules,
        "service_gates": SERVICE_APPROVAL_GATES,
        "budget_gates": budget_gates,
        "policies": policies,
    }


@app.get("/v1/capabilities")
def list_capabilities(project_id: str):
    """The full capability catalog an agent can use: BOSS's native tools (read +
    write, with risk tiers) and every registered MCP server with its live tools.
    Powers the Capabilities console so agents discover what's available and humans
    can audit/extend it."""
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")
    settings = get_settings()
    mcp = MCPClient(settings, timeout=6.0)
    with begin(engine) as connection:
        servers = list_mcp_servers(connection, project_id)
    enriched: list[dict] = []
    for s in servers:
        entry = dict(s)
        try:
            tools = mcp.list_tools(s["url"])
            entry["reachable"] = True
            entry["tools"] = [
                {"name": t.get("name"), "description": t.get("description", "")}
                for t in (tools or [])
            ]
        except Exception as exc:  # server down / unreachable
            entry["reachable"] = False
            entry["tools"] = []
            entry["error"] = f"{type(exc).__name__}: {exc}"
        enriched.append(entry)
    engine_kind = (settings.llm_engine or "auto").lower()
    if engine_kind == "claude_cli":
        engine_label = "Claude Code CLI (Max subscription)"
        engine_model = settings.claude_cli_model or "claude (default)"
    elif engine_kind == "azure" or (engine_kind == "auto" and settings.azure_openai_key):
        engine_label = "Azure OpenAI"
        engine_model = settings.azure_openai_deployment or "gpt-4o"
    else:
        engine_label = "OpenAI"
        engine_model = settings.openai_model
    try:
        skills = [skill_summary_from_service(s).model_dump() for s in entitlements.list_skills()]
    except Exception:
        skills = []
    return {
        "project_id": project_id,
        "engine": engine_kind,
        "engine_label": engine_label,
        "engine_model": engine_model,
        "native_tools": describe_native_tools(),
        "skills": skills,
        "mcp_servers": enriched,
    }


@app.get("/v1/status", response_model=StatusResponse)
def status(project_id: str) -> StatusResponse:
    if not database_is_reachable(engine):
        return StatusResponse(
            project_id=project_id,
            project_name=project_id,
            workspace_name="Database unavailable",
            org_name="Unknown org",
            database_connected=False,
            budget_state="unknown",
            pending_decisions=0,
            open_tasks=0,
            deadletters=0,
            active_jobs=0,
            top_risks=[],
            latest_snapshot_id=None,
            latest_snapshot_at=None,
            blocked_reason="Postgres is not reachable.",
        )

    with begin(engine) as connection:
        decisions.expire_overdue(connection, project_id)
        row = connection.execute(
            text(
                """
                SELECT p.id AS project_id, p.name AS project_name, w.name AS workspace_name, o.name AS org_name
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Unknown project_id")

        pending_decisions = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM decisions
                WHERE project_id = :project_id AND status IN ('pending', 'delegated')
                """
            ),
            {"project_id": project_id},
        ).scalar_one()
        open_tasks = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM tasks
                WHERE project_id = :project_id AND status = 'open'
                """
            ),
            {"project_id": project_id},
        ).scalar_one()
        budget_snapshot = budget.status_for_project(connection, project_id)
        current_health = health_snapshots.current_state(connection, project_id)
        latest_snapshot = health_snapshots.latest_snapshot(connection, project_id)

        return StatusResponse(
            project_id=row["project_id"],
            project_name=row["project_name"],
            workspace_name=row["workspace_name"],
            org_name=row["org_name"],
            database_connected=True,
            budget_state=budget_snapshot["budget_state"],
            pending_decisions=int(pending_decisions),
            open_tasks=int(open_tasks),
            deadletters=int(current_health["incident_state"]["deadletters"]),
            active_jobs=int(current_health["queue_state"]["leased_jobs"]),
            top_risks=current_health["top_risks"],
            latest_snapshot_id=latest_snapshot["id"] if latest_snapshot else None,
            latest_snapshot_at=(
                latest_snapshot["created_at"].isoformat() if latest_snapshot else None
            ),
            blocked_reason=None if settings.github_token and settings.openai_api_key else "Missing GitHub or OpenAI credentials.",
        )


@app.post("/v1/metatron/query", response_model=MetatronQueryResponse)
def metatron_query(payload: MetatronQueryRequest, request: Request) -> MetatronQueryResponse:
    # Rate limit high-cost Metatron path (P2). Early, before any DB/LLM work.
    _enforce_high_cost_rate_limit(
        request, payload.project_id, "metatron", endpoint="/v1/metatron/query"
    )
    with begin(engine) as connection:
        decisions.expire_overdue(connection, payload.project_id)
        try:
            result = metatron.answer_query(
                connection,
                payload.project_id,
                payload.question,
                task_id=payload.task_id,
                decision_id=payload.decision_id,
                limit=payload.limit,
            )
        except MetatronScopeNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return MetatronQueryResponse(**result)


@app.post("/v1/commands/propose", response_model=CommandProposeResponse, status_code=201)
def propose_command(payload: CommandProposeRequest, request: Request) -> CommandProposeResponse:
    # Rate limit high-cost command propose path (P2). Before perm check + work.
    _enforce_high_cost_rate_limit(
        request, payload.project_id, payload.actor, endpoint="/v1/commands/propose"
    )
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "command_submit",
        )
        try:
            result = intake.propose_command(
                connection,
                payload.project_id,
                payload.command,
                payload.constraints,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return CommandProposeResponse(**result)


@app.post("/v1/agent/chat", response_model=AgentChatResponse)
def agent_chat(payload: AgentChatRequest, request: Request) -> AgentChatResponse:
    """Persist one conversational turn and route it to an existing BOSS initiator."""
    _enforce_high_cost_rate_limit(
        request, payload.project_id, payload.actor, endpoint="/v1/agent/chat"
    )
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "agent_chat",
        )
        try:
            result = orchestration_agent.chat(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                thread_id=payload.thread_id,
                message=payload.message,
                confirm_plan_id=payload.confirm_plan_id,
                surface=payload.surface,
                external_ref=payload.external_ref,
            )
        except AgentThreadNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except AgentThreadOwnershipError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return AgentChatResponse(**result)


@app.get("/v1/agent/threads")
def list_agent_threads_endpoint(project_id: str, actor: str, since_days: int = 7):
    """Recent chat threads (default last 7 days) for the console's history + resume."""
    with begin(engine) as connection:
        require_project_actor(connection, project_id, actor, "agent_chat")
        return {
            "threads": orchestration_agent.list_threads(
                connection, project_id=project_id, actor=actor, since_days=since_days
            )
        }


@app.get("/v1/agent/threads/{thread_id}/messages")
def get_agent_thread_messages_endpoint(thread_id: str, project_id: str, actor: str):
    """Full message history for one chat thread (to restore a conversation)."""
    with begin(engine) as connection:
        require_project_actor(connection, project_id, actor, "agent_chat")
        owner = connection.execute(
            text("SELECT project_id FROM agent_threads WHERE id = :id"),
            {"id": thread_id},
        ).mappings().first()
        if not owner or owner["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="Agent thread not found.")
        return {
            "thread_id": thread_id,
            "messages": orchestration_agent.list_messages(connection, thread_id),
        }


@app.get("/v1/tasks", response_model=TaskListResponse)
def list_tasks(
    project_id: str, limit: int = 50, offset: int = 0
) -> TaskListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, title, description, status, priority, risk_tier,
                       owner_role_id, parent_task_id, dependency_task_ids, due_at,
                       data_boundary, dedupe_key, created_at, updated_at
                FROM tasks
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {
                "project_id": project_id,
                "limit": limit,
                "offset": offset,
            },
        ).mappings().all()
        return TaskListResponse(
            project_id=project_id,
            items=[task_summary_from_row(row) for row in rows],
            limit=limit,
            offset=offset,
        )


@app.post("/v1/tasks", response_model=TaskSummary, status_code=201)
def create_task(payload: TaskCreateRequest) -> TaskSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "task_create",
        )
        try:
            result = intake.create_task(
                connection,
                project_id=payload.project_id,
                title=payload.title,
                description=payload.description,
                dedupe_key=payload.dedupe_key,
                priority=payload.priority,
                risk_tier=payload.risk_tier,
                data_boundary=payload.data_boundary,
                inputs=payload.inputs,
                required_artifacts=payload.required_artifacts,
                owner_role_id=payload.owner_role_id,
                parent_task_id=payload.parent_task_id,
                dependency_task_ids=payload.dependency_task_ids,
                due_at=payload.due_at,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        row = connection.execute(
            text(
                """
                SELECT id, project_id, title, description, status, priority, risk_tier,
                       owner_role_id, parent_task_id, dependency_task_ids, due_at,
                       data_boundary, dedupe_key, created_at, updated_at
                FROM tasks
                WHERE id = :task_id
                """
            ),
            {"task_id": result["task_id"]},
        ).mappings().one()
        return task_summary_from_row(row)


@app.get("/v1/departments", response_model=DepartmentListResponse)
def list_departments(project_id: str, limit: int = 50, offset: int = 0) -> DepartmentListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")

    with begin(engine) as connection:
        items = canonical.list_departments(connection, project_id, limit=limit, offset=offset)
        return DepartmentListResponse(
            project_id=project_id,
            items=[DepartmentSummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.post("/v1/departments", response_model=DepartmentSummary, status_code=201)
def create_department(payload: DepartmentCreateRequest) -> DepartmentSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "pack_manage",
        )
        result = canonical.create_department(
            connection,
            project_id=payload.project_id,
            department_key=payload.department_key,
            name=payload.name,
            mission=payload.mission,
            actor=payload.actor,
        )
        return DepartmentSummary(**result)


@app.get("/v1/kpis", response_model=KpiListResponse)
def list_kpis(
    project_id: str, limit: int = 50, offset: int = 0
) -> KpiListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        return KpiListResponse(
            project_id=project_id,
            items=[
                KpiSummary(**item)
                for item in canonical.list_kpis(
                    connection, project_id, limit=limit, offset=offset
                )
            ],
            limit=limit,
            offset=offset,
        )


@app.post("/v1/kpis", response_model=KpiSummary, status_code=201)
def create_kpi(payload: KpiCreateRequest) -> KpiSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "pack_manage",
        )
        try:
            result = canonical.create_kpi(
                connection,
                project_id=payload.project_id,
                kpi_key=payload.kpi_key,
                name=payload.name,
                description=payload.description,
                target_value=payload.target_value,
                current_value=payload.current_value,
                unit=payload.unit,
                owner_role_id=payload.owner_role_id,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return KpiSummary(**result)


@app.get("/v1/policies", response_model=PolicyRecordListResponse)
def list_policy_records(project_id: str, limit: int = 50, offset: int = 0) -> PolicyRecordListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")

    with begin(engine) as connection:
        items = canonical.list_policy_records(connection, project_id, limit=limit, offset=offset)
        return PolicyRecordListResponse(
            project_id=project_id,
            items=[PolicyRecordSummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.post("/v1/policies", response_model=PolicyRecordSummary, status_code=201)
def create_policy_record(
    payload: PolicyRecordCreateRequest,
) -> PolicyRecordSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "budget_manage",
        )
        result = canonical.create_policy_record(
            connection,
            project_id=payload.project_id,
            policy_key=payload.policy_key,
            version=payload.version,
            title=payload.title,
            body=payload.body,
            actor=payload.actor,
        )
        return PolicyRecordSummary(**result)


@app.get("/v1/plans", response_model=PlanListResponse)
def list_plans(project_id: str, limit: int = 50, offset: int = 0) -> PlanListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")

    with begin(engine) as connection:
        items = canonical.list_plans(connection, project_id, limit=limit, offset=offset)
        return PlanListResponse(
            project_id=project_id,
            items=[PlanSummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.post("/v1/plans", response_model=PlanSummary, status_code=201)
def create_plan(payload: PlanCreateRequest) -> PlanSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "task_create",
        )
        result = canonical.create_plan(
            connection,
            project_id=payload.project_id,
            title=payload.title,
            objective=payload.objective,
            task_id=payload.task_id,
            phases=payload.phases,
            success_criteria=payload.success_criteria,
            risks=payload.risks,
            artifact_id=payload.artifact_id,
            actor=payload.actor,
        )
        return PlanSummary(**result)


@app.post("/v1/plans/{plan_id}/dispatch")
def dispatch_plan(plan_id: str, payload: PlanDispatchRequest) -> dict:
    """Turn a plan's phases into sequenced, executable child tasks (plan → task/job engine)."""
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "task_create")
        try:
            return intake.dispatch_plan(connection, plan_id, payload.actor)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/v1/meetings", response_model=MeetingListResponse)
def list_meetings(project_id: str, limit: int = 50, offset: int = 0) -> MeetingListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")

    with begin(engine) as connection:
        items = canonical.list_meetings(connection, project_id, limit=limit, offset=offset)
        return MeetingListResponse(
            project_id=project_id,
            items=[MeetingSummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.post("/v1/meetings", response_model=MeetingSummary, status_code=201)
def create_meeting(payload: MeetingCreateRequest) -> MeetingSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "task_create",
        )
        result = canonical.create_meeting(
            connection,
            project_id=payload.project_id,
            title=payload.title,
            purpose=payload.purpose,
            participants=payload.participants,
            agenda=payload.agenda,
            actor=payload.actor,
        )
        return MeetingSummary(**result)


@app.post("/v1/meetings/{meeting_id}/run", response_model=MeetingSummary)
def run_meeting(meeting_id: str, payload: MeetingRunRequest) -> MeetingSummary:
    with begin(engine) as connection:
        row = connection.execute(
            text("SELECT project_id, participants FROM meetings WHERE id = :meeting_id"),
            {"meeting_id": meeting_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="meeting not found")
        require_project_actor(
            connection,
            row["project_id"],
            payload.actor,
            "task_create",
        )

        minutes = payload.minutes
        dissent_records = payload.dissent_records
        consensus_reached = payload.consensus_reached
        outcome_status = payload.outcome_status
        blocked_state = payload.blocked_state
        # Explicit vote phase: tally supplied votes under quorum/threshold rules and let the
        # consensus engine derive the outcome (spec §D meeting consensus completeness).
        if payload.votes:
            eligible = len(row["participants"] or []) or len(payload.votes)
            tally = meeting_consensus.tally_votes(
                payload.votes,
                eligible_count=eligible,
                quorum=payload.quorum,
                threshold=payload.vote_threshold,
                default_action=payload.default_action,
            )
            minutes = {**payload.minutes, "vote_tally": tally}
            consensus_reached = tally["consensus_reached"]
            dissent_records = list(payload.dissent_records) + tally["dissent_records"]
            if tally["outcome"] == "no_quorum":
                outcome_status = "blocked"
                blocked_state = blocked_state or {
                    "reason": "quorum_not_met",
                    "vote_tally": tally,
                }
        try:
            result = canonical.run_meeting(
                connection,
                settings,
                meeting_id=meeting_id,
                actor=payload.actor,
                transcript=[item.model_dump() for item in payload.transcript],
                minutes=minutes,
                action_items=[item.model_dump() for item in payload.action_items],
                decision_items=[item.model_dump() for item in payload.decision_items],
                dissent_records=dissent_records,
                blocked_state=blocked_state,
                outcome_status=outcome_status,
                consensus_reached=consensus_reached,
            )
        except ValueError as error:
            detail = str(error)
            if "already" in detail:
                raise HTTPException(status_code=409, detail=detail) from error
            raise HTTPException(status_code=400, detail=detail) from error
        return MeetingSummary(**result)


@app.post("/v1/meetings/tally")
def tally_meeting_votes(payload: MeetingTallyRequest) -> dict:
    """Preview a vote tally under quorum/threshold rules (spec §D explicit vote phase)."""
    eligible = payload.eligible_count if payload.eligible_count is not None else len(payload.votes)
    return meeting_consensus.tally_votes(
        payload.votes,
        eligible_count=eligible,
        quorum=payload.quorum,
        threshold=payload.vote_threshold,
        default_action=payload.default_action,
    )


@app.post("/v1/meetings/sweep-stalled")
def sweep_stalled_meetings(payload: MeetingSweepRequest) -> dict:
    """Fail out meeting runs stalled past their phase timeout, opening an incident Decision for each
    (spec §D phase timeouts / stall prevention)."""
    return meeting_consensus.sweep_stalled_meeting_runs(engine, settings, actor=payload.actor)


@app.post("/v1/enterprise/soc2-evidence", status_code=201)
def collect_soc2_evidence(payload: SOC2EvidenceRequest) -> dict:
    """Generate a SOC2 evidence bundle (Trust Services Common Criteria) from live audit/policy/
    break-glass/change/proof data (spec §F enterprise SOC2 evidence collection)."""
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "audit_export")
        return soc2_evidence.collect_evidence(connection, payload.project_id, actor=payload.actor)


@app.post("/v1/finops/reconcile-commitments", status_code=201)
def reconcile_commitments(payload: CommitmentReconcileRequest) -> dict:
    """Reconcile active commitments vs actual usage → utilization + verdict + review decisions
    (spec §F InfraCommitment reconciler)."""
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "budget_manage")
    return finops_reconciler.reconcile_commitments(engine, payload.project_id, actor=payload.actor)


@app.get("/v1/finops/commitment-reconciliations")
def list_commitment_reconciliations(project_id: str, limit: int = 50) -> dict:
    return {"items": finops_reconciler.list_reconciliations(engine, project_id, limit)}


@app.post("/v1/finops/initiative-budget", status_code=201)
def request_initiative_budget(payload: InitiativeBudgetRequest) -> dict:
    """Tier-2+ initiative budget approval: above the threshold it requires a human Decision
    (spec §F tier2+ initiative budget approval)."""
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "budget_manage")
        return finops_reconciler.request_initiative_budget(
            connection,
            payload.project_id,
            title=payload.title,
            tier=payload.tier,
            amount_usd_micros=payload.amount_usd_micros,
            actor=payload.actor,
        )


@app.post("/v1/chaos/run", status_code=201)
def chaos_run(payload: ChaosRunRequest) -> dict:
    """Inject a controlled fault (lease_reclaim / worker_crash / deadletter_storm) and record whether
    the system recovered/detected it (spec §F load + chaos testing)."""
    from .services import chaos
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "health_refresh")
    try:
        return chaos.run_chaos(engine, settings, payload.project_id, payload.scenario, payload.actor)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/v1/chaos")
def chaos_list(project_id: str, limit: int = 20) -> dict:
    from .services import chaos
    return {"items": chaos.list_chaos_runs(engine, project_id, limit)}


@app.get("/v1/dashboard")
def get_dashboard(project_id: str) -> dict:
    """Consolidated real-time dashboard snapshot with a monotonic cursor (spec §F). Clients re-render
    only when the cursor advances."""
    with begin(engine) as connection:
        return dashboard_service.snapshot(connection, project_id)


@app.get("/v1/dashboard/poll")
def poll_dashboard(project_id: str, cursor: Optional[str] = None, timeout_ms: int = 5000) -> dict:
    """Bounded long-poll: returns when the dashboard cursor advances past `cursor` or the timeout
    elapses (spec §F real-time without a fixed-interval refresh)."""
    if timeout_ms < 0 or timeout_ms > 25000:
        raise HTTPException(status_code=400, detail="timeout_ms must be between 0 and 25000.")
    return dashboard_service.poll(engine, project_id, cursor=cursor, timeout_ms=timeout_ms)


@app.get("/v1/factory/wip")
def factory_wip(project_id: str, wip_limit: int = factory_scale.DEFAULT_WIP_LIMIT) -> dict:
    """In-flight code tasks vs the per-project WIP limit; admit a new task only if a slot is free
    (spec §E WIP limits + queue-depth gating)."""
    if wip_limit < 1:
        raise HTTPException(status_code=400, detail="wip_limit must be >= 1.")
    with begin(engine) as connection:
        return factory_scale.wip_status(connection, project_id, wip_limit=wip_limit)


@app.post("/v1/factory/merge-check")
def factory_merge_check(payload: FactoryMergeCheckRequest) -> dict:
    """Preview the fail-closed quality gate + merge-confidence verdict (spec §E confidence-gated merge)."""
    return factory_scale.evaluate_gate(payload.checks, min_confidence=payload.min_confidence)


@app.post("/v1/factory/quality-gate", status_code=201)
def factory_quality_gate(payload: FactoryQualityGateRequest) -> dict:
    """Evaluate + persist a quality gate (explicit lint + CI-green, second-model cross-check,
    confidence-gated merge) and store a review-packet artifact (spec §E)."""
    with begin(engine) as connection:
        require_project_actor(connection, payload.project_id, payload.actor, "task_create")
        return factory_scale_service.record_quality_gate(
            connection,
            payload.project_id,
            checks=payload.checks,
            actor=payload.actor,
            code_task_run_id=payload.code_task_run_id,
            task_id=payload.task_id,
            min_confidence=payload.min_confidence,
            risk_tier=payload.risk_tier,
        )


@app.post("/v1/factory/chunk-plan")
def factory_chunk_plan(payload: FactoryChunkPlanRequest) -> dict:
    """Split a plan/spec into small, independently-shippable PR chunks (spec §E spec→chunk planner)."""
    return factory_scale.chunk_spec(
        payload.spec,
        max_files_per_chunk=payload.max_files_per_chunk,
        max_chunks=payload.max_chunks,
    )


@app.get("/v1/jobs", response_model=JobListResponse)
def list_jobs(
    project_id: str,
    status: Optional[str] = None,
    kind: Optional[str] = None,
    task_id: Optional[str] = None,
    include_terminal: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> JobListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        try:
            items = jobs.list_jobs(
                connection,
                project_id,
                status=status,
                kind=kind,
                task_id=task_id,
                include_terminal=include_terminal,
                limit=limit,
                offset=offset,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return JobListResponse(
            project_id=project_id,
            items=[job_list_item_from_row(item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.get("/v1/jobs/{job_id}", response_model=JobSummary)
def get_job(job_id: str) -> JobSummary:
    with begin(engine) as connection:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, kind, status, data_boundary, priority,
                       max_attempts, attempt, run_after
                FROM jobs
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")
        return job_summary_from_row(row)


@app.get("/v1/tasks/{task_id}", response_model=TaskSummary)
def get_task(task_id: str) -> TaskSummary:
    with begin(engine) as connection:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, title, description, status, priority, risk_tier,
                       owner_role_id, parent_task_id, dependency_task_ids, due_at,
                       data_boundary, dedupe_key, created_at, updated_at
                FROM tasks
                WHERE id = :task_id
                """
            ),
            {"task_id": task_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        return task_summary_from_row(row)


@app.get(
    "/v1/self-improvement/proposals",
    response_model=SelfImprovementProposalListResponse,
)
def list_self_improvement_proposals(
    project_id: str, limit: int = 25, offset: int = 0
) -> SelfImprovementProposalListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        rows = self_improvement.list_proposals(connection, project_id, limit, offset)
        return SelfImprovementProposalListResponse(
            project_id=project_id,
            items=[self_improvement_summary_from_row(row) for row in rows],
            limit=limit,
            offset=offset,
        )


@app.post(
    "/v1/self-improvement/proposals",
    response_model=SelfImprovementProposalDetail,
    status_code=201,
)
def create_self_improvement_proposal(
    payload: SelfImprovementProposalCreateRequest,
) -> SelfImprovementProposalDetail:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "self_improvement_manage",
            )
            row = self_improvement.create_proposal(
                connection,
                project_id=payload.project_id,
                title=payload.title,
                summary=payload.summary,
                change_ref=payload.change_ref,
                diff_summary=payload.diff_summary,
                expected_impact=payload.expected_impact,
                test_plan=payload.test_plan,
                canary_scope=payload.canary_scope,
                rollback_plan=payload.rollback_plan,
                success_metrics=payload.success_metrics,
                actor=payload.actor,
                risk_tier=payload.risk_tier,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return self_improvement_detail_from_row(row)


@app.get(
    "/v1/self-improvement/proposals/{proposal_id}",
    response_model=SelfImprovementProposalDetail,
)
def get_self_improvement_proposal(
    proposal_id: str,
) -> SelfImprovementProposalDetail:
    with begin(engine) as connection:
        try:
            row = self_improvement.get_proposal(connection, proposal_id)
        except SelfImprovementProposalNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return self_improvement_detail_from_row(row)


@app.post(
    "/v1/self-improvement/proposals/{proposal_id}/canary",
    response_model=SelfImprovementProposalDetail,
)
def record_self_improvement_canary(
    proposal_id: str, payload: SelfImprovementCanaryRequest
) -> SelfImprovementProposalDetail:
    with begin(engine) as connection:
        try:
            proposal = self_improvement.get_proposal(connection, proposal_id)
            require_project_actor(
                connection,
                proposal["project_id"],
                payload.actor,
                "self_improvement_manage",
            )
            row = self_improvement.record_canary_result(
                connection,
                proposal_id,
                payload.actor,
                payload.outcome,
                payload.summary,
                payload.metrics,
                payload.evidence_refs,
            )
        except SelfImprovementProposalNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidSelfImprovementTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            raise_project_actor_error(error)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return self_improvement_detail_from_row(row)


@app.post(
    "/v1/self-improvement/proposals/{proposal_id}/rollout",
    response_model=SelfImprovementProposalDetail,
)
def record_self_improvement_rollout(
    proposal_id: str, payload: SelfImprovementRolloutRequest
) -> SelfImprovementProposalDetail:
    with begin(engine) as connection:
        try:
            proposal = self_improvement.get_proposal(connection, proposal_id)
            require_project_actor(
                connection,
                proposal["project_id"],
                payload.actor,
                "self_improvement_manage",
            )
            row = self_improvement.record_rollout(
                connection,
                proposal_id,
                payload.actor,
                payload.summary,
                payload.external_ref,
                payload.metrics,
                payload.evidence_refs,
            )
        except SelfImprovementProposalNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidSelfImprovementTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            raise_project_actor_error(error)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return self_improvement_detail_from_row(row)


@app.post(
    "/v1/self-improvement/proposals/{proposal_id}/rollback",
    response_model=SelfImprovementProposalDetail,
)
def rollback_self_improvement_proposal(
    proposal_id: str, payload: SelfImprovementRollbackRequest
) -> SelfImprovementProposalDetail:
    with begin(engine) as connection:
        try:
            proposal = self_improvement.get_proposal(connection, proposal_id)
            require_project_actor(
                connection,
                proposal["project_id"],
                payload.actor,
                "self_improvement_manage",
            )
            row = self_improvement.rollback(
                connection,
                proposal_id,
                payload.actor,
                payload.reason,
                payload.external_ref,
                payload.metrics,
                payload.evidence_refs,
            )
        except SelfImprovementProposalNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidSelfImprovementTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            raise_project_actor_error(error)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return self_improvement_detail_from_row(row)


@app.post(
    "/v1/self-improvement/proposals/{proposal_id}/run-canary",
    response_model=SelfImprovementProposalDetail,
)
def run_self_improvement_canary(
    proposal_id: str, payload: SelfImprovementRunCanaryRequest
) -> SelfImprovementProposalDetail:
    """Self-improvement runtime: auto-run the canary + eval-harness suites and record the outcome."""
    with begin(engine) as connection:
        try:
            proposal = self_improvement.get_proposal(connection, proposal_id)
            require_project_actor(
                connection,
                proposal["project_id"],
                payload.actor,
                "self_improvement_manage",
            )
        except SelfImprovementProposalNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            raise_project_actor_error(error)
    try:
        row = self_improvement.run_canary(engine, proposal_id, payload.actor)
    except SelfImprovementProposalNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except InvalidSelfImprovementTransitionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return self_improvement_detail_from_row(row)


@app.post(
    "/v1/self-improvement/proposals/{proposal_id}/rollout/start",
    response_model=SelfImprovementProposalDetail,
)
def start_self_improvement_rollout(
    proposal_id: str, payload: SelfImprovementStartRolloutRequest
) -> SelfImprovementProposalDetail:
    """Begin a staged rollout (ship narrow → widen, each stage gated by a health observation)."""
    with begin(engine) as connection:
        try:
            proposal = self_improvement.get_proposal(connection, proposal_id)
            require_project_actor(
                connection,
                proposal["project_id"],
                payload.actor,
                "self_improvement_manage",
            )
            row = self_improvement.start_rollout(
                connection,
                proposal_id,
                payload.actor,
                stages=payload.stages,
                thresholds=payload.thresholds,
            )
        except SelfImprovementProposalNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidSelfImprovementTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            raise_project_actor_error(error)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return self_improvement_detail_from_row(row)


@app.post(
    "/v1/self-improvement/proposals/{proposal_id}/rollout/advance",
    response_model=SelfImprovementProposalDetail,
)
def advance_self_improvement_rollout(
    proposal_id: str, payload: SelfImprovementAdvanceRolloutRequest
) -> SelfImprovementProposalDetail:
    """Record a stage health observation; advance, finalize, or auto-rollback on regression."""
    with begin(engine) as connection:
        try:
            proposal = self_improvement.get_proposal(connection, proposal_id)
            require_project_actor(
                connection,
                proposal["project_id"],
                payload.actor,
                "self_improvement_manage",
            )
            row = self_improvement.advance_rollout(
                connection,
                proposal_id,
                payload.actor,
                payload.observation,
            )
        except SelfImprovementProposalNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidSelfImprovementTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            raise_project_actor_error(error)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return self_improvement_detail_from_row(row)


@app.get("/v1/health-snapshots", response_model=HealthSnapshotListResponse)
def list_health_snapshots(
    project_id: str, limit: int = 25
) -> HealthSnapshotListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    with begin(engine) as connection:
        rows = health_snapshots.list_snapshots(connection, project_id, limit)
        return HealthSnapshotListResponse(
            project_id=project_id,
            items=[health_snapshot_summary_from_row(row) for row in rows],
        )


@app.post(
    "/v1/health-snapshots/refresh",
    response_model=HealthSnapshotSummary,
    status_code=201,
)
def refresh_health_snapshot(
    payload: HealthSnapshotRefreshRequest,
) -> HealthSnapshotSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "health_refresh",
        )
        row = health_snapshots.refresh_snapshot(
            connection, payload.project_id, payload.actor
        )
        return health_snapshot_summary_from_row(row)


@app.get("/v1/ops/queue", response_model=OpsQueueInspectionResponse)
def inspect_ops_queue(
    project_id: str,
    limit: int = 25,
    include_terminal: bool = False,
) -> OpsQueueInspectionResponse:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100.")
    with begin(engine) as connection:
        result = ops_queue.inspect_queue(
            connection,
            project_id,
            limit=limit,
            include_terminal=include_terminal,
        )
        return OpsQueueInspectionResponse(**result)


@app.post("/v1/ops/digests", response_model=OpsDigestResponse, status_code=201)
def create_ops_digest(payload: OpsDigestRequest) -> OpsDigestResponse:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "health_refresh",
        )
        result = ops_queue.create_ops_digest(
            connection,
            payload.project_id,
            actor=payload.actor,
            limit=payload.limit,
            include_terminal=payload.include_terminal,
        )
        return OpsDigestResponse(**result)


@app.get("/v1/artifacts", response_model=ArtifactListResponse)
def list_artifacts(project_id: str, limit: int = 25) -> ArtifactListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")

    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, artifact_type, title, uri, content_hash,
                       created_by_job_id, data_boundary, created_at
                FROM artifacts
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return ArtifactListResponse(
            project_id=project_id,
            items=[artifact_summary_from_row(row) for row in rows],
        )


@app.get("/v1/artifacts/{artifact_id}", response_model=ArtifactDetail)
def get_artifact(artifact_id: str) -> ArtifactDetail:
    with begin(engine) as connection:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, artifact_type, title, uri, content,
                       content_hash, created_by_job_id, data_boundary, created_at
                FROM artifacts
                WHERE id = :artifact_id
                """
            ),
            {"artifact_id": artifact_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return artifact_detail_from_row(row)


@app.get("/v1/roles", response_model=RoleListResponse)
def list_roles(project_id: str) -> RoleListResponse:
    with begin(engine) as connection:
        roles = specpack.list_roles_for_project(connection, project_id)
        return RoleListResponse(
            project_id=project_id,
            items=[role_summary_from_service(role) for role in roles],
        )


@app.get("/v1/roles/{role_key}", response_model=RoleDetail)
def get_role(role_key: str, project_id: str) -> RoleDetail:
    with begin(engine) as connection:
        try:
            role = specpack.get_role_for_project(connection, project_id, role_key)
        except RoleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return role_detail_from_service(role)


@app.get("/v1/roles/{role_key}/governance", response_model=RoleGovernanceDetail)
def get_role_governance(role_key: str, project_id: str) -> RoleGovernanceDetail:
    with begin(engine) as connection:
        try:
            detail = role_governance.get_governance(connection, project_id, role_key)
        except RoleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return role_governance_detail_from_service(detail)


@app.post(
    "/v1/roles/{role_key}/autonomy/check",
    response_model=RoleAutonomyCheckResponse,
)
def check_role_autonomy(
    role_key: str, request: RoleAutonomyCheckRequest
) -> RoleAutonomyCheckResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                request.project_id,
                request.actor,
                "role_review",
            )
            result = role_governance.check_autonomy(
                connection,
                project_id=request.project_id,
                role_key=role_key,
                action=request.action,
                risk_tier=request.risk_tier,
                sandboxed=request.sandboxed,
                policy_approved=request.policy_approved,
                actor=request.actor,
            )
        except RoleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return RoleAutonomyCheckResponse(**result)


@app.post(
    "/v1/roles/{role_key}/scope/check",
    response_model=RoleScopeCheckResponse,
)
def check_role_scope(
    role_key: str, request: RoleScopeCheckRequest
) -> RoleScopeCheckResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                request.project_id,
                request.actor,
                "role_review",
            )
            result = role_governance.check_scope(
                connection,
                project_id=request.project_id,
                role_key=role_key,
                action=request.action,
                actor=request.actor,
                skill_id=request.skill_id,
                evidence_ref=request.evidence_ref,
            )
        except RoleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return RoleScopeCheckResponse(**result)


@app.post(
    "/v1/roles/{role_key}/reviews",
    response_model=RoleGovernanceDetail,
    status_code=201,
)
def record_role_review(
    role_key: str, request: RoleReviewRequest
) -> RoleGovernanceDetail:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                request.project_id,
                request.actor,
                "role_review",
            )
            detail = role_governance.record_review(
                connection,
                project_id=request.project_id,
                role_key=role_key,
                actor=request.actor,
                summary=request.summary,
                metrics=request.metrics,
                evidence_refs=request.evidence_refs,
            )
        except RoleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return role_governance_detail_from_service(detail)


@app.post(
    "/v1/roles/{role_key}/autonomy/evaluate",
    response_model=RoleGovernanceDetail,
    status_code=201,
)
def evaluate_role_autonomy(
    role_key: str, request: AutonomyEvaluateRequest
) -> RoleGovernanceDetail:
    """Evidence-based autonomy engine: gather the role's allowed/denied/violation events since its
    last review and run a governance review — auto-proposing an upgrade decision or auto-downgrading
    on a detected violation (spec §C evidence-based autonomy auto-upgrade/downgrade)."""
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                request.project_id,
                request.actor,
                "role_review",
            )
            detail = autonomy_engine.evaluate(
                connection,
                project_id=request.project_id,
                role_key=role_key,
                actor=request.actor,
            )
        except RoleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        response = role_governance_detail_from_service(detail)
        response.evidence = detail.get("evidence")
        return response


@app.get("/v1/packs", response_model=PackListResponse)
def list_packs(project_id: str) -> PackListResponse:
    with begin(engine) as connection:
        packs = specpack.list_packs_for_project(connection, project_id)
        return PackListResponse(
            project_id=project_id,
            items=[pack_summary_from_service(pack) for pack in packs],
        )


@app.get("/v1/packs/{pack_id}", response_model=PackDetail)
def get_pack(pack_id: str, project_id: str) -> PackDetail:
    with begin(engine) as connection:
        try:
            pack = specpack.get_pack_for_project(connection, project_id, pack_id)
        except PackNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return pack_detail_from_service(pack)


@app.post("/v1/packs/install", response_model=PackDetail, status_code=201)
def install_pack(payload: PackInstallRequest) -> PackDetail:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "pack_manage",
            )
            result = specpack.install_pack(
                connection,
                payload.project_id,
                payload.pack_id,
                payload.actor,
            )
        except PackNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (RoleNotFoundError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return pack_detail_from_service(result)


@app.post("/v1/packs/import", response_model=PackDetail, status_code=201)
def import_pack(payload: PackImportRequest) -> PackDetail:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "pack_manage",
            )
            result = specpack.import_pack(
                connection,
                payload.project_id,
                payload.yaml_content,
                payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return pack_detail_from_service(result)


@app.get("/v1/packs/{pack_id}/export")
def export_pack(pack_id: str, project_id: str) -> Response:
    with begin(engine) as connection:
        try:
            yaml_content = specpack.export_pack_yaml(connection, project_id, pack_id)
        except PackNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return Response(
            content=yaml_content,
            media_type="application/x-yaml",
            headers={
                "Content-Disposition": f'attachment; filename="{pack_id}.yaml"',
            },
        )


@app.get("/v1/agent-modules", response_model=AgentModuleListResponse)
def list_agent_modules(project_id: str) -> AgentModuleListResponse:
    with begin(engine) as connection:
        modules = specpack.list_agent_modules_for_project(connection, project_id)
        return AgentModuleListResponse(
            project_id=project_id,
            items=[agent_module_summary_from_service(module) for module in modules],
        )


@app.get(
    "/v1/marketplace/listings",
    response_model=HostedMarketplaceListingListResponse,
)
def list_hosted_marketplace_listings(
    project_id: str,
    include_inactive: bool = False,
    limit: int = 50,
) -> HostedMarketplaceListingListResponse:
    with begin(engine) as connection:
        listings = commercial.list_marketplace_pack_listings(
            connection,
            project_id,
            include_inactive=include_inactive,
            limit=limit,
        )
        return HostedMarketplaceListingListResponse(
            project_id=project_id,
            items=[
                hosted_marketplace_listing_from_service(listing)
                for listing in listings
            ],
        )


@app.get(
    "/v1/marketplace/sync-runs",
    response_model=HostedMarketplaceSyncRunListResponse,
)
def list_hosted_marketplace_sync_runs(
    project_id: str,
    limit: int = 50,
) -> HostedMarketplaceSyncRunListResponse:
    with begin(engine) as connection:
        runs = commercial.list_marketplace_sync_runs(
            connection,
            project_id,
            limit=limit,
        )
        return HostedMarketplaceSyncRunListResponse(
            project_id=project_id,
            items=[
                hosted_marketplace_sync_run_from_service(run)
                for run in runs
            ],
        )


@app.post(
    "/v1/marketplace/sync",
    response_model=HostedMarketplaceSyncResponse,
    status_code=201,
)
def sync_hosted_marketplace(
    payload: HostedMarketplaceSyncRequest,
) -> HostedMarketplaceSyncResponse:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "module_manage",
        )
        try:
            result = commercial.sync_marketplace_pack_listings(
                connection,
                payload.project_id,
                actor=payload.actor,
                hosted_org_id=payload.hosted_org_id,
                metadata=payload.metadata,
            )
        except AccountConnectLinkNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except AccountConnectAuthSessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return HostedMarketplaceSyncResponse(
            project_id=result["project_id"],
            sync_run=hosted_marketplace_sync_run_from_service(result["sync_run"]),
            listing_count=result["listing_count"],
        )


@app.get("/v1/agent-modules/{module_id}", response_model=AgentModuleDetail)
def get_agent_module(module_id: str, project_id: str) -> AgentModuleDetail:
    with begin(engine) as connection:
        try:
            module = specpack.get_agent_module_for_project(
                connection, project_id, module_id
            )
        except AgentModuleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return agent_module_detail_from_service(module)


@app.get(
    "/v1/agent-module-install-records",
    response_model=AgentModuleInstallRecordListResponse,
)
def list_agent_module_install_records(
    project_id: str,
    module_id: Optional[str] = None,
    limit: int = 50,
) -> AgentModuleInstallRecordListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    with begin(engine) as connection:
        records = specpack.list_agent_module_install_records(
            connection,
            project_id,
            module_id=module_id,
            limit=limit,
        )
        return AgentModuleInstallRecordListResponse(
            project_id=project_id,
            items=[
                agent_module_install_record_summary_from_service(record)
                for record in records
            ],
        )


@app.post(
    "/v1/agent-modules/install",
    response_model=AgentModuleDetail,
    status_code=201,
)
def install_agent_module(payload: AgentModuleInstallRequest) -> AgentModuleDetail:
    entitlement_error: Optional[str] = None
    module: Optional[dict] = None
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "module_manage",
            )
            module = specpack.install_agent_module(
                connection,
                payload.project_id,
                payload.module_id,
                payload.actor,
                payload.config,
            )
        except AgentModuleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except AgentModuleEntitlementError as error:
            entitlement_error = str(error)
    if entitlement_error:
        raise HTTPException(status_code=403, detail=entitlement_error)
    if module is None:
        raise HTTPException(status_code=500, detail="Agent module install failed.")
    return agent_module_detail_from_service(module)


@app.post(
    "/v1/agent-modules/{module_id}/uninstall",
    response_model=AgentModuleDetail,
)
def uninstall_agent_module(
    module_id: str,
    payload: AgentModuleLifecycleRequest,
) -> AgentModuleDetail:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "module_manage",
            )
            module = specpack.uninstall_agent_module(
                connection,
                payload.project_id,
                module_id,
                payload.actor,
                payload.reason,
            )
        except AgentModuleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return agent_module_detail_from_service(module)


@app.post(
    "/v1/agent-modules/{module_id}/rollback",
    response_model=AgentModuleDetail,
)
def rollback_agent_module(
    module_id: str,
    payload: AgentModuleLifecycleRequest,
) -> AgentModuleDetail:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "module_manage",
            )
            module = specpack.rollback_agent_module(
                connection,
                payload.project_id,
                module_id,
                payload.actor,
                payload.reason,
            )
        except AgentModuleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except AgentModuleRollbackError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return agent_module_detail_from_service(module)


@app.post("/v1/agent-runtime/resolve", response_model=AgentRuntimeResolveResponse)
def resolve_agent_runtime(
    payload: AgentRuntimeResolveRequest,
) -> AgentRuntimeResolveResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "module_manage",
            )
            result = agent_runtime.resolve(
                connection,
                payload.project_id,
                job_kind=payload.job_kind,
                agent_module_id=payload.agent_module_id,
                task_payload=payload.task_payload,
                required=True,
            )
        except AgentModuleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return AgentRuntimeResolveResponse(**result)


@app.get("/v1/skills", response_model=SkillListResponse)
def list_skills(project_id: Optional[str] = None) -> SkillListResponse:
    if project_id is None:
        skills = entitlements.list_skills()
    else:
        with begin(engine) as connection:
            skills = entitlements.list_skills(
                project_id,
                connection=connection,
            )
    return SkillListResponse(
        items=[skill_summary_from_service(skill) for skill in skills]
    )


@app.get("/v1/skills/invocations")
def list_skill_invocations(project_id: str, limit: int = 50) -> dict:
    with begin(engine) as connection:
        return {"items": skill_runtime.list_invocations(connection, project_id, limit)}


@app.post("/v1/skills/import", status_code=201)
def import_skill_bundle(payload: CapabilityBundleImportRequest) -> dict:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection, payload.project_id, payload.actor, "pack_manage"
            )
            return capability_bundles.import_skill(
                connection,
                payload.project_id,
                payload.bundle,
                allow_untrusted=payload.allow_untrusted,
                enabled=payload.enabled,
                on_demand=payload.on_demand,
                actor=payload.actor,
            )
        except (CapabilityBundleError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/v1/skills/{skill_id}/export")
def export_skill_bundle(skill_id: str, project_id: str, actor: str = "ceo"):
    with begin(engine) as connection:
        try:
            require_project_actor(connection, project_id, actor, "pack_manage")
            content = capability_bundles.export_skill(
                connection, project_id, skill_id
            )
        except SkillNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except CapabilityBundleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    return Response(content=content, media_type="application/vnd.boss.capability+json")


def _set_project_skill_state(
    connection,
    skill_id: str,
    payload: SkillToggleRequest,
    *,
    enabled: bool,
) -> dict:
    require_project_actor(
        connection, payload.project_id, payload.actor, "pack_manage"
    )
    catalog = entitlements.list_skills(
        payload.project_id, connection=connection
    )
    skill = next((item for item in catalog if item["id"] == skill_id), None)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found in project catalog.")
    if skill.get("system") is True and not enabled:
        raise HTTPException(status_code=409, detail="System skills cannot be disabled.")
    rows = list_project_skills(connection, payload.project_id)
    row = next((item for item in rows if item["skill_id"] == skill_id), None)
    if row is None:
        row = ensure_skill(
            connection,
            payload.project_id,
            skill_id,
            source="system" if skill.get("system") is True else "tenant",
            enabled=enabled,
            on_demand=(
                bool(payload.on_demand)
                if payload.on_demand is not None
                else skill.get("load") == "on_demand"
            ),
            publisher=skill.get("publisher") or {},
            provenance=skill.get("provenance") or {},
            actor=payload.actor,
        )
    updated = (
        enable_skill(
            connection,
            payload.project_id,
            skill_id,
            payload.actor,
            on_demand=payload.on_demand,
        )
        if enabled
        else disable_skill(
            connection, payload.project_id, skill_id, payload.actor
        )
    )
    return updated or row


@app.post("/v1/skills/{skill_id}/enable")
def enable_project_skill(skill_id: str, payload: SkillToggleRequest) -> dict:
    with begin(engine) as connection:
        return _set_project_skill_state(connection, skill_id, payload, enabled=True)


@app.post("/v1/skills/{skill_id}/disable")
def disable_project_skill(skill_id: str, payload: SkillToggleRequest) -> dict:
    with begin(engine) as connection:
        return _set_project_skill_state(connection, skill_id, payload, enabled=False)


@app.post("/v1/skills/{skill_id}/invoke", status_code=201)
def invoke_skill(skill_id: str, payload: SkillInvokeRequest) -> dict:
    """Run API mock mode or lease-owned worker real mode through one governance gauntlet."""
    with begin(engine) as connection:
        try:
            executor_context = None
            if not payload.mock:
                if not (
                    payload.created_by_job_id
                    and payload.worker_id
                    and payload.lease_token
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Real skill invocation requires an active worker lease. "
                            "Use mock=true for a side-effect-free invocation."
                        ),
                    )
                leased_job = jobs.require_active_lease(
                    connection,
                    payload.worker_id,
                    payload.created_by_job_id,
                    payload.lease_token,
                )
                job_payload = leased_job.get("payload") or {}
                if (
                    leased_job["project_id"] != payload.project_id
                    or leased_job["kind"] != "skill.execute"
                    or job_payload.get("skill_id") != skill_id
                    or (job_payload.get("caller") or "worker") != payload.caller
                    or (job_payload.get("inputs") or {}) != payload.inputs
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Skill invocation does not match the active leased job "
                            "scope."
                        ),
                    )
                executor_context = SkillExecutorContext(
                    settings=settings,
                    connection=connection,
                    project_id=payload.project_id,
                    actor=payload.worker_id,
                    created_by_job_id=payload.created_by_job_id,
                )
            return skill_runtime.invoke(
                connection,
                payload.project_id,
                skill_id,
                payload.caller,
                payload.inputs,
                mock=payload.mock,
                executor_context=executor_context,
                actor=(
                    payload.worker_id
                    if not payload.mock
                    else payload.actor or payload.caller
                ),
            )
        except LeaseNotFoundError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except SkillNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except SkillPermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/v1/model-adapters", response_model=ModelAdapterListResponse)
def list_model_adapters() -> ModelAdapterListResponse:
    return ModelAdapterListResponse(
        items=[
            model_adapter_summary_from_service(adapter)
            for adapter in entitlements.list_model_adapters()
        ]
    )


@app.get("/v1/entitlement-pools", response_model=EntitlementPoolListResponse)
def list_entitlement_pools(project_id: str) -> EntitlementPoolListResponse:
    with begin(engine) as connection:
        items = entitlements.list_entitlement_pools(connection, project_id)
        return EntitlementPoolListResponse(
            project_id=project_id,
            items=[entitlement_pool_summary_from_service(item) for item in items],
        )


@app.post(
    "/v1/entitlement-pools/{pool_id}/probe",
    response_model=EntitlementPoolProbeResponse,
)
def probe_entitlement_pool(
    pool_id: str,
    payload: EntitlementPoolProbeRequest,
) -> EntitlementPoolProbeResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "entitlement_manage",
            )
            result = entitlements.record_capacity_probe(
                connection,
                payload.project_id,
                pool_id,
                actor=payload.actor,
                observed_capacity=payload.observed_capacity,
                usage_units=payload.usage_units,
                provider_ref=payload.provider_ref,
                model_route_decision_id=payload.model_route_decision_id,
                dedupe_key=payload.dedupe_key,
                status=payload.status,
            )
        except EntitlementPoolNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return EntitlementPoolProbeResponse(
            project_id=payload.project_id,
            pool=entitlement_pool_summary_from_service(result["pool"]),
            receipt=capacity_receipt_summary_from_service(result["receipt"]),
            duplicate=bool(result["duplicate"]),
        )


@app.get(
    "/v1/model-route-decisions",
    response_model=ModelRouteDecisionListResponse,
)
def list_model_route_decisions(
    project_id: str,
    limit: int = 25,
    skill_id: Optional[str] = None,
    task_kind: Optional[str] = None,
    workload_class: Optional[str] = None,
    selected_provider: Optional[str] = Query(default=None, alias="provider"),
    selected_source_id: Optional[str] = Query(default=None, alias="source_id"),
    selected_model_id: Optional[str] = Query(default=None, alias="model_id"),
    routing_mode: Optional[str] = None,
    budget_state: Optional[str] = None,
    data_boundary: Optional[DataBoundary] = None,
    risk_tier: Optional[int] = Query(default=None, ge=0, le=3),
    denial_reason: Optional[str] = None,
    denied_only: bool = False,
    selected_only: bool = False,
) -> ModelRouteDecisionListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if denied_only and selected_only:
        raise HTTPException(
            status_code=400,
            detail="denied_only and selected_only filters cannot both be true.",
        )
    filters = {
        "skill_id": skill_id,
        "task_kind": task_kind,
        "workload_class": workload_class,
        "selected_provider": selected_provider,
        "selected_source_id": selected_source_id,
        "selected_model_id": selected_model_id,
        "routing_mode": routing_mode,
        "budget_state": budget_state,
        "data_boundary": data_boundary,
        "risk_tier": risk_tier,
        "denial_reason": denial_reason,
        "denied_only": denied_only,
        "selected_only": selected_only,
    }
    with begin(engine) as connection:
        items = entitlements.list_model_route_decisions(
            connection, project_id, limit, filters
        )
        return ModelRouteDecisionListResponse(
            project_id=project_id,
            items=[model_route_decision_summary_from_service(item) for item in items],
        )


@app.post(
    "/v1/model-route-decisions/simulate",
    response_model=ModelRouteDecisionSimulateResponse,
)
def simulate_model_route_decision(
    payload: ModelRouteDecisionSimulateRequest,
) -> ModelRouteDecisionSimulateResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "entitlement_manage",
            )
            decision = entitlements.simulate_model_route_decision(
                connection,
                payload.project_id,
                skill_id=payload.skill_id,
                task_kind=payload.task_kind,
                workload_class=payload.workload_class,
                risk_tier=payload.risk_tier,
                data_boundary=payload.data_boundary,
                preferred_capabilities=payload.preferred_capabilities,
                budget_state=payload.budget_state,
                actor=payload.actor,
            )
        except SkillNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return ModelRouteDecisionSimulateResponse(
            project_id=payload.project_id,
            decision=model_route_decision_summary_from_service(decision),
        )


@app.get("/v1/entitlements/sources", response_model=EntitlementSourceListResponse)
def list_entitlement_sources(project_id: str) -> EntitlementSourceListResponse:
    with begin(engine) as connection:
        items = entitlements.list_sources(connection, project_id)
        return EntitlementSourceListResponse(
            project_id=project_id,
            items=[entitlement_source_summary_from_service(item) for item in items],
        )


@app.get(
    "/v1/entitlement-connection-sessions",
    response_model=EntitlementConnectionSessionListResponse,
)
def list_entitlement_connection_sessions(
    project_id: str,
    source_id: Optional[str] = None,
    limit: int = 25,
) -> EntitlementConnectionSessionListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    with begin(engine) as connection:
        items = entitlements.list_connection_sessions(
            connection,
            project_id,
            source_id=source_id,
            limit=limit,
        )
        return EntitlementConnectionSessionListResponse(
            project_id=project_id,
            items=[
                entitlement_connection_session_summary_from_service(item)
                for item in items
            ],
        )


@app.post(
    "/v1/entitlements/sources",
    response_model=EntitlementSourceSummary,
    status_code=201,
)
def upsert_entitlement_source(
    payload: EntitlementSourceUpsertRequest,
) -> EntitlementSourceSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "entitlement_manage",
        )
        item = entitlements.upsert_source(
            connection,
            payload.model_dump(exclude={"actor"}),
            actor=payload.actor,
        )
        return entitlement_source_summary_from_service(item)


@app.post(
    "/v1/entitlements/sources/{source_id}/connection-sessions",
    response_model=EntitlementConnectionSessionSummary,
    status_code=201,
)
def create_entitlement_connection_session(
    source_id: str,
    payload: EntitlementConnectionSessionCreateRequest,
) -> EntitlementConnectionSessionSummary:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "entitlement_manage",
            )
            session = entitlements.create_connection_session(
                connection,
                payload.project_id,
                source_id,
                actor=payload.actor,
                connection_mode=payload.connection_mode,
                requested_scopes=payload.requested_scopes,
                metadata=payload.metadata,
            )
        except EntitlementSourceNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidEntitlementConnectionError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return entitlement_connection_session_summary_from_service(session)


@app.post(
    "/v1/entitlements/sources/{source_id}/connect",
    response_model=EntitlementSourceSummary,
)
def connect_entitlement_source(
    source_id: str,
    payload: EntitlementSourceConnectRequest,
) -> EntitlementSourceSummary:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "entitlement_manage",
            )
            item = entitlements.connect_source(
                connection,
                payload.project_id,
                source_id,
                actor=payload.actor,
                connection_ref=payload.connection_ref,
                auth_method=payload.auth_method,
                oauth_scopes=payload.oauth_scopes,
                connection_metadata=payload.connection_metadata,
                observed_capacity=payload.observed_capacity,
            )
        except EntitlementSourceNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidEntitlementConnectionError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return entitlement_source_summary_from_service(item)


@app.post(
    "/v1/entitlement-connection-sessions/{session_id}/complete",
    response_model=EntitlementConnectionSessionCompleteResponse,
)
def complete_entitlement_connection_session(
    session_id: str,
    payload: EntitlementConnectionSessionCompleteRequest,
) -> EntitlementConnectionSessionCompleteResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "entitlement_manage",
            )
            result = entitlements.complete_connection_session(
                connection,
                payload.project_id,
                session_id,
                actor=payload.actor,
                connection_ref=payload.connection_ref,
                oauth_scopes=payload.oauth_scopes,
                connection_metadata=payload.connection_metadata,
                observed_capacity=payload.observed_capacity,
            )
        except EntitlementConnectionSessionNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (
            EntitlementConnectionSessionStateError,
            InvalidEntitlementConnectionError,
        ) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return EntitlementConnectionSessionCompleteResponse(
            project_id=payload.project_id,
            session=entitlement_connection_session_summary_from_service(
                result["session"]
            ),
            source=entitlement_source_summary_from_service(result["source"]),
        )


@app.post(
    "/v1/entitlements/sources/{source_id}/disconnect",
    response_model=EntitlementSourceSummary,
)
def disconnect_entitlement_source(
    source_id: str,
    payload: EntitlementSourceDisconnectRequest,
) -> EntitlementSourceSummary:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "entitlement_manage",
            )
            item = entitlements.disconnect_source(
                connection,
                payload.project_id,
                source_id,
                actor=payload.actor,
                reason=payload.reason,
            )
        except EntitlementSourceNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return entitlement_source_summary_from_service(item)


@app.post("/v1/router/preview", response_model=RoutingPreviewResponse)
def preview_route(payload: RoutingPreviewRequest) -> RoutingPreviewResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "entitlement_manage",
            )
            route = entitlements.preview_route(
                connection,
                payload.project_id,
                skill_id=payload.skill_id,
                task_kind=payload.task_kind,
                workload_class=payload.workload_class,
                risk_tier=payload.risk_tier,
                data_boundary=payload.data_boundary,
                preferred_capabilities=payload.preferred_capabilities,
                budget_state=payload.budget_state,
                actor=payload.actor,
            )
        except SkillNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return RoutingPreviewResponse(**route)


@app.get("/v1/router/logs", response_model=RoutingLogListResponse)
def list_routing_logs(project_id: str, limit: int = 25) -> RoutingLogListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    with begin(engine) as connection:
        items = entitlements.list_routing_logs(connection, project_id, limit)
        return RoutingLogListResponse(
            project_id=project_id,
            items=[routing_log_item_from_service(item) for item in items],
        )


@app.get("/v1/secret-refs", response_model=SecretRefListResponse)
def list_secret_refs(
    project_id: str,
    provider: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> SecretRefListResponse:
    with begin(engine) as connection:
        try:
            items = secret_refs.list_secret_refs(
                connection,
                project_id,
                provider=provider,
                status=status,
                limit=limit,
                offset=offset,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return SecretRefListResponse(
            project_id=project_id,
            items=[secret_ref_summary_from_service(item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.post("/v1/secret-refs", response_model=SecretRefSummary, status_code=201)
def create_secret_ref(payload: SecretRefCreateRequest) -> SecretRefSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            item = secret_refs.create_secret_ref(
                connection,
                project_id=payload.project_id,
                ref_kind=payload.ref_kind,
                ref_key=payload.ref_key,
                provider=payload.provider,
                status=payload.status,
                metadata=payload.metadata,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return secret_ref_summary_from_service(item)


@app.get("/v1/secret-refs/{secret_ref_id}", response_model=SecretRefSummary)
def get_secret_ref(secret_ref_id: str, project_id: str) -> SecretRefSummary:
    with begin(engine) as connection:
        try:
            item = secret_refs.get_secret_ref(connection, project_id, secret_ref_id)
        except SecretRefNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return secret_ref_summary_from_service(item)


@app.patch("/v1/secret-refs/{secret_ref_id}", response_model=SecretRefSummary)
def update_secret_ref(
    secret_ref_id: str, payload: SecretRefUpdateRequest
) -> SecretRefSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            item = secret_refs.update_secret_ref(
                connection,
                project_id=payload.project_id,
                secret_ref_id=secret_ref_id,
                provider=payload.provider,
                status=payload.status,
                metadata=payload.metadata,
                actor=payload.actor,
            )
        except SecretRefNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return secret_ref_summary_from_service(item)


@app.delete("/v1/secret-refs/{secret_ref_id}", status_code=204)
def delete_secret_ref(
    secret_ref_id: str,
    project_id: str,
    actor: str = "system",
) -> Response:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            project_id,
            actor,
            "providerops_manage",
        )
        try:
            secret_refs.delete_secret_ref(
                connection,
                project_id=project_id,
                secret_ref_id=secret_ref_id,
                actor=actor,
            )
        except SecretRefNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(status_code=204)


@app.get("/v1/providers", response_model=ProviderAccountListResponse)
def list_provider_accounts(project_id: str) -> ProviderAccountListResponse:
    with begin(engine) as connection:
        items = resource_plane.list_provider_accounts(connection, project_id)
        return ProviderAccountListResponse(
            project_id=project_id,
            items=[ProviderAccountSummary(**item) for item in items],
        )


@app.post("/v1/providers", response_model=ProviderAccountSummary, status_code=201)
def create_provider_account(
    payload: ProviderAccountCreateRequest,
) -> ProviderAccountSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            item = resource_plane.create_provider_account(
                connection,
                project_id=payload.project_id,
                provider=payload.provider,
                account_label=payload.account_label,
                credential_ref=payload.credential_ref,
                scopes=payload.scopes,
                read_only=payload.read_only,
                deploy_capable=payload.deploy_capable,
                billing_readable=payload.billing_readable,
                destructive_actions_disabled=payload.destructive_actions_disabled,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return ProviderAccountSummary(**item)


@app.post("/v1/providers/{provider_account_id}/test", response_model=ProviderConnectionTestResponse)
def test_provider_account(
    provider_account_id: str,
    project_id: str,
    actor: str = "ceo",
) -> ProviderConnectionTestResponse:
    with begin(engine) as connection:
        require_project_actor(connection, project_id, actor, "providerops_manage")
        try:
            result = resource_plane.test_provider_account(
                connection,
                project_id=project_id,
                provider_account_id=provider_account_id,
                actor=actor,
            )
        except ProviderAccountNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return ProviderConnectionTestResponse(
            status=result["status"],
            provider_account=ProviderAccountSummary(**result["provider_account"]),
            provider_health=ProviderHealthSummary(**result["provider_health"]),
            detail=result.get("detail") or {},
        )


@app.get("/v1/resources", response_model=ExternalResourceListResponse)
def list_external_resources(
    project_id: str,
    provider: Optional[str] = None,
    ownership: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> ExternalResourceListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        items = resource_plane.list_resources(
            connection,
            project_id,
            provider=provider,
            ownership=ownership,
            limit=limit,
            offset=offset,
        )
        return ExternalResourceListResponse(
            project_id=project_id,
            items=[ExternalResourceSummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.post("/v1/resources/sync", response_model=ResourceSyncResponse)
def sync_external_resources(payload: ResourceSyncRequest, request: Request) -> ResourceSyncResponse:
    # Rate limit high-cost provider sync path (P2 per review). Early gate.
    _enforce_high_cost_rate_limit(
        request, payload.project_id, payload.actor, endpoint="/v1/resources/sync"
    )
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            result = resource_plane.sync_resources(
                connection,
                project_id=payload.project_id,
                provider_account_id=payload.provider_account_id,
                actor=payload.actor,
            )
        except ProviderAccountNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return ResourceSyncResponse(
            project_id=result["project_id"],
            runs=[ResourceSyncRunSummary(**item) for item in result["runs"]],
            resources_seen=result["resources_seen"],
            resources_changed=result["resources_changed"],
            costs_seen=result["costs_seen"],
        )


@app.get("/v1/costs", response_model=CostEventListResponse)
def list_provider_costs(project_id: str, limit: int = 100, offset: int = 0) -> CostEventListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        items = resource_plane.list_cost_events(connection, project_id, limit=limit, offset=offset)
        return CostEventListResponse(
            project_id=project_id,
            items=[CostEventSummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.get("/v1/cost-anomalies", response_model=CostAnomalyListResponse)
def list_provider_cost_anomalies(
    project_id: str,
    limit: int = 100,
    offset: int = 0,
) -> CostAnomalyListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        items = resource_plane.list_cost_anomalies(connection, project_id, limit=limit, offset=offset)
        return CostAnomalyListResponse(
            project_id=project_id,
            items=[CostAnomalySummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.get("/v1/changesets", response_model=ChangeSetListResponse)
def list_changesets(
    project_id: str,
    status: Optional[str] = None,
    limit: int = 100,
) -> ChangeSetListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    with begin(engine) as connection:
        items = resource_plane.list_changesets(
            connection,
            project_id,
            status=status,
            limit=limit,
        )
        return ChangeSetListResponse(
            project_id=project_id,
            items=[ChangeSetSummary(**item) for item in items],
        )


@app.post("/v1/changesets", response_model=ChangeSetSummary, status_code=201)
def create_changeset(payload: ChangeSetCreateRequest) -> ChangeSetSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            item = resource_plane.create_changeset(
                connection,
                project_id=payload.project_id,
                provider_account_id=payload.provider_account_id,
                title=payload.title,
                summary=payload.summary,
                operations=[
                    operation.model_dump() for operation in payload.operations
                ],
                expected_cost_usd_micros=payload.expected_cost_usd_micros,
                reservation_id=payload.reservation_id,
                rollback_plan=payload.rollback_plan,
                risk_tier=payload.risk_tier,
                approval_required=payload.approval_required,
                actor=payload.actor,
            )
        except (ProviderAccountNotFoundError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return ChangeSetSummary(**item)


@app.post("/v1/changesets/{changeset_id}/approve", response_model=ChangeSetSummary)
def approve_changeset(
    changeset_id: str,
    payload: ChangeSetApproveRequest,
) -> ChangeSetSummary:
    with begin(engine) as connection:
        project_id = project_id_for_changeset(connection, changeset_id)
        required_roles = resource_plane.required_approvers_for_changeset(
            connection, changeset_id
        )
        if required_roles:
            # Dual-control: only an active member holding one of the named approver
            # roles may approve (no generic providerops_manage shortcut).
            try:
                members.require_member(
                    connection,
                    project_id,
                    payload.actor,
                    role_keys_any=required_roles,
                )
            except (
                ProjectMemberNotFoundError,
                ProjectMemberPermissionError,
            ) as error:
                record_policy_denial(
                    project_id, payload.actor, "changeset_approve", str(error)
                )
                raise HTTPException(status_code=403, detail=str(error)) from error
            require_authenticated_actor_binding(
                project_id, payload.actor, "changeset_approve"
            )
        else:
            require_project_actor(
                connection,
                project_id,
                payload.actor,
                "providerops_manage",
            )
        try:
            item = resource_plane.approve_changeset(
                connection,
                changeset_id=changeset_id,
                actor=payload.actor,
            )
        except (ChangeSetNotFoundError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return ChangeSetSummary(**item)


@app.post("/v1/changesets/{changeset_id}/apply", response_model=ChangeSetApplyResponse)
def apply_changeset(
    changeset_id: str,
    payload: ChangeSetApplyRequest,
) -> ChangeSetApplyResponse:
    with begin(engine) as connection:
        project_id = project_id_for_changeset(connection, changeset_id)
        require_project_actor(
            connection,
            project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            result = resource_plane.apply_changeset(
                connection,
                changeset_id=changeset_id,
                actor=payload.actor,
                created_by_job_id=payload.created_by_job_id,
                dry_run=payload.dry_run,
            )
        except (ChangeSetNotFoundError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return ChangeSetApplyResponse(
            status=result["status"],
            changeset=ChangeSetSummary(**result["changeset"]),
            resource_receipts=[
                ResourceReceiptSummary(**item)
                for item in result.get("resource_receipts", [])
            ],
            expected_receipts=result.get("expected_receipts"),
            decision_id=result.get("decision_id"),
        )


@app.post(
    "/v1/changesets/{changeset_id}/terraform-export",
    response_model=TerraformExportResponse,
)
def export_changeset_terraform(
    changeset_id: str,
    payload: TerraformExportRequest,
) -> TerraformExportResponse:
    """Track 1 Terraform export (BOSS-66): project a ChangeSet to .tf.json.

    The BOSS DB stays canonical — the export is a deterministic downstream
    projection. Opt-in: rejected unless at least one deployment target enabled
    terraform_export, or the caller forces it (audited escape hatch)."""
    with begin(engine) as connection:
        project_id = project_id_for_changeset(connection, changeset_id)
        require_project_actor(
            connection,
            project_id,
            payload.actor,
            "providerops_manage",
        )
        opt_in_targets = terraform_export_service.count_terraform_export_targets(
            connection, project_id
        )
        forced = False
        if opt_in_targets == 0:
            if not payload.force:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Terraform export is opt-in and disabled for this "
                        "project: no deployment target has terraform_export "
                        "enabled. Enable a target or pass force=true "
                        "(audited)."
                    ),
                )
            forced = True
            emit_event(
                connection,
                project_id,
                "terraform_export.forced",
                payload.actor,
                "changeset",
                changeset_id,
                {
                    "reason": (
                        "No deployment target has terraform_export enabled; "
                        "actor forced the export."
                    ),
                },
            )
        try:
            result = terraform_export_service.export_changeset(
                connection,
                settings,
                changeset_id,
                actor=payload.actor,
            )
        except (
            ValueError,
            terraform_export_service.TerraformExportError,
        ) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        commit_result = None
        if payload.commit_to_repo:
            try:
                commit_result = terraform_export_service.commit_export_to_repo(
                    settings,
                    changeset_id,
                    result["canonical"],
                    branch=payload.repo_branch or "main",
                )
            except Exception as error:  # noqa: BLE001 — artifact is stored; report
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Terraform export artifact stored, but the repo "
                        f"commit failed: {error}"
                    ),
                ) from error
        return TerraformExportResponse(
            **result,
            forced=forced,
            opt_in_targets=opt_in_targets,
            commit=commit_result,
        )


@app.get(
    "/v1/changesets/{changeset_id}/terraform-export",
    response_model=TerraformExportArtifactResponse,
)
def get_changeset_terraform_export(
    changeset_id: str,
) -> TerraformExportArtifactResponse:
    with begin(engine) as connection:
        project_id = project_id_for_changeset(connection, changeset_id)
        row = terraform_export_service.latest_export_artifact(
            connection, project_id, changeset_id
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail="No terraform export artifact exists for this ChangeSet.",
            )
        content = row.get("content") or {}
        return TerraformExportArtifactResponse(
            artifact_id=row["id"],
            changeset_id=changeset_id,
            project_id=project_id,
            content_hash=content.get("content_hash") or row.get("content_hash"),
            created_at=(
                row["created_at"].isoformat() if row.get("created_at") else None
            ),
            export=content,
        )


@app.post(
    "/v1/projects/{project_id}/terraform-drift-reconcile",
    response_model=TerraformDriftReconcileResponse,
)
def terraform_drift_reconcile(
    project_id: str,
    payload: TerraformDriftReconcileRequest,
) -> TerraformDriftReconcileResponse:
    """Track 1 inbound drift-sync (BOSS-69): three-way compare BOSS desired
    state vs a customer-supplied .tf/plan JSON vs synced live inventory.

    THE INVARIANT: the supplied .tf NEVER mutates the DB directly. Divergence
    yields a drift report plus a PROPOSED corrective ChangeSet through the
    normal dual-control approval gate; rejecting it changes nothing. Shares
    the terraform_export opt-in gate (force is the audited escape hatch)."""
    with begin(engine) as connection:
        require_project_actor(
            connection,
            project_id,
            payload.actor,
            "providerops_manage",
        )
        if bool(payload.tf_json) == bool(payload.plan_json):
            raise HTTPException(
                status_code=422,
                detail="Provide exactly one of tf_json or plan_json.",
            )
        opt_in_targets = terraform_export_service.count_terraform_export_targets(
            connection, project_id
        )
        forced = False
        if opt_in_targets == 0:
            if not payload.force:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Terraform drift-sync is opt-in and disabled for this "
                        "project: no deployment target has terraform_export "
                        "enabled. Enable a target or pass force=true "
                        "(audited)."
                    ),
                )
            forced = True
            if not payload.dry_run:
                emit_event(
                    connection,
                    project_id,
                    "terraform_drift.forced",
                    payload.actor,
                    "project",
                    project_id,
                    {
                        "reason": (
                            "No deployment target has terraform_export "
                            "enabled; actor forced the drift reconcile."
                        ),
                    },
                )
        try:
            result = terraform_drift_service.terraform_drift_reconcile(
                connection,
                settings,
                project_id,
                tf_payload=payload.tf_json or payload.plan_json,
                provider_account_id=payload.provider_account_id,
                actor=payload.actor,
                dry_run=payload.dry_run,
                plane=resource_plane,
            )
        except (
            ValueError,
            terraform_drift_service.TerraformDriftError,
        ) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return TerraformDriftReconcileResponse(
            **result,
            forced=forced,
            opt_in_targets=opt_in_targets,
        )


@app.get("/v1/resource-receipts", response_model=ResourceReceiptListResponse)
def list_resource_receipts(
    project_id: str,
    limit: int = 100,
    offset: int = 0,
) -> ResourceReceiptListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        items = resource_plane.list_resource_receipts(
            connection,
            project_id,
            limit=limit,
            offset=offset,
        )
        return ResourceReceiptListResponse(
            project_id=project_id,
            items=[ResourceReceiptSummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.get("/v1/worker-pools", response_model=WorkerPoolListResponse)
def list_worker_pools(project_id: str) -> WorkerPoolListResponse:
    with begin(engine) as connection:
        items = resource_plane.list_worker_pools(connection, project_id)
        return WorkerPoolListResponse(
            project_id=project_id,
            items=[WorkerPoolSummary(**item) for item in items],
        )


@app.post("/v1/worker-pools", response_model=WorkerPoolSummary, status_code=201)
def upsert_worker_pool(payload: WorkerPoolUpsertRequest) -> WorkerPoolSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            item = resource_plane.upsert_worker_pool(
                connection,
                project_id=payload.project_id,
                pool_key=payload.pool_key,
                kind=payload.kind,
                provider_account_id=payload.provider_account_id,
                current_workers=payload.current_workers,
                desired_workers=payload.desired_workers,
                min_workers=payload.min_workers,
                max_workers=payload.max_workers,
                max_queue_depth_per_worker=payload.max_queue_depth_per_worker,
                cost_per_worker_hour_usd_micros=payload.cost_per_worker_hour_usd_micros,
                capabilities=payload.capabilities,
                metadata=payload.metadata,
                actor=payload.actor,
            )
        except (ProviderAccountNotFoundError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return WorkerPoolSummary(**item)


@app.get("/v1/autoscaler/plans", response_model=ScalePlanListResponse)
def list_scale_plans(project_id: str, limit: int = 50) -> ScalePlanListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    with begin(engine) as connection:
        items = resource_plane.list_scale_plans(
            connection,
            project_id,
            limit=limit,
        )
        return ScalePlanListResponse(
            project_id=project_id,
            items=[ScalePlanSummary(**item) for item in items],
        )


@app.post("/v1/autoscaler/plans", response_model=ScalePlanRunResponse)
def run_autoscaler_plan(payload: ScalePlanRunRequest) -> ScalePlanRunResponse:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            result = resource_plane.run_autoscaler_plan(
                connection,
                project_id=payload.project_id,
                worker_pool_id=payload.worker_pool_id,
                cloud_spillover_enabled=payload.cloud_spillover_enabled,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return ScalePlanRunResponse(
            scale_plan=ScalePlanSummary(**result["scale_plan"]),
            worker_pool=WorkerPoolSummary(**result["worker_pool"]),
            budget_state=result["budget_state"],
            queue=result["queue"],
            provider_health=result["provider_health"],
            changeset_id=result["changeset_id"],
            decision_id=result["decision_id"],
        )


@app.post("/v1/resource-janitor/run", response_model=ResourceJanitorRunResponse)
def run_resource_janitor(payload: ResourceJanitorRunRequest) -> ResourceJanitorRunResponse:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            result = resource_plane.run_resource_janitor(
                connection,
                project_id=payload.project_id,
                provider_account_id=payload.provider_account_id,
                max_candidates=payload.max_candidates,
                actor=payload.actor,
            )
        except ProviderAccountNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return ResourceJanitorRunResponse(
            status=result["status"],
            project_id=result["project_id"],
            artifact_id=result["artifact_id"],
            candidate_count=result["candidate_count"],
            skipped_count=result["skipped_count"],
            estimated_monthly_savings_usd_micros=result[
                "estimated_monthly_savings_usd_micros"
            ],
            candidates=[
                ResourceJanitorCandidateSummary(**item)
                for item in result["candidates"]
            ],
            skipped=[
                ResourceJanitorSkippedSummary(**item)
                for item in result["skipped"]
            ],
            changesets=[
                ChangeSetSummary(**item)
                for item in result["changesets"]
            ],
        )


@app.get("/v1/deployments", response_model=DeploymentTargetListResponse)
def list_deployment_targets(project_id: str, limit: int = 50, offset: int = 0) -> DeploymentTargetListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        items = resource_plane.list_deployment_targets(connection, project_id, limit=limit, offset=offset)
        return DeploymentTargetListResponse(
            project_id=project_id,
            items=[DeploymentTargetSummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.post("/v1/deployments", response_model=DeploymentTargetSummary, status_code=201)
def create_deployment_target(
    payload: DeploymentTargetCreateRequest,
) -> DeploymentTargetSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            item = resource_plane.create_deployment_target(
                connection,
                project_id=payload.project_id,
                app_name=payload.app_name,
                repo=payload.repo,
                environment=payload.environment,
                provider_account_id=payload.provider_account_id,
                target_kind=payload.target_kind,
                target_ref=payload.target_ref,
                domains=payload.domains,
                required_resources=payload.required_resources,
                secret_refs=payload.secret_refs,
                health_checks=payload.health_checks,
                rollback_points=payload.rollback_points,
                actor=payload.actor,
            )
        except (ProviderAccountNotFoundError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return DeploymentTargetSummary(**item)


@app.get(
    "/v1/deployment-environment-policies",
    response_model=DeploymentEnvironmentPolicyListResponse,
)
def list_deployment_environment_policies(
    project_id: str,
    limit: int = 50,
    offset: int = 0,
) -> DeploymentEnvironmentPolicyListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        items = resource_plane.list_deployment_environment_policies(
            connection,
            project_id,
            limit=limit,
            offset=offset,
        )
        return DeploymentEnvironmentPolicyListResponse(
            project_id=project_id,
            items=[DeploymentEnvironmentPolicySummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.post(
    "/v1/deployment-environment-policies",
    response_model=DeploymentEnvironmentPolicySummary,
    status_code=201,
)
def upsert_deployment_environment_policy(
    payload: DeploymentEnvironmentPolicyUpsertRequest,
) -> DeploymentEnvironmentPolicySummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            item = resource_plane.upsert_deployment_environment_policy(
                connection,
                project_id=payload.project_id,
                environment=payload.environment,
                runtime_mode=payload.runtime_mode,
                allowed_target_kinds=payload.allowed_target_kinds,
                requires_change_set=payload.requires_change_set,
                requires_budget_reservation=payload.requires_budget_reservation,
                requires_approval=payload.requires_approval,
                requires_health_check=payload.requires_health_check,
                requires_rollback_plan=payload.requires_rollback_plan,
                rollout_notes=payload.rollout_notes,
                metadata=payload.metadata,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return DeploymentEnvironmentPolicySummary(**item)


@app.get("/v1/deployment-runs", response_model=DeploymentRunListResponse)
def list_deployment_runs(
    project_id: str,
    limit: int = 50,
) -> DeploymentRunListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    with begin(engine) as connection:
        items = resource_plane.list_deployment_runs(
            connection,
            project_id,
            limit=limit,
        )
        return DeploymentRunListResponse(
            project_id=project_id,
            items=[DeploymentRunSummary(**item) for item in items],
        )


@app.post("/v1/deployment-runs", response_model=DeploymentRunSummary, status_code=201)
def create_deployment_run(
    payload: DeploymentRunCreateRequest,
) -> DeploymentRunSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            item = resource_plane.plan_deployment_run(
                connection,
                project_id=payload.project_id,
                deployment_target_id=payload.deployment_target_id,
                commit_ref=payload.commit_ref,
                release_packet=payload.release_packet,
                expected_cost_usd_micros=payload.expected_cost_usd_micros,
                reservation_id=payload.reservation_id,
                actor=payload.actor,
            )
        except (ProviderAccountNotFoundError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return DeploymentRunSummary(**item)


@app.post(
    "/v1/deployment-runs/{deployment_run_id}/health-check",
    response_model=DeploymentRunHealthCheckResponse,
)
def run_deployment_health_check(
    deployment_run_id: str,
    payload: DeploymentRunHealthCheckRequest,
) -> DeploymentRunHealthCheckResponse:
    with begin(engine) as connection:
        project_id = project_id_for_deployment_run(connection, deployment_run_id)
        require_project_actor(
            connection,
            project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            result = resource_plane.run_deployment_health_checks(
                connection,
                project_id=project_id,
                deployment_run_id=deployment_run_id,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return DeploymentRunHealthCheckResponse(
            deployment_run=DeploymentRunSummary(**result["deployment_run"]),
            health_artifact_id=result["health_artifact_id"],
            rollback_artifact_id=result["rollback_artifact_id"],
            decision_id=result["decision_id"],
        )


@app.post(
    "/v1/deployment-runs/{deployment_run_id}/rollout/start",
    response_model=DeploymentRolloutSummary,
)
def start_deployment_rollout(
    deployment_run_id: str,
    payload: DeploymentRolloutStartRequest,
) -> DeploymentRolloutSummary:
    with begin(engine) as connection:
        project_id = project_id_for_deployment_run(connection, deployment_run_id)
        require_project_actor(
            connection, project_id, payload.actor, "providerops_manage"
        )
        try:
            rollout = deployment_rollout_engine.start_rollout(
                connection,
                project_id=project_id,
                deployment_run_id=deployment_run_id,
                actor=payload.actor,
                stages=payload.stages or None,
                thresholds=payload.thresholds,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return DeploymentRolloutSummary(**rollout)


@app.post(
    "/v1/deployment-runs/{deployment_run_id}/rollout/advance",
    response_model=DeploymentRolloutSummary,
)
def advance_deployment_rollout(
    deployment_run_id: str,
    payload: DeploymentRolloutAdvanceRequest,
) -> DeploymentRolloutSummary:
    with begin(engine) as connection:
        project_id = project_id_for_deployment_run(connection, deployment_run_id)
        require_project_actor(
            connection, project_id, payload.actor, "providerops_manage"
        )
        try:
            rollout = deployment_rollout_engine.advance_rollout(
                connection,
                project_id=project_id,
                deployment_run_id=deployment_run_id,
                actor=payload.actor,
                observation=payload.observation,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return DeploymentRolloutSummary(**rollout)


@app.post(
    "/v1/deployment-runs/{deployment_run_id}/rollout/rollback",
    response_model=DeploymentRolloutSummary,
)
def rollback_deployment_rollout(
    deployment_run_id: str,
    payload: DeploymentRolloutRollbackRequest,
) -> DeploymentRolloutSummary:
    with begin(engine) as connection:
        project_id = project_id_for_deployment_run(connection, deployment_run_id)
        require_project_actor(
            connection, project_id, payload.actor, "providerops_manage"
        )
        try:
            rollout = deployment_rollout_engine.rollback(
                connection,
                project_id=project_id,
                deployment_run_id=deployment_run_id,
                actor=payload.actor,
                reason=payload.reason,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return DeploymentRolloutSummary(**rollout)


@app.get(
    "/v1/deployment-promotions",
    response_model=DeploymentPromotionListResponse,
)
def list_deployment_promotions(
    project_id: str,
    limit: int = 50,
) -> DeploymentPromotionListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    with begin(engine) as connection:
        items = promotion_orchestrator.list_promotions(
            connection, project_id, limit=limit
        )
        return DeploymentPromotionListResponse(
            project_id=project_id,
            items=[DeploymentPromotionSummary(**item) for item in items],
        )


@app.get(
    "/v1/deployment-promotions/{promotion_id}",
    response_model=DeploymentPromotionSummary,
)
def get_deployment_promotion(promotion_id: str) -> DeploymentPromotionSummary:
    with begin(engine) as connection:
        row = connection.execute(
            text("SELECT project_id FROM deployment_promotions WHERE id = :id"),
            {"id": promotion_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Deployment promotion not found.")
        return DeploymentPromotionSummary(
            **promotion_orchestrator.get_promotion_detail(
                connection, row["project_id"], promotion_id
            )
        )


@app.post(
    "/v1/deployment-promotions",
    response_model=DeploymentPromotionSummary,
    status_code=201,
)
def create_deployment_promotion(
    payload: DeploymentPromotionStartRequest,
) -> DeploymentPromotionSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "providerops_manage",
        )
        try:
            item = promotion_orchestrator.promote(
                connection,
                project_id=payload.project_id,
                staging_target_id=payload.staging_target_id,
                production_target_id=payload.production_target_id,
                commit_ref=payload.commit_ref,
                actor=payload.actor,
                staging_reservation_id=payload.staging_reservation_id,
                production_reservation_id=payload.production_reservation_id,
                staging_expected_cost_usd_micros=(
                    payload.staging_expected_cost_usd_micros
                ),
                production_expected_cost_usd_micros=(
                    payload.production_expected_cost_usd_micros
                ),
                release_packet=payload.release_packet,
            )
        except (ValueError, IntegrationConfigurationError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return DeploymentPromotionSummary(**item)


@app.post(
    "/v1/deployment-promotions/{promotion_id}/advance",
    response_model=DeploymentPromotionSummary,
)
def advance_deployment_promotion(
    promotion_id: str,
    payload: DeploymentPromotionAdvanceRequest,
) -> DeploymentPromotionSummary:
    with begin(engine) as connection:
        row = connection.execute(
            text("SELECT project_id FROM deployment_promotions WHERE id = :id"),
            {"id": promotion_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Deployment promotion not found.")
        require_project_actor(
            connection, row["project_id"], payload.actor, "providerops_manage"
        )
        try:
            item = promotion_orchestrator.advance_after_staging_gate(
                connection,
                project_id=row["project_id"],
                promotion_id=promotion_id,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return DeploymentPromotionSummary(**item)


@app.get("/v1/provider-health", response_model=ProviderHealthListResponse)
def list_provider_health(
    project_id: str,
    provider_account_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> ProviderHealthListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        items = resource_plane.list_provider_health(
            connection,
            project_id,
            provider_account_id=provider_account_id,
            limit=limit,
            offset=offset,
        )
        return ProviderHealthListResponse(
            project_id=project_id,
            items=[ProviderHealthSummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.get("/v1/drift-reports", response_model=DriftReportListResponse)
def list_drift_reports(
    project_id: str,
    limit: int = 100,
    offset: int = 0,
) -> DriftReportListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        items = resource_plane.list_drift_reports(
            connection,
            project_id,
            limit=limit,
            offset=offset,
        )
        return DriftReportListResponse(
            project_id=project_id,
            items=[DriftReportSummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.get("/v1/reconciliation-runs", response_model=ReconciliationRunListResponse)
def list_reconciliation_runs(
    project_id: str,
    limit: int = 100,
    offset: int = 0,
) -> ReconciliationRunListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")
    with begin(engine) as connection:
        items = resource_plane.list_reconciliation_runs(
            connection,
            project_id,
            limit=limit,
            offset=offset,
        )
        return ReconciliationRunListResponse(
            project_id=project_id,
            items=[ReconciliationRunSummary(**item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.get("/v1/comms/policies", response_model=CommsChannelPolicyListResponse)
def list_comms_channel_policies(
    project_id: str,
) -> CommsChannelPolicyListResponse:
    with begin(engine) as connection:
        items = comms.list_channel_policies(connection, project_id)
        return CommsChannelPolicyListResponse(
            project_id=project_id,
            items=[comms_channel_policy_summary_from_row(item) for item in items],
        )


@app.post(
    "/v1/comms/policies",
    response_model=CommsChannelPolicySummary,
    status_code=201,
)
def upsert_comms_channel_policy(
    payload: CommsChannelPolicyUpsertRequest,
) -> CommsChannelPolicySummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "comms_manage",
        )
        try:
            item = comms.upsert_channel_policy(
                connection,
                payload.project_id,
                provider=payload.provider,
                delivery_mode=payload.delivery_mode,
                allowed_data_boundaries=list(payload.allowed_data_boundaries),
                actor=payload.actor,
                rollout_notes=payload.rollout_notes,
                metadata=payload.metadata,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return comms_channel_policy_summary_from_row(item)


@app.get("/v1/comms/outbox", response_model=OutboxMessageListResponse)
def list_outbox_messages(project_id: str, limit: int = 50) -> OutboxMessageListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")

    with begin(engine) as connection:
        items = comms.list_outbox_messages(connection, project_id, limit)
        return OutboxMessageListResponse(
            project_id=project_id,
            items=[outbox_message_summary_from_row(item) for item in items],
        )


@app.post("/v1/comms/outbox", response_model=OutboxMessageSummary, status_code=201)
def create_outbox_message(payload: OutboxMessageCreateRequest) -> OutboxMessageSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "comms_manage",
        )
        item = comms.create_outbox_message(
            connection,
            payload.project_id,
            payload.channel,
            payload.subject,
            payload.body,
            payload.dedupe_key,
            payload.actor,
            payload.data_boundary,
        )
        return outbox_message_summary_from_row(item)


@app.post("/v1/comms/outbox/{message_id}/queue", response_model=JobSummary, status_code=201)
def queue_outbox_delivery(message_id: str, payload: OutboxQueueRequest) -> JobSummary:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "comms_manage",
            )
            job = comms.queue_outbox_delivery(
                connection,
                payload.project_id,
                message_id,
                payload.reservation_id,
                payload.dedupe_key,
                payload.actor,
                payload.priority,
                payload.max_attempts,
                payload.urgency,
            )
        except OutboxMessageNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return JobSummary(**job)


@app.post("/v1/comms/outbox/{message_id}/deliver", response_model=OutboxDeliveryResponse)
def deliver_outbox_message(
    message_id: str, payload: OutboxQueueRequest
) -> OutboxDeliveryResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "comms_manage",
            )
            result = comms.deliver_outbox_message(
                connection,
                payload.project_id,
                message_id,
                payload.reservation_id,
                payload.dedupe_key,
                payload.created_by_job_id,
            )
        except OutboxMessageNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidCreatedByJobError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return OutboxDeliveryResponse(**result)


@app.get("/v1/comms/inbox", response_model=InboxMessageListResponse)
def list_inbox_messages(project_id: str, limit: int = 50) -> InboxMessageListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")

    with begin(engine) as connection:
        items = comms.list_inbox_messages(connection, project_id, limit)
        return InboxMessageListResponse(
            project_id=project_id,
            items=[inbox_message_summary_from_row(item) for item in items],
        )


@app.get("/v1/comms/inbox/ack-state")
def list_inbox_ack_state(project_id: str, limit: int = 50, pending_only: bool = False) -> dict:
    """The no-silent-drops ledger: every inbound's acknowledgment state + transition history.
    pending_only=true surfaces any inbound that has not yet reached a terminal ack (spec §D)."""
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    with begin(engine) as connection:
        return {
            "project_id": project_id,
            "items": comms.list_ack_state(
                connection, project_id, limit, pending_only=pending_only
            ),
        }


@app.post("/v1/comms/slack/render")
def render_slack_message(payload: SlackRenderRequest) -> dict:
    """Render a Slack message with persona prefix/signature and resolve the department channel
    (spec §D Slack per-department channels + persona rendering)."""
    channel = department_channel(payload.department) if payload.department else None
    rendered = render_persona_message(
        payload.persona,
        payload.role,
        payload.subject,
        payload.body,
        signature=payload.signature,
    )
    return {"channel": channel, "text": rendered}


@app.post("/v1/comms/slack/digest")
def batch_slack_digests(payload: SlackDigestBatchRequest) -> dict:
    """Collapse draft Slack outbox messages into one digest per channel (spec §D digest batching)."""
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "comms_manage"
        )
        return slack_comms.batch_digests(
            connection,
            payload.project_id,
            payload.actor,
            channel=payload.channel,
            min_batch=payload.min_batch,
        )


@app.post("/v1/comms/inbox/ingest", response_model=InboxMessageSummary, status_code=201)
def ingest_inbox_message(payload: InboxMessageIngestRequest) -> InboxMessageSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "comms_manage",
        )
        item = comms.ingest_inbox_message(
            connection,
            payload.project_id,
            payload.channel,
            payload.body,
            payload.actor,
            subject=payload.subject,
            raw_ref=payload.raw_ref,
            data_boundary=payload.data_boundary,
        )
        parsed = item.get("parsed") or {}
        if parsed.get("kind") == "decision_reply":
            comms.advance_inbox(
                connection,
                payload.project_id,
                item["id"],
                status="parsed",
                ack="parsed",
                terminal=False,
                actor=payload.actor,
            )
            parse_error = parsed.get("parse_error")
            decision_id = parsed.get("decision_id")
            action = parsed.get("action")
            if parse_error or not isinstance(decision_id, str) or not isinstance(action, str):
                comms.deadletter_inbox_message(
                    connection,
                    payload.project_id,
                    item["id"],
                    actor=payload.actor,
                    reason=str(parse_error or "decision_reply_missing_id_or_action"),
                    payload={"parsed": parsed},
                    remediation=(
                        parsed.get("remediation")
                        if isinstance(parsed.get("remediation"), str)
                        else None
                    ),
                )
                return inbox_message_summary_from_row(
                    reload_inbox_message(connection, item["id"])
                )
            comms.advance_inbox(
                connection,
                payload.project_id,
                item["id"],
                status="routed",
                ack="routed",
                terminal=False,
                actor=payload.actor,
                route_kind="decision",
                target_ref=decision_id,
            )
            try:
                decision_row = decisions.transition(
                    connection,
                    decision_id,
                    action=action,
                    actor=payload.actor,
                    note=parsed.get("note") if isinstance(parsed.get("note"), str) else None,
                    delegate_to=(
                        parsed.get("delegate_to")
                        if isinstance(parsed.get("delegate_to"), str)
                        else None
                    ),
                    external_ref=payload.raw_ref,
                )
            except (
                DecisionNotFoundError,
                InvalidDecisionTransitionError,
                ProjectMemberNotFoundError,
                ProjectMemberPermissionError,
                ValueError,
            ) as error:
                comms.deadletter_inbox_message(
                    connection,
                    payload.project_id,
                    item["id"],
                    actor=payload.actor,
                    reason=str(error),
                    payload={"parsed": parsed},
                )
                return inbox_message_summary_from_row(
                    reload_inbox_message(connection, item["id"])
                )

            comms.mark_inbox_actioned(
                connection,
                payload.project_id,
                item["id"],
                actor=payload.actor,
                decision_id=decision_id,
                outcome_status=decision_row["status"],
            )
            return inbox_message_summary_from_row(
                reload_inbox_message(connection, item["id"])
            )
        return inbox_message_summary_from_row(item)


@app.post("/v1/docs/update", response_model=JobSummary, status_code=201)
def enqueue_docs_update(payload: DocsUpdateRequest) -> JobSummary:
    """Manually enqueue the same bounded job used by completion-event triggers."""
    with begin(engine) as connection:
        try:
            result = docs_updates.enqueue(
                connection,
                project_id=payload.project_id,
                event_id=payload.event_id,
                event_type=payload.event_type,
                entity_type=payload.entity_type,
                entity_id=payload.entity_id,
                task_id=payload.task_id,
                data_boundary=payload.data_boundary,
                actor=payload.actor,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return JobSummary(**result)


@app.post("/v1/docs/update/execute")
def execute_docs_update(payload: DocsUpdateExecuteRequest) -> dict[str, Any]:
    """Worker-only execution boundary for a leased ``docs.update`` job."""
    with begin(engine) as connection:
        try:
            return docs_updates.run_update(
                connection,
                project_id=payload.project_id,
                event_id=payload.event_id,
                event_type=payload.event_type,
                entity_type=payload.entity_type,
                entity_id=payload.entity_id,
                task_id=payload.task_id,
                data_boundary=payload.data_boundary,
                created_by_job_id=payload.created_by_job_id,
            )
        except ValueError as error:
            return {
                "status": "failed",
                "retryable": False,
                "message": str(error),
            }


@app.post("/v1/jobs", response_model=JobSummary, status_code=201)
def create_job(payload: JobCreateRequest) -> JobSummary:
    with begin(engine) as connection:
        job_payload = {
            **payload.payload,
            **(
                {"requires_reservation": True}
                if payload.requires_reservation
                else {}
            ),
            **(
                {"reservation_id": payload.reservation_id}
                if payload.reservation_id
                else {}
            ),
        }
        try:
            result = jobs.create_job(
                connection,
                project_id=payload.project_id,
                task_id=payload.task_id,
                kind=payload.kind,
                payload=job_payload,
                priority=payload.priority,
                max_attempts=payload.max_attempts,
                run_after_seconds=payload.run_after_seconds,
                data_boundary=payload.data_boundary,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if payload.reservation_id:
            budget.attach_job(
                connection,
                payload.project_id,
                payload.reservation_id,
                result["id"],
            )
        return JobSummary(**result)


@app.post("/v1/bootstrap/seed")
def bootstrap_seed(actor: str = "ceo") -> dict[str, str]:
    if not settings.bootstrap_seed_api_enabled:
        record_policy_denial(
            settings.default_project_id,
            actor,
            "member_manage",
            "BOSS_BOOTSTRAP_SEED_API_ENABLED is false.",
        )
        raise HTTPException(
            status_code=403,
            detail="Bootstrap seed API is disabled. Use the local make seed command or explicitly enable BOSS_BOOTSTRAP_SEED_API_ENABLED.",
        )
    with begin(engine) as connection:
        project_exists = connection.execute(
            text("SELECT 1 FROM projects WHERE id = :project_id"),
            {"project_id": settings.default_project_id},
        ).first()
        if project_exists:
            require_project_actor(
                connection,
                settings.default_project_id,
                actor,
                "member_manage",
            )
    seed()
    with begin(engine) as connection:
        emit_event(
            connection,
            settings.default_project_id,
            "bootstrap.seed_executed",
            actor,
            "project",
            settings.default_project_id,
            {"source": "api"},
        )
    return {"status": "seeded", "actor": actor}


@app.get("/v1/budget/allocations", response_model=BudgetAllocationListResponse)
def list_budget_allocations(project_id: str) -> BudgetAllocationListResponse:
    with begin(engine) as connection:
        allocations = budget.list_allocations(connection, project_id)
        return BudgetAllocationListResponse(
            project_id=project_id,
            items=[
                budget_allocation_summary_from_service(allocation)
                for allocation in allocations
            ],
        )


@app.post(
    "/v1/budget/allocations",
    response_model=BudgetAllocationSummary,
    status_code=201,
)
def create_budget_allocation(
    payload: BudgetAllocationCreateRequest,
) -> BudgetAllocationSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "budget_manage",
        )
        try:
            allocation = budget.create_allocation(
                connection,
                project_id=payload.project_id,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                amount_usd_micros=payload.amount_usd_micros,
                period_start=payload.period_start,
                period_end=payload.period_end,
                actor=payload.actor,
                parent_allocation_id=payload.parent_allocation_id,
            )
        except ValueError as error:
            record_budget_denial_event(
                payload.project_id,
                payload.actor,
                "budget.allocation_denied",
                (
                    "budget_allocation"
                    if payload.parent_allocation_id
                    else "project"
                ),
                payload.parent_allocation_id or payload.project_id,
                {
                    "requested_usd_micros": payload.amount_usd_micros,
                    "parent_allocation_id": payload.parent_allocation_id,
                    "scope_type": payload.scope_type,
                    "scope_id": payload.scope_id,
                    "reason": str(error),
                },
            )
            raise HTTPException(status_code=409, detail=str(error)) from error
        return budget_allocation_summary_from_service(allocation)


@app.get("/v1/budget/status", response_model=BudgetStatusResponse)
def budget_status(scope_type: str, scope_id: str) -> BudgetStatusResponse:
    if scope_type != "project":
        raise HTTPException(status_code=400, detail="Only project scope is supported.")

    with begin(engine) as connection:
        snapshot = budget.status_for_project(connection, scope_id)
        return BudgetStatusResponse(
            scope_type=scope_type,
            scope_id=scope_id,
            budget_state=snapshot["budget_state"],
            cap_usd_micros=snapshot["cap_usd_micros"],
            reserved_usd_micros=snapshot["reserved_usd_micros"],
            actual_usd_micros=snapshot["actual_usd_micros"],
            available_usd_micros=snapshot["available_usd_micros"],
            committed_usd_micros=snapshot["committed_usd_micros"],
            burn_approx_usd_micros=snapshot["burn_approx_usd_micros"],
            burn_window_hours=snapshot["burn_window_hours"],
            burn_rate_usd_micros_per_hour=snapshot[
                "burn_rate_usd_micros_per_hour"
            ],
            forecasted_24h_usd_micros=snapshot["forecasted_24h_usd_micros"],
            time_to_cap_seconds=snapshot["time_to_cap_seconds"],
            utilization_ratio=snapshot["utilization_ratio"],
            active_reservations_count=snapshot["active_reservations_count"],
            top_cost_drivers=snapshot["top_cost_drivers"],
            operational_intensity=snapshot.get("operational_intensity"),
        )


@app.post("/v1/budget/set_intensity")
def budget_set_intensity(payload: BudgetSetIntensityRequest) -> dict:
    """CEO Operational Intensity slider (0-100) → spend envelope + scaling limits."""
    if payload.scope_type != "project":
        raise HTTPException(status_code=400, detail="Only project scope is supported.")
    with begin(engine) as connection:
        result = budget.set_intensity(
            connection, payload.scope_type, payload.scope_id, payload.intensity, payload.actor
        )
    return {"scope_type": payload.scope_type, "scope_id": payload.scope_id, **result}


@app.post("/v1/canaries/run")
def canaries_run(payload: CanaryRunRequest) -> dict:
    """Run the canary suite (exactly-once / no-drop / job-lifecycle / budget) and persist results."""
    from .services import canaries as canaries_svc
    return canaries_svc.run_canary_suite(engine, settings, payload.project_id, payload.actor)


@app.get("/v1/canaries")
def canaries_list(project_id: str, limit: int = 20) -> dict:
    from .services import canaries as canaries_svc
    return {"items": canaries_svc.list_canary_runs(engine, project_id, limit)}


@app.post("/v1/evals/run")
def evals_run(payload: EvalRunRequest) -> dict:
    """Run the eval-harness regression suite (routing / policy / factory-gate / autonomy-ladder)
    and persist results. Gates self-improvement rollouts (spec §C)."""
    from .services import eval_harness
    return eval_harness.run_eval_suite(
        engine, settings, payload.project_id, payload.actor, payload.suite)


@app.get("/v1/evals")
def evals_list(project_id: str, limit: int = 20) -> dict:
    from .services import eval_harness
    return {"items": eval_harness.list_eval_runs(engine, project_id, limit)}


@app.post("/v1/provider-health/poll")
def provider_health_poll(payload: ProviderHealthPollRequest) -> dict:
    """Live provider-health polling: snapshot every model lane's health into provider_health so the
    router fallback chain can be validated before an incident (spec §C)."""
    with begin(engine) as connection:
        return router_health.poll_provider_health(connection, payload.project_id, payload.actor)


@app.post("/v1/router/rationale", status_code=201)
def router_rationale(payload: RouterRationaleRequest) -> dict:
    """Persist the router's decision (selected lane + ranked fallback chain + rejected sources)
    as a durable `router_rationale` artifact — the structured 'why this lane' (spec §C)."""
    with begin(engine) as connection:
        try:
            return router_health.record_rationale(
                connection,
                payload.project_id,
                skill_id=payload.skill_id,
                task_kind=payload.task_kind,
                workload_class=payload.workload_class,
                risk_tier=payload.risk_tier,
                actor=payload.actor,
            )
        except SkillNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/v1/anomalies/scan")
def anomalies_scan(payload: AnomalyScanRequest) -> dict:
    """Scan for operational cost anomalies (retry storms, spend spikes) → incident + auto-lite."""
    from .services import anomaly_detector
    return anomaly_detector.scan(engine, settings, payload.project_id, auto_lite=payload.auto_lite)


@app.get("/v1/trace")
def trace_run(run_id: str) -> dict:
    """Reconstruct a code-task run's full timeline — the run + its fail-closed quality gate + every
    event referencing it (jobs, model/tool calls, receipts, decisions). The 'why did we do that'
    trace (spec §26 replay/observability), assembled from existing links — no per-call threading."""
    with begin(engine) as connection:
        run = connection.execute(
            text(
                "SELECT id, project_id, task_id, status, phase, title, state, pull_request_url, "
                "decision_id, error, created_at, updated_at FROM code_task_runs WHERE id = :r"
            ),
            {"r": run_id},
        ).mappings().first()
        if not run:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        gate = connection.execute(
            text(
                "SELECT id, status, gates_passed, gates_failed, evidence, created_at "
                "FROM quality_gates WHERE code_task_run_id = :r"
            ),
            {"r": run_id},
        ).mappings().first()
        timeline = connection.execute(
            text(
                "SELECT event_type, actor, entity_type, entity_id, data, created_at FROM events "
                "WHERE project_id = :p AND (entity_id = :r OR data::text LIKE :like) "
                "ORDER BY created_at ASC"
            ),
            {"p": run["project_id"], "r": run_id, "like": f"%{run_id}%"},
        ).mappings().all()
    return {
        "run": dict(run),
        "quality_gate": dict(gate) if gate else None,
        "timeline": [dict(e) for e in timeline],
        "event_count": len(timeline),
    }


@app.get("/v1/policy-evals")
def policy_evals_list(project_id: str, limit: int = 50, decision: Optional[str] = None) -> dict:
    """The policy-evaluation audit trail (allow/deny/defer at every enforcement boundary)."""
    clause = "AND decision = :decision" if decision else ""
    params: dict = {"p": project_id, "l": limit}
    if decision:
        params["decision"] = decision
    with begin(engine) as connection:
        rows = connection.execute(
            text(
                f"""
                SELECT id, boundary, action, decision, reason, detail, created_at
                FROM policy_eval_log WHERE project_id = :p {clause}
                ORDER BY created_at DESC LIMIT :l
                """
            ),
            params,
        ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@app.get("/v1/budget/forecast", response_model=BudgetForecastResponse)
def budget_forecast(
    scope_type: str,
    scope_id: str,
    horizon_hours: int = 24,
) -> BudgetForecastResponse:
    if scope_type != "project":
        raise HTTPException(status_code=400, detail="Only project scope is supported.")

    with begin(engine) as connection:
        try:
            forecast = budget.forecast(connection, scope_id, horizon_hours)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return BudgetForecastResponse(**forecast)


@app.post("/v1/budget/quote", response_model=BudgetQuoteResponse)
def quote_budget(payload: BudgetQuoteRequest) -> BudgetQuoteResponse:
    with begin(engine) as connection:
        result = budget.quote(
            connection,
            payload.project_id,
            payload.category,
            payload.amount_usd_micros,
            payload.reason,
        )
        return BudgetQuoteResponse(**result)


@app.post("/v1/budget/reserve", response_model=BudgetReserveResponse, status_code=201)
def reserve_budget(payload: BudgetReserveRequest) -> BudgetReserveResponse:
    with begin(engine) as connection:
        result = budget.reserve(
            connection,
            payload.project_id,
            payload.category,
            payload.amount_usd_micros,
            payload.reason,
            payload.expires_in_minutes,
            payload.job_id,
            payload.allocation_id,
        )
        return BudgetReserveResponse(**result)


@app.post("/v1/budget/release", response_model=BudgetReleaseResponse)
def release_budget(payload: BudgetReleaseRequest) -> BudgetReleaseResponse:
    with begin(engine) as connection:
        result = budget.release(
            connection,
            payload.project_id,
            payload.reservation_id,
            payload.reason,
        )
        return BudgetReleaseResponse(**result)


@app.post("/v1/budget/consume", response_model=BudgetConsumeResponse)
def consume_budget(payload: BudgetConsumeRequest) -> BudgetConsumeResponse:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "budget_manage",
        )
        try:
            result = budget.consume(
                connection,
                payload.project_id,
                payload.reservation_id,
                payload.receipt_id,
                payload.category,
                payload.skill_or_model_id,
                payload.estimated_usd_micros,
                payload.actual_usd_micros,
                payload.actor,
            )
        except ValueError as error:
            record_budget_denial_event(
                payload.project_id,
                payload.actor,
                "budget.consume_denied",
                "budget_reservation",
                payload.reservation_id,
                {
                    "receipt_id": payload.receipt_id,
                    "category": payload.category,
                    "skill_or_model_id": payload.skill_or_model_id,
                    "reason": str(error),
                },
            )
            raise HTTPException(status_code=409, detail=str(error)) from error
        return BudgetConsumeResponse(
            status=result["status"],
            reservation_id=result["reservation_id"],
            budget_state=result["budget_state"],
            message=result["message"],
            cost_receipt=cost_receipt_summary_from_row(result["cost_receipt"]),
        )


@app.post("/v1/worker/poll", response_model=WorkerPollResponse)
def worker_poll(payload: WorkerPollRequest) -> WorkerPollResponse:
    with begin(engine) as connection:
        job = jobs.claim_job(
            connection,
            payload.worker_id,
            payload.capabilities,
            payload.lease_ttl_seconds,
        )
        if not job:
            return WorkerPollResponse(status="idle", job=None)
        return WorkerPollResponse(status="leased", job=LeasedJob(**job))


@app.post("/v1/worker/heartbeat", response_model=WorkerHeartbeatResponse)
def worker_heartbeat(payload: WorkerHeartbeatRequest) -> WorkerHeartbeatResponse:
    with begin(engine) as connection:
        try:
            result = jobs.heartbeat(
                connection,
                payload.worker_id,
                payload.job_id,
                payload.lease_token,
                payload.lease_ttl_seconds,
            )
        except LeaseNotFoundError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return WorkerHeartbeatResponse(**result)


@app.post("/v1/worker/complete", response_model=WorkerCompleteResponse)
def worker_complete(payload: WorkerCompleteRequest) -> WorkerCompleteResponse:
    with begin(engine) as connection:
        try:
            result = jobs.complete(
                connection,
                payload.worker_id,
                payload.job_id,
                payload.lease_token,
                payload.result,
            )
        except LeaseNotFoundError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return WorkerCompleteResponse(**result)


@app.post("/v1/worker/fail", response_model=WorkerFailResponse)
def worker_fail(payload: WorkerFailRequest) -> WorkerFailResponse:
    with begin(engine) as connection:
        try:
            result = jobs.fail(
                connection,
                payload.worker_id,
                payload.job_id,
                payload.lease_token,
                payload.error,
                payload.retryable,
                payload.retry_delay_seconds,
            )
        except LeaseNotFoundError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return WorkerFailResponse(**result)


@app.get("/v1/decisions/inbox", response_model=DecisionInboxResponse)
def decision_inbox(project_id: str) -> DecisionInboxResponse:
    with begin(engine) as connection:
        decisions.expire_overdue(connection, project_id)
        rows = connection.execute(
            text(
                """
                SELECT id, task_id, question, status, risk_tier, default_action,
                       recommendation, options, cost_summary, deadline_at,
                       approved_by, approved_at, delegated_to, delegated_by,
                       delegated_at, revision_requested_by, revision_requested_at,
                       executed_by, executed_at, resolution_note, external_ref,
                       created_at, updated_at
                FROM decisions
                WHERE project_id = :project_id
                ORDER BY
                    CASE status
                        WHEN 'pending' THEN 0
                        WHEN 'delegated' THEN 1
                        WHEN 'approved' THEN 2
                        ELSE 3
                    END,
                    risk_tier DESC,
                    created_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return DecisionInboxResponse(
            project_id=project_id,
            items=[decision_card_from_row(row) for row in rows],
        )


@app.post("/v1/decisions/digests", response_model=DecisionDigestResponse, status_code=201)
def create_decision_digest(payload: DecisionDigestRequest) -> DecisionDigestResponse:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "decision_resolve",
        )
        result = decisions.create_digest(
            connection,
            project_id=payload.project_id,
            actor=payload.actor,
            max_items=payload.max_items,
            low_risk_max=payload.low_risk_max,
            urgent_risk_min=payload.urgent_risk_min,
        )
        outbox_message_id = None
        if payload.channel:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "comms_manage",
            )
            outbox = comms.create_outbox_message(
                connection,
                payload.project_id,
                payload.channel,
                "BOSS decision digest",
                result["body"],
                payload.dedupe_key or f"decision-digest-{result['artifact_id']}",
                payload.actor,
                payload.data_boundary,
            )
            outbox_message_id = outbox["id"]
        return DecisionDigestResponse(
            status=result["status"],
            artifact_id=result["artifact_id"],
            outbox_message_id=outbox_message_id,
            decision_count=result["decision_count"],
            urgent_count=result["urgent_count"],
            batched_count=result["batched_count"],
            body=result["body"],
        )


@app.post("/v1/decisions/{decision_id}/approve", response_model=DecisionCard)
def approve_decision(
    decision_id: str, payload: DecisionActionRequest
) -> DecisionCard:
    with begin(engine) as connection:
        # Act as your authenticated identity: an API-key principal is authoritative, so a claimed
        # actor from the console (e.g. "executive_console") yields to it instead of 403-ing.
        payload.actor = current_api_principal.get() or payload.actor
        require_decision_actor_binding(connection, decision_id, payload.actor)
        try:
            row = decisions.transition(
                connection,
                decision_id,
                action="approve",
                actor=payload.actor,
                note=payload.note,
                delegate_to=payload.delegate_to,
                external_ref=payload.external_ref,
            )
        except DecisionNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidDecisionTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            raise_decision_actor_error(decision_id, payload.actor, error)
        return decision_card_from_row(row)


@app.post("/v1/decisions/{decision_id}/reject", response_model=DecisionCard)
def reject_decision(
    decision_id: str, payload: DecisionActionRequest
) -> DecisionCard:
    with begin(engine) as connection:
        # Act as your authenticated identity: an API-key principal is authoritative, so a claimed
        # actor from the console (e.g. "executive_console") yields to it instead of 403-ing.
        payload.actor = current_api_principal.get() or payload.actor
        require_decision_actor_binding(connection, decision_id, payload.actor)
        try:
            row = decisions.transition(
                connection,
                decision_id,
                action="reject",
                actor=payload.actor,
                note=payload.note,
                delegate_to=payload.delegate_to,
                external_ref=payload.external_ref,
            )
        except DecisionNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidDecisionTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            raise_decision_actor_error(decision_id, payload.actor, error)
        return decision_card_from_row(row)


@app.post("/v1/decisions/{decision_id}/revise", response_model=DecisionCard)
def revise_decision(
    decision_id: str, payload: DecisionActionRequest
) -> DecisionCard:
    with begin(engine) as connection:
        # Act as your authenticated identity: an API-key principal is authoritative, so a claimed
        # actor from the console (e.g. "executive_console") yields to it instead of 403-ing.
        payload.actor = current_api_principal.get() or payload.actor
        require_decision_actor_binding(connection, decision_id, payload.actor)
        try:
            row = decisions.transition(
                connection,
                decision_id,
                action="revise",
                actor=payload.actor,
                note=payload.note,
                delegate_to=payload.delegate_to,
                external_ref=payload.external_ref,
            )
        except DecisionNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidDecisionTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            raise_decision_actor_error(decision_id, payload.actor, error)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return decision_card_from_row(row)


@app.post("/v1/decisions/{decision_id}/delegate", response_model=DecisionCard)
def delegate_decision(
    decision_id: str, payload: DecisionActionRequest
) -> DecisionCard:
    with begin(engine) as connection:
        # Act as your authenticated identity: an API-key principal is authoritative, so a claimed
        # actor from the console (e.g. "executive_console") yields to it instead of 403-ing.
        payload.actor = current_api_principal.get() or payload.actor
        require_decision_actor_binding(connection, decision_id, payload.actor)
        try:
            row = decisions.transition(
                connection,
                decision_id,
                action="delegate",
                actor=payload.actor,
                note=payload.note,
                delegate_to=payload.delegate_to,
                external_ref=payload.external_ref,
            )
        except DecisionNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidDecisionTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            raise_decision_actor_error(decision_id, payload.actor, error)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return decision_card_from_row(row)


@app.post("/v1/decisions/{decision_id}/execute", response_model=DecisionCard)
def execute_decision(
    decision_id: str, payload: DecisionActionRequest
) -> DecisionCard:
    with begin(engine) as connection:
        # Act as your authenticated identity: an API-key principal is authoritative, so a claimed
        # actor from the console (e.g. "executive_console") yields to it instead of 403-ing.
        payload.actor = current_api_principal.get() or payload.actor
        require_decision_actor_binding(connection, decision_id, payload.actor)
        try:
            row = decisions.transition(
                connection,
                decision_id,
                action="execute",
                actor=payload.actor,
                note=payload.note,
                delegate_to=payload.delegate_to,
                external_ref=payload.external_ref,
            )
        except DecisionNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidDecisionTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            raise_decision_actor_error(decision_id, payload.actor, error)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return decision_card_from_row(row)


@app.post("/v1/openai/plans", response_model=OpenAIPlanResponse)
def create_openai_plan(payload: OpenAIPlanRequest) -> OpenAIPlanResponse:
    with begin(engine) as connection:
        try:
            result = software_factory.create_plan_artifact(
                connection,
                payload.project_id,
                payload.reservation_id,
                payload.dedupe_key,
                payload.prompt,
                payload.system_prompt,
                payload.created_by_job_id,
                payload.agent_module_id,
                payload.task_id,
            )
        except AgentModuleNotFoundError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return OpenAIPlanResponse(**result)


@app.post("/v1/software-factory/proposals", response_model=PullRequestProposalResponse)
def create_pull_request_proposal(
    payload: PullRequestProposalRequest,
) -> PullRequestProposalResponse:
    with begin(engine) as connection:
        try:
            result = software_factory.propose_pull_request(
                connection,
                payload.project_id,
                payload.task_id,
                payload.reservation_id,
                payload.dedupe_key,
                payload.title,
                payload.branch_name,
                payload.summary,
                payload.files,
                payload.created_by_job_id,
                payload.agent_module_id,
            )
        except AgentModuleNotFoundError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return PullRequestProposalResponse(**result)


@app.post(
    "/v1/pipeline-config/proposals",
    response_model=CIConfigProposalResponse,
)
def propose_ci_config(
    payload: CIConfigProposalRequest,
) -> CIConfigProposalResponse:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "task_create",
        )
        try:
            descriptor = software_factory.pipeline.discover(payload.repo)
            descriptor_data = descriptor.to_dict()
            emit_event(
                connection,
                payload.project_id,
                "pipeline.discovered",
                payload.actor,
                "repository",
                payload.repo,
                descriptor_data,
            )
            if descriptor.configured:
                return CIConfigProposalResponse(
                    status="existing_pipeline",
                    pipeline_descriptor=descriptor_data,
                )
            generated = ci_config_generator.generate(
                language=payload.language,
                framework=payload.framework,
                target=payload.target,
            )
            proposal = software_factory.propose_pull_request(
                connection,
                payload.project_id,
                payload.task_id,
                payload.reservation_id,
                payload.dedupe_key,
                payload.title,
                payload.branch_name,
                (
                    "Add deterministic CI build/test/deploy-evidence configuration. "
                    "Provider deployment remains governed by BOSS ChangeSets."
                ),
                [FileChange(path=generated["path"], content=generated["content"])],
                payload.created_by_job_id,
            )
        except (ValueError, IntegrationConfigurationError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return CIConfigProposalResponse(
            status=proposal["status"],
            pipeline_descriptor=descriptor_data,
            generated_config={
                key: value for key, value in generated.items() if key != "content"
            },
            proposal=PullRequestProposalResponse(**proposal),
        )


@app.post("/v1/software-factory/merges", response_model=PullRequestMergeResponse)
def merge_pull_request(payload: PullRequestMergeRequest) -> PullRequestMergeResponse:
    with begin(engine) as connection:
        result = software_factory.merge_pull_request(
            connection,
            payload.project_id,
            None,
            payload.decision_id,
            payload.dedupe_key,
            payload.pull_request_number,
            payload.pull_request_url,
            payload.title,
            payload.branch_name,
            payload.created_by_job_id,
        )
        return PullRequestMergeResponse(**result)


@app.post(
    "/v1/software-factory/deployments", response_model=SandboxDeploymentResponse
)
def create_sandbox_deployment(
    payload: SandboxDeploymentRequest,
) -> SandboxDeploymentResponse:
    with begin(engine) as connection:
        result = software_factory.create_deployment(
            connection,
            payload.project_id,
            None,
            payload.decision_id,
            payload.dedupe_key,
            payload.merged_sha,
            payload.pull_request_url,
            payload.title,
            payload.branch_name,
            payload.environment,
            payload.created_by_job_id,
        )
        return SandboxDeploymentResponse(**result)


@app.get(
    "/v1/software-factory/rollouts",
    response_model=DeploymentRolloutListResponse,
)
def list_deployment_rollouts(
    project_id: str = Query(...),
    limit: int = Query(default=20, ge=1, le=100),
) -> DeploymentRolloutListResponse:
    with begin(engine) as connection:
        rows = software_factory.list_deployment_rollouts(connection, project_id, limit)
        return DeploymentRolloutListResponse(
            project_id=project_id,
            items=[DeploymentRolloutSummary(**row) for row in rows],
        )


@app.post(
    "/v1/infra/droplets/requests",
    response_model=InfraProvisionReviewResponse,
    status_code=201,
)
def request_infra_provision_review(
    payload: InfraProvisionReviewRequest,
) -> InfraProvisionReviewResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "infra_provision",
            )
            result = infrastructure.request_droplet_provision(
                connection,
                project_id=payload.project_id,
                task_id=payload.task_id,
                reservation_id=payload.reservation_id,
                title=payload.title,
                region=payload.region,
                size=payload.size,
                ttl_minutes=payload.ttl_minutes,
                tags=payload.tags,
                actor=payload.actor,
                data_boundary=payload.data_boundary,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return InfraProvisionReviewResponse(**result)


@app.post("/v1/infra/droplets", response_model=InfraProvisionResponse)
def create_infra_droplet(payload: InfraProvisionRequest) -> InfraProvisionResponse:
    with begin(engine) as connection:
        try:
            result = infrastructure.create_droplet(
                connection,
                project_id=payload.project_id,
                task_id=payload.task_id,
                decision_id=payload.decision_id,
                reservation_id=payload.reservation_id,
                dedupe_key=payload.dedupe_key,
                title=payload.title,
                region=payload.region,
                size=payload.size,
                ttl_minutes=payload.ttl_minutes,
                tags=payload.tags,
                created_by_job_id=payload.created_by_job_id,
                data_boundary=payload.data_boundary,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return InfraProvisionResponse(**result)


@app.post("/v1/infra/droplets/queue", response_model=JobSummary, status_code=201)
def queue_infra_droplet(payload: InfraProvisionJobRequest) -> JobSummary:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "infra_provision",
            )
            result = infrastructure.queue_droplet_provision(
                connection,
                project_id=payload.project_id,
                task_id=payload.task_id,
                decision_id=payload.decision_id,
                reservation_id=payload.reservation_id,
                dedupe_key=payload.dedupe_key,
                title=payload.title,
                region=payload.region,
                size=payload.size,
                ttl_minutes=payload.ttl_minutes,
                tags=payload.tags,
                actor=payload.actor,
                priority=payload.priority,
                max_attempts=payload.max_attempts,
                data_boundary=payload.data_boundary,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return JobSummary(**result)


@app.get("/v1/infra/resources", response_model=InfraResourceListResponse)
def list_infra_resources(
    project_id: str = Query(...),
    limit: int = Query(default=20, ge=1, le=200),
) -> InfraResourceListResponse:
    with begin(engine) as connection:
        rows = infrastructure.list_resources(connection, project_id, limit)
        return InfraResourceListResponse(
            project_id=project_id,
            items=[infra_resource_summary_from_row(row) for row in rows],
        )


@app.post(
    "/v1/infra/resources/{resource_id}/terminate",
    response_model=InfraTerminationResponse,
)
def terminate_infra_resource(
    resource_id: str, payload: InfraTerminationRequest
) -> InfraTerminationResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "infra_provision",
            )
            result = infrastructure.terminate_droplet(
                connection,
                project_id=payload.project_id,
                resource_id=resource_id,
                dedupe_key=payload.dedupe_key,
                reason=payload.reason,
                created_by_job_id=payload.created_by_job_id,
            )
        except InfraResourceNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return InfraTerminationResponse(**result)


@app.post(
    "/v1/infra/resources/{resource_id}/terminate/queue",
    response_model=JobSummary,
    status_code=201,
)
def queue_infra_resource_termination(
    resource_id: str, payload: InfraTerminationJobRequest
) -> JobSummary:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "infra_provision",
            )
            result = infrastructure.queue_droplet_termination(
                connection,
                project_id=payload.project_id,
                resource_id=resource_id,
                dedupe_key=payload.dedupe_key,
                actor=payload.actor,
                priority=payload.priority,
                max_attempts=payload.max_attempts,
                reason=payload.reason,
            )
        except InfraResourceNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return JobSummary(**result)


@app.post("/v1/infra/janitor/run", response_model=InfraJanitorRunResponse)
def run_infra_janitor(payload: InfraJanitorRunRequest) -> InfraJanitorRunResponse:
    with begin(engine) as connection:
        require_project_actor(
            connection,
            payload.project_id,
            payload.actor,
            "infra_provision",
        )
        result = infrastructure.queue_expired_resource_terminations(
            connection,
            project_id=payload.project_id,
            actor=payload.actor,
            limit=payload.limit,
        )
        return InfraJanitorRunResponse(**result)


@app.post("/v1/software-factory/proposals/queue", response_model=JobSummary, status_code=201)
def queue_pull_request_proposal(
    payload: PullRequestProposalJobRequest,
) -> JobSummary:
    with begin(engine) as connection:
        result = jobs.create_job(
            connection,
            project_id=payload.project_id,
            task_id=payload.task_id,
            kind="software_factory.propose_pr",
            payload={
                "project_id": payload.project_id,
                "task_id": payload.task_id,
                "reservation_id": payload.reservation_id,
                "dedupe_key": payload.dedupe_key,
                "title": payload.title,
                "branch_name": payload.branch_name,
                "summary": payload.summary,
                "files": [file.model_dump() for file in payload.files],
                "agent_module_id": payload.agent_module_id,
                "requires_reservation": True,
            },
            priority=payload.priority,
            max_attempts=payload.max_attempts,
        )
        budget.attach_job(
            connection,
            payload.project_id,
            payload.reservation_id,
            result["id"],
        )
        return JobSummary(**result)


@app.post("/v1/software-factory/merges/queue", response_model=JobSummary, status_code=201)
def queue_pull_request_merge(
    payload: PullRequestMergeJobRequest,
) -> JobSummary:
    with begin(engine) as connection:
        result = jobs.create_job(
            connection,
            project_id=payload.project_id,
            task_id=payload.task_id,
            kind="github.pull_request.merge",
            payload={
                "project_id": payload.project_id,
                "decision_id": payload.decision_id,
                "dedupe_key": payload.dedupe_key,
                "pull_request_number": payload.pull_request_number,
                "pull_request_url": payload.pull_request_url,
                "title": payload.title,
                "branch_name": payload.branch_name,
            },
            priority=payload.priority,
            max_attempts=payload.max_attempts,
        )
        return JobSummary(**result)


@app.post(
    "/v1/software-factory/deployments/queue",
    response_model=JobSummary,
    status_code=201,
)
def queue_sandbox_deployment(payload: SandboxDeploymentJobRequest) -> JobSummary:
    with begin(engine) as connection:
        result = jobs.create_job(
            connection,
            project_id=payload.project_id,
            task_id=payload.task_id,
            kind="github.deployment.create",
            payload={
                "project_id": payload.project_id,
                "decision_id": payload.decision_id,
                "dedupe_key": payload.dedupe_key,
                "merged_sha": payload.merged_sha,
                "pull_request_url": payload.pull_request_url,
                "title": payload.title,
                "branch_name": payload.branch_name,
                "environment": payload.environment,
            },
            priority=payload.priority,
            max_attempts=payload.max_attempts,
        )
        return JobSummary(**result)


@app.get("/v1/events", response_model=EventFeedResponse)
def event_feed(project_id: str, limit: int = 50, offset: int = 0) -> EventFeedResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")

    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, event_type, actor, entity_type, entity_id, data, created_at
                FROM events
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()

        items = [event_item_from_row(row) for row in rows]
        return EventFeedResponse(project_id=project_id, items=items, limit=limit, offset=offset)


@app.get("/v1/deadletters", response_model=DeadletterListResponse)
def list_deadletters(project_id: str, limit: int = 50) -> DeadletterListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")

    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT d.id, d.project_id, d.source_kind, d.source_id,
                       CASE
                         WHEN j.kind = 'webhook.outbound.deliver'
                         THEN 'webhook_delivery'
                       END AS related_entity_type,
                       CASE
                         WHEN j.kind = 'webhook.outbound.deliver'
                         THEN j.payload->>'delivery_id'
                       END AS related_entity_id,
                       wd.status AS related_status,
                       d.reason, d.payload, d.status, d.incident_task_id,
                       d.incident_artifact_id, d.remediation_decision_id,
                       d.requeued_job_id, d.resolved_by, d.resolved_at,
                       d.created_at, d.updated_at
                FROM deadletters d
                LEFT JOIN jobs j
                  ON d.source_kind = 'job' AND d.source_id = j.id
                LEFT JOIN webhook_deliveries wd
                  ON j.kind = 'webhook.outbound.deliver'
                 AND wd.project_id = d.project_id
                 AND wd.id = j.payload->>'delivery_id'
                WHERE d.project_id = :project_id
                ORDER BY d.created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return DeadletterListResponse(
            project_id=project_id,
            items=[deadletter_item_from_row(row) for row in rows],
        )


@app.post(
    "/v1/deadletters/{deadletter_id}/incident",
    response_model=DeadletterIncidentResponse,
)
def create_deadletter_incident(
    deadletter_id: str, payload: DeadletterIncidentRequest
) -> DeadletterIncidentResponse:
    with begin(engine) as connection:
        row = connection.execute(
            text("SELECT project_id FROM deadletters WHERE id = :deadletter_id"),
            {"deadletter_id": deadletter_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="deadletter not found")
        require_project_actor(
            connection,
            row["project_id"],
            payload.actor,
            "task_create",
        )
        try:
            result = deadletter_remediation.create_incident_packet(
                connection,
                deadletter_id=deadletter_id,
                actor=payload.actor,
                severity=payload.severity,
                summary=payload.summary,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        item = deadletter_item_from_row(result)
        return DeadletterIncidentResponse(
            status=item.status,
            deadletter=item,
            incident_task_id=item.incident_task_id or "",
            incident_artifact_id=item.incident_artifact_id or "",
        )


@app.post(
    "/v1/deadletters/{deadletter_id}/requeue",
    response_model=DeadletterRequeueResponse,
)
def requeue_deadletter_job(
    deadletter_id: str, payload: DeadletterRequeueRequest
) -> DeadletterRequeueResponse:
    with begin(engine) as connection:
        row = connection.execute(
            text("SELECT project_id FROM deadletters WHERE id = :deadletter_id"),
            {"deadletter_id": deadletter_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="deadletter not found")
        require_project_actor(
            connection,
            row["project_id"],
            payload.actor,
            "task_create",
        )
        try:
            result = deadletter_remediation.requeue_job_deadletter(
                connection,
                deadletter_id=deadletter_id,
                actor=payload.actor,
                reason=payload.reason,
                run_after_seconds=payload.run_after_seconds,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        item = deadletter_item_from_row(result)
        return DeadletterRequeueResponse(
            status=item.status,
            deadletter=item,
            requeued_job_id=item.requeued_job_id or "",
        )


@app.get("/v1/receipts", response_model=ReceiptListResponse)
def list_receipts(
    project_id: str, limit: int = 50, offset: int = 0
) -> ReceiptListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0.")

    with begin(engine) as connection:
        items = receipts.list_by_project(connection, project_id, limit, offset=offset)
        return ReceiptListResponse(
            project_id=project_id,
            items=[receipt_response_from_row(item) for item in items],
            limit=limit,
            offset=offset,
        )


@app.get("/v1/receipts/{dedupe_key}", response_model=ReceiptResponse)
def get_receipt(dedupe_key: str) -> ReceiptResponse:
    with begin(engine) as connection:
        receipt = receipts.get_by_dedupe_key(
            connection, settings.default_org_id, dedupe_key
        )
        if not receipt:
            raise HTTPException(status_code=404, detail="Receipt not found")
        return receipt_response_from_row(receipt)


@app.get("/v1/change-journal", response_model=ChangeJournalResponse)
def list_change_journal(project_id: str, limit: int = 25) -> ChangeJournalResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    with begin(engine) as connection:
        items = audit.list_change_journal(connection, project_id, limit)
        return ChangeJournalResponse(project_id=project_id, items=items)


@app.get("/v1/change-journal/{artifact_id}", response_model=ChangeJournalDetail)
def get_change_journal_item(artifact_id: str, project_id: str) -> ChangeJournalDetail:
    with begin(engine) as connection:
        try:
            item = audit.get_change_journal_item(connection, project_id, artifact_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return ChangeJournalDetail(**item)


@app.get("/v1/audit/export", response_model=AuditExportResponse)
def export_audit_trail(
    project_id: str,
    task_id: Optional[str] = None,
    decision_id: Optional[str] = None,
    job_id: Optional[str] = None,
    actor: str = "ceo",
) -> AuditExportResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                project_id,
                actor,
                "audit_export",
            )
            payload = audit.export(
                connection,
                project_id,
                actor=actor,
                task_id=task_id,
                decision_id=decision_id,
                job_id=job_id,
            )
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return AuditExportResponse(
            project_id=payload["project_id"],
            project_name=payload["project_name"],
            workspace_name=payload["workspace_name"],
            org_name=payload["org_name"],
            generated_at=payload["generated_at"],
            governance=payload["governance"],
            scope=AuditExportScope(**payload["scope"]),
            counts=AuditExportCounts(**payload["counts"]),
            chain=payload["chain"],
            tasks=[task_summary_from_row(row) for row in payload["tasks"]],
            jobs=[job_summary_from_row(row) for row in payload["jobs"]],
            decisions=[decision_card_from_row(row) for row in payload["decisions"]],
            artifacts=[artifact_summary_from_row(row) for row in payload["artifacts"]],
            receipts=[receipt_response_from_row(row) for row in payload["receipts"]],
            budget_reservations=[
                budget_reservation_summary_from_row(row)
                for row in payload["budget_reservations"]
            ],
            cost_receipts=[
                cost_receipt_summary_from_row(row)
                for row in payload["cost_receipts"]
            ],
            routing_logs=[
                routing_log_item_from_service(row) for row in payload["routing_logs"]
            ],
            deadletters=[
                deadletter_item_from_row(row) for row in payload["deadletters"]
            ],
            events=[event_item_from_row(row) for row in payload["events"]],
        )


@app.post("/v1/audit/replay-bundles", response_model=ReplayBundleResponse, status_code=201)
def create_replay_bundle(payload: ReplayBundleRequest) -> ReplayBundleResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "audit_export",
            )
            result = audit.create_replay_bundle(
                connection,
                payload.project_id,
                actor=payload.actor,
                task_id=payload.task_id,
                decision_id=payload.decision_id,
                job_id=payload.job_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return ReplayBundleResponse(
            status=result["status"],
            artifact_id=result["artifact_id"],
            project_id=result["project_id"],
            artifact_type=result["artifact_type"],
            created_at=result["created_at"],
            scope=AuditExportScope(**result["scope"]),
            counts=AuditExportCounts(**result["counts"]),
        )


@app.post(
    "/v1/compliance/evidence-bundles",
    response_model=ComplianceEvidenceBundleResponse,
    status_code=201,
)
def create_compliance_evidence_bundle(
    payload: ComplianceEvidenceBundleRequest,
) -> ComplianceEvidenceBundleResponse:
    with begin(engine) as connection:
        try:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "audit_export",
            )
            result = audit.create_compliance_evidence_bundle(
                connection,
                payload.project_id,
                actor=payload.actor,
                period_start=payload.period_start,
                period_end=payload.period_end,
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return ComplianceEvidenceBundleResponse(
            status=result["status"],
            artifact_id=result["artifact_id"],
            project_id=result["project_id"],
            artifact_type=result["artifact_type"],
            created_at=result["created_at"],
            period=result["period"],
            counts=ComplianceEvidenceCounts(**result["counts"]),
        )


def raise_workflow_error(error: workflow_runtime.WorkflowError) -> None:
    if isinstance(error, workflow_runtime.WorkflowNotFoundError):
        status_code = 404
    elif isinstance(
        error,
        (
            workflow_runtime.WorkflowConflictError,
            workflow_runtime.WorkflowLeaseError,
            workflow_runtime.WorkflowMappingAmbiguousError,
            workflow_runtime.WorkflowBindingBlockedError,
        ),
    ):
        status_code = 409
    else:
        status_code = 422
    raise HTTPException(status_code=status_code, detail=str(error)) from error


@app.get("/v1/workflows", response_model=WorkflowDefinitionListResponse)
def list_workflow_definitions(
    project_id: str, include_retired: bool = False
) -> WorkflowDefinitionListResponse:
    with begin(engine) as connection:
        return WorkflowDefinitionListResponse(
            project_id=project_id,
            items=workflow_runtime.list_definitions(
                connection, project_id, include_retired=include_retired
            ),
        )


@app.post("/v1/workflows", response_model=WorkflowDefinitionSummary, status_code=201)
def create_workflow_definition(
    payload: WorkflowDefinitionCreateRequest,
) -> WorkflowDefinitionSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "workflow_manage"
        )
        try:
            return WorkflowDefinitionSummary(
                **workflow_runtime.create_definition(
                    connection,
                    project_id=payload.project_id,
                    workflow_key=payload.workflow_key,
                    name=payload.name,
                    description=payload.description,
                    actor=payload.actor,
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.get("/v1/workflows/{definition_id}", response_model=WorkflowDefinitionDetail)
def get_workflow_definition(
    definition_id: str, project_id: str
) -> WorkflowDefinitionDetail:
    with begin(engine) as connection:
        try:
            return WorkflowDefinitionDetail(
                **workflow_runtime.get_definition(connection, project_id, definition_id)
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post(
    "/v1/workflows/{definition_id}/versions",
    response_model=WorkflowVersionSummary,
    status_code=201,
)
def publish_workflow_version(
    definition_id: str, payload: WorkflowVersionCreateRequest
) -> WorkflowVersionSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "workflow_manage"
        )
        try:
            return WorkflowVersionSummary(
                **workflow_runtime.publish_version(
                    connection,
                    project_id=payload.project_id,
                    definition_id=definition_id,
                    spec=payload.spec,
                    actor=payload.actor,
                    activate=payload.activate,
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.get("/v1/workflow-mappings", response_model=WorkflowMappingListResponse)
def list_workflow_mappings(project_id: str) -> WorkflowMappingListResponse:
    with begin(engine) as connection:
        return WorkflowMappingListResponse(
            project_id=project_id,
            items=workflow_runtime.list_mappings(connection, project_id),
        )


@app.post(
    "/v1/workflow-mappings",
    response_model=WorkflowMappingSummary,
    status_code=201,
)
def create_workflow_mapping(payload: WorkflowMappingCreateRequest) -> WorkflowMappingSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "workflow_manage"
        )
        try:
            return WorkflowMappingSummary(
                **workflow_runtime.create_mapping(
                    connection,
                    project_id=payload.project_id,
                    workflow_version_id=payload.workflow_version_id,
                    selector_kind=payload.selector_kind,
                    selector_value=payload.selector_value,
                    scope_type=payload.scope_type,
                    scope_id=payload.scope_id,
                    priority=payload.priority,
                    actor=payload.actor,
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post("/v1/workflow-mappings/resolve")
def resolve_workflow_mapping(payload: WorkflowMappingResolveRequest) -> dict[str, Any]:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "workflow_execute"
        )
        try:
            return workflow_runtime.resolve_mapping(
                connection,
                project_id=payload.project_id,
                selector_kind=payload.selector_kind,
                selector_value=payload.selector_value,
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.get("/v1/workflow-runs", response_model=WorkflowRunListResponse)
def list_workflow_runs(
    project_id: str, status: Optional[str] = None, limit: int = 50
) -> WorkflowRunListResponse:
    with begin(engine) as connection:
        try:
            return WorkflowRunListResponse(
                project_id=project_id,
                items=workflow_runtime.list_runs(
                    connection, project_id, status=status, limit=limit
                ),
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post("/v1/workflow-runs", response_model=WorkflowRunSummary, status_code=201)
def start_workflow_run(payload: WorkflowRunStartRequest) -> WorkflowRunSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "workflow_execute"
        )
        try:
            return WorkflowRunSummary(
                **workflow_runtime.start_run(
                    connection,
                    project_id=payload.project_id,
                    actor=payload.actor,
                    input_data=payload.input,
                    dedupe_key=payload.dedupe_key,
                    workflow_version_id=payload.workflow_version_id,
                    selector_kind=payload.selector_kind,
                    selector_value=payload.selector_value,
                    policy_snapshot=payload.policy_snapshot,
                    parent_run_id=payload.parent_run_id,
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.get("/v1/workflow-runs/{run_id}", response_model=WorkflowRunDetail)
def get_workflow_run(run_id: str, project_id: str) -> WorkflowRunDetail:
    with begin(engine) as connection:
        try:
            return WorkflowRunDetail(
                **workflow_runtime.get_run(connection, project_id, run_id)
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post("/v1/workflow-runs/{run_id}/advance", response_model=WorkflowRunDetail)
def advance_workflow_run(
    run_id: str, payload: WorkflowRunActorRequest
) -> WorkflowRunDetail:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "workflow_execute"
        )
        try:
            return WorkflowRunDetail(
                **workflow_runtime.advance_run(connection, payload.project_id, run_id)
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post("/v1/workflow-runs/{run_id}/pause", response_model=WorkflowRunDetail)
def pause_workflow_run(
    run_id: str, payload: WorkflowRunActorRequest
) -> WorkflowRunDetail:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "workflow_execute"
        )
        try:
            return WorkflowRunDetail(
                **workflow_runtime.pause_run(
                    connection, payload.project_id, run_id, payload.actor
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post("/v1/workflow-runs/{run_id}/resume", response_model=WorkflowRunDetail)
def resume_workflow_run(
    run_id: str, payload: WorkflowRunActorRequest
) -> WorkflowRunDetail:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "workflow_execute"
        )
        try:
            return WorkflowRunDetail(
                **workflow_runtime.resume_run(
                    connection, payload.project_id, run_id, payload.actor
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post("/v1/workflow-runs/{run_id}/cancel", response_model=WorkflowRunDetail)
def cancel_workflow_run(
    run_id: str, payload: WorkflowRunCancelRequest
) -> WorkflowRunDetail:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "workflow_execute"
        )
        try:
            return WorkflowRunDetail(
                **workflow_runtime.cancel_run(
                    connection,
                    payload.project_id,
                    run_id,
                    payload.actor,
                    reason=payload.reason,
                    compensate=payload.compensate,
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post(
    "/v1/workflow-runs/{run_id}/steps/claim",
    response_model=Optional[WorkflowStepRunSummary],
)
def claim_workflow_step(
    run_id: str, payload: WorkflowStepClaimRequest
) -> Optional[WorkflowStepRunSummary]:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.worker_id, "workflow_execute"
        )
        try:
            item = workflow_runtime.claim_step(
                connection,
                project_id=payload.project_id,
                run_id=run_id,
                worker_id=payload.worker_id,
                lease_seconds=payload.lease_seconds,
            )
            return WorkflowStepRunSummary(**item) if item else None
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post(
    "/v1/workflow-step-runs/{step_run_id}/heartbeat",
    response_model=WorkflowStepRunSummary,
)
def heartbeat_workflow_step(
    step_run_id: str, payload: WorkflowStepHeartbeatRequest
) -> WorkflowStepRunSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.worker_id, "workflow_execute"
        )
        try:
            return WorkflowStepRunSummary(
                **workflow_runtime.heartbeat_step(
                    connection,
                    project_id=payload.project_id,
                    step_run_id=step_run_id,
                    worker_id=payload.worker_id,
                    lease_seconds=payload.lease_seconds,
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post(
    "/v1/workflow-step-runs/{step_run_id}/complete",
    response_model=WorkflowStepRunSummary,
)
def complete_workflow_step(
    step_run_id: str, payload: WorkflowStepCompleteRequest
) -> WorkflowStepRunSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.worker_id, "workflow_execute"
        )
        try:
            return WorkflowStepRunSummary(
                **workflow_runtime.complete_step(
                    connection,
                    project_id=payload.project_id,
                    step_run_id=step_run_id,
                    worker_id=payload.worker_id,
                    output=payload.output,
                    evidence=payload.evidence,
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post(
    "/v1/workflow-step-runs/{step_run_id}/fail",
    response_model=WorkflowStepRunSummary,
)
def fail_workflow_step(
    step_run_id: str, payload: WorkflowStepFailRequest
) -> WorkflowStepRunSummary:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.worker_id, "workflow_execute"
        )
        try:
            return WorkflowStepRunSummary(
                **workflow_runtime.fail_step(
                    connection,
                    project_id=payload.project_id,
                    step_run_id=step_run_id,
                    worker_id=payload.worker_id,
                    error=payload.error,
                    retryable=payload.retryable,
                    retry_delay_seconds=payload.retry_delay_seconds,
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post("/v1/workflow-runs/{run_id}/migrate", response_model=WorkflowRunDetail)
def migrate_workflow_run(
    run_id: str, payload: WorkflowRunMigrateRequest
) -> WorkflowRunDetail:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "workflow_manage"
        )
        try:
            return WorkflowRunDetail(
                **workflow_runtime.migrate_run(
                    connection,
                    project_id=payload.project_id,
                    run_id=run_id,
                    target_version_id=payload.target_version_id,
                    actor=payload.actor,
                    reason=payload.reason,
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post(
    "/v1/workflow-runs/{run_id}/migrations/rollback",
    response_model=WorkflowRunDetail,
)
def rollback_workflow_migration(
    run_id: str, payload: WorkflowMigrationRollbackRequest
) -> WorkflowRunDetail:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "workflow_manage"
        )
        try:
            return WorkflowRunDetail(
                **workflow_runtime.rollback_migration(
                    connection,
                    project_id=payload.project_id,
                    run_id=run_id,
                    migration_id=payload.migration_id,
                    actor=payload.actor,
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post("/v1/workflow-runs/{run_id}/compensate", response_model=WorkflowRunDetail)
def compensate_workflow_run(
    run_id: str, payload: WorkflowCompensationBeginRequest
) -> WorkflowRunDetail:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.actor, "workflow_execute"
        )
        try:
            return WorkflowRunDetail(
                **workflow_runtime.begin_compensation(
                    connection,
                    payload.project_id,
                    run_id,
                    payload.actor,
                    reason=payload.reason,
                )
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post("/v1/workflow-runs/{run_id}/compensations/claim")
def claim_workflow_compensation(
    run_id: str, payload: WorkflowCompensationClaimRequest
) -> Optional[dict[str, Any]]:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.worker_id, "workflow_execute"
        )
        try:
            return workflow_runtime.claim_compensation(
                connection,
                project_id=payload.project_id,
                run_id=run_id,
                worker_id=payload.worker_id,
                lease_seconds=payload.lease_seconds,
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post("/v1/workflow-compensation-runs/{compensation_id}/complete")
def complete_workflow_compensation(
    compensation_id: str, payload: WorkflowCompensationCompleteRequest
) -> dict[str, Any]:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.worker_id, "workflow_execute"
        )
        try:
            return workflow_runtime.complete_compensation(
                connection,
                project_id=payload.project_id,
                compensation_id=compensation_id,
                worker_id=payload.worker_id,
                output=payload.output,
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)


@app.post("/v1/workflow-compensation-runs/{compensation_id}/fail")
def fail_workflow_compensation(
    compensation_id: str, payload: WorkflowCompensationFailRequest
) -> dict[str, Any]:
    with begin(engine) as connection:
        require_project_actor(
            connection, payload.project_id, payload.worker_id, "workflow_execute"
        )
        try:
            return workflow_runtime.fail_compensation(
                connection,
                project_id=payload.project_id,
                compensation_id=compensation_id,
                worker_id=payload.worker_id,
                error=payload.error,
            )
        except workflow_runtime.WorkflowError as error:
            raise_workflow_error(error)
