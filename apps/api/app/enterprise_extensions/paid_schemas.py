"""Request and response models for paid hosted and enterprise routes.

The Community schema module keeps the optional Cloud connection status and
self-hosted identity models; paid route models live only in this extension.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

from boss_engine.schemas import EditionStatusResponse, _require_text


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
