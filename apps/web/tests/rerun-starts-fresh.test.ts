import { describe, expect, it } from "vitest";

import { inputForRerun } from "../app/delivery-workflows-ui";

/**
 * "Run this workflow again" copied the previous run's input whole — including the `adopt_*`
 * keys that tell the runtime "this work already happened, go and watch it". So re-running a
 * project adopted the same finished build: research recorded as not-run, the old panel and the
 * old approval adopted, and `build` green in seconds against code written the day before.
 *
 * That is a re-observation, not a re-run.
 */
describe("re-running a workflow", () => {
  const adopted = {
    title: "Dependency Freshness Report",
    directive: "Build a dependency freshness report.",
    repository: "example-user/boss-sandbox",
    target_id: "deptgt_abc",
    adopt_code_task_run_id: "crun_d9f30e31",
    adopt_meeting_run_id: "mrun_76f07c8b",
    adopt_decision_id: "dec_9789",
  };

  it("drops every claim that the work already happened", () => {
    const next = inputForRerun(adopted);
    for (const key of Object.keys(next)) {
      expect(key.startsWith("adopt_")).toBe(false);
    }
  });

  it("keeps what the work IS and where it ships", () => {
    // Dropping the directive would start an empty run; dropping the target would ask again
    // for a decision somebody already made.
    const next = inputForRerun(adopted);
    expect(next.directive).toBe("Build a dependency freshness report.");
    expect(next.title).toBe("Dependency Freshness Report");
    expect(next.repository).toBe("example-user/boss-sandbox");
    expect(next.target_id).toBe("deptgt_abc");
  });

  it("leaves a run that adopted nothing exactly as it was", () => {
    // A run started from Command Center or the composer already researches and builds its own
    // work; re-running it must not change its shape.
    const fresh = { title: "A decision log", directive: "Build a decision log." };
    expect(inputForRerun(fresh)).toEqual(fresh);
  });

  it("survives a run with no input at all", () => {
    expect(inputForRerun(null)).toEqual({});
    expect(inputForRerun(undefined)).toEqual({});
  });
});
