from contextlib import asynccontextmanager
from contextvars import ContextVar
from secrets import compare_digest
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .bootstrap import seed
from .config import get_settings
from .db import begin, database_is_reachable, get_engine
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
    AccountConnectLinkRequest,
    AccountConnectBillingPortalSessionCreateRequest,
    AccountConnectBillingPortalSessionListResponse,
    AccountConnectBillingPortalSessionSummary,
    AccountConnectMutationResponse,
    AccountConnectSyncRequest,
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
    BudgetQuoteRequest,
    BudgetQuoteResponse,
    BudgetReservationSummary,
    BudgetReleaseRequest,
    BudgetReleaseResponse,
    BudgetStatusResponse,
    BudgetReserveRequest,
    BudgetReserveResponse,
    ChangeJournalDetail,
    ChangeJournalResponse,
    CommandProposeRequest,
    CommandProposeResponse,
    ComplianceEvidenceBundleRequest,
    ComplianceEvidenceBundleResponse,
    ComplianceEvidenceCounts,
    ChangeSetApplyRequest,
    ChangeSetApplyResponse,
    ChangeSetApproveRequest,
    ChangeSetCreateRequest,
    ChangeSetListResponse,
    ChangeSetSummary,
    CostAnomalyListResponse,
    CostAnomalySummary,
    CostEventListResponse,
    CostEventSummary,
    CostReceiptSummary,
    DepartmentCreateRequest,
    DepartmentListResponse,
    DepartmentSummary,
    DeploymentRolloutListResponse,
    DeploymentRolloutSummary,
    DeploymentRunCreateRequest,
    DeploymentRunHealthCheckRequest,
    DeploymentRunHealthCheckResponse,
    DeploymentRunListResponse,
    DeploymentRunSummary,
    DeploymentTargetCreateRequest,
    DeploymentTargetListResponse,
    DeploymentTargetSummary,
    DecisionActionRequest,
    DecisionCard,
    DecisionDigestRequest,
    DecisionDigestResponse,
    DecisionInboxResponse,
    DeadletterItem,
    DeadletterListResponse,
    DriftReportListResponse,
    DriftReportSummary,
    EntitlementSourceConnectRequest,
    EntitlementSourceDisconnectRequest,
    EntitlementSourceListResponse,
    EntitlementSourceSummary,
    EntitlementSourceUpsertRequest,
    EnterpriseClusterListResponse,
    EnterpriseClusterSummary,
    EnterpriseClusterUpsertRequest,
    EnterpriseIdentityProviderListResponse,
    EnterpriseIdentityProviderSummary,
    EnterpriseIdentityProviderUpsertRequest,
    EnterpriseScimGroupListResponse,
    EnterpriseScimGroupSummary,
    EnterpriseScimGroupUpsertRequest,
    EnterpriseScimUserListResponse,
    EnterpriseScimUserSummary,
    EnterpriseScimUserUpsertRequest,
    EventFeedResponse,
    EventItem,
    ExternalResourceListResponse,
    ExternalResourceSummary,
    HealthResponse,
    HealthSnapshotListResponse,
    HealthSnapshotRefreshRequest,
    HealthSnapshotSummary,
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
    JobSummary,
    LeasedJob,
    MetatronQueryRequest,
    MetatronQueryResponse,
    OpenAIPlanRequest,
    OpenAIPlanResponse,
    MeetingCreateRequest,
    MeetingListResponse,
    MeetingRunRequest,
    MeetingSummary,
    OrgMemberListResponse,
    OrgMemberSummary,
    OrgMemberUpsertRequest,
    OutboxDeliveryResponse,
    OutboxMessageCreateRequest,
    OutboxMessageListResponse,
    OutboxMessageSummary,
    OutboxQueueRequest,
    PackDetail,
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
    SelfImprovementCanaryRequest,
    SelfImprovementProposalCreateRequest,
    SelfImprovementProposalDetail,
    SelfImprovementProposalListResponse,
    SelfImprovementProposalSummary,
    SelfImprovementRollbackRequest,
    SelfImprovementRolloutRequest,
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
)
from .services.audit import AuditTrailService
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
    AccountConnectConfigurationError,
    AccountConnectLinkNotFoundError,
    AccountConnectSyncError,
    CommercialService,
)
from .services.core import emit_event
from .services.decisions import (
    DecisionNotFoundError,
    DecisionService,
    InvalidDecisionTransitionError,
)
from .services.entitlements import (
    EntitlementBrokerService,
    EntitlementPoolNotFoundError,
    EntitlementSourceNotFoundError,
    InvalidEntitlementConnectionError,
    SkillNotFoundError,
)
from .services.enterprise import EnterpriseService
from .services.health import HealthSnapshotService
from .services.infrastructure import (
    InfraResourceNotFoundError,
    InfrastructureService,
)
from .services.intake import CommandIntakeService
from .services.jobs import JobLeaseService, LeaseNotFoundError
from .services.members import (
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
from .services.receipts import ReceiptService
from .services.resource_plane import (
    ChangeSetNotFoundError,
    ProviderAccountNotFoundError,
    ResourcePlaneService,
)
from .services.role_governance import RoleGovernanceService
from .services.self_improvement import (
    InvalidSelfImprovementTransitionError,
    SelfImprovementProposalNotFoundError,
    SelfImprovementService,
)
from .services.software_factory import SoftwareFactoryService
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
budget = BudgetGovernor(settings)
intake = CommandIntakeService(settings)
jobs = JobLeaseService(settings)
receipts = ReceiptService()
software_factory = SoftwareFactoryService(settings)
decisions = DecisionService(settings)
specpack = SpecPackService(settings)
agent_runtime = AgentRuntimeService(settings)
entitlements = EntitlementBrokerService(settings)
commercial = CommercialService(settings)
enterprise = EnterpriseService(settings)
audit = AuditTrailService(settings)
comms = CommsService(settings)
infrastructure = InfrastructureService(settings)
self_improvement = SelfImprovementService(settings)
health_snapshots = HealthSnapshotService(settings)
metatron = MetatronService(settings)
role_governance = RoleGovernanceService(settings)
members = MemberDirectoryService(settings)
canonical = CanonicalObjectService()
break_glass = BreakGlassService()
resource_plane = ResourcePlaneService(settings)
oidc_auth = OidcAuthenticator(settings)
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
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
    auth_required = settings.require_api_auth or settings.oidc_enabled
    if not request.url.path.startswith("/v1/") or not auth_required:
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
    if settings.require_api_auth and not allowed_tokens and not settings.oidc_enabled:
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
) -> None:
    principal = current_api_principal.get()
    if principal and principal != actor:
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
        record_policy_denial(project_id, actor, permission, str(error))
        raise HTTPException(status_code=403, detail=str(error)) from error
    require_authenticated_actor_binding(project_id, actor, permission)


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
    except Exception:
        pass
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    return DeadletterItem(
        id=row["id"],
        project_id=row["project_id"],
        source_kind=row["source_kind"],
        source_id=row["source_id"],
        reason=row["reason"],
        payload=row["payload"] or {},
        created_at=row["created_at"].isoformat(),
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


def model_adapter_summary_from_service(payload) -> ModelAdapterSummary:
    return ModelAdapterSummary(**payload)


def entitlement_pool_summary_from_service(payload) -> EntitlementPoolSummary:
    return EntitlementPoolSummary(**payload)


def edition_status_from_service(payload) -> EditionStatusResponse:
    return EditionStatusResponse(**payload)


def license_entitlement_summary_from_service(payload) -> LicenseEntitlementSummary:
    return LicenseEntitlementSummary(**payload)


def account_connect_team_invite_from_service(
    payload,
) -> AccountConnectTeamInviteSummary:
    return AccountConnectTeamInviteSummary(**payload)


def account_connect_billing_session_from_service(
    payload,
) -> AccountConnectBillingPortalSessionSummary:
    return AccountConnectBillingPortalSessionSummary(**payload)


def enterprise_cluster_summary_from_service(payload) -> EnterpriseClusterSummary:
    return EnterpriseClusterSummary(**payload)


def enterprise_identity_provider_summary_from_service(
    payload,
) -> EnterpriseIdentityProviderSummary:
    return EnterpriseIdentityProviderSummary(**payload)


def enterprise_scim_group_summary_from_service(payload) -> EnterpriseScimGroupSummary:
    return EnterpriseScimGroupSummary(**payload)


def enterprise_scim_user_summary_from_service(payload) -> EnterpriseScimUserSummary:
    return EnterpriseScimUserSummary(**payload)


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
    binding_required = bool(settings.api_key_principal_map) or settings.oidc_enabled
    transport_authenticated = current_transport_authenticated.get()
    auth_required = settings.require_api_auth or settings.oidc_enabled
    if not auth_required:
        message = "API authentication is disabled for this runtime."
    elif settings.oidc_enabled and not settings.oidc_configured:
        message = "OIDC authentication is enabled but not fully configured."
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


@app.get("/v1/edition", response_model=EditionStatusResponse)
def get_edition(project_id: str) -> EditionStatusResponse:
    with begin(engine) as connection:
        return edition_status_from_service(
            commercial.edition_status(connection, project_id)
        )


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
def metatron_query(payload: MetatronQueryRequest) -> MetatronQueryResponse:
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
def propose_command(payload: CommandProposeRequest) -> CommandProposeResponse:
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


@app.get("/v1/tasks", response_model=TaskListResponse)
def list_tasks(project_id: str) -> TaskListResponse:
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
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return TaskListResponse(items=[task_summary_from_row(row) for row in rows])


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
def list_departments(project_id: str) -> DepartmentListResponse:
    with begin(engine) as connection:
        return DepartmentListResponse(
            project_id=project_id,
            items=[
                DepartmentSummary(**item)
                for item in canonical.list_departments(connection, project_id)
            ],
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


@app.get("/v1/policies", response_model=PolicyRecordListResponse)
def list_policy_records(project_id: str) -> PolicyRecordListResponse:
    with begin(engine) as connection:
        return PolicyRecordListResponse(
            project_id=project_id,
            items=[
                PolicyRecordSummary(**item)
                for item in canonical.list_policy_records(connection, project_id)
            ],
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
def list_plans(project_id: str) -> PlanListResponse:
    with begin(engine) as connection:
        return PlanListResponse(
            project_id=project_id,
            items=[
                PlanSummary(**item)
                for item in canonical.list_plans(connection, project_id)
            ],
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


@app.get("/v1/meetings", response_model=MeetingListResponse)
def list_meetings(project_id: str) -> MeetingListResponse:
    with begin(engine) as connection:
        return MeetingListResponse(
            project_id=project_id,
            items=[
                MeetingSummary(**item)
                for item in canonical.list_meetings(connection, project_id)
            ],
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
            text("SELECT project_id FROM meetings WHERE id = :meeting_id"),
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
        try:
            result = canonical.run_meeting(
                connection,
                settings,
                meeting_id=meeting_id,
                actor=payload.actor,
                transcript=[item.model_dump() for item in payload.transcript],
                minutes=payload.minutes,
                action_items=[item.model_dump() for item in payload.action_items],
                decision_items=[item.model_dump() for item in payload.decision_items],
                dissent_records=payload.dissent_records,
                blocked_state=payload.blocked_state,
                outcome_status=payload.outcome_status,
                consensus_reached=payload.consensus_reached,
            )
        except ValueError as error:
            detail = str(error)
            if "already" in detail:
                raise HTTPException(status_code=409, detail=detail) from error
            raise HTTPException(status_code=400, detail=detail) from error
        return MeetingSummary(**result)


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
    project_id: str, limit: int = 25
) -> SelfImprovementProposalListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    with begin(engine) as connection:
        rows = self_improvement.list_proposals(connection, project_id, limit)
        return SelfImprovementProposalListResponse(
            project_id=project_id,
            items=[self_improvement_summary_from_row(row) for row in rows],
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


@app.get("/v1/agent-modules", response_model=AgentModuleListResponse)
def list_agent_modules(project_id: str) -> AgentModuleListResponse:
    with begin(engine) as connection:
        modules = specpack.list_agent_modules_for_project(connection, project_id)
        return AgentModuleListResponse(
            project_id=project_id,
            items=[agent_module_summary_from_service(module) for module in modules],
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
def list_skills() -> SkillListResponse:
    return SkillListResponse(
        items=[skill_summary_from_service(skill) for skill in entitlements.list_skills()]
    )


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
    project_id: str, limit: int = 25
) -> ModelRouteDecisionListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    with begin(engine) as connection:
        items = entitlements.list_model_route_decisions(connection, project_id, limit)
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
) -> ExternalResourceListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    with begin(engine) as connection:
        items = resource_plane.list_resources(
            connection,
            project_id,
            provider=provider,
            ownership=ownership,
            limit=limit,
        )
        return ExternalResourceListResponse(
            project_id=project_id,
            items=[ExternalResourceSummary(**item) for item in items],
        )


@app.post("/v1/resources/sync", response_model=ResourceSyncResponse)
def sync_external_resources(payload: ResourceSyncRequest) -> ResourceSyncResponse:
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
def list_provider_costs(project_id: str, limit: int = 100) -> CostEventListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    with begin(engine) as connection:
        items = resource_plane.list_cost_events(connection, project_id, limit=limit)
        return CostEventListResponse(
            project_id=project_id,
            items=[CostEventSummary(**item) for item in items],
        )


@app.get("/v1/cost-anomalies", response_model=CostAnomalyListResponse)
def list_provider_cost_anomalies(
    project_id: str,
    limit: int = 100,
) -> CostAnomalyListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    with begin(engine) as connection:
        items = resource_plane.list_cost_anomalies(connection, project_id, limit=limit)
        return CostAnomalyListResponse(
            project_id=project_id,
            items=[CostAnomalySummary(**item) for item in items],
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


@app.get("/v1/resource-receipts", response_model=ResourceReceiptListResponse)
def list_resource_receipts(
    project_id: str,
    limit: int = 100,
) -> ResourceReceiptListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    with begin(engine) as connection:
        items = resource_plane.list_resource_receipts(
            connection,
            project_id,
            limit=limit,
        )
        return ResourceReceiptListResponse(
            project_id=project_id,
            items=[ResourceReceiptSummary(**item) for item in items],
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
def list_deployment_targets(project_id: str) -> DeploymentTargetListResponse:
    with begin(engine) as connection:
        items = resource_plane.list_deployment_targets(connection, project_id)
        return DeploymentTargetListResponse(
            project_id=project_id,
            items=[DeploymentTargetSummary(**item) for item in items],
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


@app.get("/v1/provider-health", response_model=ProviderHealthListResponse)
def list_provider_health(
    project_id: str,
    provider_account_id: Optional[str] = None,
    limit: int = 100,
) -> ProviderHealthListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    with begin(engine) as connection:
        items = resource_plane.list_provider_health(
            connection,
            project_id,
            provider_account_id=provider_account_id,
            limit=limit,
        )
        return ProviderHealthListResponse(
            project_id=project_id,
            items=[ProviderHealthSummary(**item) for item in items],
        )


@app.get("/v1/drift-reports", response_model=DriftReportListResponse)
def list_drift_reports(
    project_id: str,
    limit: int = 100,
) -> DriftReportListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    with begin(engine) as connection:
        items = resource_plane.list_drift_reports(
            connection,
            project_id,
            limit=limit,
        )
        return DriftReportListResponse(
            project_id=project_id,
            items=[DriftReportSummary(**item) for item in items],
        )


@app.get("/v1/reconciliation-runs", response_model=ReconciliationRunListResponse)
def list_reconciliation_runs(
    project_id: str,
    limit: int = 100,
) -> ReconciliationRunListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    with begin(engine) as connection:
        items = resource_plane.list_reconciliation_runs(
            connection,
            project_id,
            limit=limit,
        )
        return ReconciliationRunListResponse(
            project_id=project_id,
            items=[ReconciliationRunSummary(**item) for item in items],
        )


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
                )
                return inbox_message_summary_from_row(
                    reload_inbox_message(connection, item["id"])
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
def bootstrap_seed() -> dict[str, str]:
    seed()
    return {"status": "ok"}


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
        )


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
def event_feed(project_id: str, limit: int = 50) -> EventFeedResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")

    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, event_type, actor, entity_type, entity_id, data, created_at
                FROM events
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()

        items = [event_item_from_row(row) for row in rows]
        return EventFeedResponse(project_id=project_id, items=items)


@app.get("/v1/deadletters", response_model=DeadletterListResponse)
def list_deadletters(project_id: str, limit: int = 50) -> DeadletterListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")

    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, source_kind, source_id, reason, payload, created_at
                FROM deadletters
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return DeadletterListResponse(
            project_id=project_id,
            items=[deadletter_item_from_row(row) for row in rows],
        )


@app.get("/v1/receipts", response_model=ReceiptListResponse)
def list_receipts(project_id: str, limit: int = 50) -> ReceiptListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")

    with begin(engine) as connection:
        items = receipts.list_by_project(connection, project_id, limit)
        return ReceiptListResponse(
            project_id=project_id,
            items=[receipt_response_from_row(item) for item in items],
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
) -> AuditExportResponse:
    with begin(engine) as connection:
        try:
            payload = audit.export(
                connection,
                project_id,
                task_id=task_id,
                decision_id=decision_id,
                job_id=job_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return AuditExportResponse(
            project_id=payload["project_id"],
            project_name=payload["project_name"],
            workspace_name=payload["workspace_name"],
            org_name=payload["org_name"],
            generated_at=payload["generated_at"],
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
