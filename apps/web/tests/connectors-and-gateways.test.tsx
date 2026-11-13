// @vitest-environment happy-dom
/**
 * Add MCP › Connectors and the Gateways sheet (BOSSAPP-849..851), clicked through: picking a
 * connector pre-fills the Custom form and its tool tiers ride on the register; a stdio
 * connector without its template offers Create template (host:exec); a gateway shows its token
 * once and revokes by id.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render } from "./_helpers/kit-render";
import { clone, installKitApi, KITS, SERVERS } from "./_helpers/kit-api";

const { AddMcpSheet } = await import("../app/skills/add-mcp-sheet");
const { GatewaysSheet } = await import("../app/skills/gateways-sheet");
const { toolOverrides } = await import("../lib/kit-api");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const LINEAR = {
  key: "linear", name: "Linear", category: "project", transport: "http", description: "Issues",
  auth_kind: "oauth", token_url: "https://linear.app/settings/api", scopes_tip: null,
  setup: "Sign in with Linear, or paste a personal API key.", risk_notes: "Can change issues.",
  docs_url: null, default_tier: 2, added: false, template_ready: true, definition_version: 1,
  prefill: { name: "linear", transport: "http", url: "https://mcp.linear.app/mcp",
             auth: { type: "bearer", header_name: null }, risk_tier: 2, data_boundary: "internal",
             description: "Issues", tool_tiers: { "get_*": 0, "create_*": 2 }, tool_policies: { "create_*": "ask" },
             oauth: { discovery_url: null, scopes: ["read"], token_fallback: "bearer" },
             provenance: { source: "connector_catalog", connector_key: "linear" }, stdio_template: null },
};
const POSTGRES = {
  ...LINEAR, key: "postgres", name: "Postgres", category: "data", transport: "stdio", auth_kind: "bearer",
  template_ready: false,
  prefill: { ...LINEAR.prefill, name: "postgres", transport: "stdio", url: "stdio://p1/postgres",
             stdio_template: { name: "postgres", image: "uv", argv: ["uvx", "postgres-mcp"], network: "bridge",
                               env_allowlist: ["DATABASE_URI"], env_defaults: {}, env_from_auth: "DATABASE_URI",
                               idle_ttl_seconds: 600, reuse_key: "credential", connector_key: "postgres", description: "" } },
};
const CATALOG = { catalog_version: "t", total: 2, categories: [{ key: "project", count: 1 }, { key: "data", count: 1 }],
                  items: [LINEAR, POSTGRES] };

describe("Add MCP › Connectors", () => {
  it("picking a connector pre-fills the form and its tool tiers ride on the register", async () => {
    const calls = installKitApi(clone(KITS.fresh), (url, body, method) => {
      if (url.includes("view=connectors")) return CATALOG;
      if (body?.action === "test") return { ok: true, tools: [{ name: "get_issue" }, { name: "create_issue" }], latency_ms: 1 };
      if (body?.action === "register") return { ...SERVERS[0], id: "mcp_lin" };
      return method === "POST" ? {} : undefined;
    });
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={() => {}} />);
    await click('[data-testid="kit-add-connectors"]');
    await new Promise((r) => setTimeout(r, 200));
    await click('[data-testid="kit-connector-use-linear"]');
    expect(($('[data-testid="kit-custom-url"]') as HTMLInputElement).value).toBe("https://mcp.linear.app/mcp");
    expect(($('[data-testid="kit-custom-name"]') as HTMLInputElement).value).toBe("linear");
    expect($('[data-testid="kit-custom-picked"]')!.textContent).toContain("Sign in with Linear");
    await click('[data-testid="kit-custom-save"]');
    const register = calls.find((c) => c.body?.action === "register")!;
    expect(register.body!.server).toMatchObject({
      url: "https://mcp.linear.app/mcp", transport: "http", risk_tier: 2,
      tool_risk_overrides: { get_issue: 0, create_issue: 2 },
      provenance: { source: "connector_catalog", connector_key: "linear" } });
    expect(calls.filter((c) => c.body?.action === "policy").map((c) => c.body)).toEqual([
      expect.objectContaining({ server_id: "mcp_lin", tool: "create_issue", policy: "ask" })]);
  });

  it("a stdio connector without its template offers Create template first", async () => {
    const calls = installKitApi(clone(KITS.fresh), (url, body) => {
      if (url.includes("view=connectors")) return CATALOG;
      if (body?.action === "stdio_template_create") return { id: "t1", name: "postgres", url: "stdio://p1/postgres" };
      return undefined;
    });
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={() => {}} />);
    await click('[data-testid="kit-add-connectors"]');
    await new Promise((r) => setTimeout(r, 200));
    expect($('[data-testid="kit-connector-use-postgres"]')).toBeNull();
    await click('[data-testid="kit-connector-template-postgres"]');
    expect(calls.find((c) => c.body?.action === "stdio_template_create")!.body!.template)
      .toMatchObject({ name: "postgres", env_from_auth: "DATABASE_URI", network: "bridge" });
  });

  it("tool globs match the most specific pattern first", () => {
    const out = toolOverrides({ ...LINEAR.prefill, tool_tiers: { "*": 1, "get_*": 0, get_secret: 3 }, tool_policies: {} } as never,
                              ["get_issue", "get_secret", "other"]);
    expect(out.tiers).toEqual({ get_issue: 0, get_secret: 3, other: 1 });
  });
});

describe("Gateways", () => {
  const GATEWAY = { id: "mcpgw_1", name: "laptop", status: "active", path: "/mcp/gw/mcpgw_1", allowed_actions: [],
                    profile: { servers: [{ server_id: SERVERS[0].id, tools: null, caps: {} }] }, created_at: "",
                    tokens: [{ id: "tok_1", label: null, last4: "ab12", expires_at: "2027-01-01T00:00:00Z", revoked_at: null,
                               last_used_at: null, status: "active", created_at: "" }] };

  it("creates a gateway, shows its token once, and revokes a token by id", async () => {
    const calls = installKitApi(clone(KITS.fresh), (url, body) => {
      if (url.includes("view=gateways")) return { items: [GATEWAY] };
      if (body?.action === "gateway_create") return { gateway: GATEWAY, token: "boss_gw_SECRET_once" };
      if (body?.action === "gateway_token_revoke") return GATEWAY;
      return undefined;
    });
    await render(<GatewaysSheet projectId="p1" actor="ceo" servers={SERVERS as never} onClose={() => {}} />);
    await click('[data-testid="kit-gw-new"]');
    const nameInput = $('[data-testid="kit-gw-name"]') as HTMLInputElement;
    const { type } = await import("./_helpers/kit-render");
    await type('[data-testid="kit-gw-name"]', "laptop");
    expect(nameInput.value).toBe("laptop");
    await click(`[data-testid="kit-gw-server-${SERVERS[0].name}"]`);
    await click('[data-testid="kit-gw-create-save"]');
    const created = calls.find((c) => c.body?.action === "gateway_create")!.body!;
    expect(created).toMatchObject({ name: "laptop", profile: { servers: [{ server_id: SERVERS[0].id, tools: null }] } });
    expect($('[data-testid="kit-gw-token-value"]')!.textContent).toBe("boss_gw_SECRET_once");
    await click('[data-testid="kit-gw-token-revoke-ab12"]');
    expect(calls.find((c) => c.body?.action === "gateway_token_revoke")!.body)
      .toMatchObject({ gateway_id: "mcpgw_1", token_id: "tok_1" });
  });
});
