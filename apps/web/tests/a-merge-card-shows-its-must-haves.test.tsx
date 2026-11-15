/**
 * A merge card shows the must-have table and never reads "Automated checks: passed" while any
 * gate of its pull request's lineage failed (engine acceptance_gate.py, BOSSAPP-1211).
 *
 * Northgate's follow-up card said "Automated checks: passed" on a PR whose earlier build had
 * shipped past a refuted review and ~15 missing must-haves.
 */
import { describe, expect, it } from "vitest";

import type { DecisionFeedItem } from "../lib/decisions";
import { formatRecommendation } from "../app/decision-inbox-ui";
import { mustHavesOf } from "../app/checks-failing";

const REC = {
  action: "review_pull_request",
  title: "Northgate",
  pull_request_url: "https://github.com/acme/northgate/pull/7",
  validation_status: "passed",
  checks_failing: {
    gates: [{ gate: "acceptance_met", label: "Must-haves met", security: false }],
    findings: [], security: false, lineage: true,
  },
  acceptance: {
    verdict: "not_met",
    items: [
      { n: 1, text: "Go gateway with hot reload", status: "met", evidence: "cmd/northgate/main.go" },
      { n: 2, text: "api-guidelines.yaml rulebook of 20 rules", status: "missing",
        evidence: "no rulebook in the tree" },
    ],
  },
};

const card = (recommendation: Record<string, unknown>) =>
  ({ id: "dec_1", question: "q", recommendation } as unknown as DecisionFeedItem);

describe("a merge card with a failing lineage", () => {
  it("never says the automated checks passed", () => {
    const text = formatRecommendation(card(REC));
    expect(text).toContain("Automated checks: failing (Must-haves met)");
    expect(text).not.toContain("Automated checks: passed");
  });

  it("a card with nothing failing still reads as before", () => {
    const { checks_failing: _drop, ...plain } = REC;
    expect(formatRecommendation(card(plain))).toContain("Automated checks: passed");
  });

  it("lists the must-haves, missing first", () => {
    const table = mustHavesOf(REC)!;
    expect(table.verdict).toBe("not_met");
    expect(table.items.map((i) => i.status)).toEqual(["missing", "met"]);
    expect(mustHavesOf({})).toBeNull();
  });
});
