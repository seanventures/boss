import { describe, expect, it, vi } from "vitest";
import { BossApiError } from "@boss/api-client";
vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    listOutboxMessages: vi.fn(),
    listCommsChannelPolicies: vi.fn(),
    listInboxMessages: vi.fn(),
  },
}));
describe("Comms feed (policy form, outbox empty/error/success/ACK, perm 403, persist refresh)", () => {
  it("maps success outbox feed with policies + ACK receipt", async () => {
    const { client } = await import("../lib/client");
    const { fetchOutboxFeed } = await import("../lib/comms");
    vi.mocked(client.listOutboxMessages).mockResolvedValueOnce({ project_id: "boss-dogfood", limit: 50, offset: 0, items: [{ id: "out1", project_id: "boss-dogfood", channel: "slack:#a", subject: "s", body: "b", status: "acked", data_boundary: "internal", dedupe_key: "d1", receipt_id: "r1", created_at: "2026-06-05T00:00:00Z", updated_at: "2026-06-05T00:00:00Z" }] });
    vi.mocked(client.listCommsChannelPolicies).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [{ id: "p1", project_id: "boss-dogfood", provider: "slack", delivery_mode: "real_send_allowed", allowed_data_boundaries: ["internal"], rollout_notes: undefined, status: "active", created_by: "ceo", metadata: {}, created_at: "2026-06-05T00:00:00Z", updated_at: "2026-06-05T00:00:00Z" }] });
    const feed = await fetchOutboxFeed("boss-dogfood");
    expect(feed.apiReachable).toBe(true);
    expect(feed.items[0].receiptId).toBe("r1");
    expect(feed.policies.length).toBe(1);
  });
  it("empty/error for outbox feed (policy form surface)", async () => {
    const { client } = await import("../lib/client");
    const { fetchOutboxFeed } = await import("../lib/comms");
    vi.mocked(client.listOutboxMessages).mockRejectedValueOnce(new BossApiError("fail", 500, {}));
    vi.mocked(client.listCommsChannelPolicies).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    const feed = await fetchOutboxFeed();
    expect(feed.apiReachable).toBe(false);
    expect(feed.blockedReason).toMatch(/500/);
    expect(feed.policies).toEqual([]);
  });
  it("403 perm reflection for comms feeds", async () => {
    const { client } = await import("../lib/client");
    const { fetchOutboxFeed } = await import("../lib/comms");
    vi.mocked(client.listOutboxMessages).mockRejectedValueOnce(new BossApiError("no perm", 403, {}));
    vi.mocked(client.listCommsChannelPolicies).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });
    const feed = await fetchOutboxFeed();
    expect(feed.apiReachable).toBe(false);
    expect(feed.blockedReason).toBe("API returned 403");
  });
  it("persist refresh for outbox + inbox", async () => {
    const { client } = await import("../lib/client");
    const { fetchOutboxFeed, fetchInboxFeed } = await import("../lib/comms");
    const odata = { project_id: "boss-dogfood", limit: 50, offset: 0, items: [{ id: "o1", project_id: "boss-dogfood", channel: "e", subject: undefined, body: "b", status: "draft", data_boundary: "internal" as const, dedupe_key: "d", created_at: "z", updated_at: "z" }] };
    const pdata = { project_id: "boss-dogfood", items: [] };
    vi.mocked(client.listOutboxMessages).mockResolvedValue(odata);
    vi.mocked(client.listCommsChannelPolicies).mockResolvedValue(pdata);
    vi.mocked(client.listInboxMessages).mockResolvedValue({ project_id: "boss-dogfood", limit: 50, offset: 0, items: [] });
    const f1 = await fetchOutboxFeed();
    const f2 = await fetchOutboxFeed();
    const i1 = await fetchInboxFeed();
    expect(f1.items[0].id).toBe(f2.items[0].id);
    expect(i1.apiReachable).toBe(true);
  });
});
