from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

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


class AccountConnectLinkRequest(BaseModel):
    project_id: str
    hosted_org_id: str
    connection_ref: Optional[str] = None
    marketplace_identity: dict[str, Any] = Field(default_factory=dict)
    billing_portal_ref: Optional[str] = None
    connector_authorization_refs: list[str] = Field(default_factory=list)
    optional_telemetry_enabled: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_link(self) -> "AccountConnectLinkRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.hosted_org_id = _require_text(self.hosted_org_id, "hosted_org_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class AccountConnectUnlinkRequest(BaseModel):
    project_id: str
    hosted_org_id: Optional[str] = None
    reason: Optional[str] = None
    actor: str = "system"

    @model_validator(mode="after")
    def validate_unlink(self) -> "AccountConnectUnlinkRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class AccountConnectMutationResponse(BaseModel):
    project_id: str
    edition: EditionStatusResponse


class AccountConnectSyncRequest(BaseModel):
    project_id: str
    hosted_org_id: Optional[str] = None
    actor: str = "system"

    @model_validator(mode="after")
    def validate_sync(self) -> "AccountConnectSyncRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class AccountConnectSyncResponse(BaseModel):
    project_id: str
    edition: EditionStatusResponse
    synced_entitlement_count: int


class AccountConnectHealthCheckSummary(BaseModel):
    id: str
    project_id: str
    account_connect_org_id: Optional[str] = None
    hosted_org_id: Optional[str] = None
    check_kind: str
    status: str
    latency_ms: Optional[int] = None
    provider_ref: Optional[str] = None
    readiness_packet_artifact_id: Optional[str] = None
    response_metadata: dict[str, Any]
    error: Optional[dict[str, Any]] = None
    checked_by: str
    metadata: dict[str, Any]
    created_at: str


class AccountConnectHealthCheckListResponse(BaseModel):
    project_id: str
    items: list[AccountConnectHealthCheckSummary]


class AccountConnectHealthCheckRunRequest(BaseModel):
    project_id: str
    hosted_org_id: Optional[str] = None
    check_kind: str = "hosted_service"
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_health_check(self) -> "AccountConnectHealthCheckRunRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.check_kind = _require_text(self.check_kind, "check_kind")
        self.actor = _require_text(self.actor, "actor")
        if self.hosted_org_id is not None:
            self.hosted_org_id = _require_text(self.hosted_org_id, "hosted_org_id")
        return self


class HostedMarketplaceListingSummary(BaseModel):
    id: str
    project_id: str
    account_connect_org_id: Optional[str] = None
    hosted_org_id: Optional[str] = None
    listing_id: str
    pack_id: str
    version: str
    display_name: str
    publisher: str
    category: str
    description: Optional[str] = None
    status: str
    price_model: Optional[str] = None
    price_usd_micros: Optional[int] = None
    required_entitlements: list[str]
    capabilities: list[str]
    source_url: Optional[str] = None
    metadata: dict[str, Any]
    last_sync_run_id: Optional[str] = None
    created_at: str
    updated_at: str


class HostedMarketplaceListingListResponse(BaseModel):
    project_id: str
    items: list[HostedMarketplaceListingSummary]


class HostedMarketplaceSyncRunSummary(BaseModel):
    id: str
    project_id: str
    account_connect_org_id: Optional[str] = None
    hosted_org_id: Optional[str] = None
    status: str
    listing_count: int
    provider_ref: Optional[str] = None
    sync_packet_artifact_id: Optional[str] = None
    error: Optional[dict[str, Any]] = None
    synced_by: str
    metadata: dict[str, Any]
    created_at: str


class HostedMarketplaceSyncRunListResponse(BaseModel):
    project_id: str
    items: list[HostedMarketplaceSyncRunSummary]


class HostedMarketplaceSyncRequest(BaseModel):
    project_id: str
    hosted_org_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_marketplace_sync(self) -> "HostedMarketplaceSyncRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        if self.hosted_org_id is not None:
            self.hosted_org_id = _require_text(self.hosted_org_id, "hosted_org_id")
        return self


class HostedMarketplaceSyncResponse(BaseModel):
    project_id: str
    sync_run: HostedMarketplaceSyncRunSummary
    listing_count: int


