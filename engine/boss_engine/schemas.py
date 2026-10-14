from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

DataBoundary = Literal["public", "internal", "confidential", "restricted", "local-only"]


def _require_text(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank.")
    return cleaned


def _require_non_empty_string_list(values: list[str], field_name: str) -> list[str]:
    cleaned = [item.strip() for item in values if isinstance(item, str) and item.strip()]
    if not cleaned:
        raise ValueError(f"{field_name} must contain at least one non-empty item.")
    return cleaned


def _require_non_empty_mapping(
    values: dict[str, Any], field_name: str
) -> dict[str, Any]:
    if not values:
        raise ValueError(f"{field_name} must contain at least one entry.")
    return values


def _require_evidence(
    metrics: dict[str, Any], evidence_refs: list[str]
) -> tuple[dict[str, Any], list[str]]:
    """Report every missing evidence field, not only the first one."""
    problems: list[str] = []
    try:
        metrics = _require_non_empty_mapping(metrics, "metrics")
    except ValueError as exc:
        problems.append(str(exc))
    try:
        evidence_refs = _require_non_empty_string_list(evidence_refs, "evidence_refs")
    except ValueError as exc:
        problems.append(str(exc))
    if problems:
        raise ValueError(" ".join(problems))
    return metrics, evidence_refs


class HealthResponse(BaseModel):
    status: str
    database_connected: bool
    github_configured: bool
    openai_configured: bool


class ProjectSummary(BaseModel):
    id: str
    name: str
    slug: str
    workspace_id: str
    workspace_name: str
    org_id: str
    org_name: str
    created_at: str


class ProjectListResponse(BaseModel):
    items: list[ProjectSummary]


class AuthSessionResponse(BaseModel):
    auth_required: bool
    transport_authenticated: bool
    actor_binding_required: bool
    principal_member_id: Optional[str] = None
    auth_method: Optional[str] = None
    oidc_enabled: bool = False
    message: str
    # BOSS Identity (IDENTITY.md §10): who the credential is, when it has an identity context.
    principal: Optional[dict[str, Any]] = None
    org: Optional[str] = None
    mfa_at: Optional[str] = None
    csrf: Optional[str] = None
    expires: Optional[str] = None
    #: the projects an API key is pinned to (None: not pinned, or not a key)
    key_projects: Optional[list[str]] = None


class SamlLoginResponse(BaseModel):
    project_id: str
    session_id: str
    access_token: str
    token_type: str = "bearer"
    member_id: str
    provider_id: str
    expires_at: str


class AccountConnectOrgSummary(BaseModel):
    id: str
    project_id: str
    hosted_org_id: str
    local_org_id: str
    connection_mode: str
    marketplace_identity: dict[str, Any]
    billing_portal_ref: Optional[str] = None
    connector_authorization_refs: list[str]
    optional_telemetry_enabled: bool
    connection_ref: Optional[str] = None
    status: str
    linked_by: str
    linked_at: Optional[str] = None
    unlinked_by: Optional[str] = None
    unlinked_at: Optional[str] = None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class EditionStatusResponse(BaseModel):
    project_id: str
    edition_id: str
    display_name: str
    status: str
    account_connect_required: bool
    account_connect_status: str
    account_connect_org: Optional[AccountConnectOrgSummary] = None
    local_auth_allowed: bool
    hosted_auth_required: bool
    connection_mode: str
    license_candidate: Optional[str] = None
    legal_review_required: bool
    included_capabilities: list[str]
    commercial_capabilities: list[str]
    blocked_reason: Optional[str] = None
    source_pack: str


class CloudCatalogResponse(BaseModel):
    project_id: str
    edition_id: str
    independence: dict[str, Any]
    cloud_features: list[dict[str, Any]]
    enterprise_features: list[dict[str, Any]]
    packaging: dict[str, str]


class CloudStatusResponse(BaseModel):
    project_id: str
    edition: EditionStatusResponse
    independent: bool
    linked: bool
    easy_connect: dict[str, Any]
    message: str


class CloudEasyConnectRequest(BaseModel):
    project_id: str
    method: str = "browser"
    return_url: Optional[str] = None
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_easy_connect(self) -> "CloudEasyConnectRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.method = _require_text(self.method, "method").lower()
        self.actor = _require_text(self.actor, "actor")
        if self.method not in {"browser", "device"}:
            raise ValueError("method must be browser or device")
        return self


class CloudEasyConnectResponse(BaseModel):
    session_id: str
    status: str
    method: str
    authorization_url: Optional[str] = None
    user_code: Optional[str] = None
    verification_uri: Optional[str] = None
    device_code: Optional[str] = None
    poll_interval_seconds: int = 5
    expires_at: Optional[str] = None
    message: Optional[str] = None
    receipt_id: Optional[str] = None
    hosted_org_id: Optional[str] = None
    linked: Optional[bool] = None


class LicenseEntitlementSummary(BaseModel):
    id: str
    project_id: str
    edition_id: str
    account_connect_org_id: Optional[str] = None
    scope_type: str
    scope_id: str
    capability_key: str
    capabilities: list[str]
    seats: Optional[int] = None
    status: str
    source: str
    expires_at: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class LicenseEntitlementListResponse(BaseModel):
    project_id: str
    edition: EditionStatusResponse
    items: list[LicenseEntitlementSummary]








































































class EnterpriseIdentityProviderSummary(BaseModel):
    id: str
    org_id: str
    project_id: str
    provider_key: str
    provider_type: str
    display_name: str
    issuer: Optional[str] = None
    audience: Optional[str] = None
    entity_id: Optional[str] = None
    sso_url: Optional[str] = None
    jwks_url: Optional[str] = None
    certificate_fingerprint: Optional[str] = None
    scim_base_url: Optional[str] = None
    secret_ref: Optional[str] = None
    status: str
    test_status: str
    blocking_gaps: list[str]
    metadata: dict[str, Any]
    created_by: str
    created_at: str
    updated_at: str


class EnterpriseIdentityProviderListResponse(BaseModel):
    project_id: str
    items: list[EnterpriseIdentityProviderSummary]


class EnterpriseIdentityRolloutPolicySummary(BaseModel):
    id: str
    org_id: str
    project_id: str
    provider_type: str
    runtime_mode: str
    allowed_domains: list[str]
    rollout_notes: Optional[str] = None
    status: str
    created_by: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class EnterpriseIdentityRolloutPolicyListResponse(BaseModel):
    project_id: str
    items: list[EnterpriseIdentityRolloutPolicySummary]


class EnterpriseIdentityRolloutPolicyUpsertRequest(BaseModel):
    project_id: str
    provider_type: str
    runtime_mode: Literal[
        "disabled",
        "metadata_only",
        "auth_allowed",
        "provisioning_allowed",
    ] = "disabled"
    allowed_domains: list[str] = Field(default_factory=list)
    rollout_notes: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_identity_rollout_policy(
        self,
    ) -> "EnterpriseIdentityRolloutPolicyUpsertRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.provider_type = _require_text(self.provider_type, "provider_type")
        self.actor = _require_text(self.actor, "actor")
        self.allowed_domains = [
            domain.strip().lower()
            for domain in self.allowed_domains
            if domain and domain.strip()
        ]
        return self


class EnterpriseIdentityProviderUpsertRequest(BaseModel):
    project_id: str
    provider_key: str
    provider_type: str
    display_name: str
    issuer: Optional[str] = None
    audience: Optional[str] = None
    entity_id: Optional[str] = None
    sso_url: Optional[str] = None
    jwks_url: Optional[str] = None
    certificate_fingerprint: Optional[str] = None
    scim_base_url: Optional[str] = None
    secret_ref: Optional[str] = None
    status: str = "planned"
    test_status: str = "not_tested"
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_identity_provider(
        self,
    ) -> "EnterpriseIdentityProviderUpsertRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.provider_key = _require_text(self.provider_key, "provider_key")
        self.provider_type = _require_text(self.provider_type, "provider_type")
        self.display_name = _require_text(self.display_name, "display_name")
        self.status = _require_text(self.status, "status")
        self.test_status = _require_text(self.test_status, "test_status")
        self.actor = _require_text(self.actor, "actor")
        return self


class EnterpriseScimGroupSummary(BaseModel):
    id: str
    org_id: str
    project_id: str
    external_id: Optional[str] = None
    display_name: str
    role_keys: list[str]
    permissions: list[str]
    raw_group: dict[str, Any]
    status: str
    created_by: str
    created_at: str
    updated_at: str


class EnterpriseScimGroupListResponse(BaseModel):
    project_id: str
    items: list[EnterpriseScimGroupSummary]


class EnterpriseScimGroupUpsertRequest(BaseModel):
    project_id: str
    external_id: Optional[str] = None
    display_name: str
    role_keys: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    raw_group: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    actor: str = "system"

    @model_validator(mode="after")
    def validate_scim_group(self) -> "EnterpriseScimGroupUpsertRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.display_name = _require_text(self.display_name, "display_name")
        self.status = _require_text(self.status, "status")
        self.actor = _require_text(self.actor, "actor")
        return self


class EnterpriseScimUserSummary(BaseModel):
    id: str
    org_id: str
    project_id: str
    external_id: Optional[str] = None
    user_name: str
    display_name: str
    member_id: str
    role_keys: list[str]
    permissions: list[str]
    groups: list[str]
    raw_profile: dict[str, Any]
    status: str
    active: bool
    created_by: str
    created_at: str
    updated_at: str


class EnterpriseScimUserListResponse(BaseModel):
    project_id: str
    items: list[EnterpriseScimUserSummary]


class EnterpriseScimUserUpsertRequest(BaseModel):
    project_id: str
    external_id: Optional[str] = None
    user_name: str
    display_name: str
    active: bool = True
    role_keys: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    raw_profile: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_scim_user(self) -> "EnterpriseScimUserUpsertRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.user_name = _require_text(self.user_name, "user_name")
        self.display_name = _require_text(self.display_name, "display_name")
        self.actor = _require_text(self.actor, "actor")
        return self


class ProjectMemberSummary(BaseModel):
    id: str
    project_id: str
    member_id: str
    display_name: str
    member_type: str
    role_keys: list[str]
    permissions: list[str]
    status: str
    created_by: str
    created_at: str
    updated_at: str


class ProjectMemberListResponse(BaseModel):
    project_id: str
    items: list[ProjectMemberSummary]


#: F2/H5: a SYSTEM member maps to a fixed system principal at the identity migration, so the type
#: comes from configuration only (BOSS_WORKER_PRINCIPAL and the seeded system members), never from
#: the API — a member write naming it would mint an Owner-equivalent principal.
API_REFUSED_MEMBER_TYPES = frozenset({"system"})


def _require_api_member_type(value: str) -> str:
    value = _require_text(value, "member_type")
    if value.strip().lower() in API_REFUSED_MEMBER_TYPES:
        raise ValueError("member_type 'system' comes from configuration only; the API cannot set it")
    return value


class ProjectMemberUpsertRequest(BaseModel):
    project_id: str
    member_id: str
    display_name: str
    member_type: str = "human"
    role_keys: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_member_fields(self) -> "ProjectMemberUpsertRequest":
        self.member_id = _require_text(self.member_id, "member_id")
        self.display_name = _require_text(self.display_name, "display_name")
        self.member_type = _require_api_member_type(self.member_type)
        self.role_keys = _require_non_empty_string_list(
            self.role_keys, "role_keys"
        )
        self.permissions = _require_non_empty_string_list(
            self.permissions, "permissions"
        )
        self.actor = _require_text(self.actor, "actor")
        return self


class OrgMemberSummary(BaseModel):
    id: str
    org_id: str
    project_id: Optional[str] = None
    member_id: str
    display_name: str
    member_type: str
    role_keys: list[str]
    permissions: list[str]
    status: str
    created_by: str
    created_at: str
    updated_at: str


class OrgMemberListResponse(BaseModel):
    org_id: str
    items: list[OrgMemberSummary]


class OrgMemberUpsertRequest(BaseModel):
    org_id: str
    project_id: str
    member_id: str
    display_name: str
    member_type: str = "human"
    role_keys: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_org_member_fields(self) -> "OrgMemberUpsertRequest":
        self.org_id = _require_text(self.org_id, "org_id")
        self.project_id = _require_text(self.project_id, "project_id")
        self.member_id = _require_text(self.member_id, "member_id")
        self.display_name = _require_text(self.display_name, "display_name")
        self.member_type = _require_api_member_type(self.member_type)
        self.role_keys = _require_non_empty_string_list(
            self.role_keys, "role_keys"
        )
        self.permissions = _require_non_empty_string_list(
            self.permissions, "permissions"
        )
        self.actor = _require_text(self.actor, "actor")
        return self


class BreakGlassGrantSummary(BaseModel):
    id: str
    project_id: str
    target_member_id: str
    requested_by: str
    reason: str
    scope: dict[str, Any] = Field(default_factory=dict)
    granted_permissions: list[str]
    status: str
    expires_at: str
    postmortem_task_id: Optional[str] = None
    revoked_by: Optional[str] = None
    revoked_reason: Optional[str] = None
    revoked_at: Optional[str] = None
    created_at: str
    updated_at: str


class BreakGlassGrantListResponse(BaseModel):
    project_id: str
    items: list[BreakGlassGrantSummary]


class BreakGlassGrantCreateRequest(BaseModel):
    project_id: str
    target_member_id: str
    reason: str
    scope: dict[str, Any] = Field(default_factory=dict)
    granted_permissions: list[str]
    ttl_minutes: int = Field(default=60, ge=5, le=1440)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_grant(self) -> "BreakGlassGrantCreateRequest":
        self.target_member_id = _require_text(
            self.target_member_id, "target_member_id"
        )
        self.reason = _require_text(self.reason, "reason")
        self.granted_permissions = _require_non_empty_string_list(
            self.granted_permissions,
            "granted_permissions",
        )
        self.actor = _require_text(self.actor, "actor")
        return self


class BreakGlassGrantRevokeRequest(BaseModel):
    actor: str = "ceo"
    reason: str

    @model_validator(mode="after")
    def validate_revoke(self) -> "BreakGlassGrantRevokeRequest":
        self.actor = _require_text(self.actor, "actor")
        self.reason = _require_text(self.reason, "reason")
        return self


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
    deadletters: int = 0
    active_jobs: int = 0
    top_risks: list[dict[str, Any]] = Field(default_factory=list)
    latest_snapshot_id: Optional[str] = None
    latest_snapshot_at: Optional[str] = None
    blocked_reason: Optional[str] = None


class MetatronReference(BaseModel):
    object_type: str
    object_id: str
    title: str
    status: Optional[str] = None
    detail: Optional[str] = None
    created_at: Optional[str] = None


class MetatronQueryRequest(BaseModel):
    project_id: str
    question: str
    task_id: Optional[str] = None
    decision_id: Optional[str] = None
    limit: int = Field(default=5, ge=1, le=25)


class MetatronQueryResponse(BaseModel):
    project_id: str
    question: str
    category: str
    answer: str
    freshness_at: str
    references: list[MetatronReference] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    top_risks: list[dict[str, Any]] = Field(default_factory=list)


class HealthSnapshotSummary(BaseModel):
    id: str
    project_id: str
    generated_by: str
    budget_state: str
    queue_state: dict[str, Any] = Field(default_factory=dict)
    workload_state: dict[str, Any] = Field(default_factory=dict)
    incident_state: dict[str, Any] = Field(default_factory=dict)
    top_risks: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class HealthSnapshotListResponse(BaseModel):
    project_id: str
    items: list[HealthSnapshotSummary]


class HealthSnapshotRefreshRequest(BaseModel):
    project_id: str
    actor: str = "metatron"


class CommandProposeRequest(BaseModel):
    project_id: str
    command: str
    constraints: dict[str, Any] = Field(default_factory=dict)
    actor: str = "executive_console"


class CommandProposeResponse(BaseModel):
    status: str
    task_id: str
    job_id: str
    artifact_id: str
    plan_id: Optional[str] = None
    phases: list[dict[str, Any]] = Field(default_factory=list)


class AgentAction(BaseModel):
    kind: str
    task_id: Optional[str] = None
    run_id: Optional[str] = None
    meeting_id: Optional[str] = None
    plan_id: Optional[str] = None
    phase_count: Optional[int] = None
    phases: list[dict[str, Any]] = Field(default_factory=list)
    requires_confirmation: bool = False
    href: Optional[str] = None


class AgentPendingConfirmation(BaseModel):
    plan_id: str
    summary: str
    phase_count: int = 0
    phases: list[dict[str, Any]] = Field(default_factory=list)


class AgentMessageSummary(BaseModel):
    id: str
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str


#: Enough for a few screenshots or a long log; small enough that a turn stays a turn.
_MAX_ATTACHMENTS = 5
_MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024

#: What BOSS can actually read. An unknown type is refused by name rather than sent to the model
#: as base64 noise it will describe as "an unreadable file".
_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


class AgentAttachment(BaseModel):
    """One file on one message: a screenshot, or text too long to paste."""

    name: str = "file"
    media_type: str = "application/octet-stream"
    #: base64 for an image; the file's own text for anything textual.
    data: str = ""

    @model_validator(mode="after")
    def validate_attachment(self):
        self.name = (self.name or "file").strip()[:200]
        self.media_type = (self.media_type or "").strip().lower()
        if not self.data:
            raise ValueError(f"{self.name} is empty.")
        if self.media_type in _IMAGE_TYPES:
            return self
        # TEXT IS ANYTHING A PERSON WOULD READ. text/*, plus the application/* types that are
        # text in everything but their label — json, yaml, a .log served as octet-stream.
        if (self.media_type.startswith("text/")
                or self.media_type in {"application/json", "application/xml",
                                       "application/x-yaml", "application/yaml",
                                       "application/octet-stream", ""}):
            return self
        raise ValueError(
            f"{self.name}: BOSS cannot read {self.media_type}. Images "
            f"({', '.join(sorted(t.split('/')[1] for t in _IMAGE_TYPES))}) and text files.")

    @property
    def kind(self) -> str:
        return "image" if self.media_type in _IMAGE_TYPES else "text"


class AgentChatRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    thread_id: Optional[str] = None
    #: WHICH MEMBER OF THE WORKFORCE IS BEING ASKED.
    #:
    #: Agent Studio put "@QA Lead: " in the general agent's box, so the name was prose in a
    #: prompt and the answer came from BOSS-in-general — no lens, no remit, no memory of what
    #: that role had argued. A role key or title here makes the question addressed rather than
    #: merely annotated.
    role: Optional[str] = None
    message: str = ""
    confirm_plan_id: Optional[str] = None
    surface: Literal["chat", "github"] = "chat"
    #: WHAT THIS CONVERSATION IS FOR.
    #:
    #: "operate" is the agent that runs the factory — reads runs, fixes what is stuck, starts
    #: work on request. "discuss" is the one for thinking about an idea BEFORE any of that: it
    #: asks the questions that decide, disagrees when it has reason to, and starts nothing.
    #: The chat always existed; asked whether an idea was worth building it went and read
    #: deployments, because that is what its prompt is about.
    mode: Literal["operate", "discuss"] = "operate"
    external_ref: Optional[str] = None
    #: WHAT THE OPERATOR BROUGHT WITH THEM — a screenshot of the error, a log too long to paste.
    #:
    #: Describing a screen to an agent that cannot see it is the slowest possible way to report
    #: a UI fault, and pasting six thousand lines of log into a chat box ruins the transcript
    #: for every later turn. Both arrive here instead.
    attachments: list["AgentAttachment"] = Field(default_factory=list)
    #: WHAT THE DOCK WAS OPENED ABOUT (Agent World): {act_id} or {workflow_run_id, step_id}.
    #: The turn is signed as work under it.
    context: Optional[dict[str, Any]] = None
    #: HOW DEEP THIS TURN GOES. Unset, a question about a record is answered first in ONE
    #: tool-less call from that record, inside a hard budget (subject_answer.py — boss-1's
    #: 347 s "Ask the Qa"). "investigate" is the dock's "Investigate further" / "Do the fix":
    #: the full tool loop, on the operator's explicit say-so.
    depth: Optional[Literal["investigate"]] = None

    @model_validator(mode="after")
    def validate_turn(self):
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        self.message = self.message.strip()
        if not self.message and not self.confirm_plan_id and not self.attachments:
            raise ValueError("message or confirm_plan_id is required.")
        # A BOUND, NOT A HOPE. Every attachment is re-sent as part of the turn, so a handful of
        # large images is a request that fails slowly and expensively rather than one that is
        # refused immediately with a reason the operator can act on.
        if len(self.attachments) > _MAX_ATTACHMENTS:
            raise ValueError(
                f"at most {_MAX_ATTACHMENTS} files per message; "
                f"{len(self.attachments)} were sent.")
        total = sum(len(a.data) for a in self.attachments)
        if total > _MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"those files come to {total // 1024}KB; the limit is "
                f"{_MAX_ATTACHMENT_BYTES // 1024}KB per message. Send the relevant part.")
        return self


class ProposedAction(BaseModel):
    """An action BOSS is OFFERING, not one it took.

    The console renders each as a button. Nothing here has run: the operator's click is what
    executes it, which is the point — deploying an app or starting a build costs real money and
    is not always what "this link is wrong" means.
    """

    id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    #: NEVER REQUIRED, AND NEVER NONE. A tool that offered an action without naming it produced
    #: label=None, pydantic rejected the whole response, and the operator got "Internal Server
    #: Error" — losing the reply, the trace and every other action in the turn to a missing
    #: button caption. A nameless button is a cosmetic problem; dropping the answer is not.
    label: str = ""
    why: str = ""

    @field_validator("label", "why", mode="before")
    @classmethod
    def _never_none(cls, value: Any) -> str:
        return "" if value is None else str(value)


class AgentActionRunRequest(BaseModel):
    """Execute one action the operator accepted by clicking its button."""

    project_id: str
    actor: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    thread_id: Optional[str] = None
    #: Which offer was pressed, so it can be struck from the message that made it. Optional
    #: because an older console does not send it; without it the outcome is still recorded and
    #: every offer of the same tool is spent, which is the safe direction to be wrong in.
    action_id: Optional[str] = None
    #: What the button said, so the transcript reads as the operator's action rather than as a
    #: tool name they never saw.
    label: Optional[str] = None


class AgentActionRunResponse(BaseModel):
    ok: bool
    tool: str
    result: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class AgentChatResponse(BaseModel):
    thread_id: str
    reply: str
    actions: list[AgentAction] = Field(default_factory=list)
    pending_confirmation: Optional[AgentPendingConfirmation] = None
    #: Offers awaiting a click. Distinct from `actions`, which already happened.
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    messages: list[AgentMessageSummary] = Field(default_factory=list)
    #: WHAT BOSS READ TO ANSWER. Every tool call the turn made, in order.
    #:
    #: `actions` carries only things that CHANGED something, so a turn that investigated a bug
    #: across a dozen files reported an empty list and the operator saw a confident paragraph
    #: with nothing behind it. An answer that names its sources can be checked; one that does
    #: not has to be taken on trust, and BOSS has not earned that.
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    #: WHAT THE DOCK OFFERS NEXT under an answer given from a record in one call —
    #: {id, label, depth, message} for "Investigate further" and "Do the fix". Pressing one sends
    #: `message` with `depth`; nothing runs until then.
    next_steps: list[dict[str, Any]] = Field(default_factory=list)
    #: THE SIGNED TURN (agent_acts) when an agent was asked, and the acts of its own work the
    #: reply cited as [act:…]. Absent for BOSS-in-general, which is not an agent.
    act_id: Optional[str] = None
    cited_acts: list[str] = Field(default_factory=list)


class MeetingProposeRequest(BaseModel):
    project_id: str
    directive: str
    url: str = ""
    roles: list[str] = Field(default_factory=list)
    task_id: Optional[str] = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    actor: str = "executive_console"


class MeetingProposeResponse(BaseModel):
    status: str
    task_id: str
    #: None when this convene found the same panel already in session and returned it.
    job_id: Optional[str] = None
    #: The panel this convene started — or the one already in session on the same ask.
    run_id: Optional[str] = None
    #: The convened panel (mrun_…), so the console can land on it: ?area=meetings&meeting=<id>.
    meeting_run_id: Optional[str] = None
    #: The app build (crun_…) the panel is specifying a feature for, when it is one.
    extends_run_id: Optional[str] = None


class BudgetReserveRequest(BaseModel):
    project_id: str
    category: str
    amount_usd_micros: int = Field(gt=0)
    reason: str
    job_id: Optional[str] = None
    allocation_id: Optional[str] = None
    expires_in_minutes: int = Field(default=30, ge=5, le=240)


class BudgetReserveResponse(BaseModel):
    status: str
    reservation_id: Optional[str] = None
    decision_id: Optional[str] = None
    budget_state: str
    message: str


class BudgetQuoteRequest(BaseModel):
    project_id: str
    category: str
    amount_usd_micros: int = Field(gt=0)
    reason: str


class BudgetQuoteResponse(BaseModel):
    allowed: bool
    category: str
    reason: str
    requested_usd_micros: int
    budget_state: str
    post_reservation_budget_state: str
    cap_usd_micros: int
    available_usd_micros: int
    post_reservation_available_usd_micros: int


class BudgetReleaseRequest(BaseModel):
    project_id: str
    reservation_id: str
    reason: str


class BudgetReleaseResponse(BaseModel):
    status: str
    reservation_id: str
    budget_state: str
    message: str


class BudgetConsumeRequest(BaseModel):
    project_id: str
    reservation_id: str
    receipt_id: str
    category: str
    skill_or_model_id: str
    estimated_usd_micros: int = Field(ge=0)
    actual_usd_micros: Optional[int] = Field(default=None, ge=0)
    actor: str


class BudgetConsumeResponse(BaseModel):
    status: str
    reservation_id: str
    budget_state: str
    message: str
    cost_receipt: CostReceiptSummary


class BudgetSetIntensityRequest(BaseModel):
    scope_id: str
    intensity: int
    scope_type: str = "project"
    actor: str = "ceo"


class CanaryRunRequest(BaseModel):
    project_id: str
    actor: str = "canary"


class PlanDispatchRequest(BaseModel):
    project_id: str
    actor: str = "program_manager"


class AnomalyScanRequest(BaseModel):
    project_id: str
    auto_lite: bool = True


class EvalRunRequest(BaseModel):
    project_id: str
    actor: str = "eval_harness"
    suite: str = "all"


class ProviderHealthPollRequest(BaseModel):
    project_id: str
    actor: str = "router_health"


class RouterRationaleRequest(BaseModel):
    project_id: str
    actor: str = "router_health"
    skill_id: str = "model.generate_text"
    task_kind: str = "code"
    workload_class: str = "owner_sandbox"
    risk_tier: int = Field(default=1, ge=0, le=3)


class SkillInvokeRequest(BaseModel):
    project_id: str
    caller: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    mock: bool = True
    actor: Optional[str] = None
    created_by_job_id: Optional[str] = None
    worker_id: Optional[str] = None
    lease_token: Optional[str] = None

    @model_validator(mode="after")
    def validate_fields(self) -> "SkillInvokeRequest":
        self.caller = _require_text(self.caller, "caller")
        return self


class SlackRenderRequest(BaseModel):
    persona: str
    role: Optional[str] = None
    subject: Optional[str] = None
    body: str
    department: Optional[str] = None
    signature: bool = True

    @model_validator(mode="after")
    def validate_fields(self) -> "SlackRenderRequest":
        self.persona = _require_text(self.persona, "persona")
        self.body = _require_text(self.body, "body")
        return self


class SlackDigestBatchRequest(BaseModel):
    project_id: str
    actor: str = "system"
    channel: Optional[str] = None
    min_batch: int = Field(default=2, ge=1)


