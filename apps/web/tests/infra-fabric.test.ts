import { beforeEach, describe, expect, it, vi } from "vitest";

import { BossApiError } from "@boss/api-client";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  API_URL: "http://boss-api.test",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  authHeaders: (extra?: Record<string, string>) => ({
    Authorization: "Bearer test-key",
    ...(extra ?? {}),
  }),
  client: {
    getElasticStatus: vi.fn(),
    getProviderUsageSummary: vi.fn(),
  },
}));

describe("Infra fabric fetch helpers", async () => {
  const { client } = await import("../lib/client");
  const {
    fetchMultiCloudCatalog,
    fetchElasticStatus,
    fetchProviderUsageSummary,
    fetchInfraFabricFeed,
    emptyMultiCloudCatalog,
    emptyElasticStatus,
    emptyProviderUsageSummary,
  } = await import("../lib/infra-fabric");

  beforeEach(() => {
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  function mockFetchJson(status: number, body: unknown) {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("maps multi-cloud catalog into provider rows with skill counts", async () => {
    const fetchMock = mockFetchJson(200, {
      version: 1,
      product: "boss_multi_cloud",
      provider_count: 2,
      gates: {
        infra_real_provision: "BOSS_INFRA_REAL_PROVISION_ALLOWED",
        ssh_host_deploy: "BOSS_SSH_HOST_DEPLOY_ALLOWED",
      },
      providers: [
        {
          provider_key: "aws",
          display_name: "Amazon Web Services",
          categories: ["infrastructure", "models", "bare_metal"],
          adapter_status: "available",
          default_auth_method: "access_key",
          infra_skills: ["aws.ec2.instance.create", "aws.s3.bucket.create"],
          model_skills: ["aws.bedrock.converse"],
          connection_test: "sts:GetCallerIdentity",
        },
        {
          provider_key: "ovhcloud",
          display_name: "OVHcloud",
          categories: ["infrastructure", "bare_metal"],
          adapter_status: "available",
          default_auth_method: "application_consumer_key",
          infra_skills: ["ovh.dedicated.server.list"],
          model_skills: [],
        },
      ],
      skill_index: {
        "aws.ec2.instance.create": "aws",
        "aws.s3.bucket.create": "aws",
        "aws.bedrock.converse": "aws",
        "ovh.dedicated.server.list": "ovhcloud",
      },
    });

    const feed = await fetchMultiCloudCatalog();

    expect(fetchMock).toHaveBeenCalledWith(
      "http://boss-api.test/v1/multi-cloud/catalog",
      expect.objectContaining({
        cache: "no-store",
        headers: expect.objectContaining({ Authorization: "Bearer test-key" }),
      }),
    );
    expect(feed.apiReachable).toBe(true);
    expect(feed.product).toBe("boss_multi_cloud");
    expect(feed.providerCount).toBe(2);
    expect(feed.skillCount).toBe(4);
    expect(feed.providers).toHaveLength(2);
    expect(feed.providers[0]).toMatchObject({
      providerKey: "aws",
      displayName: "Amazon Web Services",
      adapterStatus: "available",
      categories: ["infrastructure", "models", "bare_metal"],
      skillCount: 3,
      infraSkillCount: 2,
      modelSkillCount: 1,
    });
    expect(feed.providers[1].skillCount).toBe(1);
    expect(feed.gates.infraRealProvision).toBe("BOSS_INFRA_REAL_PROVISION_ALLOWED");
  });

  it("returns empty multi-cloud catalog on HTTP failure", async () => {
    mockFetchJson(503, { detail: "down" });
    const feed = await fetchMultiCloudCatalog();
    expect(feed.apiReachable).toBe(false);
    expect(feed.blockedReason).toContain("HTTP 503");
    expect(feed.providers).toEqual([]);
  });

  it("maps elastic status pool and instance counts with real-host honesty", async () => {
    vi.mocked(client.getElasticStatus).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      pools: [
        {
          id: "pool_1",
          pool_key: "elastic-local",
          kind: "local",
          status: "active",
          current_workers: 2,
          desired_workers: 2,
        },
      ],
      instance_counts: {
        total: 3,
        active: 2,
        by_status: { running: 2, terminated: 1 },
        by_kind: { local: 2, container: 1 },
        by_provider: { local: 3 },
      },
      infra_real_provision_allowed: false,
      message: "Elastic fabric uses ProviderOps connections per project.",
    });

    const feed = await fetchElasticStatus("boss-dogfood");

    expect(client.getElasticStatus).toHaveBeenCalledWith("boss-dogfood");
    expect(feed.apiReachable).toBe(true);
    expect(feed.poolCount).toBe(1);
    expect(feed.instanceCounts).toEqual({
      total: 3,
      active: 2,
      byStatus: { running: 2, terminated: 1 },
      byKind: { local: 2, container: 1 },
      byProvider: { local: 3 },
    });
    expect(feed.realHostEffects).toBe(false);
    expect(feed.pools[0]).toMatchObject({
      poolKey: "elastic-local",
      kind: "local",
      currentWorkers: 2,
      desiredWorkers: 2,
    });
  });

  it("returns empty elastic status on client failure", async () => {
    vi.mocked(client.getElasticStatus).mockRejectedValueOnce(
      new BossApiError("elastic down", 503),
    );
    const feed = await fetchElasticStatus();
    expect(feed.configured).toBe(false);
    expect(feed.status).toBe("not_configured");
    expect(feed.hostConnected).toBe(false);
    expect(feed.realHostEffects).toBe(false);
    expect(feed.poolCount).toBe(0);
    expect(feed.blockedReason).not.toMatch(/API returned 500/i);
  });

  it("treats HTTP 500 elastic status as not_configured, never connected", async () => {
    vi.mocked(client.getElasticStatus).mockRejectedValueOnce(
      new BossApiError("UndefinedTable", 500),
    );
    const feed = await fetchElasticStatus();
    expect(feed.status).toBe("not_configured");
    expect(feed.hostConnected).toBe(false);
    expect(feed.realHostEffects).toBe(false);
    expect(feed.instanceCounts.total).toBe(0);
  });

  
  
  it("maps provider usage summary rollups", async () => {
    vi.mocked(client.getProviderUsageSummary).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      generated_at: "2026-08-12T00:00:00Z",
      providers: [
        {
          provider: "local",
          event_count: 5,
          cost_usd_micros: 0,
          categories: [
            {
              category: "api_call",
              unit: "count",
              event_count: 5,
              quantity_sum: 5,
              cost_usd_micros: 0,
            },
          ],
        },
        {
          provider: "openai",
          event_count: 2,
          cost_usd_micros: 40,
          categories: [
            {
              category: "model_token",
              unit: "tokens",
              event_count: 2,
              quantity_sum: 1200,
              cost_usd_micros: 40,
            },
          ],
          billing: {
            cost_event_count: 1,
            amount_usd_micros: 100,
          },
        },
      ],
    });

    const feed = await fetchProviderUsageSummary("boss-dogfood");

    expect(client.getProviderUsageSummary).toHaveBeenCalledWith("boss-dogfood");
    expect(feed.apiReachable).toBe(true);
    expect(feed.providers).toHaveLength(2);
    expect(feed.providers[0]).toMatchObject({
      provider: "local",
      eventCount: 5,
      costUsdMicros: 0,
    });
    expect(feed.providers[1].billing).toEqual({
      costEventCount: 1,
      amountUsdMicros: 100,
    });
    expect(feed.providers[1].categories[0].quantitySum).toBe(1200);
  });

  it("returns empty provider usage on client failure", async () => {
    vi.mocked(client.getProviderUsageSummary).mockRejectedValueOnce(
      new Error("timeout"),
    );
    const feed = await fetchProviderUsageSummary();
    expect(feed.apiReachable).toBe(false);
    expect(feed.blockedReason).toBe("timeout");
  });

  it("combines surfaces into infra fabric feed with partial failures", async () => {
    mockFetchJson(200, {
      version: 1,
      product: "boss_multi_cloud",
      provider_count: 1,
      providers: [
        {
          provider_key: "aws",
          display_name: "Amazon Web Services",
          categories: ["infrastructure"],
          adapter_status: "available",
          default_auth_method: "access_key",
          infra_skills: ["aws.ec2.instance.create"],
          model_skills: [],
        },
      ],
      skill_index: { "aws.ec2.instance.create": "aws" },
      gates: {},
    });
    // Override fetch to route by URL
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      if (String(url).includes("/v1/multi-cloud/catalog")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            version: 1,
            product: "boss_multi_cloud",
            provider_count: 1,
            providers: [
              {
                provider_key: "aws",
                display_name: "Amazon Web Services",
                categories: ["infrastructure"],
                adapter_status: "available",
                default_auth_method: "access_key",
                infra_skills: ["aws.ec2.instance.create"],
                model_skills: [],
              },
            ],
            skill_index: { "aws.ec2.instance.create": "aws" },
            gates: {},
          }),
        };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    });
    vi.stubGlobal("fetch", fetchMock);

    vi.mocked(client.getElasticStatus).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      pools: [],
      instance_counts: {
        total: 0,
        active: 0,
        by_status: {},
        by_kind: {},
        by_provider: {},
      },
      infra_real_provision_allowed: false,
    });
    vi.mocked(client.getProviderUsageSummary).mockRejectedValueOnce(
      new BossApiError("usage unavailable", 500),
    );

    const feed = await fetchInfraFabricFeed("boss-dogfood");

    expect(feed.projectId).toBe("boss-dogfood");
    expect(feed.multiCloud.apiReachable).toBe(true);
    expect(feed.elastic.apiReachable).toBe(true);
    expect(feed.elastic.status).toBe("not_configured");
    expect(feed.elastic.hostConnected).toBe(false);
    expect(feed.providerUsage.status).toBe("not_configured");
    expect(feed.partialFailures).toEqual([]);
    expect(feed.refreshedAt).toBeTruthy();
  });

  it("empty helpers start Community-safe (real host effects off)", () => {
    expect(emptyMultiCloudCatalog("x").apiReachable).toBe(false);
    expect(emptyElasticStatus("x").realHostEffects).toBe(false);
    expect(emptyProviderUsageSummary("x").providers).toEqual([]);
  });
});

describe("Infra fabric UI wiring", () => {
  it("page imports InfraFabricPanel near ProviderOps", async () => {
    const { readFileSync } = await import("node:fs");
    const { resolve } = await import("node:path");
    const page = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
    const ui = readFileSync(
      resolve(__dirname, "../app/infra-fabric-ui.tsx"),
      "utf8",
    );

    expect(page).toContain('from "./infra-fabric-ui"');
    expect(page).toContain("fetchInfraFabricFeed");
    expect(page).toContain("<InfraFabricPanel");
    expect(ui).toContain("docs/api/MULTI_CLOUD.md");
    expect(ui).toContain("BYOK");
    expect(ui).toContain("managed fleets");
    expect(ui).toContain("aria-label=\"Multi-cloud providers\"");
    expect(ui).toContain("aria-label=\"Elastic fabric\"");
    expect(ui).toContain('data-empty-state="elastic-not-configured"');
  });
});
