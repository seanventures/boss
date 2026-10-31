// @vitest-environment happy-dom
/**
 * Agents › Outside agents (KG P6b console): the data layer the panel posts through
 * (app/agents/outside-agents-actions.ts). Reads list, writes the secret to the secret store
 * and only the reference to register, turns a non-ok answer into the API's own sentence, and
 * never lets a function reject out — every door returns a typed Result.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const actions = await import("../app/agents/outside-agents-actions");

afterEach(() => { vi.unstubAllGlobals(); });

function mockFetch(over: Record<string, { status?: number; body: unknown }> = {}) {
  const calls: string[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://console.test");
    calls.push(`${(init?.method ?? "GET").toUpperCase()} ${url.pathname}`);
    const answer = over[url.pathname];
    const r = answer ?? { status: 200, body: { items: [] } };
    return new Response(JSON.stringify(r.body), { status: r.status ?? 200, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

describe("reading the outside agents list", () => {
  it("passes the project and returns the items", async () => {
    const { calls } = mockFetch({ "/v1/external-agents": { body: { items: [{ id: "ext_1" }] } } });
    const r = await actions.listOutsideAgents("p1");
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.data.items).toHaveLength(1);
    expect(calls.some((c) => c.startsWith("GET /v1/external-agents"))).toBe(true);
  });

  it("turns a non-ok answer into the API's own sentence, never throwing", async () => {
    mockFetch({ "/v1/external-agents": { status: 403, body: { detail: "not your project" } } });
    const r = await actions.listOutsideAgents("p1");
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain("not your project");
  });
});

describe("writing an outside agent", () => {
  it("writes the secret through /v1/secrets and only the reference to /v1/external-agents", async () => {
    const { calls } = mockFetch({
      "/v1/secrets": { body: { secret_ref_id: "sref_new" } },
      "/v1/external-agents": { body: { id: "ext_new" } },
    });
    const secret = await actions.putOutsideAgentSecret("p1", "alice", { name: "external-agent/Bot", value: "sk-secret" });
    expect(secret.ok).toBe(true);
    if (secret.ok) expect(secret.data.secret_ref_id).toBe("sref_new");
    const reg = await actions.registerOutsideAgent("p1", "alice", {
      display_name: "Bot", endpoint_url: "https://partner.example.com/agent",
      secret_ref_id: "sref_new", sponsor_principal_id: "alice",
    });
    expect(reg.ok).toBe(true);
    expect(calls.some((c) => c === "POST /v1/secrets")).toBe(true);
    expect(calls.some((c) => c === "POST /v1/external-agents")).toBe(true);
  });

  it("reports reachable, suspends and removes through the agent's own paths", async () => {
    const { calls } = mockFetch({
      "/v1/external-agents/ext_1/test": { body: { reachable: true } },
      "/v1/external-agents/ext_1/suspend": { body: { id: "ext_1" } },
      "/v1/external-agents/ext_1/remove": { body: { id: "ext_1" } },
    });
    const t = await actions.testOutsideAgent("p1", "alice", "ext_1");
    expect(t.ok && t.data.reachable).toBe(true);
    expect((await actions.suspendOutsideAgent("p1", "alice", "ext_1")).ok).toBe(true);
    expect((await actions.removeOutsideAgent("p1", "alice", "ext_1")).ok).toBe(true);
    expect(calls).toContain("POST /v1/external-agents/ext_1/test");
    expect(calls).toContain("POST /v1/external-agents/ext_1/suspend");
    expect(calls).toContain("POST /v1/external-agents/ext_1/remove");
  });

  it("surfaces a refusal on remove in the API's words", async () => {
    mockFetch({ "/v1/external-agents/ext_1/remove": { status: 422, body: { detail: "agent already suspended" } } });
    const r = await actions.removeOutsideAgent("p1", "alice", "ext_1");
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain("agent already suspended");
  });

  it("afterOutsideAgentWrite calls the reload it is given", () => {
    const reload = vi.fn();
    actions.afterOutsideAgentWrite(reload);
    expect(reload).toHaveBeenCalledOnce();
  });
});