class AccountConnectAuthSessionSummary(BaseModel):
    id: str
    project_id: str
    account_connect_org_id: Optional[str] = None
    hosted_org_id: Optional[str] = None
    authorization_url: Optional[str] = None
    return_url: Optional[str] = None
    provider_ref: Optional[str] = None
    state: str
    status: str
    receipt_id: Optional[str] = None
    created_by: str
    expires_at: Optional[str] = None
    completed_at: Optional[str] = None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class AccountConnectAuthSessionListResponse(BaseModel):
    project_id: str
    items: list[AccountConnectAuthSessionSummary]


class AccountConnectAuthSessionCreateRequest(BaseModel):
    project_id: str
    hosted_org_id: Optional[str] = None
    return_url: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_auth_session_create(
        self,
    ) -> "AccountConnectAuthSessionCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        if self.hosted_org_id is not None:
            self.hosted_org_id = _require_text(self.hosted_org_id, "hosted_org_id")
        if self.return_url is not None:
            self.return_url = _require_text(self.return_url, "return_url")
        return self


class AccountConnectAuthSessionCompleteRequest(BaseModel):
    project_id: str
    code: str
    state: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_auth_session_complete(
        self,
    ) -> "AccountConnectAuthSessionCompleteRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.code = _require_text(self.code, "code")
        self.state = _require_text(self.state, "state")
        self.actor = _require_text(self.actor, "actor")
        return self


class AccountConnectTeamInviteSummary(BaseModel):
    id: str
    project_id: str
    account_connect_org_id: Optional[str] = None
    hosted_org_id: str
    email: str
    display_name: Optional[str] = None
    role_keys: list[str]
    permissions: list[str]
    invite_url: Optional[str] = None
    hosted_invite_ref: Optional[str] = None
    status: str
    delivery_status: str
    receipt_id: Optional[str] = None
    created_by: str
    accepted_by: Optional[str] = None
    accepted_at: Optional[str] = None
    revoked_by: Optional[str] = None
    revoked_at: Optional[str] = None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class AccountConnectTeamInviteListResponse(BaseModel):
    project_id: str
    items: list[AccountConnectTeamInviteSummary]


class AccountConnectTeamInviteCreateRequest(BaseModel):
    project_id: str
    email: str
    display_name: Optional[str] = None
    role_keys: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_team_invite(self) -> "AccountConnectTeamInviteCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.email = _require_text(self.email, "email").lower()
        if "@" not in self.email:
            raise ValueError("email must be a valid email-like address.")
        self.actor = _require_text(self.actor, "actor")
        return self


class AccountConnectTeamInviteRevokeRequest(BaseModel):
    project_id: str
    reason: Optional[str] = None
    actor: str = "system"

    @model_validator(mode="after")
    def validate_revoke(self) -> "AccountConnectTeamInviteRevokeRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        return self


class AccountConnectBillingPortalSessionSummary(BaseModel):
    id: str
    project_id: str
    account_connect_org_id: Optional[str] = None
    hosted_org_id: str
    portal_url: Optional[str] = None
    return_url: Optional[str] = None
    provider_ref: Optional[str] = None
    status: str
    receipt_id: Optional[str] = None
    created_by: str
    expires_at: Optional[str] = None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class AccountConnectBillingPortalSessionListResponse(BaseModel):
    project_id: str
    items: list[AccountConnectBillingPortalSessionSummary]


class AccountConnectBillingPortalSessionCreateRequest(BaseModel):
    project_id: str
    return_url: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_billing_portal(
        self,
    ) -> "AccountConnectBillingPortalSessionCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.actor = _require_text(self.actor, "actor")
        if self.return_url is not None:
            self.return_url = _require_text(self.return_url, "return_url")
        return self


class EnterpriseClusterSummary(BaseModel):
    id: str
    org_id: str
    project_id: str
    deployment_mode: str
    account_connect_mode: str
    worker_pool_strategy: str
    ha_profile: str
    data_residency: str
    identity_controls: dict[str, Any]
    governance_controls: dict[str, Any]
    audit_controls: dict[str, Any]
    private_worker_pools: list[dict[str, Any]]
    compliance_packs: list[str]
    support_tier: str
    support_sla: dict[str, Any]
    status: str
    blocking_gaps: list[str]
    created_by: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class EnterpriseClusterListResponse(BaseModel):
    project_id: str
    items: list[EnterpriseClusterSummary]


class EnterpriseClusterValidationRunSummary(BaseModel):
    id: str
    org_id: str
    project_id: str
    enterprise_cluster_id: str
    validation_kind: str
    status: str
    checks: list[dict[str, Any]]
    evidence_refs: list[str]
    blocking_gaps: list[str]
    validated_by: str
    metadata: dict[str, Any]
    created_at: str


