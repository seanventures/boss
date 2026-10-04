import { readdirSync, statSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { describeApiError } from "../lib/api-error";
import { deleteConsequence, draftFromSkill } from "../app/skills/library-data";
import { codeOnly } from "./_helpers/source";

/**
 * Skills and MCP servers are managed in ONE place, and every screen says the same thing.
 *
 * THE INCIDENTS (boss-1, the R1 milestone map): MCP servers were listed on three screens with
 * three stories, none could edit a server, and Remove deleted one out from under its steps. The
 * skill editor had no Edit, tier 0 could not be saved, and Delete asked nothing.
 *
 * REWRITTEN for the Kit (skills-mcp spec §5.1): the one place is Skills & MCP › Library; System
 * Configuration and Capabilities render a link to it and nothing that edits a server.
 */
const APP = path.resolve(__dirname, "../app");

function appFiles(dir = APP): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) out.push(...appFiles(full));
    else if (/\.(tsx|ts)$/.test(name)) out.push(full);
  }
  return out;
}

describe("one home for MCP servers", () => {
  it("only the MCP proxy and the Library's own screens write a server", () => {
    const writers = appFiles()
      .filter((f) => /\/v1\/mcp\/servers/.test(codeOnly(f)) || /mcpApi\.(register|update|remove|review|policy)/.test(codeOnly(f)))
      .map((f) => path.relative(APP, f)).sort();
    expect(writers).toEqual(["api/mcp/route.ts", "skills/add-mcp-sheet.tsx", "skills/import-sheet.tsx",
                             "skills/mcp-drawer.tsx", "skills/tools-section.tsx"]);
  });

  it("System Configuration and Capabilities show a link to it, and no panel", () => {
    const page = codeOnly("app/page.tsx");
    const caps = codeOnly("app/capabilities-ui.tsx");
    for (const src of [page, caps]) {
      expect(src).not.toContain("McpServersPanel");
      expect(src).toContain('href="?area=skills&tab=library&lib=mcp"');
    }
  });

  it("no screen tells an operator to call the API", () => {
    for (const f of appFiles(path.join(APP, "skills"))) expect(codeOnly(f)).not.toMatch(/POST \/v1\//);
  });
});

describe("the skill editor edits what it loaded", () => {
  const UI = codeOnly("app/skills/skill-drawer.tsx");

  it("Edit loads every field of the row, and the save says create or update", () => {
    const draft = draftFromSkill({
      skill_key: "file_ticket", label: "File ticket", summary: "s", behaviour_kind: "mcp_tool",
      knowledge: "k", inputs: [{ name: "title" }, { name: "body" }], produces: [{ name: "url" }],
      config: { mcp_server_id: "mcp_1", tool_name: "create_issue" }, risk_tier: 0,
      trusted: true, enabled: false, requires_approval: false,
    });
    expect(draft.mcp_server_id).toBe("mcp_1");
    expect(draft.tool_name).toBe("create_issue");
    expect(draft.inputs).toBe("title\nbody");
    expect(draft.risk_tier).toBe("0");
    expect(draft.enabled).toBe(false);
    expect(UI).toContain('mode: skill ? "update" : "create"');
  });

  it("tier 0 is saved as 0", () => {
    expect(UI).not.toMatch(/Number\(draft\.risk_tier\)\s*\|\|/);
    expect(UI).toContain("risk_tier: Number(draft.risk_tier)");
  });

  it("an MCP tool skill picks its server and tool instead of typing an id", () => {
    const at = UI.indexOf('data-testid="skill-mcp-server"');
    expect(UI.slice(at - 120, at)).toContain("<select");
  });

  it("every way of running is offered, including the one agents write", () => {
    for (const id of ["sandboxed_command", "mcp_tool", "builtin", "agent_program"]) expect(UI).toContain(`id: "${id}"`);
  });

  it("a skill can be turned off without re-saving it", () => {
    expect(UI).toContain('action: skill.enabled ? "disable" : "enable"');
  });

  it("delete asks first, and says what it breaks", () => {
    const base = {
      skill_key: "file_ticket", label: "File ticket", summary: "", behaviour_kind: "builtin",
      knowledge: "", inputs: [], produces: [], config: {}, risk_tier: 1, trusted: false,
      enabled: true, requires_approval: false,
    };
    const used = deleteConsequence({ ...base, references: { custom_steps: [{ step_key: "file", label: "File it" }], agents: [] } });
    expect(used.blocked).toContain("File it");
    expect(used.blocked).toContain("turn the skill off");
    const inFlow = deleteConsequence({ ...base, references: {
      custom_steps: [], agents: [], workflows: [{ workflow_key: "weekly", name: "Weekly digest" }] } });
    expect(inFlow.blocked).toContain("Weekly digest");
    const held = deleteConsequence({ ...base, references: { custom_steps: [], agents: ["pm"] } });
    expect(held.blocked).toBeNull();
    expect(held.confirm).toContain("will no longer hold it");
    expect(UI).toContain("kit-skill-delete-confirm");
  });
});

describe("a refusal reaches the operator as a sentence", () => {
  it("a validation array is flattened, not printed as [object Object]", () => {
    expect(describeApiError({ detail: [{ loc: ["body", "risk_tier"], msg: "must be <= 3" }] }, 422))
      .toBe("risk_tier: must be <= 3");
    expect(describeApiError({}, 403)).toContain("permission");
  });

  it("every Skills & MCP route uses it", () => {
    for (const route of ["app/api/skills/route.ts", "app/api/kit/route.ts", "app/api/mcp/route.ts"]) {
      expect(codeOnly(route)).toContain("describeApiError(data, r.status)");
    }
  });
});
