import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The composer must come back when the stall bound fires.
 *
 * A turn ran past the 120s bound and the field was still disabled at 131s. The bound worked —
 * the race resolved — but the stall branch called refreshThreads(), which fires a server
 * action, and server actions serialise: it queued behind the turn that had not answered.
 * React keeps a transition pending until everything started inside it settles, so `busy`
 * stayed true until the original turn finished. The whole purpose of the bound is to give
 * control back, and it gave none.
 */
const SRC = codeOnly("app/agent-ui.tsx");

describe("ask boss does not lock the operator out", () => {
  it("does not start a server action inside the transition on the stall path", () => {
    // Bound the slice FORWARD from the stall branch: `if (r.ok && r.data)` also appears
    // earlier in the file, and searching from zero sliced backwards to an empty string —
    // which trivially "contains" nothing and would have passed the negative assertion below
    // while proving nothing at all.
    const from = SRC.indexOf("if (r === stalled)");
    const branch = SRC.slice(from, SRC.indexOf("if (r.ok && r.data)", from));
    expect(branch.length).toBeGreaterThan(100);
    expect(branch).toContain("setTimeout(() => refreshThreads(), 0)");
    // A bare call would re-enter the transition and hold `busy` open again.
    expect(branch).not.toMatch(/\n\s*refreshThreads\(\);/);
  });

  it("leaves the field usable while BOSS is working", () => {
    // A turn can run for two minutes. Locking the field means no drafting the follow-up, no
    // fixing a typo, no selecting what you wrote to copy it.
    const field = SRC.slice(SRC.indexOf('<textarea className="agent-input"'), SRC.indexOf("agent-composer-bar"));
    expect(field).not.toContain("disabled={busy}");
  });

  it("still holds SEND while a turn is in flight", () => {
    // Two turns at once is a different bug.
    const bar = SRC.slice(SRC.indexOf("agent-composer-bar"));
    expect(bar).toContain("disabled={busy || !input.trim()}");
  });

  it("bounds the wait rather than the work", () => {
    // Nothing is cancelled server-side; the transcript says so instead of implying it stopped.
    expect(SRC).toContain("nothing was cancelled");
  });
});
