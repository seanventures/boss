// @vitest-environment happy-dom
/**
 * Skills as objects an operator authors, not ids only BOSS can implement.
 *
 * REWRITTEN for the Kit (skills-mcp spec §5.5): authoring is the Library's skill drawer. It asks
 * Name, What it does and How it runs first, then only what that way of running needs; every hint
 * that was a paragraph is a tooltip on its field. A built-in skill shows its facts, not a form.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { areaVisibilityCss } from "../lib/area-visibility";
import { AREA_IDS } from "../lib/area-ids";
import { codeOnly } from "./_helpers/source";
import { $, $$, cleanup, click, render, type } from "./_helpers/kit-render";
import { clone, installKitApi, KITS, SERVERS } from "./_helpers/kit-api";

const { SkillDrawer } = await import("../app/skills/skill-drawer");
const ROUTE = codeOnly("app/api/skills/route.ts");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const crew = clone(KITS.fresh).crew ?? [];
const drawer = (props: Partial<React.ComponentProps<typeof SkillDrawer>> = {}) => (
  <SkillDrawer projectId="p1" actor="ceo" skill={null} builtIn={null} creating crew={crew} holders={[]}
               servers={SERVERS as never} builtIns={[{ id: "model.generate_text" }]} ability={undefined}
               onClose={() => {}} onSaved={() => {}} {...props} />
);

describe("the area exists in all the places an area has to exist", () => {
  it("the union, the nav, the page, the CSS rule and the edition", () => {
    expect(AREA_IDS).toContain("skills");
    expect(codeOnly("app/ConsoleShell.tsx")).toContain('{ id: "skills", label: "Skills & MCP"');
    expect(codeOnly("app/page.tsx")).toContain('data-area="skills"');
    expect(areaVisibilityCss(["skills"])).toContain('.bx-content[data-active="skills"]');
    expect(codeOnly("lib/edition.ts")).toContain('"skills"');
  });
});

describe("a new skill", () => {
  it("offers five ways to run (a Guide only explains), each explained in its tooltip", async () => {
    installKitApi();
    await render(drawer());
    const how = $$('[data-testid^="skill-how-"]');
    expect(how.map((b) => b.textContent)).toEqual(["Command", "MCP tool", "Built-in", "Program", "Guide"]);
    expect(how.every((b) => (b.dataset.tip ?? "").length > 0)).toBe(true);
  });

  it("a Guide shows only its knowledge, and is always Reads", async () => {
    installKitApi();
    await render(drawer());
    await click('[data-testid="skill-how-guide"]');
    for (const id of ["skill-inputs", "skill-produces", "skill-image", "skill-mcp-server", "skill-executor", "skill-risk"]) {
      expect($(`[data-testid="${id}"]`), id).toBeNull();
    }
    expect($('[data-testid="skill-knowledge"]')).not.toBeNull();
    expect($('[data-testid="skill-risk-guide"]')?.textContent).toBe("Reads");
  });

  it("a command's dangers are said on its fields: a pinned image, quoted inputs", async () => {
    installKitApi();
    await render(drawer());
    const image = $('[data-testid="skill-image"]')!.closest("label")!;
    expect(image.querySelector("[data-tip]")!.getAttribute("data-tip")).toContain("Pinned by digest");
    const command = $('[data-testid="skill-command"]')!.closest("label")!;
    expect(command.querySelector("[data-tip]")!.getAttribute("data-tip")).toContain("shell-quoted");
  });

  it("an MCP tool is picked from the servers, not typed", async () => {
    installKitApi();
    await render(drawer());
    await click('[data-testid="skill-how-mcp_tool"]');
    expect($('[data-testid="skill-mcp-server"]')!.tagName).toBe("SELECT");
  });

  it("knowledge gets the biggest box; risk is said in words", async () => {
    installKitApi();
    await render(drawer());
    expect(Number(($('[data-testid="skill-knowledge"]') as HTMLTextAreaElement).getAttribute("rows"))).toBe(8);
    expect($$('[data-testid^="skill-risk-"]').map((b) => b.textContent))
      .toEqual(["Reads", "Writes", "Changes the world", "Public"]);
  });

  it("Save creates it, tier 0 saved as 0", async () => {
    const calls = installKitApi();
    const saved = vi.fn();
    await render(drawer({ onSaved: saved }));
    await type('[data-testid="skill-label"]', "Trim a clip");
    await click('[data-testid="skill-risk-0"]');
    await click('[data-testid="skill-save"]');
    const sent = calls.find((c) => c.url === "/api/skills" && c.method === "POST")!.body!;
    expect(sent).toMatchObject({ skill_key: "trim_a_clip", label: "Trim a clip", risk_tier: 0, mode: "create" });
    expect(saved).toHaveBeenCalledWith("trim_a_clip");
  });
});

describe("a skill BOSS ships", () => {
  it("shows its facts, gives it to agents, and offers no form", async () => {
    const calls = installKitApi();
    await render(drawer({ creating: false, builtIn: { id: "model.generate_text", description: "Write", risk_tier: 0 } }));
    expect($('[data-testid="skill-save"]')).toBeNull();
    await click('[data-testid="kit-skill-agent-pm"] [role="switch"]');
    expect(calls.find((c) => c.body?.action === "assign")!.body).toMatchObject({ role_key: "pm", skill_id: "model.generate_text" });
  });
});

describe("the route cannot be used to reach any endpoint", () => {
  it("every write is a named action, not a path taken from the client", () => {
    expect(ROUTE).toContain('const action = String(body.action ?? "")');
    expect(ROUTE).toContain('const remove = action === "delete"');
    expect(ROUTE).toContain('action === "assign" || action === "unassign"');
    expect(ROUTE).toContain("/v1/skill-definitions");
  });
});
