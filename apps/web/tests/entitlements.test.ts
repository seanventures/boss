import { describe, expect, it, vi } from "vitest";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    listEntitlementSources: vi.fn(),
    listEntitlementConnectionSessions: vi.fn(),
  },
}));

describe("entitlement feed", async () => {
  const { client } = await import("../lib/client");
  const { fetchEntitlementFeed } = await import("../lib/entitlements");

  it("maps official subscription connection sessions into Mission Control data", async () => {
    vi.mocked(client.listEntitlementSources).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "src_boss-dogfood_openai_codex_chatgpt",
          project_id: "boss-dogfood",
          provider: "openai",
          source_id: "openai_codex_chatgpt",
          source_kind: "official_cli",
          entitlement_kind: "subscription",
          owner_type: "workspace_operator",
          owner_id: "founder",
          workload_classes_allowed: ["owner_sandbox"],
          multi_tenant_allowed: false,
          cost_mode: "subscription",
          limit_shape: {},
          supported_models: ["codex"],
          task_strengths: ["coding"],
          risk_tier_max: 1,
          status: "active",
          effective_status: "missing_credentials",
          auth_method: "official_cli_oauth",
          connection_status: "not_connected",
          oauth_scopes: ["codex.local"],
          connection_metadata: {
            supported_connection_modes: ["browser_oauth"],
            login_command: "codex login",
          },
          policy_notes: [],
          observed_capacity: { capacity_state: "unknown" },
          runtime_supported: false,
          created_at: "2026-06-04T00:00:00Z",
          updated_at: "2026-06-04T00:00:00Z",
        },
      ],
    });
    vi.mocked(client.listEntitlementConnectionSessions).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "entconn_1",
          project_id: "boss-dogfood",
          source_id: "openai_codex_chatgpt",
          provider: "openai",
          auth_method: "official_cli_oauth",
          connection_mode: "browser_oauth",
          status: "operator_action_required",
          requested_scopes: ["codex.local"],
          login_command: "codex login",
          instructions: { operator_action: "Run codex login" },
          metadata: {},
          error: {},
          requested_by: "ceo",
          created_at: "2026-06-04T00:00:00Z",
          updated_at: "2026-06-04T00:00:00Z",
        },
      ],
    });

    const feed = await fetchEntitlementFeed("boss-dogfood");

    expect(client.listEntitlementConnectionSessions).toHaveBeenCalledWith(
      "boss-dogfood",
      { limit: 20 },
    );
    expect(feed.items[0].sourceId).toBe("openai_codex_chatgpt");
    expect(feed.connectionSessions[0]).toMatchObject({
      id: "entconn_1",
      sourceId: "openai_codex_chatgpt",
      loginCommand: "codex login",
      requestedScopes: ["codex.local"],
    });
  });
});
