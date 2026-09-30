import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The progress line was dead on every new conversation.
 *
 *     "It displays same message for more than 3 mins - how can user wait and see same message:
 *      Still working — this turn is running long."
 *
 * `agent_progress` is written per tool call and keyed by THREAD. The console polls it every
 * 1.5s — but only once it has a thread id, and on a new chat it learned that from the response
 * to the turn, which arrives when the turn is already over. So for the whole turn there was
 * nothing to poll and the generic fallback sentence sat there unchanged.
 *
 * Confirmed against production before changing anything: `agent_progress` held 1,646 rows with
 * good labels ("reading the role's remit — program_manager", "querying the database — SELECT
 * …"). The trail was always being written. The page just could not address it yet.
 */
const UI = codeOnly("app/agent-ui.tsx");

describe("the progress line has something to poll from the first second", () => {
  it("a new chat gets its thread id BEFORE the turn, not from the turn", () => {
    const turn = UI.slice(UI.indexOf("const stalled = Symbol"));
    // Ordered: the ensure call must come before the request that does the work.
    const ensure = UI.indexOf('"/api/agent-thread"');
    expect(ensure).toBeGreaterThan(-1);
    expect(ensure).toBeLessThan(UI.indexOf("askBossAction(null, fd)"));
    expect(turn.length).toBeGreaterThan(0);
  });

  it("the id starts the poll and scopes the turn to the same thread", () => {
    // Both, or the trail belongs to a thread the answer does not land in.
    expect(UI).toContain("setThreadId(live)");
    expect(UI).toContain('fd.set("thread_id", live)');
  });

  it("it only does this when there is no thread yet", () => {
    expect(UI).toContain("if (!live) {");
  });

  it("failing to get an id never costs the operator their message", () => {
    // An indicator is not worth a dropped turn: without an id the turn runs exactly as it did
    // before and the line simply stays generic.
    const block = UI.slice(UI.indexOf('"/api/agent-thread"'));
    expect(block.slice(0, 1200)).toContain("catch");
  });

  it("a real step still beats the generic sentence", () => {
    // The fallback is only for when nothing has been reported yet.
    expect(UI).toContain("steps.length");
    expect(UI).toContain("steps[steps.length - 1]");
  });
});
