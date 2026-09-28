"""Document common console request bodies at the mounted API boundary.

These handlers historically accept dictionaries and enforce their business rules
inside the service.  Keep every field optional and preserve extension keys so
adding OpenAPI field types does not move those rules or drop existing inputs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OpenRequestBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    def payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_unset=True)


class ProjectBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    project_id: str | None = None
    actor: str | None = None


class CodeTaskStartBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    task_id: str | None = None
    title: str | None = None
    instructions: str | None = None
    acceptance_criteria: list[str] | None = None
    test_commands: list[str] | None = None
    owner: str | None = None
    repo: str | None = None
    base_branch: str | None = None
    workflow_version_id: str | None = None
    finalize_ctx: dict[str, Any] | None = None
    profile: str | None = None
    risk_tier: int | None = None
    model_configuration: dict[str, Any] | None = Field(default=None, alias="model_config")
    open_pr: bool | None = None
    repair_retries: int | None = None
    review_panel_size: int | None = None
    simulated: bool | None = None
    official: bool | None = None


class CodeTaskFollowUpBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["instructions"]})

    instructions: str | None = None
    title: str | None = None
    profile: str | None = None
    actor: str | None = None
    engine: str | None = None
    review_panel_size: int | None = None


class CodeTaskSteerBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["guidance", "actor"]})

    guidance: str | None = None
    actor: str | None = None


class CodeTaskFeedbackBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["message"]})

    message: str | None = None
    expected: str | None = None
    anchor: str | None = None
    severity: str | None = None
    selected_element: str | None = None
    screenshot_url: str | None = None


class PrdDecomposeBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    prd: str | None = None
    directive: str | None = None
    owner: str | None = None
    repo: str | None = None
    base_branch: str | None = None
    task_id: str | None = None
    profile: str | None = None
    risk_tier: int | None = None
    max_tasks: int | None = None
    plan_only: bool | None = None


class WebAppBuildBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    directive: str | None = None
    prd: str | None = None
    owner: str | None = None
    repo: str | None = None
    task_id: str | None = None
    review: bool | None = None


class WorkflowCreateBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "name"]})

    name: str | None = None
    description: str | None = None


class WorkflowForkBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "workflow_key"]})

    workflow_key: str | None = None
    new_key: str | None = None
    new_name: str | None = None


class WorkflowSaveBody(ProjectBody):
    model_config = ConfigDict(
        json_schema_extra={"required": ["project_id", "definition_id", "spec"]}
    )

    definition_id: str | None = None
    spec: dict[str, Any] | None = None


class WorkflowApplyBody(ProjectBody):
    model_config = ConfigDict(
        json_schema_extra={"required": ["project_id", "workflow_key", "work_class"]}
    )

    workflow_key: str | None = None
    work_class: str | None = None
    priority: int | None = None


class WorkflowStartBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    workflow_key: str | None = None
    work_class: str | None = None
    input: dict[str, Any] | None = None
    from_run: str | None = None
    from_discovery: str | None = None
    dedupe_key: str | None = None
    sample: bool | None = None


class WorkflowScheduleBody(ProjectBody):
    model_config = ConfigDict(
        json_schema_extra={"required": ["project_id", "workflow_key", "name", "cadence"]}
    )

    workflow_key: str | None = None
    name: str | None = None
    cadence: str | None = None
    minute: int | None = None
    hour: int | None = None
    weekday: int | None = None
    timezone: str | None = None
    input: dict[str, Any] | None = None
    enabled: bool | None = None


class WorkflowScheduleEnabledBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    enabled: bool | None = None


class WorkflowRetryStepBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "step_id"]})

    step_id: str | None = None


class WorkflowRecheckBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    requeue: bool | None = None


class WorkflowRunInputBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "values"]})

    values: dict[str, Any] | None = None


class WorkflowDeployTargetBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "host_id"]})

    host_id: str | None = None
    environment: str | None = None


class WorkflowCustomStepBody(ProjectBody):
    model_config = ConfigDict(
        json_schema_extra={"required": ["project_id", "label", "binding_kind"]}
    )

    label: str | None = None
    summary: str | None = None
    binding_kind: str | None = None
    skill_id: str | None = None
    mcp_server_id: str | None = None
    mcp_tool_name: str | None = None
    inputs: Any = None
    knowledge: str | None = None
    produces: Any = None
    step_key: str | None = None


class WorkflowCustomStepUpdateBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    label: str | None = None
    summary: str | None = None
    binding_kind: str | None = None
    skill_id: str | None = None
    mcp_server_id: str | None = None
    mcp_tool_name: str | None = None
    inputs: Any = None
    knowledge: str | None = None
    produces: Any = None


class ProjectAgentsBody(OpenRequestBody):
    app: str | None = None
    app_name: str | None = None
    actor: str | None = None
    action: str | None = None
    role_key: str | None = None
    role_keys: list[str] | None = None


class RoleSkillBody(ProjectBody):
    model_config = ConfigDict(
        json_schema_extra={"required": ["project_id", "role_key", "skill_id"]}
    )

    role_key: str | None = None
    skill_id: str | None = None