class EnterpriseClusterValidationRunListResponse(BaseModel):
    project_id: str
    items: list[EnterpriseClusterValidationRunSummary]


class EnterpriseClusterValidationRunCreateRequest(BaseModel):
    project_id: str
    enterprise_cluster_id: str
    validation_kind: str
    status: str = "passed"
    checks: list[dict[str, Any]]
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_cluster_validation(
        self,
    ) -> "EnterpriseClusterValidationRunCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.enterprise_cluster_id = _require_text(
            self.enterprise_cluster_id, "enterprise_cluster_id"
        )
        self.validation_kind = _require_text(self.validation_kind, "validation_kind")
        self.status = _require_text(self.status, "status")
        if not self.checks:
            raise ValueError("checks must contain at least one validation check.")
        self.actor = _require_text(self.actor, "actor")
        return self


class EnterpriseClusterUpsertRequest(BaseModel):
    project_id: str
    deployment_mode: str = "enterprise_cloud"
    account_connect_mode: str = "attached"
    worker_pool_strategy: str = "managed_workers"
    ha_profile: str = "standard"
    data_residency: str = "us"
    identity_controls: dict[str, Any] = Field(default_factory=dict)
    governance_controls: dict[str, Any] = Field(default_factory=dict)
    audit_controls: dict[str, Any] = Field(default_factory=dict)
    private_worker_pools: list[dict[str, Any]] = Field(default_factory=list)
    compliance_packs: list[str] = Field(default_factory=list)
    support_tier: str = "standard"
    support_sla: dict[str, Any] = Field(default_factory=dict)
    status: str = "planned"
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_enterprise_cluster(self) -> "EnterpriseClusterUpsertRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.deployment_mode = _require_text(self.deployment_mode, "deployment_mode")
        self.account_connect_mode = _require_text(
            self.account_connect_mode, "account_connect_mode"
        )
        self.worker_pool_strategy = _require_text(
            self.worker_pool_strategy, "worker_pool_strategy"
        )
        self.ha_profile = _require_text(self.ha_profile, "ha_profile")
        self.data_residency = _require_text(self.data_residency, "data_residency")
        self.support_tier = _require_text(self.support_tier, "support_tier")
        self.status = _require_text(self.status, "status")
        self.actor = _require_text(self.actor, "actor")
        return self


class EnterpriseSupportCaseSummary(BaseModel):
    id: str
    org_id: str
    project_id: str
    enterprise_cluster_id: Optional[str] = None
    account_connect_org_id: Optional[str] = None
    hosted_org_id: Optional[str] = None
    title: str
    severity: str
    category: str
    description: str
    status: str
    sla_due_at: Optional[str] = None
    response_time_hours: int
    hosted_case_ref: Optional[str] = None
    receipt_id: Optional[str] = None
    support_packet_artifact_id: Optional[str] = None
    assigned_to: Optional[str] = None
    created_by: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class EnterpriseSupportCaseListResponse(BaseModel):
    project_id: str
    items: list[EnterpriseSupportCaseSummary]


class EnterpriseSupportCaseCreateRequest(BaseModel):
    project_id: str
    title: str
    description: str
    severity: str = "normal"
    category: str = "support"
    enterprise_cluster_id: Optional[str] = None
    escalate_to_hosted: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_support_case(self) -> "EnterpriseSupportCaseCreateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        self.title = _require_text(self.title, "title")
        self.description = _require_text(self.description, "description")
        self.severity = _require_text(self.severity, "severity")
        self.category = _require_text(self.category, "category")
        self.actor = _require_text(self.actor, "actor")
        return self


class EnterpriseSupportCaseUpdateRequest(BaseModel):
    project_id: str
    status: Optional[str] = None
    assigned_to: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"

    @model_validator(mode="after")
    def validate_support_case_update(self) -> "EnterpriseSupportCaseUpdateRequest":
        self.project_id = _require_text(self.project_id, "project_id")
        if self.status is not None:
            self.status = _require_text(self.status, "status")
        self.actor = _require_text(self.actor, "actor")
        return self


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
        self.member_type = _require_text(self.member_type, "member_type")
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
        self.member_type = _require_text(self.member_type, "member_type")
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
    job_id: str


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
    risk_tier: int = 1
    data_boundary: DataBoundary = "internal"
    description: Optional[str] = None
    actor: str = "system"


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


class DeploymentRolloutListResponse(BaseModel):
    project_id: str
    items: list[DeploymentRolloutSummary]


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
    kind: str
    status: str
    data_boundary: DataBoundary = "internal"
    priority: int
    max_attempts: int
    attempt: int
    run_after: str


