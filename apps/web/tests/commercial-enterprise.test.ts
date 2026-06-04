import { describe, expect, it, vi } from "vitest";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    getEdition: vi.fn(),
    listLicenseEntitlements: vi.fn(),
    listEnterpriseClusters: vi.fn(),
    listEnterpriseScimGroups: vi.fn(),
    listEnterpriseScimUsers: vi.fn(),
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

    const feed = await fetchCommercialFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(true);
    expect(feed.edition?.accountConnectStatus).toBe("linked");
    expect(feed.edition?.accountConnectOrg?.hostedOrgId).toBe("hosted_org_1");
    expect(feed.entitlements[0]).toMatchObject({
      capabilityKey: "private_agent_marketplace",
      seats: 5,
    });
  });

  it("maps enterprise cluster and SCIM provisioning state", async () => {
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

    const feed = await fetchEnterpriseFeed("boss-dogfood");

    expect(feed.summary.clusterCount).toBe(1);
    expect(feed.summary.activeScimUsers).toBe(1);
    expect(feed.summary.blockingGapCount).toBe(1);
    expect(feed.scimUsers[0].memberId).toBe("scim_ops_example_com");
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
