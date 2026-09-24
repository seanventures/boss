import { beforeEach, describe, expect, it, vi } from "vitest";

import { BossApiError } from "@boss/api-client";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "test-project",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "test-project",
  client: {
    getEdition: vi.fn(),
    listProviderDefinitions: vi.fn(),
    listProviderAccounts: vi.fn(),
    listExternalResources: vi.fn(),
    listProviderCosts: vi.fn(),
    listProviderCostAnomalies: vi.fn(),
    listChangeSets: vi.fn(),
    listResourceReceipts: vi.fn(),
    listDeploymentTargets: vi.fn(),
    listDeploymentEnvironmentPolicies: vi.fn(),
    listDeploymentRuns: vi.fn(),
    listDeploymentPromotions: vi.fn(),
    listProviderHealth: vi.fn(),
    listDriftReports: vi.fn(),
    listReconciliationRuns: vi.fn(),
    listWorkerPools: vi.fn(),
    listScalePlans: vi.fn(),
  },
}));

describe("Community ProviderOps feed", async () => {
  const { client } = await import("../lib/client");
  const { fetchProviderOpsFeed } = await import("../lib/providerops");

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(client.getEdition).mockResolvedValue({ edition_id: "community" } as never);
    vi.mocked(client.listProviderDefinitions).mockResolvedValue({ items: [] });
    for (const method of [
      "listProviderAccounts", "listExternalResources", "listProviderCosts",
      "listProviderCostAnomalies", "listChangeSets", "listResourceReceipts",
      "listDeploymentTargets", "listDeploymentEnvironmentPolicies", "listDeploymentRuns",
      "listDeploymentPromotions", "listProviderHealth", "listDriftReports",
      "listReconciliationRuns", "listWorkerPools", "listScalePlans",
    ] as const) {
      vi.mocked(client[method]).mockResolvedValue({ project_id: "test-project", items: [] } as never);
    }
  });

  it("loads the complete BYOK and local infrastructure kernel", async () => {
    vi.mocked(client.listProviderDefinitions).mockResolvedValueOnce({
      items: [{
        provider: "openai", display_name: "OpenAI", category: "model",
        adapter_status: "available", default_auth_method: "api_key",
        auth_methods: [], capabilities: ["model.invoke"],
      }],
    });

    const feed = await fetchProviderOpsFeed();

    expect(feed.apiReachable).toBe(true);
    expect(feed.editionId).toBe("community");
    expect(feed.providerDefinitions[0]?.provider).toBe("openai");
    expect(feed.partialFailures).toEqual([]);
    expect(feed.editionLimitations).toEqual([]);
    expect(client.listChangeSets).toHaveBeenCalled();
    expect(client.listWorkerPools).toHaveBeenCalled();
    expect(client.listScalePlans).toHaveBeenCalled();
  });

  it("reports secondary failures without hiding healthy provider data", async () => {
    vi.mocked(client.listChangeSets).mockRejectedValueOnce(new BossApiError("upstream", 503, {}));
    vi.mocked(client.listWorkerPools).mockRejectedValueOnce(new BossApiError("upstream", 503, {}));
    vi.mocked(client.listScalePlans).mockRejectedValueOnce(new BossApiError("upstream", 503, {}));

    const feed = await fetchProviderOpsFeed();

    expect(feed.apiReachable).toBe(true);
    expect(feed.partialFailures).toContain("ChangeSets: HTTP 503");
    expect(feed.partialFailures).toContain("Worker pools: HTTP 503");
    expect(feed.partialFailures).toContain("Scale plans: HTTP 503");
    expect(feed.providerDefinitions).toEqual([]);
  });
});
