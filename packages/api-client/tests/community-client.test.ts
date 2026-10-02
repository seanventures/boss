import { describe, expect, it, vi } from "vitest";
import { createBossApiClient } from "../src/community";

describe("Community client", () => {
  it("imports and calls Community routes without the private extension", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ items: [] }), {
      headers: { "content-type": "application/json" },
    }));
    const client = createBossApiClient({ baseUrl: "https://boss.test", fetchImpl });
    expect(client).toHaveProperty("listProjects");
    expect(client).toHaveProperty("getEdition");
    expect(client).toHaveProperty("getCloudCatalog");
    expect(client).toHaveProperty("listEnterpriseIdentityProviders");
    // +1: updateMcpServer — a registered MCP server can be edited in place (URL, tiers,
    // review) instead of deleted and re-registered, which unbound every step using it.
    expect(client).toHaveProperty("updateMcpServer");
    expect(Object.keys(client)).toHaveLength(375);
    await client.listProjects();
    await client.getCloudCatalog("p1");
    expect(fetchImpl).toHaveBeenCalledWith("https://boss.test/v1/cloud/catalog?project_id=p1", expect.objectContaining({ method: "GET" }));
    expect(fetchImpl).toHaveBeenCalledWith("https://boss.test/v1/projects", expect.objectContaining({ method: "GET" }));
  });

  it("calls the hardware place contract without a raw secret field", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({
      places: [],
      default_place_id: "place_local",
      id: "place_local",
      enroll_command: "scripts/install/node_agent.sh --enroll --token example",
    }), {
      headers: { "content-type": "application/json" },
    }));
    const client = createBossApiClient({ baseUrl: "https://boss.test", fetchImpl });
    await client.listHardwarePlaces("p1");
    await client.resolveHardwarePlace("p1");
    await client.registerLocalHardwarePlace({
      project_id: "p1",
      actor: "operator",
      hostname: "boss",
      arch: "arm64",
      os: "linux",
    });
    await client.enrollHardwareAgent({ project_id: "p1", actor: "operator", name: "vm-1" });
    await client.registerHardwareProvider({
      project_id: "p1",
      actor: "operator",
      name: "prod",
      provider: "aws",
      secret_ref: "env:BOSS_AWS_SECRET",
    });
    const bodies = fetchImpl.mock.calls.map((call) => {
      const init = call[1] as RequestInit | undefined;
      return init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : null;
    });
    const provider = bodies.find((body) => body && "secret_ref" in body);
    expect(provider).toEqual({
      project_id: "p1",
      actor: "operator",
      name: "prod",
      provider: "aws",
      secret_ref: "env:BOSS_AWS_SECRET",
    });
    expect(provider).not.toHaveProperty("secret");
    expect(provider).not.toHaveProperty("token");
    expect(provider).not.toHaveProperty("password");
    expect(provider).not.toHaveProperty("provider_key");
    expect(fetchImpl).toHaveBeenCalledWith(
      "https://boss.test/v1/hardware/places?project_id=p1",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchImpl).toHaveBeenCalledWith(
      "https://boss.test/v1/hardware/resolve?project_id=p1",
      expect.objectContaining({ method: "GET" }),
    );
  });
});
