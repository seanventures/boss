/**
 * A turn that outruns the 240s bound must not be declared finished — or dead —
 * on the previous reply.
 *
 * Chat writes the user line and the assistant reply in ONE transaction. While the
 * turn is still running the messages endpoint therefore still ends on the last
 * turn's assistant message. The landing poller treated that leftover as "the
 * answer arrived", replaced the transcript, and dropped the operator's new
 * question. They sent it again. Two turns ran at once.
 *
 * The same hole lives on the Command Center: it painted the old paragraph as
 * if it had just answered the new question.
 */
import { describe, expect, it } from "vitest";

import { landingAnswerArrived } from "../lib/chat-landing";
import { codeOnly } from "./_helpers/source";

const PREVIOUS = [
  { role: "user", content: "why is the deploy stuck?" },
  { role: "assistant", content: "The health checks are pointed at the wrong ports." },
];

const SENT = "Please go ahead and fix everything";

describe("landingAnswerArrived", () => {
  it("an older assistant message is not this turn's answer", () => {
    expect(landingAnswerArrived(PREVIOUS, SENT)).toBe(false);
  });

  it("an empty server transcript is not an answer", () => {
    expect(landingAnswerArrived([], SENT)).toBe(false);
  });

  it("the new user line without a reply is still in flight", () => {
    expect(landingAnswerArrived(
      [...PREVIOUS, { role: "user", content: SENT }],
      SENT,
    )).toBe(false);
  });

  it("the reply that committed with that user line is the answer", () => {
    expect(landingAnswerArrived(
      [...PREVIOUS, { role: "user", content: SENT },
        { role: "assistant", content: "Fixed the ports and retried." }],
      SENT,
    )).toBe(true);
  });

  it("a later question's older reply does not satisfy a newer send", () => {
    expect(landingAnswerArrived(
      [...PREVIOUS, { role: "user", content: SENT },
        { role: "assistant", content: "Fixed." },
        { role: "user", content: "and the other app too" }],
      "and the other app too",
    )).toBe(false);
  });

  it("an earlier answer to the same text does not satisfy a repeated send", () => {
    expect(landingAnswerArrived(
      [{ role: "user", content: SENT }, { role: "assistant", content: "Earlier answer." },
        { role: "user", content: SENT }],
      SENT,
    )).toBe(false);
  });
});

describe("both landing pollers wait for THIS turn", () => {
  it("the dock does not treat the last assistant bubble as arrival", () => {
    const ui = codeOnly("app/agent-ui.tsx");
    expect(ui).toContain("landingAnswerArrived");
    expect(ui).not.toMatch(/if \(last && last\.role === "assistant"\)/);
  });

  it("the Command Center does not paint the previous paragraph as the new reply", () => {
    const hero = codeOnly("app/ask-boss-hero.tsx");
    expect(hero).toContain("landingAnswerArrived");
  });
});
