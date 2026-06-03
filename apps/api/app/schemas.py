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
    blocked_reason: Optional[str] = None


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
    expires_in_minutes: int = Field(default=30, ge=5, le=240)


class BudgetReserveResponse(BaseModel):
    status: str
    reservation_id: Optional[str] = None
    decision_id: Optional[str] = None
    budget_state: str
    message: str


class BudgetStatusResponse(BaseModel):
    scope_type: str
    scope_id: str
    budget_state: str
    cap_usd_micros: int
    reserved_usd_micros: int
    actual_usd_micros: int


class OpenAIPlanRequest(BaseModel):
    project_id: str
    reservation_id: str
    dedupe_key: str
    prompt: str
    system_prompt: Optional[str] = None


class OpenAIPlanResponse(BaseModel):
    status: str
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    output_text: Optional[str] = None
    decision_id: Optional[str] = None


class FileChange(BaseModel):
    path: str
    content: str


class PullRequestProposalRequest(BaseModel):
    project_id: str
    reservation_id: str
    dedupe_key: str
    title: str
    branch_name: str
    summary: str
    files: list[FileChange]


class PullRequestProposalResponse(BaseModel):
    status: str
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    pull_request_url: Optional[str] = None
    branch_name: Optional[str] = None
    message: Optional[str] = None


class ReceiptResponse(BaseModel):
    id: str
    dedupe_key: str
    effect_kind: str
    status: str
    provider_ref: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None


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


class DecisionCard(BaseModel):
    id: str
    question: str
    status: str
    risk_tier: int
    default_action: Optional[str] = None
    recommendation: dict[str, Any]
    options: list[dict[str, Any]]
    cost_summary: dict[str, Any]
    created_at: str
    updated_at: str


class DecisionInboxResponse(BaseModel):
    project_id: str
    items: list[DecisionCard]


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
