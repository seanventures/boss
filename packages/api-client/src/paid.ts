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
    getCloudCatalog(projectId: string) {
      return request<{
        project_id: string;
        edition_id: string;
        independence: Record<string, unknown>;
        cloud_features: Array<Record<string, unknown>>;
        enterprise_features: Array<Record<string, unknown>>;
        packaging: Record<string, string>;
      }>("GET", "/v1/cloud/catalog", {
        query: { project_id: projectId },
      });
    },
    getCloudStatus(projectId: string) {
      return request<{
        project_id: string;
        edition: EditionStatusResponse;
        independent: boolean;
        linked: boolean;
        easy_connect: Record<string, unknown>;
        managed_workers?: Record<string, unknown>;
        linked_capabilities?: Record<string, unknown>;
        message: string;
      }>("GET", "/v1/cloud/status", {
        query: { project_id: projectId },
      });
    },
    getCloudComparison(projectId: string) {
      return request<{
        project_id: string;
        edition_id: string;
        linked: boolean;
        rows: Array<Record<string, unknown>>;
        marketing_url: string;
        principle: string;
        connect_path: Record<string, unknown>;
      }>("GET", "/v1/cloud/comparison", {
        query: { project_id: projectId },
      });
    },
    getCloudCapabilities(projectId: string) {
      return request<{
        project_id: string;
        edition_id: string;
        linked: boolean;
        local_capabilities: Array<Record<string, unknown>>;
        cloud_unlocked: Array<Record<string, unknown>>;
        cloud_locked: Array<Record<string, unknown>>;
        enterprise_features: Array<Record<string, unknown>>;
        summary: Record<string, unknown>;
      }>("GET", "/v1/cloud/capabilities", {
        query: { project_id: projectId },
      });
    },
    getCloudWorkers(projectId: string) {
      return request<{
        project_id: string;
        status: string;
        available_now: boolean;
        cloud_eligible: boolean;
        linked: boolean;
        local_enrollments?: Array<Record<string, unknown>>;
        managed_capacity: Record<string, unknown>;
        hybrid?: Record<string, unknown>;
        enrollment: Record<string, unknown>;
      }>("GET", "/v1/cloud/workers", {
        query: { project_id: projectId },
      });
    },
    enrollCloudWorker(payload: {
      project_id: string;
      worker_node_id: string;
      display_name?: string;
      capabilities?: string[];
      metadata?: Record<string, unknown>;
      actor?: string;
    }) {
      return request<{
        id: string;
        project_id: string;
        worker_node_id: string;
        status: string;
        receipt_id?: string | null;
        managed_fleets_available: boolean;
        managed_fleet?: boolean;
        message?: string;
      }>("POST", "/v1/cloud/workers/enroll", { body: payload });
    },
    syncCloudEntitlements(payload: {
      project_id: string;
      hosted_org_id?: string;
      actor?: string;
    }) {
      return request<{
        project_id: string;
        synced_entitlement_count: number;
        edition: EditionStatusResponse;
        linked: boolean;
        blocked_reason?: string | null;
      }>("POST", "/v1/cloud/sync", { body: payload });
    },
    listCloudEntitlements(projectId: string, includeInactive = false) {
      return request<{
        project_id: string;
        linked: boolean;
        edition_id: string;
        items: Array<Record<string, unknown>>;
        local_count: number;
        hosted_count: number;
        blocked_reason?: string | null;
        message?: string;
      }>("GET", "/v1/cloud/entitlements", {
        query: {
          project_id: projectId,
          include_inactive: String(includeInactive),
        },
      });
    },
    createCloudTeamInvite(payload: {
      project_id: string;
      email: string;
      display_name?: string;
      role_keys?: string[];
      permissions?: string[];
      metadata?: Record<string, unknown>;
      actor?: string;
    }) {
      return request<Record<string, unknown>>(
        "POST",
        "/v1/cloud/team-invites",
        { body: payload },
      );
    },
    listCloudTeamInvites(projectId: string, limit = 50) {
      return request<{
        project_id: string;
        linked: boolean;
        items: Array<Record<string, unknown>>;
        blocked_reason?: string | null;
      }>("GET", "/v1/cloud/team-invites", {
        query: { project_id: projectId, limit: String(limit) },
      });
    },
    listCloudMarketplace(
      projectId: string,
      options?: { includeInactive?: boolean; limit?: number },
    ) {
      return request<{
        project_id: string;
        linked: boolean;
        items: Array<Record<string, unknown>>;
        blocked_reason?: string | null;
        message?: string | null;
      }>("GET", "/v1/cloud/marketplace", {
        query: {
          project_id: projectId,
          include_inactive: String(options?.includeInactive ?? false),
          limit: String(options?.limit ?? 50),
        },
      });
    },
    startCloudEasyConnect(payload: {
      project_id: string;
      method?: "browser" | "device";
      return_url?: string;
      actor?: string;
    }) {
      return request<{
        session_id: string;
        status: string;
        method: string;
        authorization_url?: string | null;
        user_code?: string | null;
        verification_uri?: string | null;
        device_code?: string | null;
        poll_interval_seconds?: number;
        expires_at?: string | null;
        message?: string | null;
        receipt_id?: string | null;
      }>("POST", "/v1/cloud/easy-connect", { body: payload });
    },
    pollCloudEasyConnect(
      sessionId: string,
      projectId: string,
      actor = "ceo",
    ) {
      return request<Record<string, unknown>>(
        "GET",
        `/v1/cloud/easy-connect/${encodeURIComponent(sessionId)}`,
        { query: { project_id: projectId, actor } },
      );
    },
    completeCloudEasyConnect(
      sessionId: string,
      payload: {
        project_id: string;
        actor?: string;
        code?: string;
        state?: string;
      },
    ) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/cloud/easy-connect/${encodeURIComponent(sessionId)}/complete`,
        { body: payload },
      );
    },
    completeCloudEasyConnectByState(payload: {
      project_id: string;
      actor?: string;
      code?: string;
      state?: string;
      session_id?: string;
    }) {
      return request<Record<string, unknown>>(
        "POST",
        "/v1/cloud/easy-connect/complete",
        { body: payload },
      );
    },
    unlinkCloud(payload: {
      project_id: string;
      actor?: string;
      reason?: string;
    }) {
      return request<{
        project_id: string;
        linked: boolean;
        edition: EditionStatusResponse;
        message: string;
        status: Record<string, unknown>;
      }>("POST", "/v1/cloud/unlink", { body: payload });
    },
    getCloudRetention(projectId: string) {
      return request<{
        project_id: string;
        edition_id: string;
        linked: boolean;
        tier: string;
        retention_days: number | null;
        retention_mode?: string;
        indefinite?: boolean;
        support_sla?: string | null;
        configurable?: boolean;
        source?: string;
        local_receipts?: Record<string, unknown>;
        message?: string | null;
        bounds?: Record<string, number>;
        reason?: string | null;
        updated_by?: string | null;
        updated_at?: string | null;
        receipt_id?: string;
        [key: string]: unknown;
      }>("GET", "/v1/cloud/retention", {
        query: { project_id: projectId },
      });
    },
    putCloudRetention(payload: {
      project_id: string;
      retention_days: number;
      reason?: string;
      actor?: string;
    }) {
      return request<{
        project_id: string;
        linked: boolean;
        tier: string;
        retention_days: number | null;
        support_sla?: string | null;
        receipt_id?: string;
        updated?: boolean;
        message?: string | null;
        [key: string]: unknown;
      }>("PUT", "/v1/cloud/retention", { body: payload });
    },
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
