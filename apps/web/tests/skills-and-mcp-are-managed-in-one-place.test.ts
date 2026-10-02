import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { describeApiError } from "../lib/api-error";
import { MCP_TIERS, parseOverrides } from "../app/mcp-servers-ui";
import { deleteConsequence, draftFromSkill } from "../app/skills-ui";
import { codeOnly } from "./_helpers/source";

/**
 * Skills and MCP servers are managed in one place, and every screen says the same thing.
 *
 * THE INCIDENTS (boss-1, the R1 milestone map):
 *
 * - MCP servers were listed on three screens with three stories. Skills and MCP said
 *   registration was "deliberately not a form here", System Configuration said "Register one via
 *   POST /v1/mcp/servers", and the only form sat on Capabilities, hidden from the Community
 *   rail. None could edit a server or say it was unreachable, and Remove deleted a server out
 *   from under the steps bound to it.
 * - The skill editor had no Edit: the header said "Edit <key>" as soon as a key was typed and
 *   saving silently replaced the skill with that key. Tier 0 could not be saved (`|| 1`), an
 *   mcp_tool skill asked for an opaque server id no screen showed, and Delete had no question.
 * - A FastAPI validation error reached the operator as "[object Object]".
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

describe("one MCP panel, wherever servers are shown", () => {
  it("only the shared panel writes an MCP server", () => {
    // Enumerates every console file. A second writer is how a screen grows its own rules —
    // the Capabilities server actions threw on every refusal while the API had a sentence.
    const writers = appFiles().filter((f) => {
      const src = readFileSync(f, "utf8");
      return /registerMcpServer\(|setMcpServerEnabled\(|deleteMcpServer\(|updateMcpServer\(|"mcp_(register|update|delete)"/.test(src);
    }).map((f) => path.relative(APP, f)).sort();
    expect(writers).toEqual(["api/skills/route.ts", "mcp-servers-ui.tsx"]);
  });

  it("every screen that lists servers mounts it", () => {
    // ENUMERATED, not listed by hand: a screen that reads servers (fetchMcpServers, the
    // /api/skills mcp view, or the row type) and does not mount the panel is the second story
    // this replaced. The panel itself and the route that serves it are the only exemptions.
    const screens = appFiles().filter((f) => !f.includes(`${path.sep}api${path.sep}`)
      && !f.endsWith("mcp-servers-ui.tsx")
      && /fetchMcpServers|view=mcp|McpServerRow/.test(codeOnly(f)));
    expect(screens.map((f) => path.relative(APP, f)).sort())
      .toEqual(["capabilities-ui.tsx", "page.tsx", "skills-ui.tsx"]);
    for (const file of screens) {
      expect(codeOnly(file), file).toContain("<McpServersPanel");
    }
  });

  it("no screen tells an operator to call the API", () => {
    for (const f of appFiles()) {
      expect(codeOnly(f), f).not.toContain("Register one via POST /v1/mcp/servers");
    }
    expect(codeOnly("app/skills-ui.tsx")).not.toContain("deliberately not a form");
  });

  it("the panel registers, edits, checks, turns off and removes — with a question first", () => {
    const UI = codeOnly("app/mcp-servers-ui.tsx");
    for (const id of ["mcp-register-open", "mcp-check-all", "mcp-edit-", "mcp-toggle-",
                      "mcp-delete-", "mcp-delete-confirm-", "mcp-overrides", "mcp-trusted"]) {
      expect(UI).toContain(id);
    }
    // Unreachable is said, with what to do, and "never checked" is not called down.
    expect(UI).toContain("status_note");
    expect(UI).toContain('"not checked yet"');
  });

  it("tier 0 is offered, and said as a consequence", () => {
    expect(MCP_TIERS.map((t) => t.value)).toEqual([0, 1, 2, 3]);
    expect(MCP_TIERS[0].label).toContain("reads only");
  });

  it("per-tool tiers are parsed, and a bad line is named", () => {
    expect(parseOverrides("delete_issue = 3\nsearch: 0\n")).toEqual({ delete_issue: 3, search: 0 });
    expect(parseOverrides("delete_issue = high")).toContain("delete_issue = high");
  });
});

describe("the skill editor edits what it loaded", () => {
  const UI = codeOnly("app/skills-ui.tsx");

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
    expect(UI).toContain('mode: editing ? "update" : "create"');
    expect(UI).toContain("data-testid={`skill-edit-${s.skill_key}`}");
  });

  it("tier 0 is saved as 0", () => {
    expect(UI).not.toMatch(/Number\(draft\.risk_tier\)\s*\|\|/);
    expect(UI).toContain("risk_tier: Number(draft.risk_tier)");
  });

  it("an MCP tool skill picks its server and tool instead of typing an id", () => {
    const at = UI.indexOf('data-testid="skill-mcp-server"');
    expect(UI.slice(at - 120, at)).toContain("<select");
  });

  it("every behaviour is offered, including the one agents write", () => {
    for (const id of ["sandboxed_command", "mcp_tool", "builtin", "agent_program"]) {
      expect(UI).toContain(`id: "${id}"`);
    }
  });

  it("a skill can be turned off without re-saving it", () => {
    expect(UI).toContain('action: s.enabled ? "disable" : "enable"');
  });

  it("delete asks first, and says what it breaks", () => {
    const base = {
      skill_key: "file_ticket", label: "File ticket", summary: "", behaviour_kind: "builtin",
      knowledge: "", inputs: [], produces: [], config: {}, risk_tier: 1, trusted: false,
      enabled: true, requires_approval: false,
    };
    const used = deleteConsequence({ ...base,
      references: { custom_steps: [{ step_key: "file", label: "File it" }], agents: [] } });
    expect(used.blocked).toContain("File it");
    expect(used.blocked).toContain("turn the skill off");
    // Round-1 review: a workflow binding the skill with its own `kind: skill` step was not a
    // reference at all, so the delete passed and the workflow broke at its next run.
    const inFlow = deleteConsequence({ ...base, references: {
      custom_steps: [], agents: [], workflows: [{ workflow_key: "weekly", name: "Weekly digest" }] } });
    expect(inFlow.blocked).toContain("Weekly digest");
    expect(inFlow.blocked).toContain("turn the skill off");
    const held = deleteConsequence({ ...base, references: { custom_steps: [], agents: ["pm"] } });
    expect(held.blocked).toBeNull();
    expect(held.confirm).toContain("Pm will no longer hold it");
    expect(UI).toContain("skill-delete-confirm-");
  });
});

describe("a refusal reaches the operator as a sentence", () => {
  it("a validation array is flattened, not printed as [object Object]", () => {
    expect(describeApiError({ detail: [{ loc: ["body", "risk_tier"], msg: "must be <= 3" }] }, 422))
      .toBe("risk_tier: must be <= 3");
    expect(describeApiError({ detail: "an MCP server called 'x' is already registered" }, 409))
      .toContain("already registered");
    expect(describeApiError({}, 403)).toContain("permission");
  });

  it("the route uses it for every forward", () => {
    const ROUTE = codeOnly("app/api/skills/route.ts");
    expect(ROUTE).toContain("describeApiError(data, r.status)");
    expect(ROUTE).not.toContain("data?.detail ??");
  });
});
