// @vitest-environment happy-dom
/**
 * + Add MCP (skills-mcp spec §5.5, J4): a keyless catalogue server is one press; one that needs a
 * token asks for it first; a custom server is tested with what was typed and registered only
 * after the test passed. The token goes up once and the drawer shows only `Set · …a1f3`.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render, type } from "./_helpers/kit-render";
import { clone, installKitApi, KITS, SERVERS } from "./_helpers/kit-api";

const { AddMcpSheet } = await import("../app/skills/add-mcp-sheet");
const { McpDrawer } = await import("../app/skills/mcp-drawer");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("adding an MCP server", () => {
  it("a keyless catalogue server is one press", async () => {
    const calls = installKitApi(clone(KITS.fresh), (url, body) =>
      body?.action === "catalogue_add" ? { server: SERVERS[1], test: { ok: true, tools: [{ name: "a" }], latency_ms: 2 } } : undefined);
    const added = vi.fn();
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={added} />);
    await click('[data-testid="kit-cat-add-context7"]');
    expect(calls.filter((c) => c.body?.action === "catalogue_add").map((c) => c.body)).toEqual([
      expect.objectContaining({ key: "context7", token: null })]);
    expect(added).toHaveBeenCalledWith("mcp_c7");
  });

  it("a server that needs a token asks for it before anything is sent", async () => {
    const calls = installKitApi(clone(KITS.fresh), (url, body) =>
      body?.action === "catalogue_add" ? { server: SERVERS[1], test: { ok: true, tools: [], latency_ms: 2 } } : undefined);
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={() => {}} />);
    await click('[data-testid="kit-cat-add-linear"]');
    expect(calls.some((c) => c.body?.action === "catalogue_add")).toBe(false);
    expect(($('[data-testid="kit-cat-token-linear"]') as HTMLInputElement).type).toBe("password");
    await type('[data-testid="kit-cat-token-linear"]', "lin_api_secret_1234");
    await click('[data-testid="kit-cat-add-linear"]');
    expect(calls.find((c) => c.body?.action === "catalogue_add")!.body).toMatchObject({ key: "linear", token: "lin_api_secret_1234" });
  });

  it("a custom server is tested first and registered only after it passed", async () => {
    let pass = false;
    const calls = installKitApi(clone(KITS.fresh), (url, body) =>
      body?.action === "test" ? { ok: pass, tools: [], latency_ms: 1, error: pass ? null : "401 Unauthorized" } : undefined);
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={() => {}} />);
    await click('[data-testid="kit-add-custom"]');
    await type('[data-testid="kit-custom-name"]', "tracker");
    await type('[data-testid="kit-custom-url"]', "https://mcp.example/mcp");
    await click('[data-testid="kit-custom-auth-bearer"]');
    await type('[data-testid="kit-custom-secret"]', "tok_secret_9876");
    await click('[data-testid="kit-custom-save"]');
    expect(calls.some((c) => c.body?.action === "register")).toBe(false);
    pass = true;
    await click('[data-testid="kit-custom-save"]');
    const register = calls.find((c) => c.body?.action === "register")!;
    expect(register.body!.server).toMatchObject({ name: "tracker", auth: { type: "bearer", secret: "tok_secret_9876" } });
  });

  it("a stored token is shown only as its last four", async () => {
    installKitApi();
    await render(<McpDrawer projectId="p1" actor="ceo" server={SERVERS[1] as never} crew={[]} onClose={() => {}}
                            onChanged={() => {}} onRemoved={() => {}} />);
    expect($('[data-testid="kit-mcp-secret-set"]')!.textContent).toContain("Set · …a1f3");
    expect($('[data-testid="kit-mcp-secret"]')).toBeNull();
  });
});
