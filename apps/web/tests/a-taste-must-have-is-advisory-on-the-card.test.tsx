/**
 * A judgement (taste) must-have answered partial is advisory: the card shows it as
 * "advisory (taste)" and its "Automated checks" line says "passed, N advisory" — never a plain
 * "passed" that hides it (BOSSAPP-1233). Whether an item is a judgement call is the acceptance
 * contract's tag (set when the contract was made, under a deterministic floor), never the
 * reviewer's.
 */
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { DecisionFeedItem } from "../lib/decisions";
import { formatRecommendation } from "../app/decision-inbox-ui";
import { automatedChecksLine, MustHaves, mustHavesOf } from "../app/checks-failing";

const MET_WITH_ADVISORY = {
  action: "review_pull_request",
  title: "Northgate",
  pull_request_url: "https://github.com/acme/northgate/pull/7",
  validation_status: "passed",
  acceptance: {
    verdict: "met",
    advisory: [2],
    items: [
      { n: 1, text: "Written in Go", status: "met", evidence: "go.mod" },
      { n: 2, text: "Plain, concise naming.", status: "partial", evidence: "a few long names",
        judgement: true },
      { n: 3, text: "Readable code", status: "met", evidence: "x.go", judgement: true },
    ],
  },
};

const card = (recommendation: Record<string, unknown>) =>
  ({ id: "dec_1", question: "q", recommendation } as unknown as DecisionFeedItem);

describe("a taste must-have on the merge card", () => {
  it("says passed with how many are advisory, never a plain passed", () => {
    expect(automatedChecksLine(MET_WITH_ADVISORY)).toBe("Automated checks: passed, 1 advisory.");
    expect(formatRecommendation(card(MET_WITH_ADVISORY))).toContain(
      "Automated checks: passed, 1 advisory.");
  });

  it("a card with no advisory items reads as before", () => {
    const items = MET_WITH_ADVISORY.acceptance.items.map((i) => ({ ...i, status: "met" }));
    const met = { ...MET_WITH_ADVISORY, acceptance: { verdict: "met", items } };
    expect(automatedChecksLine(met)).toBe("Automated checks: passed.");
  });

  it("an untagged partial is never advisory: the line says not met", () => {
    const items = MET_WITH_ADVISORY.acceptance.items.map((i) => ({ ...i, judgement: false }));
    const notMet = { ...MET_WITH_ADVISORY, acceptance: { verdict: "not_met", items } };
    expect(automatedChecksLine(notMet)).toContain("1 of 3 must-haves not met");
    expect(automatedChecksLine(notMet)).not.toContain("advisory");
  });

  it("the table marks taste items and shows the advisory partial after the open ones", () => {
    const data = mustHavesOf(MET_WITH_ADVISORY)!;
    expect(data.items.find((i) => i.n === 2)!.judgement).toBe(true);
    const html = renderToStaticMarkup(<MustHaves data={data} />);
    expect(html).toContain("1 advisory (taste)");
    expect(html).toContain("PARTIAL</b> — advisory (taste)");
    expect(html).toContain("Readable code<span");                 // the tag on a met taste item
    expect(html).toContain("0 partial");                          // advisory is not counted open
  });
});
