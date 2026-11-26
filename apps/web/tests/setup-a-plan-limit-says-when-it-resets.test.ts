/**
 * A plan limit is not an error (laptop, 2026-11-26): Claude Code's row read "Check failed ·
 * Test again when it's reachable." while the CLI had said "You've hit your weekly limit · resets
 * Nov 27, 7pm (UTC)". A Limited pill has no action, so its sentence fell through to "Not usable
 * yet."; it now says when the limit resets, in the viewer's clock (the pill's `at`).
 */
import { describe, expect, it } from "vitest";

import { needsSentence } from "../app/setup/model-status";
import type { Pill } from "../lib/setup-types";

const limited = (over: Partial<Pill> = {}): Pill => ({
  state: "rate_limited", tone: "amber", label: "Limited · 19:00",
  tip: "Claude Code refused: You've hit your weekly limit · resets Nov 27, 7pm (UTC). Plan limit reached.",
  at: "2026-11-27T19:00:00+00:00", ...over,
});

describe("needsSentence", () => {
  it("says when a plan limit resets, never 'Not usable yet'", () => {
    const said = needsSentence(limited());
    expect(said).toMatch(/^Limit resets /);
    expect(said).toMatch(/7:00/);
    expect(said).not.toMatch(/Not usable|Test again|failed/i);
  });

  it("says the limit plainly when the provider named no reset", () => {
    expect(needsSentence(limited({ at: null, label: "Limited" }))).toBe("Plan limit reached.");
  });

  it("keeps the other states' sentences", () => {
    expect(needsSentence({ state: "no_credit", tone: "red", label: "Out of credit", tip: "", action: "fix_link" }))
      .toBe("Top up at the provider.");
    expect(needsSentence({ state: "unreachable", tone: "amber", label: "Can't reach", tip: "", action: "test" }))
      .toBe("Test again when it's reachable.");
    expect(needsSentence({ state: "failed", tone: "red", label: "Check failed", tip: "" }))
      .toBe("Not usable yet.");
  });
});
