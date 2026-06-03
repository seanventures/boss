from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from .bootstrap import seed
from .config import get_settings
from .db import begin, database_is_reachable, get_engine
from .schemas import (
    ArtifactDetail,
    ArtifactListResponse,
    ArtifactSummary,
    AuditExportCounts,
    AuditExportResponse,
    AuditExportScope,
    BudgetQuoteRequest,
    BudgetQuoteResponse,
    BudgetReservationSummary,
    BudgetReleaseRequest,
    BudgetReleaseResponse,
    BudgetStatusResponse,
    BudgetReserveRequest,
    BudgetReserveResponse,
    CommandProposeRequest,
    CommandProposeResponse,
    CostReceiptSummary,
    DecisionActionRequest,
    DecisionCard,
    DecisionInboxResponse,
    DeadletterItem,
    DeadletterListResponse,
    EntitlementSourceListResponse,
    EntitlementSourceSummary,
    EntitlementSourceUpsertRequest,
    EventFeedResponse,
    EventItem,
    HealthResponse,
    HealthSnapshotListResponse,
    HealthSnapshotRefreshRequest,
    HealthSnapshotSummary,
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
    OutboxDeliveryResponse,
    OutboxMessageCreateRequest,
    OutboxMessageListResponse,
    OutboxMessageSummary,
    OutboxQueueRequest,
    PackDetail,
    PackInstallRequest,
    PackListResponse,
    PackSummary,
    PullRequestMergeJobRequest,
    PullRequestMergeRequest,
    PullRequestMergeResponse,
    PullRequestProposalJobRequest,
    PullRequestProposalRequest,
    PullRequestProposalResponse,
    ReceiptListResponse,
    ReceiptResponse,
    ReplayBundleRequest,
    ReplayBundleResponse,
    RoleDetail,
    RoleListResponse,
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
    WorkerPollRequest,
    WorkerPollResponse,
)
from .services.audit import AuditTrailService
from .services.budget import BudgetGovernor
from .services.comms import (
    CommsService,
    InvalidCreatedByJobError,
    OutboxMessageNotFoundError,
)
from .services.decisions import (
    DecisionNotFoundError,
    DecisionService,
    InvalidDecisionTransitionError,
)
from .services.entitlements import EntitlementBrokerService, SkillNotFoundError
from .services.health import HealthSnapshotService
from .services.intake import CommandIntakeService
from .services.jobs import JobLeaseService, LeaseNotFoundError
from .services.metatron import MetatronScopeNotFoundError, MetatronService
from .services.receipts import ReceiptService
from .services.self_improvement import (
    InvalidSelfImprovementTransitionError,
    SelfImprovementProposalNotFoundError,
    SelfImprovementService,
)
from .services.software_factory import SoftwareFactoryService
from .services.specpack import PackNotFoundError, RoleNotFoundError, SpecPackService

settings = get_settings()
engine = get_engine(settings)
budget = BudgetGovernor(settings)
intake = CommandIntakeService(settings)
jobs = JobLeaseService(settings)
receipts = ReceiptService()
software_factory = SoftwareFactoryService(settings)
decisions = DecisionService(settings)
specpack = SpecPackService(settings)
entitlements = EntitlementBrokerService(settings)
audit = AuditTrailService(settings)
comms = CommsService(settings)
self_improvement = SelfImprovementService(settings)
health_snapshots = HealthSnapshotService(settings)
metatron = MetatronService(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="BOSS Control Plane", version="0.2.0", lifespan=lifespan)


def task_summary_from_row(row) -> TaskSummary:
    return TaskSummary(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        priority=row["priority"],
        risk_tier=row["risk_tier"],
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
        parsed=row.get("parsed") or {},
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
        created_at=payload["created_at"],
    )


