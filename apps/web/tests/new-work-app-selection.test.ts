import { describe, expect, it } from "vitest";
import { parsePlanDetail, planDetailPath, workBody } from "../lib/new-work";

describe("New work application selection", () => {
  it("keeps the server's choices and inferred anchor", () => {
    const plan = parsePlanDetail({ app_choices: [{ run_id: "crun_app", app: "Existing" }],
      anchor: { run_id: "crun_app", app: "Existing", branch: "boss/code/crun_root", mode: "continue" } });
    expect(plan.appChoices).toEqual([{ runId: "crun_app", app: "Existing" }]);
    expect(plan.anchor?.runId).toBe("crun_app");
  });

  it("sends an explicit new-app choice in both plan and start", () => {
    expect(planDetailPath("project", "feature", "", "", true)).toContain("new_app=true");
    expect(workBody("feature", "Build another app", {
      brief: null, priority: "normal", notify: "", issue: { mode: "none" }, new_app: true,
    })).toHaveProperty("new_app", true);
  });
});
