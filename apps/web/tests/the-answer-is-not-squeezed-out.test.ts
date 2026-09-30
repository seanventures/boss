import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The operator opened the Deployment Manager dock and saw: their own question, "What BOSS read
 * for this answer — 19 lookups", and five buttons. No reply.
 *
 * The reply was there. `agent_messages` held 3,525 characters and the API returned all of it —
 * "**Your browser hit :3000 because BOSS health-checked and advertised a port the container
 * never published**". It was rendered, and it was invisible.
 *
 * `.agent-chat-shell` is a FIXED height, and the evidence trail and the proposals were siblings
 * of the scrolling transcript rather than inside it. So their height came out of the
 * conversation: five proposals — each a button plus a paragraph of why — left the log about one
 * bubble tall, and the answer sat below the fold of a scroll box nobody could tell was there.
 *
 * The first fix was wrong and two existing tests caught it: giving the log a min-height floor
 * makes the column overflow, and making the dock body scroll to compensate sends the composer
 * away with the conversation — a defect that had already been found and fixed once. Those
 * invariants are load-bearing. Moving the blocks INSIDE the scroll region keeps both.
 */
const UI = codeOnly("app/agent-ui.tsx");
const CSS = codeOnly("app/globals.css");

function rule(selector: string): string {
  const at = CSS.indexOf(`${selector} {`);
  return at === -1 ? "" : CSS.slice(at, CSS.indexOf("}", at));
}

describe("the evidence and the offers travel with the answer", () => {
  it("both are inside the scrolling transcript", () => {
    const log = UI.indexOf('className="agent-chat-log"');
    const close = UI.indexOf("</div>", UI.indexOf('data-testid="agent-proposals"'));
    expect(UI.indexOf('data-testid="agent-trail"')).toBeGreaterThan(log);
    expect(UI.indexOf('data-testid="agent-proposals"')).toBeGreaterThan(log);
    // …and before the transcript container closes.
    expect(close).toBeGreaterThan(UI.indexOf('data-testid="agent-proposals"'));
  });

  it("the composer is still outside it", () => {
    // The one element that must be reachable without scrolling back.
    expect(UI.indexOf('className="agent-composer"'))
      .toBeGreaterThan(UI.indexOf('data-testid="agent-proposals"'));
  });
});

describe("the invariants the first attempt broke", () => {
  it("the transcript can still shrink, which is what makes it scroll", () => {
    expect(rule(".agent-chat-log")).toContain("min-height: 0");
    expect(rule(".agent-chat-log")).toContain("overflow-y: auto");
  });

  it("and the dock body still does not scroll", () => {
    // With a scrolling dock body the composer travelled with the conversation.
    expect(rule(".bx-dock-body")).toContain("overflow: hidden");
  });

  it("no floor was left on the transcript", () => {
    // A floor is what forced the parent to overflow in the first attempt.
    expect(rule(".agent-chat-log")).not.toMatch(/min-height:\s*min\(/);
  });
});
