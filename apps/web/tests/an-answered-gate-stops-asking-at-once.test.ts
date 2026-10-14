// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * AN ANSWERED GATE STOPS ASKING AT ONCE (BOSSAPP-511, run wfr_3c8ff51c).
 *
 * A gate step that was just approved kept its amber "Needs you" on the run lane until the page
 * was reloaded. The answer is now recorded where every answer passes — callAction (a server
 * action's result naming its decision) and apiWrite (a browser POST to /v1/decisions/{id}/…) —
 * the runs every view draws are read through that overlay, and the answer says "changed" so
 * each panel refetches the server's truth.
 */
vi.mock("../app/press-relay", () => ({
  runPressedAction: vi.fn(async (_press: unknown, action: (...a: unknown[]) => Promise<unknown>, ...args: unknown[]) => action(...args)),
}));

const { laneStateOf, bucketOf, needsYou } = await import("../app/delivery-workflows-ui");
const gates = await import("../lib/decided-gates");
const { callAction } = await import("../lib/call-action");
const { apiWrite } = await import("../lib/api-write");
const { onChanged } = await import("../app/changed");

type Run = Parameters<typeof bucketOf>[0];
const run = (decisionStatus: string | null): Run => ({
  status: "running",
  steps: [{ step_id: "release", kind: "gate", status: "waiting", decision_id: "dec_9", decision_status: decisionStatus, output: {} }],
} as unknown as Run);

beforeEach(() => gates.resetAnsweredGatesForTest());
afterEach(() => { vi.unstubAllGlobals(); gates.resetAnsweredGatesForTest(); });

describe("an answered gate", () => {
  it("reads as answered the moment a server action accepts the approval, and asks the panels to refetch", async () => {
    const changed: string[] = [];
    const stop = onChanged((note) => changed.push(note.source));
    const before = [run("pending")];
    expect(laneStateOf(before[0].steps![0] as never).phase).toBe("needs_you");

    const resolve = vi.fn(async (_fd: FormData) => ({ ok: true, message: "Approved.", data: { decision: { id: "dec_9", status: "approved" } } }));
    await callAction(resolve, undefined, "decision:approve")(new FormData());

    const shown = gates.withAnsweredGates(before);
    const step = shown[0].steps![0] as never;
    expect(needsYou(step)).toBe(false);
    expect(laneStateOf(step).phase).not.toBe("needs_you");
    expect(bucketOf(shown[0])).not.toBe("needs_you");
    expect(changed).toContain("decision"); // the refetch
    stop();
  });

  it("an approval sent as a browser write to /v1/decisions/{id}/approve counts the same", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    await apiWrite("/v1/decisions/dec_9/approve", { method: "POST", body: "{}" });
    expect(needsYou(gates.withAnsweredGates([run("pending")])[0].steps![0] as never)).toBe(false);
  });

  it("a refused answer changes nothing; the server's own later state wins; unrelated runs keep their identity", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 403 })));
    await apiWrite("/v1/decisions/dec_9/approve", { method: "POST", body: "{}" });
    const open = [run("pending")];
    expect(gates.withAnsweredGates(open)).toBe(open);

    gates.noteDecided("dec_9", "approve");
    expect(gates.withAnsweredGates([run("rejected")])[0].steps![0].decision_status).toBe("rejected");
    const other = { status: "running", steps: [{ step_id: "x", decision_id: "dec_other", decision_status: "pending" }] };
    expect(gates.withAnsweredGates([other])[0]).toBe(other);
  });

  it("lapses: after the TTL the overlay no longer speaks for the server", () => {
    vi.useFakeTimers();
    try {
      gates.noteDecided("dec_9", "approved");
      vi.advanceTimersByTime(gates.ANSWER_TTL_MS + 1);
      expect(needsYou(gates.withAnsweredGates([run("pending")])[0].steps![0] as never)).toBe(true);
    } finally { vi.useRealTimers(); }
  });
});