class JobListItem(JobSummary):
    task_title: Optional[str] = None
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


class JobCreateRequest(BaseModel):
    project_id: str
    kind: str
    task_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    data_boundary: Optional[DataBoundary] = None
    requires_reservation: bool = False
    reservation_id: Optional[str] = None
    priority: int = Field(default=100, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    run_after_seconds: int = Field(default=0, ge=0, le=86_400)


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
    kind: str
    payload: dict[str, Any]
    data_boundary: DataBoundary = "internal"
    attempt: int
    max_attempts: int
    lease_token: str
    lease_expires_at: str


class WorkerPollRequest(BaseModel):
    worker_id: str
    capabilities: list[str]
    max_jobs: int = Field(default=1, ge=1, le=1)
    lease_ttl_seconds: int = Field(default=60, ge=5, le=600)


class WorkerPollResponse(BaseModel):
    status: str
    job: Optional[LeasedJob] = None


class WorkerHeartbeatRequest(BaseModel):
    worker_id: str
    job_id: str
    lease_token: str
    lease_ttl_seconds: int = Field(default=60, ge=5, le=600)


class WorkerHeartbeatResponse(BaseModel):
    job_id: str
    status: str
    lease_expires_at: str


class WorkerCompleteRequest(BaseModel):
    worker_id: str
    job_id: str
    lease_token: str
    result: dict[str, Any] = Field(default_factory=dict)


class WorkerCompleteResponse(BaseModel):
    job_id: str
    status: str
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

    @model_validator(mode="after")
    def validate_meeting_outputs(self) -> "MeetingRunRequest":
        if not self.transcript:
            raise ValueError("transcript must contain at least one entry.")
        if not self.minutes:
            raise ValueError("minutes must be a non-empty object.")
        if self.outcome_status == "blocked" and not self.blocked_state:
            raise ValueError("blocked_state is required when outcome_status is blocked.")
        if not self.consensus_reached and not self.dissent_records:
            raise ValueError("dissent_records are required when consensus_reached is false.")
        return self


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
        self.metrics = _require_non_empty_mapping(self.metrics, "metrics")
        self.evidence_refs = _require_non_empty_string_list(
            self.evidence_refs, "evidence_refs"
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
        self.metrics = _require_non_empty_mapping(self.metrics, "metrics")
        self.evidence_refs = _require_non_empty_string_list(
            self.evidence_refs, "evidence_refs"
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
        self.metrics = _require_non_empty_mapping(self.metrics, "metrics")
        self.evidence_refs = _require_non_empty_string_list(
            self.evidence_refs, "evidence_refs"
        )
        return self


class DecisionCard(BaseModel):
    id: str
    task_id: Optional[str] = None
    question: str
    status: str
    risk_tier: int
    default_action: Optional[str] = None
    recommendation: dict[str, Any]
    options: list[dict[str, Any]]
    cost_summary: dict[str, Any]
    deadline_at: Optional[str] = None
    approved_by: Optional[str] = None
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


class DecisionInboxResponse(BaseModel):
    project_id: str
    items: list[DecisionCard]


class DecisionActionRequest(BaseModel):
    actor: str = "executive_console"
    note: Optional[str] = None
    delegate_to: Optional[str] = None
    external_ref: Optional[str] = None


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


class DeadletterItem(BaseModel):
    id: str
    project_id: Optional[str] = None
    source_kind: str
    source_id: str
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
    reservation_id: str
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
    created_at: str


class RoleDetail(RoleSummary):
    spec: dict[str, Any]


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
    ref_kind: str
    ref_key: str
    provider: Optional[str] = None
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
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
    actor: str = "system"


class SecretRefUpdateRequest(BaseModel):
    project_id: str
    provider: Optional[str] = None
    status: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
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


class ProviderAccountSummary(BaseModel):
    id: str
    project_id: str
    provider: str
    account_label: str
    credential_ref: Optional[str] = None
    scopes: list[str]
    status: str
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
    credential_ref: Optional[str] = None
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
    domains: list[str]
    required_resources: list[dict[str, Any]]
    secret_refs: list[str]
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
    secret_refs: list[str] = Field(default_factory=list)
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
    health_check_results: list[dict[str, Any]]
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


AuditExportResponse.model_rebuild()
OpsQueueInspectionResponse.model_rebuild()
ProviderConnectionTestResponse.model_rebuild()
