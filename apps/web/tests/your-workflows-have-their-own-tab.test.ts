import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { groupLibrary, scheduleDraftFor } from "../app/workflows/library";
import type { WorkflowSummary } from "../lib/workflow-editor-types";

/**
 * A customer's own workflows sat under the twelve BOSS ships, so the thing somebody built was
 * filed beneath the things they did not. REWRITTEN for Workflow Studio: there are no Yours and
 * Catalogue tabs any more — the Library puts yours first and the standards under a divider, and
 * a schedule is set from a card's ⋯ menu. The behaviour is pinned in
 * tests/workflow-library.test.tsx; this keeps the rules that are not about clicks.
 */
const library = readFileSync(join(__dirname, "../app/workflows/library.tsx"), "utf8");
const area = readFileSync(join(__dirname, "../app/delivery-workflows-ui.tsx"), "utf8");
const proxy = readFileSync(join(__dirname, "../app/api/delivery-workflows/route.ts"), "utf8");

const wf = (over: Partial<WorkflowSummary>): WorkflowSummary => ({
  definition_id: `def_${over.workflow_key}`, workflow_key: "k", name: "K", is_catalogue: false,
  version: 1, step_count: 1, steps: [], run_count: 0, ...over,
});

describe("your workflows come first", () => {
  it("and copies piling up behind one source collapse onto it", () => {
    const { yours, standards } = groupLibrary([
      wf({ workflow_key: "spd", name: "SPD", is_catalogue: true }),
      wf({ workflow_key: "spd_copy", name: "SPD (copy)", forked_from_workflow_key: "spd" }),
      wf({ workflow_key: "spd_copy_2", name: "SPD (copy) (copy)", forked_from_workflow_key: "spd" }),
      wf({ workflow_key: "mine", name: "Mine" }),
    ]);
    expect(yours.map((e) => e.item.workflow_key)).toEqual(["mine"]);
    expect(standards[0].copies.map((c) => c.workflow_key)).toEqual(["spd_copy", "spd_copy_2"]);
  });

  it("but your ONE copy of a standard stays yours", () => {
    const { yours, standards } = groupLibrary([
      wf({ workflow_key: "spd", name: "SPD", is_catalogue: true }),
      wf({ workflow_key: "spd_mine", name: "SPD (mine)", forked_from_workflow_key: "spd" }),
    ]);
    expect(yours.map((e) => e.item.workflow_key)).toEqual(["spd_mine"]);
    expect(standards[0].copies).toEqual([]);
  });
});

describe("a workflow of your own can be scheduled", () => {
  it("only once there is a published version to run", () => {
    // The API refuses a schedule pointing at a workflow with no version.
    expect(library).toContain('disabled={!w.version}');
    expect(library).toContain('onClick={() => pop.setOpen("schedule")}>Schedule…</button>');
  });

  it("asks for the time zone rather than guessing, starting from the browser's", () => {
    const draft = scheduleDraftFor({ workflow_key: "k", name: "K" });
    expect(draft.timezone).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
    expect(library).toContain('data-testid="wf-schedule-zone"');
    expect(library).not.toContain("window.prompt");
  });

  it("an existing schedule is edited with what it was saved with", () => {
    const draft = scheduleDraftFor({ workflow_key: "k", name: "K" }, {
      id: "s1", workflow_key: "k", name: "K weekly", describes: "Mondays 08:00",
      enabled: true, next_run_at: null, last_run_at: null, last_error: null,
      timezone: "Asia/Kolkata", input: { sectors: "fintech", count: 3 }, start_anyway: ["host"],
    });
    expect(draft).toMatchObject({
      scheduleId: "s1", name: "K weekly", timezone: "Asia/Kolkata",
      values: { sectors: "fintech", count: "3" }, startAnyway: ["host"],
    });
  });

  it("stores declared inputs with the schedule", () => {
    // The button used to post a time zone and nothing else.
    expect(area).toContain("REQUIRED INPUTS ARE STORED WITH THE SCHEDULE");
    expect(area).toContain("missingRequired(fields, draft.values)");
    expect(area).toContain("input: suppliedInputs(fields, draft.values)");
    expect(area).toContain("schedule_id: draft.scheduleId");
  });

  it("shows the schedule in the API's own words, and a failing one says so", () => {
    expect(library).toContain("{s.describes}");
    expect(library).toContain("Last run failed: ${s.last_error}");
  });

  it("routes pause, resume and remove through the proxy", () => {
    expect(proxy).toContain("SCHEDULE_ACTIONS");
    expect(proxy).toContain("schedules/${encodeURIComponent(id)}/enabled");
    expect(proxy).toContain("schedule_id");
  });

  it("does not fail the whole screen when schedules cannot be read", () => {
    expect(area).toContain("SCHEDULES ARE NOT LOAD-BEARING");
  });
});
