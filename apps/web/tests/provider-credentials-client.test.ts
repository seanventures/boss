import { describe, expect, it } from "vitest";

import { createBossApiClient } from "@boss/api-client";

describe("provider credential lifecycle client", () => {
  it("uses the typed provider definition and lifecycle endpoints", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    await client.listProviderDefinitions();
    await client.getProviderAccount("pa_1", "boss-dogfood");
    await client.updateProviderAccount("pa_1", {
      project_id: "boss-dogfood",
      scopes: ["read", "billing:read"],
      actor: "ceo",
    });
    await client.disableProviderAccount("pa_1", {
      project_id: "boss-dogfood",
      actor: "ceo",
    });
    await client.enableProviderAccount("pa_1", {
      project_id: "boss-dogfood",
      actor: "ceo",
    });
    await client.rotateProviderCredentials("pa_1", {
      project_id: "boss-dogfood",
      credential_refs: { token: "env:BOSS_DO_TOKEN_V2" },
      overlap_minutes: 30,
      actor: "ceo",
    });
    await client.revokeProviderAccount("pa_1", {
      project_id: "boss-dogfood",
      actor: "ceo",
    });

    expect(calls.map((call) => [call.init?.method, call.url])).toEqual([
      ["GET", "http://127.0.0.1:8000/v1/provider-definitions"],
      ["GET", "http://127.0.0.1:8000/v1/providers/pa_1?project_id=boss-dogfood"],
      ["PATCH", "http://127.0.0.1:8000/v1/providers/pa_1"],
      ["POST", "http://127.0.0.1:8000/v1/providers/pa_1/disable"],
      ["POST", "http://127.0.0.1:8000/v1/providers/pa_1/enable"],
      ["POST", "http://127.0.0.1:8000/v1/providers/pa_1/rotate"],
      ["POST", "http://127.0.0.1:8000/v1/providers/pa_1/revoke"],
    ]);
    expect(JSON.parse(String(calls[5].init?.body))).toEqual({
      project_id: "boss-dogfood",
      credential_refs: { token: "env:BOSS_DO_TOKEN_V2" },
      overlap_minutes: 30,
      actor: "ceo",
    });
  });
});
