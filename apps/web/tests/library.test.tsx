// @vitest-environment happy-dom
/**
 * Library (skills-mcp spec §5.5): one list of skills — Built in and Yours — with the skills BOSS
 * uses itself behind `Show internal`; Describe opens the dock with the same prompt as before; the
 * MCP list; Import and Export all under ⋯.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, cleanup, click, render } from "./_helpers/kit-render";
import { clone, installKitApi, KITS } from "./_helpers/kit-api";

const dock = vi.fn();
vi.mock("../lib/dock-subject", () => ({ openDockAbout: (x: unknown) => dock(x) }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const { Library } = await import("../app/skills/library");
const { readKitParams } = await import("../app/skills/kit-url");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); dock.mockReset(); });

async function mount(search = "/?area=skills&tab=library") {
  window.history.replaceState(null, "", search);
  const kit = clone(KITS.fresh);
  const calls = installKitApi(kit);
  await render(<Library projectId="p1" actor="ceo" kit={kit} crew={kit.crew ?? []} initial={readKitParams()} onKitChanged={() => {}} />);
  return calls;
}

describe("the Library", () => {
  it("lists built-in skills with their ability, and hides machinery until asked", async () => {
    await mount();
    const pr = $('[data-testid="kit-skill-github.create_pr_real"]')!;
    expect(pr.textContent).toContain("Built in");
    expect(pr.textContent).toContain("Open pull requests");
    expect($('[data-testid="kit-skill-model.route_via_entitlement_broker"]')).toBeNull();
    await click('[data-testid="kit-lib-internal"]');
    expect($('[data-testid="kit-skill-model.route_via_entitlement_broker"]')).not.toBeNull();
  });

  it("Describe opens the dock to the Program Manager with the same prompt", async () => {
    await mount();
    await click('[data-testid="kit-lib-describe"]');
    expect(dock).toHaveBeenCalledTimes(1);
    const arg = dock.mock.calls[0][0] as { role: string; question: string };
    expect(arg.role).toBe("Program Manager");
    expect(arg.question).toContain("I want to create a skill for this project.");
    expect(arg.question).toContain("which skills p1 already has");
  });

  it("+ New skill opens the skill drawer with the three things a skill needs first", async () => {
    await mount();
    await click('[data-testid="kit-lib-new-skill"]');
    expect($('[data-testid="kit-skill-drawer"]')).not.toBeNull();
    for (const id of ["skill-label", "skill-summary", "skill-behaviour"]) expect($(`[data-testid="${id}"]`)).not.toBeNull();
    expect($$('[data-testid^="skill-how-"]').map((b) => b.textContent)).toEqual(["Command", "MCP tool", "Built-in", "Program"]);
  });

  it("the MCP list, and Import / Export all under ⋯", async () => {
    await mount("/?area=skills&tab=library&lib=mcp");
    expect($('[data-testid="kit-mcp-web-intel"]')!.textContent).toContain("Built in");
    expect($('[data-testid="kit-mcp-context7"]')!.textContent).toContain("Official");
    await click('[data-testid="kit-lib-more"]');
    expect($('[data-testid="kit-lib-menu"]')!.textContent).toBe("ImportExport all");
    await click('[data-testid="kit-lib-import"]');
    expect($('[data-testid="kit-import"]')).not.toBeNull();
  });
});
