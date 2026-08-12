import { describe, expect, it, vi } from "vitest";

const defaultCloudCatalog = {
  project_id: "boss-dogfood",
  edition_id: "community",
  independence: {
    community_runs_without_cloud: true,
    account_connect_required_for_local_work: false,
    easy_connect_available: true,
  },
  cloud_features: [
    {
      key: "durable_control_plane",
      title: "Durable task & decision state",
      status: "shipped",
      available_now: true,
      community_local: true,
    },
    {
      key: "managed_workers",
      title: "Managed workers & elastic infra",
      status: "planned",
      available_now: false,
      community_local: false,
      unlock: "connect_cloud",
    },
  ],
  enterprise_features: [],
  packaging: {
    community: "Self-host",
    cloud: "Managed",
    enterprise: "Identity + fleets",
  },
};

const defaultCloudStatus = {
  project_id: "boss-dogfood",
  edition: {
    project_id: "boss-dogfood",
    edition_id: "community",
    display_name: "BOSS Community",
    status: "active",
    account_connect_required: false,
    account_connect_status: "not_required",
    local_auth_allowed: true,
    hosted_auth_required: false,
    connection_mode: "disabled",
    legal_review_required: false,
    included_capabilities: [],
    commercial_capabilities: [],
    source_pack: "community_edition.yaml",
  },
  independent: true,
  linked: false,
  easy_connect: {
    ready: false,
    methods: ["browser", "device"],
    blocked_reason: "BOSS_ACCOUNT_CONNECT_URL is not configured",
    pending_sessions: [],
  },
  managed_workers: {
    status: "planned",
    available_now: false,
    managed_capacity: { message: "planned" },
  },
  message: "This install runs independently.",
};

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    getEdition: vi.fn(),
    listLicenseEntitlements: vi.fn(),
    getCloudCatalog: vi.fn(),
    getCloudStatus: vi.fn(),
    getCloudComparison: vi.fn(),
    getCloudCapabilities: vi.fn(),
    getCloudWorkers: vi.fn(),
    listAccountConnectHealthChecks: vi.fn(),
    listAccountConnectAuthSessions: vi.fn(),
    listAccountConnectTeamInvites: vi.fn(),
    listAccountConnectBillingPortalSessions: vi.fn(),
    listEnterpriseClusters: vi.fn(),
    listEnterpriseClusterValidationRuns: vi.fn(),
    listEnterpriseIdentityProviders: vi.fn(),
    listEnterpriseIdentityRolloutPolicies: vi.fn(),
    listEnterpriseScimGroups: vi.fn(),
    listEnterpriseScimUsers: vi.fn(),
    listEnterpriseSupportCases: vi.fn(),
    listAgentModules: vi.fn(),
    listAgentModuleInstallRecords: vi.fn(),
    listHostedMarketplaceListings: vi.fn(),
    listHostedMarketplaceSyncRuns: vi.fn(),
  },
}));

