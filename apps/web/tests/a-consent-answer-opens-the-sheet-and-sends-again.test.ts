import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * CONSENT IS ANSWERED ONCE, FOR EVERY ACTION (laptop 2026-11-18). A failed build's Retry showed
 * the bare code "consent_required" under the button and did nothing: the Floor's retry action
 * swallowed the API's 409 into its message, and callAction — which answers step-up for every
 * server action — never answered consent. Now a `{ok: false, data: {consent}}` answer opens the
 * consent sheet (lib/identity/consent askConsent, as Easy Mode's launch does on a 409) and, once
 * granted, sends the same call once more; cancelled, it says so in words. No raw code is shown.
 */

vi.mock("../app/press-relay", () => ({
  runPressedAction: vi.fn(async (_press: Record<string, string>, action: (...a: unknown[]) => Promise<unknown>,
                                 ...args: unknown[]) => action(...args)),
}));

const thrown = { current: null as unknown };
vi.mock("../lib/client", () => ({
  client: {
    retryCodeTask: vi.fn(async () => { throw thrown.current; }),
    submitCodeTaskFeedback: vi.fn(async () => { throw thrown.current; }),
    confirmCodeTaskFeedback: vi.fn(async () => { throw thrown.current; }),
  }, PROJECT_ID: "p", resolveProjectId: (x: string) => x,
}));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));

const { callAction } = await import("../lib/call-action");
const { CONSENT_EVENT } = await import("../lib/identity/consent");
const { codeWords, describeActionError } = await import("../lib/action-result");
const { withConsentResult } = await import("../lib/identity/consent");

const CONSENT = {
  error: "consent_required", task: "Recipe box", task_run_id: "pending:crun_x",
  agents: [{ id: "ag_forge", name: "Forge", role_key: "engineer" }], permissions: ["code_task:retry"], projects: ["recipe-box"],
  github: null, durations: [{ kind: "task", label: "This build", days: null }], max_days: 30,
};
const REFUSED = { ok: false, error: "Needs your consent", data: { consent: CONSENT } };

let answer: string | null = "grant_1";
const asked: unknown[] = [];
beforeEach(() => {
  asked.length = 0;
  answer = "grant_1";
  const target = new EventTarget();
  target.addEventListener(CONSENT_EVENT, (e) => {
    const d = (e as CustomEvent<{ onDecided?: (g: string | null) => void }>).detail;
    asked.push(d);
    d.onDecided?.(answer);
  });
  vi.stubGlobal("window", target);
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("a consent answer", () => {
  it("opens the consent sheet once, then sends the same call once more and returns its answer", async () => {
    const retry = vi.fn(async (_f: FormData) => (retry.mock.calls.length === 1 ? REFUSED : { ok: true, runId: "crun_2", attempt: 2 }));
    const form = new FormData();
    form.set("run_id", "crun_47c3");
    const out = await callAction(retry, undefined, "code_task:retry")(form) as Record<string, unknown>;
    expect(asked).toHaveLength(1);
    expect(retry).toHaveBeenCalledTimes(2);
    expect((retry.mock.calls[1][0] as FormData).get("run_id")).toBe("crun_47c3");
    expect(out.ok).toBe(true);
    expect(out.attempt).toBe(2);
  });

  it("cancelled, the refusal stands in words, and a caller that also reads data.consent never asks again", async () => {
    answer = null;
    const retry = vi.fn(async () => REFUSED);
    const out = await withConsentResult(() => callAction(retry)() as Promise<{ ok: boolean; data?: unknown; error?: string }>);
    expect(out.ok).toBe(false);
    expect(out.error).toBe("Needs your consent");
    expect(asked).toHaveLength(1);
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("never shows a bare API code", async () => {
    const bare = vi.fn(async () => ({ ok: false, error: "consent_required" }));
    const out = await callAction(bare)() as { error: string };
    expect(out.error).not.toBe("consent_required");
    expect(out.error).toMatch(/consent/i);
    expect(codeWords("some_new_code")).toBe("The API refused it (some new code).");
    expect(codeWords("The build is already running.")).toBe("The build is already running.");
    expect(describeActionError(Object.assign(new Error("consent_required"), { status: 409 }))).not.toMatch(/^consent_required/);
  });
});

describe("the Floor's Retry", () => {
  it("answers a 409 consent_required with the consent request, never its bare code", async () => {
    const { retryBuildAction } = await import("../app/factory/floor-actions");
    thrown.current = Object.assign(new Error("consent_required"), { status: 409, body: CONSENT });
    const form = new FormData();
    form.set("run_id", "crun_47c3");
    const out = await retryBuildAction(form);
    expect(out.ok).toBe(false);
    expect((out.data as { consent?: { error?: string } }).consent?.error).toBe("consent_required");
    expect(out.error).toBe("Needs your consent");
  });
});

describe("Change it", () => {
  // Enterprise validation (Change it sheet): Start on a proposed change met 409 consent_required
  // and the sheet showed it as an error line; it now answers with the consent request.
  it("Propose and Start answer a 409 consent_required with the consent request", async () => {
    const { confirmCodeTaskFeedbackAction, submitCodeTaskFeedbackAction } = await import("../app/actions");
    thrown.current = Object.assign(new Error("consent_required"), { status: 409, body: CONSENT });
    const start = new FormData();
    start.set("thread_id", "thr_1");
    const propose = new FormData();
    propose.set("run_id", "crun_47c3");
    propose.set("message", "Make the header blue");
    for (const out of [await confirmCodeTaskFeedbackAction(null, start), await submitCodeTaskFeedbackAction(null, propose)]) {
      expect(out.ok).toBe(false);
      expect(out.error).toBe("Needs your consent");
      expect((out.data as { consent?: { error?: string } }).consent?.error).toBe("consent_required");
    }
  });

  it("any other failure stays a plain error", async () => {
    const { confirmCodeTaskFeedbackAction } = await import("../app/actions");
    thrown.current = new Error("thread is closed");
    const start = new FormData();
    start.set("thread_id", "thr_1");
    const out = await confirmCodeTaskFeedbackAction(null, start);
    expect(out.error).toBe("thread is closed");
    expect(out.data).toBeUndefined();
  });
});
