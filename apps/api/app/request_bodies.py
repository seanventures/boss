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
    #: Readiness keys the operator was shown missing and chose to start without — "I will add
    #: the deployment host before the release". Anything missing that is not named still
    #: refuses (see workflow_readiness.refuse_unless_ready).
    start_anyway: list[str] | None = None
    #: "Run this again": the run this one repeats. What the operator accepted starting that run
    #: without carries over; anything newly missing still refuses.
    rerun_of: str | None = None
    #: "Stop and run again": the live run this one replaces. Checked FIRST, then stopped, then
    #: started — one transaction — so a refused re-run leaves the original running. The
    #: console stopped first and started second, and a refused start left no run at all.
    replaces_run: str | None = None


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
    # Present only when the caller is replacing the inputs of a schedule that already
    # exists. Absent means create. The same body stays one operation, so the mounted
    # count does not grow for an edit of values the create call already accepted.
    schedule_id: str | None = None
    #: Readiness keys this schedule may start without — the unattended form of Start anyway.
    #: Omitted on an edit, the stored choice stays.
    start_anyway: list[str] | None = None


class WorkflowScheduleEnabledBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    enabled: bool | None = None


class WorkflowRetryStepBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "step_id"]})

    step_id: str | None = None


class WorkflowSkipStepBody(ProjectBody):
    model_config = ConfigDict(
        json_schema_extra={"required": ["project_id", "step_id", "reason"]}
    )

    step_id: str | None = None
    # Required, because "why did this run go on without research?" must be answerable from the
    # run's record rather than from somebody's memory.
    reason: str | None = None


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
    # With host_id, target_ref is derived from the host and may be omitted.
    host_id: SkipValidation[str | None] = None


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


# Remaining Community operations retain their handler validation and pass-through inputs.

class ResumeAgentRunBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['worker_id', 'job_id', 'lease_token']})

    worker_id: SkipValidation[str | None] = None
    job_id: SkipValidation[str | None] = None
    lease_token: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})

class PlaneSyncBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['run_id', 'event']})

    run_id: SkipValidation[str | None] = None
    event: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None

class MeetingRoleUpsertBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'title', 'lens']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    title: SkipValidation[str | None] = None
    lens: SkipValidation[str | None] = None
    key: SkipValidation[str | None] = None
    risk_tier: SkipValidation[int | None] = None
    is_default: SkipValidation[bool | None] = None
    enabled: SkipValidation[bool | None] = None

class MeetingStepBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['run_id']})

    run_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None

class BenchmarkAppLaunchBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    simulated: SkipValidation[bool | None] = None
    official: SkipValidation[bool | None] = None
    engine: SkipValidation[str | None] = None
    route: SkipValidation[str | None] = None
    open_pr: SkipValidation[bool | None] = None
    force: SkipValidation[bool | None] = None
    task_id: SkipValidation[str | None] = None
    owner: SkipValidation[str | None] = None
    repo: SkipValidation[str | None] = None
    base_branch: SkipValidation[str | None] = None
    profile: SkipValidation[str | None] = None
    risk_tier: SkipValidation[int | None] = None
    repair_retries: SkipValidation[int | None] = None

class PipelineProfileCreateBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'name']})

    project_id: SkipValidation[str | None] = None
    name: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    label: SkipValidation[str | None] = None
    tier_floor: SkipValidation[int | None] = None
    tier_cap: SkipValidation[int | None] = None
    model: SkipValidation[dict[str, Any] | None] = None
    model_routing: SkipValidation[dict[str, Any] | None] = None
    caching: SkipValidation[bool | None] = None
    parallel_checks: SkipValidation[bool | None] = None

class PipelineProfileDefaultBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'name']})

    project_id: SkipValidation[str | None] = None
    name: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None

class CodeTaskStepBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['run_id']})

    run_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    fencing_token: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})

class InvestigationDbBody(OpenRequestBody):
    sql: SkipValidation[str | None] = None

