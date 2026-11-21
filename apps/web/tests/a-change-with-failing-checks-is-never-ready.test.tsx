// @vitest-environment happy-dom
/**
 * A CHANGE WITH FAILING CHECKS IS NEVER "READY TO MERGE" (contributed from an enterprise install,
 * 2026-11-20).
 *
 * The operator's rule (engine checks_failing.py): a build whose checks still fail after its
 * repair rounds opens its pull request anyway, and a person decides the merge with a reason. Its
 * merge card said CHECKS FAILING; the Work panel beside it said "Ready to merge. Change #3 is
 * built, tested and reviewed." with Review & tests green. Now the engine carries the card's own
 * record into the Work row (merge_pr.checks_failing) and every Work surface reads it:
 *
 *   - the row's / drawer's next step says "checks failing (…)" and Merge stays the button, going
 *     to the merge card that takes the reason;
 *   - the panel's header says "Checks failing.", red, never "built, tested and reviewed";
 *   - Review & tests is red with "Your move";
 *   - Helm's first question is "Why are the checks failing?".
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, mount, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const LOCAL = "http://boss-local/local_recipes/recipes/pull/3";
const DECISION = "?project_id=recipes&area=decisions&decision=dec_merge3";
/** The merge card's record (engine checks_failing.collect), as the decision carries it. */
const CARD_RECORD = {
  gates: [{ gate: "tests_pass", label: "Tests pass", security: false },
          { gate: "security_clean", label: "Security clean", security: true }],
  findings: [], security: true, draft: true, label: "checks failing",
};
/** What the engine's work.py sends for the same record (checks_failing.for_work). */
const WORK_RECORD = { gates: ["security_clean", "tests_pass"], security: true, words: "Security clean, Tests pass" };
const RAW = {
  id: "wfr_recipes", label: "Recipe tags", title: "Recipe tags", kind: "feature", workflow: "Minor Feature",
  state: "needs_you", age: "1h", action_label: "Merge", created_at: "2026-11-20T09:00:00Z", updated_at: "2026-11-20T09:40:00Z",
  attempt_count: 1, attempts: [], agents: [],
  stages: [
    { key: "plan", label: "P", state: "done" }, { key: "design", label: "D", state: "done" },
    { key: "build", label: "B", state: "done" },
    // The engine marks Review failed for such a change (work._checks_failing_review).
    { key: "review", label: "R", state: "failed" },
    { key: "pr", label: "PR", state: "waiting" }, { key: "ship", label: "S", state: "todo" },
  ],
  links: {
    decision: DECISION, pr: { number: 3, external_url: LOCAL },
    merge_pr: { number: 3, external_url: LOCAL, follow_up_of: null, failed: null, build_id: "crun_1",
                // A follow-up list on the same card never turns it back into "Ready".
                followups: { count: 2, queued: true, words: "2 follow-ups queued" },
                checks_failing: WORK_RECORD },
    merged_pr: null,
  },
};
async function load(over: Record<string, unknown> = {}) {
  const px = await import("../lib/project-experience");
  return px.parseWorkItem({ ...RAW, ...over });
}
const ctx = { projectId: "recipes", actor: "ceo", overview: { captain: null, project: { name: "Recipes" } },
              refreshWork: () => {}, open: () => {}, ask: () => {} };

describe("the Work row's next step", () => {
  it("says checks failing and keeps Merge, at the merge card that takes the reason", async () => {
    const { workNextStep } = await import("../lib/work-next-step");
    const next = workNextStep(await load())!;
    expect(next.words).toBe("Change #3 is open · checks failing (Security clean, Tests pass)");
    expect(next.words).not.toMatch(/ready/i);
    expect(next.kind).toBe("merge");
    expect(next.label).toBe("Merge change #3");
    expect(next.href).toBe(DECISION);
  });

  it("a merge that failed and a fix still running keep their own words first", async () => {
    const { workNextStep } = await import("../lib/work-next-step");
    const failed = await load({ links: { ...RAW.links, merge_pr: { ...RAW.links.merge_pr, failed: "conflict" } } });
    expect(workNextStep(failed)!.words).toBe("Merge failed: conflict");
    const fixing = await load({ links: { ...RAW.links, merge_pr: { ...RAW.links.merge_pr,
      fixing: { attempt: "crun_2", label: "Follow-up 1", state: "running", since: "2026-11-20T09:41:00Z", findings: 2 } } } });
    expect(workNextStep(fixing)!.words).toContain("BOSS is fixing 2 findings");
  });
});

describe("the Work panel", () => {
  it("header, Review & tests and Helm all say checks failing", async () => {
    const { stateSentence, flowNodes, helmSuggestions } = await import("../lib/work-panel");
    const { workNextStep } = await import("../lib/work-next-step");
    const w = await load();
    const next = workNextStep(w);
    expect(stateSentence(w, next)).toEqual({ lead: "Checks failing.", tone: "red",
      rest: "Change #3 opened with failing checks: Security clean, Tests pass. Merging needs your reason." });
    const review = flowNodes({ w, story: null, next }).find((n) => n.key === "review")!;
    expect(review.state).toBe("failed");
    expect(review.note).toMatchObject({ tone: "you", title: "Your move", text: "Checks failing: Security clean, Tests pass" });
    expect(helmSuggestions(w)[0]).toBe("Why are the checks failing?");
  });

  it("an older engine without the record but with a failed station never says built, tested and reviewed", async () => {
    const { stateSentence } = await import("../lib/work-panel");
    const { workNextStep } = await import("../lib/work-next-step");
    const w = await load({ links: { ...RAW.links, merge_pr: { ...RAW.links.merge_pr, checks_failing: null, followups: null } } });
    const said = stateSentence(w, workNextStep(w));
    expect(said.lead).not.toMatch(/ready/i);
    expect(said.rest).not.toContain("built, tested and reviewed");
  });

  it("draws it: red sentence, red review node, Merge still the button, the why chip", async () => {
    const { WorkPanel } = await import("../app/projects/work-panel");
    const w = await load();
    await mount(<WorkPanel ctx={ctx as never} w={w} story={null} deployments={null} testid="wp" />);
    const sentence = $('[data-testid="wp-sentence"]')!;
    expect(sentence.textContent).toContain("Checks failing.");
    expect(sentence.textContent).not.toMatch(/ready to merge|built, tested and reviewed/i);
    expect(sentence.querySelector("b")!.getAttribute("data-tone")).toBe("red");
    expect($('[data-testid="wp-node-review"]')!.getAttribute("data-s")).toBe("failed");
    expect($('[data-testid="wp-merge"]')!.textContent).toContain("Merge change #3");
    expect($$('[data-testid="wp-helm-chip"]').map((c) => c.textContent)[0]).toBe("Why are the checks failing?");
    expect(document.body.textContent).not.toMatch(/ready to merge/i);
  });
});

describe("one record for the card and the panel", () => {
  it("the card's record and the Work record name the same failing gates", async () => {
    const { checksFailingOf } = await import("../app/checks-failing");
    const { parseChecksFailingWork } = await import("../lib/project-experience");
    const card = checksFailingOf({ checks_failing: CARD_RECORD })!;
    const work = parseChecksFailingWork(WORK_RECORD)!;
    expect(new Set(work.gates)).toEqual(new Set(card.gates.map((g) => g.gate)));
    expect(work.security).toBe(card.security);
    // A plain list of gate dicts (the record as stored) parses the same way.
    expect(parseChecksFailingWork({ gates: CARD_RECORD.gates })!.gates).toEqual(["tests_pass", "security_clean"]);
    expect(parseChecksFailingWork({ gates: [] })).toBeNull();
  });
});
