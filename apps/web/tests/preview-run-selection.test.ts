import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { latestPreviewFor, pickBuiltRun } from "../app/code-tasks-ui";
import { describeApiFailure } from "@boss/api-client";

/**
 * Regression: "Run preview" on an app card returned `BOSS API request failed: 409`, while
 * previewing the same app from inside its code task worked. Two independent defects.
 */
describe("pickBuiltRun", () => {
  // The real shape from goldeneye: newest attempt is a blocked follow-up, the one before it
  // completed and opened PR #83.
  const bugbox = [
    { id: "crun_f91b0c5528d240798ec2", status: "blocked", pull_request_url: null },
    { id: "crun_7cc663627f2340dc85ac", status: "completed", pull_request_url: "…/pull/83" },
  ];

  it("skips a blocked newest attempt for the completed one that can be previewed", () => {
    expect(pickBuiltRun(bugbox).id).toBe("crun_7cc663627f2340dc85ac");
  });

  it("does not simply take the newest attempt", () => {
    expect(pickBuiltRun(bugbox).id).not.toBe(bugbox[0].id);
  });

  it("prefers the NEWEST previewable attempt, not merely the first completed one", () => {
    const runs = [
      { id: "newest-blocked", status: "blocked", pull_request_url: null },
      { id: "newer-done", status: "completed", pull_request_url: "…/pull/90" },
      { id: "older-done", status: "completed", pull_request_url: "…/pull/83" },
    ];
    expect(pickBuiltRun(runs).id).toBe("newer-done");
  });

  it("accepts a run with a PR even when its status is not completed", () => {
    const runs = [
      { id: "blocked-with-pr", status: "blocked", pull_request_url: "…/pull/91" },
      { id: "done", status: "completed", pull_request_url: null },
    ];
    expect(pickBuiltRun(runs).id).toBe("blocked-with-pr");
  });

  it("falls back to the newest run when nothing shipped (card hides the action anyway)", () => {
    const runs = [
      { id: "a", status: "blocked", pull_request_url: null },
      { id: "b", status: "failed", pull_request_url: null },
    ];
    expect(pickBuiltRun(runs).id).toBe("a");
  });
});

describe("describeApiFailure", () => {
  // Verbatim from the deployed API for the blocked run.
  const preview409 = {
    detail: {
      error: "preview_unavailable_no_pr",
      message: "This run has not opened a pull request yet, so there is no completed change to preview.",
      run_id: "crun_f91b0c5528d240798ec2",
      run_status: "blocked",
      run_phase: "awaiting_plan_approval",
      next_action: "Wait for the run to finish. If it is blocked on a plan-approval decision, approve it; if it failed, retry the task.",
    },
  };

  it("surfaces the server's explanation instead of the bare status code", () => {
    const msg = describeApiFailure(409, preview409);
    expect(msg).toContain("has not opened a pull request yet");
    expect(msg).toContain("approve it");
    expect(msg).not.toContain("BOSS API request failed");
  });

  it("passes a plain-string detail straight through", () => {
    expect(describeApiFailure(422, { detail: "ssh_key is required" })).toBe("ssh_key is required");
  });

  it("falls back to the error code when there is no prose", () => {
    expect(describeApiFailure(409, { detail: { error: "conflict" } })).toBe("conflict");
  });

  it("still reports something when the body is empty or unparseable", () => {
    expect(describeApiFailure(500, undefined)).toBe("BOSS API request failed: 500");
    expect(describeApiFailure(502, "<html>bad gateway</html>")).toBe("<html>bad gateway</html>");
  });

  it("ignores a blank message rather than returning an empty error", () => {
    expect(describeApiFailure(409, { detail: { message: "   " } })).toBe("BOSS API request failed: 409");
  });
});

/**
 * The twin-bug guard.
 *
 * This defect recurred three times in one card — preview, deploy, and the journey spine each
 * paired an APP-level predicate (`anyShipped`) with a RUN-level action on `members[0]`. The
 * file's own comments warn about "fix one path, miss its twin"; a unit test on the selector
 * cannot catch a caller that simply does not use it, so assert the wiring.
 *
 * Rule: components that CONSUME a built artifact take `builtRun`. Components that CREATE new
 * work (feature/rebuild panels) may key off the newest attempt.
 */
