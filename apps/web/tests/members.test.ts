import { describe, expect, it, vi } from "vitest";

describe("member feeds", () => {
  it("maps org members into a visible delegation feed", async () => {
    vi.resetModules();
    vi.doMock("../lib/client", () => ({
      PROJECT_ID: "boss-dogfood",
      resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
      client: {
        listOrgMembers: vi.fn(async () => ({
          org_id: "org_boss",
          items: [
            {
              id: "om_org_boss_delegate",
              org_id: "org_boss",
              project_id: "boss-dogfood",
              member_id: "org_delegate",
              display_name: "Org Delegate",
              member_type: "human",
              role_keys: ["operator"],
              permissions: ["command_submit"],
              status: "active",
              created_by: "ceo",
              created_at: "2026-06-03T00:00:00Z",
              updated_at: "2026-06-03T00:00:00Z",
            },
          ],
        })),
      },
    }));
    const { fetchOrgMemberFeed } = await import("../lib/members");

    const feed = await fetchOrgMemberFeed("org_boss", "boss-dogfood", "ceo");

    expect(feed.apiReachable).toBe(true);
    expect(feed.orgId).toBe("org_boss");
    expect(feed.items[0]).toMatchObject({
      memberId: "org_delegate",
      displayName: "Org Delegate",
      permissions: ["command_submit"],
    });
  });

  it("returns a visible unavailable state when org-member delegation is not reachable", async () => {
    const { emptyOrgMemberFeed } = await import("../lib/members");

    const feed = emptyOrgMemberFeed("API returned 403", "org_boss");

    expect(feed.apiReachable).toBe(false);
    expect(feed.orgId).toBe("org_boss");
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("API returned 403");
  });
});
