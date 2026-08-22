import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { pickBuiltRun } from "../app/code-tasks-ui";
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
    ["JourneySpine", /<JourneySpine\s+run=\{builtRun as never\}/],
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
