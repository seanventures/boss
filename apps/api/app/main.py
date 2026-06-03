from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from .bootstrap import seed
from .config import get_settings
from .db import begin, database_is_reachable, get_engine
from .schemas import (
    BudgetReserveRequest,
    BudgetReserveResponse,
    HealthResponse,
    OpenAIPlanRequest,
    OpenAIPlanResponse,
    PullRequestProposalRequest,
    PullRequestProposalResponse,
    ReceiptResponse,
    StatusResponse,
)
from .services.budget import BudgetGovernor
from .services.receipts import ReceiptService
from .services.software_factory import SoftwareFactoryService

settings = get_settings()
engine = get_engine(settings)
budget = BudgetGovernor(settings)
receipts = ReceiptService()
software_factory = SoftwareFactoryService(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="BOSS Control Plane", version="0.1.0", lifespan=lifespan)


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


@app.post("/v1/bootstrap/seed")
def bootstrap_seed() -> dict[str, str]:
    seed()
    return {"status": "ok"}


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


@app.get("/v1/receipts/{dedupe_key}", response_model=ReceiptResponse)
def get_receipt(dedupe_key: str) -> ReceiptResponse:
    with begin(engine) as connection:
        receipt = receipts.get_by_dedupe_key(
            connection, settings.default_org_id, dedupe_key
        )
        if not receipt:
            raise HTTPException(status_code=404, detail="Receipt not found")
        return ReceiptResponse(**receipt)

