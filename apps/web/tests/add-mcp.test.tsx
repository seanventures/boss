// @vitest-environment happy-dom
/**
 * Add MCP server (docs/design/SKILLS_AND_MCP_HOME.md): one screen. Pick a Popular server or paste
 * a URL, Test connection lists the tools and stores nothing, Give to agents picks who gets it,
 * Add server stores it and grants it. The token goes up once; the drawer shows only `Set, …a1f3`.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, cleanup, click, render, type } from "./_helpers/kit-render";
import { clone, installKitApi, KITS, SERVERS } from "./_helpers/kit-api";

const { AddMcpSheet, targetKind, nameFromUrl } = await import("../app/skills/add-mcp-sheet");
const { McpDrawer } = await import("../app/skills/mcp-drawer");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const disabled = (sel: string) => ($(sel) as HTMLButtonElement).disabled;
const actions = (calls: { body: Record<string, unknown> | null }[]) => calls.map((c) => c.body?.action).filter(Boolean);

describe("adding an MCP server", () => {
  it("a keyless catalogue server: pick, test, add (catalogue_add), done", async () => {
    const calls = installKitApi(clone(KITS.fresh), (url, body) =>
      body?.action === "catalogue_add" ? { server: SERVERS[1], test: { ok: true, tools: [{ name: "a" }], latency_ms: 2 } } : undefined);
    const added = vi.fn();
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={added} />);
    expect(disabled('[data-testid="kit-add-save"]')).toBe(true);
    await click('[data-testid="kit-cat-context7"]');
    expect(($('[data-testid="kit-custom-url"]') as HTMLInputElement).value).toBe("https://mcp.context7.com/mcp");
    await click('[data-testid="kit-add-test"]');
    expect(calls.find((c) => c.body?.action === "test")!.body).toMatchObject({ url: "https://mcp.context7.com/mcp" });
    expect($('[data-testid="kit-add-ok"]')!.textContent).toContain("1 tool");
    // TESTING STORES NOTHING.
    expect(actions(calls)).not.toContain("catalogue_add");
    await click('[data-testid="kit-add-save"]');
    expect(calls.filter((c) => c.body?.action === "catalogue_add").map((c) => c.body)).toEqual([
      expect.objectContaining({ key: "context7", token: null })]);
    expect(added).toHaveBeenCalledWith("mcp_c7", "context7");
  });

  it("a server that needs a token asks for it, says the vault keeps it, and sends it once", async () => {
    const calls = installKitApi(clone(KITS.fresh), (url, body) =>
      body?.action === "catalogue_add" ? { server: SERVERS[1], test: { ok: true, tools: [], latency_ms: 2 } } : undefined);
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={() => {}} />);
    await click('[data-testid="kit-cat-linear"]');
    expect(($('[data-testid="kit-custom-secret"]') as HTMLInputElement).type).toBe("password");
    expect($('[data-testid="kit-add-vault"]')!.textContent).toContain("Saved to the vault");
    expect(disabled('[data-testid="kit-add-test"]')).toBe(true);
    await type('[data-testid="kit-custom-secret"]', "lin_api_secret_1234");
    await click('[data-testid="kit-add-test"]');
    await click('[data-testid="kit-add-save"]');
    expect(calls.find((c) => c.body?.action === "catalogue_add")!.body).toMatchObject({ key: "linear", token: "lin_api_secret_1234" });
  });

  it("click-through: paste a URL, test (fails, then passes), give to two agents, add, and grant", async () => {
    let pass = false;
    const calls = installKitApi(clone(KITS.fresh), (url, body) =>
      body?.action === "test" ? { ok: pass, tools: pass ? [{ name: "lookup_order" }, { name: "refund" }] : [], latency_ms: 1, error: pass ? null : "401 Unauthorized" }
        : body?.action === "register" ? { ...SERVERS[0], id: "mcp_orders", name: "orders" } : undefined);
    const added = vi.fn();
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={added} defaultAgents={["pm"]} />);
    await type('[data-testid="kit-custom-url"]', "https://mcp.orders.example.com/mcp");
    expect(($('[data-testid="kit-custom-name"]') as HTMLInputElement).value).toBe("orders");
    await click('[data-testid="kit-custom-auth-bearer"]');
    await type('[data-testid="kit-custom-secret"]', "tok_secret_9876");
    await click('[data-testid="kit-add-test"]');
    expect($('[data-testid="kit-add-error"]')!.textContent).toBe("Token rejected");
    expect(disabled('[data-testid="kit-add-save"]')).toBe(true);
    pass = true;
    await click('[data-testid="kit-add-test"]');
    expect($$('[data-testid="kit-add-tools"] .kit-chip').map((c) => c.textContent)).toEqual(["lookup_order", "refund"]);
    // The agent passed in starts on; one more is picked.
    expect($('[data-testid="kit-add-agent-pm"]')!.getAttribute("aria-pressed")).toBe("true");
    expect($('[data-testid="kit-add-agent-qa"]')).toBeNull();
    await click('[data-testid="kit-add-agents-all"]');
    await click('[data-testid="kit-add-agent-qa"]');
    await click('[data-testid="kit-add-save"]');
    const register = calls.find((c) => c.body?.action === "register")!;
    expect(register.body!.server).toMatchObject({ name: "orders", url: "https://mcp.orders.example.com/mcp",
                                                  auth: { type: "bearer", secret: "tok_secret_9876" } });
    const grants = calls.filter((c) => c.body?.action === "grant").map((c) => [c.body!.server_id, c.body!.role_key]);
    expect(grants).toEqual([["mcp_orders", "pm"], ["mcp_orders", "qa"]]);
    expect(actions(calls).indexOf("register")).toBeLessThan(actions(calls).indexOf("grant"));
    expect(added).toHaveBeenCalledWith("mcp_orders", "orders");
  });

  it("a changed URL voids the last test", async () => {
    installKitApi(clone(KITS.fresh));
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={() => {}} />);
    await type('[data-testid="kit-custom-url"]', "https://a.example.com/mcp");
    await click('[data-testid="kit-add-test"]');
    expect(disabled('[data-testid="kit-add-save"]')).toBe(false);
    await type('[data-testid="kit-custom-url"]', "https://b.example.com/mcp");
    expect(disabled('[data-testid="kit-add-save"]')).toBe(true);
  });

  it("a command is not run from here: it points at a template", async () => {
    installKitApi(clone(KITS.fresh));
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={() => {}} />);
    await type('[data-testid="kit-custom-url"]', "npx -y @acme/mcp-server");
    expect($('[data-testid="kit-add-command"]')).not.toBeNull();
    expect(disabled('[data-testid="kit-add-test"]')).toBe(true);
    expect([targetKind("npx -y x"), targetKind("https://x"), targetKind("stdio://p1/t"), targetKind(" ")])
      .toEqual(["command", "http", "stdio", "empty"]);
    expect(nameFromUrl("https://mcp.linear.app/mcp")).toBe("linear");
  });

  it("OAuth needs no test: Add & sign in stores it and opens the sign-in", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const calls = installKitApi(clone(KITS.fresh), (url, body) =>
      body?.action === "register" ? { ...SERVERS[0], id: "mcp_o", name: "o" }
        : body?.action === "oauth_start" ? { redirect: "https://idp.example/authorize" } : undefined);
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={() => {}} />);
    await type('[data-testid="kit-custom-url"]', "https://mcp.o.example.com/mcp");
    await click('[data-testid="kit-custom-auth-oauth"]');
    expect($('[data-testid="kit-add-test"]')).toBeNull();
    await click('[data-testid="kit-add-save"]');
    expect(actions(calls)).toEqual(expect.arrayContaining(["register", "oauth_start"]));
  });

  it("where the agent does not exist yet (the wizard), there is no Give to agents", async () => {
    installKitApi(clone(KITS.fresh));
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={() => {}} grants={false} />);
    expect($('[data-testid="kit-add-agents"]')).toBeNull();
  });

  it("Browse all opens the connector list in the same sheet, and Back returns", async () => {
    installKitApi(clone(KITS.fresh), (url) => (url.includes("view=connectors") ? { catalog_version: "1", total: 0, categories: [], items: [] } : undefined));
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={() => {}} />);
    await click('[data-testid="kit-add-browse"]');
    expect($('[data-testid="kit-connectors"]')).not.toBeNull();
    await click('[data-testid="kit-add-back"]');
    expect($('[data-testid="kit-custom-url"]')).not.toBeNull();
  });

  it("a stored token is shown only as its last four", async () => {
    installKitApi();
    await render(<McpDrawer projectId="p1" actor="ceo" server={SERVERS[1] as never} crew={[]} onClose={() => {}}
                            onChanged={() => {}} onRemoved={() => {}} />);
    expect($('[data-testid="kit-mcp-secret-set"]')!.textContent).toContain("Set, …a1f3");
    expect($('[data-testid="kit-mcp-secret"]')).toBeNull();
  });
});
