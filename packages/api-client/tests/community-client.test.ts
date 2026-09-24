import { describe, expect, it, vi } from "vitest";
import { createBossApiClient } from "../src/community";

describe("Community client", () => {
  it("imports and calls Community routes without the private extension", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ items: [] }), {
      headers: { "content-type": "application/json" },
    }));
    const client = createBossApiClient({ baseUrl: "https://boss.test", fetchImpl });
    expect(client).toHaveProperty("listProjects");
    expect(client).toHaveProperty("getEdition");
    expect(client).toHaveProperty("getCloudCatalog");
    expect(client).toHaveProperty("listEnterpriseIdentityProviders");
    expect(Object.keys(client)).toHaveLength(369);
    await client.listProjects();
    await client.getCloudCatalog("p1");
    expect(fetchImpl).toHaveBeenCalledWith("https://boss.test/v1/cloud/catalog?project_id=p1", expect.objectContaining({ method: "GET" }));
    expect(fetchImpl).toHaveBeenCalledWith("https://boss.test/v1/projects", expect.objectContaining({ method: "GET" }));
  });
});
