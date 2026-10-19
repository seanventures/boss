// @vitest-environment happy-dom
/**
 * A REFUSED WRITE MUST SAY ITS REAL REASON, AND A STEP-UP REFUSAL MUST OPEN THE STEP-UP PROMPT
 * (boss-1 rc10.9: Setup > Repo and models > Add a model > API key > Anthropic). The API refused
 * POST /v1/setup/models with 401 step_up_required; the drawer showed no prompt and no reason —
 * only a stale pill from an older check.
 *
 * apiWrite already opens "Confirm it's you" for a 401 step_up_required and retries the exact
 * write once (lib/api-write.ts `stepUpOnce`) — this pins that contract for setupWrite
 * specifically, the door Setup's writes use. When the sheet is declined, or there is no mirror
 * to ask through, the write must still say a REAL reason: before this fix setupWrite dumped the
 * API's raw `step_up_required` code as the message, the same bare code the operator read with
 * nothing else on screen.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { installMirror } from "../lib/identity/mirror";
import { noteGesture, resetGesturesForTest } from "../lib/user-gesture";
import { SetupFailure, setupWrite } from "../lib/setup-client";
import { obsWrite } from "../app/observability/obs-write";

function press(): void {
  const button = document.createElement("button");
  document.body.appendChild(button);
  noteGesture({ type: "pointerdown", target: button });
}

afterEach(() => {
  installMirror(null);
  resetGesturesForTest();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("a setup write refused 401 step_up_required", () => {
  it("opens the step-up sheet and retries the exact write once", async () => {
    press();
    let calls = 0;
    const fetchMock = vi.fn(async () => {
      calls += 1;
      if (calls === 1) {
        return new Response(JSON.stringify({ error: "step_up_required" }), {
          status: 401, headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ catalogue: ["claude-opus"] }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const stepUp = vi.fn(async () => true);
    installMirror({ check: () => ({ allowed: true, reason: "", needsStepUp: false }) as never,
                    stepUp, refresh: async () => {} });

    const out = await setupWrite<{ catalogue: string[] }>(
      "boss-dogfood", "models", { provider: "anthropic", api_key: "sk-test" }, { label: "Connect" });

    expect(stepUp).toHaveBeenCalledTimes(1);
    expect(calls).toBe(2);
    expect(out).toEqual({ catalogue: ["claude-opus"] });
  });

  it("declined: throws the real sentence, never the raw \"step_up_required\" code", async () => {
    press();
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ error: "step_up_required" }), {
      status: 401, headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    installMirror({ check: () => ({ allowed: true, reason: "", needsStepUp: false }) as never,
                    stepUp: async () => false, refresh: async () => {} });

    let caught: unknown;
    try {
      await setupWrite("boss-dogfood", "models", { provider: "anthropic", api_key: "sk-test" },
        { label: "Connect" });
    } catch (e) { caught = e; }

    expect(caught).toBeInstanceOf(SetupFailure);
    const failure = caught as SetupFailure;
    expect(failure.message).toBe("Confirm it's you, then try again.");
    expect(failure.message).not.toContain("step_up_required");
    expect(failure.refusal?.headline).toBe("Confirm it's you");
    expect(failure.refusal?.fix).toBe("Confirm it's you, then try again.");
  });

  it("no mirror at all (console shell not mounted): still the real sentence", async () => {
    press();
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ error: "step_up_required" }), {
      status: 401, headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    installMirror(null);

    await expect(setupWrite("boss-dogfood", "models", { provider: "anthropic" }, { label: "Connect" }))
      .rejects.toMatchObject({ message: "Confirm it's you, then try again." });
  });
});

describe("a setup write refused 403 forbidden", () => {
  it("names the missing permission, never the raw code", async () => {
    press();
    const fetchMock = vi.fn(async () => new Response(
      JSON.stringify({ error: "forbidden", permission: "model_provider:manage" }),
      { status: 403, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    installMirror(null);

    let caught: unknown;
    try {
      await setupWrite("boss-dogfood", "models", { provider: "anthropic" }, { label: "Connect" });
    } catch (e) { caught = e; }

    expect(caught).toBeInstanceOf(SetupFailure);
    const failure = caught as SetupFailure;
    expect(failure.message).not.toBe("forbidden");
    expect(failure.refusal?.headline).toBe("Not allowed");
  });
});

describe("an observability write refused step_up_required with no mirror", () => {
  it("says the real reason, never the raw code", async () => {
    press();
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ error: "step_up_required" }), {
      status: 401, headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    installMirror(null);

    const result = await obsWrite("/v1/observability/rules", {}, { scope: "obs:rule", label: "Save" });
    expect(result.ok).toBe(false);
    expect(result.error).toBe("Confirm it's you, then try again.");
    expect(result.error).not.toBe("step_up_required");
  });
});
