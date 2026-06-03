from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from .bootstrap import seed
from .config import get_settings
from .db import begin, database_is_reachable, get_engine
from .schemas import (
    ArtifactDetail,
    ArtifactListResponse,
    ArtifactSummary,
    BudgetQuoteRequest,
    BudgetQuoteResponse,
    BudgetReleaseRequest,
    BudgetReleaseResponse,
    BudgetStatusResponse,
    BudgetReserveRequest,
    BudgetReserveResponse,
    CommandProposeRequest,
    CommandProposeResponse,
    DecisionActionRequest,
    DecisionCard,
    DecisionInboxResponse,
    DeadletterItem,
    DeadletterListResponse,
    EventFeedResponse,
    EventItem,
    HealthResponse,
    JobCreateRequest,
    JobSummary,
    LeasedJob,
    OpenAIPlanRequest,
    OpenAIPlanResponse,
    PullRequestMergeJobRequest,
    PullRequestMergeRequest,
    PullRequestMergeResponse,
    PullRequestProposalJobRequest,
    PullRequestProposalRequest,
    PullRequestProposalResponse,
    ReceiptResponse,
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
from .services.budget import BudgetGovernor
from .services.decisions import (
    DecisionNotFoundError,
    DecisionService,
    InvalidDecisionTransitionError,
)
from .services.intake import CommandIntakeService
from .services.jobs import JobLeaseService, LeaseNotFoundError
from .services.receipts import ReceiptService
from .services.software_factory import SoftwareFactoryService

settings = get_settings()
engine = get_engine(settings)
budget = BudgetGovernor(settings)
intake = CommandIntakeService(settings)
jobs = JobLeaseService(settings)
receipts = ReceiptService()
software_factory = SoftwareFactoryService(settings)
decisions = DecisionService(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="BOSS Control Plane", version="0.1.0", lifespan=lifespan)


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

        return StatusResponse(
            project_id=row["project_id"],
            project_name=row["project_name"],
            workspace_name=row["workspace_name"],
            org_name=row["org_name"],
            database_connected=True,
            budget_state=budget_snapshot["budget_state"],
            pending_decisions=int(pending_decisions),
            open_tasks=int(open_tasks),
            blocked_reason=None if settings.github_token and settings.openai_api_key else "Missing GitHub or OpenAI credentials.",
        )


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
        )
        return PullRequestMergeResponse(**result)


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

        items = [
            EventItem(
                id=row["id"],
                event_type=row["event_type"],
                actor=row["actor"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                data=row["data"] or {},
                created_at=row["created_at"].isoformat(),
            )
            for row in rows
        ]
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


@app.get("/v1/receipts/{dedupe_key}", response_model=ReceiptResponse)
def get_receipt(dedupe_key: str) -> ReceiptResponse:
    with begin(engine) as connection:
        receipt = receipts.get_by_dedupe_key(
            connection, settings.default_org_id, dedupe_key
        )
        if not receipt:
            raise HTTPException(status_code=404, detail="Receipt not found")
        return ReceiptResponse(**receipt)
