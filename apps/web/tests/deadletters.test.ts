import { describe, expect, it, vi } from "vitest";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    listDeadletters: vi.fn(),
  },
}));

import { client } from "../lib/client";
import { fetchDeadletterFeed } from "../lib/deadletters";


describe("deadletter related-effect visibility", () => {
  it("maps a terminal webhook job to its durable delivery", async () => {
    vi.mocked(client.listDeadletters).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      limit: 25,
      offset: 0,
      items: [
        {
          id: "dlq_webhook",
          project_id: "boss-dogfood",
          source_kind: "job",
          source_id: "job_webhook",
          related_entity_type: "webhook_delivery",
          related_entity_id: "whd_123",
          related_status: "deadlettered",
          reason: "receiver unavailable",
          payload: { message: "receiver unavailable" },
          status: "open",
          created_at: "2026-07-15T14:00:00Z",
        },
      ],
    });

    const feed = await fetchDeadletterFeed("boss-dogfood", 25);

    expect(feed.apiReachable).toBe(true);
    expect(feed.items[0]).toMatchObject({
      sourceKind: "job",
      sourceId: "job_webhook",
      relatedEntityType: "webhook_delivery",
      relatedEntityId: "whd_123",
      relatedStatus: "deadlettered",
    });
  });
});
