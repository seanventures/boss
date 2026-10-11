/**
 * "Sign in with GitHub" (device flow) as a sequence of the server's answers — no DOM: start →
 * pending → slow_down → connected, and the three endings (expired, denied, failed).
 */
import { describe, expect, it } from "vitest";

import { afterPoll, installLabel, signinOf, STOP_WORDS, type Phase } from "../lib/identity/github-signin";

const code: Phase = { at: "code", code: "BOSS-0001", uri: "https://github.com/login/device", handle: "h",
                      interval: 5, expiresAt: 0 };

describe("the device-flow sequence", () => {
  it("waits while pending, waits longer on slow_down, then is done", () => {
    let p = afterPoll(code, { state: "pending", interval: 5 });
    expect(p).toMatchObject({ at: "code", interval: 5 });
    p = afterPoll(p, { state: "pending", interval: 10, slow_down: true });
    expect(p).toMatchObject({ at: "code", interval: 10, code: "BOSS-0001" });
    // GitHub's interval never shrinks back mid-attempt
    p = afterPoll(p, { state: "pending", interval: 5 });
    expect(p).toMatchObject({ at: "code", interval: 10 });
    expect(afterPoll(p, { state: "connected", login: "octo-dev" })).toEqual({ at: "done", login: "octo-dev" });
  });

  it("stops on expired, denied and failed, each with short words and a tip", () => {
    for (const why of ["expired", "denied", "failed"] as const) {
      expect(afterPoll(code, { state: why, error: "x" })).toEqual({ at: "stopped", why, error: "x" });
      expect(STOP_WORDS[why].label.split(" ").length).toBeLessThanOrEqual(6);
    }
  });

  it("ignores answers once finished", () => {
    const done: Phase = { at: "done", login: "a" };
    expect(afterPoll(done, { state: "pending", interval: 5 })).toBe(done);
  });
});

describe("the status answer", () => {
  it("reads only a whole answer; an older API reads as not offered", () => {
    expect(signinOf({ mode: "none", app: {} })).toBeNull();
    expect(signinOf(null)).toBeNull();
    expect(signinOf({ configured: false, slug: null, install_url: null, verification_uri: "", me: null }))
      .toMatchObject({ configured: false, verification_uri: "https://github.com/login/device" });
  });

  it("says Install BOSS on <org> exactly", () => {
    expect(installLabel({ account: "globex", type: "Organization", install_url: "u" })).toBe("Install BOSS on globex");
  });
});
