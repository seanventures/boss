"""Document common console request bodies at the mounted API boundary.

These handlers historically accept dictionaries and enforce their business rules
inside the service.  Keep every field optional and preserve extension keys so
adding OpenAPI field types does not move those rules or drop existing inputs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SkipValidation


class OpenRequestBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    def payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_unset=True, warnings=False)


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


class OpenProjectBody(OpenRequestBody):
    """Document project fields without changing handlers' input coercion."""

    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None


class OrgOnboardBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["name"]})

    name: SkipValidation[str | None] = None


class DeploymentTargetCreateBody(OpenProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": [
        "project_id", "app_name", "repo", "environment", "target_kind", "target_ref", "actor"
    ]})

    id: SkipValidation[str | None] = None
    app_name: SkipValidation[str | None] = None
    repo: SkipValidation[str | None] = None
    environment: SkipValidation[str | None] = None
    target_kind: SkipValidation[str | None] = None
    target_ref: SkipValidation[str | None] = None
    app_port: SkipValidation[int | str | None] = None
    app_entry: SkipValidation[str | None] = None
    repo_owner: SkipValidation[str | None] = None
    publishable_ports: SkipValidation[str | int | list[int] | None] = None
    domains: SkipValidation[list[str] | None] = None
    secret_refs: SkipValidation[list[Any] | None] = None
    health_checks: SkipValidation[list[dict[str, Any]] | None] = None
    scheme: SkipValidation[str | None] = None
    status: SkipValidation[str | None] = None


class DeploymentTargetUpdateBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["actor"]})

    actor: SkipValidation[str | None] = None
    target_ref: SkipValidation[str | None] = None
    status: SkipValidation[str | None] = None
    environment: SkipValidation[str | None] = None
    app_name: SkipValidation[str | None] = None
    domains: SkipValidation[list[str] | None] = None
    health_checks: SkipValidation[list[dict[str, Any]] | None] = None
    secret_refs: SkipValidation[list[Any] | None] = None
    required_resources: SkipValidation[list[dict[str, Any]] | None] = None
    app_port: SkipValidation[int | str | None] = None
    app_entry: SkipValidation[str | None] = None
    repo_owner: SkipValidation[str | None] = None
    publishable_ports: SkipValidation[str | int | list[int] | None] = None


class DeploymentTargetTeardownBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["actor"]})

    actor: SkipValidation[str | None] = None
    stop_only: SkipValidation[bool | None] = None
    confirm: SkipValidation[str | None] = None
    include_data: SkipValidation[bool | None] = None


class DeploymentTargetPurgeBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["actor", "confirm"]})

    actor: SkipValidation[str | None] = None
    confirm: SkipValidation[str | None] = None


class SecretPutBody(OpenProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "name", "value"]})

    name: SkipValidation[str | None] = None
    value: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})
    secret_kind: SkipValidation[str | None] = None
    backend: SkipValidation[str | None] = None
    scope_type: SkipValidation[str | None] = None
    scope_id: SkipValidation[str | None] = None


class SecretKeypairBody(OpenProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "name"]})

    name: SkipValidation[str | None] = None
    algorithm: SkipValidation[str | None] = None
    backend: SkipValidation[str | None] = None
    scope_type: SkipValidation[str | None] = None
    scope_id: SkipValidation[str | None] = None
    comment: SkipValidation[str | None] = None


class TargetSecretBindBody(OpenProjectBody):
    model_config = ConfigDict(json_schema_extra={
        "required": ["project_id", "name", "secret_ref_id"]
    })

    name: SkipValidation[str | None] = None
    secret_ref_id: SkipValidation[str | None] = None


class ObservabilityStackDeployBody(OpenProjectBody):
    model_config = ConfigDict(json_schema_extra={
        "required": ["project_id", "host_target_id"]
    })

    host_target_id: SkipValidation[str | None] = None
    ports: SkipValidation[dict[str, Any] | None] = None


class ManualDeployBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["target_id", "actor"]})

    target_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    code_task_run_id: SkipValidation[str | None] = None
    ref: SkipValidation[str | None] = None
    commit_ref: SkipValidation[str | None] = None
    expected_cost_usd_micros: SkipValidation[int | str | None] = None
    reservation_id: SkipValidation[str | None] = None
    plan_only: SkipValidation[bool | None] = None
    wait: SkipValidation[bool | None] = None


class DeploymentHostUpdateBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["actor"]})

    actor: SkipValidation[str | None] = None
    address: SkipValidation[str | None] = None
    ssh_user: SkipValidation[str | None] = None
    ssh_port: SkipValidation[int | str | None] = None
    arch: SkipValidation[str | None] = None
    notes: SkipValidation[str | None] = None
    admin_identity_file: SkipValidation[str | None] = None
    max_apps: SkipValidation[int | None] = None


class DeploymentHostRegisterBody(OpenProjectBody):
    model_config = ConfigDict(json_schema_extra={
        "required": ["project_id", "name", "address", "actor"]
    })

    name: SkipValidation[str | None] = None
    address: SkipValidation[str | None] = None
    ssh_user: SkipValidation[str | None] = None
    ssh_port: SkipValidation[int | str | None] = None
    arch: SkipValidation[str | None] = None
    labels: SkipValidation[dict[str, Any] | None] = None
    port_min: SkipValidation[int | str | None] = None
    port_max: SkipValidation[int | str | None] = None
    max_apps: SkipValidation[int | None] = None
    notes: SkipValidation[str | None] = None
    admin_identity_file: SkipValidation[str | None] = None


class DeploymentHostAdoptKeyBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["actor", "private_key"]})

    actor: SkipValidation[str | None] = None
    private_key: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})
    backend: SkipValidation[str | None] = None


class DeploymentHostAttachBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["target_id", "actor"]})

    target_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None


class DeploymentHostBackfillBody(OpenProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "actor"]})


class AutoDeployBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["actor"]})

    actor: SkipValidation[str | None] = None
    auto_deploy: SkipValidation[bool | None] = None
    paused: SkipValidation[bool | None] = None


class DeploymentPromoteBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["actor"]})

    actor: SkipValidation[str | None] = None
    note: SkipValidation[str | None] = None


class FirstDeployBody(OpenProjectBody):
    model_config = ConfigDict(json_schema_extra={
        "required": ["project_id", "run_id", "actor"]
    })

    run_id: SkipValidation[str | None] = None
    environment: SkipValidation[str | None] = None
    target_id: SkipValidation[str | None] = None
    expected_cost_usd_micros: SkipValidation[int | str | None] = None
    reservation_id: SkipValidation[str | None] = None


class DecisionModelKeyBody(OpenProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    api_key: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})
    accept_database_fallback: SkipValidation[bool | None] = None


class ProviderUsageIngestBody(OpenProjectBody):
    model_config = ConfigDict(json_schema_extra={
        "required": ["project_id", "provider", "operation"]
    })

    provider: SkipValidation[str | None] = None
    operation: SkipValidation[str | None] = None
    category: SkipValidation[str | None] = None
    quantity: SkipValidation[float | str | None] = None
    unit: SkipValidation[str | None] = None
    provider_account_id: SkipValidation[str | None] = None
    cost_usd_micros: SkipValidation[int | str | None] = None
    resource_ref: SkipValidation[str | None] = None
    worker_instance_id: SkipValidation[str | None] = None
    receipt_id: SkipValidation[str | None] = None
    source: SkipValidation[str | None] = None
    metadata: SkipValidation[dict[str, Any] | None] = None


class ProviderUsageSyncBody(OpenProjectBody):
    model_config = ConfigDict(json_schema_extra={
        "required": ["project_id", "provider_account_id"]
    })

    provider_account_id: SkipValidation[str | None] = None


class ProjectSettingBody(OpenProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    value: SkipValidation[str | int | None] = None
