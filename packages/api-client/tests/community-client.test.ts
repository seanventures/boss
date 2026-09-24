import { describe, expect, it, vi } from "vitest";
import { createBossApiClient } from "../src/community";

const paidMethods = [
  "linkAccountConnect",
  "getCloudCatalog",
  "listEnterpriseClusters",
  "listEnterpriseSupportCases",
  "listHostedMarketplaceListings",
];

describe("Community client", () => {
  it("imports and calls Community routes without the private extension", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ items: [] }), {
      headers: { "content-type": "application/json" },
    }));
    const client = createBossApiClient({ baseUrl: "https://boss.test", fetchImpl });
    expect(client).toHaveProperty("listProjects");
    expect(client).toHaveProperty("getEdition");
    expect(client).toHaveProperty("listEnterpriseIdentityProviders");
    for (const method of paidMethods) expect(client).not.toHaveProperty(method);
    await client.listProjects();
    expect(fetchImpl).toHaveBeenCalledWith("https://boss.test/v1/projects", expect.objectContaining({ method: "GET" }));
  });
});
