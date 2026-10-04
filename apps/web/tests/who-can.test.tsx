// @vitest-environment happy-dom
/**
 * Who can (skills-mcp spec §5.4, test 5): a cell is one PUT holders; a cell for an ability that is
 * not set up is disabled with why; Gaps shows only agents with recommended-not-held cells; Apply
 * recommended is one call; a change can be undone.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, cleanup, click, render } from "./_helpers/kit-render";
import { ability, clone, installKitApi, KITS } from "./_helpers/kit-api";

const { WhoCan } = await import("../app/skills/who-can");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function mount(calls?: ReturnType<typeof installKitApi>) {
  const kit = clone(KITS.fresh);
  const used = calls ?? installKitApi(kit);
  const changed = vi.fn(); const reload = vi.fn();
  await render(<WhoCan kit={kit} crew={kit.crew ?? []} projectId="p1" actor="ceo" focusRole="pm"
                       onChanged={changed} onReload={reload} onOpenAbility={() => {}} />);
  return { kit, calls: used, changed, reload };
}

describe("Who can", () => {
  it("a cell gives the ability with one PUT, and Undo last takes it back", async () => {
    const { kit, calls } = await mount();
    await click('[data-testid="kit-cell-backend_engineer-web_search"]');
    const puts = calls.filter((c) => c.body?.action === "holders");
    expect(puts).toHaveLength(1);
    expect(puts[0].body).toMatchObject({ ability: "web_search" });
    expect(puts[0].body!.roles).toContain("backend_engineer");
    expect($('[data-testid="kit-who-changes"]')!.textContent).toBe("1 changes");
    await click('[data-testid="kit-who-undo"]');
    const undo = calls.filter((c) => c.body?.action === "holders")[1];
    expect(new Set(undo.body!.roles as string[])).toEqual(new Set(ability(kit, "web_search").holders));
  });

  it("a cell for an ability that is not set up is disabled, with why", async () => {
    await mount();
    const cell = $('[data-testid="kit-cell-qa-team_chat"]') as HTMLButtonElement;
    expect(cell.disabled).toBe(true);
    expect(cell.dataset.tip).toBe("Set up Team chat first");
  });

  it("Gaps shows only the agents with recommended abilities they do not hold", async () => {
    const { kit } = await mount();
    await click('[data-testid="kit-who-gaps"]');
    const shown = $$('[data-testid^="kit-row-"]').map((r) => r.dataset.testid!.slice("kit-row-".length));
    const gaps = (kit.crew ?? []).map((c) => c.role_key)
      .filter((r) => kit.abilities.some((a) => a.recommended.includes(r) && !a.holders.includes(r)));
    expect(new Set(shown)).toEqual(new Set(gaps));
    expect(shown).toContain("backend_engineer");
  });

  it("Apply recommended is one call", async () => {
    const { calls, reload } = await mount();
    await click('[data-testid="kit-who-apply"]');
    expect(calls.filter((c) => c.body?.action === "recommended")).toHaveLength(1);
    expect(reload).toHaveBeenCalled();
  });

  it("the agent asked for is highlighted", async () => {
    await mount();
    expect($('[data-testid="kit-row-pm"]')!.dataset.focus).toBe("true");
  });
});