class FactoryMergeCheckRequest(BaseModel):
    checks: dict[str, Any]
    min_confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class FactoryQualityGateRequest(BaseModel):
    project_id: str
    checks: dict[str, Any]
    actor: str = "system"
    code_task_run_id: Optional[str] = None
    task_id: Optional[str] = None
    min_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    risk_tier: int = Field(default=2, ge=0, le=3)
    #: Required to replace a run's recorded verdict (identity int-b: never silently).
    override_reason: Optional[str] = Field(default=None, max_length=500)


class ChaosRunRequest(BaseModel):
    project_id: str
    scenario: str
    actor: str = "system"


class CommitmentReconcileRequest(BaseModel):
    project_id: str
    actor: str = "system"


class FinopsSpendByProvider(BaseModel):
    provider: str
    cost_usd_micros: int
    event_count: int


class FinopsSpendByDay(BaseModel):
    day: str
    cost_usd_micros: int


class FinopsCommitmentUtilization(BaseModel):
    commitment_id: str
    provider: str
    utilization: float
    verdict: str


class FinopsTopResource(BaseModel):
    resource_ref: str
    provider: str
    cost_usd_micros: int


class FinopsSummaryResponse(BaseModel):
    """BOSS-244 multi-dimension FinOps chart/summary payload for Mission Control."""

    project_id: str
    days: int
    period_start: str
    period_end: str
    spend_by_provider: list[FinopsSpendByProvider]
    spend_by_day: list[FinopsSpendByDay]
    anomalies_open_count: int
    commitment_utilization: list[FinopsCommitmentUtilization]
    top_resources: list[FinopsTopResource]


class SOC2EvidenceRequest(BaseModel):
    project_id: str
    actor: str = "system"


class InitiativeBudgetRequest(BaseModel):
    project_id: str
    title: str
    tier: int = Field(default=2, ge=0, le=3)
    amount_usd_micros: int = Field(ge=0)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_fields(self) -> "InitiativeBudgetRequest":
        self.title = _require_text(self.title, "title")
        return self


class FactoryChunkPlanRequest(BaseModel):
    spec: dict[str, Any]
    max_files_per_chunk: int = Field(default=3, ge=1)
    max_chunks: int = Field(default=20, ge=1)

    @model_validator(mode="after")
    def validate_spec(self) -> "FactoryChunkPlanRequest":
        if not self.spec:
            raise ValueError("spec must be a non-empty object.")
        return self


class LegacyFinancialHold(BaseModel):
    reservation_id: str
    lease_id: str
    invocation_id: str
    receipt_id: str
    original_reservation_status: Literal["expired", "released"]
    original_estimate_usd_micros: int = Field(gt=0)
    decision_id: Optional[str] = None
    message: str


class LegacyProviderHold(BaseModel):
    reservation_id: str
    changeset_id: str
    operation_id: str
    resource_receipt_id: str
    receipt_id: str
    original_reservation_status: Literal["expired", "released"]
    original_estimate_usd_micros: int = Field(gt=0)
    decision_id: Optional[str] = None
    message: str


class BudgetStatusResponse(BaseModel):
    scope_type: str
    scope_id: str
    budget_state: str
    cap_usd_micros: int
    reserved_usd_micros: int
    actual_usd_micros: int
    available_usd_micros: int
    committed_usd_micros: int
    burn_approx_usd_micros: int
    burn_window_hours: int
    burn_rate_usd_micros_per_hour: int
    forecasted_24h_usd_micros: int
    time_to_cap_seconds: Optional[int] = None
    utilization_ratio: float
    active_reservations_count: int
    top_cost_drivers: list[dict[str, Any]]
    # Subscription / local-model calls at API list price: not money, in no cap (included_spend).
    included_in_subscription_usd_micros: int = 0
    included_cost_drivers: list[dict[str, Any]] = []
    operational_intensity: Optional[int] = None
    # BOSS-280: the enforced thresholds, surfaced so the console shows REAL knobs
    soft_cap_pct: Optional[float] = None
    auto_lite_mode_pct: Optional[float] = None
    freeze_provisioning_pct: Optional[float] = None
    freeze_tier2_pct: Optional[float] = None
    tier2_frozen: Optional[bool] = None
    # Every cap measures its own period (budget.period_spend). The numbers above are the
    # binding period's — the one nearest its cap — and `periods` carries each ("day", "month").
    period: Optional[str] = None
    cap_name: Optional[str] = None
    period_start: Optional[str] = None
    resets_at: Optional[str] = None
    resets_at_label: Optional[str] = None
    budget_timezone: Optional[str] = None
    periods: dict[str, dict[str, Any]] = Field(default_factory=dict)
    legacy_financial_hold: Optional[LegacyFinancialHold] = None
    legacy_provider_hold: Optional[LegacyProviderHold] = None


class BudgetForecastResponse(BaseModel):
    scope_type: str
    scope_id: str
    horizon_hours: int
    budget_state: str
    cap_usd_micros: int
    reserved_usd_micros: int
    actual_usd_micros: int
    available_usd_micros: int
    committed_usd_micros: int
    burn_rate_usd_micros_per_hour: int
    forecasted_usd_micros: int
    time_to_cap_seconds: Optional[int] = None
    top_cost_drivers: list[dict[str, Any]]


class BudgetAllocationSummary(BaseModel):
    id: str
    parent_allocation_id: Optional[str] = None
    project_id: str
    scope_type: str
    scope_id: str
    amount_usd_micros: int
    child_allocated_usd_micros: int
    reserved_usd_micros: int
    actual_usd_micros: int
    available_usd_micros: int
    period_start: str
    period_end: str
    status: str
    created_at: str


class BudgetAllocationListResponse(BaseModel):
    project_id: str
    items: list[BudgetAllocationSummary]


class BudgetAllocationCreateRequest(BaseModel):
    project_id: str
    scope_type: str
    scope_id: str
    amount_usd_micros: int = Field(gt=0)
    period_start: datetime
    period_end: datetime
    parent_allocation_id: Optional[str] = None
    actor: str = "ceo"


class McpServerRegisterRequest(BaseModel):
    project_id: str
    name: str
    url: str
    transport: str = "http"
    risk_tier: int = Field(default=1, ge=0, le=3)
    data_boundary: DataBoundary = "internal"
    description: Optional[str] = None
    publisher: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    tool_risk_overrides: dict[str, int] = Field(default_factory=dict)
    #: How BOSS signs in: {type: none|bearer|header, header_name?, secret?}. `secret` is
    #: write-only — written through to the secret store, never stored here or read back.
    auth: Optional[dict[str, Any]] = None
    actor: str = "system"


class McpServerUpdateRequest(BaseModel):
    """An edit to a registered MCP server. Only the fields sent are changed; the id stays, so
    every custom step and authored skill bound to the server keeps working."""

    name: Optional[str] = None
    url: Optional[str] = None
    transport: Optional[str] = None
    description: Optional[str] = None
    risk_tier: Optional[int] = Field(default=None, ge=0, le=3)
    data_boundary: Optional[DataBoundary] = None
    tool_risk_overrides: Optional[dict[str, int]] = None
    enabled: Optional[bool] = None
    #: A person's review. Recorded with who marked it (provenance.reviewed_by).
    trusted: Optional[bool] = None
    #: A new sign-in; a form that leaves `secret` out keeps the token already stored.
    auth: Optional[dict[str, Any]] = None


class CapabilityBundleImportRequest(BaseModel):
    project_id: str
    bundle: str
    allow_untrusted: bool = False
    enabled: bool = True
    on_demand: Optional[bool] = None
    actor: str = "system"


class SkillToggleRequest(BaseModel):
    project_id: str
    on_demand: Optional[bool] = None
    actor: str = "system"


class EventSubscriptionCreateRequest(BaseModel):
    project_id: str
    event_type: str
    sink_kind: Literal["job", "webhook_out", "workflow"] = "job"
    sink_ref: str
    data_boundary: DataBoundary = "internal"
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"


class EventSubscriptionUpdateRequest(BaseModel):
    project_id: str
    event_type: Optional[str] = None
    sink_kind: Optional[Literal["job", "webhook_out", "workflow"]] = None
    sink_ref: Optional[str] = None
    data_boundary: Optional[DataBoundary] = None
    enabled: Optional[bool] = None
    config: Optional[dict[str, Any]] = None
    actor: str = "system"


class EventSubscriptionSummary(BaseModel):
    id: str
    project_id: str
    event_type: str
    sink_kind: Literal["job", "webhook_out", "workflow"]
    sink_ref: str
    data_boundary: DataBoundary
    enabled: bool
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    #: For a `workflow` sink: what that workflow needs from this project, which missing items
    #: `config.start_anyway` accepts, and which the event door will refuse (`will_refuse`).
    #: The sink kind was accepted by the service but not by these schemas, so a workflow
    #: subscription could not be made or read over HTTP at all.
    readiness: Optional[dict[str, Any]] = None


class EventSubscriptionListResponse(BaseModel):
    project_id: str
    items: list[EventSubscriptionSummary]


class WebhookEndpointCreateRequest(BaseModel):
    project_id: str
    url: str
    secret_ref: str
    description: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    enabled: bool = True
    actor: str = "system"


class WebhookEndpointUpdateRequest(BaseModel):
    project_id: str
    url: Optional[str] = None
    secret_ref: Optional[str] = None
    description: Optional[str] = None
    data_boundary: Optional[DataBoundary] = None
    enabled: Optional[bool] = None
    actor: str = "system"


class WebhookEndpointSummary(BaseModel):
    id: str
    project_id: str
    url: str
    secret_ref: str
    description: Optional[str] = None
    data_boundary: DataBoundary
    enabled: bool
    created_at: str
    updated_at: str


class WebhookEndpointListResponse(BaseModel):
    project_id: str
    items: list[WebhookEndpointSummary]


class WebhookDeliveryCreateRequest(BaseModel):
    project_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str
    data_boundary: DataBoundary = "internal"
    priority: int = 100
    max_attempts: int = Field(default=3, ge=1, le=20)
    actor: str = "system"


class WebhookDeliveryExecuteRequest(BaseModel):
    project_id: str
    created_by_job_id: str
    worker_id: str
    lease_token: str


class WebhookDeliverySummary(BaseModel):
    id: str
    project_id: str
    endpoint_id: str
    event_id: Optional[str] = None
    event_type: str
    status: str
    dedupe_key: str
    request_body: dict[str, Any]
    response: Optional[dict[str, Any]] = None
    last_error: Optional[dict[str, Any]] = None
    receipt_id: Optional[str] = None
    attempt_count: int
    data_boundary: DataBoundary
    next_attempt_at: Optional[str] = None
    delivered_at: Optional[str] = None
    created_at: str
    updated_at: str
    job_id: Optional[str] = None
    created: Optional[bool] = None
    message: Optional[str] = None


class WebhookDeliveryListResponse(BaseModel):
    project_id: str
    items: list[WebhookDeliverySummary]


class InboundWebhookResponse(BaseModel):
    status: str
    source: str
    delivery_id: Optional[str] = None
    inbound_webhook_id: Optional[str] = None
    event_id: Optional[str] = None
    project_id: Optional[str] = None
    duplicate: bool = False


class GitHubCommunityJobExecuteRequest(BaseModel):
    project_id: str
    worker_id: str
    lease_token: str


class OpenAIPlanRequest(BaseModel):
    project_id: str
    reservation_id: str
    dedupe_key: str
    prompt: str
    system_prompt: Optional[str] = None
    agent_module_id: Optional[str] = None
    created_by_job_id: Optional[str] = None
    task_id: Optional[str] = None


class OpenAIPlanResponse(BaseModel):
    status: str
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    output_text: Optional[str] = None
    decision_id: Optional[str] = None
    message: Optional[str] = None


class FileChange(BaseModel):
    path: str
    content: str


class PullRequestProposalRequest(BaseModel):
    project_id: str
    task_id: Optional[str] = None
    reservation_id: str
    dedupe_key: str
    title: str
    branch_name: str
    summary: str
    files: list[FileChange]
    agent_module_id: Optional[str] = None
    created_by_job_id: Optional[str] = None


class PullRequestProposalJobRequest(PullRequestProposalRequest):
    task_id: Optional[str] = None
    priority: int = Field(default=50, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)


class PullRequestProposalResponse(BaseModel):
    status: str
    data_boundary: Optional[DataBoundary] = None
    artifact_id: Optional[str] = None
    diff_artifact_id: Optional[str] = None
    test_report_artifact_id: Optional[str] = None
    security_review_artifact_id: Optional[str] = None
    release_go_no_go_artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    pull_request_url: Optional[str] = None
    pull_request_number: Optional[int] = None
    branch_name: Optional[str] = None
    validation_status: Optional[str] = None
    validation_summary: Optional[str] = None
    validation_sources: list[str] = Field(default_factory=list)
    local_validation_receipt_id: Optional[str] = None
    message: Optional[str] = None
    # Honesty field for dual GitHub planes (MC binding vs legacy env PAT/App).
    github_plane: Optional[str] = None
    github_binding_id: Optional[str] = None
    github_connection_id: Optional[str] = None


class CIConfigProposalRequest(BaseModel):
    project_id: str
    repo: str
    language: str
    framework: Optional[str] = None
    target: str = "github_actions"
    reservation_id: str
    dedupe_key: str
    branch_name: str
    title: str = "ci: add governed build and deployment evidence pipeline"
    task_id: Optional[str] = None
    actor: str = "release_manager"
    created_by_job_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_ci_config_proposal(self) -> "CIConfigProposalRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.repo = _require_text(self.repo, "repo")
        self.language = _require_text(self.language, "language")
        self.target = _require_text(self.target, "target")
        self.reservation_id = _require_text(self.reservation_id, "reservation_id")
        self.dedupe_key = _require_text(self.dedupe_key, "dedupe_key")
        self.branch_name = _require_text(self.branch_name, "branch_name")
        self.title = _require_text(self.title, "title")
        self.actor = _require_text(self.actor, "actor")
        return self


class CIConfigProposalResponse(BaseModel):
    status: str
    pipeline_descriptor: dict[str, Any]
    generated_config: Optional[dict[str, Any]] = None
    proposal: Optional[PullRequestProposalResponse] = None


class PullRequestMergeRequest(BaseModel):
    project_id: str
    decision_id: str
    dedupe_key: str
    pull_request_number: int = Field(gt=0)
    pull_request_url: str
    title: str
    branch_name: Optional[str] = None
    created_by_job_id: Optional[str] = None


class PullRequestMergeJobRequest(PullRequestMergeRequest):
    task_id: Optional[str] = None
    priority: int = Field(default=30, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)


class SandboxDeploymentRequest(BaseModel):
    project_id: str
    decision_id: str
    dedupe_key: str
    merged_sha: str
    pull_request_url: str
    title: str
    environment: str
    branch_name: Optional[str] = None
    created_by_job_id: Optional[str] = None


class SandboxDeploymentJobRequest(SandboxDeploymentRequest):
    task_id: Optional[str] = None
    priority: int = Field(default=20, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)


class PullRequestMergeResponse(BaseModel):
    status: str
    data_boundary: Optional[DataBoundary] = None
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    deployment_decision_id: Optional[str] = None
    pull_request_url: Optional[str] = None
    branch_name: Optional[str] = None
    merged_sha: Optional[str] = None
    message: Optional[str] = None


class SandboxDeploymentResponse(BaseModel):
    status: str
    data_boundary: Optional[DataBoundary] = None
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    health_decision_id: Optional[str] = None
    health_verification_artifact_id: Optional[str] = None
    health_verification_status: Optional[str] = None
    healthcheck_url: Optional[str] = None
    rollout_id: Optional[str] = None
    rollout_artifact_id: Optional[str] = None
    rollout_status: Optional[str] = None
    rollout_stage: Optional[str] = None
    environment: Optional[str] = None
    deployment_id: Optional[int] = None
    deployment_status: Optional[str] = None
    deployment_url: Optional[str] = None
    merged_sha: Optional[str] = None
    message: Optional[str] = None


class InfraProvisionReviewRequest(BaseModel):
    project_id: str
    task_id: Optional[str] = None
    reservation_id: str
    title: str
    region: str
    size: str
    ttl_minutes: int = Field(ge=5, le=10_080)
    tags: list[str] = Field(default_factory=list)
    data_boundary: Optional[DataBoundary] = None
    actor: str = "ceo"


class InfraProvisionReviewResponse(BaseModel):
    status: str
    artifact_id: str
    decision_id: str
    data_boundary: DataBoundary = "internal"


class InfraProvisionRequest(BaseModel):
    project_id: str
    decision_id: str
    reservation_id: str
    dedupe_key: str
    title: str
    region: str
    size: str
    ttl_minutes: int = Field(ge=5, le=10_080)
    tags: list[str] = Field(default_factory=list)
    task_id: Optional[str] = None
    data_boundary: Optional[DataBoundary] = None
    created_by_job_id: Optional[str] = None


class InfraProvisionJobRequest(InfraProvisionRequest):
    actor: str = "release_manager"
    priority: int = Field(default=15, ge=0)
    max_attempts: int = Field(default=1, ge=1, le=20)


class InfraProvisionResponse(BaseModel):
    status: str
    resource_id: Optional[str] = None
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    provider_ref: Optional[str] = None
    provider_resource_id: Optional[str] = None
    resource_name: Optional[str] = None
    expires_at: Optional[str] = None
    data_boundary: Optional[DataBoundary] = None
    message: Optional[str] = None


class InfraTerminationRequest(BaseModel):
    project_id: str
    dedupe_key: str
    actor: str = "release_manager"
    reason: Optional[str] = None
    created_by_job_id: Optional[str] = None


class InfraTerminationJobRequest(InfraTerminationRequest):
    priority: int = Field(default=10, ge=0)
    max_attempts: int = Field(default=1, ge=1, le=20)


class InfraTerminationResponse(BaseModel):
    status: str
    resource_id: Optional[str] = None
    artifact_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    provider_ref: Optional[str] = None
    message: Optional[str] = None


class InfraJanitorRunRequest(BaseModel):
    project_id: str
    actor: str = "system"
    limit: int = Field(default=20, ge=1, le=200)


class InfraJanitorRunResponse(BaseModel):
    status: str
    project_id: str
    queued_job_ids: list[str] = Field(default_factory=list)
    resource_ids: list[str] = Field(default_factory=list)


