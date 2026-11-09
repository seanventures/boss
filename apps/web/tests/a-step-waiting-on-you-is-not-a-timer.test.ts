/**
 * A STEP WAITING ON A PERSON IS NOT A TIMER (operator, 2026-11-06).
 *
 * A Deploy step waiting for its stack deployment's approval showed "6h 50m…" on the rail and its
 * card, read as seven hours of work. The clock (lib/step-timing.ts) now says
 * "waiting on you · since 3:23 PM" while a person holds a step, and counts only working time
 * otherwise. The last test fails if the workflow view formats a step's time anywhere else.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

import { personWait, stepClock, workingSeconds } from "../lib/step-timing";

vi.mock("../app/press-relay", () => ({
  runPressedAction: vi.fn(async (_p: unknown, a: (...x: unknown[]) => Promise<unknown>, ...args: unknown[]) => a(...args)),
}));
const { laneStateOf, runNeedsYou } = await import("../app/delivery-workflows-ui");

const NOW = Date.parse("2026-11-06T22:13:00Z");
// The rail reads the real clock; pin it so "since 3:23 PM" (same day) is the same on any day.
vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
type Lane = Parameters<typeof laneStateOf>[0];
const base = {
  step_id: "deploy", kind: "capability", capability: "deploy", evidence: [], error: null,
  approval: {}, due_at: null, timeout_applied: null, attempt: 1, decision_id: null,
  output: {}, started_at: "2026-11-06T15:20:00Z", completed_at: null,
};
const step = (over: Record<string, unknown>) => ({ ...base, ...over }) as unknown as Lane;

const stackWaiting = step({
  status: "waiting",
  output: { await: { kind: "stack_deployment", id: "sd_1" } },
  stack_deploy: { deployment: { id: "sd_1", status: "awaiting_approval", created_at: "2026-11-06T15:23:00Z" } },
});

describe("a step waiting on a person", () => {
  it("says who it waits on and since when — never a running timer", () => {
    const clock = stepClock(stackWaiting, NOW);
    expect(clock).toMatchObject({ kind: "waiting", text: "waiting on you · since 3:23 PM" });
    expect(clock!.text).not.toMatch(/\dh|…/);
    expect(clock!.text.split(/\s+/).filter((w) => /[a-z]/i.test(w)).length).toBeLessThanOrEqual(6);
  });

  it("shows on the rail as needing you, with the time it started waiting", () => {
    expect(laneStateOf(stackWaiting)).toMatchObject({ phase: "needs_you", meta: "needs you · 3:23 PM" });
  });

  it("puts its run under Needs you", () => {
    expect(runNeedsYou({ status: "waiting", steps: [stackWaiting] })).toBe(true);
  });

  it("covers every person-wait: an open gate, a merge, an architecture review", () => {
    const gate = step({ status: "waiting", decision_id: "dec_1", decision_status: "pending" });
    const merge = step({ status: "waiting", output: { await: { kind: "merge", id: "c1" } } });
    const arch = step({ status: "waiting", output: { await: { kind: "architecture", id: "a1" } } });
    for (const s of [gate, merge, arch]) {
      expect(stepClock(s, NOW)).toMatchObject({ kind: "waiting", text: "waiting on you · since 3:20 PM" });
    }
  });

  it("an earlier day says the day, not a clock time", () => {
    const old = step({ status: "waiting", decision_id: "d", decision_status: "pending",
                       started_at: "2026-11-04T09:00:00Z" });
    expect(stepClock(old, NOW)!.text).toBe("waiting on you · since Nov 4");
  });

  it("machine waits (a build, CI) are still working time", () => {
    const build = step({ status: "waiting", output: { await: { kind: "code_task", id: "c" } } });
    expect(personWait(build)).toBeNull();
    expect(stepClock(build, NOW)).toMatchObject({ kind: "working", text: "6h 53m…" });
  });

  it("an answered gate (not yet picked up) is not waiting on you", () => {
    const answered = step({ status: "waiting", decision_id: "d", decision_status: "approved",
                            decision_approved_at: "2026-11-06T22:12:00Z" });
    expect(personWait(answered)).toBeNull();
    expect(stepClock(answered, NOW)).toMatchObject({ kind: "working", text: "1m…" });
  });
});

describe("working time leaves the waiting out", () => {
  it("a stack deploy that waited 6h for approval took only its working minutes", () => {
    const done = step({
      status: "succeeded", completed_at: "2026-11-06T22:05:00Z",
      stack_deploy: { deployment: { id: "sd_1", status: "healthy", created_at: "2026-11-06T15:23:00Z",
                                    apply_started_at: "2026-11-06T22:01:00Z" } },
    });
    // 3 min planning + 4 min applying; the 6h 38m in between was a person's.
    expect(workingSeconds(done, NOW)).toBe(7 * 60);
    expect(stepClock(done, NOW)).toMatchObject({ kind: "working", text: "7m" });
  });

  it("a gated step counts from its answer", () => {
    const done = step({ status: "succeeded", decision_id: "d", decision_status: "approved",
                        decision_approved_at: "2026-11-06T21:00:00Z", completed_at: "2026-11-06T21:00:40Z" });
    expect(stepClock(done, NOW)!.text).toBe("40s");
  });
});

describe("one clock", () => {
  it("the workflow view formats a step's time only through lib/step-timing", () => {
    const src = readFileSync(join(__dirname, "..", "app", "delivery-workflows-ui.tsx"), "utf8");
    expect(src).not.toMatch(/\bhumanDuration\b/);
    expect(src).not.toMatch(/completed_at\)\s*:\s*Date\.now\(\)/);
    expect(src).toMatch(/from "\.\.\/lib\/step-timing"/);
  });
});
