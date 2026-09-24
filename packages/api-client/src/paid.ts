import { createBossRequest, type BossApiClientConfig, type EditionStatusResponse } from "./community";

export type AccountConnectOrgSummary = {
  id: string;
  project_id: string;
  hosted_org_id: string;
  local_org_id: string;
  connection_mode: string;
  marketplace_identity: Record<string, unknown>;
  billing_portal_ref?: string;
  connector_authorization_refs: string[];
  optional_telemetry_enabled: boolean;
  connection_ref?: string;
  status: string;
  linked_by: string;
  linked_at?: string;
  unlinked_by?: string;
  unlinked_at?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type LicenseEntitlementSummary = {
  id: string;
  project_id: string;
  edition_id: string;
  account_connect_org_id?: string;
  scope_type: string;
  scope_id: string;
  capability_key: string;
  capabilities: string[];
  seats?: number;
  status: string;
  source: string;
  expires_at?: string;
  metadata: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type LicenseEntitlementListResponse = {
  project_id: string;
  edition: EditionStatusResponse;
  items: LicenseEntitlementSummary[];
};

export type AccountConnectLinkRequest = {
  project_id: string;
  hosted_org_id: string;
  connection_ref?: string;
  marketplace_identity?: Record<string, unknown>;
  billing_portal_ref?: string;
  connector_authorization_refs?: string[];
  optional_telemetry_enabled?: boolean;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type AccountConnectUnlinkRequest = {
  project_id: string;
  hosted_org_id?: string;
  reason?: string;
  actor?: string;
};

export type AccountConnectMutationResponse = {
  project_id: string;
  edition: EditionStatusResponse;
};

export type AccountConnectSyncRequest = {
  project_id: string;
  hosted_org_id?: string;
  actor?: string;
};

export type AccountConnectSyncResponse = {
  project_id: string;
  edition: EditionStatusResponse;
  synced_entitlement_count: number;
};

export type AccountConnectHealthCheckSummary = {
  id: string;
  project_id: string;
  account_connect_org_id?: string;
  hosted_org_id?: string;
  check_kind: string;
  status: string;
  latency_ms?: number;
  provider_ref?: string;
  readiness_packet_artifact_id?: string;
  response_metadata: Record<string, unknown>;
  error?: Record<string, unknown>;
  checked_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type AccountConnectHealthCheckListResponse = {
  project_id: string;
  items: AccountConnectHealthCheckSummary[];
};

export type AccountConnectHealthCheckRunRequest = {
  project_id: string;
  hosted_org_id?: string;
  check_kind?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type AccountConnectAuthSessionSummary = {
  id: string;
  project_id: string;
  account_connect_org_id?: string;
  hosted_org_id?: string;
  authorization_url?: string;
  return_url?: string;
  provider_ref?: string;
  state: string;
  status: string;
  receipt_id?: string;
  created_by: string;
  expires_at?: string;
  completed_at?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type AccountConnectAuthSessionListResponse = {
  project_id: string;
  items: AccountConnectAuthSessionSummary[];
};

export type AccountConnectAuthSessionCreateRequest = {
  project_id: string;
  hosted_org_id?: string;
  return_url?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type AccountConnectAuthSessionCompleteRequest = {
  project_id: string;
  code: string;
  state: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type AccountConnectTeamInviteSummary = {
  id: string;
  project_id: string;
  account_connect_org_id?: string;
  hosted_org_id: string;
  email: string;
  display_name?: string;
  role_keys: string[];
  permissions: string[];
  invite_url?: string;
  hosted_invite_ref?: string;
  status: string;
  delivery_status: string;
  receipt_id?: string;
  created_by: string;
  accepted_by?: string;
  accepted_at?: string;
  revoked_by?: string;
  revoked_at?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type AccountConnectTeamInviteListResponse = {
  project_id: string;
  items: AccountConnectTeamInviteSummary[];
};

export type AccountConnectTeamInviteCreateRequest = {
  project_id: string;
  email: string;
  display_name?: string;
  role_keys?: string[];
  permissions?: string[];
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type AccountConnectTeamInviteRevokeRequest = {
  project_id: string;
  reason?: string;
  actor?: string;
};

export type AccountConnectBillingPortalSessionSummary = {
  id: string;
  project_id: string;
  account_connect_org_id?: string;
  hosted_org_id: string;
  portal_url?: string;
  return_url?: string;
  provider_ref?: string;
  status: string;
  receipt_id?: string;
  created_by: string;
  expires_at?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type AccountConnectBillingPortalSessionListResponse = {
  project_id: string;
  items: AccountConnectBillingPortalSessionSummary[];
};

export type AccountConnectBillingPortalSessionCreateRequest = {
  project_id: string;
  return_url?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type EnterpriseClusterSummary = {
  id: string;
  org_id: string;
  project_id: string;
  deployment_mode: string;
  account_connect_mode: string;
  worker_pool_strategy: string;
  ha_profile: string;
  data_residency: string;
  identity_controls: Record<string, unknown>;
  governance_controls: Record<string, unknown>;
  audit_controls: Record<string, unknown>;
  private_worker_pools: Array<Record<string, unknown>>;
  compliance_packs: string[];
  support_tier: string;
  support_sla: Record<string, unknown>;
  status: string;
  blocking_gaps: string[];
  created_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type EnterpriseClusterListResponse = {
  project_id: string;
  items: EnterpriseClusterSummary[];
};

export type EnterpriseClusterValidationRunSummary = {
  id: string;
  org_id: string;
  project_id: string;
  enterprise_cluster_id: string;
  validation_kind: string;
  status: string;
  checks: Array<Record<string, unknown>>;
  evidence_refs: string[];
  blocking_gaps: string[];
  validated_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type EnterpriseClusterValidationRunListResponse = {
  project_id: string;
  items: EnterpriseClusterValidationRunSummary[];
};

export type EnterpriseClusterValidationRunCreateRequest = {
  project_id: string;
  enterprise_cluster_id: string;
  validation_kind: string;
  status?: string;
  checks: Array<Record<string, unknown>>;
  evidence_refs?: string[];
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type EnterpriseClusterUpsertRequest = {
  project_id: string;
  deployment_mode?: string;
  account_connect_mode?: string;
  worker_pool_strategy?: string;
  ha_profile?: string;
  data_residency?: string;
  identity_controls?: Record<string, unknown>;
  governance_controls?: Record<string, unknown>;
  audit_controls?: Record<string, unknown>;
  private_worker_pools?: Array<Record<string, unknown>>;
  compliance_packs?: string[];
  support_tier?: string;
  support_sla?: Record<string, unknown>;
  status?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type EnterpriseSupportCaseSummary = {
  id: string;
  org_id: string;
  project_id: string;
  enterprise_cluster_id?: string;
  account_connect_org_id?: string;
  hosted_org_id?: string;
  title: string;
  severity: string;
  category: string;
  description: string;
  status: string;
  sla_due_at?: string;
  response_time_hours: number;
  hosted_case_ref?: string;
  receipt_id?: string;
  support_packet_artifact_id?: string;
  assigned_to?: string;
  created_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type EnterpriseSupportCaseListResponse = {
  project_id: string;
  items: EnterpriseSupportCaseSummary[];
};

export type EnterpriseSupportCaseCreateRequest = {
  project_id: string;
  title: string;
  description: string;
  severity?: string;
  category?: string;
  enterprise_cluster_id?: string;
  escalate_to_hosted?: boolean;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type EnterpriseSupportCaseUpdateRequest = {
  project_id: string;
  status?: string;
  assigned_to?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type HostedMarketplaceListingSummary = {
  id: string;
  project_id: string;
  account_connect_org_id?: string;
  hosted_org_id?: string;
  listing_id: string;
  pack_id: string;
  version: string;
  display_name: string;
  publisher: string;
  category: string;
  description?: string;
  status: string;
  price_model?: string;
  price_usd_micros?: number;
  required_entitlements: string[];
  capabilities: string[];
  source_url?: string;
  metadata: Record<string, unknown>;
  last_sync_run_id?: string;
  created_at: string;
  updated_at: string;
};

export type HostedMarketplaceListingListResponse = {
  project_id: string;
  items: HostedMarketplaceListingSummary[];
};

export type HostedMarketplaceSyncRunSummary = {
  id: string;
  project_id: string;
  account_connect_org_id?: string;
  hosted_org_id?: string;
  status: string;
  listing_count: number;
  provider_ref?: string;
  sync_packet_artifact_id?: string;
  error?: Record<string, unknown>;
  synced_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type HostedMarketplaceSyncRunListResponse = {
  project_id: string;
  items: HostedMarketplaceSyncRunSummary[];
};

export type HostedMarketplaceSyncRequest = {
  project_id: string;
  hosted_org_id?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type HostedMarketplaceSyncResponse = {
  project_id: string;
  sync_run: HostedMarketplaceSyncRunSummary;
  listing_count: number;
};

declare module "./community" {
  interface EditionStatusResponse {
    account_connect_required: boolean;
    account_connect_status: string;
    account_connect_org?: AccountConnectOrgSummary;
    hosted_auth_required: boolean;
    license_candidate?: string;
    legal_review_required: boolean;
    commercial_capabilities: string[];
  }
}

/** Methods available only when the private paid extension is installed. */
export function createPaidBossApiMethods(config: BossApiClientConfig) {
  const request = createBossRequest(config);
  return {
    listLicenseEntitlements(projectId: string, includeInactive = false) {
      return request<LicenseEntitlementListResponse>("GET", "/v1/entitlements", {
        query: {
          project_id: projectId,
          include_inactive: String(includeInactive),
        },
      });
    },
    linkAccountConnect(payload: AccountConnectLinkRequest) {
      return request<AccountConnectMutationResponse>(
        "POST",
        "/v1/account-connect/link",
        { body: payload },
      );
    },
    unlinkAccountConnect(payload: AccountConnectUnlinkRequest) {
      return request<AccountConnectMutationResponse>(
        "POST",
        "/v1/account-connect/unlink",
        { body: payload },
      );
    },
    syncAccountConnect(payload: AccountConnectSyncRequest) {
      return request<AccountConnectSyncResponse>(
        "POST",
        "/v1/account-connect/sync",
        { body: payload },
      );
    },
    listAccountConnectHealthChecks(projectId: string, limit = 50) {
      return request<AccountConnectHealthCheckListResponse>(
        "GET",
        "/v1/account-connect/health-checks",
        { query: { project_id: projectId, limit: String(limit) } },
      );
    },
    runAccountConnectHealthCheck(payload: AccountConnectHealthCheckRunRequest) {
      return request<AccountConnectHealthCheckSummary>(
        "POST",
        "/v1/account-connect/health-checks",
        { body: payload },
      );
    },
    listAccountConnectAuthSessions(projectId: string, limit = 50) {
      return request<AccountConnectAuthSessionListResponse>(
        "GET",
        "/v1/account-connect/auth-sessions",
        { query: { project_id: projectId, limit: String(limit) } },
      );
    },
    createAccountConnectAuthSession(
      payload: AccountConnectAuthSessionCreateRequest,
    ) {
      return request<AccountConnectAuthSessionSummary>(
        "POST",
        "/v1/account-connect/auth-sessions",
        { body: payload },
      );
    },
    completeAccountConnectAuthSession(
      sessionId: string,
      payload: AccountConnectAuthSessionCompleteRequest,
    ) {
      return request<AccountConnectAuthSessionSummary>(
        "POST",
        `/v1/account-connect/auth-sessions/${encodeURIComponent(sessionId)}/complete`,
        { body: payload },
      );
    },
    listAccountConnectTeamInvites(projectId: string, limit = 50) {
      return request<AccountConnectTeamInviteListResponse>(
        "GET",
        "/v1/account-connect/team-invites",
        { query: { project_id: projectId, limit: String(limit) } },
      );
    },
    createAccountConnectTeamInvite(
      payload: AccountConnectTeamInviteCreateRequest,
    ) {
      return request<AccountConnectTeamInviteSummary>(
        "POST",
        "/v1/account-connect/team-invites",
        { body: payload },
      );
    },
    revokeAccountConnectTeamInvite(
      inviteId: string,
      payload: AccountConnectTeamInviteRevokeRequest,
    ) {
      return request<AccountConnectTeamInviteSummary>(
        "POST",
        `/v1/account-connect/team-invites/${encodeURIComponent(inviteId)}/revoke`,
        { body: payload },
      );
    },
    listAccountConnectBillingPortalSessions(projectId: string, limit = 50) {
      return request<AccountConnectBillingPortalSessionListResponse>(
        "GET",
        "/v1/account-connect/billing-portal-sessions",
        { query: { project_id: projectId, limit: String(limit) } },
      );
    },
    createAccountConnectBillingPortalSession(
      payload: AccountConnectBillingPortalSessionCreateRequest,
    ) {
      return request<AccountConnectBillingPortalSessionSummary>(
        "POST",
        "/v1/account-connect/billing-portal-sessions",
        { body: payload },
      );
    },
    listEnterpriseClusters(projectId: string) {
      return request<EnterpriseClusterListResponse>(
        "GET",
        "/v1/enterprise/clusters",
        { query: { project_id: projectId } },
      );
    },
    upsertEnterpriseCluster(payload: EnterpriseClusterUpsertRequest) {
      return request<EnterpriseClusterSummary>(
        "POST",
        "/v1/enterprise/clusters",
        { body: payload },
      );
    },
    listEnterpriseClusterValidationRuns(
      projectId: string,
      enterpriseClusterId?: string,
      limit = 50,
    ) {
      return request<EnterpriseClusterValidationRunListResponse>(
        "GET",
        "/v1/enterprise/cluster-validation-runs",
        {
          query: {
            project_id: projectId,
            ...(enterpriseClusterId
              ? { enterprise_cluster_id: enterpriseClusterId }
              : {}),
            limit,
          },
        },
      );
    },
    createEnterpriseClusterValidationRun(
      payload: EnterpriseClusterValidationRunCreateRequest,
    ) {
      return request<EnterpriseClusterValidationRunSummary>(
        "POST",
        "/v1/enterprise/cluster-validation-runs",
        { body: payload },
      );
    },
    listEnterpriseSupportCases(projectId: string, limit = 50) {
      return request<EnterpriseSupportCaseListResponse>(
        "GET",
        "/v1/enterprise/support-cases",
        { query: { project_id: projectId, limit } },
      );
    },
    createEnterpriseSupportCase(payload: EnterpriseSupportCaseCreateRequest) {
      return request<EnterpriseSupportCaseSummary>(
        "POST",
        "/v1/enterprise/support-cases",
        { body: payload },
      );
    },
    updateEnterpriseSupportCase(
      caseId: string,
      payload: EnterpriseSupportCaseUpdateRequest,
    ) {
      return request<EnterpriseSupportCaseSummary>(
        "POST",
        `/v1/enterprise/support-cases/${encodeURIComponent(caseId)}`,
        { body: payload },
      );
    },
    listHostedMarketplaceListings(
      projectId: string,
      includeInactive = false,
      limit = 50,
    ) {
      return request<HostedMarketplaceListingListResponse>(
        "GET",
        "/v1/marketplace/listings",
        {
          query: {
            project_id: projectId,
            include_inactive: includeInactive,
            limit: String(limit),
          },
        },
      );
    },
    listHostedMarketplaceSyncRuns(projectId: string, limit = 50) {
      return request<HostedMarketplaceSyncRunListResponse>(
        "GET",
        "/v1/marketplace/sync-runs",
        {
          query: { project_id: projectId, limit: String(limit) },
        },
      );
    },
    syncHostedMarketplace(payload: HostedMarketplaceSyncRequest) {
      return request<HostedMarketplaceSyncResponse>(
        "POST",
        "/v1/marketplace/sync",
        { body: payload },
      );
    }
  };
}
