// @vitest-environment happy-dom
/**
 * Setup › Model routing (docs/product/MODEL_ROUTER.md): Smart / Fixed, tiers, floors, headroom,
 * the classifier and Pin a job — each control writes one partial policy with the version it
 * read, and the board renders the server's answer. Held to the board's copy budget.
 */
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { click, fakeApi, flush, mountBoard, q, type Mounted } from "./_helpers/setup-board";
import { board, ROUTING } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

async function choose(el: Element | null, value: string) {
  if (!el) throw new Error("nothing to choose");
  await act(async () => {
    const select = el as HTMLSelectElement;
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await flush();
}

const writes = (api: ReturnType<typeof fakeApi>) =>
  api.calls.filter((c) => c.op === "routing").map((c) => c.body);

describe("Model routing", () => {
  it("shows Smart routing whole: tiers, floors, headroom, classifier, this week", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    const body = document.body;
    expect(q(body, "mr-mode-smart")?.getAttribute("aria-pressed")).toBe("true");
    for (const tier of ["fast", "balanced", "frontier"]) expect(q(body, `mr-tier-${tier}`)).not.toBeNull();
    expect((q(body, "mr-tier-select-balanced") as HTMLSelectElement).value).toBe("claude_code::sonnet");
    expect(body.querySelectorAll('[data-testid="mr-floor"]').length).toBe(3);
    const meter = body.querySelector('[role="meter"]')!;
    expect(meter.getAttribute("aria-valuenow")).toBe("8");
    expect(q(body, "mr-headroom-state-claude_code")?.textContent).toContain("Near limit");
    expect(q(body, "mr-jev")?.textContent).toBe("Jev · paused");
    expect(q(body, "mr-week")?.textContent).toContain("112");
    expect(body.querySelector('[role="img"]')?.getAttribute("aria-label")).toContain("Fast 34%");
  });

  it("stays within the copy budget with every part open", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    await click(q(document.body, "mr-floor-add"));
    (q(document.body, "mr-criteria") as HTMLDetailsElement).open = true;
    await click(q(document.body, "su-more-jobs"));
    expect(overBudget(document.body)).toEqual([]);
    const long = [...document.body.querySelectorAll("[data-tip]")]
      .map((el) => el.getAttribute("data-tip") ?? "")
      .filter((tip) => tip.split(/\s+/).filter(Boolean).length > 14);
    expect(long).toEqual([]);
  });

  it("switching mode writes the mode with the version it read", async () => {
    const api = fakeApi(board());
    mounted = await mountBoard(board());
    await click(q(document.body, "mr-mode-fixed"));
    expect(writes(api)).toEqual([{ op: "routing", project_id: "p",
                                   policy: { mode: "fixed" }, version: 3 }]);
  });

  it("a tier's model keeps its fallback: picking the fallback swaps them", async () => {
    const api = fakeApi(board());
    mounted = await mountBoard(board());
    await choose(q(document.body, "mr-tier-select-fast"), "openrouter::deepseek/deepseek-chat");
    // The new first was the fallback: the two swap, so neither is lost or listed twice.
    expect(writes(api)[0].policy).toEqual({ tiers: { fast: [
      { engine: "openrouter", model: "deepseek/deepseek-chat" },
      { engine: "claude_code", model: "haiku" }] } });
    await choose(q(document.body, "mr-tier-fallback-frontier"), "openrouter::anthropic/claude-opus-4.1");
    expect(writes(api)[1].policy).toEqual({ tiers: { frontier: [
      { engine: "claude_code", model: "opus" },
      { engine: "openrouter", model: "anthropic/claude-opus-4.1" }] } });
  });

  it("floors: remove one, add one, choose a preset", async () => {
    const api = fakeApi(board());
    mounted = await mountBoard(board());
    await click(document.body.querySelector('[aria-label="Remove Security check floor"]'));
    expect(writes(api)[0].policy).toEqual({ floor_preset: "custom", floors: [
      { match: { step: "review_code" }, tier: "frontier" },
      { match: { step: "panel_facilitator" }, tier: "frontier" }] });
    await click(q(document.body, "mr-floor-add"));
    await choose(q(document.body, "mr-floor-step"), "implement");
    await choose(q(document.body, "mr-floor-tier"), "balanced");
    await click(q(document.body, "mr-floor-save"));
    const added = writes(api)[1].policy as { floors: unknown[] };
    expect(added.floors.at(-1)).toEqual({ match: { step: "implement" }, tier: "balanced" });
    await choose(q(document.body, "mr-preset"), "strict");
    expect(writes(api)[2].policy).toEqual({ floor_preset: "strict" });
  });

  it("the headroom rule: Frontier is never offered to move", async () => {
    const api = fakeApi(board());
    mounted = await mountBoard(board());
    expect(q(document.body, "mr-rule-tier-frontier")).toBeNull();
    await click(q(document.body, "mr-rule-tier-balanced"));
    expect(writes(api)[0].policy).toEqual({ headroom: [
      { source: "claude_code", when: "near_limit", tiers: ["fast"], to: "openrouter" }] });
  });

  it("Fixed mode is the old page: the jobs, open; no tiers", async () => {
    const view = { ...board(), routing: { ...ROUTING, mode: "fixed" as const } };
    fakeApi(view);
    mounted = await mountBoard(view);
    expect(q(document.body, "mr-tier-fast")).toBeNull();
    expect(q(document.body, "mr-headroom")).toBeNull();
    expect(q(document.body, "su-job-build")).not.toBeNull();
    expect(q(document.body, "mr-preset")).not.toBeNull();
  });

  it("saving never disables the pressed control (keyboard focus stays on it)", async () => {
    const api = fakeApi(board());
    api.hold = true;
    mounted = await mountBoard(board());
    const fixed = q(document.body, "mr-mode-fixed") as HTMLButtonElement;
    fixed.focus();
    await click(fixed);
    expect(fixed.disabled).toBe(false);
    expect(document.activeElement).toBe(fixed);
    api.release();
    await flush();
  });

  it("focus=routing lands on the row with the jobs open", async () => {
    fakeApi(board());
    mounted = await mountBoard(board(), "?area=setup&focus=routing");
    expect(q(document.body, "su-row-jobs")?.getAttribute("data-focused")).toBe("true");
    expect(q(document.body, "su-job-hard")).not.toBeNull();
  });
});
