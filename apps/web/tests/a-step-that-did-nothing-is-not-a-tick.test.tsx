import React from "react";
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  DID_NOTHING, RunsView, bucketOf, didNothing, phaseOf, runIsLive, stoppedStep,
  type RunStep,
} from "../app/delivery-workflows-ui";
import { codeOnly } from "./_helpers/source";
import { schemaRunStatuses, standingOf } from "./_helpers/run-standing";

(globalThis as Record<string, unknown>).React = React;

/**
 * What the operator SEES for a step is what happened — review of c8de36b, 2026-10-02.
 *
 * "Publish to a channel" showed a green ✓ when nothing was published. A bare social_publish,
 * notify or video_edit has no tool behind it; the runner records an honest `status: recorded`
 * no-op and the engine's test said that "must not read as work done" — but it checked only the
 * engine's output, and the console mapped every `succeeded` step to ✓.
 */
const OUTCOMES = readFileSync("../../engine/boss_engine/services/workflow_outcomes.py", "utf8");
const UI = codeOnly("app/delivery-workflows-ui.tsx");

/** The capabilities that commission nothing, read from the engine — not listed here. */
const COMMISSIONS_NOTHING = [...OUTCOMES.matchAll(/"([a-z_]+)": commissions_nothing/g)]
  .map((m) => m[1]);

function step(over: Partial<RunStep> = {}): RunStep {
  return { step_id: "s", kind: "capability", status: "succeeded", capability: "notify",
           output: {}, evidence: [], error: null, approval: {}, due_at: null,
           timeout_applied: null, attempt: 1, started_at: null, completed_at: null,
           decision_id: null, ...over } as unknown as RunStep;
}

function runOf(status: string, steps: RunStep[]) {
  return { id: "wfr_1", status, standing: standingOf(status), workflow_key: "k",
           workflow_name: "W", version: 1, steps, step_count: steps.length, steps_done: 0,
           current_step: null, waiting_on: null, blocked_reason: null, error: null,
           updated_at: null, actions: [], input: { title: "Lead triage" } };
}

const noop = () => {};
function view(r: ReturnType<typeof runOf>) {
  return renderToStaticMarkup(
    <RunsView runs={[r] as never} openRun={r.id} setOpenRun={noop} onAdvance={noop}
              busy={null} capabilities={[]} onRetryStep={noop} onSkipStep={noop}
              onRerun={noop} onStop={noop} onDelete={noop} onStopAndRerun={noop}
              onBuildOnThis={noop} onRecheck={noop} onSetTarget={noop}
              onPrepareTarget={noop} onRunPreview={noop} onBound={noop} projectId="p"
              actor="ceo" roles={[]} targets={[]} hosts={[]} />);
}

describe("a step that did nothing does not read as work done", () => {
  it("the engine's no-op capabilities are found (the enumeration is not empty)", () => {
    // REWRITTEN (2026-10-02): `notify` is no longer one of them. It was a `recorded` no-op that
    // ended three catalogue templates by telling nobody; the runner now sends it, skips it
    // visibly (`not_applicable`, drawn as skipped) or fails it with the fix.
    expect(COMMISSIONS_NOTHING.sort()).toEqual(expect.arrayContaining(["social_publish",
                                                                       "video_edit"]));
    expect(COMMISSIONS_NOTHING).not.toContain("notify");
    expect(OUTCOMES).toContain('"notify": notified');
  });

  it("a notify with no channel reads as skipped, with its reason, not as ✓", () => {
    const s = step({ capability: "notify",
                     output: { status: "not_applicable", posted: false,
                               note: "Nobody was told: this run was given no channel." } });
    expect(phaseOf(s)).toBe("skipped");
  });

  for (const capability of COMMISSIONS_NOTHING) {
    it(`a bare ${capability} that recorded nothing is not ✓, and says what to do`, () => {
      const s = step({ step_id: capability, capability,
                       output: { status: "recorded", note: "no tool performs this" } });
      expect(didNothing(s)).toBe(true);
      expect(phaseOf(s)).toBe("nothing");
      const html = view(runOf("completed", [s]));
      expect(html).toContain('data-phase="nothing"');
      expect(html).toContain('data-testid="dw-node-did-nothing"');
      expect(html).toContain(DID_NOTHING);
      // The step's own node — not the Agents node before it, which is always complete.
      expect(html).not.toMatch(/<li class="dw-node" data-phase="done"[^>]*data-testid="dw-node"/);
      expect(html).toMatch(/<li class="dw-node" data-phase="nothing"[^>]*data-testid="dw-node"/);
    });
  }

  it("real work that succeeded is still ✓", () => {
    expect(phaseOf(step({ capability: "build", output: { status: "succeeded" } }))).toBe("done");
  });
});

describe("leftovers from the legacy rewrite read as what they were", () => {
  it("a rewritten failed_continued row reads 'failed — went on', not as a fresh stop", () => {
    // db/schema.sql rewrote these to status 'failed', output.status 'failed',
    // recorded_as 'failed_continued'. Keyed on output.status alone, this could never match.
    const legacy = step({ status: "failed", capability: "research",
                          output: { status: "failed", recorded_as: "failed_continued" } });
    expect(phaseOf(legacy)).toBe("failed_on");
  });

  it("an operator's skip says who skipped it on the step's own badge, as on the rail", () => {
    const at = UI.indexOf('data-testid={phase === "failed_on" ? "dw-step-failed-on"');
    expect(at).toBeGreaterThan(-1);
    expect(UI.slice(at, at + 900)).toContain('phase === "skipped" ? (skippedBy(step) || "skipped")');
  });
});

describe("a run is filed where the SERVER says it stands", () => {
  it("the console keeps no list of run states of its own", () => {
    // LIVE, a closed list and ACTIVE_RUN_STATES were three private copies of engine sets; LIVE
    // left out paused and compensating (review of 0fd6c45).
    expect(UI).not.toMatch(/const LIVE\b/);
    expect(UI).not.toContain("ACTIVE_RUN_STATES");
    expect(UI).not.toContain('["cancelled", "completed", "compensated"]');
  });

  for (const status of schemaRunStatuses()) {
    it(`a ${status} run is bucketed by its standing`, () => {
      const r = runOf(status, [step({ status: "succeeded", capability: "build" })]);
      const bucket = bucketOf(r);
      if (standingOf(status) === "stopped" || status === "blocked" || status === "cancelled") {
        expect(bucket).toBe("blocked");
      } else if (standingOf(status) === "active" || standingOf(status) === "compensating") {
        expect(bucket).toBe("running");
        expect(runIsLive(r)).toBe(true);
      } else {
        expect(bucket).toBe("done");
      }
    });
  }

  it("a paused run, which the server offers Stop on, is not filed under Done", () => {
    expect(bucketOf(runOf("paused", []))).toBe("running");
    expect(bucketOf(runOf("compensating", []))).toBe("running");
  });

  it("a fan-out branch an 'any' join went on without is a visible failure, not a stop", () => {
    const tolerated = step({ status: "deadlettered", capability: "build",
                             output: { tolerated_by_join: "join_scope_results" } });
    expect(stoppedStep(runOf("running", [tolerated]))).toBeNull();
    expect(phaseOf(tolerated)).toBe("failed");
  });
});
