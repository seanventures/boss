// @vitest-environment happy-dom
import { describe, expect, it, vi } from "vitest";

/**
 * A DEPLOY WAITING FOR ITS MERGE NEEDS YOU (helpdesk-ai, 2026-11-06).
 *
 * The deploy step now waits until its pull request is merged (runner._merge_wait). Its merge
 * question may already be APPROVED — BOSS does not merge on every install — and the merge itself
 * is still the person's to do in GitHub, so the step needs them until GitHub shows it merged.
 */
vi.mock("../app/press-relay", () => ({
  runPressedAction: vi.fn(async (_p: unknown, a: (...x: unknown[]) => Promise<unknown>, ...args: unknown[]) => a(...args)),
}));
const { needsYou, decidedButNotMoved, waitsForMerge, laneStateOf } = await import("../app/delivery-workflows-ui");

type Step = Parameters<typeof needsYou>[0];
const held = (decisionStatus: string | null): Step => ({
  step_id: "deploy", kind: "capability", status: "waiting", decision_id: "dec_m",
  decision_status: decisionStatus,
  output: { await: { kind: "merge", id: "crun_1" },
            merge: { pull_request_number: 3, pull_request_url: "https://github.com/a/b/pull/3" } },
} as unknown as Step);

describe("a deploy held for its merge", () => {
  it("needs you while the merge question is open", () => {
    expect(waitsForMerge(held("pending"))).toBe(true);
    expect(needsYou(held("pending"))).toBe(true);
  });
  it("still needs you once the question is approved but the PR is not merged", () => {
    expect(needsYou(held("approved"))).toBe(true);
    expect(decidedButNotMoved(held("approved"))).toBe("");
    expect(laneStateOf(held("approved")).phase).toBe("needs_you");
  });
  it("is an ordinary step once it is no longer waiting", () => {
    expect(waitsForMerge({ ...held("approved"), status: "succeeded" } as Step)).toBe(false);
  });
});
