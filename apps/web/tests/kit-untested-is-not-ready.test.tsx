// @vitest-environment happy-dom
/**
 * Ready means a test passed (tester K8, K21). An ability that is set up and held but has never
 * passed a test is `Not tested`, its one button is Test, it is not counted as ready, and Test all
 * runs it. Every press says what happened: a card's Test toasts its result, Turn on toasts.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render } from "./_helpers/kit-render";
import { ability, clone, installKitApi, KITS } from "./_helpers/kit-api";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const { KitHome } = await import("../app/skills/kit-home");
const { PILL, BUTTON } = await import("../app/skills/kit-copy");
const { ACTION_OF } = await import("../app/skills/ability-card");

const said: string[] = [];
window.addEventListener("boss:toast", (e) => said.push((e as CustomEvent<{ message: string }>).detail.message));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); said.length = 0; window.history.replaceState(null, "", "/"); });

describe("Not tested", () => {
  it("has its own words and its action is Test", () => {
    expect(PILL.untested).toBe("Not tested");
    expect(BUTTON.untested).toBe("Test");
    expect(ACTION_OF.untested).toBe("test");
  });

  it("a never-tested ability shows Not tested, is not counted ready, and its Test toasts the result", async () => {
    const kit = clone(KITS.fresh);
    const watch = ability(kit, "watch_health");
    watch.status = "untested"; watch.last_test = null;
    kit.ready = kit.abilities.filter((a) => a.status === "ready").length;
    const calls = installKitApi(kit);
    window.history.replaceState(null, "", "/?area=skills");
    await render(<KitHome projectId="p1" actor="ceo" />);
    const card = $('[data-testid="kit-card-watch_health"]')!;
    expect(card.querySelector('[data-testid="kit-pill"]')!.textContent).toBe("Not tested");
    expect($('[data-testid="kit-progress"]')!.textContent).toBe(`${kit.ready} of 12 ready`);
    await click('[data-testid="kit-action-watch_health"]');
    expect(calls.some((c) => c.body?.action === "test" && c.body?.ability === "watch_health")).toBe(true);
    expect(said).toContain("Test passed, ok");
  });

  it("Turn on says so", async () => {
    const kit = clone(KITS.fresh);
    ability(kit, "render_docs").status = "off";
    installKitApi(kit);
    window.history.replaceState(null, "", "/?area=skills");
    await render(<KitHome projectId="p1" actor="ceo" />);
    await click('[data-testid="kit-action-render_docs"]');
    expect(said).toContain("Render docs on");
  });
});
