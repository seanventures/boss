import { describe, expect, it, vi } from "vitest";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    listModelRouteDecisions: vi.fn(),
  },
}));

describe("routing feed", async () => {
  const { client } = await import("../lib/client");
  const { fetchRoutingLogFeed } = await import("../lib/routing");

  it("maps canonical ModelRouteDecision records and forwards filters", async () => {
    vi.mocked(client.listModelRouteDecisions).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "route_1",
          project_id: "boss-dogfood",
          task_id: "task_1",
          skill_id: "model.generate_text",
          task_kind: "planning",
          workload_class: "owner_sandbox",
          risk_tier: 1,
          selected_entitlement_pool_id: "pool_boss-dogfood_openai_api",
          selected_source_id: "openai_api",
          selected_provider: "openai",
          selected_model_id: "openai:gpt-5-mini",
          routing_mode: "budget_safe",
          rejected_sources: [],
          budget_state: "green",
          data_boundary: "internal",
          capacity_snapshot: {
            auth_method: "api_key",
            capacity_state: "available",
          },
          policy_result: {
            allowed: true,
            reservation_required: true,
          },
          created_at: "2026-06-04T12:00:00Z",
        },
      ],
    });

    const feed = await fetchRoutingLogFeed("boss-dogfood", 5, {
      provider: "openai",
      selected_only: true,
    });

    expect(client.listModelRouteDecisions).toHaveBeenCalledWith("boss-dogfood", {
      limit: 5,
      provider: "openai",
      selected_only: true,
    });
    expect(feed.apiReachable).toBe(true);
    expect(feed.items[0]).toMatchObject({
      id: "route_1",
      selectedProvider: "openai",
      routingMode: "budget_safe",
      policyAllowed: true,
      reservationRequired: true,
    });
    expect(feed.items[0].selectedSource).toEqual({
      auth_method: "api_key",
      capacity_state: "available",
    });
  });
});
