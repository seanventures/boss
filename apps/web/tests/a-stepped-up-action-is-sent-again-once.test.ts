import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * AFTER "CONFIRM IT'S YOU", THE PRESS LANDS (BOSSAPP-510).
 *
 * `decision:approve` and `delegation:grant` need a second factor from the last 10 minutes. The
 * person entered their code and nothing happened: the Approve form's re-send carried the SAME
 * `__boss_intent` its SubmitButton drew for the first press (it stays fixed while the press is
 * pending), middleware.ts lifted that key, and the API replayed the stored 401. Now the one
 * step-up helper (lib/call-action.ts) re-sends the exact call ONCE, as a new press, and returns
 * its answer; the API no longer remembers an identity refusal against a key either
 * (apps/api/app/idempotency.py).
 */

const relayed: { press: string; args: unknown[] }[] = [];
vi.mock("../app/press-relay", () => ({
  runPressedAction: vi.fn(async (press: Record<string, string>, action: (...a: unknown[]) => Promise<unknown>,
                                 ...args: unknown[]) => {
    relayed.push({ press: press.__boss_intent, args });
    return action(...args);
  }),
}));

const { callAction } = await import("../lib/call-action");
const { INTENT_FIELD } = await import("../lib/api-write");
const { installMirror } = await import("../lib/identity/mirror");

const STEP_UP = { ok: false, error: "Confirm it's you, then try again.", data: { identity: { error: "step_up_required", permission: "decision:approve" } } };

beforeEach(() => {
  relayed.length = 0;
  vi.stubGlobal("window", { dispatchEvent: () => true });
});
afterEach(() => { installMirror(null); vi.unstubAllGlobals(); });

function mirrorWith(stepUp: () => Promise<boolean>) {
  const spy = vi.fn(stepUp);
  installMirror({ check: () => ({ allowed: true, reason: "", needsStepUp: false }) as never, stepUp: spy, refresh: async () => {} });
  return spy;
}

describe("a step-up answer", () => {
  it("opens the sheet, then re-sends the exact approval once as a new press and returns its result", async () => {
    const stepUp = mirrorWith(async () => true);
    const seen: FormData[] = [];
    const resolveDecision = vi.fn(async (fd: FormData) => {
      seen.push(fd);
      return seen.length === 1 ? STEP_UP : { ok: true, message: "Approved." };
    });
    const form = new FormData();
    form.set("decision_id", "dec_1");
    form.set("action", "approve");
    form.set(INTENT_FIELD, "11111111-1111-4111-8111-111111111111"); // the SubmitButton's key

    const out = await callAction(resolveDecision, undefined, "decision:approve")(form);

    expect(out).toEqual({ ok: true, message: "Approved." });
    expect(stepUp).toHaveBeenCalledTimes(1);
    expect(resolveDecision).toHaveBeenCalledTimes(2);
    // The exact same request…
    for (const fd of seen) {
      expect(fd.get("decision_id")).toBe("dec_1");
      expect(fd.get("action")).toBe("approve");
    }
    // …under a new press each time, and the form names the press it was sent under, so the
    // server cannot derive the refused key again.
    expect(relayed).toHaveLength(2);
    expect(relayed[1].press).not.toBe(relayed[0].press);
    expect(seen[0].get(INTENT_FIELD)).toBe(relayed[0].press);
    expect(seen[1].get(INTENT_FIELD)).toBe(relayed[1].press);
    expect(form.get(INTENT_FIELD)).toBe("11111111-1111-4111-8111-111111111111"); // caller's form untouched
  });

  it("a thrown refusal carrying identity is replayed the same way", async () => {
    const stepUp = mirrorWith(async () => true);
    let n = 0;
    const grant = vi.fn(async (_id: string) => {
      n += 1;
      if (n === 1) throw Object.assign(new Error("Confirm it's you"), { identity: { error: "step_up_required", permission: "delegation:grant" } });
      return { ok: true };
    });
    expect(await callAction(grant, undefined, "delegation:grant")("dlg_1")).toEqual({ ok: true });
    expect(stepUp).toHaveBeenCalledTimes(1);
    expect(grant).toHaveBeenCalledTimes(2);
    expect(grant.mock.calls).toEqual([["dlg_1"], ["dlg_1"]]);
  });

  it("is re-sent only once: a second step_up_required comes back to the caller", async () => {
    const stepUp = mirrorWith(async () => true);
    const retry = vi.fn(async () => STEP_UP);
    expect(await callAction(retry, undefined, "decision:approve")()).toEqual(STEP_UP);
    expect(stepUp).toHaveBeenCalledTimes(1);
    expect(retry).toHaveBeenCalledTimes(2);
  });

  it("cancelled: nothing is re-sent", async () => {
    mirrorWith(async () => false);
    const approve = vi.fn(async () => STEP_UP);
    expect(await callAction(approve, undefined, "decision:approve")()).toEqual(STEP_UP);
    expect(approve).toHaveBeenCalledTimes(1);
  });
});