class InfraResourceSummary(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    decision_id: Optional[str] = None
    provider: str
    resource_type: str
    resource_name: str
    external_resource_id: str
    region: str
    size: str
    image: str
    tags: list[str] = Field(default_factory=list)
    status: str
    ttl_minutes: int
    expires_at: str
    terminated_at: Optional[str] = None
    termination_reason: Optional[str] = None
    provision_receipt_id: Optional[str] = None
    provision_artifact_id: Optional[str] = None
    termination_receipt_id: Optional[str] = None
    termination_artifact_id: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by_job_id: Optional[str] = None
    updated_by_job_id: Optional[str] = None
    created_at: str
    updated_at: str


class InfraResourceListResponse(BaseModel):
    project_id: str
    items: list[InfraResourceSummary]


class DeploymentRolloutSummary(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    decision_id: Optional[str] = None
    deployment_receipt_id: Optional[str] = None
    deployment_artifact_id: Optional[str] = None
    rollout_artifact_id: Optional[str] = None
    title: str
    environment: str
    merged_sha: str
    pull_request_url: str
    branch_name: Optional[str] = None
    status: str
    current_stage: str
    health_verification_status: Optional[str] = None
    rollback_decision_id: Optional[str] = None
    stages: list[dict[str, Any]]
    created_by_job_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    deployment_run_id: Optional[str] = None
    promotion_id: Optional[str] = None
    rollback_reason: Optional[str] = None
    health_observations: list[dict[str, Any]] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class DeploymentRolloutListResponse(BaseModel):
    project_id: str
    items: list[DeploymentRolloutSummary]


class DeploymentRolloutStartRequest(BaseModel):
    actor: str = "deployment_manager"
    stages: list[str] = Field(default_factory=list)
    thresholds: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_deployment_rollout_start(self) -> "DeploymentRolloutStartRequest":
        self.actor = _require_text(self.actor, "actor")
        return self


class DeploymentRolloutAdvanceRequest(BaseModel):
    actor: str = "deployment_manager"
    observation: dict[str, Any]

    @model_validator(mode="after")
    def validate_deployment_rollout_advance(self) -> "DeploymentRolloutAdvanceRequest":
        self.actor = _require_text(self.actor, "actor")
        if not self.observation:
            raise ValueError("observation is required")
        return self


class DeploymentRolloutRollbackRequest(BaseModel):
    actor: str = "deployment_manager"
    reason: str

    @model_validator(mode="after")
    def validate_deployment_rollout_rollback(self) -> "DeploymentRolloutRollbackRequest":
        self.actor = _require_text(self.actor, "actor")
        self.reason = _require_text(self.reason, "reason")
        return self


class ReceiptResponse(BaseModel):
    id: str
    project_id: str
    dedupe_key: str
    effect_kind: str
    status: str
    effect_scope: dict[str, Any] = Field(default_factory=dict)
    payload_hash: Optional[str] = None
    provider_ref: Optional[str] = None
    created_by_job_id: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    created_at: str
    updated_at: str


class ReceiptListResponse(BaseModel):
    project_id: str
    items: list[ReceiptResponse]
    limit: int
    offset: int


class JobSummary(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    work_item_id: Optional[str] = None
    tracker_binding_id: Optional[str] = None
    allocation_id: Optional[str] = None
    kind: str
    status: str
    data_boundary: DataBoundary = "internal"
    priority: int
    priority_score: Optional[float] = None
    workload_class: str = "general"
    resource_units: int = 1
    max_attempts: int
    attempt: int
    run_after: str


class JobListItem(JobSummary):
    task_title: Optional[str] = None
    work_title: Optional[str] = None
    tracker_provider: Optional[str] = None
    external_key: Optional[str] = None
    external_url: Optional[str] = None
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    last_error: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class JobListResponse(BaseModel):
    project_id: str
    items: list[JobListItem]
    limit: int
    offset: int


class JobCompletionContract(BaseModel):
    """Proof required before a worker may mark an opt-in job successful."""

    required_result_fields: list[str] = Field(default_factory=list)
    min_evidence_refs: int = Field(default=1, ge=1, le=100)
    independent_verifier_required: bool = True

    @model_validator(mode="after")
    def validate_fields(self) -> "JobCompletionContract":
        if self.required_result_fields:
            self.required_result_fields = _require_non_empty_string_list(
                self.required_result_fields, "required_result_fields"
            )
        return self


class JobCreateRequest(BaseModel):
    project_id: str
    kind: str
    task_id: Optional[str] = None
    work_item_id: Optional[str] = None
    tracker_binding_id: Optional[str] = None
    allocation_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    completion_contract: Optional[JobCompletionContract] = None
    data_boundary: Optional[DataBoundary] = None
    requires_reservation: bool = False
    reservation_id: Optional[str] = None
    priority: int = Field(default=100, ge=0)
    priority_score: Optional[float] = Field(default=None, ge=0, le=100)
    workload_class: str = "general"
    resource_units: int = Field(default=1, ge=1, le=1000)
    max_attempts: int = Field(default=3, ge=1, le=20)
    run_after_seconds: int = Field(default=0, ge=0, le=86_400)
    dedupe_key: Optional[str] = None


class DocsUpdateRequest(BaseModel):
    project_id: str
    event_id: str
    event_type: str
    entity_type: Literal["code_task_run", "discovery_run", "plan", "artifact"]
    entity_id: str
    task_id: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    actor: str = "manual"


class DocsUpdateExecuteRequest(DocsUpdateRequest):
    created_by_job_id: Optional[str] = None


class OrchestratorNextMoveExecuteRequest(BaseModel):
    """Worker-only execution boundary for agent.orchestrator.next_move jobs."""

    project_id: str
    event_id: Optional[str] = None
    event_type: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    event_data: dict[str, Any] = Field(default_factory=dict)
    task_id: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    created_by_job_id: Optional[str] = None
    worker_id: Optional[str] = None
    lease_token: Optional[str] = None


class OpsQueueJobItem(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    task_title: Optional[str] = None
    kind: str
    status: str
    priority: int
    attempt: int
    max_attempts: int
    data_boundary: DataBoundary = "internal"
    run_after: str
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    last_error: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class OpsQueueDecisionItem(BaseModel):
    id: str
    task_id: Optional[str] = None
    question: str
    status: str
    risk_tier: int
    default_action: Optional[str] = None
    deadline_at: Optional[str] = None
    created_at: str
    updated_at: str


class OpsQueueInspectionResponse(BaseModel):
    project_id: str
    generated_at: str
    budget_state: str
    queue_state: dict[str, Any] = Field(default_factory=dict)
    workload_state: dict[str, Any] = Field(default_factory=dict)
    incident_state: dict[str, Any] = Field(default_factory=dict)
    job_status_counts: dict[str, int] = Field(default_factory=dict)
    job_kind_counts: dict[str, int] = Field(default_factory=dict)
    stale_lease_job_ids: list[str] = Field(default_factory=list)
    ready_job_ids: list[str] = Field(default_factory=list)
    jobs: list[OpsQueueJobItem] = Field(default_factory=list)
    deadletters: list["DeadletterItem"] = Field(default_factory=list)
    pending_decisions: list[OpsQueueDecisionItem] = Field(default_factory=list)
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)


class OpsDigestRequest(BaseModel):
    project_id: str
    actor: str = "coo"
    limit: int = Field(default=25, ge=1, le=100)
    include_terminal: bool = False


class OpsDigestResponse(BaseModel):
    status: str
    project_id: str
    artifact_id: str
    artifact_type: str
    created_at: str
    counts: dict[str, int] = Field(default_factory=dict)
    queue_state: dict[str, Any] = Field(default_factory=dict)
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)


class LeasedJob(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    work_item_id: Optional[str] = None
    tracker_binding_id: Optional[str] = None
    allocation_id: Optional[str] = None
    kind: str
    payload: dict[str, Any]
    data_boundary: DataBoundary = "internal"
    priority_score: Optional[float] = None
    workload_class: str = "general"
    resource_units: int = 1
    attempt: int
    max_attempts: int
    lease_token: str
    lease_expires_at: str


class WorkerPollRequest(BaseModel):
    worker_id: str
    capabilities: list[str]
    max_jobs: int = Field(default=1, ge=1, le=1)
    lease_ttl_seconds: int = Field(default=60, ge=5, le=3600)


class WorkerRegistrationRequest(BaseModel):
    project_id: str
    worker_id: str
    capabilities: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "chief_operator"


class WorkerPollResponse(BaseModel):
    status: str
    job: Optional[LeasedJob] = None


class WorkerHeartbeatRequest(BaseModel):
    worker_id: str
    job_id: str
    lease_token: str
    lease_ttl_seconds: int = Field(default=60, ge=5, le=3600)
    # A worker that is stopping (deploy, restart) hands its job back: the reclaim that follows
    # must not cost the job an attempt.
    handing_back: bool = False


class WorkerHeartbeatResponse(BaseModel):
    job_id: str
    status: str
    lease_expires_at: str
    # For a code.task: is the run it carries alive and making progress (run_liveness)? The
    # worker's runtime ceiling never lets the lease of progressing work lapse.
    progressing: Optional[bool] = None


class WorkerCompleteRequest(BaseModel):
    worker_id: str
    job_id: str
    lease_token: str
    result: dict[str, Any] = Field(default_factory=dict)


class WorkerCompleteResponse(BaseModel):
    job_id: str
    status: str
    proof_disposition: Optional[Literal["accepted", "rejected", "not_required"]] = None
    proof_artifact_id: Optional[str] = None
    proof_rejection_reasons: list[str] = Field(default_factory=list)
    artifact_id: Optional[str] = None
    diff_artifact_id: Optional[str] = None
    test_report_artifact_id: Optional[str] = None
    security_review_artifact_id: Optional[str] = None
    release_go_no_go_artifact_id: Optional[str] = None
    decision_id: Optional[str] = None
    receipt_id: Optional[str] = None
    deployment_decision_id: Optional[str] = None
    pull_request_url: Optional[str] = None
    merged_sha: Optional[str] = None
    branch_name: Optional[str] = None
    validation_status: Optional[str] = None
    validation_summary: Optional[str] = None
    validation_sources: list[str] = Field(default_factory=list)
    local_validation_receipt_id: Optional[str] = None
    environment: Optional[str] = None
    deployment_id: Optional[int] = None
    deployment_status: Optional[str] = None
    deployment_url: Optional[str] = None
    rollout_id: Optional[str] = None
    rollout_artifact_id: Optional[str] = None
    rollout_status: Optional[str] = None
    rollout_stage: Optional[str] = None
    resource_id: Optional[str] = None
    provider_ref: Optional[str] = None
    provider_resource_id: Optional[str] = None
    resource_name: Optional[str] = None
    expires_at: Optional[str] = None


class WorkerFailRequest(BaseModel):
    worker_id: str
    job_id: str
    lease_token: str
    error: dict[str, Any]
    retryable: bool = True
    retry_delay_seconds: int = Field(default=30, ge=0, le=3600)


class WorkerFailResponse(BaseModel):
    job_id: str
    status: str
    run_after: Optional[str] = None
    deadletter_id: Optional[str] = None


class TaskSummary(BaseModel):
    id: str
    project_id: str
    title: str
    description: Optional[str] = None
    status: str
    # BOSS-281: activity-derived lifecycle (from the task's latest job + pending decisions) —
    # the tasks table itself only ever holds open/archived, which made every board column
    # except Backlog permanently empty.
    derived_status: Optional[str] = None
    owner_role_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    dependency_task_ids: list[str] = Field(default_factory=list)
    due_at: Optional[str] = None
    priority: str
    risk_tier: int
    data_boundary: DataBoundary = "internal"
    dedupe_key: str
    created_at: str
    updated_at: str


class TaskListResponse(BaseModel):
    project_id: str
    items: list[TaskSummary]
    limit: int
    offset: int


class TaskCreateRequest(BaseModel):
    project_id: str
    title: str
    description: Optional[str] = None
    owner_role_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    dependency_task_ids: list[str] = Field(default_factory=list)
    due_at: Optional[datetime] = None
    priority: str = "p2"
    risk_tier: int = Field(default=1, ge=0, le=3)
    data_boundary: DataBoundary = "internal"
    dedupe_key: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    required_artifacts: list[str] = Field(default_factory=list)
    actor: str = "executive_console"


class DepartmentSummary(BaseModel):
    id: str
    project_id: str
    department_key: str
    name: str
    mission: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


class DepartmentCreateRequest(BaseModel):
    project_id: str
    department_key: str
    name: str
    mission: str
    actor: str = "ceo"


class DepartmentListResponse(BaseModel):
    project_id: str
    items: list[DepartmentSummary]
    limit: int
    offset: int


class KpiSummary(BaseModel):
    id: str
    project_id: str
    kpi_key: str
    name: str
    description: Optional[str] = None
    owner_role_id: Optional[str] = None
    target_value: float
    current_value: float
    unit: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


class KpiCreateRequest(BaseModel):
    project_id: str
    kpi_key: str
    name: str
    description: Optional[str] = None
    target_value: float = Field(gt=0)
    current_value: float = Field(default=0, ge=0)
    unit: str = "count"
    owner_role_id: Optional[str] = None
    actor: str = "ceo"


class KpiListResponse(BaseModel):
    project_id: str
    items: list[KpiSummary]
    limit: int
    offset: int


class PolicyRecordSummary(BaseModel):
    id: str
    project_id: str
    policy_key: str
    version: int
    title: str
    status: str
    body: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: str
    updated_at: str


class PolicyRecordCreateRequest(BaseModel):
    project_id: str
    policy_key: str
    version: int = Field(default=1, ge=1)
    title: str
    body: dict[str, Any]
    actor: str = "ceo"


class PolicyRecordListResponse(BaseModel):
    project_id: str
    items: list[PolicyRecordSummary]
    limit: int
    offset: int


class PlanSummary(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    title: str
    objective: str
    status: str
    phases: list[Any] = Field(default_factory=list)
    success_criteria: list[Any] = Field(default_factory=list)
    risks: list[Any] = Field(default_factory=list)
    artifact_id: Optional[str] = None
    created_by: str
    created_at: str
    updated_at: str


class PlanCreateRequest(BaseModel):
    project_id: str
    title: str
    objective: str
    task_id: Optional[str] = None
    phases: list[Any] = Field(default_factory=list)
    success_criteria: list[Any] = Field(default_factory=list)
    risks: list[Any] = Field(default_factory=list)
    artifact_id: Optional[str] = None
    actor: str = "ceo"


class PlanListResponse(BaseModel):
    project_id: str
    items: list[PlanSummary]
    limit: int
    offset: int


class MeetingSummary(BaseModel):
    id: str
    project_id: str
    title: str
    purpose: str
    status: str
    participants: list[str] = Field(default_factory=list)
    agenda: list[str] = Field(default_factory=list)
    transcript_artifact_id: Optional[str] = None
    minutes_artifact_id: Optional[str] = None
    action_task_ids: list[str] = Field(default_factory=list)
    decisions: list[Any] = Field(default_factory=list)
    dissent_records: list[Any] = Field(default_factory=list)
    blocked_state: Optional[dict[str, Any]] = None
    completed_at: Optional[str] = None
    created_by: str
    created_at: str
    updated_at: str
    phase_timeline: list[dict[str, Any]] = Field(default_factory=list)
    artifact_links: list[dict[str, Any]] = Field(default_factory=list)
    receipt_id: Optional[str] = None
    simulated: bool = False


class MeetingCreateRequest(BaseModel):
    project_id: str
    title: str
    purpose: str
    participants: list[str] = Field(default_factory=list)
    agenda: list[str] = Field(default_factory=list)
    actor: str = "ceo"


class MeetingTranscriptEntry(BaseModel):
    speaker: str
    message: str
    role_id: Optional[str] = None
    phase: Optional[str] = None
    timestamp: Optional[str] = None
    references: list[str] = Field(default_factory=list)


class MeetingActionItem(BaseModel):
    title: str
    description: Optional[str] = None
    summary: Optional[str] = None
    owner_role_id: Optional[str] = None
    due_at: Optional[str] = None
    priority: str = "p2"
    risk_tier: int = Field(default=1, ge=0, le=3)
    data_boundary: DataBoundary = "internal"


class MeetingDecisionItem(BaseModel):
    question: str
    recommendation: Union[dict[str, Any], str] = Field(default_factory=dict)
    options: list[dict[str, Any]] = Field(default_factory=list)
    risk_tier: int = Field(default=1, ge=0, le=3)
    cost_summary: dict[str, Any] = Field(default_factory=dict)
    deadline_at: Optional[str] = None
    default_action: str = "hold"
    task_id: Optional[str] = None


class MeetingRunRequest(BaseModel):
    actor: str = "program_manager"
    outcome_status: Literal["completed", "blocked"] = "completed"
    transcript: list[MeetingTranscriptEntry]
    minutes: dict[str, Any]
    action_items: list[MeetingActionItem] = Field(default_factory=list)
    decision_items: list[MeetingDecisionItem] = Field(default_factory=list)
    consensus_reached: bool = True
    dissent_records: list[dict[str, Any]] = Field(default_factory=list)
    blocked_state: Optional[dict[str, Any]] = None
    # Explicit vote phase: when votes are supplied, the consensus engine tallies them under
    # quorum/threshold rules and derives consensus_reached/dissent/outcome itself.
    votes: list[dict[str, Any]] = Field(default_factory=list)
    quorum: Optional[int] = Field(default=None, ge=1)
    vote_threshold: float = Field(default=0.5, gt=0.0, le=1.0)
    default_action: str = "hold"

    @model_validator(mode="after")
    def validate_meeting_outputs(self) -> "MeetingRunRequest":
        if not self.transcript:
            raise ValueError("transcript must contain at least one entry.")
        if not self.minutes:
            raise ValueError("minutes must be a non-empty object.")
        if self.outcome_status == "blocked" and not self.blocked_state:
            raise ValueError("blocked_state is required when outcome_status is blocked.")
        # When votes drive the outcome, the tally supplies dissent — skip the manual requirement.
        if not self.votes and not self.consensus_reached and not self.dissent_records:
            raise ValueError("dissent_records are required when consensus_reached is false.")
        return self


class MeetingTallyRequest(BaseModel):
    votes: list[dict[str, Any]]
    eligible_count: Optional[int] = Field(default=None, ge=0)
    quorum: Optional[int] = Field(default=None, ge=1)
    vote_threshold: float = Field(default=0.5, gt=0.0, le=1.0)
    default_action: str = "hold"

    @model_validator(mode="after")
    def validate_votes(self) -> "MeetingTallyRequest":
        if not self.votes:
            raise ValueError("votes must contain at least one entry.")
        return self


class MeetingSweepRequest(BaseModel):
    actor: str = "meeting_watchdog"


class MeetingListResponse(BaseModel):
    project_id: str
    items: list[MeetingSummary]
    limit: int
    offset: int


class SelfImprovementProposalCreateRequest(BaseModel):
    project_id: str
    title: str
    summary: str
    change_ref: str
    diff_summary: str
    expected_impact: list[str] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    canary_scope: list[str] = Field(default_factory=list)
    rollback_plan: list[str] = Field(default_factory=list)
    success_metrics: list[str] = Field(default_factory=list)
    actor: str = "vp_agents"
    risk_tier: int = Field(default=2, ge=1, le=3)

    @model_validator(mode="after")
    def validate_change_control_evidence(self) -> "SelfImprovementProposalCreateRequest":
        self.title = _require_text(self.title, "title")
        self.summary = _require_text(self.summary, "summary")
        self.change_ref = _require_text(self.change_ref, "change_ref")
        self.diff_summary = _require_text(self.diff_summary, "diff_summary")
        self.expected_impact = _require_non_empty_string_list(
            self.expected_impact, "expected_impact"
        )
        self.test_plan = _require_non_empty_string_list(self.test_plan, "test_plan")
        self.canary_scope = _require_non_empty_string_list(
            self.canary_scope, "canary_scope"
        )
        self.rollback_plan = _require_non_empty_string_list(
            self.rollback_plan, "rollback_plan"
        )
        self.success_metrics = _require_non_empty_string_list(
            self.success_metrics, "success_metrics"
        )
        self.actor = _require_text(self.actor, "actor")
        return self


class QABenchmarkTuningProposalRequest(BaseModel):
    """Create a governed improvement proposal from one benchmark report.

    The caller must supply an already-authored candidate PR or configuration-diff
    reference. Benchmark evidence may recommend a comparison, but it never creates
    or applies a code/configuration change on its own.
    """

    project_id: str
    change_ref: str
    diff_summary: str
    actor: str = "vp_agents"
    risk_tier: int = Field(default=2, ge=1, le=3)

    @model_validator(mode="after")
    def validate_change_control_fields(self) -> "QABenchmarkTuningProposalRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.change_ref = _require_text(self.change_ref, "change_ref")
        self.diff_summary = _require_text(self.diff_summary, "diff_summary")
        self.actor = _require_text(self.actor, "actor")
        return self


class ImprovementProgramCreateRequest(BaseModel):
    project_id: str
    name: str
    objective: str
    baseline_evaluation_id: str
    minimum_evidence_weight: int = Field(default=50, ge=1, le=100)
    minimum_overall_delta: float = Field(default=1.0, ge=0)
    maximum_dimension_regression: float = Field(default=0.0, ge=0)
    hard_gates: dict[str, float] = Field(default_factory=lambda: {"functionality": 100})
    actor: str = "vp_agents"

    @model_validator(mode="after")
    def validate_program(self) -> "ImprovementProgramCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.name = _require_text(self.name, "name")
        self.objective = _require_text(self.objective, "objective")
        self.baseline_evaluation_id = _require_text(
            self.baseline_evaluation_id, "baseline_evaluation_id"
        )
        self.actor = _require_text(self.actor, "actor")
        if not self.hard_gates:
            raise ValueError("hard_gates must contain at least one metric.")
        return self


class ImprovementProgramStatusRequest(BaseModel):
    status: Literal["active", "paused", "retired"]
    actor: str = "vp_agents"

    @model_validator(mode="after")
    def validate_actor(self) -> "ImprovementProgramStatusRequest":
        self.actor = _require_text(self.actor, "actor")
        return self


class ImprovementTrialCreateRequest(BaseModel):
    candidate_evaluation_id: str
    candidate_change_ref: str
    candidate_diff_summary: str
    changed_variable: str
    control_configuration: dict[str, Any]
    candidate_configuration: dict[str, Any]
    isolation_evidence_refs: list[str]
    actor: str = "vp_agents"

    @model_validator(mode="after")
    def validate_trial(self) -> "ImprovementTrialCreateRequest":
        self.candidate_evaluation_id = _require_text(
            self.candidate_evaluation_id, "candidate_evaluation_id"
        )
        self.candidate_change_ref = _require_text(
            self.candidate_change_ref, "candidate_change_ref"
        )
        self.candidate_diff_summary = _require_text(
            self.candidate_diff_summary, "candidate_diff_summary"
        )
        self.changed_variable = _require_text(self.changed_variable, "changed_variable")
        self.actor = _require_text(self.actor, "actor")
        if not self.control_configuration or not self.candidate_configuration:
            raise ValueError("control_configuration and candidate_configuration are required.")
        self.isolation_evidence_refs = _require_non_empty_string_list(
            self.isolation_evidence_refs, "isolation_evidence_refs"
        )
        return self


class ImprovementTrialActionRequest(BaseModel):
    actor: str = "vp_agents"
    risk_tier: int = Field(default=2, ge=1, le=3)

    @model_validator(mode="after")
    def validate_actor(self) -> "ImprovementTrialActionRequest":
        self.actor = _require_text(self.actor, "actor")
        return self


class SelfImprovementProposalSummary(BaseModel):
    id: str
    project_id: str
    task_id: str
    title: str
    summary: str
    change_ref: str
    diff_summary: str
    status: str
    risk_tier: int
    expected_impact: list[str] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    canary_scope: list[str] = Field(default_factory=list)
    rollback_plan: list[str] = Field(default_factory=list)
    success_metrics: list[str] = Field(default_factory=list)
    status_note: Optional[str] = None
    proposal_artifact_id: Optional[str] = None
    current_decision_id: Optional[str] = None
    canary_artifact_id: Optional[str] = None
    rollout_artifact_id: Optional[str] = None
    rollback_artifact_id: Optional[str] = None
    rollout_state: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: str
    updated_at: str


class SelfImprovementProposalDetail(SelfImprovementProposalSummary):
    pass


class SelfImprovementProposalListResponse(BaseModel):
    project_id: str
    items: list[SelfImprovementProposalSummary]
    limit: int
    offset: int


class SelfImprovementCanaryRequest(BaseModel):
    actor: str = "release_manager"
    outcome: str
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_canary_evidence(self) -> "SelfImprovementCanaryRequest":
        self.actor = _require_text(self.actor, "actor")
        self.outcome = _require_text(self.outcome, "outcome")
        self.summary = _require_text(self.summary, "summary")
        self.metrics, self.evidence_refs = _require_evidence(
            self.metrics, self.evidence_refs
        )
        return self


class SelfImprovementRolloutRequest(BaseModel):
    actor: str = "release_manager"
    summary: str
    external_ref: Optional[str] = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_rollout_evidence(self) -> "SelfImprovementRolloutRequest":
        self.actor = _require_text(self.actor, "actor")
        self.summary = _require_text(self.summary, "summary")
        if self.external_ref is not None:
            self.external_ref = _require_text(self.external_ref, "external_ref")
        self.metrics, self.evidence_refs = _require_evidence(
            self.metrics, self.evidence_refs
        )
        return self


class SelfImprovementRollbackRequest(BaseModel):
    actor: str = "release_manager"
    reason: str
    external_ref: Optional[str] = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_rollback_evidence(self) -> "SelfImprovementRollbackRequest":
        self.actor = _require_text(self.actor, "actor")
        self.reason = _require_text(self.reason, "reason")
        if self.external_ref is not None:
            self.external_ref = _require_text(self.external_ref, "external_ref")
        self.metrics, self.evidence_refs = _require_evidence(
            self.metrics, self.evidence_refs
        )
        return self


class SelfImprovementRunCanaryRequest(BaseModel):
    actor: str = "release_manager"

    @model_validator(mode="after")
    def validate_actor(self) -> "SelfImprovementRunCanaryRequest":
        self.actor = _require_text(self.actor, "actor")
        return self


class SelfImprovementStartRolloutRequest(BaseModel):
    actor: str = "release_manager"
    stages: Optional[list[str]] = None
    thresholds: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_fields(self) -> "SelfImprovementStartRolloutRequest":
        self.actor = _require_text(self.actor, "actor")
        if self.stages is not None:
            self.stages = _require_non_empty_string_list(self.stages, "stages")
        return self


class SelfImprovementAdvanceRolloutRequest(BaseModel):
    actor: str = "release_manager"
    observation: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_fields(self) -> "SelfImprovementAdvanceRolloutRequest":
        self.actor = _require_text(self.actor, "actor")
        self.observation = _require_non_empty_mapping(self.observation, "observation")
        return self


class DecisionCard(BaseModel):
    id: str
    task_id: Optional[str] = None
    question: str
    status: str
    risk_tier: int
    default_action: Optional[str] = None
    reason: Optional[str] = None
    recommendation: dict[str, Any]
    options: list[dict[str, Any]]
    cost_summary: dict[str, Any]
    deadline_at: Optional[str] = None
    approved_by: Optional[str] = None
    # Who approved it, by name: the card never shows an actor id.
    approved_by_name: Optional[str] = None
    approved_at: Optional[str] = None
    delegated_to: Optional[str] = None
    delegated_by: Optional[str] = None
    delegated_at: Optional[str] = None
    revision_requested_by: Optional[str] = None
    revision_requested_at: Optional[str] = None
    executed_by: Optional[str] = None
    executed_at: Optional[str] = None
    resolution_note: Optional[str] = None
    external_ref: Optional[str] = None
    created_at: str
    updated_at: str
    # What a person can do with it, decided by the engine (services/decision_actions.py) so the
    # console draws only buttons that work. Defaults keep older payloads valid.
    kind: str = "general"
    allowed_actions: list[dict[str, Any]] = Field(default_factory=list)
    guidance: Optional[dict[str, Any]] = None
    # needs_action, expired or resolved (decision_actions.attention). The inbox filters on it
    # and the Command Center counts with the same rule, so the two never disagree.
    attention: Optional[str] = None


class DecisionInboxResponse(BaseModel):
    project_id: str
    items: list[DecisionCard]
    limit: Optional[int] = None
    offset: int = 0


class DecisionActionRequest(BaseModel):
    actor: str = "executive_console"
    note: Optional[str] = None
    delegate_to: Optional[str] = None
    external_ref: Optional[str] = None
    # Operator override for a merge gate BOSS declined. BOSS still performs the merge, so the
    # action stays inside the governed path with a receipt instead of the human reaching for
    # `gh pr merge` outside the system. `note` becomes mandatory and records why.
    force: bool = False
    # The `action_preview.content_hash` the approver was looking at. When it no longer matches
    # the decision's preview (the content changed while they read it), the approval is refused.
    content_hash: Optional[str] = None


class DecisionDigestRequest(BaseModel):
    project_id: str
    channel: Optional[str] = None
    actor: str = "executive_console"
    max_items: int = Field(default=20, ge=1, le=100)
    low_risk_max: int = Field(default=1, ge=0, le=3)
    urgent_risk_min: int = Field(default=2, ge=0, le=3)
    dedupe_key: Optional[str] = None
    data_boundary: DataBoundary = "internal"


class DecisionDigestResponse(BaseModel):
    status: str
    artifact_id: str
    outbox_message_id: Optional[str] = None
    decision_count: int
    urgent_count: int
    batched_count: int
    body: str


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
    limit: int
    offset: int


class ArtifactSummary(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    artifact_type: str
    title: str
    uri: Optional[str] = None
    content_hash: Optional[str] = None
    created_by_job_id: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    created_at: str


class ArtifactDetail(ArtifactSummary):
    content: dict[str, Any] = Field(default_factory=dict)


class ArtifactListResponse(BaseModel):
    project_id: str
    items: list[ArtifactSummary]
    limit: int = 25
    offset: int = 0


class ChangeJournalItem(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    artifact_type: str
    change_kind: str
    title: str
    summary: Optional[str] = None
    diff_summary: Optional[str] = None
    files_changed: list[str] = Field(default_factory=list)
    rollback_plan: Optional[str] = None
    created_by_job_id: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    created_at: str


class ChangeJournalDetail(ChangeJournalItem):
    content: dict[str, Any] = Field(default_factory=dict)


class ChangeJournalResponse(BaseModel):
    project_id: str
    items: list[ChangeJournalItem]
    limit: int = 25
    offset: int = 0


class DeadletterItem(BaseModel):
    id: str
    project_id: Optional[str] = None
    source_kind: str
    source_id: str
    related_entity_type: Optional[str] = None
    related_entity_id: Optional[str] = None
    related_status: Optional[str] = None
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str = "open"
    incident_task_id: Optional[str] = None
    incident_artifact_id: Optional[str] = None
    remediation_decision_id: Optional[str] = None
    requeued_job_id: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None
    created_at: str
    updated_at: Optional[str] = None


class DeadletterListResponse(BaseModel):
    project_id: str
    items: list[DeadletterItem]
    limit: int = 50
    offset: int = 0


class DeadletterIncidentRequest(BaseModel):
    actor: str = "system"
    severity: Literal["low", "medium", "high", "critical"] = "high"
    summary: Optional[str] = None


class DeadletterIncidentResponse(BaseModel):
    status: str
    deadletter: DeadletterItem
    incident_task_id: str
    incident_artifact_id: str


class DeadletterRequeueRequest(BaseModel):
    actor: str = "system"
    reason: str = "operator_requested_requeue"
    run_after_seconds: int = Field(default=0, ge=0, le=3600)


class DeadletterRequeueResponse(BaseModel):
    status: str
    deadletter: DeadletterItem
    requeued_job_id: str


class DeadletterClassifyItem(BaseModel):
    id: str
    source_kind: str
    source_id: str
    reason: str
    error_class: str
    status: str = "open"
    job_kind: Optional[str] = None
    remediation_decision_id: Optional[str] = None
    escalated_to_decision: bool = False
    created_at: Optional[str] = None


class DeadletterClassifyEscalation(BaseModel):
    deadletter_id: str
    decision_id: str
    reason: Optional[str] = None


class DeadletterClassifyRequest(BaseModel):
    project_id: str
    limit: int = Field(default=200, ge=1, le=500)
    escalate_model: bool = True
    actor: str = "system"


class DeadletterClassifyResponse(BaseModel):
    project_id: str
    total_open: int
    by_class: dict[str, int]
    items: list[DeadletterClassifyItem] = Field(default_factory=list)
    escalations: list[DeadletterClassifyEscalation] = Field(default_factory=list)
    error_classes: list[str] = Field(default_factory=list)


class BudgetReservationSummary(BaseModel):
    id: str
    project_id: str
    job_id: Optional[str] = None
    category: str
    amount_usd_micros: int
    status: str
    reason: Optional[str] = None
    expires_at: str
    created_at: str


class CostReceiptSummary(BaseModel):
    id: str
    project_id: str
    reservation_id: Optional[str] = None
    receipt_id: Optional[str] = None
    category: str
    skill_or_model_id: str
    estimated_usd_micros: int
    actual_usd_micros: Optional[int] = None
    status: str
    created_at: str


class AuditExportScope(BaseModel):
    task_id: Optional[str] = None
    decision_id: Optional[str] = None
    job_id: Optional[str] = None


class AuditExportCounts(BaseModel):
    tasks: int
    jobs: int
    decisions: int
    artifacts: int
    receipts: int
    budget_reservations: int
    cost_receipts: int
    routing_logs: int
    deadletters: int
    events: int


class AuditSummaryResponse(BaseModel):
    """Just the counts an unscoped export reports. See AuditTrailService.summary."""

    project_id: str
    generated_at: str
    counts: AuditExportCounts


class AuditExportResponse(BaseModel):
    project_id: str
    project_name: str
    workspace_name: str
    org_name: str
    generated_at: str
    governance: dict[str, Any] = Field(default_factory=dict)
    scope: AuditExportScope
    counts: AuditExportCounts
    chain: dict[str, list[str]]
    tasks: list[TaskSummary]
    jobs: list[JobSummary]
    decisions: list[DecisionCard]
    artifacts: list[ArtifactSummary]
    receipts: list[ReceiptResponse]
    budget_reservations: list[BudgetReservationSummary]
    cost_receipts: list[CostReceiptSummary]
    routing_logs: list["RoutingLogItem"]
    deadletters: list[DeadletterItem]
    events: list[EventItem]


class ReplayBundleRequest(BaseModel):
    project_id: str
    task_id: Optional[str] = None
    decision_id: Optional[str] = None
    job_id: Optional[str] = None
    actor: str = "system"


class ReplayBundleResponse(BaseModel):
    status: str
    artifact_id: str
    project_id: str
    artifact_type: str
    created_at: str
    scope: AuditExportScope
    counts: AuditExportCounts


class ComplianceEvidenceBundleRequest(BaseModel):
    project_id: str
    actor: str = "ceo"
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_period(self) -> "ComplianceEvidenceBundleRequest":
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError("period_end must be after period_start.")
        self.actor = _require_text(self.actor, "actor")
        return self


class ComplianceEvidenceCounts(BaseModel):
    project_members: int
    roles: int
    policy_records: int
    break_glass_grants: int
    decisions: int
    receipts: int
    budget_reservations: int
    cost_receipts: int
    deadletters: int
    compliance_events: int


class ComplianceEvidenceBundleResponse(BaseModel):
    status: str
    artifact_id: str
    project_id: str
    artifact_type: str
    created_at: str
    period: dict[str, Optional[str]]
    counts: ComplianceEvidenceCounts


class OutboxMessageCreateRequest(BaseModel):
    project_id: str
    channel: str
    subject: Optional[str] = None
    body: str
    data_boundary: DataBoundary = "internal"
    dedupe_key: str
    actor: str = "executive_console"


class EmailTestRequest(BaseModel):
    project_id: str
    to: str
    dedupe_key: str = Field(max_length=128)
    actor: str = "executive_console"

    @model_validator(mode="after")
    def validate_test(self) -> "EmailTestRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.to = _require_text(self.to, "to")
        self.dedupe_key = _require_text(self.dedupe_key, "dedupe_key")
        self.actor = _require_text(self.actor, "actor")
        return self


class OutboxMessageSummary(BaseModel):
    id: str
    project_id: str
    channel: str
    subject: Optional[str] = None
    body: str
    status: str
    data_boundary: DataBoundary = "internal"
    dedupe_key: str
    receipt_id: Optional[str] = None
    created_at: str
    updated_at: str


class OutboxMessageListResponse(BaseModel):
    project_id: str
    items: list[OutboxMessageSummary]
    limit: int = 50
    offset: int = 0


class CommsChannelPolicySummary(BaseModel):
    id: str
    project_id: str
    provider: str
    delivery_mode: str
    allowed_data_boundaries: list[DataBoundary]
    rollout_notes: Optional[str] = None
    status: str
    created_by: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class CommsChannelPolicyListResponse(BaseModel):
    project_id: str
    items: list[CommsChannelPolicySummary]


class CommsChannelPolicyUpsertRequest(BaseModel):
    project_id: str
    provider: str
    delivery_mode: Literal["disabled", "draft_only", "real_send_allowed"]
    allowed_data_boundaries: list[DataBoundary] = Field(
        default_factory=lambda: ["public", "internal"]
    )
    rollout_notes: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "executive_console"

    @model_validator(mode="after")
    def validate_comms_policy(self) -> "CommsChannelPolicyUpsertRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.provider = _require_text(self.provider, "provider")
        self.actor = _require_text(self.actor, "actor")
        return self


class OutboxQueueRequest(BaseModel):
    project_id: str
    #: Optional on /queue: without one the API reserves the comms spend itself (a person never
    #: calls the system-only /v1/budget/reserve; identity int-b). Required on /deliver.
    reservation_id: Optional[str] = None
    dedupe_key: str
    actor: str = "executive_console"
    priority: int = Field(default=25, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    urgency: Literal["low", "normal", "urgent"] = "normal"
    created_by_job_id: Optional[str] = None


class OutboxDeliveryResponse(BaseModel):
    status: str
    data_boundary: Optional[DataBoundary] = None
    message_id: Optional[str] = None
    receipt_id: Optional[str] = None
    decision_id: Optional[str] = None
    provider_ref: Optional[str] = None
    message: Optional[str] = None


class InboxMessageIngestRequest(BaseModel):
    project_id: str
    channel: str
    subject: Optional[str] = None
    body: str
    raw_ref: Optional[str] = None
    data_boundary: DataBoundary = "internal"
    actor: str = "system"


class InboxMessageSummary(BaseModel):
    id: str
    project_id: Optional[str] = None
    channel: str
    raw_ref: Optional[str] = None
    subject: Optional[str] = None
    body: str
    status: str
    data_boundary: DataBoundary = "internal"
    parsed: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class InboxMessageListResponse(BaseModel):
    project_id: str
    items: list[InboxMessageSummary]
    limit: int = 50
    offset: int = 0


class RoleSummary(BaseModel):
    id: str
    project_id: str
    role_key: str
    display_name: str
    autonomy_level: int
    source_module_id: Optional[str] = None
    source_module_version: Optional[str] = None
    trust_score: int
    successful_runs: int = 0
    policy_violation_count: int = 0
    duplicate_effect_attempt_count: int = 0
    hidden_failure_count: int = 0
    low_quality_output_count: int = 0
    scope_violation_count: int = 0
    last_reviewed_at: Optional[str] = None
    last_violation_at: Optional[str] = None
    #: role_key of this role's manager, or None for a root. BOSS has never required a hierarchy
    #: to exist, so absence is the normal case rather than a gap.
    reports_to: Optional[str] = None
    created_at: str


class RegisterRoleRequest(BaseModel):
    """Put an agent on the books.

    Everything except the key is optional: the common case is a console button pressed against
    an agent BOSS is already watching do work, where the only fact to hand is its key.
    """

    project_id: str
    role_key: str
    display_name: Optional[str] = None
    #: Left out on an update, the current level is kept (a new role starts at 0).
    autonomy_level: Optional[int] = None
    #: role_key of the manager, or None for a root. NOT validated against existing roles: the
    #: org is discovered from observed handoffs, so a manager is often registered after the
    #: role reporting to them.
    reports_to: Optional[str] = None
    spec: Optional[dict[str, Any]] = None


class RoleDetail(RoleSummary):
    spec: dict[str, Any]


# Reusable mapped workflow runtime (WFL-001 / BOSS-134..143).
WorkflowRunState = Literal[
    "queued",
    "validating",
    "running",
    "waiting",
    "paused",
    "blocked",
    "completed",
    "failed",
    "deadlettered",
    "cancelled",
    "compensating",
    "compensated",
]


class WorkflowDefinitionCreateRequest(BaseModel):
    project_id: str
    workflow_key: str
    name: str
    description: Optional[str] = None
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_fields(self) -> "WorkflowDefinitionCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.workflow_key = _require_text(self.workflow_key, "workflow_key")
        self.name = _require_text(self.name, "name")
        self.actor = _require_text(self.actor, "actor")
        return self


class WorkflowDefinitionSummary(BaseModel):
    id: str
    project_id: str
    workflow_key: str
    name: str
    description: Optional[str] = None
    status: str
    active_version_id: Optional[str] = None
    created_by: str
    created_at: str
    updated_at: str
    version_count: Optional[int] = None


class WorkflowVersionCreateRequest(BaseModel):
    project_id: str
    spec: dict[str, Any]
    activate: bool = True
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_fields(self) -> "WorkflowVersionCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        self.spec = _require_non_empty_mapping(self.spec, "spec")
        return self


class WorkflowDraftValidateRequest(BaseModel):
    project_id: str
    spec: dict[str, Any]
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_fields(self) -> "WorkflowDraftValidateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        self.spec = _require_non_empty_mapping(self.spec, "spec")
        return self


class WorkflowVersionSummary(BaseModel):
    id: str
    project_id: str
    workflow_definition_id: str
    version: int
    status: str
    spec: dict[str, Any]
    content_hash: str
    created_by: str
    created_at: str
    published_at: str
    duplicate: Optional[bool] = None


class WorkflowDefinitionDetail(WorkflowDefinitionSummary):
    versions: list[WorkflowVersionSummary] = Field(default_factory=list)


class WorkflowDefinitionListResponse(BaseModel):
    project_id: str
    items: list[WorkflowDefinitionSummary]


class WorkflowMappingCreateRequest(BaseModel):
    project_id: str
    workflow_version_id: str
    selector_kind: str
    selector_value: str
    scope_type: Literal["org", "workspace", "project"] = "project"
    scope_id: str
    priority: int = 100
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_fields(self) -> "WorkflowMappingCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.workflow_version_id = _require_text(
            self.workflow_version_id, "workflow_version_id"
        )
        self.selector_kind = _require_text(self.selector_kind, "selector_kind")
        self.selector_value = _require_text(self.selector_value, "selector_value")
        self.scope_id = _require_text(self.scope_id, "scope_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class WorkflowMappingSummary(BaseModel):
    id: str
    project_id: str
    workflow_version_id: str
    selector_kind: str
    selector_value: str
    scope_type: str
    scope_id: str
    priority: int
    enabled: bool
    created_by: str
    created_at: str
    updated_at: str


class WorkflowMappingListResponse(BaseModel):
    project_id: str
    items: list[WorkflowMappingSummary]


class WorkflowMappingResolveRequest(BaseModel):
    project_id: str
    selector_kind: str
    selector_value: str
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_fields(self) -> "WorkflowMappingResolveRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.selector_kind = _require_text(self.selector_kind, "selector_kind")
        self.selector_value = _require_text(self.selector_value, "selector_value")
        self.actor = _require_text(self.actor, "actor")
        return self


class WorkflowRunStartRequest(BaseModel):
    project_id: str
    workflow_version_id: Optional[str] = None
    selector_kind: Optional[str] = None
    selector_value: Optional[str] = None
    input: dict[str, Any] = Field(default_factory=dict)
    policy_snapshot: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str
    parent_run_id: Optional[str] = None
    actor: str = "ceo"
    #: Readiness keys the operator was shown missing and chose to start without. This door
    #: preflights like the delivery door, and without the field a refusal here (Agent Studio's
    #: Run) had no way to be overridden at all.
    start_anyway: Optional[list[str]] = None
    #: The pre-check's choices (services.workflow_precheck.gate_start): will_fail checks started
    #: anyway, steps skipped (never a gate that protects a deploy), and why. Recorded on the run.
    accept: Optional[list[str]] = None
    skip_steps: Optional[list[Any]] = None
    precheck_reason: Optional[str] = None

    @model_validator(mode="after")
    def validate_fields(self) -> "WorkflowRunStartRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.dedupe_key = _require_text(self.dedupe_key, "dedupe_key")
        self.actor = _require_text(self.actor, "actor")
        if not self.workflow_version_id and not (
            self.selector_kind and self.selector_value
        ):
            raise ValueError(
                "workflow_version_id or selector_kind/selector_value is required."
            )
        return self


class WorkflowRunActorRequest(BaseModel):
    project_id: str
    actor: str = "ceo"


class WorkflowRunCancelRequest(WorkflowRunActorRequest):
    reason: str
    compensate: bool = True


class WorkflowStepClaimRequest(BaseModel):
    project_id: str
    worker_id: str
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class WorkflowStepHeartbeatRequest(BaseModel):
    project_id: str
    worker_id: str
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class WorkflowStepCompleteRequest(BaseModel):
    project_id: str
    worker_id: str
    output: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowStepFailRequest(BaseModel):
    project_id: str
    worker_id: str
    error: str
    retryable: bool = True
    retry_delay_seconds: int = Field(default=0, ge=0, le=86400)


class WorkflowRunMigrateRequest(WorkflowRunActorRequest):
    target_version_id: str
    reason: str


class WorkflowMigrationRollbackRequest(WorkflowRunActorRequest):
    migration_id: str


class WorkflowCompensationBeginRequest(WorkflowRunActorRequest):
    reason: str


class WorkflowCompensationClaimRequest(BaseModel):
    project_id: str
    worker_id: str
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class WorkflowCompensationCompleteRequest(BaseModel):
    project_id: str
    worker_id: str
    output: dict[str, Any] = Field(default_factory=dict)


class WorkflowCompensationFailRequest(BaseModel):
    project_id: str
    worker_id: str
    error: str


class WorkflowStepRunSummary(BaseModel):
    id: str
    project_id: str
    workflow_run_id: str
    step_id: str
    step_kind: str
    iteration: int
    status: str
    input: dict[str, Any]
    output: dict[str, Any]
    binding_snapshot: dict[str, Any]
    evidence: list[dict[str, Any]]
    attempt: int
    max_attempts: int
    run_after: str
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    decision_id: Optional[str] = None
    child_workflow_run_id: Optional[str] = None
    last_error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str
    updated_at: str


class WorkflowRunSummary(BaseModel):
    id: str
    project_id: str
    workflow_definition_id: str
    workflow_version_id: str
    workflow_mapping_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    root_run_id: Optional[str] = None
    actor: str
    status: WorkflowRunState
    input: dict[str, Any]
    output: dict[str, Any]
    context: dict[str, Any]
    policy_snapshot: dict[str, Any]
    current_frontier: list[dict[str, Any]]
    dedupe_key: Optional[str] = None
    blocked_reason: Optional[str] = None
    error: Optional[str] = None
    paused_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str
    updated_at: str
    workflow_key: str
    workflow_name: str
    version: int
    duplicate: Optional[bool] = None
    #: What an operator may do to this run, decided from its state by
    #: services/workflow_run_actions — the same list the console renders and the same decision
    #: every operation on the run enforces. On the summary, not only the detail, so the agent's
    #: and the MCP list surface see what a run offers without fetching each one.
    actions: list[dict[str, Any]] = Field(default_factory=list)
    #: Where the run stands — active, stopped (at a failed step, waiting for the operator),
    #: closed or compensating — from `workflow_run_actions.standing`, so no client keeps its
    #: own list of run states.
    standing: Optional[str] = None


class WorkflowRunDetail(WorkflowRunSummary):
    steps: list[WorkflowStepRunSummary] = Field(default_factory=list)
    migrations: list[dict[str, Any]] = Field(default_factory=list)
    compensations: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowRunListResponse(BaseModel):
    project_id: str
    items: list[WorkflowRunSummary]


class RoleListResponse(BaseModel):
    project_id: str
    items: list[RoleSummary]


class RoleReviewRequest(BaseModel):
    project_id: str
    actor: str = "vp_agents"
    summary: str
    metrics: dict[str, Any]
    evidence_refs: list[str]

    @model_validator(mode="after")
    def validate_fields(self) -> "RoleReviewRequest":
        self.actor = _require_text(self.actor, "actor")
        self.summary = _require_text(self.summary, "summary")
        self.metrics = _require_non_empty_mapping(self.metrics, "metrics")
        self.evidence_refs = _require_non_empty_string_list(
            self.evidence_refs, "evidence_refs"
        )
        return self


class AutonomyEvaluateRequest(BaseModel):
    project_id: str
    actor: str = "vp_agents"

    @model_validator(mode="after")
    def validate_fields(self) -> "AutonomyEvaluateRequest":
        self.actor = _require_text(self.actor, "actor")
        return self


class RoleTrustEvidenceRequest(BaseModel):
    project_id: str
    actor: str = "vp_agents"
    evidence_type: Literal[
        "successful_run",
        "duplicate_effect_attempt",
        "hidden_failure",
        "low_quality_output",
    ]
    evidence_ref: str
    detail: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_fields(self) -> "RoleTrustEvidenceRequest":
        self.actor = _require_text(self.actor, "actor")
        self.evidence_ref = _require_text(self.evidence_ref, "evidence_ref")
        return self


class RoleTrustEvidenceResponse(BaseModel):
    event_id: str
    recorded: bool
    event_type: str


class RoleAutonomyCheckRequest(BaseModel):
    project_id: str
    actor: str = "system"
    action: str
    risk_tier: int = Field(default=0, ge=0, le=3)
    sandboxed: bool = False
    policy_approved: bool = False

    @model_validator(mode="after")
    def validate_fields(self) -> "RoleAutonomyCheckRequest":
        self.actor = _require_text(self.actor, "actor")
        self.action = _require_text(self.action, "action")
        return self


class RoleAutonomyCheckResponse(BaseModel):
    project_id: str
    role_key: str
    allowed: bool
    reason: str
    action: str
    required_capability: str
    risk_tier: int
    sandboxed: bool
    policy_approved: bool
    current_level: int
    required_level: Optional[int] = None
    allowed_capabilities: list[str] = Field(default_factory=list)
    trust_score: int
    # G2 — trust_score → risk-tier gate (fail-closed). Present on all checks.
    min_trust_required: Optional[int] = None
    max_allowed_risk_tier: Optional[int] = None
    trust_gate_passed: Optional[bool] = None


class AutonomyScoreResponse(BaseModel):
    """G1 — current earned-autonomy score for a role (read-only)."""

    project_id: str
    role_key: str
    role_id: str
    display_name: str
    trust_score: int
    autonomy_level: int
    max_allowed_risk_tier: int
    trust_risk_tier_floors: dict[str, int] = Field(default_factory=dict)
    successful_runs: int = 0
    policy_violation_count: int = 0
    scope_violation_count: int = 0
    duplicate_effect_attempt_count: int = 0
    hidden_failure_count: int = 0
    low_quality_output_count: int = 0
    last_reviewed_at: Optional[str] = None
    score_exists: bool = True
    gate_mode: str = "fail_closed"


class RoleScopeCheckRequest(BaseModel):
    project_id: str
    actor: str = "system"
    action: str
    skill_id: Optional[str] = None
    evidence_ref: Optional[str] = None

    @model_validator(mode="after")
    def validate_fields(self) -> "RoleScopeCheckRequest":
        self.actor = _require_text(self.actor, "actor")
        self.action = _require_text(self.action, "action")
        if self.skill_id is not None:
            self.skill_id = _require_text(self.skill_id, "skill_id")
        if self.evidence_ref is not None:
            self.evidence_ref = _require_text(self.evidence_ref, "evidence_ref")
        return self


class RoleScopeCheckResponse(BaseModel):
    project_id: str
    role_key: str
    allowed: bool
    reason: str
    action: str
    skill_id: str
    allowed_skills: list[str] = Field(default_factory=list)
    forbidden_skills: list[str] = Field(default_factory=list)
    trust_score_before: int
    trust_score_after: int
    autonomy_level_before: int
    autonomy_level_after: int
    scope_violation_count: int
    artifact_id: Optional[str] = None
    autonomy_change_id: Optional[str] = None


class RoleReviewSummary(BaseModel):
    id: str
    role_key: str
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    trust_score_before: int
    trust_delta: int
    trust_score_after: int
    autonomy_level_before: int
    autonomy_level_after: int
    governance_outcome: str
    autonomy_change_id: Optional[str] = None
    artifact_id: Optional[str] = None
    created_by: str
    created_at: str


class AutonomyChangeSummary(BaseModel):
    id: str
    role_key: str
    decision_id: Optional[str] = None
    change_type: str
    trigger: str
    from_level: int
    to_level: int
    status: str
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    created_at: str
    updated_at: str


class RoleGovernanceDetail(BaseModel):
    project_id: str
    role: RoleDetail
    policy: dict[str, Any] = Field(default_factory=dict)
    pending_autonomy_change: Optional[AutonomyChangeSummary] = None
    recent_reviews: list[RoleReviewSummary] = Field(default_factory=list)
    recent_autonomy_changes: list[AutonomyChangeSummary] = Field(default_factory=list)
    evidence: Optional[dict[str, Any]] = None


class PackSummary(BaseModel):
    pack_id: str
    version: str
    description: str
    status: str
    requires: list[str]
    roles: list[str]
    workflows: list[str]
    policies: list[str]
    skills: list[str]
    installed: bool
    installed_by: Optional[str] = None
    installed_at: Optional[str] = None
    updated_at: Optional[str] = None
    installed_version: Optional[str] = None


class PackDetail(PackSummary):
    role_count: Optional[int] = None
    role_results: list[dict[str, Any]] = Field(default_factory=list)
    install_status: Optional[str] = None


class PackListResponse(BaseModel):
    project_id: str
    items: list[PackSummary]


class PackInstallRequest(BaseModel):
    project_id: str
    pack_id: str
    actor: str = "system"


class PackImportRequest(BaseModel):
    project_id: str
    yaml_content: str
    actor: str = "system"


class SecretRefSummary(BaseModel):
    id: str
    project_id: str
    scope_type: str
    scope_id: str
    ref_kind: str
    ref_key: str
    provider: Optional[str] = None
    secret_kind: str
    storage_backend: str
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    rotation_due_at: Optional[str] = None
    last_verified_at: Optional[str] = None
    supersedes_secret_ref_id: Optional[str] = None
    overlap_expires_at: Optional[str] = None
    revoked_at: Optional[str] = None
    revoked_by: Optional[str] = None
    created_by: str
    created_at: str
    updated_at: str
    credential_ref: str


class SecretRefListResponse(BaseModel):
    project_id: str
    items: list[SecretRefSummary]
    limit: int
    offset: int


class SecretRefCreateRequest(BaseModel):
    project_id: str
    ref_kind: str
    ref_key: str
    provider: Optional[str] = None
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)
    scope_type: str = "project"
    scope_id: Optional[str] = None
    secret_kind: str = "api_token"
    storage_backend: Optional[str] = None
    rotation_due_at: Optional[datetime] = None
    actor: str = "system"


class SecretRefUpdateRequest(BaseModel):
    project_id: str
    provider: Optional[str] = None
    status: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    rotation_due_at: Optional[datetime] = None
    actor: str = "system"


class AgentModuleSummary(BaseModel):
    module_id: str
    version: str
    display_name: str
    description: str
    publisher: str
    category: str
    status: str
    role_key: Optional[str] = None
    job_kinds: list[str] = Field(default_factory=list)
    default_skill_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    price_model: Optional[str] = None
    price_usd_micros: Optional[int] = None
    required_entitlements: list[str] = Field(default_factory=list)
    entitlement_status: str = "not_required"
    config_schema: dict[str, Any] = Field(default_factory=dict)
    installed: bool
    installed_by: Optional[str] = None
    installed_at: Optional[str] = None
    updated_at: Optional[str] = None
    installed_version: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)


class AgentModuleDetail(AgentModuleSummary):
    prompt_bundle: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    marketplace: dict[str, Any] = Field(default_factory=dict)
    role: dict[str, Any] = Field(default_factory=dict)
    role_result: dict[str, Any] = Field(default_factory=dict)
    entitlement_result: dict[str, Any] = Field(default_factory=dict)
    install_status: Optional[str] = None


class AgentModuleListResponse(BaseModel):
    project_id: str
    items: list[AgentModuleSummary]


class AgentModuleInstallRequest(BaseModel):
    project_id: str
    module_id: str
    actor: str = "system"
    config: dict[str, Any] = Field(default_factory=dict)


class AgentModuleLifecycleRequest(BaseModel):
    project_id: str
    actor: str = "system"
    reason: Optional[str] = None

    @model_validator(mode="after")
    def validate_agent_module_lifecycle(self) -> "AgentModuleLifecycleRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        if self.reason is not None:
            self.reason = _require_text(self.reason, "reason")
        return self


class AgentModuleInstallRecordSummary(BaseModel):
    id: str
    project_id: str
    module_id: str
    action: str
    from_version: Optional[str] = None
    to_version: Optional[str] = None
    actor: str
    status: str
    entitlement_result: dict[str, Any] = Field(default_factory=dict)
    rollback_snapshot: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class AgentModuleInstallRecordListResponse(BaseModel):
    project_id: str
    items: list[AgentModuleInstallRecordSummary]


class AgentRuntimeResolveRequest(BaseModel):
    project_id: str
    job_kind: str
    agent_module_id: Optional[str] = None
    task_payload: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_agent_runtime(self) -> "AgentRuntimeResolveRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.job_kind = _require_text(self.job_kind, "job_kind")
        if self.agent_module_id is not None:
            self.agent_module_id = _require_text(
                self.agent_module_id,
                "agent_module_id",
            )
        self.actor = _require_text(self.actor, "actor")
        return self


class AgentRuntimeResolveResponse(BaseModel):
    project_id: str
    job_kind: str
    matched: bool
    agent_module: Optional[dict[str, Any]] = None
    compiled_prompt: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    role: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class SkillSummary(BaseModel):
    id: str
    kind: str
    system: bool = False
    load: Literal["eager", "on_demand"] = "eager"
    source: str
    enabled: bool
    on_demand: bool
    risk_tier: int
    description: str
    mock_mode: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    cost: dict[str, Any]
    retry: dict[str, Any]
    permissions: dict[str, Any]
    data_boundary: str
    policy: dict[str, Any]
    publisher: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    # Whether BOSS can run it here, and why not (the Library shows "Set up", never "Ready",
    # for a skill nothing can execute — the provider cost syncs, for one).
    usable: Optional[bool] = None
    reason: Optional[str] = None


class SkillListResponse(BaseModel):
    items: list[SkillSummary]


class ModelAdapterSummary(BaseModel):
    id: str
    provider: str
    source_id: str
    source_kind: str
    access_class: str
    entitlement_kind: str
    auth_method: str
    connection_ref: Optional[str] = None
    supported_models: list[str]
    task_strengths: list[str]
    allowed_workload_classes: list[str]
    multi_tenant_allowed: bool
    runtime_supported: bool
    credential_present: bool
    official_connection_required: bool
    policy_notes: list[str]


class ModelAdapterListResponse(BaseModel):
    items: list[ModelAdapterSummary]


class EntitlementSourceSummary(BaseModel):
    id: str
    project_id: str
    provider: str
    source_id: str
    source_kind: str
    entitlement_kind: str
    owner_type: str
    owner_id: str
    workload_classes_allowed: list[str]
    multi_tenant_allowed: bool
    credential_ref: Optional[str] = None
    credential_secret_ref_id: Optional[str] = None
    cost_mode: str
    limit_shape: dict[str, Any]
    supported_models: list[str]
    task_strengths: list[str]
    risk_tier_max: int
    status: str
    effective_status: str
    auth_method: str = "api_key"
    connection_status: str = "not_connected"
    connection_ref: Optional[str] = None
    oauth_scopes: list[str] = Field(default_factory=list)
    connection_metadata: dict[str, Any] = Field(default_factory=dict)
    policy_notes: list[str]
    observed_capacity: dict[str, Any]
    runtime_supported: bool
    created_at: str
    updated_at: str


class EntitlementSourceListResponse(BaseModel):
    project_id: str
    items: list[EntitlementSourceSummary]


class EntitlementSourceUpsertRequest(BaseModel):
    project_id: str
    provider: str
    source_id: str
    source_kind: str
    entitlement_kind: str
    owner_type: str
    owner_id: str
    workload_classes_allowed: list[str] = Field(default_factory=list)
    multi_tenant_allowed: bool = False
    credential_ref: Optional[str] = None
    credential_secret_ref_id: Optional[str] = None
    cost_mode: str = "usage_based_api"
    limit_shape: dict[str, Any] = Field(default_factory=dict)
    supported_models: list[str] = Field(default_factory=list)
    task_strengths: list[str] = Field(default_factory=list)
    risk_tier_max: int = 1
    status: str = "declared"
    auth_method: str = "api_key"
    connection_status: str = "not_connected"
    connection_ref: Optional[str] = None
    oauth_scopes: list[str] = Field(default_factory=list)
    connection_metadata: dict[str, Any] = Field(default_factory=dict)
    policy_notes: list[str] = Field(default_factory=list)
    observed_capacity: dict[str, Any] = Field(default_factory=dict)
    runtime_supported: bool = False
    actor: str = "system"


class EntitlementSourceConnectRequest(BaseModel):
    project_id: str
    connection_ref: str
    auth_method: Optional[str] = None
    oauth_scopes: list[str] = Field(default_factory=list)
    connection_metadata: dict[str, Any] = Field(default_factory=dict)
    observed_capacity: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"


class EntitlementSourceDisconnectRequest(BaseModel):
    project_id: str
    reason: Optional[str] = None
    actor: str = "system"


class EntitlementConnectionSessionSummary(BaseModel):
    id: str
    project_id: str
    source_id: str
    provider: str
    auth_method: str
    connection_mode: str
    status: str
    requested_scopes: list[str]
    authorization_url: Optional[str] = None
    device_code: Optional[str] = None
    user_code: Optional[str] = None
    verification_uri: Optional[str] = None
    login_command: Optional[str] = None
    connection_ref: Optional[str] = None
    instructions: dict[str, Any]
    metadata: dict[str, Any]
    error: dict[str, Any]
    requested_by: str
    completed_by: Optional[str] = None
    expires_at: Optional[str] = None
    created_at: str
    updated_at: str
    completed_at: Optional[str] = None


class EntitlementConnectionSessionListResponse(BaseModel):
    project_id: str
    items: list[EntitlementConnectionSessionSummary]


class EntitlementConnectionSessionCreateRequest(BaseModel):
    project_id: str
    connection_mode: Optional[str] = None
    requested_scopes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"


class EntitlementConnectionSessionCompleteRequest(BaseModel):
    project_id: str
    connection_ref: str
    oauth_scopes: list[str] = Field(default_factory=list)
    connection_metadata: dict[str, Any] = Field(default_factory=dict)
    observed_capacity: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"


class EntitlementConnectionSessionCompleteResponse(BaseModel):
    project_id: str
    session: EntitlementConnectionSessionSummary
    source: EntitlementSourceSummary


class EntitlementPoolSummary(BaseModel):
    id: str
    project_id: str
    provider: str
    source_id: str
    source_kind: str
    access_class: str
    auth_method: str
    connection_status: str
    effective_status: str
    owner_type: str
    owner_id: str
    allowed_workload_classes: list[str]
    routing_modes: list[str]
    capacity_state: str
    cooldown_until: Optional[str] = None
    multi_tenant_allowed: bool
    budget_policy_ref: str
    runtime_supported: bool
    supported_models: list[str]
    task_strengths: list[str]
    risk_tier_max: int
    observed_capacity: dict[str, Any]
    policy_notes: list[str]
    updated_at: Optional[str] = None


class EntitlementPoolListResponse(BaseModel):
    project_id: str
    items: list[EntitlementPoolSummary]


class CapacityReceiptSummary(BaseModel):
    id: str
    project_id: str
    entitlement_pool_id: str
    source_id: str
    provider: str
    capacity_kind: str
    model_route_decision_id: Optional[str] = None
    provider_ref: Optional[str] = None
    usage_units: dict[str, Any]
    capacity_state_after: str
    dedupe_key: str
    status: str
    observed_capacity: dict[str, Any]
    created_by: str
    created_at: str


class EntitlementPoolProbeRequest(BaseModel):
    project_id: str
    observed_capacity: dict[str, Any]
    usage_units: dict[str, Any] = Field(default_factory=dict)
    provider_ref: Optional[str] = None
    model_route_decision_id: Optional[str] = None
    dedupe_key: Optional[str] = None
    status: str = "recorded"
    actor: str = "system"


class EntitlementPoolProbeResponse(BaseModel):
    project_id: str
    pool: EntitlementPoolSummary
    receipt: CapacityReceiptSummary
    duplicate: bool


class RoutingPreviewRequest(BaseModel):
    project_id: str
    skill_id: str
    task_kind: str
    workload_class: str
    risk_tier: int = Field(default=1, ge=0, le=3)
    data_boundary: DataBoundary = "internal"
    preferred_capabilities: list[str] = Field(default_factory=list)
    budget_state: Optional[str] = None
    actor: str = "system"


class RoutingPreviewResponse(BaseModel):
    route_decision_log_id: str
    skill_id: str
    task_kind: str
    workload_class: str
    risk_tier: int
    data_boundary: DataBoundary = "internal"
    budget_state: str
    source_id: Optional[str] = None
    provider: Optional[str] = None
    model_id: Optional[str] = None
    denial_reason: Optional[str] = None
    rejected_sources: list[dict[str, Any]]
    decision: dict[str, Any]


class RoutingLogItem(BaseModel):
    id: str
    project_id: str
    skill_id: Optional[str] = None
    task_kind: str
    workload_class: str
    risk_tier: int
    data_boundary: DataBoundary = "internal"
    budget_state: str
    selected_source_id: Optional[str] = None
    selected_provider: Optional[str] = None
    selected_model_id: Optional[str] = None
    denial_reason: Optional[str] = None
    rejected_sources: list[dict[str, Any]]
    decision: dict[str, Any]
    created_at: str


class RoutingLogListResponse(BaseModel):
    project_id: str
    items: list[RoutingLogItem]


class ModelRouteDecisionSummary(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    skill_id: Optional[str] = None
    task_kind: str
    workload_class: str
    risk_tier: int
    selected_entitlement_pool_id: Optional[str] = None
    selected_source_id: Optional[str] = None
    selected_provider: Optional[str] = None
    selected_model_id: Optional[str] = None
    routing_mode: str
    rejected_sources: list[dict[str, Any]]
    budget_state: str
    data_boundary: DataBoundary = "internal"
    capacity_snapshot: dict[str, Any]
    policy_result: dict[str, Any]
    created_at: str


class ModelRouteDecisionListResponse(BaseModel):
    project_id: str
    items: list[ModelRouteDecisionSummary]


class ModelRouteDecisionSimulateRequest(RoutingPreviewRequest):
    pass


class ModelRouteDecisionSimulateResponse(BaseModel):
    project_id: str
    decision: ModelRouteDecisionSummary


class ModelCredentialBindRequest(BaseModel):
    project_id: str
    secret_ref_id: str
    actor: str = "system"


class ModelCredentialUploadRequest(BaseModel):
    project_id: str
    value: str = Field(min_length=1)
    backend: Literal["platform_secret_store", "vault", "local_runner"] = (
        "platform_secret_store"
    )
    actor: str = "system"


class ModelCredentialWriteSummary(BaseModel):
    source: EntitlementSourceSummary
    secret_ref_id: str
    backend: str
    fingerprint: Optional[str] = None
    last4: Optional[str] = None
    raw_secret_stored: Literal[False] = False


class ModelGatewayLeaseSummary(BaseModel):
    id: str
    project_id: str
    job_id: str
    agent_id: str
    worker_id: str
    route_decision_id: str
    entitlement_source_id: str
    entitlement_pool_id: str
    source_id: str
    provider: str
    model_id: Optional[str] = None
    auth_method: str
    credential_secret_ref_id: Optional[str] = None
    consumer_owner_id: Optional[str] = None
    fencing_token: int
    status: str
    estimated_cost_usd_micros: int
    budget_reservation_id: Optional[str] = None
    allocation_id: Optional[str] = None
    max_usage: dict[str, Any]
    dedupe_key: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    completed_at: Optional[str] = None
    result: dict[str, Any]


class ModelGatewayLeaseAcquireRequest(BaseModel):
    project_id: str
    job_id: str
    worker_id: str
    job_lease_token: str
    agent_id: str
    skill_id: str
    preferred_capabilities: list[str] = Field(default_factory=list)
    risk_tier: int = Field(default=1, ge=0, le=3)
    consumer_owner_id: Optional[str] = None
    estimated_cost_usd_micros: int = Field(default=0, ge=0)
    max_usage: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str = Field(min_length=1, max_length=250)
    ttl_seconds: int = Field(default=300, ge=30, le=3600)


class ModelGatewayLeaseAcquireResponse(BaseModel):
    lease: ModelGatewayLeaseSummary
    lease_token: Optional[str] = None
    duplicate: bool


class ModelGatewayLeaseListResponse(BaseModel):
    project_id: str
    items: list[ModelGatewayLeaseSummary]


class ModelGatewayLeaseHeartbeatRequest(BaseModel):
    project_id: str
    worker_id: str
    lease_token: str
    job_lease_token: str
    ttl_seconds: int = Field(default=300, ge=30, le=3600)


class ModelGatewayInvocationStartRequest(BaseModel):
    project_id: str
    worker_id: str
    lease_token: str
    job_lease_token: str
    request_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    dedupe_key: str = Field(min_length=1, max_length=250)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelGatewayInvocationSummary(BaseModel):
    id: str
    project_id: str
    model_gateway_lease_id: str
    job_id: str
    agent_id: str
    receipt_id: str
    request_hash: str
    dedupe_key: str
    status: str
    provider_ref: Optional[str] = None
    usage_units: dict[str, Any]
    actual_cost_usd_micros: Optional[int] = None
    metadata: dict[str, Any]
    error: dict[str, Any]
    started_at: str
    completed_at: Optional[str] = None


class ModelGatewayInvocationStartResponse(BaseModel):
    invocation: ModelGatewayInvocationSummary
    duplicate: bool


class ModelGatewayInvocationListResponse(BaseModel):
    project_id: str
    items: list[ModelGatewayInvocationSummary]


class ModelGatewayInvocationSettleRequest(BaseModel):
    project_id: str
    worker_id: str
    lease_token: str
    job_lease_token: str
    succeeded: bool
    provider_ref: Optional[str] = None
    usage_units: dict[str, Any] = Field(default_factory=dict)
    actual_cost_usd_micros: Optional[int] = Field(default=None, ge=0)
    observed_capacity: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)


class ModelGatewayLeaseReleaseRequest(BaseModel):
    project_id: str
    worker_id: str
    lease_token: str
    job_lease_token: str
    reason: str = Field(min_length=1, max_length=500)


class ModelGatewayLeaseReapRequest(BaseModel):
    project_id: str
    actor: str = "system"


class ModelGatewayLeaseReapResponse(BaseModel):
    project_id: str
    reaped: int


class ModelGatewayUsageItem(BaseModel):
    engine: str
    model: Optional[str] = None
    calls: int
    successful_calls: int
    failed_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd_micros: int
    first_call_at: Optional[str] = None
    last_call_at: Optional[str] = None


class ModelGatewayUsageResponse(BaseModel):
    project_id: str
    window_days: int
    totals: dict[str, int]
    items: list[ModelGatewayUsageItem]


class GitHubConnectionSummary(BaseModel):
    id: str
    project_id: str
    connection_label: str
    auth_method: Literal["github_app", "personal_access_token"]
    credential_configured: bool
    app_id: Optional[str] = None
    installation_id: Optional[str] = None
    api_base_url: str
    status: Literal["configured", "active", "error", "disabled", "revoked"]
    required_permissions: list[str] = Field(default_factory=list)
    granted_permissions: list[str] = Field(default_factory=list)
    missing_permissions: list[str] = Field(default_factory=list)
    account_login: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    last_test_status: Optional[str] = None
    last_tested_at: Optional[str] = None
    last_error: Optional[dict[str, Any]] = None
    disabled_at: Optional[str] = None
    disabled_by: Optional[str] = None
    revoked_at: Optional[str] = None
    revoked_by: Optional[str] = None
    created_by: str
    created_at: str
    updated_at: str


class GitHubConnectionListResponse(BaseModel):
    project_id: str
    items: list[GitHubConnectionSummary]


class GitHubConnectionCreateRequest(BaseModel):
    project_id: str
    connection_label: str
    auth_method: Literal["github_app", "personal_access_token"]
    credential_ref: str
    app_id: Optional[str] = None
    installation_id: Optional[str] = None
    api_base_url: str = "https://api.github.com"
    required_permissions: list[str] = Field(default_factory=lambda: ["pull"])
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_connection(self) -> "GitHubConnectionCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.connection_label = _require_text(
            self.connection_label, "connection_label"
        )
        self.credential_ref = _require_text(self.credential_ref, "credential_ref")
        self.api_base_url = _require_text(self.api_base_url, "api_base_url")
        self.required_permissions = _require_non_empty_string_list(
            self.required_permissions, "required_permissions"
        )
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubConnectionLifecycleRequest(BaseModel):
    project_id: str
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_connection_lifecycle(
        self,
    ) -> "GitHubConnectionLifecycleRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubRepositoryBindingSummary(BaseModel):
    id: str
    project_id: str
    connection_id: str
    repository_id: Optional[int] = None
    owner: str
    name: str
    full_name: str
    role: Literal["primary", "source", "upstream", "mirror"]
    default_branch: Optional[str] = None
    visibility: Optional[str] = None
    archived: bool
    status: Literal["active", "disabled"]
    permissions: dict[str, bool] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    last_synced_at: Optional[str] = None
    created_by: str
    created_at: str
    updated_at: str


class GitHubRepositoryBindingListResponse(BaseModel):
    project_id: str
    items: list[GitHubRepositoryBindingSummary]


class GitHubRepositoryBindingCreateRequest(BaseModel):
    project_id: str
    connection_id: str
    repository: str
    role: Literal["primary", "source", "upstream", "mirror"] = "primary"
    default_branch: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_repository_binding(
        self,
    ) -> "GitHubRepositoryBindingCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.connection_id = _require_text(self.connection_id, "connection_id")
        self.repository = _require_text(self.repository, "repository")
        if self.default_branch is not None:
            self.default_branch = _require_text(
                self.default_branch, "default_branch"
            )
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubRepositoryBindingUpdateRequest(BaseModel):
    project_id: str
    role: Optional[Literal["primary", "source", "upstream", "mirror"]] = None
    status: Optional[Literal["active", "disabled"]] = None
    default_branch: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_repository_binding_update(
        self,
    ) -> "GitHubRepositoryBindingUpdateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        if self.default_branch is not None:
            self.default_branch = _require_text(
                self.default_branch, "default_branch"
            )
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubConnectionTestRequest(BaseModel):
    project_id: str
    repository_binding_id: str
    dedupe_key: str
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_connection_test(self) -> "GitHubConnectionTestRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.repository_binding_id = _require_text(
            self.repository_binding_id, "repository_binding_id"
        )
        self.dedupe_key = _require_text(self.dedupe_key, "dedupe_key")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubConnectionTestResponse(BaseModel):
    status: Literal["passed", "failed"]
    duplicate: bool
    receipt_id: str
    connection: GitHubConnectionSummary
    repository_binding: GitHubRepositoryBindingSummary
    detail: dict[str, Any] = Field(default_factory=dict)


class GitHubEasyConnectRequest(BaseModel):
    """One-shot project GitHub connect using env SecretRef + optional repo link."""

    project_id: str
    connection_label: str = "Project GitHub"
    auth_method: Literal["github_app", "personal_access_token"] = (
        "personal_access_token"
    )
    env_credential_key: str = "BOSS_GITHUB_TOKEN"
    #: THE TOKEN ITSELF, for the setup wizard.
    #:
    #: "Operators never paste raw tokens into BOSS" was a fine rule for a platform whose
    #: operators also own the server's environment. It is the wrong rule for self-hosted open
    #: source: the first thing a new user is asked to do becomes "set an environment variable on
    #: the API container and restart it", which is not onboarding, it is a support ticket.
    #:
    #: The token still never lands in a column. It is written THROUGH to the configured secret
    #: backend by put_secret and only the reference is recorded — the same treatment every model
    #: credential already gets. Absent, the env path is unchanged.
    token: Optional[str] = None
    app_id: Optional[str] = None
    installation_id: Optional[str] = None
    api_base_url: str = "https://api.github.com"
    required_permissions: list[str] = Field(
        default_factory=lambda: ["pull", "push", "triage"]
    )
    repository: Optional[str] = None
    role: Literal["primary", "source", "upstream", "mirror"] = "primary"
    test_after_link: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_easy_connect(self) -> "GitHubEasyConnectRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.connection_label = _require_text(
            self.connection_label, "connection_label"
        )
        if self.token is not None:
            self.token = self.token.strip()
            if not self.token:
                raise ValueError("token was empty.")
        self.env_credential_key = _require_text(
            self.env_credential_key, "env_credential_key"
        ).upper()
        self.api_base_url = _require_text(self.api_base_url, "api_base_url")
        self.required_permissions = _require_non_empty_string_list(
            self.required_permissions, "required_permissions"
        )
        if self.repository is not None and self.repository.strip():
            self.repository = _require_text(self.repository, "repository")
        else:
            self.repository = None
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubEasyConnectResponse(BaseModel):
    connection: GitHubConnectionSummary
    repository_binding: Optional[GitHubRepositoryBindingSummary] = None
    test: Optional[GitHubConnectionTestResponse] = None
    credential_ref: str


class GitHubAccessibleRepository(BaseModel):
    full_name: str
    owner: str
    name: str
    repository_id: Optional[int] = None
    default_branch: Optional[str] = None
    visibility: Optional[str] = None
    archived: bool = False
    permissions: dict[str, bool] = Field(default_factory=dict)
    html_url: Optional[str] = None


class GitHubAccessibleRepositoryListResponse(BaseModel):
    project_id: str
    connection_id: str
    items: list[GitHubAccessibleRepository]


class GitHubBranchPolicySummary(BaseModel):
    id: str
    project_id: str
    repository_binding_id: str
    version: int
    supersedes_policy_id: Optional[str] = None
    status: Literal["active", "superseded", "disabled"]
    protected_ref_patterns: list[str]
    allowed_branch_prefixes: list[str]
    branch_name_pattern: str
    allowed_update_methods: list[str]
    allowed_merge_methods: list[str]
    required_checks: list[str]
    required_approvals: int
    require_pull_request: bool
    require_linear_history: bool
    require_signed_commits: bool
    automation_updates_enabled: bool
    force_push_allowed: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: str
    updated_at: str


class GitHubBranchPolicyListResponse(BaseModel):
    project_id: str
    items: list[GitHubBranchPolicySummary]


class GitHubBranchPolicyCreateRequest(BaseModel):
    project_id: str
    repository_binding_id: str
    protected_ref_patterns: list[str] = Field(
        default_factory=lambda: ["main", "master", "release/*"]
    )
    allowed_branch_prefixes: list[str] = Field(
        default_factory=lambda: [
            "ai/",
            "feat/",
            "fix/",
            "chore/",
            "docs/",
            "release/",
        ]
    )
    branch_name_pattern: str = r"^[A-Za-z0-9._/-]+$"
    allowed_update_methods: list[str] = Field(
        default_factory=lambda: ["fast_forward", "merge_commit", "rebase"]
    )
    allowed_merge_methods: list[str] = Field(
        default_factory=lambda: ["merge", "squash", "rebase"]
    )
    required_checks: list[str] = Field(default_factory=list)
    required_approvals: int = 1
    require_pull_request: bool = True
    require_linear_history: bool = False
    require_signed_commits: bool = False
    automation_updates_enabled: bool = True
    force_push_allowed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_branch_policy(self) -> "GitHubBranchPolicyCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.repository_binding_id = _require_text(
            self.repository_binding_id, "repository_binding_id"
        )
        self.branch_name_pattern = _require_text(
            self.branch_name_pattern, "branch_name_pattern"
        )
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubBranchPolicyUpdateRequest(BaseModel):
    project_id: str
    status: Optional[Literal["active", "disabled"]] = None
    protected_ref_patterns: Optional[list[str]] = None
    allowed_branch_prefixes: Optional[list[str]] = None
    branch_name_pattern: Optional[str] = None
    allowed_update_methods: Optional[list[str]] = None
    allowed_merge_methods: Optional[list[str]] = None
    required_checks: Optional[list[str]] = None
    required_approvals: Optional[int] = None
    require_pull_request: Optional[bool] = None
    require_linear_history: Optional[bool] = None
    require_signed_commits: Optional[bool] = None
    automation_updates_enabled: Optional[bool] = None
    force_push_allowed: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = None
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_branch_policy_update(
        self,
    ) -> "GitHubBranchPolicyUpdateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        if self.branch_name_pattern is not None:
            self.branch_name_pattern = _require_text(
                self.branch_name_pattern, "branch_name_pattern"
            )
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubBranchPolicyEvaluateRequest(BaseModel):
    project_id: str
    ref: str
    operation: Literal["observe", "create", "update", "merge", "delete"]
    method: Optional[str] = None

    @model_validator(mode="after")
    def validate_github_branch_policy_evaluate(
        self,
    ) -> "GitHubBranchPolicyEvaluateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.ref = _require_text(self.ref, "ref")
        if self.method is not None:
            self.method = _require_text(self.method, "method")
        return self


class GitHubBranchPolicyEvaluationResponse(BaseModel):
    policy_id: str
    policy_version: int
    status: Literal["allowed", "approval_required", "blocked"]
    ref: str
    operation: str
    method: Optional[str] = None
    protected: bool
    reasons: list[str]
    required_checks: list[str]
    required_approvals: int
    require_pull_request: bool
    require_linear_history: bool
    require_signed_commits: bool


class GitHubBranchSnapshotSummary(BaseModel):
    id: str
    project_id: str
    repository_binding_id: str
    branch_policy_id: Optional[str] = None
    branch_policy_version: Optional[int] = None
    source_ref: str
    target_ref: str
    source_commit_sha: Optional[str] = None
    source_tree_sha: Optional[str] = None
    target_commit_sha: Optional[str] = None
    target_tree_sha: Optional[str] = None
    merge_base_commit_sha: Optional[str] = None
    merge_base_tree_sha: Optional[str] = None
    ahead_by: int
    behind_by: int
    topology_classification: Literal[
        "equal",
        "tree_equivalent_rewrite",
        "fast_forward",
        "behind",
        "diverged",
        "missing",
    ]
    classification: Literal[
        "equal",
        "tree_equivalent_rewrite",
        "fast_forward",
        "behind",
        "diverged",
        "missing",
        "protected_blocked",
    ]
    refs: dict[str, Any] = Field(default_factory=dict)
    comparison: dict[str, Any] = Field(default_factory=dict)
    protection: dict[str, Any] = Field(default_factory=dict)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    policy_evaluation: dict[str, Any] = Field(default_factory=dict)
    receipt_id: str
    dedupe_key: str
    observed_by: str
    observed_at: str
    created_at: str


class GitHubBranchSnapshotListResponse(BaseModel):
    project_id: str
    items: list[GitHubBranchSnapshotSummary]


class GitHubBranchSnapshotCaptureRequest(BaseModel):
    project_id: str
    repository_binding_id: str
    source_ref: str
    target_ref: str
    dedupe_key: str
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_branch_snapshot_capture(
        self,
    ) -> "GitHubBranchSnapshotCaptureRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.repository_binding_id = _require_text(
            self.repository_binding_id, "repository_binding_id"
        )
        self.source_ref = _require_text(self.source_ref, "source_ref")
        self.target_ref = _require_text(self.target_ref, "target_ref")
        self.dedupe_key = _require_text(self.dedupe_key, "dedupe_key")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubBranchSnapshotCaptureResponse(BaseModel):
    status: Literal["succeeded", "failed"]
    duplicate: bool
    receipt_id: str
    snapshot: Optional[GitHubBranchSnapshotSummary] = None
    error: Optional[dict[str, Any]] = None


