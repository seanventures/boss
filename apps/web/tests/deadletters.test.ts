import { describe, expect, it, vi } from "vitest";
import { BossApiError } from "@boss/api-client";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    listDeadletters: vi.fn(),
    listDeadletterRemediations: vi.fn(),
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

    vi.mocked(client.listDeadletterRemediations).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "dlr_1",
          project_id: "boss-dogfood",
          deadletter_id: "dlq_webhook",
          action: "hold",
          status: "applied",
          actor: "ceo",
          reason: "waiting_on_receiver",
          created_at: "2026-07-15T14:05:00Z",
        },
      ],
      actions: ["retry", "requeue", "escalate", "hold"],
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
    expect(feed.items[0].latestRemediation).toMatchObject({
      action: "hold",
      status: "applied",
    });
  });

  it("still lists deadletters when remediations are 404 on an older tag", async () => {
    vi.mocked(client.listDeadletters).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      limit: 25,
      offset: 0,
      items: [
        {
          id: "dlq_old",
          project_id: "boss-dogfood",
          source_kind: "job",
          source_id: "job_old",
          reason: "terminal",
          payload: {},
          status: "open",
          created_at: "2026-07-15T14:00:00Z",
        },
      ],
    });
    vi.mocked(client.listDeadletterRemediations).mockRejectedValueOnce(
      new BossApiError("BOSS API request failed: 404", 404),
    );

    const feed = await fetchDeadletterFeed("boss-dogfood", 25);
    expect(feed.apiReachable).toBe(true);
    expect(feed.items[0].id).toBe("dlq_old");
    expect(feed.items[0].remediations).toEqual([]);
  });
});
