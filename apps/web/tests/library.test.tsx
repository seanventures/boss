// @vitest-environment happy-dom
/**
 * All skills (docs/design/SKILLS_AND_MCP_HOME.md): one list of skills — Built in and Yours — with
 * the skills BOSS uses itself behind `Show internal`; Add skill › Describe opens the dock with the
 * same prompt as before; Write one opens the drawer; Import and Export all under ⋯.
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
const { KitHome } = await import("../app/skills/kit-home");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); dock.mockReset(); });

async function mount(search = "/?area=skills&tab=library", request: "write" | "import" | "built_in" | null = null) {
  window.history.replaceState(null, "", search);
  const kit = clone(KITS.fresh);
  const calls = installKitApi(kit);
  await render(<Library projectId="p1" actor="ceo" kit={kit} crew={kit.crew ?? []} initial={readKitParams()} onKitChanged={() => {}}
                        request={request} />);
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

  it("Add skill › Describe opens the dock to the Program Manager with the same prompt", async () => {
    installKitApi(clone(KITS.fresh));
    window.history.replaceState(null, "", "/?area=skills");
    await render(<KitHome projectId="p1" actor="ceo" />);
    await click('[data-testid="kit-add-skill-btn"]');
    await click('[data-testid="kit-add-skill-describe"]');
    expect(dock).toHaveBeenCalledTimes(1);
    const arg = dock.mock.calls[0][0] as { role: string; question: string };
    expect(arg.role).toBe("Program Manager");
    expect(arg.question).toContain("I want to create a skill for this project.");
    expect(arg.question).toContain("which skills p1 already has");
  });

  it("Write one opens the skill drawer with the three things a skill needs first", async () => {
    await mount(undefined, "write");
    expect($('[data-testid="kit-skill-drawer"]')).not.toBeNull();
    for (const id of ["skill-label", "skill-summary", "skill-behaviour"]) expect($(`[data-testid="${id}"]`)).not.toBeNull();
    expect($$('[data-testid^="skill-how-"]').map((b) => b.textContent)).toEqual(["Command", "MCP tool", "Built-in", "Program", "Guide"]);
  });

  it("Built in shows only built-in skills", async () => {
    await mount(undefined, "built_in");
    const rows = $$('li[data-testid^="kit-skill-"]');
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.every((r) => r.textContent?.includes("Built in"))).toBe(true);
  });

  it("Import / Export all under ⋯", async () => {
    await mount();
    await click('[data-testid="kit-lib-more"]');
    expect($$('[data-testid="kit-lib-menu"] [role="menuitem"]').map((b) => b.textContent!.trim())).toEqual(["Import", "Export all"]);
    await click('[data-testid="kit-lib-import"]');
    expect($('[data-testid="kit-import"]')).not.toBeNull();
  });
});