class GitHubBranchLeaseSummary(BaseModel):
    id: str
    project_id: str
    repository_binding_id: str
    ref_name: str
    owner_kind: Literal[
        "task", "workflow_run", "code_task_run", "agent", "manual"
    ]
    owner_id: str
    run_id: Optional[str] = None
    worktree_path: Optional[str] = None
    acquire_key: str
    fencing_token: int
    status: Literal["active", "released", "expired", "reclaimed"]
    acquired_by: str
    acquired_at: str
    heartbeat_at: str
    lease_expires_at: str
    released_by: Optional[str] = None
    released_at: Optional[str] = None
    release_reason: Optional[str] = None
    reclaimed_from_lease_id: Optional[str] = None
    created_at: str
    updated_at: str


class GitHubBranchLeaseListResponse(BaseModel):
    project_id: str
    items: list[GitHubBranchLeaseSummary]


class GitHubBranchLeaseAcquireRequest(BaseModel):
    project_id: str
    repository_binding_id: str
    ref_name: str
    owner_kind: Literal[
        "task", "workflow_run", "code_task_run", "agent", "manual"
    ]
    owner_id: str
    run_id: Optional[str] = None
    worktree_path: Optional[str] = None
    acquire_key: str
    ttl_seconds: int = Field(default=300, ge=30, le=3600)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_branch_lease_acquire(
        self,
    ) -> "GitHubBranchLeaseAcquireRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.repository_binding_id = _require_text(
            self.repository_binding_id, "repository_binding_id"
        )
        self.ref_name = _require_text(self.ref_name, "ref_name")
        self.owner_id = _require_text(self.owner_id, "owner_id")
        self.acquire_key = _require_text(self.acquire_key, "acquire_key")
        if self.run_id is not None:
            self.run_id = _require_text(self.run_id, "run_id")
        if self.worktree_path is not None:
            self.worktree_path = _require_text(
                self.worktree_path, "worktree_path"
            )
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubBranchLeaseAcquireResponse(BaseModel):
    lease: GitHubBranchLeaseSummary
    lease_token: Optional[str] = None
    duplicate: bool


