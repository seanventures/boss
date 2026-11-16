// @vitest-environment happy-dom
/**
 * A person adds one remote MCP server from the Library and sees the tool the test found.
 * The adapter is the fake control plane: nothing is requested at the server's own URL.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render, type } from "./_helpers/kit-render";
import { clone, installKitApi, KITS, SERVERS } from "./_helpers/kit-api";

vi.mock("../lib/dock-subject", () => ({ openDockAbout: () => {} }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const { Library } = await import("../app/skills/library");
const { readKitParams } = await import("../app/skills/kit-url");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const ORDERS = {
  id: "mcp_orders", name: "orders", url: "https://mcp.example/mcp", enabled: true, risk_tier: 0,
  data_boundary: "internal", description: null, trusted: false, reachable: true, last_error: null,
  tools: [{ name: "lookup_order", description: "Find one order" }],
  tool_policy: {}, tool_risk_overrides: {}, provenance: { source: "custom" }, publisher: {},
  auth: { type: "none", header_name: null, set: false, last4: null },
  references: { custom_steps: [], skills: [], agents: [] },
};

describe("adding one remote MCP server", () => {
  it("shows the tool the test found", async () => {
    let saved = false;
    const kit = clone(KITS.fresh);
    const calls = installKitApi(kit, (url, body, method) => {
      if (body?.action === "test") return { ok: true, tools: [{ name: "lookup_order", description: "Find one order" }], latency_ms: 3 };
      if (body?.action === "register") { saved = true; return ORDERS; }
      if (method === "GET" && url.startsWith("/api/mcp") && !url.includes("view=")) {
        return { items: saved ? [...SERVERS, ORDERS] : SERVERS };
      }
      return undefined;
    });
    window.history.replaceState(null, "", "/?area=skills&tab=library&lib=mcp");
    await render(<Library projectId="p1" actor="ceo" kit={kit} crew={kit.crew ?? []} initial={readKitParams()} onKitChanged={() => {}} />);
    await click('[data-testid="kit-lib-add-mcp"]');
    await click('[data-testid="kit-add-custom"]');
    await type('[data-testid="kit-custom-name"]', "orders");
    await type('[data-testid="kit-custom-url"]', "https://mcp.example/mcp");
    await click('[data-testid="kit-custom-save"]');
    const tool = $('[data-testid="kit-tool-mcp:orders.lookup_order"]');
    expect(tool).not.toBeNull();
    expect(tool!.textContent).toContain("lookup_order");
    expect($('[data-testid="kit-mcp-orders"]')!.textContent).toContain("1 tool");
    expect(calls.some((c) => c.url.includes("https://mcp.example"))).toBe(false);
    expect(calls.some((c) => c.body?.action === "test" && c.body.url === "https://mcp.example/mcp")).toBe(true);
  });
});
