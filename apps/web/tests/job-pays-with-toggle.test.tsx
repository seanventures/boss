// @vitest-environment happy-dom
/**
 * One menu, one toggle (operator, 2026-11-03): each model is listed once in a job's menu, and a
 * "Subscription | API key" toggle beside it chooses who pays, for any engine that offers both.
 * The server offers each model on each credential as its own option ("sub:sonnet" / "key:sonnet",
 * claude_credential.split_pick); lib/job-picks folds them.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { board } from "./_helpers/setup-fixtures";
import { click, mountBoard, q, type Mounted } from "./_helpers/setup-board";
import { foldOptions, pickFor, splitPick, withPick } from "../lib/job-picks";
import type { SetupView } from "../lib/setup-types";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.unstubAllGlobals(); });

const CLAUDE = [
  { engine: "claude_code", model: "sub:sonnet", label: "Claude Code · subscription · sonnet", metered: false },
  { engine: "claude_code", model: "sub:opus", label: "Claude Code · subscription · opus", metered: false },
  { engine: "claude_code", model: "key:sonnet", label: "Claude Code · API key · sonnet", metered: true },
  { engine: "claude_code", model: "key:opus", label: "Claude Code · API key · opus", metered: true },
  { engine: "codex", model: "gpt-6-sol", label: "Codex · gpt-6-sol", metered: false },
];

describe("folding the picks", () => {
  it("lists each model once with the picks it can take", () => {
    const menu = foldOptions(CLAUDE);
    expect(menu.map((m) => [m.engine, m.bare, m.label, m.picks])).toEqual([
      ["claude_code", "sonnet", "Claude Code · sonnet", ["sub", "key"]],
      ["claude_code", "opus", "Claude Code · opus", ["sub", "key"]],
      ["codex", "gpt-6-sol", "Codex · gpt-6-sol", []],
    ]);
  });
  it("keeps the job's credential when the new model can take it, else the subscription", () => {
    const [sonnet] = foldOptions(CLAUDE);
    expect(pickFor(sonnet, "key")).toBe("key");
    expect(pickFor(sonnet, null)).toBe("sub");
    expect(pickFor(foldOptions(CLAUDE)[2], "key")).toBeNull();
  });
  it("splits and joins a pick the way the server does", () => {
    expect(splitPick("key:opus")).toEqual({ pick: "key", bare: "opus" });
    expect(splitPick("gpt-6-sol")).toEqual({ pick: null, bare: "gpt-6-sol" });
    expect(withPick("sub", "sonnet")).toBe("sub:sonnet");
    expect(withPick(null, "gpt-6-sol")).toBe("gpt-6-sol");
  });
});

function api(view: SetupView) {
  const writes: Record<string, unknown>[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    if (method !== "GET") writes.push(body);
    return new Response(JSON.stringify(view), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return writes;
}
function withReviewOnSubscription(): SetupView {
  const view = board();
  view.jobs = view.jobs.map((j) => j.job === "review"
    ? { ...j, options: CLAUDE, choice: { engine: "claude_code", model: "sub:sonnet" },
        resolved: { engine: "claude_code", model: "sub:sonnet", label: "Claude Code · subscription · sonnet", why: "chosen" } }
    : j);
  return view;
}

describe("the job row", () => {
  it("shows one Claude Code entry per model and a toggle; API key saves key:sonnet", async () => {
    const writes = api(withReviewOnSubscription());
    mounted = await mountBoard(withReviewOnSubscription(), "?area=setup&focus=routing");
    const select = q(document.body, "su-job-select-review") as HTMLSelectElement | null;
    if (!select) throw new Error("the Review row is not on the board");
    const labels = [...select.options].map((o) => o.textContent);
    expect(labels).toContain("Claude Code · sonnet");
    expect(labels.filter((l) => l?.includes("sonnet")).length).toBe(1);
    const toggle = q(document.body, "su-job-pick-review") as HTMLElement;
    expect(toggle).not.toBeNull();
    expect(q(toggle, "su-job-pick-review-sub")?.getAttribute("aria-pressed")).toBe("true");
    await click(q(toggle, "su-job-pick-review-key"));
    const saved = writes.find((w) => w.op === "job") as { choice?: { model?: string } } | undefined;
    expect(saved?.choice?.model).toBe("key:sonnet");
  });

  it("no toggle for a model with a single way to pay", async () => {
    const view = withReviewOnSubscription();
    view.jobs = view.jobs.map((j) => j.job === "review"
      ? { ...j, choice: { engine: "codex", model: "gpt-6-sol" } } : j);
    api(view);
    mounted = await mountBoard(view, "?area=setup&focus=routing");
    expect(q(document.body, "su-job-pick-review")).toBeNull();
  });
});
