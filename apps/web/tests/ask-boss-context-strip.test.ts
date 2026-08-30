import { describe, expect, it } from "vitest";

import { APP_FACTS_END, stripAppFacts } from "../app/agent-ui";

/**
 * The operator must see the question they asked, not the context BOSS was given.
 *
 * The block is prepended at send time as `${facts}\n\n${typed}`, so the boundary is the LAST
 * blank line. Splitting on the FIRST one meant that as soon as any fact contained a blank line
 * the rest stayed on screen — a one-line question displayed under twenty lines of run_id,
 * branch, "DEPLOY THIS ONE" and instructions addressed to the model.
 */
const FACTS = [
  "[app context] You are talking about ONE app in BOSS.",
  "run_id: crun_dad37a91c4b0470480fd",
  "app: Living Colors",
  "repo: paramg/boss-sandbox",
  "status: completed (done)",
  "",                                   // <- a blank line INSIDE the block
  "DEPLOY THIS ONE. Do not judge deployability from the run named above.",
  "If the operator points you somewhere else, DO THAT.",
].join("\n");

describe("stripAppFacts", () => {
  it("removes the whole block even when it contains a blank line", () => {
    // This is what the operator saw: a one-line question under twenty lines of run_id, branch
    // and "DEPLOY THIS ONE". The terminator makes it exact regardless of blank lines.
    const q = "Which of the 3 acceptance criteria are not actually covered by a test?";
    expect(stripAppFacts(`${FACTS}\n${APP_FACTS_END}\n\n${q}`)).toBe(q);
  });

  it("removes a block with no internal blank line", () => {
    const q = "What changed today?";
    const facts = "[app context] one app\nrun_id: crun_1\napp: Shorty";
    expect(stripAppFacts(`${facts}\n\n${q}`)).toBe(q);
  });

  it("leaves an ordinary message completely alone", () => {
    const msg = "Deploy Shorty to staging.\n\nAnd tell me what it cost.";
    expect(stripAppFacts(msg)).toBe(msg);
  });

  it("keeps every paragraph of a multi-paragraph question", () => {
    // A blank line cannot be the boundary: the facts contain them and so do people's
    // questions. With the explicit terminator the split is exact.
    const q = "First, why did it fail?\n\nSecond, what would you change?";
    const out = stripAppFacts(`${FACTS}\n${APP_FACTS_END}\n\n${q}`);
    expect(out).toBe(q);
  });

  it("never discards what the operator typed, even on a legacy message", () => {
    // The legacy path uses the FIRST blank line. A last-blank-line rule strips more of the
    // block but deletes every paragraph except the final one of a message someone wrote in
    // several — and losing what a person typed is a far worse failure than showing some stale
    // context above it.
    const q = "para one\n\npara two";
    const out = stripAppFacts(`${FACTS}\n\n${q}`);
    expect(out).toContain("para one");
    expect(out).toContain("para two");
  });

  it("never returns an empty string", () => {
    // A message that is nothing but context would otherwise render as an empty bubble.
    expect(stripAppFacts(FACTS)).toBeTruthy();
  });
});
