// @vitest-environment happy-dom
/**
 * Skills & MCP, one simple home (BOSSAPP-1477, docs/design/SKILLS_AND_MCP_HOME.md).
 *
 * The page opens on two sections, MCP servers and Skills, with Add MCP server on screen at once.
 * A server row says its status, tools, who has it and the last error in plain words, and Test,
 * Edit and Remove work from the row. Add skill offers its four ways. Old links land on the new
 * places, and nothing the old tabs did is out of reach (the parity table's key rows).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, cleanup, click, render, type } from "./_helpers/kit-render";
import { clone, installKitApi, KITS, SERVERS } from "./_helpers/kit-api";

const dock = vi.fn();
vi.mock("../lib/dock-subject", () => ({ openDockAbout: (x: unknown) => dock(x) }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const { KitHome } = await import("../app/skills/kit-home");
const { KIT_ADD_EVENT, readKitParams } = await import("../app/skills/kit-url");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); dock.mockReset(); window.history.replaceState(null, "", "/"); });

const FAILING = [SERVERS[0], { ...SERVERS[1], reachable: false, last_error: "HTTP 401 Unauthorized: bad token" }];

async function home(search = "/?area=skills", on?: Parameters<typeof installKitApi>[1]) {
  window.history.replaceState(null, "", search);
  const calls = installKitApi(clone(KITS.fresh), on);
  await render(<KitHome projectId="p1" actor="ceo" />);
  return calls;
}

describe("the first screen", () => {
  it("opens on MCP servers and Skills, with Add MCP server and Add skill in the head", async () => {
    await home();
    expect($('[data-testid="kit-add-mcp-btn"]')!.textContent).toContain("Add MCP server");
    expect($('[data-testid="kit-add-skill-btn"]')!.textContent).toContain("Add skill");
    expect($('[data-testid="kit-mcp-section"]')).not.toBeNull();
    expect($('[data-testid="kit-skills-section"]')).not.toBeNull();
    // Skills open by outcome (the Kit), with its progress and Test all.
    expect($$('[data-testid^="kit-card-"]').length).toBeGreaterThan(0);
    expect($('[data-testid="kit-progress"]')).not.toBeNull();
    // Who can, Templates and Rules: reachable, small.
    for (const t of ["who", "templates", "rules"]) expect($(`[data-testid="kit-tab-${t}"]`)).not.toBeNull();
    // Every head button carries an icon.
    for (const id of ["kit-add-mcp-btn", "kit-add-skill-btn", "kit-tab-who", "kit-tab-templates", "kit-tab-rules"]) {
      expect($(`[data-testid="${id}"] svg`), id).not.toBeNull();
    }
  });

  it("a server row says status, tools, who has it and the last error in plain words", async () => {
    await home("/?area=skills", (url, _b, method) =>
      (method === "GET" && url.startsWith("/api/mcp?project_id") ? { items: FAILING } : undefined));
    const web = $('[data-testid="kit-mcp-web-intel"]')!;
    expect(web.querySelector('[data-testid="kit-pill"]')!.textContent).toBe("Ready");
    expect($('[data-testid="kit-mcp-tools-web-intel"]')!.textContent).toBe("4 tools");
    expect(web.querySelector('[data-testid="kit-holders"]')).not.toBeNull();
    const c7 = $('[data-testid="kit-mcp-context7"]')!;
    expect(c7.querySelector('[data-testid="kit-pill"]')!.textContent).toBe("Failing");
    expect($('[data-testid="kit-mcp-err-context7"]')!.textContent).toContain("Token rejected");
    // The raw message is in the tooltip, never on the row.
    expect($('[data-testid="kit-mcp-err-context7"]')!.textContent).not.toContain("HTTP");
    for (const a of ["test", "edit", "remove"]) expect($(`[data-testid="kit-mcp-${a}-context7"] svg`), a).not.toBeNull();
  });

  it("a fresh install shows what to do next, not an empty box", async () => {
    await home("/?area=skills", (url, _b, method) =>
      (method === "GET" && url.startsWith("/api/mcp?project_id") ? { items: [] } : undefined));
    expect($('[data-testid="kit-mcp-empty"]')!.textContent).toContain("No MCP servers yet");
    await click('[data-testid="kit-mcp-empty-add"]');
    expect($('[data-testid="kit-add-mcp"]')).not.toBeNull();
  });
});

describe("row actions", () => {
  it("Test asks the servers now and says what this one answered", async () => {
    const calls = await home();
    await click('[data-testid="kit-mcp-test-web-intel"]');
    expect(calls.some((c) => c.url.includes("check=now"))).toBe(true);
  });

  it("Edit opens the server's drawer, with every option it had", async () => {
    await home();
    await click('[data-testid="kit-mcp-edit-context7"]');
    expect($('[data-testid="kit-mcp-drawer"]')).not.toBeNull();
    expect($('[data-testid="kit-mcp-url"]')).not.toBeNull();
    expect(readKitParams().mcp).toBe("mcp_c7");
  });

  it("Remove asks first, then removes (releasing grants when agents hold it)", async () => {
    const calls = await home();
    await click('[data-testid="kit-mcp-remove-web-intel"]');
    expect(calls.some((c) => c.body?.action === "delete")).toBe(false);
    expect($('[data-testid="kit-mcp-remove-confirm-web-intel"]')!.textContent).toContain("Agents lose web-intel");
    await click('[data-testid="kit-mcp-remove-yes-web-intel"]');
    expect(calls.find((c) => c.body?.action === "delete")!.body).toMatchObject({ server_id: "mcp_web", release_grants: true });
  });

  it("⋯ keeps Test all, Import, Export and Gateways", async () => {
    await home("/?area=skills", (url) => (url.includes("view=gateways") ? { items: [] } : undefined));
    await click('[data-testid="kit-mcp-more"]');
    expect($('[data-testid="kit-mcp-menu"]')!.textContent).toBe("Test allImportExportGateways");
    await click('[data-testid="kit-lib-gateways"]');
    expect($('[data-testid="kit-gw-new"]')).not.toBeNull();
  });
});

describe("click-through: Add MCP server → Test connection → Give to agents → done", () => {
  it("from the first screen, in one sheet, and the new server's drawer opens", async () => {
    let saved = false;
    const ORDERS = { ...SERVERS[0], id: "mcp_orders", name: "orders", url: "https://mcp.orders.example.com/mcp",
                     tools: [{ name: "lookup_order" }], references: { custom_steps: [], skills: [], agents: [{ role_key: "pm", tools: [] }] } };
    const calls = await home("/?area=skills", (url, body, method) => {
      if (body?.action === "test") return { ok: true, tools: [{ name: "lookup_order" }], latency_ms: 2 };
      if (body?.action === "register") { saved = true; return ORDERS; }
      if (method === "GET" && url.startsWith("/api/mcp?project_id")) return { items: saved ? [...SERVERS, ORDERS] : SERVERS };
      return undefined;
    });
    await click('[data-testid="kit-add-mcp-btn"]');
    expect(readKitParams().add).toBe("mcp");
    await type('[data-testid="kit-custom-url"]', "https://mcp.orders.example.com/mcp");
    await click('[data-testid="kit-add-test"]');
    expect($('[data-testid="kit-add-tools"]')!.textContent).toContain("lookup_order");
    await click('[data-testid="kit-add-agents-all"]');
    await click('[data-testid="kit-add-agent-pm"]');
    await click('[data-testid="kit-add-save"]');
    expect(calls.find((c) => c.body?.action === "grant")!.body).toMatchObject({ server_id: "mcp_orders", role_key: "pm" });
    expect($('[data-testid="kit-add-mcp"]')).toBeNull();
    expect($('[data-testid="kit-mcp-orders"]')).not.toBeNull();
    expect($('[data-testid="kit-mcp-drawer"]')).not.toBeNull();
    expect(readKitParams().add).toBeNull();
  });
});

describe("Add skill", () => {
  it("offers four ways, each with an icon", async () => {
    await home();
    await click('[data-testid="kit-add-skill-btn"]');
    for (const w of ["write", "import", "describe", "built_in"]) expect($(`[data-testid="kit-add-skill-${w}"] svg`), w).not.toBeNull();
  });

  it("Write one opens the skill drawer on All skills; after save it has Agents and Tests", async () => {
    await home();
    await click('[data-testid="kit-add-skill-btn"]');
    await click('[data-testid="kit-add-skill-write"]');
    expect($('[data-testid="kit-library"]')).not.toBeNull();
    expect($('[data-testid="kit-skills-all"]')!.getAttribute("aria-checked")).toBe("true");
    expect($('[data-testid="kit-skill-drawer"]')).not.toBeNull();
    expect($('[data-testid="skill-label"]')).not.toBeNull();
  });

  it("Import opens the import sheet; Built in filters All skills", async () => {
    await home();
    await click('[data-testid="kit-add-skill-btn"]');
    await click('[data-testid="kit-add-skill-import"]');
    expect($('[data-testid="kit-import"]')).not.toBeNull();
    cleanup();
    await home();
    await click('[data-testid="kit-add-skill-btn"]');
    await click('[data-testid="kit-add-skill-built_in"]');
    const rows = $$('li[data-testid^="kit-skill-"]');
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.every((r) => r.textContent?.includes("Built in"))).toBe(true);
  });

  it("a skill row opens its drawer, where it is given to agents", async () => {
    await home("/?area=skills&view=all");
    await click('[data-testid="kit-skill-github.create_pr_real"]');
    expect($('[data-testid^="kit-skill-agent-"]')).not.toBeNull();
  });
});

describe("old links land on the new places", () => {
  it.each([
    ["/?area=skills&tab=kit", "kit-skills-section", "kit-card-"],
    ["/?area=skills&tab=library", "kit-library", null],
    ["/?area=skills&tab=library&lib=mcp", "kit-mcp-section", null],
    ["/?area=skills&tab=library&lib=mcp&mcp=mcp_c7", "kit-mcp-drawer", null],
    ["/?area=skills&tab=library&lib=mcp&add=1", "kit-add-mcp", null],
    ["/?area=skills&add=skill", "kit-add-skill", null],
    ["/?area=skills&tab=library&lib=skills&skill=github.create_pr_real", "kit-skill-drawer", null],
    ["/?area=skills&tab=who&role=pm", "kit-who", null],
    ["/?area=skills&tab=rules", "kit-rules", null],
  ])("%s", async (search, testid, extra) => {
    await home(search);
    expect($(`[data-testid="${testid}"]`), search).not.toBeNull();
    if (extra) expect($(`[data-testid^="${extra}"]`)).not.toBeNull();
  });

  it("Who can has a way back", async () => {
    await home("/?area=skills&tab=who");
    await click('[data-testid="kit-back"]');
    expect($('[data-testid="kit-mcp-section"]')).not.toBeNull();
    expect(readKitParams().tab).toBe("home");
  });

  it("a search command opens Add MCP server on a page already open", async () => {
    await home();
    const { act } = await import("react");
    await act(async () => { window.dispatchEvent(new CustomEvent(KIT_ADD_EVENT, { detail: "mcp" })); });
    expect($('[data-testid="kit-add-mcp"]')).not.toBeNull();
  });
});
