/**
 * A knowledge-graph rule's reviewer is a PERSON, named (brec_77cc82cb106640b09d44): 11 approved
 * rules in northpeak-bank stored review.by = "ceo" and every panel read "the owner". The server
 * now stores the person's id with their name; old records read as what they are.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { reviewerWords } from "../lib/actor-words";

describe("reviewerWords", () => {
  it("names the person the server recorded", () => {
    expect(reviewerWords("usr_example1abc", "Example Person")).toBe("Example Person");
  });
  it("a legacy record reads 'the operator', never the bare key", () => {
    expect(reviewerWords("ceo", null)).toBe("the operator");
    expect(reviewerWords("ceo")).not.toBe("ceo");
  });
  it("an id with no name never prints the id", () => {
    expect(reviewerWords("usr_example1abc", null)).toBe("a person");
  });
  it("the viewer is 'you'", () => {
    expect(reviewerWords("usr_example1abc", "Example Person", "usr_example1abc")).toBe("you");
  });
});

describe("every reviewer line in the Graph and Rules review passes the stored name", () => {
  const panel = readFileSync("app/knowledge/node-panel.tsx", "utf8");
  const review = readFileSync("app/knowledge/rules-review.tsx", "utf8");
  const words = readFileSync("lib/kg-provenance.ts", "utf8");
  it("node-panel: Approved by and the timeline, through the one formatter", () => {
    expect(panel).toContain("approvalWords(d.provenance)");
    expect(panel).toContain("eventWords(e)");
    expect(words).toContain("personName(p.approved_by, p.approved_by_name)");
    expect(panel).not.toMatch(/actorWords\((p|d\.provenance)\.approved_by\)/);
  });
  it("rules-review: Reviewed by", () => {
    expect(review).toContain("personName(r.review.by, r.review.by_name)");
    expect(review).not.toContain("actorWords(r.review.by)");
  });
});
