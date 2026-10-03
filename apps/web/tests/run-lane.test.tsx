import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  DID_NOTHING, RunsView, laneStateOf, type RunStep,
} from "../app/delivery-workflows-ui";
import { flowInto } from "../app/workflows/flow/flow-lane";
import { runPalette, runSpec } from "../app/workflows/flow/run-lane";
import { standingOf } from "./_helpers/run-standing";

(globalThis as Record<string, unknown>).React = React;

/**
 * A run is drawn by the same lane the designer draws: one row that never wraps, every step
 * named the way the editor names it, and where the work has got to on each node in words, a
 * mark and a colour. Rendered for real (RunsView, server markup), so what is asserted is what
 * the Runs tab shows.
 */

function step(over: Partial<RunStep>): RunStep {
  return { step_id: "s", kind: "capability", status: "succeeded", capability: "research",
           output: {}, evidence: [], error: null, approval: {}, due_at: null,
           timeout_applied: null, attempt: 1, started_at: null, completed_at: null,
           decision_id: null, ...over } as unknown as RunStep;
}

const STEPS: RunStep[] = [
  step({ step_id: "research", capability: "research", started_at: "2026-10-02T10:00:00Z",
         completed_at: "2026-10-02T10:05:43Z" }),
  step({ step_id: "meeting", capability: "meeting", status: "skipped",
         output: { status: "not_applicable", note: "No research phase: started directly" } }),
  step({ step_id: "notify", capability: "notify", status: "skipped",
         output: { skipped_by_operator: true, skipped_by: "ceo", skip_reason: "out of budget" } }),
  step({ step_id: "video_edit", capability: "video_edit", output: { status: "recorded" } }),
  step({ step_id: "review", capability: "review", status: "failed",
         output: { status: "failed", recorded_as: "failed_continued" } }),
  step({ step_id: "deploy", capability: "deploy", status: "waiting", decision_id: "dec_1",
         decision_status: "pending" }),
  step({ step_id: "monitor", capability: "monitor", status: "pending" }),
];

function runOf(steps: RunStep[], status = "waiting") {
  return { id: "wfr_1", status, standing: standingOf(status), workflow_key: "k", workflow_name: "W",
           version: 1, steps, step_count: steps.length, steps_done: 0, current_step: null,
           waiting_on: null, blocked_reason: null, error: null, updated_at: null, actions: [],
           input: { title: "Lead triage" } };
}

const noop = () => {};
function view(steps: RunStep[], status?: string) {
  const r = runOf(steps, status);
  return renderToStaticMarkup(
    <RunsView runs={[r] as never} openRun={r.id} setOpenRun={noop} onAdvance={noop}
              busy={null} capabilities={[{ key: "research", label: "Research" }, { key: "deploy", label: "Deploy" }] as never}
              onRetryStep={noop} onSkipStep={noop} onRerun={noop} onStop={noop} onDelete={noop}
              onStopAndRerun={noop} onBuildOnThis={noop} onRecheck={noop} onSetTarget={noop}
              onPrepareTarget={noop} onRunPreview={noop} onBound={noop} projectId="p"
              actor="ceo" roles={[]} targets={[]} hosts={[]} />);
}

/** The node for a step, as markup. */
function nodeOf(html: string, id: string): string {
  const at = html.indexOf(`data-step-id="${id}"`);
  expect(at).toBeGreaterThan(-1);
  const start = html.lastIndexOf("<button", at);
  return html.slice(start, html.indexOf("</button>", at) + 9);
}

describe("each step says where the work has got to", () => {
  const html = view(STEPS);

  it("done, with its time and a tick on the icon", () => {
    const n = nodeOf(html, "research");
    expect(n).toContain('data-phase="done"');
    expect(n).toContain("5m 43s");
    expect(n).toContain('data-testid="wf-node-mark"');
    expect(n).toContain(">Research<");
  });

  it("a phase that never happened is skipped, with the recorded reason as its tooltip", () => {
    const n = nodeOf(html, "meeting");
    expect(n).toContain('data-phase="skipped"');
    expect(n).toContain(">skipped<");
    expect(n).toContain('data-tip="No research phase: started directly"');
    expect(n).not.toContain("not run");
  });

  it("an operator's skip says who and why, unlike a branch never taken", () => {
    expect(nodeOf(html, "notify")).toContain("skipped by ceo: out of budget");
  });

  it("a step that did nothing is never drawn as done, and says what to do", () => {
    const n = nodeOf(html, "video_edit");
    expect(n).toContain('data-phase="skipped"');
    expect(n).toContain("nothing done");
    expect(n).toContain(DID_NOTHING);
  });

  it("a step that failed while the run went on says so in words, and the line carries on past it", () => {
    const n = nodeOf(html, "review");
    expect(n).toContain('data-phase="failed"');
    expect(n).toContain("failed — went on");
    expect(flowInto("failed", "needs_you")).toBe("moving");
  });

  it("a gate waiting for you says needs you, and is the step in flight", () => {
    const n = nodeOf(html, "deploy");
    expect(n).toContain('data-phase="needs_you"');
    expect(n).toContain("needs you");
  });

  it("what is ahead is pending, and the line into it has not been crossed", () => {
    expect(nodeOf(html, "monitor")).toContain('data-phase="future"');
    expect(flowInto("needs_you", "future")).toBe("future");
  });

  it("names each step the way the designer does", () => {
    expect(nodeOf(html, "deploy")).toContain(">Deploy<");
    // No label on the run: the id, made readable — never the raw id.
    expect(nodeOf(html, "video_edit")).toContain(">Video edit<");
  });
});

describe("the run is one row", () => {
  it("draws eleven steps without wrapping", () => {
    const eleven = Array.from({ length: 11 }, (_, i) => step({ step_id: `s${i}`, capability: "research" }));
    const html = view(eleven);
    const tops = [...html.matchAll(/class="wf-slot" data-slot="s\d+" style="[^"]*top:([\d.]+)px/g)].map((m) => m[1]);
    expect(tops).toHaveLength(11);
    expect(new Set(tops).size).toBe(1);
  });

  it("opens on the step that wants attention", () => {
    expect(nodeOf(view(STEPS), "deploy")).toContain('data-selected="true"');
  });
});

describe("who is on the run", () => {
  it("is a chip above the lane, not a step in it", () => {
    const html = view(STEPS);
    const chip = html.indexOf('data-testid="dw-run-agents"');
    expect(chip).toBeGreaterThan(-1);
    expect(chip).toBeLessThan(html.indexOf('data-testid="wf-lane"'));
    expect(html).not.toContain('data-step-id="__agents__"');
    expect(runSpec([step({ step_id: "a" })]).steps.map((s) => s.id)).toEqual(["a"]);
  });
});

describe("the adapter", () => {
  it("keeps the run's order and names agents from the roster", () => {
    const spec = runSpec([step({ step_id: "a", captain: "qa" }), step({ step_id: "b" })]);
    expect(spec.entry_step).toBe("a");
    expect(spec.steps[0].next).toEqual(["b"]);
    expect(spec.steps[1].next).toBeUndefined();
    expect(runPalette([], [{ role_key: "qa", display_name: "QA Lead" }]).agents[0].name).toBe("QA Lead");
  });

  it("maps every run phase to a lane phase", () => {
    expect(laneStateOf(step({ status: "running" })).phase).toBe("moving");
    expect(laneStateOf(step({ status: "failed" })).phase).toBe("failed");
    expect(laneStateOf(step({ status: "pending" }))).toEqual({ phase: "future", meta: "pending" });
  });
});
