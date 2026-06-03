import { describe, expect, it, vi } from "vitest";

import { BossApiError } from "@boss/api-client";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    queryMetatron: vi.fn(),
  },
}));

describe("metatron feed", async () => {
  const { client } = await import("../lib/client");
  const { fetchMetatronQuery, placeholderMetatron } = await import(
    "../lib/metatron"
  );

  it("returns null when no question is provided", async () => {
    await expect(fetchMetatronQuery()).resolves.toBeNull();
  });

  it("maps a metatron response into a UI card", async () => {
    vi.mocked(client.queryMetatron).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      question: "What is the current status?",
      category: "status",
      answer: "BOSS is healthy.",
      freshness_at: "2026-06-01T12:00:00Z",
      references: [
        {
          object_type: "task",
          object_id: "tsk_1",
          title: "Improve BOSS",
          status: "open",
          detail: "Primary task",
          created_at: "2026-06-01T11:59:00Z",
        },
      ],
      suggested_actions: ["Review pending decisions."],
      top_risks: [{ severity: "medium", title: "Budget pressure" }],
    });

    const card = await fetchMetatronQuery("What is the current status?");

    expect(card?.apiReachable).toBe(true);
    expect(card?.category).toBe("status");
    expect(card?.references[0].objectType).toBe("task");
    expect(card?.suggestedActions).toEqual(["Review pending decisions."]);
  });

  it("returns an unavailable placeholder when the API fails", async () => {
    vi.mocked(client.queryMetatron).mockRejectedValueOnce(
      new BossApiError("request failed", 503, {}),
    );

    const card = await fetchMetatronQuery("What changed?");

    expect(card?.apiReachable).toBe(false);
    expect(card?.blockedReason).toBe("API returned 503");
  });

  it("returns a direct placeholder helper", () => {
    const card = placeholderMetatron("What changed?", "metatron offline");

    expect(card.question).toBe("What changed?");
    expect(card.apiReachable).toBe(false);
    expect(card.blockedReason).toBe("metatron offline");
  });
});