class GitHubBranchLeaseHeartbeatRequest(BaseModel):
    project_id: str
    lease_token: str
    fencing_token: int
    ttl_seconds: int = Field(default=300, ge=30, le=3600)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_github_branch_lease_heartbeat(
        self,
    ) -> "GitHubBranchLeaseHeartbeatRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.lease_token = _require_text(self.lease_token, "lease_token")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubBranchLeaseReleaseRequest(BaseModel):
    project_id: str
    lease_token: str
    fencing_token: int
    reason: str
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_branch_lease_release(
        self,
    ) -> "GitHubBranchLeaseReleaseRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.lease_token = _require_text(self.lease_token, "lease_token")
        self.reason = _require_text(self.reason, "reason")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubBranchLeaseReclaimRequest(BaseModel):
    project_id: str
    reason: str
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_branch_lease_reclaim(
        self,
    ) -> "GitHubBranchLeaseReclaimRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.reason = _require_text(self.reason, "reason")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubCodeSyncRunSummary(BaseModel):
    id: str
    project_id: str
    repository_binding_id: str
    branch_policy_id: str
    branch_policy_version: int
    initial_snapshot_id: str
    result_snapshot_id: Optional[str] = None
    branch_lease_id: Optional[str] = None
    fencing_token: Optional[int] = None
    source_ref: str
    target_ref: str
    topology_classification: Literal[
        "equal",
        "tree_equivalent_rewrite",
        "fast_forward",
        "behind",
        "diverged",
        "missing",
    ]
    action: Literal[
        "noop",
        "preserve_remote_identity",
        "fast_forward_target",
        "fast_forward_source",
        "create_resolution_branch",
        "blocked",
    ]
    status: Literal[
        "planned",
        "pending_approval",
        "approved",
        "applying",
        "succeeded",
        "noop",
        "conflicted",
        "blocked",
        "failed",
        "deadlettered",
    ]
    dedupe_key: str
    changeset_id: Optional[str] = None
    conflict_set_id: Optional[str] = None
    decision_id: Optional[str] = None
    resolution_branch: Optional[str] = None
    rollback_ref: Optional[str] = None
    rollback_sha: Optional[str] = None
    apply_attempts: int
    receipt_ids: list[str] = Field(default_factory=list)
    resource_receipt_ids: list[str] = Field(default_factory=list)
    error: Optional[dict[str, Any]] = None
    created_by: str
    approved_by: Optional[str] = None
    applied_by: Optional[str] = None
    created_at: str
    updated_at: str
    completed_at: Optional[str] = None


class GitHubCodeSyncRunListResponse(BaseModel):
    project_id: str
    items: list[GitHubCodeSyncRunSummary]


class GitHubCodeSyncPlanRequest(BaseModel):
    project_id: str
    repository_binding_id: str
    snapshot_id: str
    dedupe_key: str
    resolution_branch: Optional[str] = None
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_code_sync_plan(self) -> "GitHubCodeSyncPlanRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.repository_binding_id = _require_text(
            self.repository_binding_id, "repository_binding_id"
        )
        self.snapshot_id = _require_text(self.snapshot_id, "snapshot_id")
        self.dedupe_key = _require_text(self.dedupe_key, "dedupe_key")
        if self.resolution_branch is not None:
            self.resolution_branch = _require_text(
                self.resolution_branch, "resolution_branch"
            )
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubCodeSyncApplyRequest(BaseModel):
    project_id: str
    lease_id: str
    lease_token: str
    fencing_token: int = Field(ge=1)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_code_sync_apply(self) -> "GitHubCodeSyncApplyRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.lease_id = _require_text(self.lease_id, "lease_id")
        self.lease_token = _require_text(self.lease_token, "lease_token")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubConflictSetSummary(BaseModel):
    id: str
    project_id: str
    code_sync_run_id: str
    repository_binding_id: str
    resolution_branch: str
    resolution_branch_head_sha: str
    base_commit_sha: Optional[str] = None
    base_tree_sha: Optional[str] = None
    ours_commit_sha: str
    ours_tree_sha: str
    theirs_commit_sha: str
    theirs_tree_sha: str
    status: Literal[
        "open", "resolution_proposed", "approved", "resolved", "blocked"
    ]
    files: list[dict[str, Any]] = Field(default_factory=list)
    conflict_count: int
    decision_id: Optional[str] = None
    resolved_by_plan_id: Optional[str] = None
    created_at: str
    updated_at: str
    resolved_at: Optional[str] = None


class GitHubConflictSetListResponse(BaseModel):
    project_id: str
    items: list[GitHubConflictSetSummary]


class GitHubResolutionPlanSummary(BaseModel):
    id: str
    project_id: str
    conflict_set_id: str
    version: int
    status: Literal["proposed", "approved", "applied", "rejected", "failed"]
    resolved_files: list[dict[str, Any]] = Field(default_factory=list)
    validation_evidence: dict[str, Any] = Field(default_factory=dict)
    independent_review: dict[str, Any] = Field(default_factory=dict)
    decision_id: Optional[str] = None
    changeset_id: Optional[str] = None
    result_commit_sha: Optional[str] = None
    created_by: str
    approved_by: Optional[str] = None
    applied_by: Optional[str] = None
    created_at: str
    updated_at: str
    applied_at: Optional[str] = None


class GitHubResolutionPlanListResponse(BaseModel):
    project_id: str
    items: list[GitHubResolutionPlanSummary]


class GitHubResolutionPlanCreateRequest(BaseModel):
    project_id: str
    resolved_files: list[dict[str, Any]]
    validation_evidence: dict[str, Any]
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_resolution_plan_create(
        self,
    ) -> "GitHubResolutionPlanCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubResolutionPlanApproveRequest(BaseModel):
    project_id: str
    review_evidence: dict[str, Any]
    actor: str

    @model_validator(mode="after")
    def validate_github_resolution_plan_approve(
        self,
    ) -> "GitHubResolutionPlanApproveRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubResolutionPlanApplyRequest(BaseModel):
    project_id: str
    lease_id: str
    lease_token: str
    fencing_token: int = Field(ge=1)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_resolution_plan_apply(
        self,
    ) -> "GitHubResolutionPlanApplyRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.lease_id = _require_text(self.lease_id, "lease_id")
        self.lease_token = _require_text(self.lease_token, "lease_token")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubResolutionApplyResponse(BaseModel):
    plan: GitHubResolutionPlanSummary
    conflict_set: GitHubConflictSetSummary
    sync_run: GitHubCodeSyncRunSummary


