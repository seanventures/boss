import { describe, expect, it, vi } from "vitest";

import { BossApiError } from "@boss/api-client";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    listInfraResources: vi.fn(),
  },
}));

describe("infrastructure resource feed", async () => {
  const { client } = await import("../lib/client");
  const { fetchInfrastructureResourceFeed } = await import(
    "../lib/infrastructure"
  );

  it("maps infra resources into the executive-console feed", async () => {
    vi.mocked(client.listInfraResources).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "infra_1",
          project_id: "boss-dogfood",
          provider: "digitalocean",
          resource_type: "droplet",
          resource_name: "boss-preview",
          external_resource_id: "123456",
          region: "nyc3",
          size: "s-1vcpu-1gb",
          image: "ubuntu-24-04-x64",
          tags: ["preview"],
          status: "active",
          ttl_minutes: 60,
          expires_at: "2026-06-03T12:00:00Z",
          data_boundary: "internal",
          metadata: {
            provider_url: "https://api.digitalocean.com/v2/droplets/123456",
          },
          created_at: "2026-06-03T11:00:00Z",
          updated_at: "2026-06-03T11:00:00Z",
        },
      ],
    });

    const feed = await fetchInfrastructureResourceFeed();

    expect(feed.apiReachable).toBe(true);
    expect(feed.items[0]).toMatchObject({
      provider: "digitalocean",
      resourceName: "boss-preview",
      externalResourceId: "123456",
      providerRef: "https://api.digitalocean.com/v2/droplets/123456",
    });
  });

  it("returns an unavailable state when the infra API is down", async () => {
    vi.mocked(client.listInfraResources).mockRejectedValueOnce(
      new BossApiError("request failed", 503, {}),
    );

    const feed = await fetchInfrastructureResourceFeed();

    expect(feed.apiReachable).toBe(false);
    expect(feed.projectId).toBe("boss-dogfood");
    expect(feed.blockedReason).toBe("API returned 503");
  });
});
