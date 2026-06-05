import { describe, expect, it, vi } from "vitest";

import { BossApiError } from "@boss/api-client";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    listKpis: vi.fn(),
  },
}));

describe("KPI feed", async () => {
  const { client } = await import("../lib/client");
  const { emptyKpiFeed, fetchKpiFeed } = await import("../lib/kpis");

  it("maps persisted KPI records into Mission Control items", async () => {
    vi.mocked(client.listKpis).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "kpi_1",
          project_id: "boss-dogfood",
          kpi_key: "verified_work_throughput",
          name: "Verified Work Throughput",
          description: "Completed artifacts that pass required gates.",
          target_value: 10,
          current_value: 2,
          unit: "artifacts/week",
          status: "active",
          created_by: "ceo",
          created_at: "2026-06-05T00:00:00Z",
          updated_at: "2026-06-05T00:01:00Z",
        },
      ],
    });

    const feed = await fetchKpiFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(true);
    expect(feed.items[0]).toMatchObject({
      id: "kpi_1",
      kpiKey: "verified_work_throughput",
      targetValue: 10,
      currentValue: 2,
    });
  });

  it("returns an explicit empty/error state when the KPI API is unavailable", async () => {
    vi.mocked(client.listKpis).mockRejectedValueOnce(
      new BossApiError("request failed", 503, {}),
    );

    const feed = await fetchKpiFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("API returned 503");
  });

  it("builds a local empty state for inaccessible projects", () => {
    const feed = emptyKpiFeed("network down", "boss-dogfood");

    expect(feed.apiReachable).toBe(false);
    expect(feed.blockedReason).toBe("network down");
    expect(feed.items).toEqual([]);
  });
});
