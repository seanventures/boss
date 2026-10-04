// @vitest-environment happy-dom
/**
 * The Studio and the Kit (tester K15). `+ Skill › Open pull requests` added three raw ids,
 * including github.merge_pr_real — which the Kit sets Off and the runtime refuses; an MCP chip read
 * "context7" where the Kit says Library docs; and the Check's sentence about the same item sat
 * under the chip as a paragraph. Now: offered tools only, the Kit's words on every chip and
 * Lacks row (the id in the tooltip), and one line for a problem the chip does not already say.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Palette } from "../lib/workflow-editor-types";
import { $, cleanup, click, render } from "./_helpers/kit-render";
import { clone, installKitApi, KITS } from "./_helpers/kit-api";

const { ScopePicker } = await import("../app/workflows/editor/inspector/scope-picker");
const { GrantRow } = await import("../app/workflows/editor/inspector/grant-row");
const { itemsByAbility, itemLabel } = await import("../lib/use-kit-abilities");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const PALETTE = { capabilities: [], customSteps: [], blocks: [], humanRole: "human", riskFloor: 2,
  agents: [{ role_key: "pm", name: "Juno", skills: ["github.create_pr_real"], mcp: [] }],
  bindable: { skills: [], mcp_servers: [] } } as unknown as Palette;

describe("what a step is offered", () => {
  it("never a tool the Kit has turned Off", () => {
    const groups = itemsByAbility(clone(KITS.fresh).abilities, "skill");
    const prs = groups.find((g) => g.ability.key === "open_prs")!;
    expect(prs.items).toEqual(["github.create_pr_real", "github.create_pr_sandbox"]);
    expect(prs.items).not.toContain("github.merge_pr_real");
  });

  it("names items in the Kit's words", () => {
    const abilities = clone(KITS.fresh).abilities;
    expect(itemLabel(abilities, "skill", "github.create_pr_real")).toBe("Open a PR");
    expect(itemLabel(abilities, "mcp", "context7")).toBe("Library docs");
    expect(itemLabel(abilities, "mcp", "web-intel")).toBe("web-intel"); // two abilities: its own name
    expect(itemLabel(abilities, "skill", "not.in.kit")).toBe("not.in.kit");
  });
});

describe("chips and problems", () => {
  it("a skill chip reads Open a PR and an MCP chip reads Library docs, ids in the tooltip", async () => {
    installKitApi();
    await render(<ScopePicker kind="skill" selected={["github.create_pr_real"]} captain="pm" palette={PALETTE}
                              projectId="p-k15a" onChange={() => {}} />);
    const chip = $('[data-testid="wf-scope-chip-skill-github.create_pr_real"]')!;
    expect(chip.firstChild!.textContent).toBe("Open a PR");
    expect(chip.getAttribute("title")).toContain("github.create_pr_real");
    cleanup();
    installKitApi();
    await render(<ScopePicker kind="mcp" selected={["context7"]} captain="pm" palette={PALETTE}
                              projectId="p-k15b" onChange={() => {}} problem="Names MCP server context7, which this project has not registered. Add it under Skills and MCP, or take it out." />);
    expect($('[data-testid="wf-scope-chip-mcp-context7"]')!.firstChild!.textContent).toBe("Library docs");
    // The chip already says Not set up · Set up ↗; the Check's paragraph is not repeated.
    expect($('[data-testid="wf-scope-setup-mcp-context7"]')).not.toBeNull();
    expect($('[data-testid="wf-scope-problem-mcp"]')).toBeNull();
  });

  it("any other problem is one line, the whole of it in the tooltip", async () => {
    installKitApi();
    const long = "Names a server this agent does not hold, so the step would be refused when it runs.";
    await render(<ScopePicker kind="mcp" selected={["web-intel"]} captain="pm" palette={PALETTE}
                              projectId="p-k15c" onChange={() => {}} problem={long} />);
    const line = $('[data-testid="wf-scope-problem-mcp"]')!;
    expect(line.className).toContain("wf-one-line");
    expect(line.getAttribute("title")).toBe(long);
  });

  it("a Lacks row names the item in the Kit's words", async () => {
    await render(<GrantRow projectId="p1" actor="ceo" roleKey="pm" agentName="Juno" lacks={["context7"]}
                           labelOf={(id) => (id === "context7" ? "Library docs" : id)} onGranted={async () => {}} />);
    expect($('.wf-grant-text')!.textContent).toBe("Lacks Library docs");
  });
});

describe("the Studio never says the old name", () => {
  it("no screen under workflows says Skills and MCP", async () => {
    const { readFileSync } = await import("node:fs");
    const { resolve } = await import("node:path");
    for (const f of ["../app/workflows/define-step-form.tsx", "../app/agent-detail-ui.tsx"]) {
      const src = readFileSync(resolve(__dirname, f), "utf8").split("\n")
        .filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l) && !l.includes("//")).join("\n");
      expect(src, f).not.toContain("Skills and MCP");
    }
    void click;
  });
});
