import { describe, expect, it, vi } from "vitest";

import { notifyChanged, onChanged } from "../app/changed";
import { codeOnly } from "./_helpers/source";

/**
 * "if operation is done through Workflow or Factory page (code task) or through BOSS agent it
 * should be updated all places — it does not matter where, but other places should reflect the
 * changes."
 *
 * The console renders every area into one document and shows one at a time, and each area polls
 * on its own schedule. So a target set on a Factory card, a step re-queued from Workflows, and
 * an action BOSS ran in the dock all wrote the same rows and left the other two panels showing
 * what they had already fetched — until their own interval happened to fire, or for ever once
 * they stopped polling. The operator fixed the problem in one place and went back to a screen
 * still describing it.
 */
describe("the signal", () => {
  // A real EventTarget, not a DOM: this suite has none, and asserting about the source of a
  // mechanism is not the same as knowing it works.
  it("reaches a listener", () => {
    const bus = new EventTarget();
    const seen = vi.fn();
    const off = onChanged(seen, undefined, bus);
    notifyChanged("factory", { op: "set_target" }, bus);
    off();
    expect(seen).toHaveBeenCalledTimes(1);
    expect(seen.mock.calls[0][0]).toMatchObject({ source: "factory" });
  });

  it("does not echo back to whoever sent it", () => {
    // A panel already reloads on its own actions; reacting to its own echo doubles every request.
    const bus = new EventTarget();
    const seen = vi.fn();
    const off = onChanged(seen, "factory", bus);
    notifyChanged("factory", undefined, bus);
    off();
    expect(seen).not.toHaveBeenCalled();
  });

  it("but does reach everyone else", () => {
    const bus = new EventTarget();
    const seen = vi.fn();
    const off = onChanged(seen, "factory", bus);
    notifyChanged("workflows", undefined, bus);
    off();
    expect(seen).toHaveBeenCalledTimes(1);
  });

  it("unsubscribes cleanly", () => {
    const bus = new EventTarget();
    const seen = vi.fn();
    onChanged(seen, undefined, bus)();
    notifyChanged("agent", undefined, bus);
    expect(seen).not.toHaveBeenCalled();
  });

  it("a notification never throws at the caller", () => {
    // It runs immediately after a write that SUCCEEDED. Breaking there would turn a completed
    // action into a visible failure.
    expect(() => notifyChanged("anything")).not.toThrow();          // no window at all
    expect(() => notifyChanged("anything", {}, new EventTarget())).not.toThrow();
  });
});

describe("all three places both send and listen", () => {
  it("the Factory card announces a target it set, and reloads on other people's", () => {
    const UI = codeOnly("app/code-tasks-ui.tsx");
    expect(UI).toContain('notifyChanged("factory"');
    expect(UI).toContain('onChanged(() => { void reload(); }, "factory")');
  });

  it("the Workflows screen announces its actions, and reloads on other people's", () => {
    const UI = codeOnly("app/delivery-workflows-ui.tsx");
    expect(UI).toContain('notifyChanged("workflows", { action })');
    expect(UI).toContain('onChanged(() => { void load(); }, "workflows")');
  });

  it("BOSS announces what it ran from the dock", () => {
    // The operator's own case: they fixed it by clicking BOSS's button, then went back to a
    // workflow that still said it was broken.
    const UI = codeOnly("app/agent-ui.tsx");
    expect(UI).toContain('notifyChanged("agent", { tool: action.tool })');
  });
});
