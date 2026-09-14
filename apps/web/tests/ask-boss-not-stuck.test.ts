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

  it("never starts a second turn, but no longer refuses the message either", () => {
    // TWO TURNS AT ONCE IS STILL THE BUG. What changed is what happens instead of starting
    // one: pressing Send while busy QUEUES, rather than doing nothing. Holding the button
    // meant a second thought had to be kept in the operator's head for the two minutes a turn
    // can take, or typed into the box and lost.
    const send = SRC.slice(SRC.indexOf("const send = useCallback"));
    const body = send.slice(0, send.indexOf("}, [input, files, busy, runTurn]);"));
    // Queued, and the turn is NOT started.
    expect(body).toContain("if (busy && !files.length) {");
    expect(body).toContain("setQueued((q) => [...q, msg]);");
    // An attachment cannot be queued — the file handles are consumed by the turn that sends
    // them — so that case still waits.
    expect(body).toContain("if (busy) return;");
  });

  it("the queue drains one at a time, in order", () => {
    // Sending them together would interleave the answers, and the second question usually
    // depends on the first one's answer.
    expect(SRC).toContain("const [next, ...rest] = queued;");
    expect(SRC).toContain("if (busy || released || !queued.length) return;");
  });

  it("Stop says what it actually does", () => {
    // A turn is a server action; there is no cancel endpoint, so the model keeps going and its
    // answer still lands. Calling this Cancel would be a lie the next message exposes.
    expect(SRC).toContain('data-testid="agent-stop"');
    expect(SRC).toContain("Stopped waiting. BOSS may still finish this turn");
    expect(SRC).not.toMatch(/>\s*Cancel\s*</);
  });

  it("bounds the wait rather than the work", () => {
    // Nothing is cancelled server-side, and the transcript says so instead of implying it
    // stopped. The wording moved when the console stopped telling the operator to come back
    // and look: it now watches for the answer itself, so the sentence says the work continues
    // AND that they can keep typing.
    expect(SRC).toContain("Nothing was cancelled");
    expect(SRC).toContain("will appear here on its own");
  });

  it("and then actually waits for it", () => {
    // "Reopen this thread in a moment" is work the console can do itself: the turn writes its
    // reply into the thread when it lands, and polling for that is one interval.
    expect(SRC).toContain("setLanding(true);");
    expect(SRC).toContain("if (!landing || !threadId) return;");
    expect(SRC).toContain('view: "messages"');
  });

  it("but gives up eventually rather than polling for ever", () => {
    expect(SRC).toContain("20 * 60_000");
  });
});
