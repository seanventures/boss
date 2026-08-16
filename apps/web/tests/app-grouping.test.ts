import { describe, expect, it } from "vitest";

import { groupRunsIntoApps } from "../app/app-grouping";

/**
 * ApproveIt appeared three times: the QA-benchmark attempts, a change started as its own task,
 * and its follow-up. Each carried a DIFFERENT identity field, and the old first-match chain
 * picked one per run, so runs of one app never met.
 */
const run = (id: string, spec: unknown, fctx: Record<string, unknown> = {}) =>
  ({ id, spec, finalize_ctx: fctx }) as never;

describe("runs of one app group together", () => {
  it("joins a change to the app whose branch it was built on", () => {
    const groups = groupRunsIntoApps([
      run("crun_a", { base_branch: "main" }, { qa_benchmark: { id: "approveit" } }),
      run("crun_b", { base_branch: "boss/code/crun_a" }),
    ]);
    expect(groups).toHaveLength(1);
  });

  it("joins a chain: benchmark -> change -> follow-up", () => {
    const groups = groupRunsIntoApps([
      run("crun_a", { base_branch: "main" }, { qa_benchmark: { id: "approveit" } }),
      run("crun_b", { base_branch: "boss/code/crun_a" }),
      run("crun_c", { base_branch: "boss/code/crun_b" }, { root_run_id: "crun_b" }),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].map((r) => r.id)).toEqual(["crun_a", "crun_b", "crun_c"]);
  });

  it("joins two attempts at one benchmark started weeks apart", () => {
    const groups = groupRunsIntoApps([
      run("crun_a", { base_branch: "main" }, { qa_benchmark: { id: "approveit" } }),
      run("crun_z", { base_branch: "main" }, { qa_benchmark: { id: "approveit" } }),
    ]);
    expect(groups).toHaveLength(1);
  });

  it("merges through a shared app_id even when nothing else matches", () => {
    const groups = groupRunsIntoApps([
      run("crun_a", { base_branch: "main" }, { app_id: "app_1" }),
      run("crun_b", { base_branch: "main" }, { app_id: "app_1" }),
    ]);
    expect(groups).toHaveLength(1);
  });

  it("keeps genuinely different apps apart", () => {
    const groups = groupRunsIntoApps([
      run("crun_a", { base_branch: "main" }, { qa_benchmark: { id: "approveit" } }),
      run("crun_x", { base_branch: "main" }, { qa_benchmark: { id: "bugbox" } }),
    ]);
    expect(groups).toHaveLength(2);
  });

  it("keeps a from-scratch rebuild apart: a name is not an identity", () => {
    const groups = groupRunsIntoApps([
      run("crun_a", { base_branch: "main" }),
      run("crun_b", { base_branch: "main" }),
    ]);
    expect(groups).toHaveLength(2);
  });

  it("preserves the order apps first appear in", () => {
    const groups = groupRunsIntoApps([
      run("crun_x", { base_branch: "main" }, { qa_benchmark: { id: "bugbox" } }),
      run("crun_a", { base_branch: "main" }, { qa_benchmark: { id: "approveit" } }),
      run("crun_b", { base_branch: "boss/code/crun_a" }),
    ]);
    expect(groups.map((g) => g[0].id)).toEqual(["crun_x", "crun_a"]);
  });
});
