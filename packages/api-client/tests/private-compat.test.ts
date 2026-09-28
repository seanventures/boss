import { describe, expect, it, vi } from "vitest";
import { createBossApiClient as createCommunityClient } from "../src/community";
import { createBossApiClient as createPrivateClient } from "../src/index";

function clientFixture() {
  const requests: Array<{ url: string; method: string; body?: unknown; authorization?: string }> = [];
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    requests.push({
      url: String(input),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      authorization: new Headers(init?.headers).get("Authorization") ?? undefined,
    });
    return new Response(JSON.stringify({ project_id: "p1", items: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  const config = { baseUrl: "https://boss.test/", apiKey: "test-key", fetchImpl };
  return { requests, config };
}

describe("Community API client boundary", () => {
  it("keeps all Community calls while excluding paid service methods", async () => {
    const { requests, config } = clientFixture();
    const client = createCommunityClient(config);
    expect(client).toHaveProperty("listProjects");
    expect(client).toHaveProperty("getEdition");
    expect(client).toHaveProperty("getCloudCatalog");
    expect(client).toHaveProperty("listEnterpriseIdentityProviders");
    expect(client).toHaveProperty("getAgentStudioCatalog");
    for (const method of [
      "linkAccountConnect",
      "listEnterpriseClusters",
      "listEnterpriseSupportCases",
      "listHostedMarketplaceListings",
    ]) {
      expect(client).not.toHaveProperty(method);
    }
    await client.listProjects();
    expect(requests).toEqual([{
      url: "https://boss.test/v1/projects",
      method: "GET",
      authorization: "Bearer test-key",
    }]);
  });

  it("preserves the private package's paid routes and request contract", async () => {
    const { requests, config } = clientFixture();
    const client = createPrivateClient(config);
    expect(Object.keys(client)).toHaveLength(393);
    await client.linkAccountConnect({ project_id: "p1", hosted_org_id: "h1" });
    await client.getCloudCatalog("p1");
    await client.listEnterpriseClusters("p1");
    await client.listEnterpriseSupportCases("p1");
    await client.listHostedMarketplaceListings("p1");
    expect(requests).toEqual([
      { url: "https://boss.test/v1/account-connect/link", method: "POST", body: { project_id: "p1", hosted_org_id: "h1" }, authorization: "Bearer test-key" },
      { url: "https://boss.test/v1/cloud/catalog?project_id=p1", method: "GET", authorization: "Bearer test-key" },
      { url: "https://boss.test/v1/enterprise/clusters?project_id=p1", method: "GET", authorization: "Bearer test-key" },
      { url: "https://boss.test/v1/enterprise/support-cases?project_id=p1&limit=50", method: "GET", authorization: "Bearer test-key" },
      { url: "https://boss.test/v1/marketplace/listings?project_id=p1&include_inactive=false&limit=50", method: "GET", authorization: "Bearer test-key" },
    ]);
  });
});