class GitHubMergeAuthorizationSummary(BaseModel):
    id: str
    project_id: str
    repository_binding_id: str
    branch_policy_id: str
    branch_policy_version: int
    code_sync_run_id: Optional[str] = None
    resolution_plan_id: Optional[str] = None
    pull_request_number: int
    pull_request_url: str
    source_ref: str
    target_ref: str
    head_commit_sha: str
    head_tree_sha: str
    base_commit_sha: str
    base_tree_sha: str
    merge_method: Literal["merge", "squash", "rebase"]
    status: Literal[
        "pending_approval",
        "authorized",
        "queued",
        "applying",
        "merged",
        "blocked",
        "expired",
        "failed",
        "deadlettered",
        "cancelled",
    ]
    dedupe_key: str
    required_checks: list[str] = Field(default_factory=list)
    observed_checks: list[dict[str, Any]] = Field(default_factory=list)
    required_approvals: int
    observed_reviews: list[dict[str, Any]] = Field(default_factory=list)
    evidence_bundle: dict[str, Any] = Field(default_factory=dict)
    policy_evaluation: dict[str, Any] = Field(default_factory=dict)
    authorized_by: Optional[str] = None
    authorized_at: Optional[str] = None
    expires_at: str
    queue_position: int
    queued_at: Optional[str] = None
    branch_lease_id: Optional[str] = None
    fencing_token: Optional[int] = None
    changeset_id: str
    apply_attempts: int
    receipt_ids: list[str] = Field(default_factory=list)
    resource_receipt_ids: list[str] = Field(default_factory=list)
    result_merge_sha: Optional[str] = None
    result_target_tree_sha: Optional[str] = None
    rollback_plan: dict[str, Any] = Field(default_factory=dict)
    error: Optional[dict[str, Any]] = None
    created_by: str
    applied_by: Optional[str] = None
    created_at: str
    updated_at: str
    merged_at: Optional[str] = None


class GitHubMergeAuthorizationListResponse(BaseModel):
    project_id: str
    items: list[GitHubMergeAuthorizationSummary]


class GitHubMergeAuthorizationCreateRequest(BaseModel):
    project_id: str
    repository_binding_id: str
    pull_request_number: int = Field(ge=1)
    source_ref: str
    target_ref: str
    merge_method: Literal["merge", "squash", "rebase"] = "merge"
    dedupe_key: str
    evidence_bundle: dict[str, Any]
    expires_in_seconds: int = Field(default=3600, ge=60, le=86400)
    code_sync_run_id: Optional[str] = None
    resolution_plan_id: Optional[str] = None
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_merge_authorization_create(
        self,
    ) -> "GitHubMergeAuthorizationCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.repository_binding_id = _require_text(
            self.repository_binding_id, "repository_binding_id"
        )
        self.source_ref = _require_text(self.source_ref, "source_ref")
        self.target_ref = _require_text(self.target_ref, "target_ref")
        self.dedupe_key = _require_text(self.dedupe_key, "dedupe_key")
        if self.code_sync_run_id is not None:
            self.code_sync_run_id = _require_text(
                self.code_sync_run_id, "code_sync_run_id"
            )
        if self.resolution_plan_id is not None:
            self.resolution_plan_id = _require_text(
                self.resolution_plan_id, "resolution_plan_id"
            )
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubMergeAuthorizationApproveRequest(BaseModel):
    project_id: str
    review_evidence: dict[str, Any]
    actor: str

    @model_validator(mode="after")
    def validate_github_merge_authorization_approve(
        self,
    ) -> "GitHubMergeAuthorizationApproveRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubMergeAuthorizationEnqueueRequest(BaseModel):
    project_id: str
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_merge_authorization_enqueue(
        self,
    ) -> "GitHubMergeAuthorizationEnqueueRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubMergeAuthorizationApplyRequest(BaseModel):
    project_id: str
    lease_id: str
    lease_token: str
    fencing_token: int = Field(ge=1)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_merge_authorization_apply(
        self,
    ) -> "GitHubMergeAuthorizationApplyRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.lease_id = _require_text(self.lease_id, "lease_id")
        self.lease_token = _require_text(self.lease_token, "lease_token")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubReconciliationRunSummary(BaseModel):
    id: str
    project_id: str
    repository_binding_id: str
    trigger_signal_id: Optional[str] = None
    repair_of_run_id: Optional[str] = None
    source: Literal["webhook", "poll", "repair"]
    status: Literal[
        "queued",
        "running",
        "succeeded",
        "failed",
        "deadlettered",
        "cancelled",
    ]
    dedupe_key: str
    requested_refs: list[str] = Field(default_factory=list)
    attempt_count: int
    max_attempts: int
    job_id: Optional[str] = None
    observation_ids: list[str] = Field(default_factory=list)
    receipt_ids: list[str] = Field(default_factory=list)
    rate_limit_status: dict[str, Any] = Field(default_factory=dict)
    next_attempt_at: Optional[str] = None
    deadletter_id: Optional[str] = None
    error: Optional[dict[str, Any]] = None
    created_by: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str
    updated_at: str


class GitHubReconciliationRunListResponse(BaseModel):
    project_id: str
    items: list[GitHubReconciliationRunSummary]


class GitHubReconciliationSignalSummary(BaseModel):
    id: str
    project_id: str
    repository_binding_id: str
    inbound_webhook_id: Optional[str] = None
    source: Literal["webhook", "poll", "repair"]
    delivery_id: Optional[str] = None
    provider_event: Optional[str] = None
    action: Optional[str] = None
    ref_name: str
    expected_commit_sha: Optional[str] = None
    expected_deleted: bool
    payload_hash: Optional[str] = None
    dedupe_key: str
    status: Literal[
        "pending",
        "processing",
        "applied",
        "stale",
        "failed",
        "deadlettered",
    ]
    reconciliation_run_id: Optional[str] = None
    observed_commit_sha: Optional[str] = None
    error: Optional[dict[str, Any]] = None
    provider_occurred_at: Optional[str] = None
    processed_at: Optional[str] = None
    created_at: str
    updated_at: str


class GitHubReconciliationSignalListResponse(BaseModel):
    project_id: str
    items: list[GitHubReconciliationSignalSummary]


class GitHubRepositoryRefStateSummary(BaseModel):
    id: str
    project_id: str
    repository_binding_id: str
    ref_name: str
    exists: bool
    commit_sha: Optional[str] = None
    tree_sha: Optional[str] = None
    protected: bool
    protection: dict[str, Any] = Field(default_factory=dict)
    latest_observation_id: str
    latest_signal_id: Optional[str] = None
    latest_receipt_id: str
    observed_at: str
    created_at: str
    updated_at: str


class GitHubRepositoryRefStateListResponse(BaseModel):
    project_id: str
    items: list[GitHubRepositoryRefStateSummary]


class GitHubReconciliationPollRequest(BaseModel):
    project_id: str
    repository_binding_id: Optional[str] = None
    refs: Optional[list[str]] = None
    dedupe_key: str
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_reconciliation_poll(
        self,
    ) -> "GitHubReconciliationPollRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        if self.repository_binding_id is not None:
            self.repository_binding_id = _require_text(
                self.repository_binding_id, "repository_binding_id"
            )
        self.dedupe_key = _require_text(self.dedupe_key, "dedupe_key")
        self.actor = _require_text(self.actor, "actor")
        return self


class GitHubReconciliationPollResponse(BaseModel):
    project_id: str
    items: list[GitHubReconciliationRunSummary]


class GitHubReconciliationExecuteRequest(BaseModel):
    project_id: str
    job_id: str
    worker_id: str
    lease_token: str

    @model_validator(mode="after")
    def validate_github_reconciliation_execute(
        self,
    ) -> "GitHubReconciliationExecuteRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.job_id = _require_text(self.job_id, "job_id")
        self.worker_id = _require_text(self.worker_id, "worker_id")
        self.lease_token = _require_text(self.lease_token, "lease_token")
        return self


class GitHubReconciliationExecuteResponse(BaseModel):
    run: GitHubReconciliationRunSummary
    job: dict[str, Any]


class GitHubReconciliationRepairRequest(BaseModel):
    project_id: str
    dedupe_key: str
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_github_reconciliation_repair(
        self,
    ) -> "GitHubReconciliationRepairRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.dedupe_key = _require_text(self.dedupe_key, "dedupe_key")
        self.actor = _require_text(self.actor, "actor")
        return self


class ProviderAccountSummary(BaseModel):
    id: str
    project_id: str
    provider: str
    account_label: str
    auth_method: str
    credential_ref: Optional[str] = None
    credential_refs: dict[str, str] = Field(default_factory=dict)
    connection_metadata: dict[str, Any] = Field(default_factory=dict)
    scopes: list[str]
    status: str
    owner_actor: Optional[str] = None
    credential_version: int = 1
    credential_rotated_at: Optional[str] = None
    disabled_at: Optional[str] = None
    disabled_by: Optional[str] = None
    revoked_at: Optional[str] = None
    revoked_by: Optional[str] = None
    read_only: bool
    deploy_capable: bool
    billing_readable: bool
    destructive_actions_disabled: bool
    capabilities: list[str]
    missing_permissions: list[str]
    last_test_status: Optional[str] = None
    last_tested_at: Optional[str] = None
    last_error: Optional[dict[str, Any]] = None
    created_by: str
    created_at: str
    updated_at: str


class ProviderAccountListResponse(BaseModel):
    project_id: str
    items: list[ProviderAccountSummary]


class ProviderAccountCreateRequest(BaseModel):
    project_id: str
    provider: str
    account_label: str
    auth_method: Optional[str] = None
    credential_ref: Optional[str] = None
    credential_refs: dict[str, str] = Field(default_factory=dict)
    connection_metadata: dict[str, Any] = Field(default_factory=dict)
    scopes: list[str] = Field(default_factory=list)
    read_only: bool = True
    deploy_capable: bool = False
    billing_readable: bool = False
    destructive_actions_disabled: bool = True
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_provider_account(self) -> "ProviderAccountCreateRequest":
        self.provider = _require_text(self.provider, "provider").lower()
        self.account_label = _require_text(self.account_label, "account_label")
        self.actor = _require_text(self.actor, "actor")
        return self


class ProviderAccountUpdateRequest(BaseModel):
    project_id: str
    account_label: Optional[str] = None
    scopes: Optional[list[str]] = None
    read_only: Optional[bool] = None
    deploy_capable: Optional[bool] = None
    billing_readable: Optional[bool] = None
    destructive_actions_disabled: Optional[bool] = None
    connection_metadata: Optional[dict[str, Any]] = None
    owner_actor: Optional[str] = None
    actor: str = "ceo"


class ProviderAccountLifecycleRequest(BaseModel):
    project_id: str
    actor: str = "ceo"


class ProviderCredentialRotateRequest(BaseModel):
    project_id: str
    credential_ref: Optional[str] = None
    credential_refs: dict[str, str] = Field(default_factory=dict)
    overlap_minutes: int = Field(default=15, ge=0, le=1440)
    actor: str = "ceo"


class ProviderCredentialRotationResponse(BaseModel):
    provider_account: ProviderAccountSummary
    prior_secret_refs: list[SecretRefSummary]
    active_secret_refs: list[SecretRefSummary]
    overlap_expires_at: str


class ProviderCredentialSlotSummary(BaseModel):
    key: str
    secret_kind: str
    required: bool
    default_ref: Optional[str] = None


class ProviderAuthMethodSummary(BaseModel):
    key: str
    runtime_status: str
    credential_slots: list[ProviderCredentialSlotSummary]


class ProviderDefinitionSummary(BaseModel):
    provider: str
    display_name: str
    category: str
    adapter_status: str
    default_auth_method: str
    auth_methods: list[ProviderAuthMethodSummary]
    capabilities: list[str]


class ProviderDefinitionListResponse(BaseModel):
    items: list[ProviderDefinitionSummary]


class ProviderConnectionTestResponse(BaseModel):
    status: str
    provider_account: ProviderAccountSummary
    provider_health: "ProviderHealthSummary"
    detail: dict[str, Any] = Field(default_factory=dict)


class ResourceSyncRequest(BaseModel):
    project_id: str
    provider_account_id: Optional[str] = None
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_sync_request(self) -> "ResourceSyncRequest":
        self.actor = _require_text(self.actor, "actor")
        return self


class ResourceSyncRunSummary(BaseModel):
    id: str
    status: str
    provider: Optional[str] = None
    provider_account_id: Optional[str] = None
    resources_seen: int
    resources_changed: int
    costs_seen: int = 0
    billing_status: Optional[str] = None
    billing_message: Optional[str] = None
    provider_health_id: Optional[str] = None
    drift_report_id: Optional[str] = None
    deadletter_id: Optional[str] = None
    inventory_snapshot_artifact_id: Optional[str] = None
    error: Optional[dict[str, Any]] = None


class ResourceSyncResponse(BaseModel):
    project_id: str
    runs: list[ResourceSyncRunSummary]
    resources_seen: int
    resources_changed: int
    costs_seen: int = 0


class ExternalResourceSummary(BaseModel):
    id: str
    project_id: str
    provider_account_id: Optional[str] = None
    provider: str
    resource_type: str
    external_id: str
    name: str
    state: str
    ownership: str
    environment: str
    owner_role: Optional[str] = None
    task_id: Optional[str] = None
    deployment_target_id: Optional[str] = None
    receipt_id: Optional[str] = None
    budget_allocation_id: Optional[str] = None
    health_status: str
    estimated_monthly_usd_micros: Optional[int] = None
    actual_monthly_usd_micros: Optional[int] = None
    cost_confidence: str
    last_seen_at: str
    tags: list[str]
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class ExternalResourceListResponse(BaseModel):
    project_id: str
    items: list[ExternalResourceSummary]
    limit: int
    offset: int


class ResourceTopologyEdgeSummary(BaseModel):
    id: str
    project_id: str
    from_resource_id: str
    to_resource_id: str
    relation_type: str
    provider: Optional[str] = None
    metadata: dict[str, Any] = {}
    created_at: str
    updated_at: str


class ResourceTopologyEdgeUpsertRequest(BaseModel):
    project_id: str
    from_resource_id: str
    to_resource_id: str
    relation_type: str
    provider: Optional[str] = None
    metadata: dict[str, Any] = {}
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_edge_request(self) -> "ResourceTopologyEdgeUpsertRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.from_resource_id = _require_text(
            self.from_resource_id, "from_resource_id"
        )
        self.to_resource_id = _require_text(self.to_resource_id, "to_resource_id")
        self.relation_type = _require_text(self.relation_type, "relation_type").lower()
        self.actor = _require_text(self.actor, "actor")
        if self.from_resource_id == self.to_resource_id:
            raise ValueError("from_resource_id and to_resource_id must differ.")
        return self


class ResourceTopologyEdgeMutationResponse(BaseModel):
    edge: ResourceTopologyEdgeSummary
    receipt_id: str
    deleted: bool = False


class ResourceTopologyNodeSummary(BaseModel):
    id: str
    project_id: str
    provider: str
    resource_type: str
    external_id: str
    name: str
    state: Optional[str] = None
    ownership: Optional[str] = None
    environment: Optional[str] = None
    health_status: Optional[str] = None
    tags: list[str] = []
    metadata: dict[str, Any] = {}


class ResourceTopologyGraphResponse(BaseModel):
    project_id: str
    nodes: list[ResourceTopologyNodeSummary]
    edges: list[ResourceTopologyEdgeSummary]
    node_count: int
    edge_count: int
    relation_counts: dict[str, int] = {}


class ResourceTopologyInferRequest(BaseModel):
    project_id: str
    provider: Optional[str] = None
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_infer_request(self) -> "ResourceTopologyInferRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        if self.provider is not None:
            self.provider = _require_text(self.provider, "provider").lower()
        return self


class ResourceTopologyInferResponse(BaseModel):
    project_id: str
    resources_scanned: int
    edges_upserted: int
    skipped: int
    edges: list[ResourceTopologyEdgeSummary]


class CostEventSummary(BaseModel):
    id: str
    project_id: str
    provider_account_id: Optional[str] = None
    external_resource_id: Optional[str] = None
    billing_account_id: Optional[str] = None
    task_id: Optional[str] = None
    role_key: Optional[str] = None
    provider: str
    category: str
    amount_usd_micros: int
    cost_layer: str
    confidence: str
    source_ref: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    receipt_id: Optional[str] = None
    metadata: dict[str, Any]
    created_at: str


class CostEventListResponse(BaseModel):
    project_id: str
    items: list[CostEventSummary]
    limit: int
    offset: int


class CostAnomalySummary(BaseModel):
    id: str
    project_id: str
    provider_account_id: Optional[str] = None
    cost_event_id: Optional[str] = None
    decision_id: Optional[str] = None
    provider: str
    anomaly_type: str
    severity: str
    status: str
    amount_usd_micros: int
    threshold_usd_micros: int
    summary: str
    recommended_actions: list[dict[str, Any]]
    created_at: str
    resolved_at: Optional[str] = None


class CostAnomalyListResponse(BaseModel):
    project_id: str
    items: list[CostAnomalySummary]
    limit: int
    offset: int


class ProviderHealthSummary(BaseModel):
    id: str
    project_id: str
    provider_account_id: Optional[str] = None
    provider: str
    status: str
    api_available: bool
    rate_limit_status: str
    usage_cap_status: str
    last_checked_at: str
    detail: dict[str, Any]
    created_at: str


class ProviderHealthListResponse(BaseModel):
    project_id: str
    items: list[ProviderHealthSummary]
    limit: int
    offset: int


class DriftReportSummary(BaseModel):
    id: str
    project_id: str
    provider_account_id: Optional[str] = None
    classification: str
    summary: str
    unmanaged_count: int
    orphaned_count: int
    security_impacting_count: int
    production_impacting_count: int
    recommendations: list[dict[str, Any]]
    detail: dict[str, Any] = {}
    created_at: str


class DriftReportListResponse(BaseModel):
    project_id: str
    items: list[DriftReportSummary]
    limit: int
    offset: int


class ReconciliationRunSummary(BaseModel):
    id: str
    project_id: str
    provider_account_id: Optional[str] = None
    provider: Optional[str] = None
    status: str
    resources_seen: int
    resources_changed: int
    costs_seen: int
    started_by: str
    started_at: str
    finished_at: Optional[str] = None
    error: Optional[dict[str, Any]] = None
    metadata: dict[str, Any]


class ReconciliationRunListResponse(BaseModel):
    project_id: str
    items: list[ReconciliationRunSummary]
    limit: int
    offset: int


class DeploymentTargetSummary(BaseModel):
    id: str
    project_id: str
    app_name: str
    repo: str
    environment: str
    provider_account_id: Optional[str] = None
    target_kind: str
    target_ref: str
    #: WHICH HOST THIS APP IS BOUND TO — absent from this model entirely until now.
    #:
    #: `host_id` is the fact everything downstream depends on: the ssh credential resolves
    #: through it, the host panel counts apps by it, and a deploy without it is refused. It was
    #: not serialised here, so the console could not show where an app was bound and neither
    #: could the agent — and reading the field back as `None` was indistinguishable from an app
    #: that genuinely had no host. That misread cost a diagnosis in this very session: two
    #: targets looked unbound while the host panel, querying the column directly, listed one of
    #: them as bound to it.
    host_id: Optional[str] = None
    #: A stack target's (target_kind 'stack') repository manifest and public address — the
    #: canonical columns POST /v1/deployments/stack/targets writes. None on every other kind.
    manifest_path: Optional[str] = None
    public_url: Optional[str] = None
    domains: list[str]
    required_resources: list[dict[str, Any]]
    # Existing deployment targets record a binding (for example,
    # {"name": "ssh_key", "secret_ref_id": "sref_..."}), while the public
    # create endpoint and web console also support a bare secret ref such as
    # "env:OPENAI_API_KEY". Preserve both durable representations.
    secret_refs: list[str | dict[str, Any]]
    health_checks: list[dict[str, Any]]
    rollback_points: list[dict[str, Any]]
    status: str
    created_by: str
    created_at: str
    updated_at: str


class DeploymentTargetListResponse(BaseModel):
    project_id: str
    items: list[DeploymentTargetSummary]
    limit: int
    offset: int


class DeploymentTargetCreateRequest(BaseModel):
    project_id: str
    app_name: str
    repo: str
    environment: str
    provider_account_id: Optional[str] = None
    target_kind: str
    target_ref: str
    domains: list[str] = Field(default_factory=list)
    required_resources: list[dict[str, Any]] = Field(default_factory=list)
    secret_refs: list[str | dict[str, Any]] = Field(default_factory=list)
    health_checks: list[dict[str, Any]] = Field(default_factory=list)
    rollback_points: list[dict[str, Any]] = Field(default_factory=list)
    actor: str = "ceo"


class DeploymentEnvironmentPolicySummary(BaseModel):
    id: str
    project_id: str
    environment: str
    runtime_mode: str
    allowed_target_kinds: list[str]
    requires_change_set: bool
    requires_budget_reservation: bool
    requires_approval: bool
    requires_health_check: bool
    requires_rollback_plan: bool
    rollout_notes: Optional[str] = None
    status: str
    created_by: str
    metadata: dict[str, Any]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DeploymentEnvironmentPolicyListResponse(BaseModel):
    project_id: str
    items: list[DeploymentEnvironmentPolicySummary]
    limit: int
    offset: int


class DeploymentEnvironmentPolicyUpsertRequest(BaseModel):
    project_id: str
    environment: str
    runtime_mode: str = "metadata_only"
    allowed_target_kinds: list[str] = Field(default_factory=list)
    requires_change_set: bool = True
    requires_budget_reservation: bool = True
    requires_approval: bool = True
    requires_health_check: bool = True
    requires_rollback_plan: bool = True
    rollout_notes: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "ceo"

    @model_validator(mode="after")
    def validate_deployment_environment_policy(
        self,
    ) -> "DeploymentEnvironmentPolicyUpsertRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.environment = _require_text(self.environment, "environment")
        self.runtime_mode = _require_text(self.runtime_mode, "runtime_mode")
        self.actor = _require_text(self.actor, "actor")
        return self


class DeploymentRunSummary(BaseModel):
    id: str
    project_id: str
    deployment_target_id: str
    changeset_id: Optional[str] = None
    decision_id: Optional[str] = None
    release_artifact_id: Optional[str] = None
    health_artifact_id: Optional[str] = None
    rollback_artifact_id: Optional[str] = None
    app_name: str
    environment: str
    repo: str
    target_kind: str
    target_ref: str
    commit_ref: Optional[str] = None
    status: str
    release_packet: dict[str, Any]
    # Either shape, because both are in the table. Older rows hold a LIST of check results;
    # rows written by the connector hold the observe() verdict — {"healthy": bool, "checks":
    # [...]} — which carries the overall answer alongside the individual probes.
    #
    # Declared narrowly as `list`, this 500'd the endpoint the moment the connector wrote its
    # first real deployment. That is the third response model today describing a shape its
    # writer had stopped producing: the schema and the writer are edited in different files, and
    # nothing fails until something reads it.
    health_check_results: Union[list[dict[str, Any]], dict[str, Any]]
    rollback_plan: list[str]
    release_receipt_ids: list[dict[str, Any]]
    created_by: str
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    failure_reason: Optional[str] = None


class DeploymentRunListResponse(BaseModel):
    project_id: str
    items: list[DeploymentRunSummary]


class DeploymentRunCreateRequest(BaseModel):
    project_id: str
    deployment_target_id: str
    commit_ref: Optional[str] = None
    release_packet: dict[str, Any] = Field(default_factory=dict)
    expected_cost_usd_micros: int = Field(default=0, ge=0)
    reservation_id: Optional[str] = None
    actor: str = "release_manager"

    @model_validator(mode="after")
    def validate_deployment_run_create(self) -> "DeploymentRunCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.deployment_target_id = _require_text(
            self.deployment_target_id,
            "deployment_target_id",
        )
        self.actor = _require_text(self.actor, "actor")
        return self


class DeploymentRunHealthCheckRequest(BaseModel):
    actor: str = "deployment_manager"

    @model_validator(mode="after")
    def validate_deployment_run_health_check(
        self,
    ) -> "DeploymentRunHealthCheckRequest":
        self.actor = _require_text(self.actor, "actor")
        return self


class DeploymentRunHealthCheckResponse(BaseModel):
    deployment_run: DeploymentRunSummary
    health_artifact_id: str
    rollback_artifact_id: Optional[str] = None
    decision_id: Optional[str] = None


class DeploymentPromotionStartRequest(BaseModel):
    project_id: str
    staging_target_id: str
    production_target_id: str
    commit_ref: str
    staging_reservation_id: Optional[str] = None
    production_reservation_id: Optional[str] = None
    staging_expected_cost_usd_micros: int = Field(default=0, ge=0)
    production_expected_cost_usd_micros: int = Field(default=0, ge=0)
    release_packet: dict[str, Any] = Field(default_factory=dict)
    actor: str = "release_manager"

    @model_validator(mode="after")
    def validate_deployment_promotion_start(self) -> "DeploymentPromotionStartRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.staging_target_id = _require_text(
            self.staging_target_id, "staging_target_id"
        )
        self.production_target_id = _require_text(
            self.production_target_id, "production_target_id"
        )
        self.commit_ref = _require_text(self.commit_ref, "commit_ref")
        self.actor = _require_text(self.actor, "actor")
        return self


class DeploymentPromotionAdvanceRequest(BaseModel):
    actor: str = "release_manager"

    @model_validator(mode="after")
    def validate_deployment_promotion_advance(
        self,
    ) -> "DeploymentPromotionAdvanceRequest":
        self.actor = _require_text(self.actor, "actor")
        return self


class DeploymentPromotionSummary(BaseModel):
    id: str
    project_id: str
    app_name: str
    repo: str
    commit_ref: str
    staging_target_id: str
    production_target_id: str
    staging_deployment_run_id: Optional[str] = None
    production_deployment_run_id: Optional[str] = None
    production_rollout_id: Optional[str] = None
    pipeline_provider: str
    pipeline_descriptor: dict[str, Any]
    pipeline_verdict: dict[str, Any]
    gate_result: dict[str, Any]
    status: str
    failure_reason: Optional[str] = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    staging_deployment_run: Optional[DeploymentRunSummary] = None
    production_deployment_run: Optional[DeploymentRunSummary] = None
    production_rollout: Optional[DeploymentRolloutSummary] = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)


class DeploymentPromotionListResponse(BaseModel):
    project_id: str
    items: list[DeploymentPromotionSummary]


class ChangeSetOperationRequest(BaseModel):
    skill_id: str
    action: str
    resource_type: str
    resource_ref: Optional[str] = None
    desired_state: dict[str, Any] = Field(default_factory=dict)
    risk_tier: int = Field(default=2, ge=0, le=3)
    destructive: bool = False
    production_affecting: bool = False


class ChangeSetSummary(BaseModel):
    id: str
    project_id: str
    provider_account_id: Optional[str] = None
    title: str
    summary: str
    status: str
    risk_tier: int
    operations: list[dict[str, Any]]
    expected_cost_usd_micros: int
    cost_quote: dict[str, Any]
    reservation_id: Optional[str] = None
    approval_decision_id: Optional[str] = None
    rollback_plan: list[str]
    expected_receipts: list[dict[str, Any]]
    policy_result: dict[str, Any]
    created_by: str
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    applied_by: Optional[str] = None
    applied_at: Optional[str] = None
    failure_reason: Optional[str] = None
    created_at: str
    updated_at: str


