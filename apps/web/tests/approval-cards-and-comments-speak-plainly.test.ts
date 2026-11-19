/**
 * The recipe-box walk (2026-11-18): an approval card read "Why meeting prd ready to publish",
 * "How the panel got here · 0 agents" beside a five-seat panel, and an unlabelled future date;
 * the first build page read "Comments 11 open · 0 fixed" with one finding listed three times.
 */
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));
vi.mock("../app/actions", () => ({}));

import { deadlineLabel, reasonInWords } from "../app/decision-inbox-ui";
import { panelSize } from "../app/prd-record";
import { commentsWord, fixFindingsInstructions, foldComments, sameFinding } from "../lib/review-comments";
import type { ReviewComment } from "../lib/cockpit";

describe("a decision's reason, in words", () => {
  it("never shows a reason code split on underscores", () => {
    for (const code of ["meeting_prd_ready_to_publish", "merge_decision_required", "ambient_needs_you",
                        "legacy_model_outcome_unknown", "plan_ready_for_review"]) {
      const said = reasonInWords(code);
      expect(said).not.toBe(code.replace(/_/g, " "));
      expect(said.length).toBeGreaterThan(10);
    }
    expect(reasonInWords("meeting_prd_ready_to_publish")).toMatch(/PRD/);
  });
  it("keeps a sentence as written", () => {
    expect(reasonInWords("Approve before 'Deploy config' runs.")).toBe("Approve before 'Deploy config' runs.");
  });
});

describe("a card's deadline", () => {
  const now = Date.parse("2026-11-18T20:00:00Z");
  it("says Decide by before it, and that it passed after", () => {
    expect(deadlineLabel("2026-11-19T19:57:00Z", now)).toMatch(/^Decide by /);
    expect(deadlineLabel("2026-11-17T19:57:00Z", now)).toMatch(/^Deadline passed /);
  });
  it("says nothing when there is none", () => {
    expect(deadlineLabel(undefined, now)).toBe("");
    expect(deadlineLabel(null, now)).toBe("");
  });
});

describe("the panel's size", () => {
  it("counts the seats on the agenda when no position was kept", () => {
    expect(panelSize({ positions: [], agenda: { brief: "x", participants: ["PM", "QA", "SWE", "SRE", "Facilitator"] } })).toBe(5);
    expect(panelSize({ positions: [], agenda: { brief: "", participants: [] } })).toBe(0);
  });
});

const c = (id: string, body: string, extra: Partial<ReviewComment> = {}): ReviewComment => ({
  id, round: 1, severity: "major", body, path: "tests/recipes.test.js", line: 43,
  authorRoleKey: "code_reviewer", status: "open", ...extra,
});

describe("review comments, once each", () => {
  const items = [
    c("a", "The disputed doubling test calls `scaleIngredients` directly. It never changes the serving control or checks the amounts rendered."),
    c("b", "The disputed test calls `scaleIngredients` directly. It never changes the serving control or checks amounts rendered on the page.", { round: 2 }),
    c("c", "The search input is controlled by the server supplied `search` prop.", { path: "pages/index.js", line: 67 }),
    c("d", "The disputed doubling test calls scaleIngredients directly.", { line: 12 }),
  ];
  it("folds a near-identical finding at the same place into the first", () => {
    const rows = foldComments(items);
    expect(rows.map((r) => r.id)).toEqual(["a", "c", "d"]);
    expect(rows[0].ids).toEqual(["a", "b"]);
    expect(rows[0].repeats).toBe(1);
  });
  it("counts findings, not repeats", () => {
    expect(commentsWord(items)).toBe("3 open · 0 fixed");
    expect(commentsWord([])).toBe("None");
  });
  it("an open repeat keeps the row open", () => {
    const rows = foldComments([c("a", "Same words here today", { status: "addressed" }), c("b", "Same words here today")]);
    expect(rows).toHaveLength(1);
    expect(rows[0].status).toBe("open");
  });
  it("findings that differ in what they point at never fold (review #224)", () => {
    const noPlace = foldComments([
      c("p1", "Missing test for the delete recipe endpoint", { path: undefined, line: undefined }),
      c("p2", "Missing test for the update recipe endpoint", { path: undefined, line: undefined }),
    ]);
    expect(noPlace.map((r) => r.id)).toEqual(["p1", "p2"]);
    const noLine = foldComments([
      c("l1", "No input validation on the title field", { path: "src/api.js", line: undefined }),
      c("l2", "No input validation on the servings field", { path: "src/api.js", line: undefined }),
    ]);
    expect(noLine.map((r) => r.id)).toEqual(["l1", "l2"]);
    // Even at the same file and line, one differing word on a short finding is another finding.
    expect(foldComments([c("s1", "No input validation on the title field"),
                         c("s2", "No input validation on the servings field")])).toHaveLength(2);
    expect(sameFinding("Missing test for the delete recipe endpoint", "Missing test for the update recipe endpoint")).toBe(false);
  });
  it("different findings stay apart", () => {
    expect(sameFinding("The search input never updates local state", "A named ingredient with an empty amount is saved as 0")).toBe(false);
  });
  it("asks the follow-up to fix every open finding once", () => {
    const said = fixFindingsInstructions(items);
    expect(said.match(/tests\/recipes\.test\.js:43/g)).toHaveLength(1);
    expect(said).toContain("pages/index.js:67");
    expect(fixFindingsInstructions([c("x", "done", { status: "addressed" })])).toBe("");
  });
});