def role_detail_from_service(payload) -> RoleDetail:
    return RoleDetail(
        id=payload["id"],
        project_id=payload["project_id"],
        role_key=payload["role_key"],
        display_name=payload["display_name"],
        autonomy_level=payload["autonomy_level"],
        created_at=payload["created_at"],
        spec=payload["spec"] or {},
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


def skill_summary_from_service(payload) -> SkillSummary:
    return SkillSummary(
        id=payload["id"],
        kind=payload["kind"],
        risk_tier=payload["risk_tier"],
        description=payload["description"],
        mock_mode=payload["mock_mode"],
        cost=payload.get("cost") or {},
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
        policy_notes=payload.get("policy_notes") or [],
        observed_capacity=payload.get("observed_capacity") or {},
        runtime_supported=bool(payload["runtime_supported"]),
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
    )


def routing_log_item_from_service(payload) -> RoutingLogItem:
    return RoutingLogItem(
        id=payload["id"],
        project_id=payload["project_id"],
        skill_id=payload.get("skill_id"),
        task_kind=payload["task_kind"],
        workload_class=payload["workload_class"],
        risk_tier=payload["risk_tier"],
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


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        database_connected=database_is_reachable(engine),
        github_configured=bool(settings.github_token),
        openai_configured=bool(settings.openai_api_key),
    )


@app.get("/v1/status", response_model=StatusResponse)
def status(project_id: str) -> StatusResponse:
    if not database_is_reachable(engine):
        return StatusResponse(
            project_id=project_id,
            project_name="BOSS dogfood bootstrap",
            workspace_name="Database unavailable",
            org_name="BOSS",
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
        result = intake.propose_command(
            connection,
            payload.project_id,
            payload.command,
            payload.constraints,
        )
        return CommandProposeResponse(**result)


@app.get("/v1/tasks", response_model=TaskListResponse)
def list_tasks(project_id: str) -> TaskListResponse:
    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, title, description, status, priority, risk_tier,
                       dedupe_key, created_at, updated_at
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
        result = intake.create_task(
            connection,
            project_id=payload.project_id,
            title=payload.title,
            description=payload.description,
            dedupe_key=payload.dedupe_key,
            priority=payload.priority,
            risk_tier=payload.risk_tier,
            inputs=payload.inputs,
            required_artifacts=payload.required_artifacts,
        )
        row = connection.execute(
            text(
                """
                SELECT id, project_id, title, description, status, priority, risk_tier,
                       dedupe_key, created_at, updated_at
                FROM tasks
                WHERE id = :task_id
                """
            ),
            {"task_id": result["task_id"]},
        ).mappings().one()
        return task_summary_from_row(row)


@app.get("/v1/jobs/{job_id}", response_model=JobSummary)
def get_job(job_id: str) -> JobSummary:
    with begin(engine) as connection:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, kind, status, priority, max_attempts,
                       attempt, run_after
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
                       dedupe_key, created_at, updated_at
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
        except (InvalidSelfImprovementTransitionError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
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
                       created_by_job_id, created_at
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
                       content_hash, created_by_job_id, created_at
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


@app.get("/v1/skills", response_model=SkillListResponse)
def list_skills() -> SkillListResponse:
    return SkillListResponse(
        items=[skill_summary_from_service(skill) for skill in entitlements.list_skills()]
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
        item = entitlements.upsert_source(
            connection,
            payload.model_dump(exclude={"actor"}),
            actor=payload.actor,
        )
        return entitlement_source_summary_from_service(item)


@app.post("/v1/router/preview", response_model=RoutingPreviewResponse)
def preview_route(payload: RoutingPreviewRequest) -> RoutingPreviewResponse:
    with begin(engine) as connection:
        try:
            route = entitlements.preview_route(
                connection,
                payload.project_id,
                skill_id=payload.skill_id,
                task_kind=payload.task_kind,
                workload_class=payload.workload_class,
                risk_tier=payload.risk_tier,
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
        item = comms.create_outbox_message(
            connection,
            payload.project_id,
            payload.channel,
            payload.subject,
            payload.body,
            payload.dedupe_key,
            payload.actor,
        )
        return outbox_message_summary_from_row(item)


@app.post("/v1/comms/outbox/{message_id}/queue", response_model=JobSummary, status_code=201)
def queue_outbox_delivery(message_id: str, payload: OutboxQueueRequest) -> JobSummary:
    with begin(engine) as connection:
        try:
            job = comms.queue_outbox_delivery(
                connection,
                payload.project_id,
                message_id,
                payload.reservation_id,
                payload.dedupe_key,
                payload.actor,
                payload.priority,
                payload.max_attempts,
            )
        except OutboxMessageNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return JobSummary(**job)


@app.post("/v1/comms/outbox/{message_id}/deliver", response_model=OutboxDeliveryResponse)
def deliver_outbox_message(
    message_id: str, payload: OutboxQueueRequest
) -> OutboxDeliveryResponse:
    with begin(engine) as connection:
        try:
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
        item = comms.ingest_inbox_message(
            connection,
            payload.project_id,
            payload.channel,
            payload.body,
            payload.actor,
            subject=payload.subject,
            raw_ref=payload.raw_ref,
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
        result = jobs.create_job(
            connection,
            project_id=payload.project_id,
            task_id=payload.task_id,
            kind=payload.kind,
            payload=job_payload,
            priority=payload.priority,
            max_attempts=payload.max_attempts,
            run_after_seconds=payload.run_after_seconds,
        )
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


@app.post("/v1/decisions/{decision_id}/approve", response_model=DecisionCard)
def approve_decision(
    decision_id: str, payload: DecisionActionRequest
) -> DecisionCard:
    with begin(engine) as connection:
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
        return decision_card_from_row(row)


@app.post("/v1/decisions/{decision_id}/reject", response_model=DecisionCard)
def reject_decision(
    decision_id: str, payload: DecisionActionRequest
) -> DecisionCard:
    with begin(engine) as connection:
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
        return decision_card_from_row(row)


@app.post("/v1/decisions/{decision_id}/revise", response_model=DecisionCard)
def revise_decision(
    decision_id: str, payload: DecisionActionRequest
) -> DecisionCard:
    with begin(engine) as connection:
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
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return decision_card_from_row(row)


@app.post("/v1/decisions/{decision_id}/delegate", response_model=DecisionCard)
def delegate_decision(
    decision_id: str, payload: DecisionActionRequest
) -> DecisionCard:
    with begin(engine) as connection:
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
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return decision_card_from_row(row)


@app.post("/v1/decisions/{decision_id}/execute", response_model=DecisionCard)
def execute_decision(
    decision_id: str, payload: DecisionActionRequest
) -> DecisionCard:
    with begin(engine) as connection:
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
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return decision_card_from_row(row)


@app.post("/v1/openai/plans", response_model=OpenAIPlanResponse)
def create_openai_plan(payload: OpenAIPlanRequest) -> OpenAIPlanResponse:
    with begin(engine) as connection:
        result = software_factory.create_plan_artifact(
            connection,
            payload.project_id,
            payload.reservation_id,
            payload.dedupe_key,
            payload.prompt,
            payload.system_prompt,
            payload.created_by_job_id,
        )
        return OpenAIPlanResponse(**result)


@app.post("/v1/software-factory/proposals", response_model=PullRequestProposalResponse)
def create_pull_request_proposal(
    payload: PullRequestProposalRequest,
) -> PullRequestProposalResponse:
    with begin(engine) as connection:
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
        )
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
