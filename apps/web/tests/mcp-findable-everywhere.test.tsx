// @vitest-environment happy-dom
/**
 * MCP is findable from everywhere (BOSSAPP-1478, docs/design/SKILLS_AND_MCP_HOME.md).
 *
 * Wherever an agent or a step needs a tool, Add MCP server is at most two clicks away, and it is
 * the SAME sheet as on Skills & MCP (AddMcpSheet), never a second form: the agent page's Loadout,
 * the create-agent wizard, the workflow step editor's MCP picker, search (⌘K) and Get started.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Palette } from "../lib/workflow-editor-types";
import { codeOnly } from "./_helpers/source";
import { $, cleanup, click, render, type } from "./_helpers/kit-render";
import { clone, installKitApi, KITS, SERVERS } from "./_helpers/kit-api";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const { LoadoutGrants } = await import("../app/skills/agent-abilities");
const { AgentBotWizard } = await import("../app/agents/create-wizard");
const { AgentQuickCreate } = await import("../app/agent-quick-create");
const { ScopePicker } = await import("../app/workflows/editor/inspector/scope-picker");
const { addCommandsFor, addHref } = await import("../app/skills/kit-url");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const GRANTABLE = { skills: [], mcp_servers: [{ mcp_server_id: "mcp_1", server: "GitHub", url: "http://mcp.example/github", enabled: true, risk_tier: 0 }], agents: [] };
const PALETTE = { capabilities: [], customSteps: [], blocks: [], humanRole: "human", riskFloor: 2,
  agents: [{ role_key: "sre", name: "Beacon", skills: [], mcp: ["boss-devops-reference"] }],
  bindable: { skills: [], mcp_servers: [] } } as unknown as Palette;

describe("Add MCP server in at most two clicks", () => {
  it("the agent page: Loadout › MCP › Add ▾ › Add MCP server, this agent pre-chosen, and the grant lands", async () => {
    const changed = vi.fn();
    const calls = installKitApi(clone(KITS.fresh), (_u, body) =>
      body?.action === "test" ? { ok: true, tools: [{ name: "t" }], latency_ms: 1 }
        : body?.action === "register" ? { ...SERVERS[0], id: "mcp_new", name: "new" } : undefined);
    await render(<LoadoutGrants projectId="p1" roleKey="pm" actor="ceo" canEdit onChanged={changed} onFromFile={() => {}}
                                skills={[]} available={[]} servers={[]} availableServers={[]} allowed={[]} forbidden={[]}
                                initialAbilities={[]} />);
    await click('[data-testid="loadout-mcp-add"]');           // 1
    await click('[data-testid="loadout-mcp-new"]');           // 2
    expect($('[data-testid="kit-add-mcp"]')).not.toBeNull();
    expect($('[data-testid="kit-add-agent-pm"]')!.getAttribute("aria-pressed")).toBe("true");
    await type('[data-testid="kit-custom-url"]', "https://mcp.new.example.com/mcp");
    await click('[data-testid="kit-add-test"]');
    await click('[data-testid="kit-add-save"]');
    expect(calls.find((c) => c.body?.action === "grant")!.body).toMatchObject({ server_id: "mcp_new", role_key: "pm" });
    expect(changed).toHaveBeenCalled();
    expect($('[data-testid="kit-add-mcp"]')).toBeNull();
  });

  it("the create-agent wizard: one click on the Skills and MCP step; the new server is ticked, not granted", async () => {
    const calls = installKitApi(clone(KITS.fresh), (url, body) =>
      url.includes("/v1/agents/grantable") ? GRANTABLE
        : body?.action === "test" ? { ok: true, tools: [{ name: "t" }], latency_ms: 1 }
        : body?.action === "register" ? { ...SERVERS[0], id: "mcp_2", name: "two" } : undefined);
    await render(<AgentBotWizard projectId="p1" actor="ceo" onClose={() => {}} />);
    await click('[data-testid="abw-kind-agent"]');
    await click('[data-testid="abw-next"]');
    await type('[data-testid="abw-name"]', "Release auditor");
    await type('[data-testid="abw-purpose"]', "Checks a release");
    await click('[data-testid="abw-next"]');
    await click('[data-testid="abw-add-mcp"]');               // 1
    expect($('[data-testid="kit-add-mcp"]')).not.toBeNull();
    expect($('[data-testid="kit-add-agents"]')).toBeNull();
    await type('[data-testid="kit-custom-url"]', "https://mcp.two.example.com/mcp");
    await click('[data-testid="kit-add-test"]');
    await click('[data-testid="kit-add-save"]');
    expect(calls.some((c) => c.body?.action === "grant")).toBe(false);
    expect(calls.some((c) => c.body?.action === "register")).toBe(true);
    expect($('[data-testid="kit-add-mcp"]')).toBeNull();
    expect($('[data-testid="agent-bot-wizard"]')).not.toBeNull();
  });

  it("New agent (the dialog Agent Studio's New agent opens): one click under MCP; the new server is ticked", async () => {
    const many = { ...GRANTABLE, skills: Array.from({ length: 10 }, (_, i) => ({
      skill_id: `s.${i}`, label: `S ${i}`, summary: "", risk_tier: 0, enabled: true })) };
    const calls = installKitApi(clone(KITS.fresh), (url, body) =>
      url.includes("/v1/agents/grantable") ? many
        : body?.action === "test" ? { ok: true, tools: [{ name: "t" }], latency_ms: 1 }
        : body?.action === "register" ? { ...SERVERS[0], id: "mcp_2", name: "two" } : undefined);
    await render(<AgentQuickCreate projectId="p1" actor="ceo" onClose={() => {}} onCreated={() => {}} />);
    // THE CONSOLE'S OWN CONTROLS: the search field with its icon, styled checkboxes, an icon close.
    const filter = $('[data-testid="aq-skill-filter"]')!;
    expect(filter.closest(".wf-lib-search")!.querySelector("svg")).not.toBeNull();
    expect($('[data-testid="aq-skill-s.0"]')!.classList.contains("aq-check")).toBe(true);
    expect($('[data-testid="aq-close"]')!.querySelector("svg")).not.toBeNull();
    expect($('[data-testid="aq-close"]')!.textContent).toBe("");
    const add = $('[data-testid="aq-add-mcp"]')!;
    expect(add.classList.contains("wf-btn")).toBe(true);
    expect(add.querySelector("svg")).not.toBeNull();
    await click('[data-testid="aq-add-mcp"]');                // 1
    expect($('[data-testid="kit-add-mcp"]')).not.toBeNull();
    expect($('[data-testid="kit-add-agents"]')).toBeNull();
    await type('[data-testid="kit-custom-url"]', "https://mcp.two.example.com/mcp");
    await click('[data-testid="kit-add-test"]');
    await click('[data-testid="kit-add-save"]');
    expect(calls.some((c) => c.body?.action === "grant")).toBe(false);
    expect(calls.some((c) => c.body?.action === "register")).toBe(true);
    expect($('[data-testid="kit-add-mcp"]')).toBeNull();
    expect($('[data-testid="agent-quick-create"]')).not.toBeNull();
    // Ticked: the GitHub row the grantable list carries is unticked, the new one is counted.
    expect($('[data-testid="aq-mcp-mcp_1"]')).not.toBeNull();
    expect(($('[data-testid="aq-mcp-mcp_1"]') as HTMLInputElement).checked).toBe(false);
    expect($('[data-testid="aq-mcp"]')!.closest("fieldset")!.querySelector("legend")!.textContent).toContain("· 1");
  });

  it("the workflow step editor: + MCP › Add MCP server, the step's agent pre-chosen, the server scoped", async () => {
    const change = vi.fn();
    const added = vi.fn();
    installKitApi(clone(KITS.fresh), (_u, body) =>
      body?.action === "test" ? { ok: true, tools: [{ name: "t" }], latency_ms: 1 }
        : body?.action === "register" ? { ...SERVERS[0], id: "mcp_3", name: "three" } : undefined);
    await render(<ScopePicker kind="mcp" selected={[]} captain="sre" palette={PALETTE} projectId="p1" actor="ceo"
                              onChange={change} onAdded={added} />);
    await click('[data-testid="wf-scope-add-mcp"]');          // 1
    await click('[data-testid="wf-scope-add-mcp-server"]');   // 2
    expect($('[data-testid="kit-add-agent-sre"]')!.getAttribute("aria-pressed")).toBe("true");
    await type('[data-testid="kit-custom-url"]', "https://mcp.three.example.com/mcp");
    await click('[data-testid="kit-add-test"]');
    await click('[data-testid="kit-add-save"]');
    expect(change).toHaveBeenCalledWith(["three"]);
    expect(added).toHaveBeenCalled();
  });

  it("the skills picker does not offer it (skills are added from Add skill)", async () => {
    installKitApi();
    await render(<ScopePicker kind="skill" selected={[]} captain="sre" palette={PALETTE} projectId="p1" actor="ceo" onChange={() => {}} />);
    await click('[data-testid="wf-scope-add-skill"]');
    expect($('[data-testid="wf-scope-add-mcp-server"]')).toBeNull();
  });

  it("search: typing mcp (or tools) offers Add MCP server, which opens the sheet on Skills & MCP", () => {
    expect(addCommandsFor("mcp").map((c) => c.label)).toEqual(["Add MCP server"]);
    expect(addCommandsFor("connect tools").map((c) => c.label)).toEqual(["Add MCP server"]);
    expect(addCommandsFor("skill").map((c) => c.label)).toEqual(["Add skill"]);
    expect(addCommandsFor("billing")).toEqual([]);
    const shell = codeOnly("app/ConsoleShell.tsx");
    expect(shell).toContain("addCommandsFor(query)");
    expect(shell).toContain("window.dispatchEvent(new CustomEvent(KIT_ADD_EVENT, { detail: kind }))");
  });

  it("Get started: Connect your tools (MCP) is one press to the sheet", () => {
    const welcome = codeOnly("app/setup/welcome.tsx");
    expect(welcome).toContain('href={`/${addHref("mcp")}`} data-testid="su-wl-mcp"');
    expect(addHref("mcp")).toBe("?area=skills&add=mcp");
  });

  it("every place opens the one sheet: no second form", () => {
    for (const f of ["app/skills/kit-home.tsx", "app/skills/agent-abilities.tsx", "app/agents/create-wizard.tsx",
                     "app/agent-quick-create.tsx", "app/workflows/editor/inspector/scope-picker.tsx"]) {
      expect(codeOnly(f), f).toContain("<AddMcpSheet");
    }
    // Only the sheet tests a typed server.
    for (const f of ["app/skills/agent-abilities.tsx", "app/agents/create-wizard.tsx", "app/workflows/editor/inspector/scope-picker.tsx"]) {
      expect(codeOnly(f), f).not.toMatch(/mcpApi\.(test|register)/);
    }
  });
});
