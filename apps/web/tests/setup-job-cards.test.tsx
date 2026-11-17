// @vitest-environment happy-dom
/** Who does what as cards (BOSSAPP-1341) and the OpenRouter model field (BOSSAPP-1340). */
import { afterEach, describe, expect, it, vi } from "vitest";

import { board } from "./_helpers/setup-fixtures";
import { click, mountBoard, q, type Mounted } from "./_helpers/setup-board";
import type { SetupView } from "../lib/setup-types";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.unstubAllGlobals(); });

function stub(view: SetupView) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(view),
    { status: 200, headers: { "Content-Type": "application/json" } })));
}

describe("job cards", () => {
  it("each card shows engine, model, how it is paid, scope and health", async () => {
    const view = board();
    stub(view);
    mounted = await mountBoard(view, "?area=setup", true, "setup");
    const card = q(document.body, "su-who-card-review")!;
    expect(card.textContent).toContain("Review");
    expect(card.textContent).toContain("OpenRouter");
    expect(card.textContent).toContain("openai/gpt-5");
    expect(q(card, "su-who-paid-review")!.textContent).toBe("API key");
    expect(card.textContent).toContain("All projects");
    expect(q(card, "su-who-health-review")!.textContent).toBe("Not checked");
    expect(q(document.body, "su-who-paid-build")!.textContent).toBe("Subscription");
  });

  it("never says Working without a server pill; shows the server's pill when sent", async () => {
    const view = board();
    view.jobs = view.jobs.map((j) => j.job === "build" && j.resolved
      ? { ...j, resolved: { ...j.resolved, pill: { state: "failed", tone: "red", label: "Refusing runs", tip: "x" } } } : j);
    stub(view);
    mounted = await mountBoard(view, "?area=setup", true, "setup");
    expect(q(document.body, "su-who-health-chat")!.textContent).not.toContain("Working");
    expect(q(document.body, "su-who-pill-build")!.textContent).toContain("Refusing runs");
  });

  it("Change edits in place under the summary and leaves Advanced closed", async () => {
    const view = board();
    stub(view);
    mounted = await mountBoard(view, "?area=setup", true, "setup");
    expect(q(document.body, "su-who-edit-build")).toBeNull();
    await click(q(document.body, "su-who-change-build"));
    const edit = q(document.body, "su-who-edit-build")!;
    expect(edit.querySelector("select")).not.toBeNull();
    expect((q(document.body, "su-advanced-all") as HTMLDetailsElement).open).toBe(false);
    await click(q(document.body, "su-who-change-build"));
    expect(q(document.body, "su-who-edit-build")).toBeNull();
  });

  it("a card shows the pay toggle for an engine with two ways", async () => {
    const view = board();
    view.jobs = view.jobs.map((j) => j.job === "build" ? { ...j, options: [
      { engine: "codex", model: "sub:gpt-6-sol", label: "Codex · subscription · gpt-6-sol", metered: false },
      { engine: "codex", model: "key:gpt-6-sol", label: "Codex · API key · gpt-6-sol", metered: true }],
      choice: { engine: "codex", model: "sub:gpt-6-sol" },
      resolved: { engine: "codex", model: "sub:gpt-6-sol", label: "Codex · gpt-6-sol", why: "chosen" } } : j);
    stub(view);
    mounted = await mountBoard(view, "?area=setup", true, "setup");
    expect(q(document.body, "su-who-paid-build")!.textContent).toBe("Subscription");
    await click(q(document.body, "su-who-change-build"));
    expect(q(document.body, "su-job-pick-build")).not.toBeNull();
  });
});

describe("OpenRouter in Add a model", () => {
  it("pre-fills no model and Connect needs only the key", async () => {
    const view = board();
    stub(view);
    mounted = await mountBoard(view, "?area=setup&add=openrouter", true, "setup");
    const field = q(document.body, "su-model-name") as HTMLInputElement | null;
    expect(field).not.toBeNull();
    expect(field!.value).toBe("");
    expect(field!.placeholder).toMatch(/provider picks/i);
  });
});
