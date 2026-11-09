// @vitest-environment happy-dom
/**
 * The autonomy metric per run (docs/product/BOSS_FIXES_ITSELF.md §12): the run view's header says
 * "3 problems · BOSS solved 2 · you solved 1", with what each class means in its tooltip, and says
 * nothing for a run that had no problems.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { setConsoleLive } from "../../lib/console-live";
import { mountAs, tid, unmount } from "../_helpers/multirepo-fixtures";
import { fakeSelfFixApi, newState, runAutonomy } from "./_fixtures";

const { RunAutonomy, runAutonomyLine } = await import("../../app/self-fix/run-autonomy");

afterEach(unmount);

async function open(problems: number) {
  const calls = fakeSelfFixApi(newState({ run_autonomy: runAutonomy(problems) }));
  setConsoleLive(true);
  await mountAs(<RunAutonomy projectId="p1" runId="wfr_f383649a8b024d97a80c" live />, "owner");
  return calls;
}

describe("a run says who solved its problems", () => {
  it("draws the run's line, with the metric explained in its tooltip", async () => {
    const calls = await open(3);
    expect(calls[0].search).toContain("view=run_autonomy");
    expect(calls[0].search).toContain("run_id=wfr_f383649a8b024d97a80c");
    const line = tid("dw-run-autonomy")!;
    expect(line.textContent).toBe("3 problems · BOSS solved 2 · you solved 1");
    const tip = line.getAttribute("title") ?? "";
    expect(tip).toContain("approval counts as governance");
    expect(tip).toContain("Outside agent");
    expect(tip).toContain("BOSS solved 67%");
  });

  it("says nothing for a run with no problems", async () => {
    await open(0);
    expect(tid("dw-run-autonomy")).toBeNull();
  });

  it("builds the line from the counts when the API sends no sentence", () => {
    const v = runAutonomy(3);
    expect(runAutonomyLine({ ...v, sentence: "", counts: { problems: 4, by_boss: 1, by_person: 1, by_outside_agent: 1, open: 1 } }))
      .toBe("4 problems, BOSS solved 1, you solved 1, outside agent 1, 1 open");
  });
});
