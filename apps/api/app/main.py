from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from .bootstrap import seed
from .config import get_settings
from .db import begin, database_is_reachable, get_engine
from .schemas import (
    BudgetStatusResponse,
    BudgetReserveRequest,
    BudgetReserveResponse,
    CommandProposeRequest,
    CommandProposeResponse,
    DecisionCard,
    DecisionInboxResponse,
    EventFeedResponse,
    EventItem,
    HealthResponse,
    OpenAIPlanRequest,
    OpenAIPlanResponse,
    PullRequestProposalRequest,
    PullRequestProposalResponse,
    ReceiptResponse,
    StatusResponse,
    TaskCreateRequest,
    TaskListResponse,
    TaskSummary,
)
from .services.budget import BudgetGovernor
from .services.intake import CommandIntakeService
from .services.receipts import ReceiptService
from .services.software_factory import SoftwareFactoryService

settings = get_settings()
engine = get_engine(settings)
budget = BudgetGovernor(settings)
intake = CommandIntakeService(settings)
receipts = ReceiptService()
software_factory = SoftwareFactoryService(settings)


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
                WHERE project_id = :project_id AND status = 'pending'
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
        )


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
        )
        return BudgetReserveResponse(**result)


@app.get("/v1/decisions/inbox", response_model=DecisionInboxResponse)
def decision_inbox(project_id: str) -> DecisionInboxResponse:
    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, question, status, risk_tier, default_action,
                       recommendation, options, cost_summary, created_at, updated_at
                FROM decisions
                WHERE project_id = :project_id
                ORDER BY
                    CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                    risk_tier DESC,
                    created_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()

        items = [
            DecisionCard(
                id=row["id"],
                question=row["question"],
                status=row["status"],
                risk_tier=row["risk_tier"],
                default_action=row["default_action"],
                recommendation=row["recommendation"] or {},
                options=row["options"] or [],
                cost_summary=row["cost_summary"] or {},
                created_at=row["created_at"].isoformat(),
                updated_at=row["updated_at"].isoformat(),
            )
            for row in rows
        ]
        return DecisionInboxResponse(project_id=project_id, items=items)


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
            payload.reservation_id,
            payload.dedupe_key,
            payload.title,
            payload.branch_name,
            payload.summary,
            payload.files,
        )
        return PullRequestProposalResponse(**result)


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


@app.get("/v1/receipts/{dedupe_key}", response_model=ReceiptResponse)
def get_receipt(dedupe_key: str) -> ReceiptResponse:
    with begin(engine) as connection:
        receipt = receipts.get_by_dedupe_key(
            connection, settings.default_org_id, dedupe_key
        )
        if not receipt:
            raise HTTPException(status_code=404, detail="Receipt not found")
        return ReceiptResponse(**receipt)