class InvestigationHostBody(OpenRequestBody):
    host: SkipValidation[str | None] = None
    command: SkipValidation[str | None] = None

class InvestigationOperationBody(OpenRequestBody):
    list: SkipValidation[bool | None] = None
    tool: SkipValidation[str | None] = None
    args: SkipValidation[dict[str, Any] | None] = None

class HostStallBody(OpenRequestBody):
    stalled_seconds: SkipValidation[float | None] = None

class CodeTaskRedeliveredBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['worker_id', 'job_id', 'lease_token', 'attempt']})

    worker_id: SkipValidation[str | None] = None
    job_id: SkipValidation[str | None] = None
    lease_token: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})
    attempt: SkipValidation[int | None] = None

class ShepherdProposeBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['run_id']})

    run_id: SkipValidation[str | None] = None

class ShepherdSweepBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    limit: SkipValidation[int | None] = None

class ShepherdAutopilotBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    limit: SkipValidation[int | None] = None
    actor: SkipValidation[str | None] = None

class ShepherdAnswerBody(OpenRequestBody):
    answer: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None

class CodeTaskSetupDeploymentBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['actor', 'host_id', 'app_name']})

    actor: SkipValidation[str | None] = None
    host_id: SkipValidation[str | None] = None
    app_name: SkipValidation[str | None] = None
    repo: SkipValidation[str | None] = None
    environment: SkipValidation[str | None] = None
    app_port: SkipValidation[int | str | None] = None
    app_entry: SkipValidation[str | None] = None
    repo_owner: SkipValidation[str | None] = None
    auto_deploy: SkipValidation[bool | None] = None

class PreviewFeedbackBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ["message"]})

    message: SkipValidation[str | None] = None
    expected: SkipValidation[str | None] = None
    anchor: SkipValidation[str | None] = None
    severity: SkipValidation[str | None] = None
    selected_element: SkipValidation[str | None] = None
    screenshot_url: SkipValidation[str | None] = None

class PreviewApproveBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['actor']})

    actor: SkipValidation[str | None] = None
    note: SkipValidation[str | None] = None

class CodeTaskOriginBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['kind', 'actor']})

    kind: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    meeting_run_id: SkipValidation[str | None] = None
    task_id: SkipValidation[str | None] = None
    discovery_run_id: SkipValidation[str | None] = None

class VerifierRatchetCaptureBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['run_id', 'label']})

    run_id: SkipValidation[str | None] = None
    label: SkipValidation[str | None] = None
    source: SkipValidation[str | None] = None
    bug_class: SkipValidation[str | None] = None
    files: SkipValidation[list[dict[str, Any]] | None] = None
    reason: SkipValidation[str | None] = None

class MutationGateBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['files', 'target_file']})

    files: SkipValidation[dict[str, str] | None] = None
    target_file: SkipValidation[str | None] = None
    test_command: SkipValidation[list[str] | None] = None
    min_score: SkipValidation[float | None] = None
    max_mutants: SkipValidation[int | None] = None
    timeout: SkipValidation[float | None] = None
    require_property_tests: SkipValidation[bool | None] = None

class VisualGateBody(OpenRequestBody):
    evidence: SkipValidation[dict[str, Any] | None] = None

class DiscoveryStartBody(OpenRequestBody):
    seed: SkipValidation[str | None] = None
    scope: SkipValidation[str | None] = None
    title: SkipValidation[str | None] = None
    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    context: SkipValidation[str | None] = None

class DiscoveryExecuteBody(BaseModel):
    """The worker's lease of a discovery.run job. All three are required: every research stage
    spends only under this lease, so a call without one must not reach the pipeline."""
    job_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)

class DiscoveryPromoteBody(OpenRequestBody):
    outcome_index: SkipValidation[int | None] = None
    actor: SkipValidation[str | None] = None

class SandboxExecBody(OpenRequestBody):
    actor: SkipValidation[str | None] = None
    command: SkipValidation[str | None] = None
    break_glass: SkipValidation[bool | None] = None
    reason: SkipValidation[str | None] = None