class ChangeSetListResponse(BaseModel):
    project_id: str
    items: list[ChangeSetSummary]


class ChangeSetCreateRequest(BaseModel):
    project_id: str
    provider_account_id: Optional[str] = None
    title: str
    summary: str
    operations: list[ChangeSetOperationRequest]
    expected_cost_usd_micros: int = Field(default=0, ge=0)
    reservation_id: Optional[str] = None
    rollback_plan: list[str] = Field(default_factory=list)
    risk_tier: int = Field(default=2, ge=0, le=3)
    approval_required: bool = True
    actor: str = "ceo"


class ChangeSetApproveRequest(BaseModel):
    actor: str = "ceo"


class ChangeSetApplyRequest(BaseModel):
    actor: str = "ceo"
    created_by_job_id: Optional[str] = None
    dry_run: bool = False


class ResourceReceiptSummary(BaseModel):
    id: str
    project_id: str
    changeset_id: Optional[str] = None
    operation_id: Optional[str] = None
    provider_account_id: Optional[str] = None
    receipt_id: Optional[str] = None
    dedupe_key: str
    provider: str
    action: str
    resource_type: str
    resource_ref: Optional[str] = None
    status: str
    provider_ref: Optional[str] = None
    request_hash: str
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    created_by_job_id: Optional[str] = None
    created_at: str
    updated_at: str


class ResourceReceiptListResponse(BaseModel):
    project_id: str
    items: list[ResourceReceiptSummary]
    limit: int
    offset: int


class WorkerPoolSummary(BaseModel):
    id: str
    project_id: str
    pool_key: str
    kind: str
    provider_account_id: Optional[str] = None
    provider: Optional[str] = None
    status: str
    current_workers: int
    desired_workers: int
    min_workers: int
    max_workers: int
    max_queue_depth_per_worker: int
    cost_per_worker_hour_usd_micros: int
    capabilities: list[str]
    metadata: dict[str, Any]
    created_by: str
    created_at: str
    updated_at: str


class WorkerPoolListResponse(BaseModel):
    project_id: str
    items: list[WorkerPoolSummary]


class WorkerPoolUpsertRequest(BaseModel):
    project_id: str
    pool_key: str
    kind: str = "local"
    provider_account_id: Optional[str] = None
    current_workers: int = Field(default=0, ge=0)
    desired_workers: int = Field(default=0, ge=0)
    min_workers: int = Field(default=0, ge=0)
    max_workers: int = Field(default=1, ge=0)
    max_queue_depth_per_worker: int = Field(default=5, ge=1)
    cost_per_worker_hour_usd_micros: int = Field(default=0, ge=0)
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "providerops_manager"

    @model_validator(mode="after")
    def validate_worker_pool(self) -> "WorkerPoolUpsertRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.pool_key = _require_text(self.pool_key, "pool_key")
        self.kind = _require_text(self.kind, "kind").lower()
        self.actor = _require_text(self.actor, "actor")
        if self.kind not in {"local", "cloud"}:
            raise ValueError("kind must be local or cloud.")
        if self.min_workers > self.max_workers:
            raise ValueError("min_workers cannot exceed max_workers.")
        if self.desired_workers > self.max_workers:
            raise ValueError("desired_workers cannot exceed max_workers.")
        return self


class ScalePlanSummary(BaseModel):
    id: str
    project_id: str
    worker_pool_id: Optional[str] = None
    status: str
    trigger_reason: str
    queue_depth: int
    running_jobs: int
    leased_jobs: int
    retryable_jobs: int
    current_workers: int
    desired_workers_before: int
    desired_workers_after: int
    target_workers: int
    budget_state: str
    budget_available_usd_micros: int
    estimated_hourly_delta_usd_micros: int
    provider_health_status: Optional[str] = None
    actions: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]
    changeset_id: Optional[str] = None
    decision_id: Optional[str] = None
    created_by: str
    created_at: str
    applied_at: Optional[str] = None


class ScalePlanListResponse(BaseModel):
    project_id: str
    items: list[ScalePlanSummary]


class ScalePlanRunRequest(BaseModel):
    project_id: str
    worker_pool_id: Optional[str] = None
    cloud_spillover_enabled: bool = False
    actor: str = "providerops_manager"

    @model_validator(mode="after")
    def validate_scale_plan_request(self) -> "ScalePlanRunRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class ScalePlanRunResponse(BaseModel):
    scale_plan: ScalePlanSummary
    worker_pool: WorkerPoolSummary
    budget_state: str
    queue: dict[str, int]
    provider_health: dict[str, Any]
    changeset_id: Optional[str] = None
    decision_id: Optional[str] = None


class ResourceJanitorRunRequest(BaseModel):
    project_id: str
    provider_account_id: Optional[str] = None
    max_candidates: int = Field(default=10, ge=1, le=100)
    actor: str = "resource_janitor"

    @model_validator(mode="after")
    def validate_janitor_request(self) -> "ResourceJanitorRunRequest":
        self.actor = _require_text(self.actor, "actor")
        return self


class ResourceJanitorCandidateSummary(BaseModel):
    external_resource_id: str
    provider_account_id: Optional[str] = None
    provider: str
    resource_type: str
    external_id: str
    name: str
    environment: str
    ownership: str
    health_status: str
    estimated_monthly_savings_usd_micros: int
    cost_confidence: str
    cleanup_skill_id: str
    destructive: bool


class ResourceJanitorSkippedSummary(BaseModel):
    external_resource_id: str
    provider: str
    resource_type: Optional[str] = None
    external_id: str
    reason: str


class ResourceJanitorRunResponse(BaseModel):
    status: str
    project_id: str
    artifact_id: str
    candidate_count: int
    skipped_count: int
    estimated_monthly_savings_usd_micros: int
    candidates: list[ResourceJanitorCandidateSummary]
    skipped: list[ResourceJanitorSkippedSummary]
    changesets: list[ChangeSetSummary]


class ChangeSetApplyResponse(BaseModel):
    status: str
    changeset: ChangeSetSummary
    resource_receipts: list[ResourceReceiptSummary]
    expected_receipts: Optional[list[dict[str, Any]]] = None
    decision_id: Optional[str] = None


class TerraformExportRequest(BaseModel):
    actor: str = "ceo"
    # Audited escape hatch: export even when no deployment target opted in.
    force: bool = False
    # Optional, explicit repo mirror of the projection (never automatic).
    commit_to_repo: bool = False
    repo_branch: Optional[str] = None


class TerraformExportMappingEntry(BaseModel):
    operation_index: int
    provider: str
    resource_ref: Optional[str] = None
    reused: bool
    tf_address: str
    tf_type: str


class TerraformExportResponse(BaseModel):
    artifact_id: str
    changeset_id: str
    project_id: str
    content_hash: str
    tf_json: dict[str, Any]
    canonical: str
    warnings: list[dict[str, Any]]
    mapping: list[TerraformExportMappingEntry]
    providers: list[str]
    forced: bool = False
    opt_in_targets: int = 0
    commit: Optional[dict[str, Any]] = None


class TerraformExportArtifactResponse(BaseModel):
    artifact_id: str
    changeset_id: str
    project_id: str
    content_hash: Optional[str] = None
    created_at: Optional[str] = None
    export: dict[str, Any]


class TerraformDriftReconcileRequest(BaseModel):
    actor: str = "ceo"
    provider_account_id: Optional[str] = None
    # Exactly one of tf_json (.tf.json dialect) or plan_json
    # (`terraform show -json` / plan JSON) must be supplied.
    tf_json: Optional[dict[str, Any]] = None
    plan_json: Optional[dict[str, Any]] = None
    # dry_run computes and returns the comparison WITHOUT persisting a drift
    # report row or a proposed ChangeSet.
    dry_run: bool = False
    # Audited escape hatch mirroring terraform-export's opt-in gate.
    force: bool = False


class TerraformDriftReconcileResponse(BaseModel):
    project_id: str
    input_format: str
    dry_run: bool
    drift_report_id: Optional[str] = None
    # The PROPOSED corrective ChangeSet (normal dual-control gate); never
    # auto-approved, absent when the .tf did not diverge.
    changeset_id: Optional[str] = None
    counts: dict[str, int]
    resources: list[dict[str, Any]]
    resource_refs: list[str]
    warnings: list[dict[str, Any]]
    forced: bool = False
    opt_in_targets: int = 0


class IacVerifyRequest(BaseModel):
    """Operator-triggered IaC verification lane (fmt/validate/lint/security/plan-dry).

    ``files`` is a path→content map written into a disposable workdir. Paths must
    be relative and may not escape the workdir. Cloud credentials are scrubbed
    from the lane subprocess environment (BOSS-67 / spec 05 §3.2).
    """

    files: dict[str, str] = Field(default_factory=dict)
    actor: str = "operator"
    project_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_iac_verify_request(self) -> "IacVerifyRequest":
        self.actor = _require_text(self.actor, "actor")
        if not isinstance(self.files, dict) or not self.files:
            raise ValueError("files must be a non-empty path→content map.")
        cleaned: dict[str, str] = {}
        for raw_path, content in self.files.items():
            path = str(raw_path or "").strip().replace("\\", "/")
            if not path or path.startswith("/") or path.startswith("~"):
                raise ValueError(
                    f"files path must be a relative workspace path (got {raw_path!r})."
                )
            parts = [part for part in path.split("/") if part not in ("", ".")]
            if any(part == ".." for part in parts):
                raise ValueError(f"files path must not contain '..' (got {raw_path!r}).")
            if not parts:
                raise ValueError(f"files path must be a relative workspace path (got {raw_path!r}).")
            cleaned["/".join(parts)] = "" if content is None else str(content)
        self.files = cleaned
        return self


class IacVerifyStepSummary(BaseModel):
    step: str
    ok: bool
    output: str = ""
    duration_ms: int = 0
    skipped: bool = False
    reason: Optional[str] = None
    note: Optional[str] = None


class IacVerifyResponse(BaseModel):
    ok: bool
    tf_binary: Optional[str] = None
    scanner: Optional[str] = None
    files: list[str] = Field(default_factory=list)
    steps: list[IacVerifyStepSummary] = Field(default_factory=list)
    actor: str = "operator"
    project_id: Optional[str] = None


class IacDetectRequest(BaseModel):
    """Detect whether a file map / task description is an IaC change (no tools run)."""

    files: Optional[dict[str, str]] = None
    file_paths: Optional[list[str]] = None
    title: Optional[str] = None
    instructions: Optional[str] = None
    acceptance_criteria: Optional[list[str]] = None
    task_type: Optional[str] = None

    @model_validator(mode="after")
    def validate_iac_detect_request(self) -> "IacDetectRequest":
        has_files = isinstance(self.files, dict) and bool(self.files)
        has_paths = isinstance(self.file_paths, list) and bool(self.file_paths)
        has_task = any(
            [
                (self.title or "").strip(),
                (self.instructions or "").strip(),
                bool(self.acceptance_criteria),
                (self.task_type or "").strip(),
            ]
        )
        if not (has_files or has_paths or has_task):
            raise ValueError(
                "Provide files, file_paths, and/or task title/instructions/task_type."
            )
        return self


class IacDetectResponse(BaseModel):
    is_iac_task: bool
    iac_files: list[str] = Field(default_factory=list)
    file_count: int = 0
    task_type: Optional[str] = None


class PdfRenderRequest(BaseModel):
    """Markdown → PDF control-plane render (reportlab, no system deps)."""

    markdown: str = ""
    title: Optional[str] = None
    subtitle: Optional[str] = None
    as_base64: bool = True

    @model_validator(mode="after")
    def validate_pdf_render_request(self) -> "PdfRenderRequest":
        if not (self.markdown or "").strip() and not (self.title or "").strip():
            raise ValueError("markdown or title is required.")
        return self


class PdfRenderResponse(BaseModel):
    content_type: str = "application/pdf"
    page_count: int = 1
    byte_length: int = 0
    content_base64: Optional[str] = None
    title: Optional[str] = None


class OncallPreviewRequest(BaseModel):
    """Format an oncall investigation card without posting it."""

    incident_id: str
    alert: dict[str, Any] = Field(default_factory=dict)
    hypothesis: Optional[dict[str, Any]] = None
    autonomy: Optional[dict[str, Any]] = None
    remediation: Optional[dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_oncall_preview(self) -> "OncallPreviewRequest":
        self.incident_id = _require_text(self.incident_id, "incident_id")
        if not isinstance(self.alert, dict):
            raise ValueError("alert must be an object.")
        return self


class OncallNotifyRequest(BaseModel):
    """Post an oncall card to the configured webhook (receipted external effect)."""

    project_id: str
    actor: str = "operator"
    incident_id: str
    alert: dict[str, Any] = Field(default_factory=dict)
    hypothesis: Optional[dict[str, Any]] = None
    autonomy: Optional[dict[str, Any]] = None
    remediation: Optional[dict[str, Any]] = None
    dedupe_key: Optional[str] = None

    @model_validator(mode="after")
    def validate_oncall_notify(self) -> "OncallNotifyRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        self.incident_id = _require_text(self.incident_id, "incident_id")
        if not isinstance(self.alert, dict):
            raise ValueError("alert must be an object.")
        return self


class MultiCloudSkillDescribeRequest(BaseModel):
    """Describe multi-cloud skill(s) from the product catalog + Resource Plane registry."""

    skill_id: Optional[str] = None
    skill_ids: Optional[list[str]] = None

    @model_validator(mode="after")
    def validate_skill_describe(self) -> "MultiCloudSkillDescribeRequest":
        ids: list[str] = []
        if self.skill_id and str(self.skill_id).strip():
            ids.append(str(self.skill_id).strip())
        for item in self.skill_ids or []:
            cleaned = str(item or "").strip()
            if cleaned and cleaned not in ids:
                ids.append(cleaned)
        if not ids:
            raise ValueError("skill_id or skill_ids is required.")
        self.skill_ids = ids
        return self


AuditExportResponse.model_rebuild()
OpsQueueInspectionResponse.model_rebuild()
ProviderConnectionTestResponse.model_rebuild()


# ── AI SRE reliability pipeline (RB-3/RB-4/RB-5) ─────────────────────────────
class ReliabilityWindow(BaseModel):
    start: float
    end: float


class ReliabilityScanRequest(BaseModel):
    project_id: str
    actor: str = "system"
    source: dict[str, Any] = Field(default_factory=lambda: {"kind": "memory"})
    window: ReliabilityWindow
    query: str = ""
    baseline: Optional[dict[str, float]] = None
    source_id: Optional[str] = None
    persist: bool = False

    @model_validator(mode="after")
    def _validate(self) -> "ReliabilityScanRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class ReliabilityScanResponse(BaseModel):
    events: int
    distinct_templates: int
    reduction_ratio: Optional[float] = None
    top_templates: list[dict[str, Any]] = Field(default_factory=list)
    signals: list[dict[str, Any]] = Field(default_factory=list)
    signal_count: int = 0
    incidents: list[dict[str, Any]] = Field(default_factory=list)


class ReliabilityIncidentSummary(BaseModel):
    id: str
    dedupe_key: str
    title: str
    service: str
    severity: str
    status: str
    confidence: float
    signal_count: int
    first_seen: datetime
    last_seen: datetime


class ReliabilityIncidentListResponse(BaseModel):
    project_id: str
    items: list[ReliabilityIncidentSummary] = Field(default_factory=list)


class ReliabilityIncidentDetail(ReliabilityIncidentSummary):
    signals: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    remediations: list[dict[str, Any]] = Field(default_factory=list)


class ReliabilityDiagnoseRequest(BaseModel):
    project_id: str
    actor: str = "system"

    @model_validator(mode="after")
    def _validate(self) -> "ReliabilityDiagnoseRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class ReliabilityDiagnoseResponse(BaseModel):
    incident_id: str
    service: str
    hypothesis: dict[str, Any]                                  # the top-ranked (winner)
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)   # full ranked list w/ confidence
    changes: list[dict[str, Any]] = Field(default_factory=list)      # correlated recent changes (RB-17)
    context: dict[str, Any] = Field(default_factory=dict)
    known_causes: list[dict[str, Any]] = Field(default_factory=list)


class ReliabilityFeedbackRequest(BaseModel):
    project_id: str
    actor: str = "system"
    verdict: str                                    # real | false_alarm | fix_good | fix_bad
    cause: Optional[dict[str, Any]] = None          # {kind,key,name,confidence,evidence} for verdict=real

    @model_validator(mode="after")
    def _validate(self) -> "ReliabilityFeedbackRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        self.verdict = _require_text(self.verdict, "verdict")
        return self


class ReliabilityFeedbackResponse(BaseModel):
    incident_id: str
    verdict: str
    outcome: str
    status: str
    learning_example_id: str
    tuning: dict[str, Any] = Field(default_factory=dict)


class ReliabilityRemediateRequest(BaseModel):
    project_id: str
    actor: str = "system"
    owner: Optional[str] = None
    repo: Optional[str] = None
    base_branch: str = "main"

    @model_validator(mode="after")
    def _validate(self) -> "ReliabilityRemediateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class ReliabilityRemediateResponse(BaseModel):
    remediation_id: str
    incident_id: str
    code_task_run_id: str
    status: str


class ReliabilityVerifyRequest(BaseModel):
    project_id: str
    actor: str = "system"
    source: dict[str, Any] = Field(default_factory=lambda: {"kind": "memory"})
    window: ReliabilityWindow
    query: str = ""
    stability_minutes: float = 30.0

    @model_validator(mode="after")
    def _validate(self) -> "ReliabilityVerifyRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class ReliabilityVerifyResponse(BaseModel):
    incident_id: str
    state: str                                   # recurred | stabilizing | recovered
    status: str                                  # the incident status after this check
    checks: int
    recurred_signatures: list[str] = Field(default_factory=list)
    first_clear: Optional[str] = None
    stable_at: Optional[str] = None


class ReliabilitySourceRequest(BaseModel):
    project_id: str
    actor: str = "system"
    kind: str                                    # prometheus | loki | grafana | log_export | memory
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str = ""
    enabled: bool = True

    @model_validator(mode="after")
    def _validate(self) -> "ReliabilitySourceRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        self.kind = _require_text(self.kind, "kind")
        self.name = _require_text(self.name, "name")
        return self


class ReliabilitySourceSummary(BaseModel):
    id: str
    kind: str
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    created_at: datetime


class ReliabilitySourceListResponse(BaseModel):
    project_id: str
    items: list[ReliabilitySourceSummary] = Field(default_factory=list)


class ReliabilityGraphWalkRequest(BaseModel):
    project_id: str
    actor: str = "system"
    walker: str                                  # kubernetes | infra | cooccurrence
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> "ReliabilityGraphWalkRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        self.walker = _require_text(self.walker, "walker")
        return self


class ReliabilityGraphWalkResponse(BaseModel):
    walker: str
    counts: dict[str, int] = Field(default_factory=dict)


class ReliabilityGraphResponse(BaseModel):
    project_id: str
    root: Optional[dict[str, Any]] = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


class ReliabilityPolicyRequest(BaseModel):
    project_id: str
    actor: str = "system"
    auto_remediate: bool = False
    min_confidence: float = 0.8
    max_blast_radius: int = 5
    max_auto_severity: str = "critical"

    @model_validator(mode="after")
    def _validate(self) -> "ReliabilityPolicyRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        if self.max_auto_severity not in ("info", "warning", "critical"):
            raise ValueError("max_auto_severity must be info|warning|critical")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be within [0,1]")
        return self


class ReliabilityPolicyResponse(BaseModel):
    project_id: str
    auto_remediate: bool
    min_confidence: float
    max_blast_radius: int
    max_auto_severity: str
    updated_at: Optional[datetime] = None


class ReliabilityDecisionResponse(BaseModel):
    tier: int
    action: str                                  # auto_remediate | gated_pr | handoff
    reason: str
    confidence: float = 0.0
    blast_radius: int = 0
    policy: dict[str, Any] = Field(default_factory=dict)


# ── Agent Studio, executable Panels, and durable A2A coordination ────────────
class AgentDefinitionCreateRequest(BaseModel):
    project_id: str
    agent_key: str
    name: str
    description: str = ""
    owner: str = "vp_agents"
    actor: str = "vp_agents"

    @model_validator(mode="after")
    def _validate(self) -> "AgentDefinitionCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.agent_key = _require_text(self.agent_key, "agent_key")
        self.name = _require_text(self.name, "name")
        self.actor = _require_text(self.actor, "actor")
        return self


class AgentVersionCreateRequest(BaseModel):
    project_id: str
    role_key: str
    model_policy: dict[str, Any] = Field(default_factory=dict)
    prompts: dict[str, Any] = Field(default_factory=dict)
    rules: list[Any] = Field(default_factory=list)
    requested_skills: list[str] = Field(default_factory=list)
    requested_integrations: list[str] = Field(default_factory=list)
    scopes: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    a2a_policy: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    agent_module_id: Optional[str] = None
    actor: str = "vp_agents"


class AgentStudioActorRequest(BaseModel):
    project_id: str
    actor: str = "vp_agents"


class AgentTestRunRequest(AgentStudioActorRequest):
    scenario: dict[str, Any] = Field(default_factory=dict)
    assertions: list[dict[str, Any]] = Field(default_factory=list)


class AgentStudioRunRequest(AgentStudioActorRequest):
    input: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: Optional[str] = None
    task_id: Optional[str] = None
    workflow_run_id: Optional[str] = None
    budget_reservation_id: Optional[str] = None


class PanelDefinitionCreateRequest(BaseModel):
    project_id: str
    panel_key: str
    name: str
    description: str = ""
    purpose: str
    owner: str = "vp_agents"
    actor: str = "vp_agents"


class PanelVersionCreateRequest(AgentStudioActorRequest):
    topology: str = "panel"
    members: list[dict[str, Any]] = Field(default_factory=list)
    context_policy: dict[str, Any] = Field(default_factory=dict)
    independence_policy: dict[str, Any] = Field(default_factory=dict)
    quorum_policy: dict[str, Any] = Field(default_factory=dict)
    bounds: dict[str, Any] = Field(default_factory=dict)
    evidence_policy: dict[str, Any] = Field(default_factory=dict)
    consolidation_policy: dict[str, Any] = Field(default_factory=dict)
    dissent_policy: dict[str, Any] = Field(default_factory=dict)
    escalation_policy: dict[str, Any] = Field(default_factory=dict)
    output_contract: dict[str, Any] = Field(default_factory=dict)


class PanelRunRequest(AgentStudioActorRequest):
    input: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: Optional[str] = None
    task_id: Optional[str] = None
    workflow_run_id: Optional[str] = None


class A2AChannelCreateRequest(BaseModel):
    project_id: str
    channel_key: str
    name: str
    description: str = ""
    allowed_message_kinds: list[str] = Field(default_factory=list)
    allowed_schema_versions: dict[str, Any] = Field(default_factory=dict)
    retention_seconds: int = 604800
    max_payload_bytes: int = 65536
    actor: str = "coo"


class A2ASubscriptionCreateRequest(BaseModel):
    project_id: str
    channel_id: str
    subscriber_kind: str
    subscriber_id: str
    consumer_group: Optional[str] = None
    recipient_filter: dict[str, Any] = Field(default_factory=dict)
    ack_mode: str = "processed"
    visibility_timeout_seconds: int = 60
    max_attempts: int = 3
    data_boundaries: list[DataBoundary] = Field(
        default_factory=lambda: ["public", "internal"]
    )
    actor: str = "coo"


class A2AMessagePublishRequest(BaseModel):
    project_id: str
    channel_id: str
    message_kind: str
    schema_name: str
    schema_version: int = 1
    sender_kind: str
    sender_id: str
    sender_version_id: Optional[str] = None
    recipient: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str
    data_boundary: DataBoundary = "internal"
    priority: int = 100
    ttl_seconds: Optional[int] = None
    ack_mode: str = "processed"
    conversation_id: Optional[str] = None
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    trace_id: Optional[str] = None
    reply_to_id: Optional[str] = None
    lineage: dict[str, Any] = Field(default_factory=dict)
    actor: str = "coo"


class A2ADispatchRequest(BaseModel):
    project_id: str
    worker_id: str
    lease_token: str
    created_by_job_id: str


class A2APollRequest(BaseModel):
    project_id: str
    subscription_id: str
    consumer_name: str
    count: int = 10
    block_ms: int = 0
    actor: str = "coo"


class A2AAcknowledgeRequest(BaseModel):
    project_id: str
    acknowledgement: str
    actor_kind: str
    actor_id: str
    result: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    reason: Optional[str] = None


class A2AMessageCancelRequest(BaseModel):
    project_id: str
    actor: str = "coo"
    reason: str

    @model_validator(mode="after")
    def _validate(self) -> "A2AMessageCancelRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        self.reason = _require_text(self.reason, "reason")
        return self


class A2AMessageReplayRequest(BaseModel):
    project_id: str
    dedupe_key: str
    reason: str
    ttl_seconds: Optional[int] = None
    actor: str = "coo"

    @model_validator(mode="after")
    def _validate(self) -> "A2AMessageReplayRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.dedupe_key = _require_text(self.dedupe_key, "dedupe_key")
        self.reason = _require_text(self.reason, "reason")
        self.actor = _require_text(self.actor, "actor")
        return self


class WorkItemUpsertRequest(BaseModel):
    project_id: str
    subject_type: str
    subject_id: str
    title: str
    description: str = ""
    status: str = "active"
    strategic_value: int = Field(default=50, ge=0, le=100)
    customer_impact: int = Field(default=50, ge=0, le=100)
    urgency: int = Field(default=50, ge=0, le=100)
    cost_of_delay: int = Field(default=50, ge=0, le=100)
    risk_reduction: int = Field(default=50, ge=0, le=100)
    dependency_pressure: int = Field(default=50, ge=0, le=100)
    confidence: int = Field(default=75, ge=0, le=100)
    manual_adjustment: int = Field(default=0, ge=-100, le=100)
    rationale: str = ""
    workload_class: str = "general"
    estimated_units: int = Field(default=1, ge=1, le=10000)
    due_at: Optional[datetime] = None
    owner_actor: Optional[str] = None
    evidence: list[Any] = Field(default_factory=list)
    source: str = "boss"
    actor: str = "product_manager"


class WorkAdmissionRequest(BaseModel):
    project_id: str
    role_key: str
    requested_autonomy_level: int = Field(ge=0, le=10)
    estimated_cost_usd_micros: int = Field(ge=0)
    actor: str = "chief_operator"

    @model_validator(mode="after")
    def validate_fields(self) -> "WorkAdmissionRequest":
        self.role_key = _require_text(self.role_key, "role_key")
        self.actor = _require_text(self.actor, "actor")
        return self


class WorkScorePreviewRequest(BaseModel):
    """Pure scoring preview — no DB write, no project required (BOSS-353)."""

    strategic_value: int = Field(default=50, ge=0, le=100)
    customer_impact: int = Field(default=50, ge=0, le=100)
    urgency: int = Field(default=50, ge=0, le=100)
    cost_of_delay: int = Field(default=50, ge=0, le=100)
    risk_reduction: int = Field(default=50, ge=0, le=100)
    dependency_pressure: int = Field(default=50, ge=0, le=100)
    confidence: int = Field(default=75, ge=0, le=100)
    manual_adjustment: int = Field(default=0, ge=-100, le=100)
    estimated_cost_usd_micros: int = Field(default=0, ge=0)


class ResourceQuotaUpsertRequest(BaseModel):
    project_id: str
    scope_type: Literal["global", "org", "workspace", "project", "agent", "workload"]
    scope_id: str
    resource_kind: str = "job_concurrency"
    reserved_min: int = Field(default=0, ge=0)
    soft_limit: int = Field(default=1, ge=0)
    hard_limit: int = Field(default=1, ge=1)
    burst_limit: int = Field(default=0, ge=0)
    allocation_weight: float = Field(default=1, gt=0)
    borrow_enabled: bool = False
    policy: dict[str, Any] = Field(default_factory=dict)
    actor: str = "chief_operator"


