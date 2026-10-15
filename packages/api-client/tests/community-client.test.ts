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
    // 376 on main (one more than this line said), +5: workflow webhook triggers (#512) — list,
    // create, rotate, enable/disable, deliveries. +3: startWorkflowTestRun, getWorkflowTestRuns,
    // rerunWorkflowFromStep (#512).
    expect(Object.keys(client)).toHaveLength(384);
    await client.listProjects();
    await client.getCloudCatalog("p1");
    expect(fetchImpl).toHaveBeenCalledWith("https://boss.test/v1/cloud/catalog?project_id=p1", expect.objectContaining({ method: "GET" }));
    expect(fetchImpl).toHaveBeenCalledWith("https://boss.test/v1/projects", expect.objectContaining({ method: "GET" }));
  });

  it("starts a test run with pins and re-runs a run from a step", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ id: "wfr_1" }), {
      headers: { "content-type": "application/json" },
    }));
    const client = createBossApiClient({ baseUrl: "https://boss.test", fetchImpl });
    await client.startWorkflowTestRun({
      project_id: "p1", workflow_key: "wf", pins: { a: { answer: 42 } }, allow_live: ["c"],
    });
    await client.getWorkflowTestRuns("p1");
    await client.rerunWorkflowFromStep("wfr_0", { project_id: "p1", from_step: "c" });
    const calls = fetchImpl.mock.calls.map((call) => [String(call[0]),
      (call[1] as RequestInit | undefined)?.method,
      (call[1] as RequestInit | undefined)?.body ? JSON.parse(String((call[1] as RequestInit).body)) : null]);
    expect(calls).toEqual([
      ["https://boss.test/v1/delivery-workflows/test-runs", "POST",
        { project_id: "p1", workflow_key: "wf", pins: { a: { answer: 42 } }, allow_live: ["c"] }],
      ["https://boss.test/v1/delivery-workflows/test-runs?project_id=p1&limit=25", "GET", null],
      ["https://boss.test/v1/delivery-workflows/runs/wfr_0/rerun", "POST",
        { project_id: "p1", from_step: "c" }],
    ]);
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
      // Writing GETs are read through their POST twin (identity_guard.writing_get).
      "https://boss.test/v1/hardware/places/load?project_id=p1",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchImpl).toHaveBeenCalledWith(
      "https://boss.test/v1/hardware/resolve/load?project_id=p1",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("manages workflow webhook triggers on their project-scoped routes", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ webhooks: [] }), {
      headers: { "content-type": "application/json" },
    }));
    const client = createBossApiClient({ baseUrl: "https://boss.test", fetchImpl });
    await client.listWorkflowWebhooks("p1", "orders");
    await client.rotateWorkflowWebhookSecret("p1", "wht_1");
    await client.listWorkflowWebhookDeliveries("p1", "wht_1");
    expect(fetchImpl).toHaveBeenCalledWith(
      "https://boss.test/v1/delivery-workflows/webhooks?project_id=p1&workflow_key=orders",
      expect.objectContaining({ method: "GET" }));
    expect(fetchImpl).toHaveBeenCalledWith(
      "https://boss.test/v1/delivery-workflows/webhooks/wht_1/rotate",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ project_id: "p1" }) }));
    expect(fetchImpl).toHaveBeenCalledWith(
      "https://boss.test/v1/delivery-workflows/webhooks/wht_1/deliveries?project_id=p1&limit=50",
      expect.objectContaining({ method: "GET" }));
  });
});