describe("card wiring: artifact consumers use the built run", () => {
  const source = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");

  it.each([
    ["AppPrimaryAction", /<AppPrimaryAction[\s\S]{0,200}?run=\{builtRun\}/],
    ["DeploymentLine", /<DeploymentLine\s+run=\{builtRun\}/],
    // The spine now spreads builtRun so it can carry the GROUP's origin (a follow-up records
    // none of its own, and where an app came from is a property of the app). Still builtRun —
    // the point of this guard is that it is not members[0].
    ["JourneySpine", /<JourneySpine[\s\S]{0,240}?\.\.\.builtRun/],
  ])("%s takes builtRun", (_name, pattern) => {
    expect(source).toMatch(pattern);
  });

  it("no artifact consumer is still wired to members[0]", () => {
    for (const tag of ["AppPrimaryAction", "DeploymentLine", "JourneySpine"]) {
      const open = source.indexOf(`<${tag}`);
      expect(open, `${tag} not found in the card`).toBeGreaterThan(-1);
      // Stop at this element's own closing "/>" — the components that FOLLOW it
      // (RebuildWithSpec, the spec panels) may legitimately key off members[0].
      const call = source.slice(open, source.indexOf("/>", open));
      expect(call, `${tag} must not act on members[0]`).not.toMatch(/run=\{members\[0\]/);
    }
  });
});

/**
 * A failed preview must not render as a starting one.
 *
 * Five consecutive preview failures on one run showed as the pending label "Starting
 * preview…", so the operator reported the preview as stuck. AppPrimaryAction had branches for
 * live / starting / building / nothing-built, and none for `failed`.
 */
describe("latestPreviewFor", () => {
  const previews = [
    { id: "prev_new", run_id: "crun_a", status: "failed", error: "app did not serve HTTP" },
    { id: "prev_old", run_id: "crun_a", status: "expired" },
    { id: "prev_other", run_id: "crun_b", status: "running" },
  ] as any[];

  it("returns the newest attempt for the run", () => {
    expect(latestPreviewFor(previews, "crun_a")?.id).toBe("prev_new");
  });

  it("does not leak another run's preview", () => {
    expect(latestPreviewFor(previews, "crun_b")?.id).toBe("prev_other");
  });

  it("returns null when the run has never been previewed", () => {
    expect(latestPreviewFor(previews, "crun_none")).toBeNull();
  });
});

describe("card renders a failed preview as failed", () => {
  const source = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");

  it("has a failed branch, not only starting/live/building", () => {
    expect(source).toMatch(/lastPreview\?\.status === "failed"/);
  });

  it("offers a retry rather than a dead button", () => {
    expect(source).toContain("Retry preview");
  });

  it("shows the failure reason so a retry is not blind", () => {
    expect(source).toMatch(/data-testid="preview-failed"/);
    expect(source).toMatch(/failed\.error/);
  });

  it("passes the newest preview into the action", () => {
    expect(source).toMatch(/lastPreview=\{latestPreviewFor\(previews, builtRun\.id\)\}/);
  });
});

/**
 * A request with no deadline is a spinner with no end.
 *
 * The Discovery "Send to PRD meeting" button sat on "Convening…" indefinitely. The action was
 * written correctly — try/catch, returning {taskId} or {error} either way — but it was never
 * reached, because the fetch underneath had no timeout. When the API went cold (a first query
 * on goldeneye measured 28.9s) the await simply never returned and `pending` never cleared.
 */
describe("api client request deadline", () => {
  const source = readFileSync(
    resolve(__dirname, "../../../packages/api-client/src/index.ts"), "utf8");

  it("every request carries an abort deadline", () => {
    expect(source).toMatch(/signal:\s*AbortSignal\.timeout\(timeoutMs\)/);
  });

  it("the deadline is configurable but never unbounded by default", () => {
    expect(source).toMatch(/config\.timeoutMs\s*\?\?\s*120_000/);
  });

  it("a timeout becomes a BossApiError the UI can render, not a hang", () => {
    expect(source).toContain('cause.name === "TimeoutError"');
    expect(source).toContain("did not answer within");
  });

  it("says the work may still be in flight rather than implying it failed", () => {
    expect(source).toContain("It may still be working");
  });
});