class SandboxPreserveBody(OpenRequestBody):
    actor: SkipValidation[str | None] = None

class AgentWakeupsTickBody(OpenRequestBody):
    limit: SkipValidation[int | None] = None
    project_id: SkipValidation[str | None] = None

class MemoryReviewBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'actor']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    decision: SkipValidation[str | None] = None

class MemoryForgetBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'actor']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None

class AgentThreadEnsureBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'actor']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    surface: SkipValidation[str | None] = None
    external_ref: SkipValidation[str | None] = None

class OrgAssignmentBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    reports_to: SkipValidation[str | None] = None
    display_name: SkipValidation[str | None] = None
    sort: SkipValidation[int | None] = None
    actor: SkipValidation[str | None] = None

class AutonomySetBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'actor']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    mode: SkipValidation[str | None] = None
    threshold_usd: SkipValidation[float | str | None] = None
    max_risk_tier: SkipValidation[int | None] = None

class AutonomyApplyBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    limit: SkipValidation[int | None] = None

class ArtifactRebuildBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None

class SkillDefinitionSaveBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    skill_key: SkipValidation[str | None] = None
    label: SkipValidation[str | None] = None
    behaviour_kind: SkipValidation[str | None] = None
    summary: SkipValidation[str | None] = None
    knowledge: SkipValidation[str | None] = None
    inputs: SkipValidation[list[Any] | None] = None
    produces: SkipValidation[list[Any] | None] = None
    config: SkipValidation[dict[str, Any] | None] = None
    risk_tier: SkipValidation[int | None] = None
    trusted: SkipValidation[bool | None] = None
    enabled: SkipValidation[bool | None] = None
    #: create | update | upsert (the default, for scripts written before the distinction).
    mode: SkipValidation[str | None] = None

class SkillDefinitionSwitchBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'enabled']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    enabled: SkipValidation[bool | None] = None

class ChannelOAuthClientBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    client_id: SkipValidation[str | None] = None
    client_secret: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})
    redirect_uri: SkipValidation[str | None] = None

class ChannelConnectBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    connection_key: SkipValidation[str | None] = None

class CodingAgentCredentialBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'provider']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    provider: SkipValidation[str | None] = None
    credential: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})
    accept_database_fallback: SkipValidation[bool | None] = None

class CodingAgentConnectBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'provider']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    provider: SkipValidation[str | None] = None
    prove: SkipValidation[bool | None] = None

class ModelSetupBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'provider']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    provider: SkipValidation[str | None] = None
    api_key: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})
    base_url: SkipValidation[str | None] = None
    model: SkipValidation[str | None] = None

class ElasticCapacityBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'desired_workers']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    desired_workers: SkipValidation[int | None] = None
    pool_key: SkipValidation[str | None] = None
    worker_pool_id: SkipValidation[str | None] = None
    kind: SkipValidation[str | None] = None
    provider_account_id: SkipValidation[str | None] = None
    capabilities: SkipValidation[list[str] | None] = None
    auto_reconcile: SkipValidation[bool | None] = None
    cloud_spillover_enabled: SkipValidation[bool | None] = None
    reason: SkipValidation[str | None] = None
    metadata: SkipValidation[dict[str, Any] | None] = None

class ElasticReconcileBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    cloud_spillover_enabled: SkipValidation[bool | None] = None
    reason: SkipValidation[str | None] = None
    metadata: SkipValidation[dict[str, Any] | None] = None

class ElasticHeartbeatBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    status: SkipValidation[str | None] = None
    endpoint: SkipValidation[str | None] = None
    metadata: SkipValidation[dict[str, Any] | None] = None

class ElasticTerminateBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    reason: SkipValidation[str | None] = None
    destroy_provider_resource: SkipValidation[bool | None] = None






class EmailConfigBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    host: SkipValidation[str | None] = None
    from_address: SkipValidation[str | None] = None
    port: SkipValidation[int | str | None] = None
    username: SkipValidation[str | None] = None
    password: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})
    use_tls: SkipValidation[bool | None] = None
    use_ssl: SkipValidation[bool | None] = None
    from_name: SkipValidation[str | None] = None

class WorkflowSchedulesTickBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None

class DeliveryCapabilitiesRunBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    worker_id: SkipValidation[str | None] = None

class WorkTrackerConnectBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'provider_key']})

    project_id: SkipValidation[str | None] = None
    provider_key: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    configuration: SkipValidation[dict[str, Any] | None] = None
    set_as_default: SkipValidation[bool | None] = None
    test: SkipValidation[bool | None] = None
    api_token: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})

class WorkTrackerDefaultBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'provider_key']})

    project_id: SkipValidation[str | None] = None
    provider_key: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None

class WorkTrackerTestBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id', 'provider_key']})

    project_id: SkipValidation[str | None] = None
    provider_key: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None

class CaptainPlaybookCompileBody(OpenRequestBody):
    text: SkipValidation[str | None] = None
    instructions: SkipValidation[str | None] = None
    name: SkipValidation[str | None] = None

class CloudEasyConnectBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    method: SkipValidation[str | None] = None
    return_url: SkipValidation[str | None] = None

class CloudEasyCompleteBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    code: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})
    state: SkipValidation[str | None] = None

class CloudEasyCompleteByStateBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    session_id: SkipValidation[str | None] = None
    code: SkipValidation[str | None] = Field(default=None, json_schema_extra={"writeOnly": True})
    state: SkipValidation[str | None] = None

class CloudUnlinkBody(OpenRequestBody):
    model_config = ConfigDict(json_schema_extra={"required": ['project_id']})

    project_id: SkipValidation[str | None] = None
    actor: SkipValidation[str | None] = None
    reason: SkipValidation[str | None] = None


class StackTargetCreateBody(OpenProjectBody):
    """Where a stack is deployed: a repository, on a fleet host, for an environment."""

    model_config = ConfigDict(json_schema_extra={
        "required": ["project_id", "host_id", "repository", "actor"]
    })

    host_id: SkipValidation[str | None] = None
    repository: SkipValidation[str | None] = None
    app_name: SkipValidation[str | None] = None
    environment: SkipValidation[str | None] = None
    manifest_path: SkipValidation[str | None] = None
    public_url: SkipValidation[str | None] = None


class StackDeployPlanBody(OpenProjectBody):
    """Plan repository@ref onto a stack target; held for approval by a decision."""

    model_config = ConfigDict(json_schema_extra={
        "required": ["project_id", "target_id", "ref"]
    })

    target_id: SkipValidation[str | None] = None
    repository: SkipValidation[str | None] = None
    ref: SkipValidation[str | None] = None


class StackDeployRollbackBody(OpenRequestBody):
    actor: SkipValidation[str | None] = None


class StackDeployExecuteBody(BaseModel):
    """The worker's lease of a deployment.stack_apply job. All three are required: the apply
    writes to a host, and only the job's current lease may start it."""
    job_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)


# ── The workflow editor (Workflow Studio) ──────────────────────────────────────────────────────


class WorkflowCheckBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "spec"]})

    spec: dict[str, Any] | None = None
    definition_id: str | None = None


class WorkflowPublishBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "name", "spec"]})

    definition_id: str | None = None
    name: str | None = None
    description: str | None = None
    spec: dict[str, Any] | None = None
    from_workflow_key: str | None = None
    update_copy_id: str | None = None


class WorkflowRenameBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id"]})

    name: str | None = None
    description: str | None = None


class WorkflowDraftBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "description"]})

    description: str | None = None


class WorkflowWireBody(ProjectBody):
    model_config = ConfigDict(json_schema_extra={"required": ["project_id", "spec"]})

    spec: dict[str, Any] | None = None
    ids: list[str] | None = None