describe("commercial, enterprise, and marketplace feeds", async () => {
  const { client } = await import("../lib/client");
  const { fetchCommercialFeed } = await import("../lib/commercial");
  const { fetchEnterpriseFeed } = await import("../lib/enterprise");
  const { fetchAgentModuleFeed } = await import("../lib/agent-modules");

  function mockCloudSurfaces(
    overrides: {
      catalog?: typeof defaultCloudCatalog;
      status?: typeof defaultCloudStatus;
    } = {},
  ) {
    vi.mocked(client.getCloudCatalog).mockResolvedValueOnce(
      (overrides.catalog ?? defaultCloudCatalog) as never,
    );
    vi.mocked(client.getCloudStatus).mockResolvedValueOnce(
      (overrides.status ?? defaultCloudStatus) as never,
    );
    vi.mocked(client.getCloudComparison).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition_id: "community",
      linked: false,
      rows: [
        {
          capability: "BYOK models & infra keys (SecretRefs)",
          community: "yes",
          cloud: "yes",
          enterprise: "yes",
          status: "shipped",
        },
      ],
      marketing_url: "https://boss.sean.network/#cloud",
      principle: "Community is real.",
      connect_path: { methods: ["browser", "device"] },
    } as never);
    vi.mocked(client.getCloudCapabilities).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition_id: "community",
      linked: false,
      local_capabilities: [
        { key: "durable_control_plane", title: "Durable", status: "shipped" },
      ],
      cloud_unlocked: [],
      cloud_locked: [
        { key: "managed_workers", title: "Workers", status: "planned" },
      ],
      enterprise_features: [],
      summary: {
        local_count: 1,
        cloud_unlocked_count: 0,
        cloud_locked_count: 1,
        independent_without_cloud: true,
      },
    } as never);
    vi.mocked(client.getCloudWorkers).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      status: "planned",
      available_now: false,
      cloud_eligible: false,
      linked: false,
      managed_capacity: { message: "planned" },
      enrollment: { ready: false },
    } as never);
  }

  it("maps edition, Account Connect, and license entitlements", async () => {
    mockCloudSurfaces();
    vi.mocked(client.getEdition).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition_id: "cloud",
      display_name: "BOSS Cloud",
      status: "active",
      account_connect_required: true,
      account_connect_status: "linked",
      account_connect_org: {
        id: "acctconn_1",
        project_id: "boss-dogfood",
        hosted_org_id: "hosted_org_1",
        local_org_id: "org_boss",
        connection_mode: "cloud_required",
        marketplace_identity: { publisher: "seanheiney" },
        billing_portal_ref: "stripe:portal:test",
        connector_authorization_refs: ["github:installation:test"],
        optional_telemetry_enabled: true,
        connection_ref: "secretref:account-connect",
        status: "linked",
        linked_by: "ceo",
        linked_at: "2026-06-03T00:00:00Z",
        metadata: {},
        created_at: "2026-06-03T00:00:00Z",
        updated_at: "2026-06-03T00:00:00Z",
      },
      local_auth_allowed: false,
      hosted_auth_required: true,
      connection_mode: "cloud_required",
      license_candidate: "source-available-commercial",
      legal_review_required: true,
      included_capabilities: ["cloud_auth"],
      commercial_capabilities: ["cloud_auth", "marketplace_install"],
      source_pack: "cloud_edition.yaml",
    });
    vi.mocked(client.listLicenseEntitlements).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition: {} as never,
      items: [
        {
          id: "lic_1",
          project_id: "boss-dogfood",
          edition_id: "cloud",
          account_connect_org_id: "acctconn_1",
          scope_type: "project",
          scope_id: "boss-dogfood",
          capability_key: "private_agent_marketplace",
          capabilities: ["private_agent_marketplace"],
          seats: 5,
          status: "active",
          source: "account_connect",
          metadata: {},
        },
      ],
    });
    vi.mocked(client.listAccountConnectHealthChecks).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "accthealth_1",
          project_id: "boss-dogfood",
          account_connect_org_id: "acctconn_1",
          hosted_org_id: "hosted_org_1",
          check_kind: "hosted_service",
          status: "healthy",
          latency_ms: 19,
          provider_ref: "acct-health-1",
          readiness_packet_artifact_id: "art_acct_readiness_1",
          response_metadata: { status: "healthy" },
          checked_by: "ceo",
          metadata: {},
          created_at: "2026-06-03T00:00:00Z",
        },
      ],
    });
    vi.mocked(client.listAccountConnectAuthSessions).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "acctauth_1",
          project_id: "boss-dogfood",
          account_connect_org_id: "acctconn_1",
          hosted_org_id: "hosted_org_1",
          authorization_url: "https://accounts.example/oauth/authorize",
          return_url: "https://boss.local/account-connect/callback",
          provider_ref: "hosted_auth_1",
          state: "acctstate_1",
          status: "ready",
          receipt_id: "rcpt_auth",
          created_by: "ceo",
          expires_at: "2026-06-04T00:00:00Z",
          metadata: {},
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T00:00:00Z",
        },
      ],
    });
    vi.mocked(client.listAccountConnectTeamInvites).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "acctinvite_1",
          project_id: "boss-dogfood",
          account_connect_org_id: "acctconn_1",
          hosted_org_id: "hosted_org_1",
          email: "ops@example.com",
          display_name: "Ops Lead",
          role_keys: ["operator"],
          permissions: ["command_submit"],
          invite_url: "https://accounts.example/invite/1",
          hosted_invite_ref: "hosted_invite_1",
          status: "pending_acceptance",
          delivery_status: "sent",
          receipt_id: "rcpt_invite",
          created_by: "ceo",
          metadata: {},
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T00:00:00Z",
        },
      ],
    });
    vi.mocked(client.listAccountConnectBillingPortalSessions).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "acctbill_1",
          project_id: "boss-dogfood",
          account_connect_org_id: "acctconn_1",
          hosted_org_id: "hosted_org_1",
          portal_url: "https://billing.example/session/1",
          return_url: "https://boss.local/settings",
          provider_ref: "bps_1",
          status: "ready",
          receipt_id: "rcpt_billing",
          created_by: "ceo",
          expires_at: "2026-06-04T00:00:00Z",
          metadata: {},
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T00:00:00Z",
        },
      ],
    });

    const feed = await fetchCommercialFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(true);
    expect(feed.edition?.accountConnectStatus).toBe("linked");
    expect(feed.edition?.accountConnectOrg?.hostedOrgId).toBe("hosted_org_1");
    expect(feed.entitlements[0]).toMatchObject({
      capabilityKey: "private_agent_marketplace",
      seats: 5,
    });
    expect(feed.healthChecks[0]).toMatchObject({
      status: "healthy",
      providerRef: "acct-health-1",
      latencyMs: 19,
      readinessPacketArtifactId: "art_acct_readiness_1",
    });
    expect(feed.authSessions[0]).toMatchObject({
      providerRef: "hosted_auth_1",
      authorizationUrl: "https://accounts.example/oauth/authorize",
      status: "ready",
    });
    expect(feed.teamInvites[0]).toMatchObject({
      email: "ops@example.com",
      receiptId: "rcpt_invite",
      deliveryStatus: "sent",
    });
    expect(feed.billingPortalSessions[0]).toMatchObject({
      providerRef: "bps_1",
      portalUrl: "https://billing.example/session/1",
      status: "ready",
    });
    expect(feed.cloud?.independent).toBe(true);
    expect(feed.cloud?.features.some((f) => f.key === "durable_control_plane")).toBe(
      true,
    );
  });

  it("maps community edition (local, no hosted required) and falls back to edition-pack entitlements", async () => {
    mockCloudSurfaces();
    vi.mocked(client.getEdition).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition_id: "community",
      display_name: "BOSS Community",
      status: "active",
      account_connect_required: false,
      account_connect_status: "not_required",
      account_connect_org: undefined,
      local_auth_allowed: true,
      hosted_auth_required: false,
      connection_mode: "disabled",
      license_candidate: "open-source-candidate",
      legal_review_required: false,
      included_capabilities: ["core_control_plane", "local_workers"],
      commercial_capabilities: ["core_control_plane"],
      source_pack: "community_edition.yaml",
      blocked_reason: undefined,
    });
    vi.mocked(client.listLicenseEntitlements).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition: {} as never,
      items: [
        {
          id: "ent_local_boss-dogfood_core_control_plane",
          project_id: "boss-dogfood",
          edition_id: "community",
          account_connect_org_id: undefined,
          scope_type: "project",
          scope_id: "boss-dogfood",
          capability_key: "core_control_plane",
          capabilities: ["core_control_plane", "local_workers"],
          status: "active",
          source: "edition_pack",
          metadata: { source_pack: "community_edition.yaml" },
        },
      ],
    });
    // other lists empty for community blocked-hosted
    vi.mocked(client.listAccountConnectHealthChecks).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    vi.mocked(client.listAccountConnectAuthSessions).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    vi.mocked(client.listAccountConnectTeamInvites).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    vi.mocked(client.listAccountConnectBillingPortalSessions).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });

    const feed = await fetchCommercialFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(true);
    expect(feed.edition?.editionId).toBe("community");
    expect(feed.edition?.localAuthAllowed).toBe(true);
    expect(feed.edition?.accountConnectRequired).toBe(false);
    expect(feed.edition?.status).toBe("active");
    expect(feed.entitlements[0]).toMatchObject({ capabilityKey: "core_control_plane", source: "edition_pack" });
    expect(feed.healthChecks).toEqual([]);
    expect(feed.authSessions).toEqual([]);
    expect(feed.cloud?.independent).toBe(true);
    expect(feed.cloud?.managedWorkers.status).toBe("planned");
  });

  it("maps blocked cloud edition (account_connect_required, not linked) with empty hosted lists + blockedReason", async () => {
    mockCloudSurfaces({
      status: {
        ...defaultCloudStatus,
        independent: false,
        linked: false,
        message: "account_connect_required",
        edition: {
          ...defaultCloudStatus.edition,
          edition_id: "cloud",
          display_name: "BOSS Cloud",
          status: "blocked_account_connect_required",
          account_connect_required: true,
          account_connect_status: "not_linked",
          local_auth_allowed: false,
          hosted_auth_required: true,
          connection_mode: "cloud_required",
          source_pack: "cloud_edition.yaml",
        },
      },
    });
    vi.mocked(client.getEdition).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition_id: "cloud",
      display_name: "BOSS Cloud",
      status: "blocked_account_connect_required",
      account_connect_required: true,
      account_connect_status: "not_linked",
      account_connect_org: undefined,
      local_auth_allowed: false,
      hosted_auth_required: true,
      connection_mode: "cloud_required",
      license_candidate: "source-available-commercial",
      legal_review_required: true,
      included_capabilities: ["cloud_auth"],
      commercial_capabilities: ["cloud_auth", "marketplace_install"],
      source_pack: "cloud_edition.yaml",
      blocked_reason: "account_connect_required",
    });
    vi.mocked(client.listLicenseEntitlements).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition: {} as never,
      items: [], // hosted not linked, only would have from edition but status blocked
    });
    vi.mocked(client.listAccountConnectHealthChecks).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    vi.mocked(client.listAccountConnectAuthSessions).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    vi.mocked(client.listAccountConnectTeamInvites).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    vi.mocked(client.listAccountConnectBillingPortalSessions).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });

    const feed = await fetchCommercialFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(true);
    expect(feed.edition?.status).toBe("blocked_account_connect_required");
    expect(feed.edition?.blockedReason).toBe("account_connect_required");
    expect(feed.edition?.accountConnectStatus).toBe("not_linked");
    expect(feed.entitlements).toEqual([]);
    expect(feed.cloud?.linked).toBe(false);
    // marketplace would be blocked too (see agent-modules + commercial context)
  });

  it("handles marketplace blocked via commercial edition context + empty agent module marketplace feeds", async () => {
    const { fetchAgentModuleFeed } = await import("../lib/agent-modules");
    // reuse client mock; simulate reachable but no hosted due to no link (blocked)
    vi.mocked(client.listAgentModules).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    vi.mocked(client.listAgentModuleInstallRecords).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    vi.mocked(client.listHostedMarketplaceListings).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    vi.mocked(client.listHostedMarketplaceSyncRuns).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });

    const feed = await fetchAgentModuleFeed("boss-dogfood");
    expect(feed.apiReachable).toBe(true);
    expect(feed.hostedListings).toEqual([]);
    expect(feed.marketplaceSyncRuns).toEqual([]);
    // UI polish renders "No hosted... (Hosted catalog requires...)" using commercial.edition.blocked
  });

  it("maps commercial feed error to empty blocked state (for marketplace / acct connect surfaces)", async () => {
    vi.mocked(client.getEdition).mockRejectedValueOnce(new Error("commercial api down"));
    vi.mocked(client.listLicenseEntitlements).mockRejectedValueOnce(new Error("entitlements down"));
    vi.mocked(client.getCloudCatalog).mockRejectedValueOnce(new Error("cloud catalog down"));
    vi.mocked(client.getCloudStatus).mockRejectedValueOnce(new Error("cloud status down"));
    vi.mocked(client.getCloudComparison).mockRejectedValueOnce(new Error("cloud comparison down"));
    vi.mocked(client.getCloudCapabilities).mockRejectedValueOnce(new Error("cloud caps down"));
    vi.mocked(client.getCloudWorkers).mockRejectedValueOnce(new Error("cloud workers down"));
    vi.mocked(client.listAccountConnectHealthChecks).mockRejectedValueOnce(new Error("health down"));
    vi.mocked(client.listAccountConnectAuthSessions).mockRejectedValueOnce(new Error("auth down"));
    vi.mocked(client.listAccountConnectTeamInvites).mockRejectedValueOnce(new Error("invites down"));
    vi.mocked(client.listAccountConnectBillingPortalSessions).mockRejectedValueOnce(new Error("billing down"));

    const feed = await fetchCommercialFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(false);
    expect(feed.blockedReason).toContain("commercial api down");
    expect(feed.edition).toBeUndefined();
    expect(feed.entitlements).toEqual([]);
    expect(feed.healthChecks).toEqual([]);
    // ensures UI shows blocked for edition/packs/marketplace/account-connect
  });

  it("maps enterprise cluster, SCIM, and support/SLA state", async () => {
    vi.mocked(client.listEnterpriseClusters).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "cluster_1",
          org_id: "org_boss",
          project_id: "boss-dogfood",
          deployment_mode: "enterprise_cloud",
          account_connect_mode: "attached",
          worker_pool_strategy: "dedicated_managed_workers",
          ha_profile: "regional_ha",
          data_residency: "us",
          identity_controls: { sso_oidc_configured: true },
          governance_controls: { budget_governor_precedence: true },
          audit_controls: { audit_export_enabled: true },
          private_worker_pools: [{ pool: "managed" }],
          compliance_packs: ["soc2"],
          support_tier: "enterprise",
          support_sla: { response_hours: 4 },
          status: "planned",
          blocking_gaps: ["scim_config_missing"],
          created_by: "ceo",
          metadata: {},
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T01:00:00Z",
        },
      ],
    });
    vi.mocked(client.listEnterpriseClusterValidationRuns).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "entclval_1",
          org_id: "org_boss",
          project_id: "boss-dogfood",
          enterprise_cluster_id: "cluster_1",
          validation_kind: "multi_node_readiness",
          status: "blocked",
          checks: [
            {
              name: "private_worker_pool",
              status: "blocked",
              required: true,
            },
          ],
          evidence_refs: ["artifact:k8s_validate"],
          blocking_gaps: ["cluster_validation_private_worker_pool_blocked"],
          validated_by: "ceo",
          metadata: {
            validation_packet_artifact_id: "art_cluster_packet_1",
            blocking_decision_id: "dec_cluster_block_1",
            live_rollout_claim: false,
          },
          created_at: "2026-06-03T01:00:00Z",
        },
      ],
    });
    vi.mocked(client.listEnterpriseIdentityProviders).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "entidp_boss-dogfood_okta_oidc",
          org_id: "org_boss",
          project_id: "boss-dogfood",
          provider_key: "okta_oidc",
          provider_type: "oidc",
          display_name: "Okta OIDC",
          issuer: "https://idp.example",
          audience: "boss-api",
          jwks_url: "https://idp.example/.well-known/jwks.json",
          status: "active",
          test_status: "not_tested",
          blocking_gaps: [],
          metadata: {},
          created_by: "ceo",
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T01:00:00Z",
        },
        {
          id: "entidp_boss-dogfood_saml_pending",
          org_id: "org_boss",
          project_id: "boss-dogfood",
          provider_key: "saml_pending",
          provider_type: "saml",
          display_name: "Pending SAML",
          status: "blocked",
          test_status: "not_tested",
          blocking_gaps: ["saml_sso_url_missing"],
          metadata: {},
          created_by: "ceo",
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T01:00:00Z",
        },
      ],
    });
    vi.mocked(client.listEnterpriseIdentityRolloutPolicies).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "entidpol_boss-dogfood_saml",
          org_id: "org_boss",
          project_id: "boss-dogfood",
          provider_type: "saml",
          runtime_mode: "auth_allowed",
          allowed_domains: ["example.com"],
          rollout_notes: "Dogfood SAML approved.",
          status: "active",
          created_by: "ceo",
          metadata: {},
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T01:00:00Z",
        },
      ],
    });
    vi.mocked(client.listEnterpriseScimGroups).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "group_1",
          org_id: "org_boss",
          project_id: "boss-dogfood",
          external_id: "okta_group_1",
          display_name: "Operators",
          role_keys: ["operator"],
          permissions: ["command_submit"],
          raw_group: {},
          status: "active",
          created_by: "ceo",
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T01:00:00Z",
        },
      ],
    });
    vi.mocked(client.listEnterpriseScimUsers).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "user_1",
          org_id: "org_boss",
          project_id: "boss-dogfood",
          external_id: "okta_user_1",
          user_name: "ops@example.com",
          display_name: "Ops User",
          member_id: "scim_ops_example_com",
          role_keys: ["operator"],
          permissions: ["command_submit"],
          groups: ["Operators"],
          raw_profile: {},
          status: "active",
          active: true,
          created_by: "ceo",
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T01:00:00Z",
        },
      ],
    });
    vi.mocked(client.listEnterpriseSupportCases).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "entsup_1",
          org_id: "org_boss",
          project_id: "boss-dogfood",
          enterprise_cluster_id: "cluster_1",
          account_connect_org_id: "acctconn_1",
          hosted_org_id: "hosted_org_1",
          title: "Hosted escalation blocked",
          severity: "high",
          category: "support_sla",
          description: "Account Connect URL is not configured.",
          status: "blocked_configuration",
          sla_due_at: "2026-06-03T05:00:00Z",
          response_time_hours: 4,
          hosted_case_ref: undefined,
          receipt_id: "rcpt_support_1",
          support_packet_artifact_id: "art_support_packet_1",
          assigned_to: undefined,
          created_by: "ceo",
          metadata: { blocked_reason: "account_connect_url_missing" },
          created_at: "2026-06-03T01:00:00Z",
          updated_at: "2026-06-03T01:00:00Z",
        },
        {
          id: "entsup_2",
          org_id: "org_boss",
          project_id: "boss-dogfood",
          title: "Local SLA review",
          severity: "normal",
          category: "operational_trust",
          description: "Track local enterprise support readiness.",
          status: "resolved",
          sla_due_at: "2026-06-04T01:00:00Z",
          response_time_hours: 24,
          receipt_id: undefined,
          assigned_to: "cto",
          created_by: "ceo",
          metadata: {},
          created_at: "2026-06-03T01:00:00Z",
          updated_at: "2026-06-03T02:00:00Z",
        },
      ],
    });

    const feed = await fetchEnterpriseFeed("boss-dogfood");

    expect(feed.summary.clusterCount).toBe(1);
    expect(feed.clusterValidationRuns[0]).toMatchObject({
      validationKind: "multi_node_readiness",
      status: "blocked",
      evidenceRefs: ["artifact:k8s_validate"],
      metadata: {
        validation_packet_artifact_id: "art_cluster_packet_1",
        blocking_decision_id: "dec_cluster_block_1",
        live_rollout_claim: false,
      },
    });
    expect(feed.summary.identityProviderCount).toBe(2);
    expect(feed.summary.activeIdentityProviders).toBe(1);
    expect(feed.summary.activeScimUsers).toBe(1);
    expect(feed.summary.blockingGapCount).toBe(3);
    expect(feed.summary.supportCaseCount).toBe(2);
    expect(feed.summary.openSupportCases).toBe(1);
    expect(feed.summary.blockedSupportCases).toBe(1);
    expect(feed.identityProviders[0].providerKey).toBe("okta_oidc");
    expect(feed.identityRolloutPolicies[0].runtimeMode).toBe("auth_allowed");
    expect(feed.scimUsers[0].memberId).toBe("scim_ops_example_com");
    expect(feed.supportCases[0]).toMatchObject({
      title: "Hosted escalation blocked",
      receiptId: "rcpt_support_1",
      supportPacketArtifactId: "art_support_packet_1",
      hostedOrgId: "hosted_org_1",
    });
  });

  it("maps agent module entitlement gates and install records", async () => {
    vi.mocked(client.listAgentModules).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          module_id: "private_enterprise_agent_v1",
          version: "1.0.0",
          display_name: "Private Enterprise Agent",
          description: "Enterprise-only worker module.",
          publisher: "boss",
          category: "enterprise",
          status: "active",
          role_key: "private_enterprise_agent",
          job_kinds: ["enterprise.marketplace.validate"],
          default_skill_ids: ["enterprise.registry.validate"],
          tags: ["enterprise"],
          price_model: "enterprise_contract",
          required_entitlements: ["private_agent_marketplace"],
          entitlement_status: "denied",
          config_schema: {},
          installed: false,
          config: {},
        },
      ],
    });
    vi.mocked(client.listAgentModuleInstallRecords).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "amir_1",
          project_id: "boss-dogfood",
          module_id: "private_enterprise_agent_v1",
          action: "install",
          to_version: "1.0.0",
          actor: "ceo",
          status: "denied",
          entitlement_result: {
            status: "denied",
            reason: "missing_entitlement",
          },
          rollback_snapshot: {},
          metadata: {
            module_integrity_artifact_id: "art_agent_integrity_1",
          },
          created_at: "2026-06-03T01:00:00Z",
        },
      ],
    });
    vi.mocked(client.listHostedMarketplaceListings).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "mklisting_1",
          project_id: "boss-dogfood",
          account_connect_org_id: "acctconn_1",
          hosted_org_id: "hosted_org_1",
          listing_id: "listing_private_agent",
          pack_id: "private_enterprise_agent_v1",
          version: "1.0.0",
          display_name: "Private Enterprise Agent",
          publisher: "BOSS",
          category: "enterprise",
          description: "Enterprise-only hosted module.",
          status: "active",
          price_model: "enterprise_contract",
          required_entitlements: ["private_agent_marketplace"],
          capabilities: ["agent_module.install"],
          metadata: {},
          last_sync_run_id: "mktsync_1",
          created_at: "2026-06-04T00:00:00Z",
          updated_at: "2026-06-04T00:00:00Z",
        },
      ],
    });
    vi.mocked(client.listHostedMarketplaceSyncRuns).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "mktsync_1",
          project_id: "boss-dogfood",
          account_connect_org_id: "acctconn_1",
          hosted_org_id: "hosted_org_1",
          status: "synced",
          listing_count: 1,
          provider_ref: "marketplace-sync-1",
          sync_packet_artifact_id: "art_marketplace_sync_packet_1",
          synced_by: "ceo",
          metadata: {},
          created_at: "2026-06-04T00:00:00Z",
        },
      ],
    });

    const feed = await fetchAgentModuleFeed("boss-dogfood");

    expect(feed.items[0].requiredEntitlements).toEqual([
      "private_agent_marketplace",
    ]);
    expect(feed.items[0].entitlementStatus).toBe("denied");
    expect(feed.installRecords[0]).toMatchObject({
      moduleId: "private_enterprise_agent_v1",
      status: "denied",
      metadata: {
        module_integrity_artifact_id: "art_agent_integrity_1",
      },
    });
    expect(feed.hostedListings[0]).toMatchObject({
      packId: "private_enterprise_agent_v1",
      requiredEntitlements: ["private_agent_marketplace"],
    });
    expect(feed.marketplaceSyncRuns[0]).toMatchObject({
      status: "synced",
      providerRef: "marketplace-sync-1",
      syncPacketArtifactId: "art_marketplace_sync_packet_1",
    });
  });
});
