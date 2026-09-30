import { describe, expect, it } from "vitest";

import { BossApiError, createBossApiClient } from "@boss/api-client";

describe("Community API client contract", () => {
  it("scopes task and budget reads to the requested project", async () => {
    const urls: string[] = [];
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl: async (input: RequestInfo | URL) => {
        urls.push(String(input));
        return new Response(JSON.stringify({ items: [] }), {
          headers: { "Content-Type": "application/json" },
        });
      },
    });

    await client.getHealth();
    await client.listTasks("project-a", 10, 20);
    await client.getBudgetStatus("project", "project-a");
    await client.listProviderDefinitions();

    expect(urls).toEqual([
      "http://127.0.0.1:8000/health",
      "http://127.0.0.1:8000/v1/tasks?project_id=project-a&limit=10&offset=20",
      "http://127.0.0.1:8000/v1/budget/status?scope_type=project&scope_id=project-a",
      "http://127.0.0.1:8000/v1/provider-definitions",
    ]);
  });

  it("sends the configured API key and surfaces failed responses", async () => {
    let authorization: string | null = null;
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      apiKey: "test-key",
      fetchImpl: async (_input: RequestInfo | URL, init?: RequestInit) => {
        authorization = new Headers(init?.headers).get("authorization");
        return new Response(JSON.stringify({ detail: "unavailable" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        });
      },
    });

    await expect(client.listProviderDefinitions()).rejects.toBeInstanceOf(BossApiError);
    expect(authorization).toBe("Bearer test-key");
  });
});
