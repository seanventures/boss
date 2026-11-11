/**
 * The Workflow screen's buttons work on an install with API auth on, and it speaks in names.
 *
 * boss-1 runs with BOSS_REQUIRE_API_AUTH=true and the console key bound to principal `ceo`.
 * "Advance now" posted to /v1/delivery-workflows/run-capabilities, which only the worker
 * principal may call, so it answered 403 there every time. The builder told operators to open
 * "the Agents page" (the rail says Agent work) and offered raw role keys — "release_manager" —
 * as the people a step could be given to.
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";
// Loaded once, at collection, like the other screen tests: this module is large.
import { LIVE_RUN_STATUSES, runsToRecheckInBulk } from "../app/delivery-workflows-ui";

const UI = codeOnly("app/delivery-workflows-ui.tsx");

describe("the operator's 'move things along' button", () => {
  it("re-checks each unfinished run instead of calling the worker-only pump", () => {
    // Wrapped in the press (main's 45e7693: one press, one effect) at integration, so the
    // handler runs from `onAdvance=` to the end of its pressOn call.
    const at = UI.indexOf("onAdvance={() => void pressOn(\"Advance\"");
    expect(at).toBeGreaterThan(-1);
    const handler = UI.slice(at, UI.indexOf("})}", at));
    expect(handler).not.toContain('post("advance"');
    expect(handler).toContain('post("recheck"');
    // And it says what happened, including when nothing could move.
    expect(handler).toContain("back in the queue");
    expect(handler).toContain("Nothing could move yet");
  });

  it("re-checks only live runs, never a failed one from long ago, and says how many", () => {
    // It re-checked every run not completed, cancelled or compensated: failed and
    // dead-lettered runs included. recheck_run sets a run back to running when its blocker has
    // cleared, so one click could revive old failed work across the project.
    const live = runsToRecheckInBulk([
      { id: "a", status: "running" }, { id: "b", status: "waiting" },
      { id: "c", status: "blocked" }, { id: "d", status: "failed" },
      { id: "e", status: "deadlettered" }, { id: "f", status: "completed" },
      { id: "g", status: "queued" }, { id: "h", status: "cancelled" },
    ]);
    expect(live.map((r) => r.id)).toEqual(["a", "b", "c", "g"]);
    for (const status of LIVE_RUN_STATUSES) {
      expect(["failed", "deadlettered", "completed", "cancelled", "compensated"])
        .not.toContain(status);
    }
    // Wrapped in the press (main's 45e7693: one press, one effect) at integration, so the
    // handler runs from `onAdvance=` to the end of its pressOn call.
    const at = UI.indexOf("onAdvance={() => void pressOn(\"Advance\"");
    expect(at).toBeGreaterThan(-1);
    const handler = UI.slice(at, UI.indexOf("})}", at));
    expect(handler).toContain("runsToRecheckInBulk(runs)");
    expect(handler).toContain("live run");
  });

  it("is labelled for what it does", () => {
    expect(UI).toContain("Check every run again");
    expect(UI).not.toContain("Advance now");
  });
});

describe("names, not role keys", () => {
  it("points at the area the rail actually has", () => {
    // The builder's "Open it in Agent work" left with the builder; whatever this area says
    // about agents names the area the rail has now — Agent Studio — and never a retired one.
    expect(UI).not.toContain("Agents page");
    for (const retired of ["Agent work", "Team structure"]) {
      expect(UI).not.toContain(retired);
    }
  });

  it("a run waiting on a role says the role's name", () => {
    expect(UI).toContain("roleLabel(run.waiting_on)");
  });
});

it("the empty Runs tab says how to start, without jargon", () => {
  // REWRITTEN for the New project tab: with no runs the area opens on New project, so the
  // empty line is short, the how-to is its tooltip (labels, not paragraphs), and one button
  // goes to the tab.
  expect(UI).not.toContain("apply a workflow to a class of work");
  expect(UI).toContain('title="Start one with + New build (Add a project), or press Start on a workflow in the Library"');
  expect(UI).toContain('data-testid="dw-empty-new"');
});
