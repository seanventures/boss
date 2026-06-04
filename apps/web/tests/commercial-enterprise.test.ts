import { describe, expect, it, vi } from "vitest";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    getEdition: vi.fn(),
    listLicenseEntitlements: vi.fn(),
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
  },
}));

describe("commercial, enterprise, and marketplace feeds", async () => {
  const { client } = await import("../lib/client");
  const { fetchCommercialFeed } = await import("../lib/commercial");
  const { fetchEnterpriseFeed } = await import("../lib/enterprise");
  const { fetchAgentModuleFeed } = await import("../lib/agent-modules");

  it("maps edition, Account Connect, and license entitlements", async () => {
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
          metadata: {},
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
          metadata: {},
          created_at: "2026-06-03T01:00:00Z",
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
    });
  });
});
