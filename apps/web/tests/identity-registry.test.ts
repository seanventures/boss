import { describe, expect, it, vi } from "vitest";
import { BossApiError } from "@boss/api-client";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    listEnterpriseIdentityProviders: vi.fn(),
    listEnterpriseIdentityRolloutPolicies: vi.fn(),
    listEnterpriseScimGroups: vi.fn(),
    listEnterpriseScimUsers: vi.fn(),
  },
}));

const { client } = await import("../lib/client");
const { fetchIdentityRegistryFeed } = await import("../lib/identity-registry");

describe("Community self-hosted identity registry", () => {
  it("keeps identity providers visible when other optional identity routes return 404", async () => {
    vi.mocked(client.listEnterpriseIdentityProviders).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [{
        id: "idp_local", org_id: "org_boss", project_id: "boss-dogfood",
        provider_key: "local_oidc", provider_type: "oidc", display_name: "Local OIDC",
        status: "active", test_status: "passed", blocking_gaps: [], metadata: {},
        created_by: "ceo", created_at: "2026-09-24T00:00:00Z", updated_at: "2026-09-24T00:00:00Z",
      }],
    });
    const unavailable = new BossApiError("optional route unavailable", 404, {});
    vi.mocked(client.listEnterpriseIdentityRolloutPolicies).mockRejectedValueOnce(unavailable);
    vi.mocked(client.listEnterpriseScimGroups).mockRejectedValueOnce(unavailable);
    vi.mocked(client.listEnterpriseScimUsers).mockRejectedValueOnce(unavailable);

    const feed = await fetchIdentityRegistryFeed("boss-dogfood");
    expect(feed.apiReachable).toBe(true);
    expect(feed.identityProviders).toEqual([
      expect.objectContaining({ id: "idp_local", displayName: "Local OIDC", status: "active" }),
    ]);
    expect(feed.identityRolloutPolicies).toEqual([]);
    expect(feed.scimGroups).toEqual([]);
    expect(feed.scimUsers).toEqual([]);
  });

  it("does not fetch hidden areas and reports a real API failure", async () => {
    vi.clearAllMocks();
    const inactive = await fetchIdentityRegistryFeed("boss-dogfood", false);
    expect(inactive.apiReachable).toBe(false);
    expect(client.listEnterpriseIdentityProviders).not.toHaveBeenCalled();

    vi.mocked(client.listEnterpriseIdentityProviders).mockRejectedValueOnce(
      new BossApiError("API unavailable", 503, {}),
    );
    vi.mocked(client.listEnterpriseIdentityRolloutPolicies).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    vi.mocked(client.listEnterpriseScimGroups).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    vi.mocked(client.listEnterpriseScimUsers).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    const failed = await fetchIdentityRegistryFeed("boss-dogfood");
    expect(failed.apiReachable).toBe(false);
    expect(failed.blockedReason).toBe("API returned 503");
  });
});