class ResourceAllocationPlanRequest(BaseModel):
    project_id: str
    resource_kind: str = "job_concurrency"
    actor: str = "chief_operator"


class WorkTrackerBindingUpsertRequest(BaseModel):
    project_id: str
    work_item_id: str
    provider_key: Optional[str] = None
    external_workspace_id: Optional[str] = None
    external_project_id: Optional[str] = None
    external_issue_id: Optional[str] = None
    external_key: Optional[str] = None
    external_url: Optional[str] = None
    external_state: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "product_manager"


class WorkTrackerSyncExecuteRequest(BaseModel):
    project_id: str
    binding_id: str
    worker_id: str
    lease_token: str
    created_by_job_id: str


class DeliveryBotIdentityUpsertRequest(BaseModel):
    project_id: str
    provider: Literal["mattermost", "github", "plane"]
    agent_key: str
    display_name: str
    external_identity: Optional[str] = None
    agent_definition_id: Optional[str] = None
    credential_secret_ref_id: Optional[str] = None
    avatar_url: Optional[str] = None
    policy: dict[str, Any] = Field(default_factory=dict)
    actor: str = "chief_operator"


class DeliveryStatusChannelUpsertRequest(BaseModel):
    project_id: str
    provider: Literal["mattermost", "github", "plane"]
    channel_key: str
    display_name: str
    purpose: str
    external_channel_id: Optional[str] = None
    external_url: Optional[str] = None
    bot_identity_id: Optional[str] = None
    event_kinds: list[str] = Field(default_factory=list)
    allowed_data_boundaries: list[DataBoundary] = Field(
        default_factory=lambda: ["public", "internal"]
    )
    configuration: dict[str, Any] = Field(default_factory=dict)
    actor: str = "chief_operator"


class DeliveryPresenceEventCreateRequest(BaseModel):
    project_id: str
    provider: Literal["mattermost", "github", "plane"]
    event_kind: str
    subject_type: str
    subject_id: str
    status: str
    summary: str
    dedupe_key: str
    channel_id: Optional[str] = None
    bot_identity_id: Optional[str] = None
    external_url: Optional[str] = None
    receipt_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "delivery_presence"


class ToolchainSnapshotFile(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=262_144)


class ToolchainAssessRequest(BaseModel):
    project_id: str
    source_key: str
    revision: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    files: list[ToolchainSnapshotFile] = Field(min_length=1, max_length=500)
    actor: str = "security_engineer"


class ToolchainCandidateReviewRequest(BaseModel):
    project_id: str
    decision: Literal["approve", "reject"]
    note: str = Field(min_length=1, max_length=4000)
    actor: str = "security_engineer"


class ToolchainCommandGuardRequest(BaseModel):
    project_id: str
    command: str = Field(min_length=1, max_length=32_768)
    cwd: Optional[str] = Field(default=None, max_length=4096)
    actor: str = "security_engineer"


class ToolchainContextChunk(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=1_000_000)
    authority: Literal[
        "policy", "approval", "contract", "proof", "fact", "reference"
    ] = "reference"
    required: bool = False
    priority: int = Field(default=50, ge=0, le=100)
    source_ref: Optional[str] = Field(default=None, max_length=2048)


class ToolchainContextPackRequest(BaseModel):
    project_id: str
    chunks: list[ToolchainContextChunk] = Field(max_length=1000)
    token_budget: int = Field(ge=128, le=2_000_000)
    reserved_output_tokens: int = Field(default=0, ge=0)
    actor: str = "agent_loop"


class KnowledgeContextItem(BaseModel):
    """A message or free-form context unit for knowledge packing.

    Accepts either toolchain-style ``text`` or chat-style ``content`` (+ optional
    ``role``). Empty bodies are ignored at pack time.
    """

    id: Optional[str] = Field(default=None, max_length=256)
    text: Optional[str] = Field(default=None, max_length=1_000_000)
    content: Optional[str] = Field(default=None, max_length=1_000_000)
    role: Optional[str] = Field(default=None, max_length=64)
    authority: Literal[
        "policy", "approval", "contract", "proof", "fact", "reference"
    ] = "reference"
    required: bool = False
    priority: int = Field(default=50, ge=0, le=100)
    source_ref: Optional[str] = Field(default=None, max_length=2048)


class KnowledgePackContextRequest(BaseModel):
    """Operator/agent knowledge packing request (BOSS-356).

    Provide any combination of ``messages``, ``items``, or ``chunks``. Optional
    ``tool_ids`` inject native tool catalog snippets as reference context.
    Secrets are redacted before packing and never returned in ``context``.
    """

    project_id: str
    actor: str = "agent_loop"
    messages: list[KnowledgeContextItem] = Field(default_factory=list, max_length=1000)
    items: list[KnowledgeContextItem] = Field(default_factory=list, max_length=1000)
    chunks: list[ToolchainContextChunk] = Field(default_factory=list, max_length=1000)
    token_budget: Optional[int] = Field(default=None, ge=128, le=2_000_000)
    reserved_output_tokens: int = Field(default=0, ge=0)
    tool_ids: list[str] = Field(default_factory=list, max_length=100)


# ── Knowledge graph (RB-15) — Community-safe /v1/knowledge/graph/* ────────────


class KnowledgeGraphNodeUpsertRequest(BaseModel):
    """Create-or-merge a knowledge-graph node keyed by (project, kind, node_key).

    BOSS-355: carries provenance ``source``, ``confidence``, and temporal
    validity ``valid_from`` / ``valid_to`` (ISO-8601 or null for unbounded).
    """

    project_id: str
    actor: str = "system"
    kind: str
    node_key: str
    name: Optional[str] = None
    attrs: dict[str, Any] = Field(default_factory=dict)
    layer: str = "deterministic"
    source: str = "manual"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None

    @model_validator(mode="after")
    def _validate(self) -> "KnowledgeGraphNodeUpsertRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        self.kind = _require_text(self.kind, "kind")
        self.node_key = _require_text(self.node_key, "node_key")
        self.layer = _require_text(self.layer, "layer")
        self.source = _require_text(self.source, "source")
        if self.layer not in ("deterministic", "inferred"):
            raise ValueError("layer must be deterministic|inferred")
        return self


class KnowledgeGraphNodeUpsertResponse(BaseModel):
    id: str
    project_id: str
    kind: str
    node_key: str
    source: Optional[str] = None
    confidence: Optional[float] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None


class KnowledgeGraphEdgeUpsertRequest(BaseModel):
    """Create-or-merge a directed edge keyed by (project, src, dst, kind)."""

    project_id: str
    actor: str = "system"
    src_id: str
    dst_id: str
    kind: str
    attrs: dict[str, Any] = Field(default_factory=dict)
    layer: str = "deterministic"
    source: str = "manual"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None

    @model_validator(mode="after")
    def _validate(self) -> "KnowledgeGraphEdgeUpsertRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        self.src_id = _require_text(self.src_id, "src_id")
        self.dst_id = _require_text(self.dst_id, "dst_id")
        self.kind = _require_text(self.kind, "kind")
        self.layer = _require_text(self.layer, "layer")
        self.source = _require_text(self.source, "source")
        if self.layer not in ("deterministic", "inferred"):
            raise ValueError("layer must be deterministic|inferred")
        return self


class KnowledgeGraphEdgeUpsertResponse(BaseModel):
    id: str
    project_id: str
    src_id: str
    dst_id: str
    kind: str
    source: Optional[str] = None
    confidence: Optional[float] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None


class KnowledgeGraphNodeListResponse(BaseModel):
    project_id: str
    items: list[dict[str, Any]] = Field(default_factory=list)
    count: int = 0


class KnowledgeRetrievalEvalResponse(BaseModel):
    """BOSS-357 coverage metrics + regression/threshold alarm."""

    project_id: str
    coverage_score: float
    threshold: float
    alarm: bool
    alarm_reasons: list[str] = Field(default_factory=list)
    prior_coverage_score: Optional[float] = None
    regression: bool = False
    below_threshold: bool = False
    metrics: dict[str, Any] = Field(default_factory=dict)
    snapshot_id: Optional[str] = None
    evaluated_at: Optional[str] = None


class KnowledgeGraphSubgraphResponse(BaseModel):
    project_id: str
    root: Optional[dict[str, Any]] = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    depth: int = 2


class KnowledgeGraphNeighborsResponse(BaseModel):
    project_id: str
    node_id: str
    items: list[dict[str, Any]] = Field(default_factory=list)


class KnowledgeGraphRootCauseRequest(BaseModel):
    """Persist a confirmed root cause as a ``caused_by`` edge (learning write-back)."""

    project_id: str
    actor: str = "system"
    service: str
    cause_kind: str
    cause_key: str
    cause_name: str
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: Optional[dict[str, Any]] = None

    @model_validator(mode="after")
    def _validate(self) -> "KnowledgeGraphRootCauseRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        self.service = _require_text(self.service, "service")
        self.cause_kind = _require_text(self.cause_kind, "cause_kind")
        self.cause_key = _require_text(self.cause_key, "cause_key")
        self.cause_name = _require_text(self.cause_name, "cause_name")
        return self


class KnowledgeGraphRootCauseResponse(BaseModel):
    service_id: str
    cause_id: str
    edge_id: str


class KnowledgeGraphKnownCausesResponse(BaseModel):
    project_id: str
    service: str
    items: list[dict[str, Any]] = Field(default_factory=list)


class KnowledgeGraphSeedFromInfraRequest(BaseModel):
    """Seed ``resource`` nodes from active ``infra_resources`` inventory."""

    project_id: str
    actor: str = "system"

    @model_validator(mode="after")
    def _validate(self) -> "KnowledgeGraphSeedFromInfraRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class KnowledgeGraphSeedFromInfraResponse(BaseModel):
    project_id: str
    resources: int


class ProviderSloUpsertRequest(BaseModel):
    """Create or update a provider SLO definition (BOSS-241)."""

    project_id: str
    provider: str
    metric_key: str
    target: float
    window_seconds: int = Field(default=3600, ge=60, le=2_592_000)
    comparator: str = "gte"
    provider_account_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"
    slo_id: Optional[str] = None

class ProviderSloEvaluateRequest(BaseModel):
    project_id: str
    slo_id: Optional[str] = None
    actor: str = "system"

class ProviderSloSummary(BaseModel):
    id: str
    project_id: str
    provider: str
    provider_account_id: Optional[str] = None
    metric_key: str
    target: float
    window_seconds: int
    comparator: str
    status: str
    last_value: Optional[float] = None
    evaluated_at: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "system"
    created_at: str
    updated_at: str

class ProviderSloListResponse(BaseModel):
    project_id: str
    items: list[ProviderSloSummary]
    limit: int
    offset: int

class ProviderSloEvaluateResponse(BaseModel):
    project_id: str
    count: int
    summary: dict[str, int]
    evaluations: list[ProviderSloEvaluationSummary]


class ProviderSloEvaluationSummary(BaseModel):
    id: str
    definition_id: str
    project_id: str
    provider: str
    provider_account_id: Optional[str] = None
    metric_key: str
    target: float
    window_seconds: int
    comparator: str
    status: str
    last_value: Optional[float] = None
    evaluated_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class FeatureCatalogEntrySummary(BaseModel):
    id: str
    project_id: str
    label: Optional[str] = None
    slug: str
    title: str
    doc_path: str
    status: str
    source: Optional[str] = None
    implementation_files: list[Any] = Field(default_factory=list)
    test_coverage: list[Any] = Field(default_factory=list)
    source_refs: list[Any] = Field(default_factory=list)
    needs_label: bool = False
    last_documented_event_id: Optional[str] = None
    last_documented_at: Optional[str] = None
    created_at: str
    updated_at: str
    idempotent: bool = False

class FeatureCatalogListResponse(BaseModel):
    project_id: str
    items: list[FeatureCatalogEntrySummary]
    count: int

class FeatureSeedRequest(BaseModel):
    project_id: str
    actor: str = "system"

class FeatureSeedResponse(BaseModel):
    project_id: str
    items: list[FeatureCatalogEntrySummary]
    entry_count: int
    seeded: bool
    source: Optional[str] = None
    reason: Optional[str] = None

class FeatureUpsertRequest(BaseModel):
    project_id: str
    title: str
    label: Optional[str] = None
    slug: Optional[str] = None
    doc_path: Optional[str] = None
    status: Optional[str] = None
    source: Optional[str] = None
    implementation_files: list[Any] = Field(default_factory=list)
    test_coverage: list[Any] = Field(default_factory=list)
    source_refs: list[Any] = Field(default_factory=list)
    needs_label: Optional[bool] = None
    actor: str

class FeatureDocumentedRequest(BaseModel):
    project_id: str
    event_id: str
    status: Optional[str] = None
    actor: str

class CapabilityItem(BaseModel):
    id: str
    title: str
    summary: str
    initiator_tool: Optional[str] = None
    enabled: bool
    reason: Optional[str] = None

class CapabilityManifestResponse(BaseModel):
    project_id: str
    capabilities: list[CapabilityItem]
    count: int

class CapabilityManifestTableResponse(BaseModel):
    project_id: str
    table: str

class LearningExampleRecordRequest(BaseModel):
    project_id: str
    agent: str
    kind: str
    label: str = ""
    outcome: str = ""
    ref: str = ""
    source: str = "manual"
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def _validate(self) -> "LearningExampleRecordRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.agent = _require_text(self.agent, "agent")
        self.kind = _require_text(self.kind, "kind")
        self.actor = _require_text(self.actor, "actor")
        self.source = (self.source or "manual").strip() or "manual"
        return self

class LearningExampleRecordResponse(BaseModel):
    id: str
    project_id: str
    agent: str
    kind: str

class LearningExampleItem(BaseModel):
    id: str
    agent: str
    kind: str
    label: str = ""
    outcome: str = ""
    ref: str = ""
    source: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None

class LearningCorpusResponse(BaseModel):
    project_id: str
    agent: str
    kind: Optional[str] = None
    items: list[LearningExampleItem] = Field(default_factory=list)
    count: int = 0

class LearningEvaluateResponse(BaseModel):
    project_id: str
    agent: str
    total: int
    by_outcome: dict[str, int] = Field(default_factory=dict)
    by_label: dict[str, int] = Field(default_factory=dict)
    false_alarm_rate: Optional[float] = None
    fix_success_rate: Optional[float] = None

class LearningSuggestTuningRequest(BaseModel):
    project_id: str
    agent: str
    actor: str = "system"

    @model_validator(mode="after")
    def _validate(self) -> "LearningSuggestTuningRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.agent = _require_text(self.agent, "agent")
        self.actor = _require_text(self.actor, "actor")
        return self

class LearningSuggestTuningResponse(BaseModel):
    project_id: str
    agent: str
    raise_threshold: list[dict[str, Any]] = Field(default_factory=list)
    few_shot_candidates: list[dict[str, Any]] = Field(default_factory=list)

class IntentClassifyRequest(BaseModel):
    message: str

    @model_validator(mode="after")
    def _validate(self) -> "IntentClassifyRequest":
        self.message = _require_text(self.message, "message")
        return self

class IntentClassifyResponse(BaseModel):
    intent: Optional[str] = None
    arguments: dict[str, Any] = Field(default_factory=dict)

class IntentRouteRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    confirm_plan_id: Optional[str] = None
    pending_plan_id: Optional[str] = None
    external_ref: Optional[str] = None

    @model_validator(mode="after")
    def _validate(self) -> "IntentRouteRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        if not self.messages and not self.confirm_plan_id:
            raise ValueError("messages or confirm_plan_id is required.")
        if self.messages is not None and not isinstance(self.messages, list):
            raise ValueError("messages must be a list.")
        # Fail closed on empty content when no confirmation is in flight.
        if not self.confirm_plan_id:
            if not self.messages:
                raise ValueError("messages or confirm_plan_id is required.")
            last = self.messages[-1] if self.messages else {}
            content = str((last or {}).get("content") or "").strip()
            if not content:
                raise ValueError("messages must end with non-empty content.")
        return self

class IntentRouteResponse(BaseModel):
    reply: str
    actions: list[dict[str, Any]] = Field(default_factory=list)
    pending_confirmation: Optional[dict[str, Any]] = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)

class TelemetryQueryRequest(BaseModel):
    """Direct connector query — metrics|logs|traces|alerts (not a reliability scan)."""
    project_id: str
    actor: str = "system"
    kind: str                                    # metrics | logs | traces | alerts
    query: str = ""
    source_id: Optional[str] = None
    source: Optional[dict[str, Any]] = None      # inline connector config when source_id omitted
    window: ReliabilityWindow
    limit: int = 5000

    @model_validator(mode="after")
    def _validate(self) -> "TelemetryQueryRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        self.kind = _require_text(self.kind, "kind").lower()
        if self.kind not in ("metrics", "logs", "traces", "alerts"):
            raise ValueError("kind must be metrics|logs|traces|alerts")
        if not self.source_id and not self.source:
            raise ValueError("source_id or source is required")
        if self.limit < 1 or self.limit > 10000:
            raise ValueError("limit must be within [1, 10000]")
        return self

class TelemetryQueryResponse(BaseModel):
    project_id: str
    kind: str
    query: str = ""
    source_id: Optional[str] = None
    source_kind: Optional[str] = None
    window: ReliabilityWindow
    count: int = 0
    items: list[dict[str, Any]] = Field(default_factory=list)

class TelemetryConnectorCatalogItem(BaseModel):
    kind: str
    title: str
    capabilities: list[str] = Field(default_factory=list)
    auth: str = "none"
    required_config: list[str] = Field(default_factory=list)
    optional_config: list[str] = Field(default_factory=list)
    required_env_present: bool = False

class TelemetryConnectorCatalogResponse(BaseModel):
    items: list[TelemetryConnectorCatalogItem] = Field(default_factory=list)
    count: int = 0

class ChangeCorrelateRequest(BaseModel):
    """Rank recent changes near an incident onset or service window (RB-17)."""
    project_id: str
    actor: str = "system"
    incident_ref: Optional[str] = None
    service: Optional[str] = None
    window: Optional[ReliabilityWindow] = None
    lookback_minutes: Optional[int] = None
    limit: int = 10

    @model_validator(mode="after")
    def _validate(self) -> "ChangeCorrelateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        if not (self.incident_ref or self.service):
            raise ValueError("incident_ref or service is required")
        if not self.incident_ref and self.window is None:
            raise ValueError("window is required when incident_ref is omitted")
        if self.limit < 1 or self.limit > 100:
            raise ValueError("limit must be within [1, 100]")
        if self.lookback_minutes is not None and self.lookback_minutes < 1:
            raise ValueError("lookback_minutes must be >= 1")
        return self

class ChangeCorrelateResponse(BaseModel):
    project_id: str
    service: Optional[str] = None
    incident_ref: Optional[str] = None
    onset: str
    lookback_minutes: int
    changes: list[dict[str, Any]] = Field(default_factory=list)
    count: int = 0


# ── Agent Studio, executable Panels, and durable A2A coordination ────────────

class ModelRateLimitLaneSummary(BaseModel):
    """Per-provider AIMD lane state (process-local)."""

    provider: str
    limit: int
    max_concurrency: int
    floor_concurrency: int = 1
    in_flight: int = 0
    successes: int = 0
    backoff_remaining_seconds: float = 0.0
    pacing_wait_seconds: float = 0.0

class ModelRateLimitsResponse(BaseModel):
    """Observability snapshot of the model-call rate limiter.

    State is process-local: API and worker processes do not share counters.
    Lanes are empty until the process has paced at least one call for a provider.
    """

    project_id: Optional[str] = None
    mode: str
    scope: str = "process_local"
    available: bool = True
    note: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    lanes: list[ModelRateLimitLaneSummary] = Field(default_factory=list)

class ModelRateLimitReportRequest(BaseModel):
    """Worker-reported rate-limit or success signal for AIMD pacing."""

    project_id: Optional[str] = None
    provider: str = Field(min_length=1, max_length=100)
    outcome: Literal["rate_limited", "ok"]
    retry_after_s: Optional[float] = Field(default=None, ge=0)

class ModelRateLimitReportResponse(BaseModel):
    accepted: bool
    project_id: Optional[str] = None
    provider: str
    outcome: str
    scope: str = "process_local"
    note: str = ""
    mode: Optional[str] = None
    lane: Optional[ModelRateLimitLaneSummary] = None

class PipelineAdapterCatalogItem(BaseModel):
    """Static pipeline adapter kind — capabilities only, no secret values."""

    kind: str
    display_name: str
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    read_only_operations: list[str] = Field(default_factory=list)
    effect_operations: list[str] = Field(default_factory=list)
    config_keys: list[str] = Field(default_factory=list)
    secret_config_keys: list[str] = Field(default_factory=list)
    requires_credentials: bool = True
    status: str = "available"
    is_default: bool = False

class PipelineAdapterCatalogResponse(BaseModel):
    default_provider: str
    note: str = ""
    items: list[PipelineAdapterCatalogItem] = Field(default_factory=list)


# ── Operator utilities: CI generate, redaction, standards, mining, debate ─────


class CIConfigGenerateRequest(BaseModel):
    """Dry-run CI config generation (no PR, no provider write)."""

    language: str = Field(min_length=1, max_length=64)
    framework: Optional[str] = Field(default=None, max_length=64)
    target: str = Field(default="github_actions", min_length=1, max_length=64)

    @model_validator(mode="after")
    def _validate(self) -> "CIConfigGenerateRequest":
        self.language = _require_text(self.language, "language").lower()
        self.target = _require_text(self.target, "target").lower()
        if self.framework is not None:
            self.framework = self.framework.strip() or None
        return self


class CIConfigGenerateResponse(BaseModel):
    target: str
    path: str
    content: str
    checksum: str
    commands: dict[str, str] = Field(default_factory=dict)


class RedactRequest(BaseModel):
    """PII/secret redaction before a data boundary (deterministic)."""

    text: str = ""


class RedactResponse(BaseModel):
    text: str
    redacted: str
    summary: dict[str, int] = Field(default_factory=dict)
    has_pii: bool = False


class PlatformStandardsCatalogResponse(BaseModel):
    """Catalog of BOSS platform standards (keyed blocks agents must apply)."""

    keys: list[str] = Field(default_factory=list)
    standards: dict[str, str] = Field(default_factory=dict)
    count: int = 0
    #: Rules a ROLE mandated, not the platform. An agent can now change how every future build
    #: is prompted, so the operator has to be able to see what was added, by whom and why —
    #: an invisible standard is an ungovernable one.
    mandated: list[dict] = Field(default_factory=list)


class PlatformStandardsApplyRequest(BaseModel):
    """Resolve which platform standards apply to a task description."""

    instructions: str = ""
    force: bool = False


class PlatformStandardsApplyResponse(BaseModel):
    applies: bool
    force: bool = False
    block: str = ""
    keys_applied: list[str] = Field(default_factory=list)


class TelemetryMineLogEvent(BaseModel):
    ts: float = 0.0
    message: str
    service: str = ""
    level: str = "info"
    fields: dict[str, Any] = Field(default_factory=dict)


class TelemetryMineMetricPoint(BaseModel):
    ts: float
    value: float


class TelemetryMineMetricSeries(BaseModel):
    name: str
    points: list[TelemetryMineMetricPoint] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class TelemetryMineRequest(BaseModel):
    """Model-free log/metric distillation (templates + anomaly signals)."""

    events: list[TelemetryMineLogEvent] = Field(default_factory=list)
    baseline: Optional[dict[str, float]] = None
    metrics: Optional[list[TelemetryMineMetricSeries]] = None
    top_n: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def _validate(self) -> "TelemetryMineRequest":
        if not self.events and not self.metrics:
            raise ValueError("events or metrics is required")
        return self


class TelemetryMineResponse(BaseModel):
    events: int = 0
    distinct_templates: int = 0
    reduction_ratio: Optional[float] = None
    top_templates: list[dict[str, Any]] = Field(default_factory=list)
    signals: list[dict[str, Any]] = Field(default_factory=list)
    signal_count: int = 0


class VerificationTargetProfileRequest(BaseModel):
    """Resolve web vs python verification target from a task spec."""

    spec: dict[str, Any] = Field(default_factory=dict)
    title: str = ""


class VerificationTargetProfileResponse(BaseModel):
    kind: str
    steps: list[str] = Field(default_factory=list)
    command: str
    test_commands: list[str] = Field(default_factory=list)


class AwsMetalTypesResponse(BaseModel):
    """Advisory AWS EC2 bare-metal instance type shortlist."""

    region: Optional[str] = None
    items: list[str] = Field(default_factory=list)
    count: int = 0
    note: str = (
        "Advisory only — validate availability via EC2 describe-instance-type-offerings."
    )


class AwsMetalClassifyRequest(BaseModel):
    instance_type: str = Field(min_length=1, max_length=64)


class AwsMetalClassifyResponse(BaseModel):
    instance_type: str
    is_bare_metal: bool


class VerifierDebateTurn(BaseModel):
    """Scripted debate turn for pure (no-model) debate runs."""

    argument: str = ""
    concede: bool = False
    withdraw: bool = False
    verdict: Optional[str] = None
    rationale: str = ""


class VerifierDebateRequest(BaseModel):
    """Bounded generator↔verifier debate with scripted debaters (zero model cost).

    Provide ordered defend/challenge turns; optional judge turns used only if the
    debate is unresolved after max_rounds.
    """

    claim: str = Field(min_length=1)
    defend_turns: list[VerifierDebateTurn] = Field(default_factory=list)
    challenge_turns: list[VerifierDebateTurn] = Field(default_factory=list)
    judge_turns: list[VerifierDebateTurn] = Field(default_factory=list)
    max_rounds: int = Field(default=2, ge=1, le=8)

    @model_validator(mode="after")
    def _validate(self) -> "VerifierDebateRequest":
        self.claim = _require_text(self.claim, "claim")
        return self


class VerifierDebateResponse(BaseModel):
    verdict: str
    reason: str
    confidence: float
    rounds_used: int
    transcript: list[dict[str, Any]] = Field(default_factory=list)


class DesignPromptRequest(BaseModel):
    """Build the product_design role prompt for a UI-bearing task."""

    title: str = ""
    instructions: str = ""
    criteria: list[str] = Field(default_factory=list)
    design_system: Optional[str] = None
    spec: dict[str, Any] = Field(default_factory=dict)
    test_files: list[str] = Field(default_factory=list)


class DesignPromptResponse(BaseModel):
    ui_bearing: bool
    product_design_prompt: Optional[str] = None
    design_impl_clause: Optional[str] = None
    design_review_clause: Optional[str] = None


class LlmCallItem(BaseModel):
    id: str
    project_id: Optional[str] = None
    code_task_run_id: Optional[str] = None
    stage: Optional[str] = None
    engine: Optional[str] = None
    model: Optional[str] = None
    role: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd_micros: int = 0
    price_estimated: Optional[bool] = None
    latency_ms: int = 0
    retries: int = 0
    turns: int = 0
    ok: bool = True
    error: Optional[str] = None
    created_at: Optional[str] = None


class LlmCallListResponse(BaseModel):
    project_id: str
    count: int
    total: int
    limit: int
    offset: int
    items: list[LlmCallItem] = Field(default_factory=list)


class LlmCallSummaryGroup(BaseModel):
    group_key: str
    calls: int
    cost_usd_micros: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    ok_calls: int = 0
    failed_calls: int = 0
    avg_latency_ms: float = 0.0


class LlmCallSummaryResponse(BaseModel):
    project_id: str
    group_by: str
    total_calls: int
    total_cost_usd_micros: int
    groups: list[LlmCallSummaryGroup] = Field(default_factory=list)
