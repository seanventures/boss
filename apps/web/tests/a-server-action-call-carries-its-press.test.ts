import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * A server action invoked from code carries its press to the server, like a form's does.
 *
 * Review of 45e7693: 63 server actions were called from code — Deploy (`deployTargetAction`),
 * Redeploy, the shepherd's sweep and answer, the agent chat turn (`askBossAction`, the
 * 2026-09-10 duplicate-chat path) — and none carried a press, so their API writes went out with
 * no Idempotency-Key and a double press acted twice. lib/call-action.ts now sends each call
 * through app/press-relay.ts with the press's key first; middleware.ts lifts it exactly as it
 * lifts a SubmitButton's, and the real action gets its own arguments untouched.
 */

const relayed: unknown[][] = [];
let hold: Promise<void> | null = null;
vi.mock("../app/press-relay", () => ({
  runPressedAction: vi.fn(async (press: unknown, action: (...a: unknown[]) => Promise<unknown>,
                                 ...args: unknown[]) => {
    relayed.push([press, ...args]);
    if (hold) await hold;
    return action(...args);
  }),
}));

const { callAction } = await import("../lib/call-action");
const { INTENT_FIELD, TOAST_EVENT, WriteInFlightError, intentFromActionBody } =
  await import("../lib/api-write");

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const shown: string[] = [];

beforeEach(() => {
  relayed.length = 0;
  hold = null;
  shown.length = 0;
  vi.stubGlobal("window", {
    dispatchEvent: (e: CustomEvent<{ message: string }>) => {
      if (e.type === TOAST_EVENT) shown.push(e.detail.message);
      return true;
    },
  });
});
afterEach(() => vi.unstubAllGlobals());

/** The shape of the real one: an optional parameter after an optional parameter. Appending the
 *  press to ITS arguments would have landed the key in `planOnly`. */
async function deployTargetAction(targetId: string, ref?: string, planOnly = false) {
  return { targetId, ref: ref ?? null, planOnly };
}

describe("a server action called from code", () => {
  it("is sent with a fresh press key FIRST, and its own arguments untouched", async () => {
    const out = await callAction(deployTargetAction)("tgt_1");
    expect(out).toEqual({ targetId: "tgt_1", ref: null, planOnly: false });
    const [press, ...args] = relayed[0];
    expect((press as Record<string, string>)[INTENT_FIELD]).toMatch(UUID);
    expect(args).toEqual(["tgt_1"]);
  });

  it("puts the press where middleware.ts looks for it in the encoded request", () => {
    // React encodes the relay's arguments as JSON; this is the text middleware reads.
    const key = "0b8f6a52-3c1d-4e2f-9a7b-112233445566";
    expect(intentFromActionBody(JSON.stringify([{ [INTENT_FIELD]: key }, "$h1", "tgt_1"])))
      .toBe(key);
  });

  it("pressed twice before it returns runs ONCE, and both presses get the answer", async () => {
    let release = () => {};
    hold = new Promise<void>((resolve) => { release = resolve; });
    const deploy = callAction(deployTargetAction);
    const first = deploy("tgt_1", "main");
    const second = deploy("tgt_1", "main");
    release();
    expect(await first).toEqual(await second);
    expect(relayed.length).toBe(1);
  });

  it("is a new press — a new key — once the last one finished", async () => {
    await callAction(deployTargetAction)("tgt_1");
    await callAction(deployTargetAction)("tgt_1");
    expect(relayed.length).toBe(2);
    expect(relayed[0][0]).not.toEqual(relayed[1][0]);
  });

  it("compares a FormData by its fields, ignoring the SubmitButton's own key", async () => {
    let release = () => {};
    hold = new Promise<void>((resolve) => { release = resolve; });
    const askBossAction = async (_prev: unknown, fd: FormData) => String(fd.get("message"));
    const a = new FormData(); a.set("message", "ship it"); a.set(INTENT_FIELD, "x");
    const b = new FormData(); b.set("message", "ship it"); b.set(INTENT_FIELD, "y");
    const turn = callAction(askBossAction);
    const [one, two] = [turn(null, a), turn(null, b)];
    release();
    expect([await one, await two]).toEqual(["ship it", "ship it"]);
    expect(relayed.length).toBe(1);
  });

  it("with a press scope, a different call is refused and the refusal is said", async () => {
    let release = () => {};
    hold = new Promise<void>((resolve) => { release = resolve; });
    const redeploy = callAction(async (id: string) => id, { scope: "deploy", label: "Redeploy" });
    const deploy = callAction(deployTargetAction, { scope: "deploy", label: "Deploy" });
    const running = redeploy("dep_1");
    await expect(deploy("tgt_1")).rejects.toBeInstanceOf(WriteInFlightError);
    expect(shown).toEqual([
      "Deploy was not sent — Redeploy is still in progress. Try again when it finishes."]);
    release();
    expect(await running).toBe("dep_1");
  });

  it("frees the press when the action fails, so it can be pressed again", async () => {
    const failing = callAction(async () => { throw new Error("host unreachable"); },
                               { scope: "hosts", label: "Verify" });
    await expect(failing()).rejects.toThrow("host unreachable");
    await expect(failing()).rejects.toThrow("host unreachable");
    expect(relayed.length).toBe(2);
  });
});
