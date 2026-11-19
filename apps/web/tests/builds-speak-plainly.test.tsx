// @vitest-environment happy-dom
/**
 * GUARD: the merge card, the build page and the work list never render a raw gate key, a
 * "· retry N" or BOSS's raw repair prompt (lib/plain-words.ts, engine services/plain_words.py).
 *
 * The e2e recipe-box walk (2026-11-18): "Checks failing — decide the merge: Recipe box: Fix the
 * open review item · retry 2", "Review passed (code_review_passed)", "Must-haves met
 * (acceptance_met)", a headline quoting "Autonomous review-repair: the quality gate blocked on
 * code_review_passed, acceptance_met…", "not convened: profile 'lean' does not include this
 * seat", "Checks: passed  Quality: fail" beside a failed review, and "Tests 9/9 passed" while a
 * suite never loaded.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { DecisionFeedItem } from "../lib/decisions";
import { formatRecommendation } from "../app/decision-inbox-ui";
import { ChecksFailing, checksFailingOf } from "../app/checks-failing";
import { checksSentence } from "../app/factory/result-review";
import { testsWord } from "../lib/build-detail";
import { gateFailedWords, leaks, plainSentence, plainSkip, plainTitle } from "../lib/plain-words";

const REPAIR = "Autonomous review-repair: the quality gate blocked on code_review_passed, acceptance_met. "
  + "Address EVERY review finding below so ALL gates pass. If 'tests_strong_enough' failed, STRENGTHEN the tests.";

const REC = {
  action: "review_pull_request",
  title: "Recipe box: Fix the open review item · retry 2",
  pull_request_url: "boss-local://recipe-box/changes/2",
  checks_failing: {
    gates: [
      { gate: "code_review_passed", label: "Review passed", security: false },
      { gate: "acceptance_met", label: "Must-haves met", security: false, detail: "Must-haves: 5 met, 4 partial, 2 missing" },
    ],
    findings: [{ gate: "code_review_passed", stage: "review", security: false, severity: "major", text: "The test calls scaleIngredients directly." }],
    security: false,
  },
};

/** The visible text of rendered markup, with every closed <details> body left out. */
function visible(html: string): string {
  const shown = html.replace(/<details(?![^>]*\bopen)[^>]*>[\s\S]*?<\/details>/g, (d) => d.replace(/<\/summary>[\s\S]*<\/details>/, "</summary></details>"));
  return shown.replace(/<[^>]+>/g, " ").replace(/&[a-z]+;|&#\d+;/g, " ").replace(/\s+/g, " ");
}

describe("the merge card speaks plainly", () => {
  it("names each failing gate as what went wrong; the keys stay in a fold", () => {
    const record = checksFailingOf(REC)!;
    const html = renderToStaticMarkup(<ChecksFailing record={record} />);
    const text = visible(html);
    expect(text).toContain("Review asked for changes");
    expect(text).toContain("Must-haves not all met");
    expect(leaks(text)).toEqual([]);
    // Still there for whoever greps a log, behind "Check names".
    expect(html).toContain("decision-checks-keys");
    expect(html).toContain("code_review_passed");
  });

  it("its sentence names the work, never the attempt", () => {
    const text = formatRecommendation({ id: "d", question: "q", recommendation: REC } as unknown as DecisionFeedItem);
    expect(text).toContain("“Recipe box: Fix the open review item”");
    expect(leaks(text)).toEqual([]);
    const title = plainSentence("Checks failing — decide the merge: Recipe box: Fix the open review item · retry 2 (change #2)");
    expect(title).toBe("Checks failing — decide the merge: Recipe box: Fix the open review item (change #2)");
  });
});

describe("the build page speaks plainly", () => {
  it("a raw repair prompt becomes one sentence", () => {
    expect(plainSentence(REPAIR)).toBe("BOSS is fixing what its checks found: Review asked for changes, Must-haves not all met");
    expect(leaks(plainSentence(REPAIR))).toEqual([]);
    expect(leaks(REPAIR)).toContain("repair prompt");
  });

  it("a skipped seat reads as the project's quality level", () => {
    expect(plainSkip("not convened: profile 'lean' does not include this seat")).toBe("Not part of the Lean quality level");
  });

  it("one checks sentence: the tests and the failed gates in the same words as the Now band", () => {
    expect(checksSentence("passed", [gateFailedWords("code_review_passed")]))
      .toBe("Checks: tests passed; 1 gate failed (Review asked for changes).");
    expect(checksSentence("passed", [])).toBe("Checks: tests passed, every gate passed.");
  });

  it("a suite that did not load never reads passed", () => {
    expect(testsWord({ passed: 9, failed: 0, total: 9, ok: false, loadError: "A test suite did not load (missing jsdom)" }))
      .toBe("A suite did not load");
    expect(testsWord({ passed: 9, failed: 0, total: 9, ok: true })).toBe("9/9 passed");
  });
});

describe("the work list speaks plainly", () => {
  it("titles lose the attempt counter, plain words stay", () => {
    expect(plainTitle("Recipe box · retry 2")).toBe("Recipe box");
    expect(leaks("Retry the build")).toEqual([]);
    expect(leaks("It is consistent with the code")).toEqual([]);
    expect(leaks("Recipe box · retry 2")).toEqual(["retry N"]);
  });
});
