// @vitest-environment happy-dom
/**
 * The Studio and the Kit (skills-mcp spec §3.3, test 8): a step's `+ MCP` lists abilities first;
 * an ability that is not set up says `Not set up` and links to it; a built-in step's Uses row
 * shows its ability, red when it is not set up.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Palette } from "../lib/workflow-editor-types";
import { $, cleanup, click, render } from "./_helpers/kit-render";
import { installKitApi } from "./_helpers/kit-api";

const { ScopePicker } = await import("../app/workflows/editor/inspector/scope-picker");
const { StepAbility } = await import("../app/workflows/editor/inspector/step-ability");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const PALETTE = { capabilities: [], customSteps: [], blocks: [], humanRole: "human", riskFloor: 2,
  agents: [{ role_key: "sre", name: "Beacon", skills: [], mcp: ["boss-devops-reference"] }],
  bindable: { skills: [], mcp_servers: [] } } as unknown as Palette;

describe("the Studio knows abilities", () => {
  it("+ MCP lists abilities first; Library docs says Not set up and links to it", async () => {
    installKitApi();
    await render(<ScopePicker kind="mcp" selected={[]} captain="sre" palette={PALETTE} projectId="p-scope" onChange={() => {}} />);
    await click('[data-testid="wf-scope-add-mcp"]');
    expect($('[data-testid="wf-scope-abilities-mcp"]')).not.toBeNull();
    expect($('[data-testid="wf-scope-ability-mcp-library_docs"]')!.textContent).toContain("Library docs");
    const setUp = $('[data-testid="wf-scope-ability-setup-library_docs"]')!;
    expect(setUp.textContent).toBe("Not set up · Set up ↗");
    expect(setUp.getAttribute("href")).toBe("?area=skills&ability=library_docs");
    expect($('[data-testid="wf-scope-ability-setup-web_research"]')).toBeNull();
  });

  it("picking an ability narrows the step to what is behind it", async () => {
    installKitApi();
    const change = vi.fn();
    await render(<ScopePicker kind="mcp" selected={[]} captain="sre" palette={PALETTE} projectId="p-scope2" onChange={change} />);
    await click('[data-testid="wf-scope-add-mcp"]');
    const box = $('[data-testid="wf-scope-ability-mcp-watch_health"] input')!;
    await click(box);
    expect(change).toHaveBeenCalledWith(["boss-devops-reference"]);
  });

  it("a build step's Uses row shows Open pull requests, not set up, with Set up ↗", async () => {
    installKitApi();
    await render(<StepAbility step={{ id: "build", kind: "capability", binding: { capability: "build" } } as never} projectId="p-scope3" />);
    expect($('[data-testid="wf-step-ability"]')!.textContent).toBe("Open pull requests · Not set up");
    expect($('[data-testid="wf-step-ability-setup"]')!.getAttribute("href")).toBe("?area=skills&ability=open_prs");
  });
});
